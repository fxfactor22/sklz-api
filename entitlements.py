"""Shared subscription enforcement for SKLZ paid features.

Server-side gate — the API refuses to serve paid content to free users,
regardless of what the UI shows. Admin emails bypass (owner access).
"""
from __future__ import annotations

import os

from fastapi import HTTPException, status
from supabase import Client


def _admin_emails() -> set[str]:
    raw = os.environ.get("ADMIN_EMAILS", "fxfactor24@gmail.com")
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


def plan_of(sb: Client, uid: str, email: str = "") -> str:
    """Current plan name, or 'Free'. Admins always get Bundle."""
    if email and email.lower() in _admin_emails():
        return "Bundle"
    try:
        r = (sb.table("subscriptions").select("plan,active")
             .eq("user_id", uid).execute()).data or []
        for row in r:
            if row.get("active"):
                return row.get("plan", "Free")
    except Exception:
        pass
    return "Free"


def is_paid(sb: Client, user) -> bool:
    return plan_of(sb, str(user.id), getattr(user, "email", "")) != "Free"


def require_paid(sb: Client, user, feature: str = "This feature") -> str:
    """Raise 402 unless the user has an active subscription."""
    plan = plan_of(sb, str(user.id), getattr(user, "email", ""))
    if plan == "Free":
        raise HTTPException(
            status.HTTP_402_PAYMENT_REQUIRED,
            f"{feature} requires a subscription. Upgrade at /pricing.html to unlock.")
    return plan


def require_plan(sb: Client, user, allowed: set[str], feature: str = "This feature") -> str:
    """Raise 402 unless the user's plan is in `allowed`."""
    plan = plan_of(sb, str(user.id), getattr(user, "email", ""))
    base = plan.split(" ")[0]           # "Bundle (Founder)" -> "Bundle"
    if plan == "Free" or not (plan in allowed or base in allowed):
        raise HTTPException(
            status.HTTP_402_PAYMENT_REQUIRED,
            f"{feature} requires: {', '.join(sorted(allowed))}. Upgrade at /pricing.html.")
    return plan
