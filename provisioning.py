"""SKLZ-side provisioning actions.

ISKRA creates the business. SKLZ remembers exactly which business ISKRA
created. Retries may repeat the request; they may never create a second
company.

The order of operations is the whole design:

    persist intent (with its key)  →  call ISKRA  →  persist result

If the process dies in the middle, the intent row survives with its key,
and the retry sends that same key. ISKRA answers with the business it
already made.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status as http
from pydantic import BaseModel
from supabase import Client

import iskra_client as iskra
import provider_rules as rules
from auth import get_current_user
from db import get_supabase

router = APIRouter(prefix="/api/providers", tags=["provisioning"])

LIFECYCLE_STATES = ("active", "past_due", "suspended", "offboarded")
INVITE_ROLES = ("owner", "manager", "staff", "viewer")


# ── helpers ─────────────────────────────────────────────────────────
def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _provider(sb: Client, provider_id: str) -> dict:
    if not rules.is_valid_uuid(provider_id):
        raise HTTPException(http.HTTP_400_BAD_REQUEST,
                            "provider id must be a UUID")
    try:
        rows = (sb.table("providers").select("*")
                .eq("id", provider_id).limit(1).execute()).data or []
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(http.HTTP_503_SERVICE_UNAVAILABLE,
                            "provider store unavailable") from exc
    if not rows:
        raise HTTPException(http.HTTP_404_NOT_FOUND, "unknown provider")
    return rows[0]


def _audit(sb: Client, provider_id: str | None, event: str,
           actor=None, detail: dict | None = None) -> None:
    """Record what happened. Redacted, always — never a token."""
    try:
        sb.table("provider_audit").insert({
            "provider_id": provider_id, "event": event,
            "actor": str(getattr(actor, "id", "") or "") or None,
            "detail": iskra.redact(detail or {})}).execute()
    except Exception:  # noqa: BLE001
        pass          # an audit failure must not fail the operation


def _open_intent(sb: Client, provider_id: str, kind: str) -> dict | None:
    try:
        rows = (sb.table("provisioning_intents").select("*")
                .eq("provider_id", provider_id).eq("kind", kind)
                .in_("state", ["in_flight", "succeeded"])
                .order("created_at", desc=True).limit(1).execute()).data or []
    except Exception:
        return None
    return rows[0] if rows else None


def _new_key(kind: str) -> str:
    return f"sklz-{kind}-{secrets.token_hex(12)}"


def _settle(sb: Client, intent_id: str, state: str,
            result: dict | None = None, code: str = "",
            detail: str = "") -> None:
    try:
        sb.table("provisioning_intents").update({
            "state": state,
            "result": iskra.redact(result) if result else None,
            "error_code": code or None, "error_detail": detail[:400] or None,
            "settled_at": _now()}).eq("id", intent_id).execute()
    except Exception:  # noqa: BLE001
        pass


def _iskra_http(exc: iskra.IskraError) -> HTTPException:
    """Surface ISKRA's meaning rather than flattening it."""
    return HTTPException(
        http.HTTP_502_BAD_GATEWAY if exc.status in (0, 500, 502)
        else exc.status or http.HTTP_502_BAD_GATEWAY,
        {"error": exc.code, "detail": exc.detail,
         "retryable": exc.retryable, "retry_with_same_key": exc.same_key,
         "iskra_status": exc.status})


# ── request models ──────────────────────────────────────────────────
class ProvisionIn(BaseModel):
    business_name: str | None = None
    owner_email: str | None = None
    plan: str | None = None
    slug: str | None = None
    locale: str | None = None
    timezone: str | None = None
    currency: str | None = None
    country: str | None = None
    business_type: str | None = None


class InviteIn(BaseModel):
    email: str
    role: str = "owner"
    rotate: bool = False


class LifecycleIn(BaseModel):
    state: str
    reason: str | None = None


# ── diagnostics ─────────────────────────────────────────────────────
@router.get("/_iskra-diag")
async def iskra_diag(user=Depends(get_current_user)) -> dict:
    """Compare fingerprints before debugging anything else.

    A mismatch is a CONFIGURATION failure and says so — not a generic
    outage. The last time these systems disagreed on a secret, this
    comparison is what found it.
    """
    if not rules.is_platform_admin(user):
        raise HTTPException(http.HTTP_403_FORBIDDEN, "platform admin only")
    mine = iskra.secret_fingerprint()
    out = {"sklz": {"configured": bool(mine), "fingerprint": mine},
           "base_url": iskra.base_url()}
    try:
        theirs = iskra.diag()
    except iskra.IskraError as exc:
        out["iskra"] = {"reachable": False, "error": exc.code,
                        "detail": exc.detail}
        out["verdict"] = "iskra_unreachable"
        return out
    remote = ((theirs.get("secret") or {}).get("fingerprint") or "")
    out["iskra"] = {"reachable": True,
                    "configured": (theirs.get("secret") or {}).get(
                        "configured", False),
                    "fingerprint": remote,
                    "server_time": theirs.get("server_time")}
    if not mine:
        out["verdict"] = "sklz_secret_missing"
    elif not remote:
        out["verdict"] = "iskra_secret_missing"
    elif mine == remote:
        out["verdict"] = "match"
    else:
        out["verdict"] = "fingerprint_mismatch"
    return out


