"""The private 48-hour offer, at checkout.

A discount that a browser can ask for is not a discount, it is a price
list. Every test here exists to prove that the only thing that grants the
50% setup reduction is a row in `demo_links` that the server itself finds
eligible — and that when it refuses, no Stripe session is created at all.

The cases are lettered A-O so a failure names the guarantee it broke.
"""
import asyncio
import os
import sys
import types
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

sys.path.insert(0, ".")

TOK = "a" * 32
OTHER = "b" * 32
NOW = datetime.now(timezone.utc)

# ── a Stripe that records instead of charging ──────────────────────────
CREATED = []            # every Checkout Session this module asked for


class _Obj(dict):
    __getattr__ = dict.get


def _fake_stripe(setup_cents=49900, product="prod_setup_sd"):
    """A stripe module stand-in. It never reaches the network and it has
    no key: a test that accidentally needed a real one would fail here
    rather than somewhere expensive."""
    m = types.ModuleType("stripe")
    m.api_key = None

    class Price:
        @staticmethod
        def list(lookup_keys, limit=1):
            key = lookup_keys[0]
            amounts = {"sd_setup": setup_cents, "sd_monthly": 4900,
                       "sdpro_setup": 99900, "sdpro_monthly": 9900,
                       "ptos_setup": 149900, "ptos_monthly": 14900}
            products = {"sd_setup": product, "sd_monthly": "prod_mon_sd",
                        "sdpro_setup": "prod_setup_pro",
                        "sdpro_monthly": "prod_mon_pro",
                        "ptos_setup": "prod_setup_os",
                        "ptos_monthly": "prod_mon_os"}
            return _Obj(data=[_Obj(id="price_" + key,
                                   unit_amount=amounts[key],
                                   currency="usd",
                                   product=products[key])])

    class Session:
        @staticmethod
        def create(**params):
            CREATED.append(params)
            return _Obj(url="https://checkout.stripe.com/c/pay/test")

    m.Price = Price
    m.checkout = types.SimpleNamespace(Session=Session)
    return m


class FakeSB:
    """Only what `_offer_or_refuse` asks of it: one row by token."""

    def __init__(self, rows=None, boom=False):
        self.rows = rows or []
        self.boom = boom
        self.queried = []

    def table(self, name):
        assert name == "demo_links"
        return self

    def select(self, *_a, **_k):
        return self

    def eq(self, col, val):
        self.queried.append((col, val))
        return self

    def limit(self, _n):
        return self

    def execute(self):
        if self.boom:
            raise RuntimeError("store down")
        tok = dict(self.queried).get("token")
        return _Obj(data=[r for r in self.rows if r["token"] == tok])


def _row(token=TOK, hours=48, purpose="private_demo", revoked=False,
         age_hours=1.0):
    created = NOW - timedelta(hours=age_hours)
    return {"token": token, "purpose": purpose, "revoked": revoked,
            "provider_name": "Prospect FX",
            "created_at": created.isoformat(),
            "expires_at": (created + timedelta(hours=hours)).isoformat()}


@pytest.fixture(autouse=True)
def billing(monkeypatch):
    """A fresh module view per test, with Stripe stubbed and a key set.

    The key is the literal string "sk_test_not_a_key": this suite must
    never be able to run against a real Stripe account.
    """
    CREATED.clear()
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_not_a_key")
    monkeypatch.setitem(sys.modules, "stripe", _fake_stripe())
    import billing as b
    return b


def _call(billing, sb=None, **body):
    from billing import PackageCheckoutIn
    payload = PackageCheckoutIn(**body)
    return asyncio.run(billing.checkout_package(
        payload, sb=sb if sb is not None else FakeSB([_row()])))


def _lines(params):
    return params["line_items"]


# ── A. the full-price path is exactly what it was ──────────────────────
def test_A_no_token_is_the_full_price_checkout(billing):
    out = _call(billing, package="signal_desk")
    assert out["offer_applied"] is False
    assert out["setup_cents"] == 49900 and out["monthly_cents"] == 4900
    assert out["due_today_cents"] == 54800
    assert out["setup_discount_percent"] == 0 and out["promo_code"] == ""
    p = CREATED[-1]
    assert p["mode"] == "subscription"
    assert _lines(p) == [{"price": "price_sd_setup", "quantity": 1},
                         {"price": "price_sd_monthly", "quantity": 1}]
    assert "offer_type" not in p["metadata"]
    assert p["cancel_url"].endswith("/signal-desk.html#packages")


