"""P0 — provider identity rules."""
import os, sys, types
sys.path.insert(0, ".")
import provider_rules as rules

OWNER = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"
TENANT = "33333333-3333-4333-8333-333333333333"
TENANT_B = "44444444-4444-4444-8444-444444444444"


def user(uid=OWNER, email="t@example.com", role="user"):
    return types.SimpleNamespace(id=uid, email=email, role=role)


def provider(owner=OWNER, tenant=None, status="active"):
    return {"id": "99999999-9999-4999-8999-999999999999",
            "owner_user_id": owner, "iskra_tenant_id": tenant,
            "display_name": "Falcon FX", "slug": "falcon-fx",
            "status": status}


def setup_function():
    os.environ["OWNER_EMAIL"] = "boss@sklzlabs.com"


# ── identity shape ───────────────────────────────────────────────────
def test_tenant_link_must_be_a_uuid_never_an_email():
    for bad in ("trader@example.com", "falcon-fx", "", None, "12345",
                "{33333333-3333-4333-8333-333333333333}",
                "333333333333433383333333333333333"):
        assert rules.is_valid_uuid(bad) is False, repr(bad)
    assert rules.is_valid_uuid(TENANT) is True
    assert rules.is_valid_uuid(TENANT.upper()) is True


def test_slug_shape():
    for good in ("falcon-fx", "abc", "a1-b2-c3"):
        assert rules.is_valid_slug(good), good
    for bad in ("-lead", "trail-", "ab", "Has Space", "x" * 41):
        assert not rules.is_valid_slug(bad), bad
    # uppercase is ACCEPTED and normalised on write, matching the unique
    # index which is on lower(slug)
    assert rules.is_valid_slug("Falcon-FX") is True


# ── who may do what ──────────────────────────────────────────────────
def test_owner_can_view_and_edit_presentation_only():
    p, u = provider(), user()
    assert rules.can_view(u, p) and rules.can_edit(u, p)
    assert rules.editable_fields(u, p) == {"display_name"}


def test_an_owner_cannot_change_their_own_status():
    """Status is SKLZ's statement about the business, not the reverse."""
    assert "status" not in rules.editable_fields(user(), provider())


def test_an_unrelated_user_can_neither_view_nor_mutate():
    stranger = user(uid=OTHER, email="nobody@example.com")
    p = provider()
    assert rules.can_view(stranger, p) is False
    assert rules.can_edit(stranger, p) is False
    assert rules.editable_fields(stranger, p) == set()
    assert rules.can_link_tenant(stranger, p) is False


def test_platform_admin_by_role_not_only_by_email():
    """profiles.role already exists; the env allowlist is a bootstrap."""
    by_role = user(uid=OTHER, email="nobody@example.com", role="admin")
    assert rules.is_platform_admin(by_role) is True
    assert rules.editable_fields(by_role, provider()) == {
        "display_name", "slug", "status"}


def test_bootstrap_email_allowlist_still_works():
    boss = user(uid=OTHER, email="boss@sklzlabs.com", role="user")
    assert rules.is_platform_admin(boss) is True


def test_an_empty_allowlist_does_not_promote_everyone():
    os.environ["OWNER_EMAIL"] = ""
    assert rules.is_platform_admin(user(email="")) is False
    assert rules.is_platform_admin(user(email="anyone@example.com")) is False


# ── the ISKRA link ───────────────────────────────────────────────────
def test_only_the_platform_may_link_a_tenant():
    assert rules.can_link_tenant(user(), provider()) is False      # owner
    assert rules.can_link_tenant(user(role="admin"), provider()) is True


def test_a_malformed_tenant_id_is_rejected():
    for bad in ("trader@example.com", "not-a-uuid", ""):
        assert rules.link_rejection(provider(), bad) != ""


def test_relinking_to_a_different_tenant_is_refused():
    """Re-pointing a live provider would move a business's customers."""
    p = provider(tenant=TENANT)
    assert rules.link_rejection(p, TENANT_B) != ""
    assert "already linked" in rules.link_rejection(p, TENANT_B)
    # the same tenant again is not an error
    assert rules.link_rejection(p, TENANT) == ""


def test_a_closed_provider_cannot_be_linked():
    assert rules.link_rejection(provider(status="closed"), TENANT) != ""


def test_two_providers_cannot_share_one_tenant():
    """Enforced by a partial unique index; asserted here as intent."""
    sql = open("migrations/P0-migration.sql").read()
    assert "providers_iskra_tenant_uniq" in sql
    assert "unique index" in sql.lower()
    assert "where iskra_tenant_id is not null" in sql.lower()


# ── safety posture ───────────────────────────────────────────────────
def test_providers_table_is_not_reachable_by_a_public_key():
    sql = open("migrations/P0-migration.sql").read().lower()
    assert "enable row level security" in sql
    assert "revoke all on public.providers from anon, authenticated" in sql
    assert "create policy" not in sql       # no policy = no public access


def test_nothing_existing_is_rekeyed_in_this_migration():
    sql = open("migrations/P0-migration.sql").read().lower()
    for table in ("subscriptions", "copy_slaves", "journal_trades",
                  "signals", "bot_orders", "tg_leads"):
        assert f"alter table public.{table}" not in sql, table


def test_status_is_independent_of_subscription_state():
    sql = open("migrations/P0-migration.sql").read()
    assert "'draft', 'active', 'suspended', 'closed'" in sql
    assert "subscription" not in sql.split("check (status")[1][:200]