# ── provision ───────────────────────────────────────────────────────
@router.post("/{provider_id}/provision")
async def provision(provider_id: str, body: ProvisionIn,
                    user=Depends(get_current_user),
                    sb: Client = Depends(get_supabase)) -> dict:
    """Create (or recover) this provider's ISKRA business.

    Returns `invite` exactly once, when ISKRA mints one. It is never
    stored and never audited — a replay will not return it, by design.
    """
    prov = _provider(sb, provider_id)
    if not rules.is_platform_admin(user):
        raise HTTPException(http.HTTP_403_FORBIDDEN,
                            "provisioning is a platform action")
    if prov.get("status") == "closed":
        raise HTTPException(http.HTTP_400_BAD_REQUEST,
                            "a closed provider cannot be provisioned")

    payload = {
        "provider_id": str(prov["id"]),
        "provider_label": prov["display_name"],
        "business_name": (body.business_name or prov["display_name"]).strip(),
        "slug": (body.slug or prov["slug"]).strip().lower(),
    }
    for field in ("owner_email", "plan", "locale", "timezone", "currency",
                  "country", "business_type"):
        value = getattr(body, field, None)
        if value:
            payload[field] = str(value).strip()

    want_hash = iskra.canonical_request_hash(payload)
    intent = _open_intent(sb, str(prov["id"]), "tenant")

    if intent:
        # A previous attempt exists. Its key is the one that makes a
        # retry safe, so it is reused rather than replaced.
        if intent["request_hash"] != want_hash:
            raise HTTPException(
                http.HTTP_409_CONFLICT,
                {"error": "intent_body_changed",
                 "detail": "this provider already has a provisioning "
                           "intent with different details. Reusing its key "
                           "with a changed body is a conflict; resolve it "
                           "deliberately rather than minting a new key.",
                 "idempotency_key": intent["idempotency_key"]})
        key, intent_id = intent["idempotency_key"], intent["id"]
    else:
        key, intent_id = _new_key("tenant"), None
        try:
            rows = (sb.table("provisioning_intents").insert({
                "provider_id": str(prov["id"]), "kind": "tenant",
                "idempotency_key": key, "request_hash": want_hash,
            }).execute()).data or []
            intent_id = rows[0]["id"] if rows else None
        except Exception as exc:  # noqa: BLE001
            # The partial unique index refused a second live intent: a
            # concurrent request owns this. Do not start another.
            raise HTTPException(
                http.HTTP_409_CONFLICT,
                {"error": "provisioning_in_progress",
                 "detail": "another provisioning attempt for this provider "
                           "is already running",
                 "retryable": True}) from exc

    payload["idempotency_key"] = key
    _audit(sb, str(prov["id"]), "provisioning_requested", user,
           {"idempotency_key": key, "slug": payload["slug"],
            "has_owner_email": bool(payload.get("owner_email"))})

    try:
        result = iskra.create_tenant(payload)
    except iskra.IskraError as exc:
        if not (exc.status == 409 and exc.retryable):
            _settle(sb, intent_id, "failed", None, exc.code, exc.detail)
        _audit(sb, str(prov["id"]), "provisioning_failed", user,
               {"error": exc.code, "iskra_status": exc.status,
                "retry_with_same_key": exc.same_key})
        raise _iskra_http(exc) from exc

    tenant = (result.get("tenant") or {})
    tenant_id = str(tenant.get("id") or "")
    if not rules.is_valid_uuid(tenant_id):
        _settle(sb, intent_id, "failed", None, "bad_tenant_id",
                "ISKRA did not return a UUID tenant id")
        raise HTTPException(http.HTTP_502_BAD_GATEWAY,
                            {"error": "bad_tenant_id",
                             "detail": "ISKRA returned no usable tenant id"})

    existing = str(prov.get("iskra_tenant_id") or "")
    if existing and existing.lower() != tenant_id.lower():
        # FAIL CLOSED. We hold tenant A, ISKRA answered tenant B. One of
        # them is somebody else's company; guessing which would be worse
        # than stopping.
        _settle(sb, intent_id, "failed", iskra.redact(result),
                "mapping_conflict",
                f"local {existing} vs iskra {tenant_id}")
        _audit(sb, str(prov["id"]), "mapping_conflict", user,
               {"local_tenant_id": existing, "iskra_tenant_id": tenant_id})
        raise HTTPException(
            http.HTTP_409_CONFLICT,
            {"error": "mapping_conflict",
             "detail": "this provider is already linked to a different "
                       "ISKRA tenant. Not overwriting. Reconciliation "
                       "required.",
             "local_tenant_id": existing, "iskra_tenant_id": tenant_id})

    if not existing:
        try:
            sb.table("providers").update({"iskra_tenant_id": tenant_id}) \
                .eq("id", str(prov["id"])).execute()
        except Exception as exc:  # noqa: BLE001
            # The business exists but the link did not persist. The intent
            # row keeps the key, so a retry recovers rather than repeats.
            _settle(sb, intent_id, "in_flight", None, "link_write_failed",
                    str(exc)[:300])
            raise HTTPException(
                http.HTTP_503_SERVICE_UNAVAILABLE,
                {"error": "link_write_failed",
                 "detail": "ISKRA created the business but SKLZ could not "
                           "store the link. Retry this call — the same "
                           "idempotency key will return the same tenant.",
                 "retryable": True}) from exc
        _audit(sb, str(prov["id"]), "tenant_link_stored", user,
               {"iskra_tenant_id": tenant_id})

    _settle(sb, intent_id, "succeeded", iskra.redact(result))
    replayed = bool(result.get("replayed"))
    _audit(sb, str(prov["id"]),
           "provisioning_replay_recovered" if replayed
           else "provisioning_succeeded", user,
           {"iskra_tenant_id": tenant_id,
            "created": bool(result.get("created"))})

    # `invite` is returned to THIS caller and nowhere else: not stored,
    # not audited, not logged. ISKRA cannot reissue it.
    out = {"ok": True, "created": bool(result.get("created")),
           "replayed": replayed, "tenant": tenant,
           "iskra_tenant_id": tenant_id}
    if result.get("invite"):
        out["invite"] = result["invite"]
        out["invite_note"] = ("This link is shown once and is not stored. "
                              "Deliver it now; a replay will not return it.")
    return out


