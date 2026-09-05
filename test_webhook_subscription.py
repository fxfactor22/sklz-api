"""Reproduce the 31 Aug webhook failure, then prove it is gone.

WHAT HAPPENED. Stripe delivered customer.subscription.deleted
(evt_1UATCUF07IAsxK4LyqZrNE6C). The endpoint answered HTTP 200 — so Stripe
recorded a success and never retried — with this body:

  {"ok": false,
   "error": "AttributeError: 'get' is a dict method, but a Subscription is
             not a dict. Use .to_dict() to convert it...",
   "event": "customer.subscription.deleted"}

The cancellation was therefore never written down: the subscription row stayed
active, _partner_churn never fired, and once attribution ships the churn relay
would silently not fire either.

WHY. webhook() tried dict(obj), fell back to obj.to_dict_recursive(), and on
failure of both did `pass` — leaving a StripeObject in a variable the rest of
the code treats as a dict. Which of those conversions works depends on the
installed stripe-python version, so the bug appears at deploy time, not in
review.

THE FIXTURE. _Subscription below is that object: not a dict, .get() raises
the real message, to_dict_recursive() is gone. If the handler ever again
depends on either, these tests fail.

Run: python3 test_webhook_subscription.py
"""
import asyncio, json, os, sys, types

os.environ["STRIPE_SECRET_KEY"] = "sk_test_stub"
os.environ["STRIPE_WEBHOOK_SECRET"] = "whsec_stub"
os.environ.pop("ATTRIBUTION_WEBHOOK_URL", None)     # relay must stay silent
os.environ.pop("ATTRIBUTION_WEBHOOK_SECRET", None)

fails = []
ok = lambda m: print("  ok   " + m)


def bad(m):
    fails.append(m)
    print("  FAIL " + m)


# ── the object Stripe actually handed us on 31 Aug ────────────────────────
class _StripeObj:
    """Dict-ish, but not a dict, and hostile to the usual escapes."""
    _NOT_A_DICT = ("'%s' is a dict method, but Subscription is not a dict. "
                   "Use .to_dict() to convert it. "
                   "Docs: https://github.com/stripe/stripe-python"
                   "#working-with-api-resources")

    def __init__(self, d):
        self._v = d

    # the failure mode: .get() is refused
    def get(self, *a, **k):
        raise AttributeError(self._NOT_A_DICT % "get")

    def keys(self, *a, **k):
        raise AttributeError(self._NOT_A_DICT % "keys")

    def items(self, *a, **k):
        raise AttributeError(self._NOT_A_DICT % "items")

    def to_dict_recursive(self):
        raise AttributeError("to_dict_recursive was removed")

    def __getitem__(self, k):
        return self._v[k]

    def __iter__(self):                     # so dict(obj) cannot rescue it
        raise TypeError("Subscription is not iterable")


