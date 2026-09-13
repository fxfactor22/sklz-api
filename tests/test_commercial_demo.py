"""The commercial demo: one link, one offer, and no invented discount.

Two things are being guarded here. The first is that a prospect's link
opens the whole environment rather than one screen. The second is much
more important: the 50% offer is a sales-assisted discount, and every
test that mentions Stripe or the public package response exists to prove
that nothing here quietly became a checkout coupon.
"""
import importlib.util
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, ".")
API = open("./orders_api.py").read()


def _fn(name, end=None):
    s = API[API.index(name):]
    return s[:s.index(end)] if end and end in s else s


def _code(src):
    """Executable lines only — comments and docstrings removed.

    The "this must not mention X" tests below are about what the code
    DOES. Prose explaining why it does not do X is not a violation, and a
    test that cannot tell the difference punishes the comment.
    """
    out, in_doc, quote = [], False, ""
    for line in src.splitlines():
        s = line.strip()
        if in_doc:
            if quote in s:
                in_doc = False
            continue
        if s.startswith(('"""', "'''")):
            quote = s[:3]
            body = s[3:]
            if quote not in body:
                in_doc = True
            continue
        if s.startswith("#"):
            continue
        out.append(line.split("  # ")[0])
    return "\n".join(out)


def _mod():
    """The pure helpers, loaded without the FastAPI/Supabase surface."""
    ns = {}
    exec("import hashlib, os as _os\n"
         "from datetime import datetime, timedelta, timezone", ns)

    def block(start, end):
        i = API.index(start)
        return API[i:API.index(end, i)]

    exec(block("PACKAGE_DEFS = [", '@router.get("/packages")'), ns)
    exec(block("DEMO_HOURS = 48", "class DemoLinkIn("), ns)
    return ns


M = _mod()
NOW = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)


def _row(**kw):
    row = {"token": "a" * 32, "purpose": "private_demo", "revoked": False,
           "created_at": NOW.isoformat(),
           "expires_at": (NOW + timedelta(hours=48)).isoformat()}
    row.update(kw)
    return row


# ── the primary link is the experience, not the desk ───────────────────
def test_create_returns_the_website_as_the_primary_url():
    fn = _code(_fn("async def create_demo_link(", "@demo_router.get"))
    assert 'links = _demo_links_for(site, token)' in fn
    assert '"url": links["experience"]' in fn
    assert "signal-desk.html?t=" not in fn


def test_the_experience_link_is_the_trader_site():
    links = M["_demo_links_for"]("https://www.sklzlabs.com", "abc")
    assert links["experience"] == \
        "https://www.sklzlabs.com/demo/trader-site.html?t=abc"


def test_every_deep_link_carries_the_same_one_token():
    links = M["_demo_links_for"]("https://s", "TOK")
    assert set(links) == {"experience", "os", "signal_desk", "portal"}
    assert all(v.endswith("?t=TOK") for v in links.values())
    assert len({v.split("?")[0] for v in links.values()}) == 4


def test_site_url_is_still_the_one_source_of_the_host():
    fn = _fn("async def create_demo_link(", "@demo_router.get")
    assert 'SITE_URL' in fn
    assert 'https://www.sklzlabs.com' in fn      # same default as before


def test_the_listing_and_the_read_expose_the_same_link_set():
    for fn in (_fn("async def list_demo_links(", "@demo_router.post"),
               _fn("async def read_demo_link(", "@demo_router.get(\"\")")):
        assert "_demo_links_for(site" in fn


# ── who gets an offer, decided by the server and nothing else ──────────
def test_a_standard_48h_private_demo_gets_the_offer():
    o = M["_offer_for"](_row(), NOW)
    assert o["eligible"] is True
    assert o["setup_discount_percent"] == 50
    assert o["applies_to"] == "setup_only"
    assert o["monthly_discounted"] is False
    assert o["seconds_remaining"] == 48 * 3600


def test_a_showcase_never_gets_the_private_offer():
    o = M["_offer_for"](_row(purpose="showcase", expires_at=None), NOW)
    assert o["eligible"] is False
    assert o["reason"] == "showcase"
    assert o["promo_code"] == ""
    assert "packages" not in o


