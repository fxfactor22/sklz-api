"""Crypto order capture for the provider demo.

A prospect who has sent USDT or SOL tells us here. Verification is manual
and deliberate: no blockchain watcher, no processor, no automatic
activation. The status ladder exists so a human can move an order along
and nobody has to remember where it got to.
"""
from __future__ import annotations

import re
import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status as http
from pydantic import BaseModel
from supabase import Client

import provider_rules as rules
from aio import offload
from auth import get_current_user
from db import get_supabase

router = APIRouter(prefix="/api/orders", tags=["orders"])

PACKAGES = {"signal_desk", "pro_trader_os"}
ASSETS = {"USDT", "SOL"}
NETWORKS = {"TRC20", "Solana"}
STATUSES = ("awaiting_payment", "submitted", "verifying", "confirmed",
            "activation_pending", "activated", "rejected")

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_recent: dict[str, list[float]] = {}


class CryptoOrderIn(BaseModel):
    name: str
    email: str
    telegram: str = ""
    package: str
    asset: str
    network: str
    amount: str
    wallet: str = ""
    tx_hash: str


def _clean(v: str, n: int) -> str:
    return (v or "").strip()[:n]


def _rate_ok(ip: str) -> bool:
    import time
    now = time.time()
    hits = [t for t in _recent.get(ip, []) if now - t < 3600]
    if len(hits) >= 6:
        _recent[ip] = hits
        return False
    hits.append(now)
    _recent[ip] = hits
    return True


@router.post("/crypto")
async def submit_crypto_order(body: CryptoOrderIn, request: Request,
                              sb: Client = Depends(get_supabase)) -> dict:
    """Public. A prospect reporting a payment they have already made."""
    ip = (request.client.host if request.client else "") or "unknown"
    if not _rate_ok(ip):
        raise HTTPException(http.HTTP_429_TOO_MANY_REQUESTS,
                            "too many submissions — message us on Telegram")

    name = _clean(body.name, 120)
    email = _clean(body.email, 160).lower()
    tx = _clean(body.tx_hash, 200)
    if not name or not _EMAIL.match(email) or len(tx) < 12:
        raise HTTPException(http.HTTP_400_BAD_REQUEST,
                            "name, a valid email and the transaction hash "
                            "are all required")
    if body.package not in PACKAGES or body.asset not in ASSETS \
       or body.network not in NETWORKS:
        raise HTTPException(http.HTTP_400_BAD_REQUEST, "unknown selection")

    reference = "SKLZ-" + secrets.token_hex(3).upper()
    row = {"reference": reference, "name": name, "email": email,
           "telegram": _clean(body.telegram, 80),
           "package": body.package, "asset": body.asset,
           "network": body.network, "amount": _clean(body.amount, 40),
           "wallet": _clean(body.wallet, 120), "tx_hash": tx,
           "status": "submitted"}

    def _insert():
        return sb.table("crypto_orders").insert(row).execute()

    try:
        await offload(_insert)
    except Exception as exc:  # noqa: BLE001
        if "crypto_orders_tx_uniq" in str(exc) or "duplicate" in str(exc):
            # The same hash twice is one payment. Return the original
            # reference rather than creating a second order.
            def _find():
                return (sb.table("crypto_orders").select("reference")
                        .eq("tx_hash", tx).limit(1).execute()).data or []
            try:
                found = await offload(_find)
                if found:
                    return {"ok": True, "reference": found[0]["reference"],
                            "duplicate": True,
                            "note": "we already have this transaction"}
            except Exception:  # noqa: BLE001
                pass
        raise HTTPException(http.HTTP_503_SERVICE_UNAVAILABLE,
                            "could not record the order") from exc

    print(f"[order] crypto {reference} {body.package} {body.amount} "
          f"{body.asset}/{body.network} from {email}")
    return {"ok": True, "reference": reference, "status": "submitted",
            "note": "We'll verify the transaction and contact you on Telegram."}


@router.get("")
async def list_orders(status_filter: str = "",
                      user=Depends(get_current_user),
                      sb: Client = Depends(get_supabase)) -> dict:
    if not rules.is_platform_admin(user):
        raise HTTPException(http.HTTP_403_FORBIDDEN, "platform admin only")

    def _q():
        q = sb.table("crypto_orders").select("*")
        if status_filter:
            q = q.eq("status", status_filter)
        return (q.order("created_at", desc=True).limit(100).execute()).data or []

    return {"ok": True, "orders": await offload(_q)}


class StatusIn(BaseModel):
    status: str
    notes: str = ""


@router.post("/{reference}/status")
async def set_status(reference: str, body: StatusIn,
                     user=Depends(get_current_user),
                     sb: Client = Depends(get_supabase)) -> dict:
    if not rules.is_platform_admin(user):
        raise HTTPException(http.HTTP_403_FORBIDDEN, "platform admin only")
    if body.status not in STATUSES:
        raise HTTPException(http.HTTP_400_BAD_REQUEST,
                            f"status must be one of {STATUSES}")

    def _upd():
        return (sb.table("crypto_orders")
                .update({"status": body.status,
                         "notes": _clean(body.notes, 500) or None,
                         "updated_at": datetime.now(timezone.utc).isoformat()})
                .eq("reference", reference).execute()).data or []

    rows = await offload(_upd)
    if not rows:
        raise HTTPException(http.HTTP_404_NOT_FOUND, "unknown reference")
    return {"ok": True, "reference": reference, "status": body.status}