SUB = {
    "id": "sub_1UATCUF07IAsxK4L",
    "object": "subscription",
    "customer": "cus_TEST",
    "status": "canceled",
    "current_period_end": 1756598400,
    "metadata": {"user_id": "user-abc", "product": "bundle_monthly",
                 "ref": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"},
}

# ── stubs for everything billing.py imports ───────────────────────────────
stripe_stub = types.ModuleType("stripe")
stripe_stub.api_key = None


class _Webhook:
    @staticmethod
    def construct_event(payload, sig, secret):
        # Signature verification is Stripe's; what matters here is that it
        # returns an SDK object, not JSON.
        b = _Webhook.body
        return {"type": _Webhook.etype,
                "data": {"object": _StripeObj(b) if isinstance(b, dict) else b}}


stripe_stub.Webhook = _Webhook
stripe_stub.checkout = types.SimpleNamespace(Session=object)
stripe_stub.Price = types.SimpleNamespace(list=lambda **k: types.SimpleNamespace(data=[]))
sys.modules["stripe"] = stripe_stub

for name in ("supabase", "db", "auth", "tv_access", "partner", "sklz_tiers"):
    sys.modules.setdefault(name, types.ModuleType(name))
sys.modules["supabase"].Client = object
sys.modules["db"].get_supabase = lambda: None
sys.modules["auth"].get_current_user = lambda: None
sys.modules["tv_access"]._require_admin = lambda u: None

import billing  # noqa: E402

# These three make outbound HTTP calls of their own; billing.py defines them
# itself, so they are replaced on the module rather than on an import.
churned, credited = [], []
# The real one returns at its guard when uid is empty, so the stub must too —
# otherwise "credited" would mean something different here than in production.
billing._credit_referrer = lambda uid, *a, **k: credited.append(uid) if uid else None
billing._partner_payment = lambda *a, **k: None
billing._partner_churn = lambda uid, *a, **k: churned.append(uid)

# Capture the relay instead of making a network call. The real one returns at
# its guard when the ref is empty, so an empty ref must record nothing here
# either — otherwise "relayed" would mean something different in the test than
# it does in production.
relayed = []


def _fake_relay(ref, event, product="", amount=None, currency="USD", session_id=""):
    if not ref:
        return
    relayed.append({"ref": ref, "event": event, "product": product,
                    "amount": amount, "session_id": session_id})


billing._attr_relay = _fake_relay

# a supabase stand-in that records upserts
upserts = []


class _Tbl:
    def upsert(self, row, on_conflict=None):
        upserts.append(row)
        return self

    def select(self, *a, **k):
        return self

    def eq(self, *a, **k):
        return self

    def execute(self):
        return types.SimpleNamespace(data=[])


class _SB:
    def table(self, name):
        return _Tbl()


class _Req:
    def __init__(self, raw):
        self._raw = raw

    async def body(self):
        return self._raw


def deliver(etype, body, raw=None):
    """One webhook delivery, exactly as FastAPI would make it."""
    upserts.clear(); churned.clear(); credited.clear(); relayed.clear()
    _Webhook.etype, _Webhook.body = etype, body
    raw = json.dumps({"type": etype, "data": {"object": body}}).encode() if raw is None else raw
    return asyncio.run(billing.webhook(_Req(raw), stripe_signature="t=1,v1=x", sb=_SB()))


print("\nTHE 31 AUG DELIVERY")
res = deliver("customer.subscription.deleted", SUB)

if "error" in res:
    bad("still failing: " + str(res["error"])[:120])
else:
    ok("customer.subscription.deleted processed without an AttributeError")

if res.get("received") is True:
    ok("handler returned received:true")
else:
    bad("handler returned %r" % res)

if churned == ["user-abc"]:
    ok("_partner_churn fired for the cancelling user")
else:
    bad("_partner_churn saw %r — churn is still not recorded" % churned)

if any(u.get("active") is False for u in upserts):
    ok("subscription row marked inactive")
else:
    bad("nothing deactivated the subscription: %r" % upserts)

if relayed and relayed[0]["ref"] == SUB["metadata"]["ref"] \
        and relayed[0]["event"] == "customer.subscription.deleted":
    ok("churn relayed with the attribution ref intact")
else:
    bad("relay got %r" % relayed)


print("\nTHE SAME OBJECT ON THE OTHER SUBSCRIPTION EVENTS")
for etype in ("customer.subscription.created", "customer.subscription.updated"):
    body = dict(SUB, status="active")
    res = deliver(etype, body)
    if "error" in res:
        bad(etype + " failed: " + str(res["error"])[:100])
        continue
    live = [u for u in upserts if u.get("active") is True]
    if not live:
        bad(etype + ": subscription not activated")
    elif live[0].get("stripe_subscription_id") != SUB["id"]:
        bad(etype + ": wrong subscription id %r" % live[0].get("stripe_subscription_id"))
    elif live[0].get("current_period_end") is None:
        bad(etype + ": period end lost — nested values are not being read")
    elif not (relayed and relayed[0]["ref"] == SUB["metadata"]["ref"]):
        bad(etype + ": renewal did not relay the ref — month two loses attribution")
    else:
        ok(etype + " -> active, period end kept, ref relayed")


print("\nCHECKOUT AND INVOICE PATHS (same conversion, different shape)")
SESSION = {"id": "cs_test_1", "object": "checkout_session", "mode": "payment",
           "customer": "cus_TEST", "client_reference_id": None,
           "amount_total": 4900, "currency": "usd",
           "metadata": {"user_id": "user-abc", "product": "suite_lifetime",
                        "ref": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"}}
res = deliver("checkout.session.completed", SESSION)
if "error" in res:
    bad("checkout.session.completed failed: " + str(res["error"])[:100])
elif not relayed:
    bad("checkout.session.completed did not relay")
elif relayed[0]["ref"] != SESSION["metadata"]["ref"]:
    bad("checkout relay ref is %r" % relayed[0]["ref"])
elif relayed[0]["amount"] != 49.0:
    bad("amount relayed as %r — cents/dollars confusion" % relayed[0]["amount"])
else:
    ok("checkout.session.completed relays ref and $49.00")

res = deliver("invoice.paid", {"id": "in_1", "object": "invoice",
                               "customer": "cus_TEST", "amount_paid": 4900})
if "error" in res:
    bad("invoice.paid failed: " + str(res["error"])[:100])
else:
    ok("invoice.paid processed")


print("\nTHE REF BOUNDARY")
REF = SESSION["metadata"]["ref"]
# A: the bot's opaque intent uuid, in metadata.ref where it belongs.
deliver("checkout.session.completed",
        dict(SESSION, client_reference_id=REF, metadata={"ref": REF, "product": "suite_lifetime"}))
if relayed and relayed[0]["ref"] == REF:
    ok("A  bot checkout-intent uuid is relayed")
else:
    bad("A  bot ref was not relayed: %r" % relayed)

# B: the load-bearing case. A website checkout puts the customer's own user id
# in client_reference_id, and Supabase user ids are uuids — so a format check
# alone cannot tell it apart from a ref. It must not be relayed.
USER_UUID = "7c9e6679-7425-40de-944b-e07fc1f90ae7"
deliver("checkout.session.completed",
        dict(SESSION, client_reference_id=USER_UUID,
             metadata={"user_id": USER_UUID, "product": "suite_lifetime"}))
if not relayed:
    ok("B  a website customer's user uuid is NOT relayed")
elif relayed[0]["ref"] == USER_UUID:
    bad("B  a real user id was sent to the attribution endpoint as a ref")
else:
    bad("B  unexpected relay: %r" % relayed)

# C: a well-formed ref the receiver has never heard of. Relaying is harmless —
# it answers 200 "unknown ref" and writes nothing — so this one is allowed
# through rather than second-guessed here.
STRANGER = "11111111-2222-3333-4444-555555555555"
deliver("checkout.session.completed",
        dict(SESSION, client_reference_id=None, metadata={"ref": STRANGER}))
if relayed and relayed[0]["ref"] == STRANGER:
    ok("C  an unknown-but-well-formed ref is relayed (receiver refuses it)")
else:
    bad("C  well-formed ref was dropped: %r" % relayed)

# D: malformed. Refused here as well as at checkout time.
for junk in ("not-a-uuid", "'; drop table subscriptions;--", "x" * 200):
    deliver("checkout.session.completed",
            dict(SESSION, client_reference_id=None, metadata={"ref": junk}))
    if relayed:
        bad("D  malformed ref relayed: %r" % junk[:30])
        break
else:
    ok("D  malformed refs are not relayed")

# missing entirely: the ordinary website checkout, unchanged.
deliver("checkout.session.completed",
        dict(SESSION, client_reference_id=None, metadata={"product": "suite_lifetime"}))
if not relayed:
    ok("   no ref at all -> nothing relayed (website checkouts unaffected)")
else:
    bad("   a relay happened with no ref: %r" % relayed)


print("\nNO PHANTOM SUBSCRIPTIONS")
REF2 = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
USER = "7c9e6679-7425-40de-944b-e07fc1f90ae7"

# A. A bot checkout has no account behind it. client_reference_id is the
#    intent uuid, not a person, and must not become one.
deliver("checkout.session.completed",
        dict(SESSION, mode="payment", client_reference_id=REF2,
             metadata={"ref": REF2, "product": "suite_lifetime"}))
if not upserts:
    ok("A  bot checkout writes no subscription row")
else:
    bad("A  phantom subscription for %r" % upserts[0].get("user_id", upserts[0]))
if credited:
    bad("A  affiliate credit ran with %r" % credited)
else:
    ok("A  affiliate credit did not run on an intent uuid")
if relayed and relayed[0]["ref"] == REF2:
    ok("A  attribution still relays")
else:
    bad("A  attribution did not relay: %r" % relayed)

# B. The website path must be untouched.
deliver("checkout.session.completed",
        dict(SESSION, mode="payment", client_reference_id=USER,
             metadata={"product": "suite_lifetime"}))
if len(upserts) == 1 and upserts[0].get("user_id") == USER:
    ok("B  website checkout still upserts the real user")
else:
    bad("B  website upserts are %r" % upserts)
if upserts and upserts[0].get("active") is True:
    ok("B  lifetime purchase still marked active")
else:
    bad("B  active flag is %r" % (upserts[0].get("active") if upserts else None))
if credited == [USER]:
    ok("B  affiliate credit still runs for a real user")
else:
    bad("B  credit saw %r" % credited)
if relayed:
    bad("B  a website checkout relayed attribution: %r" % relayed)
else:
    ok("B  website checkout relays nothing")

# C. Attributed AND authenticated: relay by ref, upsert by the real user id.
deliver("checkout.session.completed",
        dict(SESSION, mode="payment", client_reference_id=REF2,
             metadata={"ref": REF2, "user_id": USER, "product": "suite_lifetime"}))
if len(upserts) == 1 and upserts[0].get("user_id") == USER:
    ok("C  explicit metadata.user_id is used for the subscription")
else:
    bad("C  upserts are %r" % upserts)
if relayed and relayed[0]["ref"] == REF2:
    ok("C  and the ref — not the user id — is what gets relayed")
else:
    bad("C  relay is %r" % relayed)

# D. A malformed user_id is not a user.
for junk in ("not-a-uuid", "", "   ", "'; drop table subscriptions;--"):
    deliver("checkout.session.completed",
            dict(SESSION, mode="payment", client_reference_id=REF2,
                 metadata={"ref": REF2, "user_id": junk, "product": "suite_lifetime"}))
    if upserts:
        bad("D  malformed user_id %r created a row" % junk[:20])
        break
else:
    ok("D  malformed metadata.user_id creates no user state")

# E. A ref the receiver has never heard of still must not mint a user.
deliver("checkout.session.completed",
        dict(SESSION, mode="payment", client_reference_id="11111111-2222-3333-4444-555555555555",
             metadata={"ref": "11111111-2222-3333-4444-555555555555", "product": "suite_lifetime"}))
if not upserts:
    ok("E  an unknown ref writes no subscription row")
else:
    bad("E  unknown ref created %r" % upserts)

print("\nFALLBACK: RAW BODY UNUSABLE")
# If the raw payload cannot be parsed, the SDK object is all we have. On a
# current stripe-python that object refuses .get() but honours .to_dict() —
# which is precisely what its own error message tells you to call.
_Sub2 = type("_Sub2", (_StripeObj,), {"to_dict": lambda self: dict(self._v)})
res = deliver("customer.subscription.deleted", _Sub2(SUB), raw=b"<not json>")
if "error" in res:
    bad("fallback path still raises: " + str(res["error"])[:120])
elif churned == ["user-abc"]:
    ok("SDK object alone is still normalised (churn recorded)")
else:
    bad("fallback lost the event: churn=%r" % churned)

# And when nothing can be decoded at all, that must be loud. A 200 here would
# retire the event forever; a 5xx is retried and shows red in the dashboard.
try:
    res = deliver("customer.subscription.deleted", SUB, raw=b"<not json>")
    bad("undecodable payload was acknowledged: %r" % res)
except Exception as exc:
    if getattr(exc, "status_code", None) == 500 and "could not decode" in str(getattr(exc, "detail", "")):
        ok("an undecodable payload refuses to ack (500, Stripe retries)")
    else:
        bad("undecodable payload raised %r" % exc)


print("\nNORMALISER")
d = billing._d(_StripeObj({"a": 1}))
if d == {}:
    ok("a fully hostile object degrades to {} rather than raising")
else:
    bad("_d returned %r" % d)
for src, want in ((None, {}), ({"a": 1}, {"a": 1})):
    if billing._d(src) != want:
        bad("_d(%r) -> %r" % (src, billing._d(src)))
ok("_d passes plain dicts through and treats None as empty")


class _ToDict:
    def to_dict(self):
        return {"metadata": {"ref": "r"}}


if billing._d(_ToDict()) == {"metadata": {"ref": "r"}}:
    ok("_d falls back to .to_dict() when dict() is refused")
else:
    bad("_d ignored .to_dict()")


print("\nBAD SIGNATURE IS STILL REJECTED")


def _boom(*a, **k):
    raise ValueError("no signatures found matching the expected signature")


_Webhook_ok = _Webhook.construct_event
_Webhook.construct_event = staticmethod(_boom)
try:
    asyncio.run(billing.webhook(_Req(b"{}"), stripe_signature="bad", sb=_SB()))
    bad("a forged webhook was accepted")
except Exception as exc:
    if getattr(exc, "status_code", None) == 400:
        ok("invalid signature -> 400 (unchanged)")
    else:
        bad("invalid signature raised %r" % exc)
finally:
    _Webhook.construct_event = _Webhook_ok

print("")
if fails:
    print("%d PROBLEM%s" % (len(fails), "S" if len(fails) > 1 else ""))
    sys.exit(1)
print("subscription events handled — the 31 Aug shape no longer breaks the webhook.")
