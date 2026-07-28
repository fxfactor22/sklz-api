"""SKLZ — account profile and security.

Everything a user needs to manage their own account:
  - display name, phone, address, preferred language
  - change password (verifies the current one first)
  - two-factor authentication via TOTP (authenticator app)

2FA uses Supabase's own MFA, so the secret and verification live with the
auth provider rather than being reimplemented here. Reimplementing TOTP is
a good way to get it subtly wrong.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from supabase import Client

from auth import get_current_user
from db import get_supabase

router = APIRouter(prefix="/api/profile", tags=["profile"])

LANGS = ("en", "ar", "ru")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ProfileIn(BaseModel):
    display_name: str = Field(default="", max_length=80)
    phone: str = Field(default="", max_length=40)
    address_line1: str = Field(default="", max_length=120)
    address_line2: str = Field(default="", max_length=120)
    city: str = Field(default="", max_length=80)
    country: str = Field(default="", max_length=80)
    postcode: str = Field(default="", max_length=20)
    language: str = "en"


@router.get("")
async def get_profile(user=Depends(get_current_user),
                      sb: Client = Depends(get_supabase)) -> dict:
    uid = str(user.id)
    row = {}
    try:
        rows = (sb.table("user_profiles").select("*")
                .eq("user_id", uid).execute()).data or []
        row = rows[0] if rows else {}
    except Exception:
        pass
    return {
        "email": getattr(user, "email", ""),
        "display_name": row.get("display_name", ""),
        "phone": row.get("phone", ""),
        "address_line1": row.get("address_line1", ""),
        "address_line2": row.get("address_line2", ""),
        "city": row.get("city", ""),
        "country": row.get("country", ""),
        "postcode": row.get("postcode", ""),
        "language": row.get("language", "en"),
        "two_factor_enabled": bool(row.get("totp_factor_id")),
    }


@router.put("")
async def save_profile(body: ProfileIn, user=Depends(get_current_user),
                       sb: Client = Depends(get_supabase)) -> dict:
    uid = str(user.id)
    lang = body.language if body.language in LANGS else "en"
    row = {"user_id": uid, **body.model_dump(), "language": lang,
           "updated_at": _now()}
    try:
        existing = (sb.table("user_profiles").select("user_id")
                    .eq("user_id", uid).execute()).data
        if existing:
            sb.table("user_profiles").update(row).eq("user_id", uid).execute()
        else:
            sb.table("user_profiles").insert(row).execute()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR,
                            f"could not save profile: {str(exc)[:180]}") from exc
    return {"ok": True, "message": "Profile saved."}


# ── two-factor authentication ───────────────────────────────────────
@router.post("/2fa/enroll")
async def enroll_2fa(user=Depends(get_current_user),
                     sb: Client = Depends(get_supabase)) -> dict:
    """Start 2FA setup. Returns a QR code for an authenticator app.

    Not active until /2fa/verify is called with a code from the app — so a
    half-finished setup can never lock anyone out.
    """
    try:
        res = sb.auth.mfa.enroll({"factor_type": "totp",
                                  "friendly_name": "SKLZ Labs"})
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR,
                            f"could not start 2FA setup: {str(exc)[:180]}") from exc
    totp = getattr(res, "totp", None) or {}
    return {
        "ok": True,
        "factor_id": getattr(res, "id", ""),
        "qr_code": getattr(totp, "qr_code", "") if totp else "",
        "secret": getattr(totp, "secret", "") if totp else "",
        "uri": getattr(totp, "uri", "") if totp else "",
        "note": ("Scan the QR code with an authenticator app, then enter the "
                 "6-digit code to finish. 2FA is not active until you do."),
    }


class VerifyIn(BaseModel):
    factor_id: str
    code: str = Field(min_length=6, max_length=8)


@router.post("/2fa/verify")
async def verify_2fa(body: VerifyIn, user=Depends(get_current_user),
                     sb: Client = Depends(get_supabase)) -> dict:
    """Finish 2FA setup by proving the app is generating correct codes."""
    uid = str(user.id)
    try:
        ch = sb.auth.mfa.challenge({"factor_id": body.factor_id})
        sb.auth.mfa.verify({"factor_id": body.factor_id,
                            "challenge_id": getattr(ch, "id", ""),
                            "code": body.code})
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "That code was not accepted. Check your "
                            "authenticator app and try again.") from exc
    try:
        existing = (sb.table("user_profiles").select("user_id")
                    .eq("user_id", uid).execute()).data
        payload = {"totp_factor_id": body.factor_id, "updated_at": _now()}
        if existing:
            sb.table("user_profiles").update(payload).eq("user_id", uid).execute()
        else:
            sb.table("user_profiles").insert({"user_id": uid, **payload}).execute()
    except Exception:
        pass
    return {"ok": True,
            "message": ("Two-factor authentication is on. You will need your "
                        "authenticator app to sign in from now on.")}


class Disable2FA(BaseModel):
    password: str


@router.post("/2fa/disable")
async def disable_2fa(body: Disable2FA, user=Depends(get_current_user),
                      sb: Client = Depends(get_supabase)) -> dict:
    """Turn 2FA off. Requires the account password, not just a session."""
    uid = str(user.id)
    email = getattr(user, "email", "")
    try:
        sb.auth.sign_in_with_password({"email": email,
                                       "password": body.password})
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "Password is not correct.") from exc
    try:
        rows = (sb.table("user_profiles").select("totp_factor_id")
                .eq("user_id", uid).execute()).data or []
        fid = rows[0].get("totp_factor_id") if rows else ""
        if fid:
            try:
                sb.auth.mfa.unenroll({"factor_id": fid})
            except Exception:
                pass
        sb.table("user_profiles").update(
            {"totp_factor_id": "", "updated_at": _now()}
        ).eq("user_id", uid).execute()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR,
                            f"could not disable 2FA: {str(exc)[:180]}") from exc
    return {"ok": True, "message": "Two-factor authentication is off."}
