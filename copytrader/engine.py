"""SKLZ CopyTrader — the copy engine.

Pure decision logic: given a leader's trade and a follower's configuration,
decide whether to copy, at what size, or why not. No network calls live here
so every rule is unit-testable.

Follower-facing simplicity is the product promise: a follower chooses only
a RISK LEVEL and an ALLOCATION. Everything below is derived.

Safety principles:
  - allocation is a hard ceiling; funds outside it are never touched
  - every guard fails CLOSED (on doubt, do not trade)
  - order ids are deterministic, so a retry can never double-fill
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone

# risk level -> fraction of allocation committed to a single trade
RISK_LEVELS = {
    "low":    0.05,
    "medium": 0.10,
    "high":   0.20,
}
MAX_CUSTOM_RISK = 0.35          # even "custom" cannot exceed this


@dataclass
class FollowerConfig:
    follower_id: str
    leader_id: str
    allocation: float                       # capital committed, in quote ccy
    risk_level: str = "medium"              # low | medium | high | custom
    custom_risk_pct: float | None = None    # used when risk_level == "custom"
    quote: str = "USDT"
    max_open_positions: int = 10
    max_exposure_per_asset: float = 0.30    # fraction of allocation
    max_daily_loss: float = 0.10            # fraction of allocation
    blacklist: list[str] = field(default_factory=list)
    whitelist: list[str] = field(default_factory=list)   # empty = allow all
    paused: bool = False


@dataclass
class FollowerState:
    free_quote: float                       # spendable quote balance
    open_positions: int = 0
    exposure_by_asset: dict[str, float] = field(default_factory=dict)
    realized_pnl_today: float = 0.0
    emergency_stopped: bool = False


@dataclass
class LeaderTrade:
    trade_id: str                           # unique, from the leader's fill
    symbol: str                             # e.g. "BTC/USDT"
    side: str                               # buy | sell
    leader_notional: float                  # what the leader spent/received
    leader_equity: float                    # leader account size at the time
    ts: str = ""


@dataclass
class CopyDecision:
    copy: bool
    amount: float = 0.0                     # base units to trade
    notional: float = 0.0                   # quote value
    reason: str = ""
    client_order_id: str = ""
    warnings: list[str] = field(default_factory=list)


def risk_fraction(cfg: FollowerConfig) -> float:
    if cfg.risk_level == "custom":
        pct = cfg.custom_risk_pct or RISK_LEVELS["medium"]
        return max(0.001, min(pct, MAX_CUSTOM_RISK))
    return RISK_LEVELS.get(cfg.risk_level, RISK_LEVELS["medium"])


def client_order_id(trade: LeaderTrade, cfg: FollowerConfig) -> str:
    """Deterministic per (leader trade, follower). A replay produces the same
    id, so the exchange rejects the duplicate instead of filling twice."""
    raw = f"{trade.trade_id}:{cfg.follower_id}:{cfg.leader_id}"
    return "sklz" + hashlib.sha256(raw.encode()).hexdigest()[:24]


def _asset_of(symbol: str) -> str:
    return symbol.split("/")[0] if "/" in symbol else symbol[:-4]


def decide(trade: LeaderTrade, cfg: FollowerConfig, state: FollowerState,
           market: dict | None = None, price: float = 0.0) -> CopyDecision:
    """The single decision point. Returns whether and how much to copy."""
    coid = client_order_id(trade, cfg)
    warn: list[str] = []
    asset = _asset_of(trade.symbol)

    def no(reason: str) -> CopyDecision:
        return CopyDecision(copy=False, reason=reason,
                            client_order_id=coid, warnings=warn)

    # ---- hard stops -------------------------------------------------
    if cfg.paused:
        return no("copying is paused by the follower")
    if state.emergency_stopped:
        return no("emergency stop is active")
    if cfg.allocation <= 0:
        return no("no capital allocated")

    # ---- asset filters ----------------------------------------------
    if cfg.blacklist and asset in cfg.blacklist:
        return no(f"{asset} is blacklisted")
    if cfg.whitelist and asset not in cfg.whitelist:
        return no(f"{asset} is not on the whitelist")

    # ---- daily loss circuit breaker ---------------------------------
    loss_cap = cfg.allocation * cfg.max_daily_loss
    if state.realized_pnl_today <= -abs(loss_cap):
        return no(f"daily loss limit reached "
                  f"({state.realized_pnl_today:.2f} of -{loss_cap:.2f})")

    # ---- sells: close what we actually hold -------------------------
    if trade.side == "sell":
        held = state.exposure_by_asset.get(asset, 0.0)
        if held <= 0:
            return no(f"no {asset} position to close")
        notional = held
        amount = (notional / price) if price else 0.0
        if market:
            amount = _apply_precision(amount, market)
            ok, why = _meets_minimums(amount, notional, market)
            if not ok:
                return no(why)
        return CopyDecision(copy=True, amount=amount, notional=notional,
                            reason="closing position to follow leader exit",
                            client_order_id=coid, warnings=warn)

    # ---- buys: position count + concentration -----------------------
    if state.open_positions >= cfg.max_open_positions:
        return no(f"max open positions reached ({cfg.max_open_positions})")

    # proportional sizing: mirror the leader's conviction, capped by risk level
    leader_weight = (trade.leader_notional / trade.leader_equity) \
        if trade.leader_equity > 0 else risk_fraction(cfg)
    weight = min(leader_weight, risk_fraction(cfg))
    if leader_weight > risk_fraction(cfg):
        warn.append(f"leader risked {leader_weight:.1%}; capped to "
                    f"{risk_fraction(cfg):.1%} by your risk level")

    notional = cfg.allocation * weight

    # concentration guard
    cap = cfg.allocation * cfg.max_exposure_per_asset
    already = state.exposure_by_asset.get(asset, 0.0)
    if already >= cap:
        return no(f"{asset} exposure already at limit "
                  f"({already:.2f} of {cap:.2f})")
    if already + notional > cap:
        notional = cap - already
        warn.append(f"reduced to respect {asset} concentration limit")

    # total allocation ceiling
    committed = sum(state.exposure_by_asset.values())
    room = cfg.allocation - committed
    if room <= 0:
        return no("allocation fully deployed")
    if notional > room:
        notional = room
        warn.append("reduced to remaining allocation")

    # actual spendable cash
    # after cross-exchange translation the currency actually spent may differ
    # from the subscription's configured quote — report the real one
    spend = getattr(state, "spend_quote", None) or cfg.quote
    if notional > state.free_quote:
        notional = state.free_quote
        warn.append(f"reduced to available {spend} balance")
    if notional <= 0:
        return no(f"no free {spend} balance")

    if price <= 0:
        return no("no price available")
    amount = notional / price

    if market:
        amount = _apply_precision(amount, market)
        notional = amount * price
        ok, why = _meets_minimums(amount, notional, market)
        if not ok:
            return no(why)

    return CopyDecision(copy=True, amount=amount, notional=notional,
                        reason="copying leader entry",
                        client_order_id=coid, warnings=warn)


def _apply_precision(amount: float, market: dict) -> float:
    p = market.get("amount_precision")
    if p is None:
        return amount
    try:
        if isinstance(p, int):
            return float(f"{amount:.{p}f}")
        step = float(p)                      # some venues give a step size
        if step > 0:
            return (amount // step) * step
    except (TypeError, ValueError):
        pass
    return amount


def _meets_minimums(amount: float, notional: float, market: dict) -> tuple[bool, str]:
    if not market.get("active", True):
        return False, "market is not active on this exchange"
    if amount <= 0:
        return False, "size rounds to zero at this exchange's precision"
    mn_a = market.get("min_amount")
    if mn_a and amount < float(mn_a):
        return False, (f"below exchange minimum size "
                       f"({amount:.8f} < {float(mn_a):.8f})")
    mn_c = market.get("min_cost")
    if mn_c and notional < float(mn_c):
        return False, (f"below exchange minimum order value "
                       f"({notional:.2f} < {float(mn_c):.2f})")
    return True, ""


def portfolio_health(cfg: FollowerConfig, state: FollowerState) -> dict:
    """Simple, honest read of how exposed a follower currently is."""
    committed = sum(state.exposure_by_asset.values())
    used = (committed / cfg.allocation) if cfg.allocation else 0.0
    loss_cap = cfg.allocation * cfg.max_daily_loss
    loss_used = (abs(min(state.realized_pnl_today, 0)) / loss_cap) if loss_cap else 0.0
    flags = []
    if used > 0.9:
        flags.append("allocation almost fully deployed")
    if loss_used > 0.7:
        flags.append("approaching daily loss limit")
    if state.open_positions >= cfg.max_open_positions:
        flags.append("at maximum open positions")
    if state.emergency_stopped:
        flags.append("EMERGENCY STOP ACTIVE")
    return {
        "allocation": cfg.allocation,
        "deployed": round(committed, 2),
        "deployed_pct": round(used, 4),
        "free": round(max(cfg.allocation - committed, 0), 2),
        "open_positions": state.open_positions,
        "daily_pnl": round(state.realized_pnl_today, 2),
        "daily_loss_limit": round(loss_cap, 2),
        "daily_loss_used_pct": round(min(loss_used, 1.0), 4),
        "risk_level": cfg.risk_level,
        "risk_per_trade_pct": round(risk_fraction(cfg), 4),
        "flags": flags,
        "as_of": datetime.now(timezone.utc).isoformat(),
    }
