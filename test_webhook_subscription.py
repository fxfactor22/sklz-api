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
billing._credit_referrer = lambda uid, *a, **k: credited.append(uid)
billing._partner_payment = lambda *a, **k: None
billing._partner_churn = lambda uid, *a, **k: churned.append(uid)

# capture the relay instead of making a network call
relayed = []
billing._attr_relay = lambda ref, event, product="", amount=None, currency="USD", session_id="": \
    relayed.append({"ref": ref, "event": event, "product": product,
                    "amount": amount, "session_id": session_id})

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
