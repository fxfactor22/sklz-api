"""Accounts: signup, login, session refresh, current-user, logout.

Built on Supabase Auth — we never handle raw passwords ourselves. Supabase
does hashing, email confirmation, token issuance, and reset flows. This API:
  - proxies signup/login to Supabase Auth (returns its JWT session)
  - mirrors each user into our own `profiles` table (for role + app data)
  - verifies incoming JWTs the trustworthy way (auth.get_user(token)),
    never by decoding client-side claims

The access token returned here is what the website stores and sends as
`Authorization: Bearer <token>` on later calls. `get_current_user` is the
dependency every protected route will use.
"""
from __future__ import annotations

import os
import time
from collections import defaultdict

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, EmailStr, Field
from supabase import Client

from db import get_supabase

router = APIRouter(prefix="/api/auth", tags=["auth"])

# --- basic per-IP rate limiting on auth endpoints ------------------------
_HITS: dict[str, list[float]] = defaultdict(list)


def _rate_ok(ip: str, max_per_min: int) -> bool:
    now = time.time()
    hits = [t for t in _HITS[ip] if now - t < 60.0]
    hits.append(now)
    _HITS[ip] = hits
    return len(hits) <= max_per_min


# --- schemas -------------------------------------------------------------
class SignupIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    display_name: str | None = Field(default=None, max_length=80)


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class RefreshIn(BaseModel):
    refresh_token: str


class SessionOut(BaseModel):
    access_token: str | None
    refresh_token: str | None
    user: dict
    email_confirmation_required: bool = False


class ProfileOut(BaseModel):
    id: str
    email: str
    display_name: str | None
    role: str


# --- helpers -------------------------------------------------------------
def _profile_from_user(user, sb: Client) -> dict:
    """Ensure a profiles row exists for this auth user; return it."""
    uid = user.id
    email = user.email
    meta = getattr(user, "user_metadata", {}) or {}
    display = meta.get("display_name")
    row = {
        "id": uid,
        "email": email,
        "display_name": display,
        "role": "user",
    }
    # Upsert but don't overwrite an existing role (only set on first insert).
    try:
        existing = sb.table("profiles").select("*").eq("id", uid).limit(1).execute()
        if existing.data:
            return existing.data[0]
        sb.table("profiles").insert(row).execute()
    except Exception:
        pass
    return row


def _user_public(user) -> dict:
    return {
        "id": user.id,
        "email": user.email,
        "email_confirmed": bool(getattr(user, "email_confirmed_at", None)),
    }


# --- routes --------------------------------------------------------------
@router.post("/signup", response_model=SessionOut)
async def signup(payload: SignupIn, request: Request,
                 sb: Client = Depends(get_supabase)) -> SessionOut:
    ip = request.client.host if request.client else "unknown"
    if not _rate_ok(f"signup:{ip}", 10):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS,
                            "Too many attempts. Please wait a minute.")
    try:
        res = sb.auth.sign_up({
            "email": payload.email,
            "password": payload.password,
            "options": {"data": {"display_name": payload.display_name}},
        })
    except Exception as exc:  # Supabase raises on duplicate / weak password etc.
        msg = str(exc)
        import sys as _s
        print(f"[signup-error] {type(exc).__name__}: {msg}", file=_s.stderr, flush=True)
        # Supabase already hides account-existence in most cases; keep generic.
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "Could not create account. " + _clean(msg))

    user = res.user
    session = res.session
    if user is not None:
        _profile_from_user(user, sb)

    # If email confirmation is on, session is None until they confirm.
    if session is None:
        return SessionOut(
            access_token=None, refresh_token=None,
            user=_user_public(user) if user else {},
            email_confirmation_required=True,
        )
    return SessionOut(
        access_token=session.access_token,
        refresh_token=session.refresh_token,
        user=_user_public(user),
    )


@router.post("/login", response_model=SessionOut)
async def login(payload: LoginIn, request: Request,
                sb: Client = Depends(get_supabase)) -> SessionOut:
    ip = request.client.host if request.client else "unknown"
    if not _rate_ok(f"login:{ip}", 10):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS,
                            "Too many attempts. Please wait a minute.")
    try:
        res = sb.auth.sign_in_with_password({
            "email": payload.email,
            "password": payload.password,
        })
    except Exception:
        # Never distinguish "wrong password" from "no such user".
        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                            "Invalid email or password.")
    if res.session is None or res.user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                            "Invalid email or password.")
    _profile_from_user(res.user, sb)
    return SessionOut(
        access_token=res.session.access_token,
        refresh_token=res.session.refresh_token,
        user=_user_public(res.user),
    )