def test_a_showcase_is_ineligible_even_with_a_48h_shaped_row():
    """A showcase should never have an expiry, but purpose decides first."""
    o = M["_offer_for"](_row(purpose="showcase"), NOW)
    assert o["eligible"] is False and o["reason"] == "showcase"


def test_a_non_48h_private_demo_gets_no_offer():
    for hours in (1, 2, 6, 24, 72, 168):
        row = _row(expires_at=(NOW + timedelta(hours=hours)).isoformat())
        o = M["_offer_for"](row, NOW)
        assert o["eligible"] is False, hours
        assert o["reason"] == "not_a_standard_48h_demo", hours


def test_clock_drift_inside_the_tolerance_still_qualifies():
    """created_at is a database default; expires_at is written here."""
    row = _row(created_at=(NOW - timedelta(minutes=4)).isoformat())
    assert M["_offer_for"](row, NOW)["eligible"] is True


def test_a_revoked_link_has_no_active_offer():
    o = M["_offer_for"](_row(revoked=True), NOW)
    assert o["eligible"] is False and o["reason"] == "revoked"


def test_an_expired_link_has_no_active_offer():
    later = NOW + timedelta(hours=49)
    o = M["_offer_for"](_row(), later)
    assert o["eligible"] is False and o["reason"] == "expired"
    assert o["promo_code"] == ""


def test_an_ineligible_offer_never_carries_prices():
    for row in (_row(purpose="showcase"), _row(revoked=True),
                _row(expires_at=(NOW + timedelta(hours=3)).isoformat())):
        assert "packages" not in M["_offer_for"](row, NOW)


def test_eligibility_reads_the_stored_row_and_nothing_else():
    fn = _code(_fn("def _offer_for(", "class DemoLinkIn("))
    for forbidden in ("localStorage", "request", "headers", "referer",
                      "provider_name", "contact_email", "note"):
        assert forbidden not in fn, forbidden


# ── the promo code ─────────────────────────────────────────────────────
def test_the_code_is_deterministic_for_one_token():
    t = "6b6a955dab4b9086ad869620844750c0"
    assert M["_offer_code"](t) == M["_offer_code"](t)


def test_different_tokens_produce_different_codes():
    seen = {M["_offer_code"]("%032x" % i) for i in range(500)}
    assert len(seen) == 500


def test_the_code_has_the_advertised_shape():
    c = M["_offer_code"]("a" * 32)
    assert c.startswith("SKLZ50-")
    body = c.split("-", 1)[1]
    assert len(body) == 10
    assert all(ch in "0123456789ABCDEF" for ch in body)


def test_the_code_does_not_leak_the_token():
    t = "0123456789abcdef0123456789abcdef"
    code = M["_offer_code"](t)
    body = code.split("-", 1)[1].lower()
    assert body not in t
    assert t[:10] not in body and t[-10:] not in body


def test_the_code_is_derived_from_the_token_alone():
    fn = _code(_fn("def _offer_code(", "def _offer_packages("))
    for pii in ("email", "contact", "name", "note", "telegram"):
        assert pii not in fn, pii
    assert "hashlib.sha256" in fn


def test_an_empty_token_yields_no_code():
    assert M["_offer_code"]("") == ""


# ── prices: one source, setup only ─────────────────────────────────────
def test_the_public_package_response_is_unchanged():
    cfg = M["_package_config"]()
    assert cfg["signal_desk"]["setup"]["display"] == "$499"
    assert cfg["signal_desk"]["monthly"]["display"] == "$49"
    assert cfg["signal_desk_pro"]["setup"]["display"] == "$999"
    assert cfg["signal_desk_pro"]["monthly"]["display"] == "$99"
    assert cfg["pro_trader_os"]["setup"]["display"] == "$1,499"
    assert cfg["pro_trader_os"]["monthly"]["display"] == "$149"
    # no offer figure has leaked into the public shape
    for p in cfg.values():
        assert "offer_setup" not in p and "private_offer" not in p


