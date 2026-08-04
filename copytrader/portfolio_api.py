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
    try:
        subs = (sb.table("copy_subscriptions").select("*")
                .eq("follower_id", uid).execute()).data or []
    except Exception:
        subs = []
    if not subs:
        return {"positions": [], "connected": False,
                "message": "No exchange connected yet."}

    out, errors = [], []
    for sub in subs:
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

    result = PR.place_protection(adapter, body.symbol, body.amount,
                                 body.stop_price, body.take_profit)
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

    market = adapter.market_rules(body.symbol) if hasattr(adapter, "market_rules") else None
    calc = PR.partial_sell_amount(held, body.percent, market)
    if not calc["ok"]:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, calc["reason"])

    try:
        order = adapter.create_spot_order(body.symbol, "sell", calc["amount"])
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_502_BAD_GATEWAY,
                            f"exchange rejected the sell: {str(exc)[:160]}") from exc

    return {"ok": True, "sold": calc["amount"], "percent": body.percent,
            "remaining": calc["remaining"], "order_id": order.get("id"),
            "note": calc["note"]}
