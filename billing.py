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
}
PLAN_NAMES = {
    "suite_monthly": "Indicator Suite", "suite_annual": "Indicator Suite",
    "suite_lifetime": "Indicator Suite — Lifetime",
    "gpt_monthly": "TradeGPT Pro", "gpt_annual": "TradeGPT Pro",
    "bundle_monthly": "Bundle", "bundle_annual": "Bundle",
    "bundle_founder": "Bundle (Founder)",
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


# ------------------------------------------------------------------- config
@router.get("/config")
async def config(user=Depends(get_current_user),
                 sb: Client = Depends(get_supabase)) -> dict:
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
        "metadata": {"user_id": uid, "product": product},
        "allow_promotion_codes": False,
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
    if "Bundle" in (row.get("plan") or ""):
        return {"ok": True, "already": True, "plan": row.get("plan")}

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
    row = _sub_row(sb, str(user.id))
    return {"plan": row.get("plan", "Free"),
            "active": bool(row.get("active")),
            "founder": bool(row.get("founder")),
            "current_period_end": row.get("current_period_end")}


# ------------------------------------------------------------------ webhook
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
        uid = obj.get("client_reference_id") or (obj.get("metadata") or {}).get("user_id", "")
        fields = {"stripe_customer_id": obj.get("customer")}
        if obj.get("mode") == "payment":            # lifetime purchase
            product = (obj.get("metadata") or {}).get("product", "suite_lifetime")
            fields.update({"plan": PLAN_NAMES.get(product, "Indicator Suite — Lifetime"),
                           "active": True, "current_period_end": None})
        upsert(uid, fields)

    elif etype in ("customer.subscription.created", "customer.subscription.updated"):
        meta = obj.get("metadata") or {}
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

    elif etype == "customer.subscription.deleted":
        uid = (obj.get("metadata") or {}).get("user_id", "")
        upsert(uid, {"active": False})

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
                            "customer.subscription.deleted"])
        created["webhook"] = "created"
        created["webhook_secret_SAVE_TO_RAILWAY"] = wh.secret
    return created
