"""Stripe billing — checkout, promo funnel, upsell, webhook sync.

THE FUNNEL (fully disclosed, by design):
  * Indicator Suite: $7 first month (a one-time $22-off coupon on the $29/mo
    price — Stripe Checkout itself renders "$7.00 due today, then $29.00/mo").
  * Post-purchase upsell: "+$7 today -> Bundle, renews $49/mo". One click,
    card on file: we swap the subscription to the bundle price for NEXT
    period (no proration) and invoice $7 immediately for the remainder of
    this one. The customer pays exactly what the button said.
  * Founder pricing: first 100 bundle subscriptions lock $39/mo for life,
    enforced server-side by counting founder-flagged rows.

COMPLIANCE GUARDRAILS (non-negotiable):
  renewal price on every checkout (Stripe shows it), no pre-checked boxes,
  cancel-anytime via the Stripe customer portal, refunds handled in Stripe.
  Hidden charges are how merchants lose their Stripe account; everything
  here is on the button.

ENDPOINTS
  GET  /api/billing/config          prices + founder counter (pricing page)
  POST /api/billing/checkout        {product} -> Stripe Checkout URL
  POST /api/billing/upsell          one-click suite -> bundle upgrade
  GET  /api/billing/portal          Stripe customer portal URL
  GET  /api/billing/status          current subscription (dashboard)
  POST /api/billing/webhook         Stripe -> subscriptions table
  POST /api/billing/admin/setup     idempotent product/price/coupon/webhook
                                    bootstrap (admin only, run once)
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel
from supabase import Client

from auth import get_current_user
from db import get_supabase
from tv_access import _require_admin

router = APIRouter(prefix="/api/billing", tags=["billing"])

SITE = os.environ.get("SITE_URL", "https://www.sklzlabs.com")
FOUNDER_CAP = 100

# Attribution refs are uuids and nothing else. Compiled once; used to reject
# anything else before it reaches Stripe.
_UUID_RE = __import__("re").compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

# lookup_key -> (product name, unit_amount cents, interval|None for one-time)
CATALOG = {
    "suite_monthly":  ("SKLZ Indicator Suite", 2900, "month"),
    "suite_annual":   ("SKLZ Indicator Suite", 29000, "year"),
    "suite_lifetime": ("SKLZ Indicator Suite", 34900, None),
    "gpt_monthly":    ("TradeGPT Pro", 2900, "month"),
    "gpt_annual":     ("TradeGPT Pro", 29000, "year"),
    "bundle_monthly": ("SKLZ Bundle", 4900, "month"),
    "bundle_annual":  ("SKLZ Bundle", 49000, "year"),
    "bundle_founder": ("SKLZ Bundle", 3900, "month"),
    "copy_basic_monthly":  ("SKLZ Core", 2900, "month"),
    "copy_crypto_monthly": ("SKLZ Plus", 4900, "month"),
    "copy_pro_monthly":    ("SKLZ Pro", 7900, "month"),
    "copy_basic_annual":   ("SKLZ Core", 27800, "year"),
    "copy_crypto_annual":  ("SKLZ Plus", 47000, "year"),
    "copy_pro_annual":     ("SKLZ Pro", 75800, "year"),
}
PLAN_NAMES = {
    "suite_monthly": "Indicator Suite", "suite_annual": "Indicator Suite",
    "suite_lifetime": "Indicator Suite — Lifetime",
    "gpt_monthly": "TradeGPT Pro", "gpt_annual": "TradeGPT Pro",
    "bundle_monthly": "Bundle", "bundle_annual": "Bundle",
    "bundle_founder": "Bundle (Founder)",
    "copy_basic_monthly": "SKLZ Core",
    "copy_crypto_monthly": "SKLZ Plus",
    "copy_pro_monthly": "SKLZ Pro",
    "copy_basic_annual": "SKLZ Core (Annual)",
    "copy_crypto_annual": "SKLZ Plus (Annual)",
    "copy_pro_annual": "SKLZ Pro (Annual)",
}
INTRO_COUPON_ID = "sklz-first-month-7"      # $22 off once -> $7 first month


def _stripe():
    key = os.environ.get("STRIPE_SECRET_KEY", "")
    if not key:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "billing not configured")
    import stripe
    stripe.api_key = key
    return stripe


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _founder_taken(sb: Client) -> int:
    try:
        rows = (sb.table("subscriptions").select("user_id,founder,active")
                  .eq("founder", True).execute()).data or []
        return len([r for r in rows if r.get("active")])
    except Exception:
        return FOUNDER_CAP        # fail closed: never oversell founder slots


def _price_id(stripe, lookup_key: str) -> str:
    try:
        res = stripe.Price.list(lookup_keys=[lookup_key], limit=1)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_502_BAD_GATEWAY,
                            f"Stripe rejected the API key or call: {exc}") from exc
    if not res.data:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR,
                            f"price {lookup_key} missing — run admin/setup first")
    return res.data[0].id


def _sub_row(sb: Client, uid: str) -> dict:
    try:
        r = sb.table("subscriptions").select("*").eq("user_id", uid).execute()
        return (r.data or [{}])[0]
    except Exception:
        return {}


class CheckoutIn(BaseModel):
    product: str
    # An OPAQUE attribution id minted by whoever sent the customer here — the
    # Telegram bot mints a uuid per checkout. It is echoed onto the Stripe
    # session as client_reference_id and metadata.ref, and relayed back on
    # payment so the sender can join a payment to the visit that produced it.
    #
    # It carries no meaning to us and must carry none to Stripe: never put a
    # chat id, an email or a phone number in here. A uuid in Stripe metadata
    # is a row number; an identifier is a person.
    ref: str | None = None


# ------------------------------------------------------------------- config
@router.get("/config")
async def config(sb: Client = Depends(get_supabase)) -> dict:
    # public: the pricing page shows the founder counter to logged-out visitors
    taken = _founder_taken(sb)
    return {
        "prices": {
            "suite_monthly": {"intro": 7, "renews": 29},
            "suite_annual": {"amount": 290},
            "suite_lifetime": {"amount": 349},
            "gpt_monthly": {"amount": 29},
            "gpt_annual": {"amount": 290},
            "bundle_monthly": {"amount": 49},
            "bundle_annual": {"amount": 490},
            "bundle_founder": {"amount": 39},
            "copy_basic_monthly": {"amount": 29},
            "copy_crypto_monthly": {"amount": 49},
            "copy_pro_monthly": {"amount": 79},
            "copy_basic_annual": {"amount": 278},
            "copy_crypto_annual": {"amount": 470},
            "copy_pro_annual": {"amount": 758},
        },
        "founder_remaining": max(FOUNDER_CAP - taken, 0),
        "guarantee_days": 7,
    }


# ----------------------------------------------------------------- checkout
@router.post("/checkout")
async def checkout(payload: CheckoutIn, user=Depends(get_current_user),
                   sb: Client = Depends(get_supabase)) -> dict:
    product = payload.product
    if product == "bundle_monthly" and _founder_taken(sb) < FOUNDER_CAP:
        product = "bundle_founder"          # founder slots auto-apply
    if product not in CATALOG:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "unknown product")

    # GUARD: an active subscriber must UPGRADE, never stack a second
    # subscription — double-billing is how disputes are born.
    row = _sub_row(sb, str(user.id))
    if row.get("active") and row.get("stripe_subscription_id"):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "You already have an active plan (" + str(row.get("plan")) +
            "). Use the Upgrade button on the Billing page, or manage your "
            "plan from the billing portal — this prevents double charges.")

    stripe = _stripe()
    _, _, interval = CATALOG[product]
    uid = str(user.id)
    email = getattr(user, "email", None)

    params: dict = {
        "mode": "subscription" if interval else "payment",
        "line_items": [{"price": _price_id(stripe, product), "quantity": 1}],
        "success_url": f"{SITE}/success.html?p={product}",
        "cancel_url": f"{SITE}/pricing.html",
        "client_reference_id": uid,
        "metadata": {"user_id": uid, "product": product,
                     **({"ref": payload.ref} if payload.ref and _UUID_RE.match(payload.ref) else {})},
        # NOTE: never add allow_promotion_codes here — Stripe rejects any
        # session carrying both it and `discounts` (the $7 intro coupon).
        # Promotion-code entry stays off by default anyway.
    }
    if email:
        params["customer_email"] = email
    if interval:
        params["subscription_data"] = {"metadata": {
            "user_id": uid, "product": product,
            "founder": "true" if product == "bundle_founder" else "false"}}
    if product == "suite_monthly":
        params["discounts"] = [{"coupon": INTRO_COUPON_ID}]   # $7 first month
    if not interval:
        params["payment_intent_data"] = {"metadata": {
            "user_id": uid, "product": product}}

    try:
        session = stripe.checkout.Session.create(**params)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_502_BAD_GATEWAY,
                            f"Stripe checkout failed: {exc}") from exc
    return {"url": session.url}


# ------------------------------------------------------------------- upsell
@router.post("/upsell")
async def upsell(user=Depends(get_current_user),
                 sb: Client = Depends(get_supabase)) -> dict:
    """Suite monthly -> Bundle. $7 invoiced now; renews at the bundle price
    (founder $39 if slots remain, else $49). Exactly what the button says."""
    stripe = _stripe()
    uid = str(user.id)
    row = _sub_row(sb, uid)
    sub_id = row.get("stripe_subscription_id")
    if not sub_id or not row.get("active"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "no active subscription to upgrade")
    plan = row.get("plan") or ""
    if "Bundle" in plan:
        return {"ok": True, "already": True, "plan": plan}
    if "Lifetime" in plan or "annual" in plan.lower():
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "Annual and lifetime plans are managed from the "
                            "billing portal — contact support to switch.")

    target = ("bundle_founder" if _founder_taken(sb) < FOUNDER_CAP
              else "bundle_monthly")
    sub = stripe.Subscription.retrieve(sub_id)
    item_id = sub["items"]["data"][0]["id"]

    stripe.Subscription.modify(
        sub_id,
        items=[{"id": item_id, "price": _price_id(stripe, target)}],
        proration_behavior="none",
        metadata={"user_id": uid, "product": target,
                  "founder": "true" if target == "bundle_founder" else "false"},
    )
    stripe.InvoiceItem.create(
        customer=sub["customer"], amount=700, currency="usd",
        description="Bundle upgrade — remainder of current period")
    inv = stripe.Invoice.create(customer=sub["customer"], auto_advance=True)
    stripe.Invoice.pay(inv["id"])

    renews = 39 if target == "bundle_founder" else 49
    try:
        sb.table("subscriptions").upsert({
            "user_id": uid, "plan": PLAN_NAMES[target], "active": True,
            "founder": target == "bundle_founder",
            "stripe_subscription_id": sub_id,
            "updated_at": _now()}, on_conflict="user_id").execute()
    except Exception:
        pass          # webhook will reconcile
    return {"ok": True, "charged_today": 7, "renews_at": renews,
            "plan": PLAN_NAMES[target]}


# ------------------------------------------------------------------- portal
@router.get("/portal")
async def portal(user=Depends(get_current_user),
                 sb: Client = Depends(get_supabase)) -> dict:
    stripe = _stripe()
    cus = _sub_row(sb, str(user.id)).get("stripe_customer_id")
    if not cus:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "no billing account yet")
    s = stripe.billing_portal.Session.create(
        customer=cus, return_url=f"{SITE}/dashboard.html")
    return {"url": s.url}


# ------------------------------------------------------------------- status
@router.get("/status")
async def status_ep(user=Depends(get_current_user),
                    sb: Client = Depends(get_supabase)) -> dict:
    # owner/admin bypass: full Bundle, always active
    import os as _os
    admins = {e.strip().lower() for e in
              _os.environ.get("ADMIN_EMAILS", "fxfactor24@gmail.com").split(",")}
    if (getattr(user, "email", "") or "").lower() in admins:
        return {"plan": "Bundle (Founder)", "active": True, "founder": True,
                "current_period_end": None, "owner": True}
    row = _sub_row(sb, str(user.id))
    return {"plan": row.get("plan", "Free"),
            "active": bool(row.get("active")),
            "founder": bool(row.get("founder")),
            "current_period_end": row.get("current_period_end")}


# ------------------------------------------------------------------ webhook
def _partner_payment(uid: str, price: float, active: bool, email: str = "") -> None:
    """Tell the partner engine a referred customer paid — recurring commission."""
    import os, urllib.request, json as _json
    try:
        key = os.environ.get("INTERNAL_KEY", "") or os.environ.get("BOT_INGEST_KEY", "")
        base = os.environ.get("SELF_API_URL", "https://api.sklzlabs.com")
        if not key or not uid:
            return
        body = _json.dumps({"customer_user_id": uid, "price": price,
                            "active": active, "email": email}).encode()
        req = urllib.request.Request(base + "/api/partner/payment", data=body,
                                     headers={"Authorization": "Bearer " + key,
                                              "Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=8)
    except Exception:
        pass


def _partner_churn(uid: str) -> None:
    import os, urllib.request
    try:
        key = os.environ.get("INTERNAL_KEY", "") or os.environ.get("BOT_INGEST_KEY", "")
        base = os.environ.get("SELF_API_URL", "https://api.sklzlabs.com")
        if not key or not uid:
            return
        req = urllib.request.Request(base + "/api/partner/churn?customer_user_id=" + uid,
                                     data=b"", method="POST",
                                     headers={"Authorization": "Bearer " + key})
        urllib.request.urlopen(req, timeout=8)
    except Exception:
        pass


def _credit_referrer(uid: str) -> None:
    """If this user was referred, credit the referrer via the affiliate endpoint."""
    import os, urllib.request
    try:
        key = os.environ.get("INTERNAL_KEY", "") or os.environ.get("BOT_INGEST_KEY", "")
        base = os.environ.get("SELF_API_URL", "https://api.sklzlabs.com")
        if not key or not uid:
            return
        req = urllib.request.Request(
            base + "/api/affiliate/convert?referred_user_id=" + uid,
            data=b"", method="POST",
            headers={"Authorization": "Bearer " + key})
        urllib.request.urlopen(req, timeout=8)
    except Exception:
        pass



@router.post("/webhook")
async def webhook(request: Request,
                  stripe_signature: str = Header(default=""),
                  sb: Client = Depends(get_supabase)) -> dict:
    stripe = _stripe()
    secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
    payload = await request.body()
    try:
        event = stripe.Webhook.construct_event(payload, stripe_signature, secret)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"bad signature: {exc}") from exc

    etype = event["type"]
    obj = event["data"]["object"]

    # Stripe's SDK returns StripeObject, which supports obj["k"] but NOT
    # obj.get("k"). Convert to a plain dict so ordinary dict access works.
    try:
        obj = dict(obj)
    except Exception:
        try:
            obj = obj.to_dict_recursive()
        except Exception:
            pass

    try:
        return await _handle_event(etype, obj, sb)
    except Exception as exc:  # noqa: BLE001
        import sys as _s, traceback as _tb
        print(f"[stripe-webhook] {etype} FAILED: {type(exc).__name__}: {exc}",
              file=_s.stderr, flush=True)
        _tb.print_exc(file=_s.stderr)
        # acknowledge so Stripe stops retrying; the error is ours, not theirs,
        # and the log above names it.
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:200]}",
                "event": etype}


# ------------------------------------------------- attribution relay (out)
def _attr_relay(ref: str, event: str, product: str = "",
                amount: float | None = None, currency: str = "USD",
                session_id: str = "") -> None:
    """Tell whoever sent this customer that the payment cleared.

    Stripe is payment truth and stays payment truth: this fires only from
    inside a signature-verified Stripe webhook, never from a redirect, a
    success URL or a button. The receiver must be able to prove the same, so
    the body is HMAC-signed under a shared secret and stamped with a
    timestamp — an unsigned POST to that endpoint could otherwise mark any
    customer paid.

    Failure here must never fail the webhook. If the relay is down we have
    still taken the money and Stripe still has the record; what is lost is a
    row in somebody's funnel report, and that is not worth a 500 that makes
    Stripe retry a payment we already processed.

    Unconfigured (no URL or no secret) means "nobody is listening", which is
    the normal state for a checkout that did not come from a bot.
    """
    url = os.environ.get("ATTRIBUTION_WEBHOOK_URL", "").strip()
    secret = os.environ.get("ATTRIBUTION_WEBHOOK_SECRET", "").strip()
    if not (ref and url and secret):
        return
    try:
        import hmac as _hmac, hashlib as _hl, json as _json, time as _time
        import urllib.request as _rq
        body = _json.dumps({"ref": ref, "event": event, "product": product,
                            "amount": amount, "currency": currency,
                            "session_id": session_id},
                           separators=(",", ":")).encode()
        ts = str(int(_time.time()))
        mac = _hmac.new(secret.encode(), (ts + ".").encode() + body, _hl.sha256).hexdigest()
        req = _rq.Request(url, data=body, method="POST", headers={
            "content-type": "application/json",
            "x-sklz-signature": f"t={ts},v1={mac}"})
        with _rq.urlopen(req, timeout=6) as r:
            r.read(1)
    except Exception as exc:  # noqa: BLE001
        import sys as _s
        print(f"[attr-relay] {event} ref={ref} failed: {exc}", file=_s.stderr, flush=True)


def _d(v):
    """Stripe objects are dict-like but lack .get(); normalise them."""
    if isinstance(v, dict):
        return v
    try:
        return dict(v)
    except Exception:
        return {}


async def _handle_event(etype: str, obj: dict, sb: Client) -> dict:

    def upsert(uid: str, fields: dict) -> None:
        if not uid:
            return
        try:
            sb.table("subscriptions").upsert(
                {"user_id": uid, "updated_at": _now(), **fields},
                on_conflict="user_id").execute()
        except Exception:
            pass

    if etype == "checkout.session.completed":
        uid = obj.get("client_reference_id") or _d(obj.get("metadata")).get("user_id", "")
        fields = {"stripe_customer_id": obj.get("customer")}
        if obj.get("mode") == "payment":            # lifetime purchase
            product = _d(obj.get("metadata")).get("product", "suite_lifetime")
            fields.update({"plan": PLAN_NAMES.get(product, "Indicator Suite — Lifetime"),
                           "active": True, "current_period_end": None})
        upsert(uid, fields)
        _credit_referrer(uid)
        meta = _d(obj.get("metadata"))
        _attr_relay(meta.get("ref", "") or (obj.get("client_reference_id") or ""),
                    "checkout.session.completed",
                    meta.get("product", ""),
                    (obj.get("amount_total") or 0) / 100.0,
                    (obj.get("currency") or "usd").upper(),
                    obj.get("id") or "")

    elif etype in ("customer.subscription.created", "customer.subscription.updated"):
        meta = _d(obj.get("metadata"))
        uid = meta.get("user_id", "")
        product = meta.get("product", "")
        active = obj.get("status") in ("active", "trialing")
        end = obj.get("current_period_end")
        end_iso = (datetime.fromtimestamp(end, tz=timezone.utc).isoformat()
                   if end else None)
        upsert(uid, {"plan": PLAN_NAMES.get(product, "Subscription"),
                     "active": active,
                     "founder": meta.get("founder") == "true",
                     "stripe_customer_id": obj.get("customer"),
                     "stripe_subscription_id": obj.get("id"),
                     "current_period_end": end_iso})
        if active:
            _credit_referrer(uid)
            # Renewals attribute too — a subscription that came from a campaign
            # is worth its lifetime to that campaign, not just its first month.
            _attr_relay(meta.get("ref", ""), etype, product, None,
                        (obj.get("currency") or "usd").upper(), obj.get("id") or "")

    elif etype == "invoice.paid":
        # recurring monthly payment cleared -> partner commission (if referred)
        cust = obj.get("customer")
        amount = (obj.get("amount_paid") or 0) / 100.0
        uid = ""
        try:
            row = (sb.table("subscriptions").select("user_id,plan")
                   .eq("stripe_customer_id", cust).execute()).data
            if row:
                uid = row[0]["user_id"]
                plan = (row[0].get("plan") or "").lower()
        except Exception:
            plan = ""
        # only membership/bundle earns commission; TradeGPT and others do not
        if uid and ("bundle" in plan or "membership" in plan):
            _partner_payment(uid, amount if amount in (39.0, 49.0) else 49.0, True)

    elif etype == "customer.subscription.deleted":
        meta = _d(obj.get("metadata"))
        uid = meta.get("user_id", "")
        upsert(uid, {"active": False})
        _partner_churn(uid)
        _attr_relay(meta.get("ref", ""), "customer.subscription.deleted",
                    meta.get("product", ""))

    return {"received": True}


# -------------------------------------------------------------- admin setup
@router.post("/admin/setup")
async def admin_setup(user=Depends(get_current_user),
                      sb: Client = Depends(get_supabase)) -> dict:
    """Idempotent bootstrap: products, prices (by lookup_key), the $7 intro
    coupon, and the webhook endpoint. Safe to run repeatedly."""
    _require_admin(user)
    key_prefix = os.environ.get("STRIPE_SECRET_KEY", "")[:8]
    if not key_prefix.startswith("sk_") and not key_prefix.startswith("rk_"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"STRIPE_SECRET_KEY looks wrong (starts '{key_prefix}…'). It must "
            "be the SECRET key (sk_live_… / sk_test_…) from Stripe -> "
            "Developers -> API keys — not the publishable pk_ key.")
    stripe = _stripe()
    created: dict = {"prices": [], "existing": [],
                     "mode": "TEST" if "test" in key_prefix else "LIVE"}

    try:
        existing_products = stripe.Product.list(limit=100, active=True).data
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_502_BAD_GATEWAY,
                            f"Stripe rejected the key: {exc}") from exc
    by_name = {p["name"]: p["id"] for p in existing_products}

    products: dict[str, str] = {}
    for key, (pname, amount, interval) in CATALOG.items():
        res = stripe.Price.list(lookup_keys=[key], limit=1)
        if res.data:
            created["existing"].append(key)
            continue
        if pname not in products:
            products[pname] = (by_name.get(pname)
                               or stripe.Product.create(name=pname).id)
        p: dict = {"product": products[pname], "currency": "usd",
                   "unit_amount": amount, "lookup_key": key}
        if interval:
            p["recurring"] = {"interval": interval}
        stripe.Price.create(**p)
        created["prices"].append(key)

    try:
        stripe.Coupon.retrieve(INTRO_COUPON_ID)
        created["coupon"] = "existing"
    except Exception:
        stripe.Coupon.create(id=INTRO_COUPON_ID, amount_off=2200,
                             currency="usd", duration="once",
                             name="First month $7")
        created["coupon"] = "created"

    wh_url = "https://api.sklzlabs.com/api/billing/webhook"
    hooks = stripe.WebhookEndpoint.list(limit=20)
    existing = [h for h in hooks.data if h.url == wh_url]
    if existing:
        created["webhook"] = "existing — secret unchanged"
    else:
        wh = stripe.WebhookEndpoint.create(
            url=wh_url,
            enabled_events=["checkout.session.completed",
                            "customer.subscription.created",
                            "customer.subscription.updated",
                            "customer.subscription.deleted",
                            "invoice.paid"])
        created["webhook"] = "created"
        created["webhook_secret_SAVE_TO_RAILWAY"] = wh.secret
    return created


# ------------------------------------------------ guest checkout (pay first)
@router.post("/checkout-public")
async def checkout_public(payload: CheckoutIn,
                          sb: Client = Depends(get_supabase)) -> dict:
    """No auth: a visitor pays first; Stripe collects their email; the claim
    page then turns the paid session into an account."""
    product = payload.product
    if product == "bundle_monthly" and _founder_taken(sb) < FOUNDER_CAP:
        product = "bundle_founder"
    if product not in CATALOG:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "unknown product")

    stripe = _stripe()
    _, _, interval = CATALOG[product]
    params: dict = {
        "mode": "subscription" if interval else "payment",
        "line_items": [{"price": _price_id(stripe, product), "quantity": 1}],
        "success_url": f"{SITE}/claim.html?sid={{CHECKOUT_SESSION_ID}}&p={product}",
        "cancel_url": f"{SITE}/pricing.html",
        "metadata": {"product": product, "guest": "true"},
    }
    # Attribution. A ref is validated as a uuid before it is echoed anywhere:
    # this value is supplied by a caller and lands in Stripe metadata, so it
    # is the one field on this endpoint an outsider controls. A uuid shape is
    # all we ever send, which keeps arbitrary text out of the payment record.
    ref = (payload.ref or "").strip()
    if ref:
        if not _UUID_RE.match(ref):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "ref must be a uuid")
        params["client_reference_id"] = ref
        params["metadata"]["ref"] = ref
    if interval:
        sub_meta = {"product": product,
                    "founder": "true" if product == "bundle_founder" else "false"}
        # The ref has to ride on the SUBSCRIPTION too. checkout.session.completed
        # fires once; every renewal afterwards arrives as a subscription or
        # invoice event that has never seen the checkout session, so a ref that
        # lives only on the session cannot attribute month two.
        if ref:
            sub_meta["ref"] = ref
        params["subscription_data"] = {"metadata": sub_meta}
    else:
        pi_meta = {"product": product}
        if ref:
            pi_meta["ref"] = ref
        params["payment_intent_data"] = {"metadata": pi_meta}
    try:
        session = stripe.checkout.Session.create(**params)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_502_BAD_GATEWAY,
                            f"Stripe checkout failed: {exc}") from exc
    return {"url": session.url}


class ClaimIn(BaseModel):
    session_id: str
    password: str
    display_name: str = ""


@router.post("/claim")
async def claim(payload: ClaimIn,
                sb: Client = Depends(get_supabase)) -> dict:
    """Turn a PAID guest checkout session into an account + linked plan."""
    stripe = _stripe()
    try:
        sess = stripe.checkout.Session.retrieve(payload.session_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"unknown checkout session: {exc}") from exc
    if sess.get("payment_status") != "paid":
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "this checkout session is not paid")
    email = ((sess.get("customer_details") or {}).get("email")
             or sess.get("customer_email") or "").lower()
    if not email:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "no email on the checkout session")
    product = (sess.get("metadata") or {}).get("product", "suite_monthly")

    # create the account — or attach to an existing one with the same email
    uid = None
    confirmation_required = False
    existing_account = False
    try:
        res = sb.auth.sign_up({
            "email": email, "password": payload.password,
            "options": {"data": {"display_name": payload.display_name}},
        })
        uid = str(res.user.id) if res.user else None
        confirmation_required = res.session is None
    except Exception:
        existing_account = True
        try:
            users = sb.auth.admin.list_users()
            for u in users:
                if (getattr(u, "email", "") or "").lower() == email:
                    uid = str(u.id)
                    break
        except Exception:
            uid = None
    if not uid:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Payment received, but the account could not be created or "
            "matched. Log in with this email if you already have an account, "
            "or contact support — your purchase is safe in Stripe.")

    # link the Stripe objects to the user so renewals sync automatically
    sub_id = sess.get("subscription")
    end_iso = None
    if sub_id:
        try:
            sub = stripe.Subscription.retrieve(sub_id)
            stripe.Subscription.modify(sub_id, metadata={
                "user_id": uid, "product": product,
                "founder": (sub.get("metadata") or {}).get("founder", "false")})
            end = sub.get("current_period_end")
            if end:
                end_iso = datetime.fromtimestamp(end, tz=timezone.utc).isoformat()
        except Exception:
            pass

    try:
        sb.table("subscriptions").upsert({
            "user_id": uid,
            "plan": PLAN_NAMES.get(product, "Subscription"),
            "active": True,
            "founder": product == "bundle_founder",
            "stripe_customer_id": sess.get("customer"),
            "stripe_subscription_id": sub_id,
            "current_period_end": end_iso,
            "updated_at": _now()}, on_conflict="user_id").execute()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR,
                            f"account created but plan link failed: {exc}") from exc

    return {"ok": True, "email": email,
            "plan": PLAN_NAMES.get(product, "Subscription"),
            "existing_account": existing_account,
            "email_confirmation_required": confirmation_required}