def test_the_packages_endpoint_body_did_not_grow_an_offer():
    fn = _code(_fn("async def packages(", "class LeadIn("))
    assert "_offer_packages" not in fn
    assert "discount" not in fn.lower()


def test_offer_setup_is_exactly_half_of_the_live_setup():
    cfg = M["_package_config"]()
    for k, p in M["_offer_packages"]().items():
        assert p["offer_setup"]["usd"] == \
            round(cfg[k]["setup"]["usd"] * 0.5, 2), k


def test_the_expected_offer_figures():
    o = M["_offer_packages"]()
    assert o["signal_desk"]["normal_setup"]["display"] == "$499"
    assert o["signal_desk"]["offer_setup"]["display"] == "$249.50"
    assert o["signal_desk_pro"]["offer_setup"]["display"] == "$499.50"
    assert o["pro_trader_os"]["offer_setup"]["display"] == "$749.50"


def test_monthly_is_untouched_by_the_offer():
    cfg = M["_package_config"]()
    for k, p in M["_offer_packages"]().items():
        assert p["monthly"]["usd"] == cfg[k]["monthly"]["usd"], k
        assert p["monthly"]["display"] == cfg[k]["monthly"]["display"], k
        assert p["monthly"]["discounted"] is False, k


def test_an_env_price_override_moves_normal_and_offer_together(monkeypatch):
    M["_os"].environ["SKLZ_PRICE_SIGNAL_DESK_SETUP"] = "800"
    M["_os"].environ["SKLZ_PRICE_SIGNAL_DESK_MONTHLY"] = "80"
    try:
        cfg = M["_package_config"]()
        off = M["_offer_packages"]()["signal_desk"]
        assert cfg["signal_desk"]["setup"]["display"] == "$800"
        assert off["normal_setup"]["display"] == "$800"
        assert off["offer_setup"]["display"] == "$400"
        assert off["monthly"]["display"] == "$80"
    finally:
        del M["_os"].environ["SKLZ_PRICE_SIGNAL_DESK_SETUP"]
        del M["_os"].environ["SKLZ_PRICE_SIGNAL_DESK_MONTHLY"]


def test_the_offer_never_quotes_the_activation_aggregate():
    """Setup and monthly, separately. The 'due today' total is ambiguous
    and is deliberately not part of the private offer."""
    for p in M["_offer_packages"]().values():
        assert "activation" not in p
        assert "due today" not in str(p)


def test_offer_prices_come_from_the_one_package_configuration():
    fn = _code(_fn("def _offer_packages(", "@router.get"))
    assert "_package_config()" in fn
    for literal in ("499", "999", "1499", "249", "749"):
        assert literal not in fn, literal


# ── no discount reached checkout ───────────────────────────────────────
def test_nothing_here_touches_stripe():
    offer_code = _code(
        _fn("OFFER_SETUP_DISCOUNT_PERCENT = 50", '@router.get("/packages")')
        + _fn("def _ts(", "class DemoLinkIn("))
    for s in ("stripe", "coupon", "allow_promotion_codes", "price_id",
              "checkout", "discounts"):
        assert s not in offer_code.lower(), s


def test_the_offer_declares_itself_sales_assisted():
    o = M["_offer_for"](_row(), NOW)
    assert o["redemption"] == "sales_assisted"


def test_crypto_and_card_amounts_are_untouched():
    """_package_config drives both payment paths; the offer reads it and
    writes nothing back to it."""
    before = M["_package_config"]()
    M["_offer_packages"]()
    assert M["_package_config"]() == before


# ── the created link reports its own offer ─────────────────────────────
def test_create_reports_the_offer_it_just_minted():
    fn = _fn("async def create_demo_link(", "@demo_router.get")
    assert "_offer_for(" in fn
    assert "minted_at" in fn
    assert '"offer": offer' in fn


def test_read_reports_the_offer_from_the_stored_row():
    fn = _fn("async def read_demo_link(", '@demo_router.get("")')
    assert '"offer": _offer_for(row, now)' in fn