# ── owner invite ────────────────────────────────────────────────────
@router.post("/{provider_id}/owner-invite")
async def owner_invite(provider_id: str, body: InviteIn,
                       user=Depends(get_current_user),
                       sb: Client = Depends(get_supabase)) -> dict:
    """Mint or rotate an invitation.

    Rotation is a NEW intent with a NEW key on purpose: re-inviting the
    same address invalidates the previous link, which is a fresh action
    and not a retry of an old one.
    """
    prov = _provider(sb, provider_id)
    if not rules.is_platform_admin(user):
        raise HTTPException(http.HTTP_403_FORBIDDEN,
                            "inviting an owner is a platform action")
    if not prov.get("iskra_tenant_id"):
        raise HTTPException(http.HTTP_404_NOT_FOUND,
                            {"error": "not_provisioned",
                             "detail": "provision this provider first"})
    role = (body.role or "owner").lower()
    if role not in INVITE_ROLES:
        raise HTTPException(http.HTTP_400_BAD_REQUEST,
                            f"role must be one of {INVITE_ROLES}")
    email = (body.email or "").strip()
    if "@" not in email:
        raise HTTPException(http.HTTP_400_BAD_REQUEST,
                            "a delivery address is required")

    payload = {"provider_id": str(prov["id"]), "email": email, "role": role}
    key = _new_key("invite")
    payload["idempotency_key"] = key
    intent_id = None
    try:
        rows = (sb.table("provisioning_intents").insert({
            "provider_id": str(prov["id"]), "kind": "owner_invite",
            "idempotency_key": key,
            "request_hash": iskra.canonical_request_hash(payload),
        }).execute()).data or []
        intent_id = rows[0]["id"] if rows else None
    except Exception:  # noqa: BLE001
        pass
    # The email is an address, not a key, and it is not audited.
    _audit(sb, str(prov["id"]), "owner_invite_requested", user,
           {"role": role, "rotate": bool(body.rotate)})

    try:
        result = iskra.owner_invite(payload)
    except iskra.IskraError as exc:
        _settle(sb, intent_id, "failed", None, exc.code, exc.detail)
        raise _iskra_http(exc) from exc

    _settle(sb, intent_id, "succeeded", iskra.redact(result))
    out = {"ok": True, "tenant": result.get("tenant") or {}}
    if result.get("invite"):
        out["invite"] = result["invite"]
        out["invite_note"] = ("Shown once, not stored. Any previous link "
                              "for this address has stopped working.")
    return out


