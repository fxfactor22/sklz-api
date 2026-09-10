"""Provider records — the root of the Pro Trader OS business identity.

SKLZ owns provider identity. `iskra_tenant_id` links one provider to one
ISKRA tenant and is nullable, because a provider exists here before it
exists there and may never need a tenant at all.

Nothing in this module rekeys existing data. Signals, journals, copy
accounts, subscriptions and alerts remain keyed by `user_id`; a provider
sits alongside them and will be adopted deliberately, later, per surface.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from supabase import Client

import provider_rules as rules
from auth import get_current_user
from db import get_supabase

router = APIRouter(prefix="/api/providers", tags=["providers"])


class ProviderIn(BaseModel):
    owner_user_id: str = ""
    display_name: str
    slug: str


class ProviderPatch(BaseModel):
    display_name: str | None = None
    slug: str | None = None
    status: str | None = None


class TenantLinkIn(BaseModel):
    iskra_tenant_id: str


def _get(sb: Client, provider_id: str) -> dict:
    if not rules.is_valid_uuid(provider_id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "provider id must be a UUID")
    try:
        rows = (sb.table("providers").select("*")
                .eq("id", provider_id).limit(1).execute()).data or []
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "provider store unavailable") from exc
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown provider")
    return rows[0]


def _public(row: dict) -> dict:
    return {"id": row["id"], "display_name": row["display_name"],
            "slug": row["slug"], "status": row["status"],
            "owner_user_id": row["owner_user_id"],
            "iskra_tenant_id": row.get("iskra_tenant_id"),
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at")}


@router.post("")
async def create_provider(body: ProviderIn,
                          user=Depends(get_current_user),
                          sb: Client = Depends(get_supabase)) -> dict:
    """Create a provider. Platform action in this phase.

    Self-serve onboarding is deliberately not built yet: a provider is a
    business relationship, and the flow that creates one belongs with the
    ISKRA membership contract rather than ahead of it.
    """
    if not rules.is_platform_admin(user):
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "creating a provider is a platform action")
    owner = (body.owner_user_id or str(getattr(user, "id", ""))).strip()
    if not rules.is_valid_uuid(owner):
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "owner_user_id must be a UUID")
    slug = body.slug.strip().lower()
    if not rules.is_valid_slug(slug):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "slug must be 3-40 characters, lowercase letters, digits and "
            "hyphens, not starting or ending with a hyphen")
    if not body.display_name.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "display_name is required")
    try:
        rows = (sb.table("providers").insert({
            "owner_user_id": owner,
            "display_name": body.display_name.strip()[:120],
            "slug": slug, "status": "draft"}).execute()).data or []
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        if "providers_slug_uniq" in msg or "duplicate key" in msg:
            raise HTTPException(status.HTTP_409_CONFLICT,
                                "that slug is taken") from exc
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "could not create provider") from exc
    return {"ok": True, "provider": _public(rows[0])}


@router.get("/mine")
async def my_providers(user=Depends(get_current_user),
                       sb: Client = Depends(get_supabase)) -> dict:
    try:
        rows = (sb.table("providers").select("*")
                .eq("owner_user_id", str(user.id))
                .order("created_at").execute()).data or []
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "provider store unavailable") from exc
    return {"ok": True, "providers": [_public(r) for r in rows]}


@router.get("/{provider_id}")
async def get_provider(provider_id: str,
                       user=Depends(get_current_user),
                       sb: Client = Depends(get_supabase)) -> dict:
    row = _get(sb, provider_id)
    if not rules.can_view(user, row):
        # 404 rather than 403: an unrelated caller should not learn that a
        # provider with this id exists.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown provider")
    return {"ok": True, "provider": _public(row)}


@router.patch("/{provider_id}")
async def update_provider(provider_id: str, body: ProviderPatch,
                          user=Depends(get_current_user),
                          sb: Client = Depends(get_supabase)) -> dict:
    row = _get(sb, provider_id)
    if not rules.can_view(user, row):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown provider")
    allowed = rules.editable_fields(user, row)
    if not allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "not permitted")

    patch: dict = {}
    if body.display_name is not None and "display_name" in allowed:
        if not body.display_name.strip():
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                "display_name cannot be empty")
        patch["display_name"] = body.display_name.strip()[:120]
    if body.slug is not None:
        if "slug" not in allowed:
            raise HTTPException(status.HTTP_403_FORBIDDEN,
                                "changing the slug is a platform action")
        if not rules.is_valid_slug(body.slug):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid slug")
        patch["slug"] = body.slug.strip().lower()
    if body.status is not None:
        if "status" not in allowed:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "provider status is set by SKLZ, not by the provider")
        if body.status not in rules.STATUSES:
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                f"status must be one of {rules.STATUSES}")
        patch["status"] = body.status

    if not patch:
        return {"ok": True, "provider": _public(row), "changed": False}
    try:
        rows = (sb.table("providers").update(patch)
                .eq("id", provider_id).execute()).data or []
    except Exception as exc:  # noqa: BLE001
        if "providers_slug_uniq" in str(exc):
            raise HTTPException(status.HTTP_409_CONFLICT,
                                "that slug is taken") from exc
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "could not update provider") from exc
    return {"ok": True, "provider": _public(rows[0] if rows else row),
            "changed": True}


@router.post("/{provider_id}/link-iskra")
async def link_iskra_tenant(provider_id: str, body: TenantLinkIn,
                            user=Depends(get_current_user),
                            sb: Client = Depends(get_supabase)) -> dict:
    """Link this provider to exactly one ISKRA tenant.

    SKLZ makes the link because SKLZ owns the mapping. The database
    enforces one-tenant-one-provider with a partial unique index, so a
    race between two admins ends in a rejection rather than a silently
    shared tenant.
    """
    row = _get(sb, provider_id)
    if not rules.can_link_tenant(user, row):
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "linking an ISKRA tenant is a platform action")
    tenant = (body.iskra_tenant_id or "").strip().lower()
    why = rules.link_rejection(row, tenant)
    if why:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, why)
    if str(row.get("iskra_tenant_id") or "").lower() == tenant:
        return {"ok": True, "provider": _public(row), "already_linked": True}
    try:
        rows = (sb.table("providers").update({"iskra_tenant_id": tenant})
                .eq("id", provider_id).execute()).data or []
    except Exception as exc:  # noqa: BLE001
        if "providers_iskra_tenant_uniq" in str(exc) or "duplicate" in str(exc):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "that ISKRA tenant is already linked to another provider"
            ) from exc
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "could not link tenant") from exc
    return {"ok": True, "provider": _public(rows[0] if rows else row),
            "already_linked": False}
