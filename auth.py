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
