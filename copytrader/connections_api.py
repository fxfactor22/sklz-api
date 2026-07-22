"""SKLZ CopyTrader — exchange connection API.

This is where a user's API key first enters the system, so the safety rules
live here and fail closed:

  - the key is verified against the exchange BEFORE anything is stored
  - a key with withdrawal permission is rejected and never persisted
  - the key and secret are encrypted (AES-256-GCM) before they touch the DB
  - plaintext is never returned by any response and never logged
  - every action is written to an audit trail

Endpoints
  GET    /api/copy/exchanges          list supported exchanges
  POST   /api/copy/connect            verify + store a key
  GET    /api/copy/connections        my connections (masked)
  POST   /api/copy/connections/{id}/recheck   re-verify permissions
  DELETE /api/copy/connections/{id}   revoke and delete
  GET    /api/copy/connections/{id}/balances  spot balances
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from supabase import Client

from auth import get_current_user
from db import get_supabase
from copytrader import vault
from copytrader.exchanges import (ExchangeAdapter, SUPPORTED, list_supported)

router = APIRouter(prefix="/api/copy", tags=["copytrader"])


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _audit(sb: Client, uid: str, action: str, detail: dict,
           request: Request | None = None) -> None:
    """Audit trail. NEVER pass a secret in `detail`."""
    try:
        ip = ""
        if request and request.client:
            ip = request.client.host or ""
        sb.table("copy_audit").insert({
            "user_id": uid, "action": action,
            "detail": detail, "ip": ip}).execute()
    except Exception:  # noqa: BLE001
        pass


class ConnectIn(BaseModel):
    exchange_id: str
    api_key: str = Field(min_length=8)
    secret: str = Field(min_length=8)
    passphrase: str = ""
    label: str = ""
    confirm_no_withdrawal: bool = False
    """Required only when the exchange cannot report its own permissions."""


@router.get("/exchanges")
async def exchanges() -> dict:
    return {"exchanges": list_supported(),
            "note": ("Spot trading only. Create keys with read and spot-trade "
                     "permissions. Keys with withdrawal access are rejected.")}


@router.post("/connect")
async def connect(body: ConnectIn, request: Request,
                  user=Depends(get_current_user),
                  sb: Client = Depends(get_supabase)) -> dict:
    uid = str(user.id)

    if not vault.vault_ready():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "Credential vault is not configured on the server.")
    if body.exchange_id not in SUPPORTED:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"unsupported exchange: {body.exchange_id}")
    if SUPPORTED[body.exchange_id][1] and not body.passphrase:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"{SUPPORTED[body.exchange_id][0]} requires a passphrase")

    # 1) VERIFY FIRST — nothing is stored until the key passes
    try:
        adapter = ExchangeAdapter(body.exchange_id, body.api_key,
                                  body.secret, body.passphrase)
        chk = adapter.verify_permissions()
    except Exception as exc:  # noqa: BLE001
        _audit(sb, uid, "connect_failed",
               {"exchange": body.exchange_id, "error": str(exc)[:200]}, request)
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"could not connect: {str(exc)[:200]}") from exc

    if not chk.ok:
        _audit(sb, uid, "connect_rejected",
               {"exchange": body.exchange_id, "reason": chk.message,
                "can_withdraw": chk.can_withdraw}, request)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, chk.message)

    # 2) if the exchange could not confirm withdrawal is off, the user must
    if not chk.verified and not body.confirm_no_withdrawal:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {"needs_confirmation": True, "message": chk.message,
             "action": ("Re-send with confirm_no_withdrawal=true once you have "
                        "checked that withdrawals are disabled on this key.")})

    fp = vault.fingerprint(body.api_key)

    # 3) encrypt, bound to this user
    aad = f"copy:{uid}:{body.exchange_id}"
    row = {
        "user_id": uid,
        "exchange_id": body.exchange_id,
        "label": (body.label or SUPPORTED[body.exchange_id][0])[:60],
        "key_fingerprint": fp,
        "key_masked": vault.masked(body.api_key),
        "enc_api_key": vault.encrypt(body.api_key, aad=aad),
        "enc_secret": vault.encrypt(body.secret, aad=aad),
        "enc_passphrase": vault.encrypt(body.passphrase, aad=aad) if body.passphrase else None,
        "permissions": {"can_read": chk.can_read, "can_trade": chk.can_trade,
                        "can_withdraw": chk.can_withdraw,
                        "verified_by_exchange": chk.verified,
                        "message": chk.message},
        "withdrawal_verified_disabled": bool(chk.verified and chk.can_withdraw is False),
        "status": "active",
        "last_checked_at": _now(),
    }
    try:
        existing = (sb.table("copy_connections").select("id")
                    .eq("user_id", uid).eq("exchange_id", body.exchange_id)
                    .eq("key_fingerprint", fp).execute()).data
        if existing:
            sb.table("copy_connections").update(row) \
                .eq("id", existing[0]["id"]).execute()
            cid = existing[0]["id"]
        else:
            res = sb.table("copy_connections").insert(row).execute()
            cid = (res.data or [{}])[0].get("id", "")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR,
                            f"could not save connection: {str(exc)[:200]}") from exc

    _audit(sb, uid, "connect_success",
           {"exchange": body.exchange_id, "fingerprint": fp,
            "verified": chk.verified}, request)

    return {"ok": True, "connection_id": cid,
            "exchange": SUPPORTED[body.exchange_id][0],
            "key": vault.masked(body.api_key),
            "verified_by_exchange": chk.verified,
            "message": chk.message}


@router.get("/connections")
async def connections(user=Depends(get_current_user),
                      sb: Client = Depends(get_supabase)) -> dict:
    """Masked view. Encrypted blobs are never returned."""
    try:
        rows = (sb.table("copy_connections")
                .select("id,exchange_id,label,key_masked,permissions,"
                        "withdrawal_verified_disabled,status,last_checked_at,created_at")
                .eq("user_id", str(user.id))
                .order("created_at", desc=True).execute()).data or []
    except Exception:
        rows = []
    for r in rows:
        r["exchange_name"] = SUPPORTED.get(r["exchange_id"], (r["exchange_id"],))[0]
    return {"connections": rows}


def _load_adapter(sb: Client, uid: str, connection_id: str) -> ExchangeAdapter:
    """Decrypt just-in-time and build a client. The only place plaintext exists."""
    try:
        rows = (sb.table("copy_connections").select("*")
                .eq("id", connection_id).eq("user_id", uid).execute()).data
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR,
                            "could not load connection") from exc
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "connection not found")
    c = rows[0]
    aad = f"copy:{uid}:{c['exchange_id']}"
    try:
        api_key = vault.decrypt(c["enc_api_key"], aad=aad)
        secret = vault.decrypt(c["enc_secret"], aad=aad)
        passphrase = vault.decrypt(c["enc_passphrase"], aad=aad) if c.get("enc_passphrase") else ""
    except vault.VaultError as exc:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR,
                            "stored credential could not be decrypted") from exc
    return ExchangeAdapter(c["exchange_id"], api_key, secret, passphrase)


@router.post("/connections/{connection_id}/recheck")
async def recheck(connection_id: str, request: Request,
                  user=Depends(get_current_user),
                  sb: Client = Depends(get_supabase)) -> dict:
    """Re-verify permissions — keys change, and withdrawal can be enabled later."""
    uid = str(user.id)
    adapter = _load_adapter(sb, uid, connection_id)
    chk = adapter.verify_permissions()
    upd = {
        "permissions": {"can_read": chk.can_read, "can_trade": chk.can_trade,
                        "can_withdraw": chk.can_withdraw,
                        "verified_by_exchange": chk.verified,
                        "message": chk.message},
        "withdrawal_verified_disabled": bool(chk.verified and chk.can_withdraw is False),
        "status": "active" if chk.ok else "error",
        "last_checked_at": _now(),
    }
    sb.table("copy_connections").update(upd).eq("id", connection_id).execute()
    _audit(sb, uid, "recheck", {"connection_id": connection_id, "ok": chk.ok}, request)
    if not chk.ok:
        return {"ok": False, "status": "error", "message": chk.message,
                "action": "This connection has been disabled. Fix the key permissions."}
    return {"ok": True, "message": chk.message, "verified": chk.verified}


@router.delete("/connections/{connection_id}")
async def revoke(connection_id: str, request: Request,
                 user=Depends(get_current_user),
                 sb: Client = Depends(get_supabase)) -> dict:
    uid = str(user.id)
    try:
        sb.table("copy_connections").delete() \
            .eq("id", connection_id).eq("user_id", uid).execute()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR,
                            "could not revoke") from exc
    _audit(sb, uid, "revoke", {"connection_id": connection_id}, request)
    return {"ok": True,
            "note": ("Credentials deleted from SKLZ. Also delete the key on the "
                     "exchange itself for complete removal.")}


@router.get("/connections/{connection_id}/balances")
async def balances(connection_id: str, user=Depends(get_current_user),
                   sb: Client = Depends(get_supabase)) -> dict:
    adapter = _load_adapter(sb, str(user.id), connection_id)
    try:
        bals = adapter.balances()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_502_BAD_GATEWAY,
                            f"exchange error: {str(exc)[:200]}") from exc
    return {"balances": [{"asset": b.asset, "free": b.free,
                          "used": b.used, "total": b.total} for b in bals]}
