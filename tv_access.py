"""TradingView invite-only access management.

THE CONSTRAINT, STATED HONESTLY
  TradingView has no official API for granting script access. The only
  supported mechanism is the "Manage Access" button on each script's page,
  which lets the author add a username with an optional expiry date.
  (Unofficial automations exist but require disabling 2FA and use unsupported
  backend calls — unacceptable risk for a business account holding four
  commercial scripts.)

SO THIS ROUTER IS THE WORKFLOW AROUND THAT BUTTON
  1. A client saves their TradingView username here (dashboard form).
  2. The admin sees a queue of pending grants.
  3. Admin opens TradingView -> script -> Manage Access -> adds the username,
     sets the expiry (+30d monthly, none for lifetime)  — ~15 seconds.
  4. Admin marks it granted here; the dashboard then tracks the expiry and
     shows who is due for renewal or removal.

  The client sees their live status (pending / active until X / lifetime /
  expired) on their own dashboard.

ENDPOINTS
  client:  POST /api/tv/request      GET /api/tv/mine
  admin:   GET  /api/tv/queue        POST /api/tv/grant
           POST /api/tv/revoke
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from supabase import Client

from auth import get_current_user
from db import get_supabase

router = APIRouter(prefix="/api/tv", tags=["tradingview"])

PRODUCT = "SKLZ Indicator Suite"          # one grant covers all 4 indicators
TV_USERNAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{3,30}$")


def _admin_emails() -> set[str]:
    raw = os.environ.get("ADMIN_EMAILS", "fxfactor24@gmail.com")
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


def _require_admin(user) -> None:
    email = (getattr(user, "email", "") or "").lower()
    if email not in _admin_emails():
        raise HTTPException(status.HTTP_403_FORBIDDEN, "admin only")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _status_of(row: dict) -> str:
    """Derive the live status; 'active' rows past expiry read as 'expired'."""
    s = row.get("status", "pending")
    exp = row.get("expires_at")
    if s == "active" and exp:
        try:
            dt = datetime.fromisoformat(str(exp).replace("Z", "+00:00"))
            if dt < _now():
                return "expired"
        except Exception:
            pass
    return s


class RequestIn(BaseModel):
    tv_username: str = Field(min_length=3, max_length=30)
    plan: str = "monthly"                  # monthly | lifetime


class GrantIn(BaseModel):
    request_id: int
    plan: str = "monthly"                  # monthly | lifetime


class RevokeIn(BaseModel):
    request_id: int


# ------------------------------------------------------------------- client
@router.post("/request")
async def request_access(payload: RequestIn, user=Depends(get_current_user),
                         sb: Client = Depends(get_supabase)) -> dict:
    tv = payload.tv_username.strip()
    if not TV_USERNAME_RE.match(tv):
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "TradingView usernames are 3-30 characters: "
                            "letters, digits, underscore, dot, or dash")
    if payload.plan not in ("monthly", "lifetime"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "plan must be monthly or lifetime")

    uid = str(user.id)
    # one live request per user: replace any prior pending row
    try:
        existing = (sb.table("tv_access").select("*")
                      .eq("user_id", uid).execute()).data or []
        for row in existing:
            if row.get("status") == "pending":
                sb.table("tv_access").delete().eq("id", row["id"]).execute()
    except Exception:
        pass

    row = {
        "user_id": uid,
        "email": getattr(user, "email", ""),
        "tv_username": tv,
        "product": PRODUCT,
        "plan": payload.plan,
        "status": "pending",
        "requested_at": _now().isoformat(),
    }
    try:
        sb.table("tv_access").insert(row).execute()
    except Exception as exc:  # noqa: BLE001  (missing table, RLS, network)
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            f"could not store the request — is the tv_access table created? ({exc})",
        ) from exc
    return {"ok": True, "status": "pending", "tv_username": tv,
            "note": "Access is granted manually on TradingView — usually "
                    "within a few hours."}


@router.get("/mine")
async def my_access(user=Depends(get_current_user),
                    sb: Client = Depends(get_supabase)) -> dict:
    uid = str(user.id)
    rows = (sb.table("tv_access").select("*")
              .eq("user_id", uid).execute()).data or []
    rows.sort(key=lambda r: r.get("requested_at", ""), reverse=True)
    if not rows:
        return {"exists": False}
    r = rows[0]
    return {"exists": True,
            "tv_username": r.get("tv_username"),
            "product": r.get("product", PRODUCT),
            "plan": r.get("plan", "monthly"),
            "status": _status_of(r),
            "expires_at": r.get("expires_at"),
            "granted_at": r.get("granted_at")}


# -------------------------------------------------------------------- admin
@router.get("/queue")
async def queue(user=Depends(get_current_user),
                sb: Client = Depends(get_supabase)) -> dict:
    _require_admin(user)
    try:
        rows = (sb.table("tv_access").select("*").execute()).data or []
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR,
                            f"tv_access table unreachable ({exc})") from exc
    for r in rows:
        r["live_status"] = _status_of(r)
    pending = [r for r in rows if r["live_status"] == "pending"]
    active = [r for r in rows if r["live_status"] == "active"]
    expired = [r for r in rows if r["live_status"] in ("expired", "revoked")]
    # soonest expiries first so renewals are visible
    active.sort(key=lambda r: r.get("expires_at") or "9999")
    pending.sort(key=lambda r: r.get("requested_at", ""))
    return {"pending": pending, "active": active, "expired": expired}


@router.post("/grant")
async def grant(payload: GrantIn, user=Depends(get_current_user),
                sb: Client = Depends(get_supabase)) -> dict:
    _require_admin(user)
    expires = None
    if payload.plan == "monthly":
        expires = (_now() + timedelta(days=30)).isoformat()
    elif payload.plan != "lifetime":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "plan must be monthly or lifetime")

    upd = {"status": "active", "plan": payload.plan,
           "granted_at": _now().isoformat(), "expires_at": expires}
    try:
        res = (sb.table("tv_access").update(upd)
                 .eq("id", payload.request_id).execute())
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR,
                            f"tv_access table unreachable ({exc})") from exc
    if not res.data:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "request not found")
    return {"ok": True, **upd,
            "reminder": "Make sure the username is added in TradingView -> "
                        "script page -> Manage Access with the same expiry."}


@router.post("/revoke")
async def revoke(payload: RevokeIn, user=Depends(get_current_user),
                 sb: Client = Depends(get_supabase)) -> dict:
    _require_admin(user)
    try:
        res = (sb.table("tv_access")
                 .update({"status": "revoked", "expires_at": _now().isoformat()})
                 .eq("id", payload.request_id).execute())
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR,
                            f"tv_access table unreachable ({exc})") from exc
    if not res.data:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "request not found")
    return {"ok": True,
            "reminder": "Remove the username in TradingView -> Manage Access "
                        "as well — this dashboard only tracks state."}
