"""Provider authorization rules — pure, so they can be tested and read.

Kept out of the transport module for the same reason the demo simulator
is: a rule that decides who may change a trading business should be
readable and testable without standing up a database.

WHAT THIS PHASE DELIBERATELY DOES NOT DO
========================================
No team roles, no RBAC matrix, no invitations. There are exactly two
principals here — the provider's owner and a SKLZ platform admin — and
everything else waits for the ISKRA membership contract, so that we do
not end up with two unrelated systems expressing the same permission.
"""
from __future__ import annotations

import os
import re
import uuid

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE)

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$")

STATUSES = ("draft", "active", "suspended", "closed")


def is_valid_uuid(value: str | None) -> bool:
    """Strict. A tenant link is a UUID or it is nothing.

    `uuid.UUID()` accepts braces, urns and unhyphenated forms, which would
    let the same tenant be stored in several shapes and defeat the unique
    index. The regex is the constraint the database will enforce anyway.
    """
    if not value or not isinstance(value, str):
        return False
    if not _UUID_RE.match(value.strip()):
        return False
    try:
        uuid.UUID(value.strip())
    except (ValueError, AttributeError, TypeError):
        return False
    return True


def is_valid_slug(value: str | None) -> bool:
    return bool(value) and bool(SLUG_RE.match(str(value).strip().lower()))


def _bootstrap_admin_emails() -> set[str]:
    """The env allowlist, which is a BOOTSTRAP and not the model.

    Every admin check in SKLZ today is an email list in an environment
    variable. That cannot be the permanent answer — it is unauditable,
    it cannot be granted per provider, and it ties authority to an
    address someone may change. `profiles.role` already exists and is
    already carried on the user object; this list stays only so the first
    admin can exist before any role has been assigned.
    """
    raw = os.environ.get("OWNER_EMAIL", "")
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


def is_platform_admin(user) -> bool:
    role = (getattr(user, "role", "") or "").lower()
    if role in ("admin", "owner", "platform_admin"):
        return True
    email = (getattr(user, "email", "") or "").lower()
    return bool(email) and email in _bootstrap_admin_emails()


def is_owner(user, provider: dict) -> bool:
    uid = str(getattr(user, "id", "") or "")
    return bool(uid) and str(provider.get("owner_user_id") or "") == uid


def can_view(user, provider: dict) -> bool:
    return is_owner(user, provider) or is_platform_admin(user)


def can_edit(user, provider: dict) -> bool:
    """The owner may edit presentation. Only SKLZ may change lifecycle."""
    return is_owner(user, provider) or is_platform_admin(user)


def editable_fields(user, provider: dict) -> set[str]:
    if is_platform_admin(user):
        return {"display_name", "slug", "status"}
    if is_owner(user, provider):
        # An owner may not activate or un-suspend themselves. Status is a
        # statement by the platform about the business, not by the
        # business about itself.
        return {"display_name"}
    return set()


def can_link_tenant(user, provider: dict) -> bool:
    """Linking is a platform action.

    The link is the authoritative mapping between two systems; letting a
    provider assert its own tenant id would let one business claim
    another's CRM. SKLZ makes the link, having confirmed both sides.
    """
    return is_platform_admin(user)


def link_rejection(provider: dict, tenant_id: str) -> str:
    """Why this link must be refused, or "" if it may proceed."""
    if not is_valid_uuid(tenant_id):
        return "iskra_tenant_id must be a UUID"
    current = provider.get("iskra_tenant_id")
    if current and str(current).lower() != tenant_id.strip().lower():
        # Immutable once set: re-pointing a live provider at a different
        # tenant would silently move a business's customers, conversations
        # and funnel to somebody else's records.
        return ("this provider is already linked to a different ISKRA "
                "tenant; unlink is a deliberate platform action")
    if provider.get("status") == "closed":
        return "a closed provider cannot be linked"
    return ""
