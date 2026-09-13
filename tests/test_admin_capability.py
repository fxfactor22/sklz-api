"""One definition of platform admin, reported rather than re-derived.

The UI used to answer this question itself, from profiles.role. That is
not the field the server tests, and it knows nothing of ADMIN_EMAILS, so
an allowlisted admin was hidden from a menu whose API would have served
them. The answer now comes from the server, computed by the same call the
protected routes make.
"""
import os
import sys
import types

sys.path.insert(0, ".")
AUTH = open("./auth.py").read()
RULES = open("./provider_rules.py").read()


class _User:
    """The shape get_current_user returns: a Supabase auth user."""
    def __init__(self, email="", role=""):
        self.email, self.role, self.id = email, role, "uid-1"


def _rules():
    import importlib
    import provider_rules
    return importlib.reload(provider_rules)


# ── there is exactly one rule, and /me calls it ────────────────────────
def test_me_reports_the_same_call_the_admin_routes_make():
    assert "import provider_rules as rules" in AUTH
    assert "is_platform_admin=rules.is_platform_admin(user)" in AUTH


def test_the_field_is_additive_and_defaults_closed():
    block = AUTH[AUTH.index("class ProfileOut"):AUTH.index("# --- helpers")]
    assert "is_platform_admin: bool = False" in block
    # the four existing fields are untouched, so existing consumers hold
    for field in ("id: str", "email: str", "display_name: str | None",
                  "role: str"):
        assert field in block, field


def _site(*parts):
    """The static site lives in its own repo. These two assertions are the
    cross-repo half of "one definition", so they check it where they can
    find it and skip rather than fail when it is not checked out beside
    this one. The site suite asserts the same thing from its side."""
    import pytest
    for root in (os.environ.get("SKLZ_SITE_REPO", ""),
                 os.path.expanduser("~/projects/sklz/site-repo"),
                 os.path.expanduser("~/mnt/sklz/site-repo"),
                 "/home/claude/site2"):
        if root and os.path.isfile(os.path.join(root, *parts)):
            return os.path.join(root, *parts)
    pytest.skip("site repo not available beside the API repo")


def test_the_browser_is_never_asked_to_decide():
    js = open(_site("sklz-auth.js"), encoding="utf-8").read()
    fn = js[js.index("function isAdmin("):js.index("function logout(")]
    assert "profile.is_platform_admin === true" in fn
    # no second definition: no role list, no allowlist, no email test
    for leak in ("ADMIN_EMAILS", "OWNER_EMAIL", "@", "role", "owner",
                 "platform_admin\"", "ADMIN_ROLES"):
        assert leak not in fn, leak


def test_no_allowlist_value_reaches_the_frontend():
    for parts in (("sklz-auth.js",), ("dashboard.html",),
                  ("admin", "demo.html")):
        path = _site(*parts)
        src = open(path, encoding="utf-8").read()
        # the real default in provider_rules, and the variable names
        for secret in ("fxfactor24@gmail.com", "ADMIN_EMAILS", "OWNER_EMAIL"):
            hits = [ln for ln in src.split("\n")
                    if secret in ln and not ln.strip().startswith("*")]
            assert not hits, (path, secret, hits[:1])


# ── A/B/C/D: the four principals ───────────────────────────────────────
def test_A_role_platform_admin_without_an_allowlisted_email():
    os.environ["ADMIN_EMAILS"] = "nobody@example.com"
    os.environ["OWNER_EMAIL"] = ""
    r = _rules()
    assert r.is_platform_admin(_User("stranger@example.com",
                                     "platform_admin")) is True


def test_B_role_admin_and_owner_without_an_allowlisted_email():
    os.environ["ADMIN_EMAILS"] = "nobody@example.com"
    os.environ["OWNER_EMAIL"] = ""
    r = _rules()
    assert r.is_platform_admin(_User("stranger@example.com", "admin")) is True
    assert r.is_platform_admin(_User("stranger@example.com", "owner")) is True


def test_C_ordinary_role_but_the_email_is_allowlisted():
    """The case the old UI got wrong."""
    os.environ["ADMIN_EMAILS"] = "boss@sklzlabs.com"
    os.environ["OWNER_EMAIL"] = ""
    r = _rules()
    # exactly what Supabase hands the server for a normal signed-in user
    assert r.is_platform_admin(_User("boss@sklzlabs.com",
                                     "authenticated")) is True
    assert r.is_platform_admin(_User("BOSS@SKLZLABS.COM",
                                     "authenticated")) is True


def test_D_ordinary_role_and_not_allowlisted():
    os.environ["ADMIN_EMAILS"] = "boss@sklzlabs.com"
    os.environ["OWNER_EMAIL"] = ""
    r = _rules()
    assert r.is_platform_admin(_User("member@example.com",
                                     "authenticated")) is False
    assert r.is_platform_admin(_User("", "authenticated")) is False


def test_the_server_boundary_is_not_the_boolean():
    """The capability is for display. The routes still demand the rule."""
    orders = open("./orders_api.py").read()
    for name in ("async def create_demo_link(", "async def list_demo_links(",
                 "async def revoke_demo_link("):
        fn = orders[orders.index(name):]
        fn = fn[:fn.index("\n\n\n")] if "\n\n\n" in fn else fn
        assert "rules.is_platform_admin(user)" in fn, name
        assert "HTTP_403_FORBIDDEN" in fn, name
        # the routes never consult the reported field
        assert "is_platform_admin=" not in fn, name
