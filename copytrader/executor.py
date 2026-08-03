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
    # Bybit and several others will not return account trades without a
    # symbol — they answer with an empty list rather than an error, which
    # looks exactly like "no trades" and hides the problem.
    trades = []
    try:
        trades = adapter.client.fetch_my_trades(since=since, limit=100) or []
    except Exception as exc:  # noqa: BLE001
        log(f"[fills] account-wide read failed ({type(exc).__name__}), "
            f"falling back to per-symbol")

    if not trades:
        # ask per symbol, using whatever the account actually holds plus the
        # pairs already seen from this leader
        symbols = set()
        try:
            for b in adapter.balances(non_zero=True):
                asset = getattr(b, "asset", None) or (b.get("asset") if isinstance(b, dict) else None)
                if asset and asset.upper() not in ("USDT", "USDC", "USD"):
                    symbols.add(f"{asset.upper()}/USDT")
        except Exception:
            pass
        try:
            prev = (sb.table("copy_leader_trades").select("symbol")
                    .eq("leader_id", leader_id)
                    .order("created_at", desc=True).limit(50).execute()).data or []
            symbols.update(r["symbol"] for r in prev if r.get("symbol"))
        except Exception:
            pass
        # a sensible default set, so a first-ever trade is not missed
        symbols.update(os.environ.get(
            "COPY_WATCH_SYMBOLS",
            "BTC/USDT,ETH/USDT,SOL/USDT,BNB/USDT,XRP/USDT,ADA/USDT"
        ).split(","))

        for sym in sorted(s.strip() for s in symbols if s and s.strip()):
            try:
                got = adapter.client.fetch_my_trades(
                    symbol=sym, since=since, limit=50) or []
                trades.extend(got)
            except Exception:
                continue
        if trades:
            log(f"[fills] read {len(trades)} trade(s) via per-symbol query")

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


def _clean_exc(exc: Exception) -> str:
    """Readable error text with any credentials stripped out."""
    import re
    s = str(exc)
    s = re.sub(r"[a-fA-F0-9]{32,}", "<redacted>", s)
    s = re.sub(r"(api[_-]?key|secret|signature)=[^&\s]+", r"\1=<redacted>", s,
               flags=re.I)
    return s[:220] if s else type(exc).__name__


def resolve_for_follower(adapter, leader_symbol: str, log=print) -> dict:
    """Find the equivalent pair on the follower's exchange.

    Exchanges quote against different stablecoins — Bybit uses USDT, Coinbase
    largely USDC, Kraken often USD. Sending the leader's symbol verbatim means
    the order simply fails on any venue that quotes differently.

    So: keep the base asset, try the quotes the follower actually supports.
    A USDC pair is not identical to a USDT one — the price can differ slightly
    and the stablecoins carry different risk — but for copying a directional
    spot position it is the right equivalent, and that difference is disclosed
    rather than hidden.
    """
    base = leader_symbol.split("/")[0].upper() if "/" in leader_symbol else leader_symbol
    leader_quote = (leader_symbol.split("/")[1].upper()
                    if "/" in leader_symbol else "USDT")

    try:
        markets = adapter.load_markets() or {}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"could not read markets: {str(exc)[:120]}"}

    # exact match first — no translation needed
    if leader_symbol in markets and markets[leader_symbol].get("active"):
        return {"ok": True, "symbol": leader_symbol, "translated": False,
                "reason": "exact pair available"}

    # otherwise try equivalent quotes, in order of closeness
    for quote in ("USDT", "USDC", "USD", "EUR", "GBP"):
        if quote == leader_quote:
            continue
        cand = f"{base}/{quote}"
        m = markets.get(cand)
        if m and m.get("active") and m.get("spot"):
            note = (f"leader traded {leader_symbol}; this exchange quotes "
                    f"{base} in {quote}, so {cand} was used instead")
            log(f"[fanout] {note}")
            return {"ok": True, "symbol": cand, "translated": True,
                    "from_quote": leader_quote, "to_quote": quote,
                    "reason": note}

    return {"ok": False,
            "reason": (f"{base} is not tradeable on this exchange against any "
                       f"supported quote currency — the follower cannot copy "
                       f"this trade")}


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
        adapter.load_markets()

        # Resolve the pair FIRST. The follower may quote in a different
        # stablecoin than the leader, and checking the balance before knowing
        # which currency will actually be spent asks the wrong question —
        # "no free USDT" on an account holding USDC is not a real constraint.
        resolved = resolve_for_follower(adapter, lt["symbol"], log)
        if not resolved.get("ok"):
            # the follower simply cannot trade this pair — record why rather
            # than failing silently, so they can see it on their dashboard
            class _NoCopy:
                copy = False
                amount = 0.0
                notional = 0.0
                reason = resolved["reason"]
            return _record(sb, sub, lt, _NoCopy(), "skipped",
                           resolved["reason"], log)
        follower_symbol = resolved["symbol"]

        # the quote actually being spent, which may differ from the
        # subscription's configured quote after translation
        spend_quote = (follower_symbol.split("/")[1].upper()
                       if "/" in follower_symbol else cfg.quote)
        free = adapter.quote_balance(spend_quote)
        if resolved.get("translated"):
            log(f"[fanout] checking {spend_quote} balance "
                f"(subscription is configured in {cfg.quote})")

        market = adapter.market_rules(follower_symbol)
        price = adapter.price(follower_symbol)
    except Exception as exc:  # noqa: BLE001
        return _record(sb, sub, lt, None, "failed",
                       f"could not read follower account: {type(exc).__name__}", log)

    state = _state(sb, sub, free)
    # tell the decision engine which currency this balance is in, so a skip
    # message names the right one
    try:
        state.spend_quote = spend_quote
    except Exception:
        pass
    d = decide(trade, cfg, state, market, price)

    if not d.copy:
        return _record(sb, sub, lt, d, "skipped", d.reason, log)

    if mode != "live":
        return _record(sb, sub, lt, d, "simulated",
                       f"DRY RUN — would {lt['side']} {d.amount:.8f} "
                       f"{lt['symbol']} (~{d.notional:.2f} {cfg.quote})", log)

    # ---- pre-flight: does this clear the venue's own minimums? ----
    lim = (market or {}).get("limits") or {}
    min_cost = (lim.get("cost") or {}).get("min")
    min_amt = (lim.get("amount") or {}).get("min")
    notional_now = d.amount * price if price else 0

    if min_cost and notional_now < float(min_cost):
        return _record(sb, sub, lt, d, "skipped",
                       (f"order of {notional_now:.2f} {spend_quote} is below "
                        f"this exchange's minimum of {min_cost} — increase the "
                        f"allocation or risk level to copy trades this size"),
                       log)
    if min_amt and d.amount < float(min_amt):
        return _record(sb, sub, lt, d, "skipped",
                       (f"amount {d.amount} is below the exchange minimum of "
                        f"{min_amt} {follower_symbol.split('/')[0]}"), log)

    # ---- live execution ----
    try:
        order = adapter.create_spot_order(follower_symbol, lt["side"], d.amount,
                                          client_order_id=d.client_order_id)
    except Exception as exc:  # noqa: BLE001
        # the exception TYPE alone is nearly useless — "InvalidOrder" could be
        # size, precision, or an unsupported order type. Keep the message.
        msg = _clean_exc(exc)
        return _record(sb, sub, lt, d, "failed",
                       f"exchange rejected order: {msg}", log)

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
