"""SKLZ CopyTrader — fill detection and execution.

Two responsibilities:

  1. WATCH a leader's exchange account for new spot fills and record them.
  2. FAN OUT each new fill to every follower, run the engine, and either
     simulate the resulting order (dry run) or place it for real.

DRY RUN IS THE DEFAULT. Nothing is sent to an exchange unless
COPY_EXECUTION_MODE=live is set explicitly. In dry run the full pipeline runs
and every decision is written to copy_orders with status 'simulated', so the
system can be watched for days before it is given authority to spend money.

Idempotency is enforced twice:
  - a leader fill is recorded once (unique trade_id)
  - a follower order carries a deterministic client_order_id, so a replay is
    rejected by the exchange rather than filled twice
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from supabase import Client

from copytrader.engine import (FollowerConfig, FollowerState, decide)
from copytrader.exchanges import ExchangeAdapter


def execution_mode() -> str:
    """'dry' (default) or 'live'. Anything other than exactly 'live' is dry."""
    return "live" if os.environ.get("COPY_EXECUTION_MODE", "").lower() == "live" else "dry"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ───────────────────────────── fill detection ─────────────────────────────
def poll_leader_fills(adapter: ExchangeAdapter, sb: Client, leader_id: str,
                      lookback_minutes: int = 30, log=print) -> list[dict]:
    """Read recent spot fills from the leader's exchange and record new ones.

    Returns the fills that were newly recorded (i.e. not seen before).
    """
    since = int((datetime.now(timezone.utc)
                 - timedelta(minutes=lookback_minutes)).timestamp() * 1000)
    try:
        trades = adapter.client.fetch_my_trades(since=since, limit=100)
    except Exception as exc:  # noqa: BLE001
        log(f"[fills] could not read trades: {type(exc).__name__}")
        return []

    # what have we already recorded?
    try:
        known = {r["trade_id"] for r in
                 (sb.table("copy_leader_trades").select("trade_id")
                  .eq("leader_id", leader_id)
                  .order("created_at", desc=True).limit(500).execute()).data or []}
    except Exception:
        known = set()

    equity = _leader_equity(adapter, sb, leader_id)
    fresh = []
    for t in trades:
        tid = str(t.get("id") or "")
        if not tid or tid in known:
            continue
        symbol = t.get("symbol") or ""
        side = (t.get("side") or "").lower()
        price = float(t.get("price") or 0)
        amount = float(t.get("amount") or 0)
        notional = float(t.get("cost") or (price * amount))
        if side not in ("buy", "sell") or notional <= 0:
            continue
        row = {"leader_id": leader_id, "trade_id": tid, "symbol": symbol,
               "side": side, "notional": round(notional, 8),
               "leader_equity": equity, "price": price}
        try:
            sb.table("copy_leader_trades").insert(row).execute()
            fresh.append(row)
            log(f"[fills] new leader fill: {side.upper()} {symbol} "
                f"{notional:.2f} ({notional/equity:.1%} of equity)"
                if equity else f"[fills] new leader fill: {side.upper()} {symbol}")
        except Exception:
            pass          # unique constraint = already recorded, fine
    return fresh


def _leader_equity(adapter: ExchangeAdapter, sb: Client, leader_id: str,
                   quote: str = "USDT") -> float:
    """Total account value in quote terms — needed to express a leader's trade
    as a fraction of their account, which is how followers scale it."""
    try:
        bals = adapter.balances()
    except Exception:  # noqa: BLE001
        return 0.0
    total = 0.0
    for b in bals:
        if b.asset == quote:
            total += b.total
            continue
        try:
            total += b.total * adapter.price(f"{b.asset}/{quote}")
        except Exception:  # noqa: BLE001
            continue
    total = round(total, 2)
    try:
        sb.table("copy_leader_equity").insert(
            {"leader_id": leader_id, "equity": total, "quote": quote}).execute()
    except Exception:
        pass
    return total


# ───────────────────────────── fan-out ─────────────────────────────
def _cfg(row: dict) -> FollowerConfig:
    return FollowerConfig(
        follower_id=row["follower_id"], leader_id=row["leader_id"],
        allocation=float(row.get("allocation") or 0),
        risk_level=row.get("risk_level") or "medium",
        custom_risk_pct=row.get("custom_risk_pct"),
        quote=row.get("quote") or "USDT",
        max_open_positions=int(row.get("max_open_positions") or 10),
        max_exposure_per_asset=float(row.get("max_exposure_per_asset") or 0.30),
        max_daily_loss=float(row.get("max_daily_loss") or 0.10),
        blacklist=row.get("blacklist") or [],
        whitelist=row.get("whitelist") or [],
        paused=bool(row.get("paused")))


def _state(sb: Client, sub: dict, free_quote: float) -> FollowerState:
    exposure: dict[str, float] = {}
    try:
        pos = (sb.table("copy_positions").select("asset,cost_basis")
               .eq("subscription_id", sub["id"]).execute()).data or []
        exposure = {p["asset"]: float(p.get("cost_basis") or 0)
                    for p in pos if float(p.get("cost_basis") or 0) > 0}
    except Exception:
        pass
    pnl = 0.0
    try:
        today = datetime.now(timezone.utc).date().isoformat()
        rows = (sb.table("copy_orders").select("realized_pnl")
                .eq("subscription_id", sub["id"])
                .gte("created_at", today).execute()).data or []
        pnl = sum(float(r.get("realized_pnl") or 0) for r in rows)
    except Exception:
        pass
    return FollowerState(free_quote=free_quote, open_positions=len(exposure),
                         exposure_by_asset=exposure, realized_pnl_today=pnl,
                         emergency_stopped=bool(sub.get("emergency_stopped")))


def fan_out(sb: Client, leader_trade: dict, load_adapter, log=print) -> list[dict]:
    """Run one leader fill through every subscriber.

    `load_adapter(user_id, connection_id) -> ExchangeAdapter` is injected so
    this module never touches credentials directly.
    """
    mode = execution_mode()
    try:
        subs = (sb.table("copy_subscriptions").select("*")
                .eq("leader_id", leader_trade["leader_id"]).execute()).data or []
    except Exception as exc:  # noqa: BLE001
        log(f"[fanout] could not load subscribers: {exc}")
        return []

    results = []
    for sub in subs:
        res = _copy_one(sb, sub, leader_trade, load_adapter, mode, log)
        results.append(res)
    if results:
        acted = sum(1 for r in results if r.get("status") in ("simulated", "filled"))
        log(f"[fanout] {leader_trade['symbol']} {leader_trade['side']}: "
            f"{acted}/{len(results)} followers acted ({mode} mode)")
    return results


def _copy_one(sb: Client, sub: dict, lt: dict, load_adapter,
              mode: str, log) -> dict:
    from copytrader.engine import LeaderTrade

    cfg = _cfg(sub)
    trade = LeaderTrade(trade_id=lt["trade_id"], symbol=lt["symbol"],
                        side=lt["side"], leader_notional=float(lt["notional"]),
                        leader_equity=float(lt.get("leader_equity") or 0))

    # live account context
    free, market, price = 0.0, None, 0.0
    adapter = None
    try:
        adapter = load_adapter(sub["follower_id"], sub["connection_id"])
        free = adapter.quote_balance(cfg.quote)
        adapter.load_markets()
        market = adapter.market_rules(lt["symbol"])
        price = adapter.price(lt["symbol"])
    except Exception as exc:  # noqa: BLE001
        return _record(sb, sub, lt, None, "failed",
                       f"could not read follower account: {type(exc).__name__}", log)

    state = _state(sb, sub, free)
    d = decide(trade, cfg, state, market, price)

    if not d.copy:
        return _record(sb, sub, lt, d, "skipped", d.reason, log)

    if mode != "live":
        return _record(sb, sub, lt, d, "simulated",
                       f"DRY RUN — would {lt['side']} {d.amount:.8f} "
                       f"{lt['symbol']} (~{d.notional:.2f} {cfg.quote})", log)

    # ---- live execution ----
    try:
        order = adapter.create_spot_order(lt["symbol"], lt["side"], d.amount,
                                          client_order_id=d.client_order_id)
    except Exception as exc:  # noqa: BLE001
        return _record(sb, sub, lt, d, "failed",
                       f"exchange rejected order: {type(exc).__name__}", log)

    rec = _record(sb, sub, lt, d, "filled", "order placed", log,
                  exchange_order_id=str(order.get("id") or ""),
                  filled_price=float(order.get("average") or order.get("price") or price))
    _update_position(sb, sub["id"], lt, d, price)
    return rec


def _record(sb: Client, sub: dict, lt: dict, d, status: str, note: str, log,
            exchange_order_id: str = "", filled_price: float | None = None) -> dict:
    row = {
        "subscription_id": sub["id"],
        "leader_trade_id": lt["trade_id"],
        "client_order_id": (d.client_order_id if d else
                            f"na-{lt['trade_id']}-{sub['id']}")[:64],
        "symbol": lt["symbol"], "side": lt["side"],
        "amount": (d.amount if d else 0), "notional": (d.notional if d else 0),
        "status": status,
        "skip_reason": note if status in ("skipped", "failed", "simulated") else "",
        "exchange_order_id": exchange_order_id,
        "filled_price": filled_price,
        "warnings": (d.warnings if d else []),
        "created_at": _now(),
    }
    try:
        sb.table("copy_orders").insert(row).execute()
    except Exception:
        pass          # duplicate client_order_id = already processed
    if status != "skipped":
        log(f"[copy] {status}: {note}")
    return row


def _update_position(sb: Client, sub_id: str, lt: dict, d, price: float) -> None:
    asset = lt["symbol"].split("/")[0]
    try:
        rows = (sb.table("copy_positions").select("*")
                .eq("subscription_id", sub_id).eq("asset", asset).execute()).data or []
        cur_amt = float(rows[0]["amount"]) if rows else 0.0
        cur_cost = float(rows[0]["cost_basis"]) if rows else 0.0
        if lt["side"] == "buy":
            new_amt, new_cost = cur_amt + d.amount, cur_cost + d.notional
        else:
            new_amt = max(cur_amt - d.amount, 0.0)
            new_cost = 0.0 if new_amt <= 0 else max(cur_cost - d.notional, 0.0)
        payload = {"subscription_id": sub_id, "asset": asset,
                   "amount": new_amt, "cost_basis": new_cost,
                   "updated_at": _now()}
        if rows:
            sb.table("copy_positions").update(payload).eq("id", rows[0]["id"]).execute()
        else:
            sb.table("copy_positions").insert(payload).execute()
    except Exception:
        pass
