"""SKLZ Affiliate — single-level referrals, free-month credit reward.

Model (Phase 1):
  - every user has a referral CODE and link: sklzlabs.com/?ref=CODE
  - when a new user signs up with ?ref=CODE, we record referred_by
  - when that referred user becomes a PAYING subscriber, the referrer earns
    one free-month CREDIT (recorded; applied to their next invoice manually
    or via billing integration later)
  - single level only: no referrals-of-referrals (kept clean + compliant)

Credit, not cash: discounts your own product, so there's no payout/tax
liability. Triggered on paid conversion so free-signups can't be farmed.

Endpoints:
  GET  /api/affiliate/me            my code, link, stats, referrals, credits
  POST /api/affiliate/attribute     record a referral at signup (public-ish)
  POST /api/affiliate/convert       mark a referral converted (billing webhook)
"""
from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Header, status
from pydantic import BaseModel
from supabase import Client

from auth import get_current_user
from db import get_supabase

router = APIRouter(prefix="/api/affiliate", tags=["affiliate"])

SITE = os.environ.get("SITE_URL", "https://www.sklzlabs.com")
REWARD_MONTHS = 1                 # free months per converted referral


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _code_for(uid: str) -> str:
    """Deterministic short code from user id — stable, no collisions in practice."""
    h = hashlib.sha256(uid.encode()).hexdigest()
    return h[:8]


def _ensure_row(sb: Client, uid: str) -> dict:
    """Get or create the affiliate row for a user."""
    try:
        r = (sb.table("affiliates").select("*").eq("user_id", uid).execute()).data
        if r:
            return r[0]
    except Exception:
        pass
    row = {"user_id": uid, "code": _code_for(uid),
           "credits_months": 0, "created_at": _now()}
    try:
        sb.table("affiliates").insert(row).execute()
    except Exception:
        pass
    return row


@router.get("/me")
async def me(user=Depends(get_current_user),
             sb: Client = Depends(get_supabase)) -> dict:
    uid = str(user.id)
    row = _ensure_row(sb, uid)
    code = row["code"]
    # who did I refer?
    try:
        refs = (sb.table("referrals").select("*")
                .eq("referrer_id", uid)
                .order("created_at", desc=True).execute()).data or []
    except Exception:
        refs = []
    joined = len(refs)
    converted = sum(1 for r in refs if r.get("converted"))
    # privacy: don't expose referred users' emails/ids, just masked + status
    ref_list = [{
        "joined_at": r.get("created_at"),
        "status": "paid" if r.get("converted") else "signed up",
        "masked": (r.get("referred_email") or "new user")[:2] + "•••"
                  if r.get("referred_email") else "new user",
    } for r in refs]
    return {
        "code": code,
        "link": f"{SITE}/?ref={code}",
        "joined": joined,
        "converted": converted,
        "pending": joined - converted,
        "credits_months": row.get("credits_months", 0),
        "reward_per_conversion": REWARD_MONTHS,
        "referrals": ref_list,
    }


class Attribute(BaseModel):
    code: str
    new_user_id: str
    new_user_email: str = ""


@router.post("/attribute")
async def attribute(body: Attribute, sb: Client = Depends(get_supabase)) -> dict:
    """Record that new_user signed up via `code`. Called right after signup.
    Idempotent + self-referral guarded. Does NOT reward yet (needs conversion)."""
    code = body.code.strip().lower()
    try:
        owner = (sb.table("affiliates").select("user_id")
                 .eq("code", code).execute()).data
    except Exception:
        owner = None
    if not owner:
        return {"ok": False, "reason": "unknown code"}
    referrer_id = owner[0]["user_id"]
    if referrer_id == body.new_user_id:
        return {"ok": False, "reason": "self-referral ignored"}
    # already attributed?
    try:
        exists = (sb.table("referrals").select("id")
                  .eq("referred_id", body.new_user_id).execute()).data
        if exists:
            return {"ok": True, "already": True}
    except Exception:
        pass
    try:
        sb.table("referrals").insert({
            "referrer_id": referrer_id,
            "referred_id": body.new_user_id,
            "referred_email": body.new_user_email,
            "converted": False,
            "created_at": _now(),
        }).execute()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR,
                            f"attribute failed: {exc}") from exc
    return {"ok": True}


@router.post("/convert")
async def convert(referred_user_id: str,
                  authorization: str = Header(default=""),
                  sb: Client = Depends(get_supabase)) -> dict:
    """Mark a referred user as converted (became paying) and credit the referrer.
    Gated by INTERNAL_KEY so only the billing flow can call it."""
    expected = os.environ.get("INTERNAL_KEY", "") or os.environ.get("BOT_INGEST_KEY", "")
    token = authorization.replace("Bearer ", "").strip()
    if not expected or token != expected:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "unauthorized")
    try:
        rows = (sb.table("referrals").select("*")
                .eq("referred_id", referred_user_id)
                .eq("converted", False).execute()).data or []
    except Exception:
        rows = []
    if not rows:
        return {"ok": True, "credited": 0, "note": "no pending referral"}
    ref = rows[0]
    # mark converted
    sb.table("referrals").update({"converted": True, "converted_at": _now()}) \
        .eq("id", ref["id"]).execute()
    # credit the referrer
    referrer_id = ref["referrer_id"]
    arow = _ensure_row(sb, referrer_id)
    new_credits = (arow.get("credits_months", 0) or 0) + REWARD_MONTHS
    sb.table("affiliates").update({"credits_months": new_credits}) \
        .eq("user_id", referrer_id).execute()
    return {"ok": True, "credited": REWARD_MONTHS, "referrer_total": new_credits}
