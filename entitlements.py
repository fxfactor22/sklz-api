"""SKLZ entitlements — one place that answers "what does this plan get".

Tiers (new signups):
  SKLZ Core  $29  everything except crypto; MT5 copy on the affiliate
                  broker only, single account
  SKLZ Plus  $49  + crypto trading/copy; MT5 copy affiliate broker, single
  SKLZ Pro   $79  multi-account MT5 copy, any broker, everything on

Legacy plans keep their promises — Founder $39 is locked for life and that
stays true forever. Legacy mapping below is deliberately generous (bundle
holders get Plus-level access including single-account MT5 copy) because
downgrading a paying customer's expectations by surprise is how trust dies.

Enforcement philosophy: entitlement checks answer 402 with a sentence that
names the plan that unlocks the feature — never a bare "forbidden".
"""
from __future__ import annotations

import os
import time

from fastapi import Depends, HTTPException, status
from supabase import Client

from db import get_supabase
from auth import get_current_user

# plan -> (crypto, mt5_max_accounts, any_broker)
_MATRIX: dict[str, tuple[bool, int, bool]] = {
    # new tiers
    "copy_basic_monthly":  (False, 1, False),
    "copy_crypto_monthly": (True,  1, False),
    "copy_pro_monthly":    (True, 10, True),
    # legacy — promises kept, generously
    "bundle_monthly":  (True, 1, False),
    "bundle_annual":   (True, 1, False),
    "bundle_founder":  (True, 1, False),
    "suite_monthly":   (False, 0, False),
    "suite_annual":    (False, 0, False),
    "suite_lifetime":  (False, 0, False),
    "gpt_monthly":     (False, 0, False),
    "gpt_annual":      (False, 0, False),
}

_AFFILIATE_BROKERS = [
    s.strip().lower() for s in os.environ.get(
        "AFFILIATE_BROKERS",
        "IC Markets,ICMarkets,Raw Trading,International Capital").split(",")
    if s.strip()]

_cache: dict[str, tuple[float, dict]] = {}   # uid -> (ts, sub row)


def _sub(sb: Client, uid: str) -> dict:
    hit = _cache.get(uid)
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    try:
        r = (sb.table("subscriptions").select("plan,active")
             .eq("user_id", uid).execute())
        row = (r.data or [{}])[0]
    except Exception:
        row = {}
    _cache[uid] = (time.time(), row)
    return row


def entitlements_for(sb: Client, uid: str) -> dict:
    row = _sub(sb, uid)
    plan = (row.get("plan") or "") if row.get("active") else ""
    crypto, mt5_max, any_broker = _MATRIX.get(plan, (False, 0, False))
    return {"plan": plan, "active": bool(row.get("active")),
            "crypto": crypto, "mt5_max_accounts": mt5_max,
            "any_broker": any_broker}


def broker_allowed(name: str) -> bool:
    n = (name or "").lower()
    return any(a in n for a in _AFFILIATE_BROKERS)


# ---- FastAPI dependencies ----
async def require_active(user=Depends(get_current_user),
                         sb: Client = Depends(get_supabase)):
    e = entitlements_for(sb, user.id)
    if not e["active"]:
        raise HTTPException(
            status.HTTP_402_PAYMENT_REQUIRED,
            "SKLZ is a paid platform — plans start at $29/mo on the "
            "pricing page.")
    return user


async def require_crypto(user=Depends(get_current_user),
                         sb: Client = Depends(get_supabase)):
    e = entitlements_for(sb, user.id)
    if not e["crypto"]:
        raise HTTPException(
            status.HTTP_402_PAYMENT_REQUIRED,
            "Crypto trading needs SKLZ Plus ($49/mo) or Pro ($79/mo).")
    return user


# ---- legacy interface (the module this one replaced) ----
# journal.py gates its routes with require_paid; identical contract.
require_paid = require_active
