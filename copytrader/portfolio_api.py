"""SKLZ — follower portfolio.

What someone sees when software is trading their money.

THE PRINCIPLE
=============
A follower handed over API keys and let a stranger's trades run on their
account. The minimum they are owed is a clear, current answer to: what do I
hold, what did it cost, what is it worth now, and what happened to everything
that already closed.

Two things this does that most copy platforms do not:

  It shows what was SKIPPED and why. Silence looks like breakage. "Your
  balance was too low for this one" is information; nothing at all is not.

  It compares the follower's result to the leader's honestly, including
  slippage and fee drag. If the leader made 3% and the follower made 2.1%,
  that gap is stated rather than buried — it is usually fees and fill
  differences, and hiding it is how trust goes.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from supabase import Client

from auth import get_current_user
from db import get_supabase
from copytrader.connections_api import _load_adapter

router = APIRouter(prefix="/api/copy/portfolio", tags=["copytrading"])


def _num(v, d=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def _parse(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None


@router.get("/open")
async def open_positions(user=Depends(get_current_user),
                         sb: Client = Depends(get_supabase)) -> dict:
    """Current holdings on the follower's own exchange, priced live.

    Read from the exchange rather than from our own records, because the
    exchange is the truth. If our accounting and their balance disagree, the
    balance wins and we should show it.
    """
    uid = str(user.id)
    conns = _user_connections(sb, uid)
    if not conns:
        return {"positions": [], "connected": False,
                "message": "No exchange connected yet."}

    out, errors = [], []
    for sub in conns:
        try:
            adapter = _load_adapter(sb, uid, sub["connection_id"])
            balances = adapter.balances(non_zero=True)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"could not read your exchange: {str(exc)[:120]}")
            continue

        quote = (sub.get("quote") or "USDT").upper()
        for b in balances or []:
            asset = (getattr(b, "asset", None)
                     or (b.get("asset") if isinstance(b, dict) else "") or "").upper()
            free = _num(getattr(b, "free", None)
                        if not isinstance(b, dict) else b.get("free"))
            total = _num(getattr(b, "total", None)
                         if not isinstance(b, dict) else b.get("total"), free)
            if not asset or total <= 0:
                continue
            if asset in ("USDT", "USDC", "USD", "EUR", "GBP"):
                out.append({"asset": asset, "amount": round(total, 8),
                            "is_cash": True, "value": round(total, 2)})
                continue

            # price it in whatever the account actually quotes against
            price = value = None
            for q in (quote, "USDT", "USDC", "USD"):
                try:
                    price = adapter.price(f"{asset}/{q}")
                    if price:
                        value = total * price
                        break
                except Exception:
                    continue

            # what we paid, from our own fill records
            cost = None
            try:
                fills = (sb.table("copy_orders").select("*")
                         .eq("subscription_id", sub["id"])
                         .eq("status", "placed")
                         .like("symbol", f"{asset}/%").execute()).data or []
                spent = sum(_num(f.get("notional")) for f in fills
                            if (f.get("side") or "") == "buy")
                sold = sum(_num(f.get("notional")) for f in fills
                           if (f.get("side") or "") == "sell")
                if spent:
                    cost = spent - sold
            except Exception:
                pass

            row = {"asset": asset, "amount": round(total, 8),
                   "free": round(free, 8), "is_cash": False,
                   "price": round(price, 8) if price else None,
                   "value": round(value, 2) if value else None,
                   "cost_basis": round(cost, 2) if cost else None}
            if value and cost:
                row["unrealised"] = round(value - cost, 2)
                row["unrealised_pct"] = round((value - cost) / cost * 100, 2)
            out.append(row)

    out.sort(key=lambda r: (r["is_cash"], -(r.get("value") or 0)))
    holdings = [r for r in out if not r["is_cash"]]
    cash = sum(r["value"] for r in out if r["is_cash"])
    invested = sum(r.get("value") or 0 for r in holdings)

    return {
        "positions": out,
        "connected": True,
        "summary": {
            "holdings": len(holdings),
            "invested_value": round(invested, 2),
            "cash": round(cash, 2),
            "total": round(invested + cash, 2),
            "unrealised": round(sum(r.get("unrealised") or 0 for r in holdings), 2),
        },
        "errors": errors,
        "note": ("Read live from your exchange, not from our records. If these "
                 "differ from what your exchange app shows, trust the app and "
                 "tell us."),
    }


@router.get("/history")
async def history(days: int = 30, user=Depends(get_current_user),
                  sb: Client = Depends(get_supabase)) -> dict:
    """Every copy decision — placed, skipped, or failed — with the reason.

    Skipped trades are included deliberately. A follower who sees the leader
    trade but nothing on their own account needs to know why, and "silence"
    is the worst possible answer.
    """
    uid = str(user.id)
    since = (datetime.now(timezone.utc) - timedelta(days=min(days, 365))).isoformat()
    try:
        subs = (sb.table("copy_subscriptions").select("id")
                .eq("follower_id", uid).execute()).data or []
        ids = [s["id"] for s in subs]
        rows = []
        if ids:
            rows = (sb.table("copy_orders").select("*")
                    .in_("subscription_id", ids)
                    .gte("created_at", since)
                    .order("created_at", desc=True)
                    .limit(500).execute()).data or []
    except Exception:
        rows = []

    placed = [r for r in rows if r.get("status") == "placed"]
    skipped = [r for r in rows if r.get("status") == "skipped"]
    failed = [r for r in rows if r.get("status") == "failed"]

    # realised P/L, matching sells against buys per asset
    by_asset: dict = {}
    for r in sorted(placed, key=lambda x: x.get("created_at") or ""):
        asset = (r.get("symbol") or "").split("/")[0]
        d = by_asset.setdefault(asset, {"bought": 0.0, "sold": 0.0,
                                        "buy_count": 0, "sell_count": 0})
        if r.get("side") == "buy":
            d["bought"] += _num(r.get("notional"))
            d["buy_count"] += 1
        else:
            d["sold"] += _num(r.get("notional"))
            d["sell_count"] += 1

    realised = []
    for asset, d in by_asset.items():
        if d["sold"] <= 0:
            continue
        # only count assets fully or partly closed; open ones sit in /open
        pnl = d["sold"] - min(d["bought"], d["sold"])
        realised.append({"asset": asset, "bought": round(d["bought"], 2),
                         "sold": round(d["sold"], 2),
                         "realised": round(pnl, 2),
                         "trades": d["buy_count"] + d["sell_count"]})

    skip_reasons: dict = {}
    for r in skipped + failed:
        why = (r.get("skip_reason") or "unknown")[:80]
        skip_reasons[why] = skip_reasons.get(why, 0) + 1

    return {
        "trades": rows,
        "counts": {"placed": len(placed), "skipped": len(skipped),
                   "failed": len(failed), "total": len(rows)},
        "realised": realised,
        "realised_total": round(sum(r["realised"] for r in realised), 2),
        "why_skipped": [{"reason": k, "count": v}
                        for k, v in sorted(skip_reasons.items(),
                                           key=lambda kv: kv[1], reverse=True)],
        "note": ("Skipped trades are shown on purpose. If the leader traded "
                 "and you did not, the reason is here rather than hidden."),
    }


@router.get("/vs-leader")
async def vs_leader(user=Depends(get_current_user),
                    sb: Client = Depends(get_supabase)) -> dict:
    """How the follower's result compares to the leader's, honestly.

    The gap is usually fees and fill differences. Naming it is better than
    letting someone discover it and wonder what else is not being said.
    """
    uid = str(user.id)
    try:
        subs = (sb.table("copy_subscriptions").select("*")
                .eq("follower_id", uid).execute()).data or []
    except Exception:
        subs = []
    if not subs:
        return {"comparable": False, "reason": "not following anyone yet"}

    out = []
    for sub in subs:
        try:
            orders = (sb.table("copy_orders").select("*")
                      .eq("subscription_id", sub["id"])
                      .eq("status", "placed").execute()).data or []
        except Exception:
            orders = []
        if not orders:
            out.append({"subscription_id": sub["id"], "copied": 0,
                        "note": "no trades copied yet"})
            continue

        filled = [o for o in orders if o.get("filled_price")]
        slippage = []
        for o in filled:
            lp = _num(o.get("leader_price"))
            fp = _num(o.get("filled_price"))
            if lp and fp:
                diff = (fp - lp) / lp * 100
                slippage.append(diff if o.get("side") == "buy" else -diff)

        out.append({
            "subscription_id": sub["id"],
            "copied": len(orders),
            "avg_slippage_pct": round(sum(slippage) / len(slippage), 4)
            if slippage else None,
            "note": (
                f"Across {len(slippage)} fills your price differed from the "
                f"leader by {sum(slippage)/len(slippage):+.3f}% on average. "
                f"This is normal — you trade on a different venue, seconds "
                f"later, and your exchange charges its own fees."
                if slippage else
                "Not enough filled trades yet to measure the difference."),
        })

    return {"comparable": True, "subscriptions": out,
            "honest_note": ("Your returns will not match the leader exactly. "
                            "Different exchange, different fees, and a delay "
                            "between their fill and yours. Anyone promising "
                            "identical results is not telling you the truth.")}


# ── position protection and partial exits ───────────────────────────
class ProtectIn(BaseModel):
    connection_id: str
    symbol: str
    amount: float
    stop_price: float | None = None
    take_profit: float | None = None


@router.post("/protect")
async def protect(body: ProtectIn, user=Depends(get_current_user),
                  sb: Client = Depends(get_supabase)) -> dict:
    """Set a stop loss and/or take profit on a position.

    The response says whether the exchange is holding the order or SKLZ is
    watching it. That distinction is the whole point: a bot-watched stop does
    not fire when SKLZ is offline, and someone who believes otherwise has been
    misled rather than protected.
    """
    from copytrader import protection as PR

    try:
        adapter = _load_adapter(sb, str(user.id), body.connection_id)
        adapter.load_markets()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_502_BAD_GATEWAY,
                            f"could not reach your exchange: {str(exc)[:140]}") from exc

    symbol = body.symbol
    try:
        from copytrader.executor import resolve_for_follower
        res = resolve_for_follower(adapter, body.symbol)
        if res.get("ok"):
            symbol = res["symbol"]
    except Exception:
        pass

    result = PR.place_protection(adapter, symbol, body.amount,
                                 body.stop_price, body.take_profit)
    result["symbol"] = symbol
    result["explanation"] = PR.describe_protection(result)

    try:
        sb.table("position_protection").upsert({
            "user_id": str(user.id), "connection_id": body.connection_id,
            "symbol": body.symbol, "amount": body.amount,
            "stop_price": body.stop_price, "take_profit": body.take_profit,
            "protection": result.get("protection"),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }, on_conflict="user_id,connection_id,symbol").execute()
    except Exception:
        pass
    return result


class PartialIn(BaseModel):
    connection_id: str
    symbol: str
    percent: float = Field(ge=1, le=100)


@router.post("/sell-partial")
async def sell_partial(body: PartialIn, user=Depends(get_current_user),
                       sb: Client = Depends(get_supabase)) -> dict:
    """Sell part of a holding — take some profit, keep the rest running."""
    from copytrader import protection as PR

    try:
        adapter = _load_adapter(sb, str(user.id), body.connection_id)
        adapter.load_markets()
        asset = body.symbol.split("/")[0].upper()
        held = 0.0
        for b in adapter.balances(non_zero=True) or []:
            a = (getattr(b, "asset", None)
                 or (b.get("asset") if isinstance(b, dict) else "") or "").upper()
            if a == asset:
                held = _num(getattr(b, "free", None)
                            if not isinstance(b, dict) else b.get("free"))
                break
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_502_BAD_GATEWAY,
                            f"could not read your balance: {str(exc)[:140]}") from exc

    if held <= 0:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"you do not hold any {asset}")

    # The page sends a symbol built from a guessed quote currency. Trust the
    # exchange instead: Coinbase lists USDC where Bybit lists USDT, and sending
    # BTC/USDT to Coinbase returns "target is not enabled for trading", which
    # looks like a broken button rather than a wrong pair name.
    symbol = body.symbol
    try:
        from copytrader.executor import resolve_for_follower
        res = resolve_for_follower(adapter, body.symbol)
        if res.get("ok"):
            symbol = res["symbol"]
        else:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, res["reason"])
    except HTTPException:
        raise
    except Exception:
        pass

    market = adapter.market_rules(symbol) if hasattr(adapter, "market_rules") else None
    calc = PR.partial_sell_amount(held, body.percent, market)
    if not calc["ok"]:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, calc["reason"])

    try:
        order = adapter.create_spot_order(symbol, "sell", calc["amount"])
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_502_BAD_GATEWAY,
                            f"exchange rejected the sell: {str(exc)[:160]}") from exc

    return {"ok": True, "sold": calc["amount"], "percent": body.percent,
            "remaining": calc["remaining"], "order_id": order.get("id"),
            "symbol": symbol, "note": calc["note"]}


def _user_connections(sb, uid: str) -> list[dict]:
    """Every exchange connection this user can trade through.

    Looking these up only via copy_subscriptions was wrong: a master trader
    has no subscription — they have a leader row — so their own holdings never
    loaded and every position control had nothing to attach to. Connections
    are the real anchor; subscriptions and leadership are both just ways of
    pointing at one.
    """
    out, seen = [], set()

    # anything they connected directly
    try:
        for r in (sb.table("copy_connections").select("id,exchange_id")
                  .eq("user_id", uid).eq("status", "active").execute()).data or []:
            if r["id"] not in seen:
                seen.add(r["id"])
                out.append({"connection_id": r["id"], "role": "owner",
                            "quote": "USDT"})
    except Exception:
        pass

    # connections named by a subscription they follow through
    try:
        for s in (sb.table("copy_subscriptions").select("*")
                  .eq("follower_id", uid).execute()).data or []:
            cid = s.get("connection_id")
            if cid and cid not in seen:
                seen.add(cid)
                out.append({"connection_id": cid, "role": "follower",
                            "quote": s.get("quote") or "USDT",
                            "subscription": s})
    except Exception:
        pass

    return out


def _enrich(orders: list[dict], subs: list[dict], sb, uid: str) -> list[dict]:
    """Add current price and live P/L to each recent order.

    Priced once per symbol rather than per row — a dashboard that makes twelve
    identical price calls is slow for no reason.
    """
    if not orders:
        return []
    adapter = None
    for sub in subs:
        try:
            adapter = _load_adapter(sb, uid, sub["connection_id"])
            break
        except Exception:
            continue

    prices: dict = {}
    out = []
    for o in orders:
        row = dict(o)
        sym = o.get("symbol") or ""
        entry = _num(o.get("filled_price"))
        cur = None
        if adapter and sym and o.get("status") == "placed":
            if sym not in prices:
                try:
                    prices[sym] = adapter.price(sym)
                except Exception:
                    prices[sym] = None
            cur = prices[sym]
        row["current_price"] = cur
        if entry and cur:
            pct = (cur - entry) / entry * 100
            if o.get("side") == "sell":
                pct = -pct
            row["pnl_pct"] = round(pct, 2)
            row["pnl_usd"] = round(_num(o.get("notional")) * pct / 100, 2)
        out.append(row)
    return out


@router.get("/dashboard")
async def dashboard(days: int = 30, user=Depends(get_current_user),
                    sb: Client = Depends(get_supabase)) -> dict:
    """Everything the client dashboard needs, in one call.

    Win rate and similar headline figures are reported with their honest
    uncertainty. A 72% win rate over 42 trades sounds authoritative and is
    not: the true value could sit anywhere from 57% to 85%. Showing the
    number alone is how every other platform flatters itself.
    """
    uid = str(user.id)
    since = (datetime.now(timezone.utc) - timedelta(days=min(days, 365))).isoformat()

    conns = _user_connections(sb, uid)
    try:
        follow_subs = (sb.table("copy_subscriptions").select("id")
                       .eq("follower_id", uid).execute()).data or []
    except Exception:
        follow_subs = []
    ids = [s["id"] for s in follow_subs]
    subs = conns          # holdings come from connections, not subscriptions

    orders = []
    if ids:
        try:
            orders = (sb.table("copy_orders").select("*")
                      .in_("subscription_id", ids)
                      .gte("created_at", since)
                      .order("created_at", desc=True)
                      .limit(1000).execute()).data or []
        except Exception:
            orders = []

    placed = [o for o in orders if o.get("status") == "placed"]

    # holdings and cash, live from the exchange
    balance = invested = cash = unrealised = 0.0
    holdings = []
    for sub in subs:
        try:
            adapter = _load_adapter(sb, uid, sub["connection_id"])
            for b in adapter.balances(non_zero=True) or []:
                asset = (getattr(b, "asset", None)
                         or (b.get("asset") if isinstance(b, dict) else "") or "").upper()
                total = _num(getattr(b, "total", None)
                             if not isinstance(b, dict) else b.get("total"))
                if not asset or total <= 0:
                    continue
                if asset in ("USDT", "USDC", "USD", "EUR", "GBP"):
                    cash += total
                    # surface it so the client knows which quote this account
                    # actually trades in rather than assuming USDT
                    holdings.append({"asset": asset, "amount": total,
                                     "value": round(total, 2), "is_cash": True})
                    continue
                price = None
                for q in ("USDT", "USDC", "USD"):
                    try:
                        price = adapter.price(f"{asset}/{q}")
                        if price:
                            break
                    except Exception:
                        continue
                val = total * price if price else 0
                invested += val
                holdings.append({"asset": asset, "amount": total,
                                 "value": round(val, 2)})
        except Exception:
            continue
    balance = invested + cash

    # realised results per asset, matching sells against buys
    per_asset: dict = {}
    for o in sorted(placed, key=lambda x: x.get("created_at") or ""):
        asset = (o.get("symbol") or "").split("/")[0]
        d = per_asset.setdefault(asset, {"bought": 0.0, "sold": 0.0, "trades": 0})
        d["trades"] += 1
        if o.get("side") == "buy":
            d["bought"] += _num(o.get("notional"))
        else:
            d["sold"] += _num(o.get("notional"))

    top = []
    closed_wins = closed_losses = 0
    for asset, d in per_asset.items():
        if d["sold"] > 0:
            pnl = d["sold"] - min(d["bought"], d["sold"])
            if pnl > 0:
                closed_wins += 1
            elif pnl < 0:
                closed_losses += 1
        else:
            pnl = 0.0
        top.append({"asset": asset, "pnl": round(pnl, 2), "trades": d["trades"]})
    top.sort(key=lambda r: abs(r["pnl"]), reverse=True)

    realised = sum(r["pnl"] for r in top)
    closed_total = closed_wins + closed_losses

    # Equity curve: cumulative realised P/L over time, computed by matching
    # each sell against the average cost of what was bought before it. Without
    # this the chart is decorative, and a decorative equity curve on a trading
    # platform is worse than none.
    curve = []
    running = 0.0
    held: dict = {}          # asset -> {"qty": float, "cost": float}
    for o in sorted(placed, key=lambda x: x.get("created_at") or ""):
        asset = (o.get("symbol") or "").split("/")[0]
        notional = _num(o.get("notional"))
        price = _num(o.get("filled_price")) or None
        qty = (notional / price) if price else 0

        h = held.setdefault(asset, {"qty": 0.0, "cost": 0.0})
        if o.get("side") == "buy":
            h["qty"] += qty
            h["cost"] += notional
        else:
            if h["qty"] > 0 and qty > 0:
                avg = h["cost"] / h["qty"]
                sold_qty = min(qty, h["qty"])
                realised_here = (price - avg) * sold_qty if price else 0
                running += realised_here
                h["qty"] -= sold_qty
                h["cost"] -= avg * sold_qty
        curve.append({"t": o.get("created_at"), "v": round(running, 2)})

    # daily points rather than per-trade, so the chart reads as a timeline
    daily: dict = {}
    for p in curve:
        day = (p["t"] or "")[:10]
        if day:
            daily[day] = p["v"]
    curve_daily = [{"t": k, "v": v} for k, v in sorted(daily.items())]

    # the honest bit
    win_rate = (closed_wins / closed_total) if closed_total else None
    verdict = None
    if closed_total == 0:
        verdict = "No positions have been closed yet, so there is nothing to measure."
    elif closed_total < 30:
        try:
            import research_stats as RS
            lo, hi = RS.wilson_interval(closed_wins, closed_total)
            verdict = (f"{closed_total} closed position(s). The true win rate "
                       f"could be anywhere from {lo:.0%} to {hi:.0%} — far too "
                       f"few to judge. Treat this number as decoration until "
                       f"there are at least 30.")
        except Exception:
            verdict = (f"Only {closed_total} closed position(s) — too few to "
                       f"draw any conclusion from.")
    else:
        try:
            import research_stats as RS
            lo, hi = RS.wilson_interval(closed_wins, closed_total)
            verdict = (f"{closed_total} closed positions, win rate "
                       f"{win_rate:.0%} (range {lo:.0%}-{hi:.0%})."
                       + (" That range includes 50%, so this is not yet "
                          "distinguishable from chance."
                          if lo <= 0.5 <= hi else ""))
        except Exception:
            verdict = f"{closed_total} closed positions."

    return {
        "balance": round(balance, 2),
        "invested": round(invested, 2),
        "cash": round(cash, 2),
        "realised_pnl": round(realised, 2),
        "realised_pct": round(realised / balance * 100, 2) if balance else None,
        "trades_total": len(orders),
        "trades_placed": len(placed),
        "open_positions": len(holdings),
        "closed_positions": closed_total,
        "wins": closed_wins,
        "losses": closed_losses,
        "win_rate": round(win_rate, 4) if win_rate is not None else None,
        "sample_verdict": verdict,
        "enough_data": closed_total >= 30,
        "top_assets": top[:6],
        "holdings": sorted(holdings, key=lambda h: -h["value"])[:8],
        "recent": _enrich(orders[:12], subs, sb, uid),
        "curve": curve_daily[-60:],
        "distribution": {"wins": closed_wins, "losses": closed_losses,
                         "open": len(holdings)},
        "following": len(subs),
    }


@router.get("/leader")
async def leader_dashboard(user=Depends(get_current_user),
                           sb: Client = Depends(get_supabase)) -> dict:
    """What a master trader needs to see.

    Their own record, who is following, how much capital is tracking them, and
    whether fan-out is actually reaching those people. That last one matters
    most: a leader whose trades are silently failing to copy has followers
    paying for nothing, and would never know from their own account.
    """
    uid = str(user.id)
    try:
        leaders = (sb.table("copy_leaders").select("*")
                   .eq("user_id", uid).execute()).data or []
    except Exception:
        leaders = []
    if not leaders:
        return {"is_leader": False,
                "message": "You are not registered as a master trader."}

    leader = leaders[0]
    lid = leader["id"]

    try:
        subs = (sb.table("copy_subscriptions").select("*")
                .eq("leader_id", lid).execute()).data or []
    except Exception:
        subs = []

    sub_ids = [s["id"] for s in subs]
    orders = []
    if sub_ids:
        try:
            orders = (sb.table("copy_orders").select("*")
                      .in_("subscription_id", sub_ids)
                      .order("created_at", desc=True)
                      .limit(500).execute()).data or []
        except Exception:
            orders = []

    placed = [o for o in orders if o.get("status") == "placed"]
    skipped = [o for o in orders if o.get("status") in ("skipped", "failed")]

    # the number that matters: are the trades actually reaching people?
    delivery = (len(placed) / len(orders) * 100) if orders else None
    fail_reasons: dict = {}
    for o in skipped:
        why = (o.get("skip_reason") or "unknown")[:70]
        fail_reasons[why] = fail_reasons.get(why, 0) + 1

    tracking = sum(_num(s.get("allocation")) for s in subs)
    active = [s for s in subs if not s.get("paused")
              and not s.get("emergency_stopped")]

    share_pct = _num(leader.get("revenue_share_pct"), 10)
    try:
        earnings = (sb.table("leader_earnings").select("amount,paid")
                    .eq("leader_id", lid).execute()).data or []
    except Exception:
        earnings = []
    earned = sum(_num(e.get("amount")) for e in earnings)
    unpaid = sum(_num(e.get("amount")) for e in earnings if not e.get("paid"))

    return {
        "is_leader": True,
        "profile": {
            "display_name": leader.get("display_name"),
            "status": leader.get("approval_status"),
            "suspended": bool(leader.get("suspended")),
            "verified_trades": leader.get("verified_trades") or 0,
            "verified_months": leader.get("verified_months") or 0,
            "revenue_share_pct": share_pct,
        },
        "followers": {
            "total": len(subs),
            "active": len(active),
            "paused": len(subs) - len(active),
            "capital_tracking": round(tracking, 2),
        },
        "delivery": {
            "copied": len(placed),
            "not_copied": len(skipped),
            "rate_pct": round(delivery, 1) if delivery is not None else None,
            "reasons": [{"reason": k, "count": v}
                        for k, v in sorted(fail_reasons.items(),
                                           key=lambda kv: kv[1], reverse=True)][:6],
            "note": ("This is the share of your trades that actually reached a "
                     "follower account. A low rate means people following you "
                     "are paying for trades they never received — the reasons "
                     "below say why."),
        },
        "earnings": {
            "total": round(earned, 2),
            "unpaid": round(unpaid, 2),
            "share_pct": share_pct,
            "note": (f"{share_pct:.0f}% of subscription revenue from followers "
                     f"you bring, paid monthly while they stay subscribed."),
        },
        "recent": orders[:10],
    }


@router.get("/share")
async def share_card(user=Depends(get_current_user),
                     sb: Client = Depends(get_supabase)) -> dict:
    """A shareable summary of the user's own results.

    Deliberately includes the sample size and the period. A screenshot showing
    "+43%" with no context is what every trading grifter posts; including the
    number of trades and the timeframe is what makes it a claim rather than a
    boast.
    """
    uid = str(user.id)
    d = await dashboard(30, user, sb)

    pnl = d.get("realised_pnl") or 0
    closed = d.get("closed_positions") or 0

    if closed == 0:
        headline = "Just getting started"
        detail = "No positions closed yet."
    else:
        headline = ("+" if pnl >= 0 else "") + f"${pnl:,.2f}"
        detail = f"over {closed} closed position" + ("s" if closed != 1 else "")

    honest = None
    if 0 < closed < 30:
        honest = (f"{closed} trades is too small a sample to mean much — "
                  f"this could easily be luck.")

    return {
        "headline": headline,
        "detail": detail,
        "period": "last 30 days",
        "closed_positions": closed,
        "win_rate": d.get("win_rate") if d.get("enough_data") else None,
        "honest_note": honest,
        "disclaimer": ("Past results do not predict future returns. "
                       "Trading involves risk of loss."),
    }