# ── B. an eligible link discounts SETUP and nothing else ───────────────
def test_B_eligible_token_discounts_setup_only(billing):
    out = _call(billing, package="signal_desk", demo_token=TOK)
    assert out["offer_applied"] is True
    p = CREATED[-1]
    setup, monthly = _lines(p)
    # setup: an inline one-time amount on the SAME product as the
    # catalogue price, so nothing new is left behind in Stripe
    assert setup["price_data"]["product"] == "prod_setup_sd"
    assert setup["price_data"]["unit_amount"] == 24950
    assert setup["price_data"]["currency"] == "usd"
    assert "price" not in setup
    # monthly: untouched, still the catalogue Price ID
    assert monthly == {"price": "price_sd_monthly", "quantity": 1}


# ── C. the response says what Stripe was actually asked to charge ──────
def test_C_response_cents_are_truthful(billing):
    out = _call(billing, package="signal_desk", demo_token=TOK)
    assert out["setup_cents"] == 24950          # what is charged
    assert out["setup_list_cents"] == 49900     # what it normally costs
    assert out["monthly_cents"] == 4900         # unchanged
    assert out["due_today_cents"] == 24950 + 4900 == 29850
    assert out["setup_discount_percent"] == 50
    assert out["promo_code"].startswith("SKLZ50-")


@pytest.mark.parametrize("pkg,cents,due", [
    ("signal_desk", 24950, 29850),
    ("signal_desk_pro", 49950, 59850),
    ("pro_trader_os", 74950, 89850),
])
def test_C2_every_package_halves_its_own_setup(billing, pkg, cents, due):
    out = _call(billing, package=pkg, demo_token=TOK)
    assert out["setup_cents"] == cents
    assert out["due_today_cents"] == due


# ── D. the discounted line is one-time, not a second subscription ──────
def test_D_discounted_setup_carries_no_recurring(billing):
    _call(billing, package="signal_desk", demo_token=TOK)
    setup = _lines(CREATED[-1])[0]
    assert "recurring" not in setup["price_data"]


# ── E-I. every refusal creates nothing ─────────────────────────────────
@pytest.mark.parametrize("row,reason", [
    (_row(age_hours=50), "expired"),
    (_row(revoked=True), "revoked"),
    (_row(purpose="showcase", hours=48), "showcase"),
    (_row(hours=168), "not_a_standard_48h_demo"),
])
def test_EFGH_an_ineligible_link_is_refused_and_nothing_is_created(
        billing, row, reason):
    with pytest.raises(HTTPException) as e:
        _call(billing, package="signal_desk", demo_token=row["token"],
              sb=FakeSB([row]))
    assert e.value.status_code == 409
    assert e.value.detail == "offer_not_available: " + reason
    assert CREATED == []


def test_I_an_unknown_token_buys_nothing_at_all(billing):
    with pytest.raises(HTTPException) as e:
        _call(billing, package="signal_desk", demo_token=OTHER,
              sb=FakeSB([_row()]))
    assert e.value.status_code == 409
    assert e.value.detail == "offer_not_available: unknown link"
    assert CREATED == []


# ── J. a malformed token never reaches the database ────────────────────
@pytest.mark.parametrize("bad", [
    "A" * 32 + "!", "a" * 31, "a" * 33, "zz" + "a" * 30,
    "' or 1=1 --", "../../etc/passwd", "a" * 31 + " ", "a" * 32 + "0",
])
def test_J_a_malformed_token_is_refused_before_any_lookup(billing, bad):
    sb = FakeSB([_row()])
    with pytest.raises(HTTPException) as e:
        _call(billing, package="signal_desk", demo_token=bad, sb=sb)
    assert e.value.status_code == 400
    assert sb.queried == []          # the store was never asked
    assert CREATED == []


def test_J2_a_token_is_normalised_not_guessed(billing):
    """Upper-case hex is the same link, and only because it is hex."""
    out = _call(billing, package="signal_desk", demo_token=TOK.upper(),
                sb=FakeSB([_row()]))
    assert out["offer_applied"] is True


# ── K. nothing the browser sends is money ──────────────────────────────
def test_K_the_browser_cannot_set_a_price(billing):
    out = _call(billing, package="signal_desk", demo_token=TOK,
                unit_amount=1, setup_cents=1, amount=1,
                setup_discount_percent=99, discount=99,
                promo_code="SKLZ50-FREE", coupon="free", offer_applied=True)
    # every figure is still the server's own
    assert out["setup_cents"] == 24950
    assert out["setup_discount_percent"] == 50
    assert out["promo_code"] != "SKLZ50-FREE"
    assert _lines(CREATED[-1])[0]["price_data"]["unit_amount"] == 24950


