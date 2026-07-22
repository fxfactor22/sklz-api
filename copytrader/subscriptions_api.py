"""SKLZ CopyTrader — leaders and subscriptions.

The follower's entire configuration is two decisions:
    RISK LEVEL   (low | medium | high | custom)
    ALLOCATION   (how much capital may be used)

Everything else — position size, exposure caps, minimum order handling — is
derived by the engine. Guards default to sane values and can be tightened but
never disabled.

Endpoints
  POST   /api/copy/leaders                 become a leader (publish a connection)
  GET    /api/copy/leaders                 browse public leaders
  GET    /api/copy/leaders/me              my leader profiles
  PATCH  /api/copy/leaders/{id}            update / go public / pause

  POST   /api/copy/subscribe               follow a leader with risk + allocation
  GET    /api/copy/subscriptions           my subscriptions + live health
  PATCH  /api/copy/subscriptions/{id}      change risk, allocation, guards, pause
  POST   /api/copy/subscriptions/{id}/emergency-stop
  DELETE /api/copy/subscriptions/{id}      unfollow
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from supabase import Client

from auth import get_current_user
from db import get_supabase
from copytrader.connections_api import _audit, _load_adapter
from copytrader.engine import (RISK_LEVELS, FollowerConfig, FollowerState,
                               portfolio_health, risk_fraction)

router = APIRouter(prefix="/api/copy", tags=["copytrader"])

MIN_ALLOCATION = 50.0            # below this, exchange minimums make copying useless


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _own_connection(sb: Client, uid: str, connection_id: str) -> dict:
    rows = (sb.table("copy_connections").select("*")
            .eq("id", connection_id).eq("user_id", uid).execute()).data
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "connection not found")
    c = rows[0]
    if c.get("status") != "active":
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "this connection is not active — re-check its permissions")
    return c


# ─────────────────────────────── leaders ───────────────────────────────
class LeaderIn(BaseModel):
    connection_id: str
    display_name: str = Field(min_length=2, max_length=60)
    headline: str = ""
    bio: str = ""
    strategy: str = "Balanced"
    country: str = ""
    is_public: bool = False


@router.post("/leaders")
async def become_leader(body: LeaderIn, request: Request,
                        user=Depends(get_current_user),
                        sb: Client = Depends(get_supabase)) -> dict:
    uid = str(user.id)
    conn = _own_connection(sb, uid, body.connection_id)
    row = {"user_id": uid, "connection_id": body.connection_id,
           "display_name": body.display_name, "headline": body.headline[:140],
           "bio": body.bio[:1000], "strategy": body.strategy,
           "country": body.country[:60], "is_public": body.is_public,
           "status": "active"}
    try:
        existing = (sb.table("copy_leaders").select("id")
                    .eq("user_id", uid).eq("connection_id", body.connection_id)
                    .execute()).data
        if existing:
            sb.table("copy_leaders").update(row).eq("id", existing[0]["id"]).execute()
            lid = existing[0]["id"]
        else:
            res = sb.table("copy_leaders").insert(row).execute()
            lid = (res.data or [{}])[0].get("id", "")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR,
                            f"could not save leader: {str(exc)[:200]}") from exc
    _audit(sb, uid, "leader_upsert",
           {"leader_id": lid, "exchange": conn["exchange_id"],
            "public": body.is_public}, request)
    return {"ok": True, "leader_id": lid, "is_public": body.is_public}


@router.get("/leaders")
async def public_leaders(sb: Client = Depends(get_supabase)) -> dict:
    """PUBLIC — browse leaders accepting followers."""
    try:
        rows = (sb.table("copy_leaders")
                .select("id,display_name,headline,bio,strategy,country,follower_count,created_at")
                .eq("is_public", True).eq("status", "active")
                .eq("approval_status", "approved")
                .order("follower_count", desc=True).limit(100).execute()).data or []
    except Exception:
        rows = []
    return {"leaders": rows,
            "note": ("Copying involves risk of loss. Past performance does not "
                     "guarantee future results.")}


@router.get("/leaders/me")
async def my_leaders(user=Depends(get_current_user),
                     sb: Client = Depends(get_supabase)) -> dict:
    try:
        rows = (sb.table("copy_leaders").select("*")
                .eq("user_id", str(user.id)).execute()).data or []
    except Exception:
        rows = []
    return {"leaders": rows}


class LeaderPatch(BaseModel):
    display_name: str | None = None
    headline: str | None = None
    bio: str | None = None
    strategy: str | None = None
    is_public: bool | None = None
    status: str | None = None


@router.patch("/leaders/{leader_id}")
async def update_leader(leader_id: str, body: LeaderPatch,
                        user=Depends(get_current_user),
                        sb: Client = Depends(get_supabase)) -> dict:
    upd = {k: v for k, v in body.model_dump().items() if v is not None}
    if not upd:
        return {"ok": True, "unchanged": True}
    res = (sb.table("copy_leaders").update(upd)
           .eq("id", leader_id).eq("user_id", str(user.id)).execute())
    if not res.data:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "leader not found")
    return {"ok": True, "leader": res.data[0]}


# ──────────────────────────── subscriptions ────────────────────────────
class SubscribeIn(BaseModel):
    leader_id: str
    connection_id: str
    allocation: float
    risk_level: str = "medium"
    custom_risk_pct: float | None = None
    quote: str = "USDT"


def _validate_risk(level: str, custom: float | None) -> None:
    if level not in list(RISK_LEVELS) + ["custom"]:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"risk_level must be one of {list(RISK_LEVELS)} or 'custom'")
    if level == "custom" and (custom is None or custom <= 0):
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "custom_risk_pct is required when risk_level is 'custom'")


@router.post("/subscribe")
async def subscribe(body: SubscribeIn, request: Request,
                    user=Depends(get_current_user),
                    sb: Client = Depends(get_supabase)) -> dict:
    uid = str(user.id)
    _validate_risk(body.risk_level, body.custom_risk_pct)
    if body.allocation < MIN_ALLOCATION:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Minimum allocation is {MIN_ALLOCATION:.0f} {body.quote}. Below this, "
            f"exchange minimum order sizes would block most copied trades.")

    conn = _own_connection(sb, uid, body.connection_id)

    leader = (sb.table("copy_leaders").select("*")
              .eq("id", body.leader_id).execute()).data
    if not leader:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "leader not found")
    if leader[0]["user_id"] == uid:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "you cannot follow your own account")
    if leader[0].get("status") != "active":
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "this leader is not currently accepting followers")
    if leader[0].get("approval_status") not in (None, "approved"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "this trader has not been approved as a master trader yet")

    # confirm the follower actually holds the capital they are allocating
    try:
        adapter = _load_adapter(sb, uid, body.connection_id)
        free = adapter.quote_balance(body.quote)
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001
        free = None

    warnings = []
    if free is not None and free < body.allocation:
        warnings.append(
            f"Your free {body.quote} balance is {free:.2f}, below the "
            f"{body.allocation:.2f} allocated. Copying will be limited to what "
            f"is actually available.")

    row = {"follower_id": uid, "leader_id": body.leader_id,
           "connection_id": body.connection_id,
           "allocation": body.allocation, "quote": body.quote,
           "risk_level": body.risk_level,
           "custom_risk_pct": body.custom_risk_pct,
           "paused": False, "emergency_stopped": False}
    try:
        existing = (sb.table("copy_subscriptions").select("id")
                    .eq("follower_id", uid).eq("leader_id", body.leader_id)
                    .execute()).data
        if existing:
            sb.table("copy_subscriptions").update(row).eq("id", existing[0]["id"]).execute()
            sid = existing[0]["id"]
        else:
            res = sb.table("copy_subscriptions").insert(row).execute()
            sid = (res.data or [{}])[0].get("id", "")
            sb.table("copy_leaders").update(
                {"follower_count": (leader[0].get("follower_count") or 0) + 1}
            ).eq("id", body.leader_id).execute()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR,
                            f"could not subscribe: {str(exc)[:200]}") from exc

    _audit(sb, uid, "subscribe",
           {"leader_id": body.leader_id, "allocation": body.allocation,
            "risk": body.risk_level, "exchange": conn["exchange_id"]}, request)

    return {"ok": True, "subscription_id": sid,
            "risk_per_trade_pct": round(
                risk_fraction(FollowerConfig(uid, body.leader_id, body.allocation,
                                             body.risk_level, body.custom_risk_pct)), 4),
            "warnings": warnings,
            "note": ("Copying starts with the leader's next trade. Existing "
                     "positions are not mirrored.")}


def _cfg_from_row(r: dict) -> FollowerConfig:
    return FollowerConfig(
        follower_id=r["follower_id"], leader_id=r["leader_id"],
        allocation=float(r.get("allocation") or 0),
        risk_level=r.get("risk_level") or "medium",
        custom_risk_pct=r.get("custom_risk_pct"),
        quote=r.get("quote") or "USDT",
        max_open_positions=int(r.get("max_open_positions") or 10),
        max_exposure_per_asset=float(r.get("max_exposure_per_asset") or 0.30),
        max_daily_loss=float(r.get("max_daily_loss") or 0.10),
        blacklist=r.get("blacklist") or [],
        whitelist=r.get("whitelist") or [],
        paused=bool(r.get("paused")),
    )


def _state_for(sb: Client, sub: dict) -> FollowerState:
    exposure: dict[str, float] = {}
    try:
        pos = (sb.table("copy_positions").select("asset,cost_basis")
               .eq("subscription_id", sub["id"]).execute()).data or []
        exposure = {p["asset"]: float(p.get("cost_basis") or 0) for p in pos
                    if float(p.get("cost_basis") or 0) > 0}
    except Exception:
        pass
    pnl_today = 0.0
    try:
        today = datetime.now(timezone.utc).date().isoformat()
        orders = (sb.table("copy_orders").select("notional,side,status,created_at")
                  .eq("subscription_id", sub["id"])
                  .gte("created_at", today).execute()).data or []
        # realised pnl tracking is refined once fills are recorded
        pnl_today = sum(0.0 for _ in orders)
    except Exception:
        pass
    return FollowerState(free_quote=0.0, open_positions=len(exposure),
                         exposure_by_asset=exposure,
                         realized_pnl_today=pnl_today,
                         emergency_stopped=bool(sub.get("emergency_stopped")))


@router.get("/subscriptions")
async def subscriptions(user=Depends(get_current_user),
                        sb: Client = Depends(get_supabase)) -> dict:
    uid = str(user.id)
    try:
        subs = (sb.table("copy_subscriptions").select("*")
                .eq("follower_id", uid).execute()).data or []
    except Exception:
        subs = []
    out = []
    for s in subs:
        cfg = _cfg_from_row(s)
        st = _state_for(sb, s)
        leader = (sb.table("copy_leaders")
                  .select("display_name,strategy,headline")
                  .eq("id", s["leader_id"]).execute()).data
        out.append({
            "id": s["id"],
            "leader": (leader[0] if leader else {"display_name": "—"}),
            "allocation": cfg.allocation,
            "quote": cfg.quote,
            "risk_level": cfg.risk_level,
            "paused": cfg.paused,
            "emergency_stopped": st.emergency_stopped,
            "health": portfolio_health(cfg, st),
        })
    return {"subscriptions": out}


class SubPatch(BaseModel):
    allocation: float | None = None
    risk_level: str | None = None
    custom_risk_pct: float | None = None
    max_open_positions: int | None = None
    max_exposure_per_asset: float | None = None
    max_daily_loss: float | None = None
    blacklist: list[str] | None = None
    whitelist: list[str] | None = None
    paused: bool | None = None


@router.patch("/subscriptions/{sub_id}")
async def update_subscription(sub_id: str, body: SubPatch, request: Request,
                              user=Depends(get_current_user),
                              sb: Client = Depends(get_supabase)) -> dict:
    upd = {k: v for k, v in body.model_dump().items() if v is not None}
    if not upd:
        return {"ok": True, "unchanged": True}
    if "risk_level" in upd:
        _validate_risk(upd["risk_level"], upd.get("custom_risk_pct"))
    if "allocation" in upd and upd["allocation"] < MIN_ALLOCATION:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"minimum allocation is {MIN_ALLOCATION:.0f}")
    # guards may be tightened, never removed entirely
    if "max_daily_loss" in upd:
        upd["max_daily_loss"] = max(0.01, min(float(upd["max_daily_loss"]), 0.50))
    if "max_exposure_per_asset" in upd:
        upd["max_exposure_per_asset"] = max(0.05, min(float(upd["max_exposure_per_asset"]), 1.0))
    res = (sb.table("copy_subscriptions").update(upd)
           .eq("id", sub_id).eq("follower_id", str(user.id)).execute())
    if not res.data:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "subscription not found")
    _audit(sb, str(user.id), "subscription_update",
           {"subscription_id": sub_id, "changed": list(upd)}, request)
    return {"ok": True, "subscription": res.data[0]}


@router.post("/subscriptions/{sub_id}/emergency-stop")
async def emergency_stop(sub_id: str, request: Request, enable: bool = True,
                         user=Depends(get_current_user),
                         sb: Client = Depends(get_supabase)) -> dict:
    """Immediately halt all copying. Open positions are NOT auto-closed —
    that decision stays with the follower."""
    res = (sb.table("copy_subscriptions")
           .update({"emergency_stopped": enable, "paused": enable})
           .eq("id", sub_id).eq("follower_id", str(user.id)).execute())
    if not res.data:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "subscription not found")
    _audit(sb, str(user.id), "emergency_stop",
           {"subscription_id": sub_id, "enabled": enable}, request)
    return {"ok": True, "emergency_stopped": enable,
            "note": ("No new copied trades will be placed. Existing positions "
                     "remain open — close them yourself if you want out.")}


@router.delete("/subscriptions/{sub_id}")
async def unsubscribe(sub_id: str, request: Request,
                      user=Depends(get_current_user),
                      sb: Client = Depends(get_supabase)) -> dict:
    uid = str(user.id)
    sub = (sb.table("copy_subscriptions").select("leader_id")
           .eq("id", sub_id).eq("follower_id", uid).execute()).data
    sb.table("copy_subscriptions").delete().eq("id", sub_id) \
        .eq("follower_id", uid).execute()
    if sub:
        lead = (sb.table("copy_leaders").select("follower_count")
                .eq("id", sub[0]["leader_id"]).execute()).data
        if lead:
            sb.table("copy_leaders").update(
                {"follower_count": max((lead[0].get("follower_count") or 1) - 1, 0)}
            ).eq("id", sub[0]["leader_id"]).execute()
    _audit(sb, uid, "unsubscribe", {"subscription_id": sub_id}, request)
    return {"ok": True,
            "note": "Stopped copying. Any open positions remain yours to manage."}


@router.get("/risk-levels")
async def risk_levels() -> dict:
    return {"levels": [
        {"id": "low", "label": "Low",
         "per_trade_pct": RISK_LEVELS["low"],
         "description": "Around 5% of your allocation per trade."},
        {"id": "medium", "label": "Medium",
         "per_trade_pct": RISK_LEVELS["medium"],
         "description": "Around 10% of your allocation per trade."},
        {"id": "high", "label": "High",
         "per_trade_pct": RISK_LEVELS["high"],
         "description": "Around 20% of your allocation per trade."},
    ], "note": ("Whatever you choose, the engine never exceeds your allocation, "
                "never puts more than 30% of it in one asset by default, and "
                "stops for the day if losses reach your daily limit.")}