@router.post("/refresh", response_model=SessionOut)
async def refresh(payload: RefreshIn, sb: Client = Depends(get_supabase)) -> SessionOut:
    try:
        res = sb.auth.refresh_session(payload.refresh_token)
    except Exception:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session expired. Please log in.")
    if res.session is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session expired. Please log in.")
    return SessionOut(
        access_token=res.session.access_token,
        refresh_token=res.session.refresh_token,
        user=_user_public(res.user) if res.user else {},
    )


def get_current_user(
    authorization: str = Header(default=""),
    sb: Client = Depends(get_supabase),
):
    """Dependency for protected routes. Verifies the bearer JWT with Supabase
    (trustworthy) and returns the auth user. Raises 401 if missing/invalid."""
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token.")
    token = authorization.split(" ", 1)[1].strip()
    try:
        res = sb.auth.get_user(token)
    except Exception:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token.")
    if res is None or res.user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token.")
    return res.user


@router.get("/me", response_model=ProfileOut)
async def me(user=Depends(get_current_user), sb: Client = Depends(get_supabase)) -> ProfileOut:
    profile = _profile_from_user(user, sb)
    return ProfileOut(
        id=profile["id"], email=profile["email"],
        display_name=profile.get("display_name"), role=profile.get("role", "user"),
    )


@router.post("/logout")
async def logout(authorization: str = Header(default=""),
                 sb: Client = Depends(get_supabase)) -> dict:
    # With JWTs, logout is primarily client-side (drop the tokens). We also
    # ask Supabase to revoke the refresh token for this session if possible.
    if authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
        try:
            sb.auth.admin.sign_out(token)  # revoke; best-effort
        except Exception:
            pass
    return {"ok": True}


def _clean(msg: str) -> str:
    """Trim provider error noise to something safe to show a user."""
    low = msg.lower()
    if "password" in low and ("weak" in low or "short" in low or "least" in low):
        return "Password must be at least 8 characters."
    if "already" in low or "registered" in low or "exists" in low:
        return "If this email is new, your account was created."
    return "Please check your details and try again."


# ── password reset ──────────────────────────────────────────────────
class ResetRequest(BaseModel):
    email: EmailStr


@router.post("/reset-password")
async def reset_password(payload: ResetRequest, request: Request,
                         sb: Client = Depends(get_supabase)) -> dict:
    """Send a password reset email.

    Always reports success, whether or not the address is registered —
    otherwise this endpoint becomes a way to discover who has an account.
    """
    ip = request.client.host if request.client else "unknown"
    if not _rate_ok(f"reset:{ip}", 5):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS,
                            "Too many reset requests. Please wait a minute.")
    site = os.environ.get("SITE_URL") or "https://www.sklzlabs.com"
    try:
        sb.auth.reset_password_for_email(
            payload.email,
            {"redirect_to": f"{site}/reset.html"})
    except Exception as exc:  # noqa: BLE001
        import sys as _s
        print(f"[reset] {type(exc).__name__}: {exc}", file=_s.stderr, flush=True)
    return {"ok": True,
            "message": ("If that address has an account, a reset link is on "
                        "its way. Check your inbox and spam folder.")}


class NewPassword(BaseModel):
    access_token: str
    password: str = Field(min_length=8)


@router.post("/set-password")
async def set_password(payload: NewPassword,
                       sb: Client = Depends(get_supabase)) -> dict:
    """Complete a reset using the token from the emailed link."""
    try:
        sb.auth.set_session(payload.access_token, payload.access_token)
        sb.auth.update_user({"password": payload.password})
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "That reset link has expired or already been used. "
            "Request a new one.") from exc
    return {"ok": True, "message": "Password updated. You can log in now."}


class ChangePassword(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8)


@router.post("/change-password")
async def change_password(payload: ChangePassword,
                          user=Depends(get_current_user),
                          sb: Client = Depends(get_supabase)) -> dict:
    """Change password while logged in. Verifies the current one first."""
    email = getattr(user, "email", "")
    try:
        sb.auth.sign_in_with_password({"email": email,
                                       "password": payload.current_password})
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "Current password is not correct.") from exc
    try:
        sb.auth.update_user({"password": payload.new_password})
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR,
                            f"could not update password: {str(exc)[:160]}") from exc
    return {"ok": True, "message": "Password changed."}