def test_K2_a_token_alone_grants_nothing_without_a_row(billing):
    """The presence of a token in a request body is not authority."""
    with pytest.raises(HTTPException):
        _call(billing, package="signal_desk", demo_token=TOK, sb=FakeSB([]))
    assert CREATED == []


# ── L. the displayed price and the charged price must agree ────────────
def test_L_a_price_override_stops_the_checkout(billing, monkeypatch):
    """_package_config honours SKLZ_PRICE_*; CATALOG does not. If they
    ever disagree the customer would read one number and be billed
    another, so no session is created at all."""
    monkeypatch.setenv("SKLZ_PRICE_SIGNAL_DESK_SETUP", "599")
    with pytest.raises(HTTPException) as e:
        _call(billing, package="signal_desk")
    assert e.value.status_code == 503
    assert e.value.detail.startswith("pricing_mismatch")
    assert CREATED == []


def test_L2_the_guard_runs_on_the_offer_path_too(billing, monkeypatch):
    monkeypatch.setenv("SKLZ_PRICE_SIGNAL_DESK_MONTHLY", "59")
    with pytest.raises(HTTPException) as e:
        _call(billing, package="signal_desk", demo_token=TOK)
    assert e.value.status_code == 503
    assert e.value.detail.startswith("pricing_mismatch")
    assert CREATED == []


# ── M. Stripe's own number is checked before a discount is quoted ──────
def test_M_a_stripe_price_that_disagrees_stops_the_offer(billing,
                                                         monkeypatch):
    monkeypatch.setitem(sys.modules, "stripe", _fake_stripe(setup_cents=39900))
    with pytest.raises(HTTPException) as e:
        _call(billing, package="signal_desk", demo_token=TOK)
    assert e.value.status_code == 503
    assert e.value.detail.startswith("pricing_mismatch")
    assert CREATED == []


# ── N. a cancelled checkout cannot be sent anywhere else ───────────────
@pytest.mark.parametrize("surface,tail", [
    ("experience", "/demo/trader-site.html?t=" + TOK),
    ("os", "/demo/pro-trader-os.html?t=" + TOK),
    ("signal_desk", "/demo/signal-desk.html?t=" + TOK),
    ("portal", "/demo/client-portal.html?t=" + TOK),
])
def test_N_cancel_returns_to_the_named_surface(billing, surface, tail):
    _call(billing, package="signal_desk", demo_token=TOK, surface=surface)
    assert CREATED[-1]["cancel_url"].endswith(tail)


@pytest.mark.parametrize("surface", [
    "https://evil.example/steal", "//evil.example", "javascript:alert(1)",
    "../../../admin", "", "OS", "unknown", "experience\n"])
def test_N2_a_surface_cannot_become_a_redirect(billing, surface):
    _call(billing, package="signal_desk", demo_token=TOK, surface=surface)
    url = CREATED[-1]["cancel_url"]
    assert url.startswith(billing.SITE + "/demo/")
    assert "evil.example" not in url and "javascript" not in url


# ── O. the payment records the offer, never the credential ─────────────
def test_O_metadata_carries_the_offer_and_not_the_token(billing):
    out = _call(billing, package="signal_desk", demo_token=TOK)
    md = CREATED[-1]["metadata"]
    sub = CREATED[-1]["subscription_data"]["metadata"]
    assert md["offer_type"] == "private_48h"
    assert md["setup_discount_percent"] == "50"
    assert md["promo_code"] == out["promo_code"]
    assert md["setup_list_cents"] == "49900"
    assert md["setup_charged_cents"] == "24950"
    assert sub["offer_type"] == "private_48h"
    # The token opens a live demo environment. It is a credential, and it
    # is not recorded on the payment: everything Stripe keeps is derived
    # (the promo code) or public (the figures).
    assert TOK not in repr(md) and TOK not in repr(sub)
    assert "demo_token" not in repr(CREATED[-1])
    # The one place it legitimately appears is the cancel URL, because
    # that is the prospect's own link back into their own demo.
    assert TOK in CREATED[-1]["cancel_url"]


# ── the eligibility rule exists once, and this is not where ────────────
def test_eligibility_is_not_reimplemented_here():
    src = open("./billing.py").read()
    fn = src[src.index("async def _offer_or_refuse("):]
    fn = fn[:fn.index("class PackageCheckoutIn(")]
    assert "_offer_for(" in fn
    for own in ("timedelta", "48", "expires_at", "revoked", "purpose"):
        assert own not in fn, own


def test_the_store_is_asked_only_for_the_one_token(billing):
    sb = FakeSB([_row(), _row(token=OTHER)])
    _call(billing, package="signal_desk", demo_token=TOK, sb=sb)
    assert sb.queried == [("token", TOK)]
