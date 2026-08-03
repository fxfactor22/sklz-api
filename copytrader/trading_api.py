"""SKLZ CopyTrader — leader manual trading and master-trader applications.

MANUAL TRADING
  A leader places a spot order from the dashboard on their own connected
  account. This respects COPY_EXECUTION_MODE exactly like the copy engine:
  in dry run it records what it would have done and sends nothing. There is
  no path where copying is safe but a button is not.

  Guards, because a leader's fills fan out to followers:
    - only the connection owner may trade it
    - spot only, market orders only
    - a hard per-order notional ceiling (COPY_MAX_MANUAL_NOTIONAL)
    - the order must clear the exchange's own minimums
    - every attempt is audited

MASTER TRADER APPLICATIONS
  Anyone may apply. An admin approves. Only approved leaders are listed
  publicly and only approved leaders can be followed.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from supabase import Client

from auth import get_current_user
from db import get_supabase
from copytrader.connections_api import _audit, _load_adapter
from copytrader.executor import execution_mode

router = APIRouter(prefix="/api/copy", tags=["copytrader"])


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _max_manual_notional() -> float:
    try:
        return float(os.environ.get("COPY_MAX_MANUAL_NOTIONAL", "1000"))
    except ValueError:
        return 1000.0


def _is_admin(user) -> bool:
    admins = {e.strip().lower() for e in
              os.environ.get("ADMIN_EMAILS", "fxfactor24@gmail.com").split(",")}
    return (getattr(user, "email", "") or "").lower() in admins


# ─────────────────────────── manual trading ───────────────────────────
class ManualOrderIn(BaseModel):
    connection_id: str
    symbol: str = Field(min_length=3)          # e.g. "BTC/USDT"
    side: str                                  # buy | sell
    notional: float | None = None              # spend this much quote
    amount: float | None = None                # or trade this many base units
    note: str = ""
    confirm: bool = False


@router.get("/trade/preview")
async def trade_preview(connection_id: str, symbol: str,
                        user=Depends(get_current_user),
                        sb: Client = Depends(get_supabase)) -> dict:
    """What would this order look like? Price, minimums, balance, limits."""
    adapter = _load_adapter(sb, str(user.id), connection_id)
    try:
        adapter.load_markets()
        rules = adapter.market_rules(symbol)
        price = adapter.price(symbol)
        quote = rules.get("quote") or "USDT"
        free_quote = adapter.quote_balance(quote)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_502_BAD_GATEWAY,
                            f"could not read market: {str(exc)[:160]}") from exc
    base_free = 0.0
    try:
        for b in adapter.balances():
            if b.asset == (rules.get("base") or ""):
                base_free = b.free
    except Exception:
        pass
    return {"symbol": symbol, "price": price, "rules": rules,
            "free_quote": free_quote, "free_base": base_free,
            "max_notional": _max_manual_notional(),
            "mode": execution_mode()}


@router.post("/trade")
async def manual_trade(body: ManualOrderIn, request: Request,
                       user=Depends(get_current_user),
                       sb: Client = Depends(get_supabase)) -> dict:
    uid = str(user.id)
    mode = execution_mode()

    if body.side not in ("buy", "sell"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "side must be buy or sell")
    if not body.confirm:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "confirm must be true — this places an order on your account")
    if not body.notional and not body.amount:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "provide either notional (quote to spend) or amount (base units)")

    adapter = _load_adapter(sb, uid, body.connection_id)   # ownership enforced inside
    try:
        adapter.load_markets()
        rules = adapter.market_rules(body.symbol)
        price = adapter.price(body.symbol)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_502_BAD_GATEWAY,
                            f"could not read market: {str(exc)[:160]}") from exc

    if not rules.get("active", True):
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "this market is not active on the exchange")

    amount = body.amount or ((body.notional or 0) / price if price else 0)
    amount = adapter.amount_to_precision(body.symbol, amount)
    notional = amount * price

    # ceiling — a leader's fills fan out, so a fat finger is expensive
    ceiling = _max_manual_notional()
    if notional > ceiling:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Order value {notional:.2f} exceeds the manual trade limit of "
            f"{ceiling:.2f}. Raise COPY_MAX_MANUAL_NOTIONAL to allow larger orders.")

    mn_a, mn_c = rules.get("min_amount"), rules.get("min_cost")
    if mn_a and amount < float(mn_a):
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"below exchange minimum size ({amount} < {mn_a})")
    if mn_c and notional < float(mn_c):
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"below exchange minimum order value "
                            f"({notional:.2f} < {float(mn_c):.2f})")

    row = {"user_id": uid, "connection_id": body.connection_id,
           "symbol": body.symbol, "side": body.side,
           "notional": round(notional, 8), "amount": amount, "price": price,
           "mode": mode, "note": body.note[:200], "created_at": _now()}

    # DRY RUN — record the intent, send nothing
    if mode != "live":
        row["status"] = "simulated"
        try:
            sb.table("copy_manual_orders").insert(row).execute()
        except Exception:
            pass
        _audit(sb, uid, "manual_trade_simulated",
               {"symbol": body.symbol, "side": body.side,
                "notional": row["notional"]}, request)
        return {"ok": True, "mode": "dry", "status": "simulated",
                "symbol": body.symbol, "side": body.side,
                "amount": amount, "notional": round(notional, 2), "price": price,
                "message": (f"DRY RUN — would {body.side} {amount} {body.symbol} "
                            f"(~{notional:.2f}). Nothing was sent to the exchange. "
                            f"Set COPY_EXECUTION_MODE=live to trade for real.")}

    # LIVE
    try:
        order = adapter.create_spot_order(body.symbol, body.side, amount)
    except Exception as exc:  # noqa: BLE001
        row.update(status="failed", error=str(exc)[:200])
        try:
            sb.table("copy_manual_orders").insert(row).execute()
        except Exception:
            pass
        _audit(sb, uid, "manual_trade_failed",
               {"symbol": body.symbol, "error": type(exc).__name__}, request)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY,
                            f"exchange rejected the order: {str(exc)[:160]}") from exc

    row.update(status="filled", exchange_order_id=str(order.get("id") or ""))
    try:
        sb.table("copy_manual_orders").insert(row).execute()
    except Exception:
        pass
    _audit(sb, uid, "manual_trade_filled",
           {"symbol": body.symbol, "side": body.side,
            "notional": row["notional"], "order_id": row["exchange_order_id"]}, request)
    return {"ok": True, "mode": "live", "status": "filled",
            "symbol": body.symbol, "side": body.side, "amount": amount,
            "notional": round(notional, 2),
            "exchange_order_id": row["exchange_order_id"],
            "message": ("Order placed. If you are a leader, your followers will "
                        "be copied on the next poll.")}


@router.get("/trade/history")
async def manual_history(limit: int = 50, user=Depends(get_current_user),
                         sb: Client = Depends(get_supabase)) -> dict:
    try:
        rows = (sb.table("copy_manual_orders").select("*")
                .eq("user_id", str(user.id))
                .order("created_at", desc=True).limit(limit).execute()).data or []
    except Exception:
        rows = []
    return {"orders": rows, "mode": execution_mode()}


# ─────────────────────── master trader applications ───────────────────────
class ApplyIn(BaseModel):
    connection_id: str
    display_name: str = Field(min_length=2, max_length=60)
    headline: str = ""
    bio: str = ""
    strategy: str = "Balanced"
    country: str = ""
    experience: str = ""
    track_record: str = ""


@router.post("/apply-master")
async def apply_master(body: ApplyIn, request: Request,
                       user=Depends(get_current_user),
                       sb: Client = Depends(get_supabase)) -> dict:
    """Apply to become a master trader others can copy."""
    uid = str(user.id)
    own = (sb.table("copy_connections").select("id,exchange_id")
           .eq("id", body.connection_id).eq("user_id", uid).execute()).data
    if not own:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "connection not found")

    row = {"user_id": uid, "connection_id": body.connection_id,
           "display_name": body.display_name, "headline": body.headline[:140],
           "bio": body.bio[:1000], "strategy": body.strategy,
           "country": body.country[:60], "experience": body.experience[:500],
           "track_record": body.track_record[:500],
           "approval_status": "pending", "is_public": False,
           "status": "active", "applied_at": _now()}
    try:
        existing = (sb.table("copy_leaders").select("id,approval_status")
                    .eq("user_id", uid).eq("connection_id", body.connection_id)
                    .execute()).data
        if existing:
            if existing[0].get("approval_status") == "approved":
                return {"ok": True, "already_approved": True,
                        "leader_id": existing[0]["id"],
                        "message": "You are already an approved master trader."}
            sb.table("copy_leaders").update(row).eq("id", existing[0]["id"]).execute()
            lid = existing[0]["id"]
        else:
            res = sb.table("copy_leaders").insert(row).execute()
            lid = (res.data or [{}])[0].get("id", "")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR,
                            f"could not submit application: {str(exc)[:200]}") from exc

    _audit(sb, uid, "master_application", {"leader_id": lid}, request)
    return {"ok": True, "leader_id": lid, "status": "pending",
            "message": ("Application submitted. A SKLZ admin will review it. "
                        "You will not appear publicly until approved.")}


@router.get("/my-application")
async def my_application(user=Depends(get_current_user),
                         sb: Client = Depends(get_supabase)) -> dict:
    try:
        rows = (sb.table("copy_leaders").select("*")
                .eq("user_id", str(user.id)).execute()).data or []
    except Exception:
        rows = []
    return {"applications": rows}


@router.get("/admin/applications")
async def list_applications(status_filter: str = "pending",
                            user=Depends(get_current_user),
                            sb: Client = Depends(get_supabase)) -> dict:
    if not _is_admin(user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "admin only")
    try:
        rows = (sb.table("copy_leaders").select("*")
                .eq("approval_status", status_filter)
                .order("applied_at", desc=True).execute()).data or []
    except Exception:
        rows = []
    return {"applications": rows, "count": len(rows)}


class ReviewIn(BaseModel):
    approve: bool
    note: str = ""


@router.post("/admin/applications/{leader_id}/review")
async def review_application(leader_id: str, body: ReviewIn, request: Request,
                             user=Depends(get_current_user),
                             sb: Client = Depends(get_supabase)) -> dict:
    if not _is_admin(user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "admin only")
    upd = {"approval_status": "approved" if body.approve else "rejected",
           "is_public": bool(body.approve),
           "review_note": body.note[:500], "reviewed_at": _now()}
    res = sb.table("copy_leaders").update(upd).eq("id", leader_id).execute()
    if not res.data:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "application not found")
    _audit(sb, str(user.id), "application_review",
           {"leader_id": leader_id, "approved": body.approve}, request)
    return {"ok": True, "approved": body.approve, "leader": res.data[0]}


@router.get("/symbols/{connection_id}")
async def tradeable_symbols(connection_id: str,
                            quote: str = "USDT",
                            user=Depends(get_current_user),
                            sb: Client = Depends(get_supabase)) -> dict:
    """Spot pairs this connection can actually trade, with their minimums.

    Read from the exchange rather than hard-coded, so the list matches what
    the venue really offers and the minimum notional shown is the real one —
    a rejected order because $5 was below the floor is a confusing failure.
    """
    # _load_adapter enforces ownership of the connection
    adapter = _load_adapter(sb, str(user.id), connection_id)
    try:
        markets = adapter.load_markets()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_502_BAD_GATEWAY,
                            f"could not read markets: {str(exc)[:160]}") from exc

    q = (quote or "USDT").upper()
    out = []
    for sym, m in (markets or {}).items():
        if not m.get("spot") or not m.get("active"):
            continue
        if (m.get("quote") or "").upper() != q:
            continue
        limits = m.get("limits") or {}
        cost = limits.get("cost") or {}
        amount = limits.get("amount") or {}
        out.append({
            "symbol": sym,
            "base": m.get("base"),
            "quote": m.get("quote"),
            "min_notional": cost.get("min"),
            "min_amount": amount.get("min"),
        })

    # majors first, then alphabetical — the common ones should be reachable
    priority = ["BTC", "ETH", "SOL", "XRP", "BNB", "ADA", "DOGE", "LINK",
                "AVAX", "DOT", "MATIC", "LTC", "TRX", "ATOM"]
    def rank(row):
        b = (row.get("base") or "").upper()
        return (priority.index(b) if b in priority else 999, b)
    out.sort(key=rank)

    return {"symbols": out, "count": len(out), "quote": q,
            "note": ("Minimum notional is the exchange's own floor — an order "
                     "below it will be rejected by the venue, not by us.")}
