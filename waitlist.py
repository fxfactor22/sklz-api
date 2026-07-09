"""Waitlist endpoint.

POST /api/waitlist  { email, source?, interest? }
- validates the email
- stores it in the `waitlist` table (dedupe on email)
- never reveals whether an email already existed (privacy + anti-enumeration):
  always returns the same success shape.

Simple in-memory + DB dedupe. Rate limiting is basic (per-IP, in-memory) —
fine for launch volume; moves to Redis when traffic grows.
"""
from __future__ import annotations

import time
from collections import defaultdict

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, EmailStr, Field
from supabase import Client

from db import get_supabase

router = APIRouter(prefix="/api", tags=["waitlist"])

# --- tiny in-memory rate limiter (per IP) --------------------------------
_HITS: dict[str, list[float]] = defaultdict(list)
_WINDOW = 60.0      # seconds
_MAX = 5           # requests per window per IP


def _rate_ok(ip: str) -> bool:
    now = time.time()
    hits = [t for t in _HITS[ip] if now - t < _WINDOW]
    hits.append(now)
    _HITS[ip] = hits
    return len(hits) <= _MAX


class WaitlistIn(BaseModel):
    email: EmailStr
    source: str = Field(default="website", max_length=60)
    interest: str = Field(default="copy_strategy", max_length=60)


class WaitlistOut(BaseModel):
    ok: bool
    message: str


@router.post("/waitlist", response_model=WaitlistOut)
async def join_waitlist(
    payload: WaitlistIn,
    request: Request,
    sb: Client = Depends(get_supabase),
) -> WaitlistOut:
    ip = request.client.host if request.client else "unknown"
    if not _rate_ok(ip):
        # Soft-fail: don't leak that they're rate limited as an error state.
        return WaitlistOut(ok=True, message="You're on the list.")

    email = payload.email.lower().strip()
    row = {
        "email": email,
        "source": payload.source,
        "interest": payload.interest,
        "ip": ip,
    }
    try:
        # upsert on email → idempotent, no duplicate rows, no error if exists
        sb.table("waitlist").upsert(row, on_conflict="email").execute()
    except Exception:
        # Never surface internal errors to the form; log server-side only.
        # (A structured logger goes here in the next iteration.)
        return WaitlistOut(ok=True, message="You're on the list.")

    return WaitlistOut(ok=True, message="You're on the list. We'll be in touch.")