# ── lifecycle ───────────────────────────────────────────────────────
@router.post("/{provider_id}/iskra-lifecycle")
async def set_lifecycle(provider_id: str, body: LifecycleIn,
                        user=Depends(get_current_user),
                        sb: Client = Depends(get_supabase)) -> dict:
    """Ask ISKRA to change a tenant's lifecycle state.

    SKLZ requests; ISKRA decides. This does NOT change the SKLZ provider
    status — the two lifecycles are independent, and linking them by
    reflex is how an invoice stops somebody's trading.
    """
    prov = _provider(sb, provider_id)
    if not rules.is_platform_admin(user):
        raise HTTPException(http.HTTP_403_FORBIDDEN,
                            "lifecycle changes are a platform action")
    state = (body.state or "").lower().strip()
    if state not in LIFECYCLE_STATES:
        raise HTTPException(http.HTTP_400_BAD_REQUEST,
                            f"state must be one of {LIFECYCLE_STATES}")
    if not prov.get("iskra_tenant_id"):
        raise HTTPException(http.HTTP_404_NOT_FOUND,
                            {"error": "not_provisioned",
                             "detail": "provision this provider first"})

    payload = {"provider_id": str(prov["id"]), "state": state}
    if body.reason:
        payload["reason"] = str(body.reason)[:200]
    key = _new_key("lifecycle")
    payload["idempotency_key"] = key
    intent_id = None
    try:
        rows = (sb.table("provisioning_intents").insert({
            "provider_id": str(prov["id"]), "kind": "lifecycle",
            "idempotency_key": key,
            "request_hash": iskra.canonical_request_hash(payload),
        }).execute()).data or []
        intent_id = rows[0]["id"] if rows else None
    except Exception:  # noqa: BLE001
        pass
    _audit(sb, str(prov["id"]), "lifecycle_request", user,
           {"state": state, "reason": payload.get("reason")})

    try:
        result = iskra.lifecycle(payload)
    except iskra.IskraError as exc:
        _settle(sb, intent_id, "failed", None, exc.code, exc.detail)
        _audit(sb, str(prov["id"]), "lifecycle_result", user,
               {"ok": False, "error": exc.code})
        raise _iskra_http(exc) from exc

    _settle(sb, intent_id, "succeeded", iskra.redact(result))
    _audit(sb, str(prov["id"]), "lifecycle_result", user,
           {"ok": True, "state": state, "was": result.get("was")})
    return {"ok": True, "tenant": result.get("tenant") or {},
            "was": result.get("was"),
            "note": ("SKLZ provider status is unchanged. Provider and "
                     "tenant lifecycles are independent.")}


# ── status ──────────────────────────────────────────────────────────
@router.get("/{provider_id}/iskra-status")
async def iskra_status(provider_id: str,
                       user=Depends(get_current_user),
                       sb: Client = Depends(get_supabase)) -> dict:
    """What ISKRA thinks, and whether it agrees with us.

    Reconciliation information. Never the source of truth for SKLZ
    provider status.
    """
    prov = _provider(sb, provider_id)
    if not rules.can_view(user, prov):
        raise HTTPException(http.HTTP_404_NOT_FOUND, "unknown provider")

    local = str(prov.get("iskra_tenant_id") or "")
    if not local:
        return {"ok": True, "state": "not_provisioned",
                "provider_status": prov["status"], "tenant": None}

    try:
        result = iskra.status(str(prov["id"]))
    except iskra.IskraError as exc:
        if exc.status == 404:
            return {"ok": True, "state": "mapping_conflict",
                    "provider_status": prov["status"],
                    "local_tenant_id": local,
                    "detail": "SKLZ holds a tenant link that ISKRA does "
                              "not recognise"}
        return {"ok": False, "state": "integration_unavailable",
                "provider_status": prov["status"],
                "error": exc.code, "detail": exc.detail}

    tenant = result.get("tenant") or {}
    remote = str(tenant.get("id") or "")
    if remote and remote.lower() != local.lower():
        _audit(sb, str(prov["id"]), "mapping_conflict", user,
               {"local_tenant_id": local, "iskra_tenant_id": remote})
        return {"ok": False, "state": "mapping_conflict",
                "provider_status": prov["status"],
                "local_tenant_id": local, "iskra_tenant_id": remote,
                "detail": "SKLZ and ISKRA disagree about which business "
                          "this provider owns. Not resolving automatically."}

    life = str(tenant.get("lifecycle_state") or "").lower()
    state = {"active": "linked_active", "past_due": "linked_past_due",
             "suspended": "linked_suspended",
             "offboarded": "linked_offboarded"}.get(life, "linked_unknown")
    return {"ok": True, "state": state, "provider_status": prov["status"],
            "tenant": tenant, "link": result.get("link") or {}}
