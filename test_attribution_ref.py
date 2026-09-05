"""Prove the attribution ref reaches Stripe, before this ships.

The claim being tested is narrow and load-bearing: a `ref` supplied to
checkout-public must arrive on the Stripe Checkout Session as BOTH
client_reference_id and metadata.ref, and — for a subscription — on the
subscription's own metadata as well, because every renewal after the first
arrives as an event that never saw the checkout session.

Stripe is stubbed. We are not testing Stripe; we are testing the params we
hand it, which is the only part we wrote.

Run: python3 test_attribution_ref.py
"""
import asyncio, os, sys, types

os.environ.setdefault("STRIPE_SECRET_KEY", "sk_test_stub")

# ── stub the modules billing.py reaches for ───────────────────────────────
captured = {}


class _Session:
    @staticmethod
    def create(**params):
        captured.clear()
        captured.update(params)
        return types.SimpleNamespace(url="https://checkout.stripe.test/session",
                                     id="cs_test_stub")


class _Price:
    @staticmethod
    def list(**kw):
        return types.SimpleNamespace(data=[types.SimpleNamespace(id="price_stub")])


stripe_stub = types.ModuleType("stripe")
stripe_stub.api_key = None
stripe_stub.checkout = types.SimpleNamespace(Session=_Session)
stripe_stub.Price = _Price
sys.modules["stripe"] = stripe_stub

# supabase / project-local imports billing.py pulls in
for name in ("supabase", "db", "auth", "tv_access", "partner", "sklz_tiers"):
    if name not in sys.modules:
        m = types.ModuleType(name)
        sys.modules[name] = m
sys.modules["supabase"].Client = object
sys.modules["db"].get_supabase = lambda: None
sys.modules["auth"].get_current_user = lambda: None
sys.modules["tv_access"]._require_admin = lambda u: None
sys.modules["partner"]._credit_referrer = lambda *a, **k: None
sys.modules["partner"]._partner_payment = lambda *a, **k: None
sys.modules["partner"]._partner_churn = lambda *a, **k: None

import billing  # noqa: E402

# _founder_taken hits the database; keep founder OUT of the way so the
# product under test is not silently swapped for bundle_founder.
billing._founder_taken = lambda sb: billing.FOUNDER_CAP

REF = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
fails = []
ok = lambda m: print("  ok   " + m)


def bad(m):
    fails.append(m)
    print("  FAIL " + m)


def call(product, ref=None):
    payload = billing.CheckoutIn(product=product, **({"ref": ref} if ref is not None else {}))
    return asyncio.run(billing.checkout_public(payload, sb=None))


print("\nREF REACHES STRIPE")
call("copy_crypto_monthly", REF)

if captured.get("client_reference_id") == REF:
    ok("client_reference_id == ref")
else:
    bad("client_reference_id is %r" % captured.get("client_reference_id"))

if (captured.get("metadata") or {}).get("ref") == REF:
    ok("session metadata.ref == ref")
else:
    bad("session metadata.ref is %r" % (captured.get("metadata") or {}).get("ref"))

sub = (captured.get("subscription_data") or {}).get("metadata") or {}
if sub.get("ref") == REF:
    ok("subscription metadata.ref == ref  (renewals stay attributable)")
else:
    bad("subscription metadata.ref is %r — month two would lose attribution" % sub.get("ref"))

print("\nSERVER STILL OWNS COMMERCIALS")
# The caller sent only a product key. Everything commercial must come from
# the server's own catalog.
if "line_items" in captured and captured["line_items"][0]["price"] == "price_stub":
    ok("price resolved server-side from the catalog lookup key")
else:
    bad("line_items look wrong: %r" % captured.get("line_items"))

if captured.get("mode") == "subscription":
    ok("recurring vs one-time decided by the server catalog, not the caller")
else:
    bad("mode is %r" % captured.get("mode"))

for forbidden in ("amount", "unit_amount", "currency", "price_data"):
    if forbidden in captured:
        bad("caller-influenced %r reached Stripe — the server must own the amount" % forbidden)
if not any(k in captured for k in ("amount", "unit_amount", "price_data")):
    ok("no amount of any kind is sent by the caller")

# one-time product takes the payment_intent path and must still carry the ref
call("suite_lifetime", REF)
pi = (captured.get("payment_intent_data") or {}).get("metadata") or {}
if captured.get("mode") == "payment" and pi.get("ref") == REF:
    ok("one-time purchase carries the ref on the payment intent")
else:
    bad("one-time path: mode=%r ref=%r" % (captured.get("mode"), pi.get("ref")))

print("\nREF IS VALIDATED")
call("copy_basic_monthly")                      # absent
if "client_reference_id" not in captured and "ref" not in (captured.get("metadata") or {}):
    ok("no ref supplied -> nothing added (website checkouts unaffected)")
else:
    bad("a ref appeared when none was given")

for junk in ["not-a-uuid", "'; drop table subscriptions;--", "x" * 500,
             "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeeez"]:
    try:
        call("copy_basic_monthly", junk)
        bad("junk ref accepted: %r" % junk[:40])
    except Exception as exc:
        if getattr(exc, "status_code", None) == 400:
            ok("rejected junk ref (%s…)" % junk[:18])
        else:
            bad("junk ref raised the wrong thing: %r" % exc)

print("\nUNKNOWN PRODUCT STILL REFUSED")
try:
    call("__not_a_product__", REF)
    bad("an unknown product was accepted")
except Exception as exc:
    if getattr(exc, "status_code", None) == 400:
        ok("unknown product -> 400")
    else:
        bad("unknown product raised %r" % exc)

print("")
if fails:
    print("%d PROBLEM%s" % (len(fails), "S" if len(fails) > 1 else ""))
    sys.exit(1)
print("ref plumbing verified — attribution reaches Stripe, server still owns the money.")
