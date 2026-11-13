"""SKLZ Partner Program — single-level, performance-based commissions.

Model (from the Partner Program spec):
  - 50% recurring commission on the membership, per cleared payment:
      $49 Plus / Bundle -> $25     $39 founder -> $20
      $29 Core -> $14.50          $79 Pro -> $39.50
      annual plans credit 50% of the annual price when the invoice clears
      ($139 Core / $235 Plus / $379 Pro)
  - commission accrues each month the referred customer's payment CLEARS
      (recurring, sustainable — stops on churn)
  - TradeGPT and non-membership products earn NO commission
  - Partner Levels by ACTIVE customer count (not recruitment):
      Partner 0-24 | Elite 25-99 | Ambassador 100-499 | Legend 500+
  - commission wallet: earned / pending / available / paid, full ledger
  - payouts are MANUAL (owner fulfils); we track, we never move money

Explicitly NOT multi-level: no commission for recruiting affiliates. Matches
the spec's "No additional commissions simply for recruiting affiliates."
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel
from supabase import Client

from auth import get_current_user
from db import get_supabase

router = APIRouter(prefix="/api/partner", tags=["partner"])

# commission per membership price (rounded up per your call) — the two
# legacy Bundle prices keep their rounded figures; every other price pays
# exactly half of what cleared, so a $278 Core annual credits $139 at once.
COMMISSION = {49: 25.0, 39: 20.0}
COMMISSION_RATE = 0.5
DEFAULT_COMMISSION = 25.0   # only if the price is missing/zero

LEVELS = [
    ("Partner", 0, 24),
    ("Elite Partner", 25, 99),
    ("Ambassador", 100, 499),
    ("Legend", 500, 10**9),
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _level(active: int) -> dict:
    for i, (name, lo, hi) in enumerate(LEVELS):
        if lo <= active <= hi:
            nxt = LEVELS[i + 1] if i + 1 < len(LEVELS) else None
            return {"name": name, "min": lo,
                    "next": (nxt[0] if nxt else None),
                    "next_at": (nxt[1] if nxt else None),
                    "to_next": (nxt[1] - active if nxt else 0)}
    return {"name": "Partner", "min": 0, "next": "Elite Partner",
            "next_at": 25, "to_next": 25 - active}


def _commission_for(price_usd: float) -> float:
    try:
        price = float(price_usd or 0)
    except (TypeError, ValueError):
        price = 0.0
    if price <= 0:
        return DEFAULT_COMMISSION
    if abs(price - round(price)) < 1e-9 and int(round(price)) in COMMISSION:
        return COMMISSION[int(round(price))]
    return round(price * COMMISSION_RATE, 2)


# ── partner row bootstrap (reuses affiliate code if present) ──────────
def _ensure_partner(sb: Client, uid: str) -> dict:
    try:
        r = (sb.table("partners").select("*").eq("user_id", uid).execute()).data
        if r:
            return r[0]
    except Exception:
        pass
    # reuse affiliate code if the user already has one
    code = None
    try:
        a = (sb.table("affiliates").select("code").eq("user_id", uid).execute()).data
        if a:
            code = a[0]["code"]
    except Exception:
        pass
    if not code:
        import hashlib
        code = hashlib.sha256(uid.encode()).hexdigest()[:8]
    row = {"user_id": uid, "code": code, "paid_out": 0.0, "created_at": _now()}
    try:
        sb.table("partners").insert(row).execute()
    except Exception:
        pass
    return row


@router.get("/me")
async def me(user=Depends(get_current_user),
             sb: Client = Depends(get_supabase)) -> dict:
    uid = str(user.id)
    p = _ensure_partner(sb, uid)
    # customers referred by me
    try:
        custs = (sb.table("partner_customers").select("*")
                 .eq("partner_id", uid).order("created_at", desc=True).execute()).data or []
    except Exception:
        custs = []
    active = sum(1 for c in custs if c.get("active"))
    total = len(custs)
    churned = total - active
    mrr_commission = sum(_commission_for(c.get("price", 49))
                         for c in custs if c.get("active"))
    # ledger
    try:
        ledger = (sb.table("partner_commissions").select("*")
                  .eq("partner_id", uid).order("created_at", desc=True)
                  .limit(2000).execute()).data or []
    except Exception:
        ledger = []
    earned = sum(e.get("amount", 0) for e in ledger)
    pending = sum(e.get("amount", 0) for e in ledger if e.get("status") == "pending")
    cleared = sum(e.get("amount", 0) for e in ledger if e.get("status") == "cleared")
    paid_out = p.get("paid_out", 0.0) or 0.0
    available = round(cleared - paid_out, 2)
    lvl = _level(active)
    by_cust: dict[str, float] = {}
    for e in ledger:
        by_cust[e.get("customer_id")] = by_cust.get(e.get("customer_id"), 0.0) + float(e.get("amount") or 0)

    def _label(c: dict) -> str:
        em = c.get("email") or "customer"
        return em if "@" not in em else em[:2] + "•••"      # imported rows carry a name, not an email

    def _comm(c: dict) -> float:
        cid = c.get("customer_id") or ""
        if cid.startswith("skillsart:"):
            return round(by_cust.get(cid, 0.0), 2)
        return _commission_for(c.get("price", 49))

    return {
        "code": p["code"],
        "link": f"{os.environ.get('SITE_URL','https://www.sklzlabs.com')}/?ref={p['code']}",
        "level": lvl,
        "customers": {"active": active, "churned": churned, "total": total},
        "mrr_commission": round(mrr_commission, 2),
        "wallet": {"earned": round(earned, 2), "pending": round(pending, 2),
                   "available": max(0.0, available), "paid_out": round(paid_out, 2)},
        "recent_commissions": ledger[:20],
        "customer_list": [{
            "masked": _label(c),
            "active": c.get("active"), "since": c.get("created_at"),
            "months": c.get("months_active", 0),
            "commission": _comm(c),
            "source": "skillsart" if (c.get("customer_id") or "").startswith("skillsart:") else "sklz",
        } for c in custs[:400]],
    }


# ── billing hook: a referred customer's monthly payment cleared ──────
class Payment(BaseModel):
    customer_user_id: str
    price: float = 49.0
    active: bool = True
    email: str = ""


@router.post("/payment")
async def record_payment(body: Payment,
                         authorization: str = Header(default=""),
                         sb: Client = Depends(get_supabase)) -> dict:
    """Called by the billing webhook on each cleared membership payment.
    Credits the referrer their monthly commission. Gated by INTERNAL_KEY."""
    expected = os.environ.get("INTERNAL_KEY", "") or os.environ.get("BOT_INGEST_KEY", "")
    token = authorization.replace("Bearer ", "").strip()
    if not expected or token != expected:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "unauthorized")

    # who referred this customer?
    try:
        ref = (sb.table("referrals").select("referrer_id")
               .eq("referred_id", body.customer_user_id).execute()).data
    except Exception:
        ref = None
    if not ref:
        return {"ok": True, "commission": 0, "note": "customer not referred"}
    partner_id = ref[0]["referrer_id"]
    _ensure_partner(sb, partner_id)

    # upsert customer record (track active + months)
    try:
        existing = (sb.table("partner_customers").select("*")
                    .eq("partner_id", partner_id)
                    .eq("customer_id", body.customer_user_id).execute()).data
    except Exception:
        existing = None
    months = 1
    if existing:
        months = (existing[0].get("months_active", 0) or 0) + (1 if body.active else 0)
        sb.table("partner_customers").update(
            {"active": body.active, "months_active": months, "price": body.price,
             "updated_at": _now()}).eq("id", existing[0]["id"]).execute()
    else:
        sb.table("partner_customers").insert(
            {"partner_id": partner_id, "customer_id": body.customer_user_id,
             "email": body.email, "active": body.active, "price": body.price,
             "months_active": 1, "created_at": _now()}).execute()

    if not body.active:
        return {"ok": True, "commission": 0, "note": "inactive — no commission"}

    amount = _commission_for(body.price)
    # retention bonus: small extra at 6 and 12 months (per spec)
    bonus = 0.0
    if months == 6:
        bonus = 10.0
    elif months == 12:
        bonus = 25.0
    sb.table("partner_commissions").insert({
        "partner_id": partner_id, "customer_id": body.customer_user_id,
        "amount": amount + bonus, "base": amount, "bonus": bonus,
        "month_index": months, "status": "cleared", "created_at": _now(),
    }).execute()
    return {"ok": True, "commission": amount, "bonus": bonus, "month": months}


@router.post("/churn")
async def record_churn(customer_user_id: str,
                       authorization: str = Header(default=""),
                       sb: Client = Depends(get_supabase)) -> dict:
    """Mark a referred customer inactive (subscription cancelled)."""
    expected = os.environ.get("INTERNAL_KEY", "") or os.environ.get("BOT_INGEST_KEY", "")
    if authorization.replace("Bearer ", "").strip() != expected or not expected:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "unauthorized")
    try:
        sb.table("partner_customers").update({"active": False, "updated_at": _now()}) \
            .eq("customer_id", customer_user_id).execute()
    except Exception:
        pass
    return {"ok": True}


@router.post("/request-payout")
async def request_payout(user=Depends(get_current_user),
                         sb: Client = Depends(get_supabase)) -> dict:
    """Partner requests a payout of available balance. Flags for MANUAL fulfilment
    by the owner — we never move money. Owner marks paid via /admin/mark-paid."""
    uid = str(user.id)
    data = await me(user=user, sb=sb)
    avail = data["wallet"]["available"]
    if avail <= 0:
        return {"ok": False, "note": "no available balance"}
    try:
        sb.table("partner_payouts").insert(
            {"partner_id": uid, "amount": avail, "status": "requested",
             "created_at": _now()}).execute()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR,
                            f"could not request payout: {exc}") from exc
    return {"ok": True, "requested": avail,
            "note": "Payout requested. SKLZ will process it manually and mark it paid."}


# ── Skillsart import: those clients are SKLZ Labs clients now ────────
# The owner's referral book from skillsart.com (300 clients, 375 orders)
# is folded INTO his SKLZ partner record: every client becomes a
# partner_customers row (customer_id "skillsart:<id>"), every order a
# partner_commissions row, and the commissions — already settled on
# Skillsart — are counted as earned AND paid out, so lifetime figures are
# true and nothing becomes withdrawable twice. Idempotent: rerunning
# skips anything already imported. Owner only.
import json as _json
from pathlib import Path as _Path
from tv_access import _require_admin as _owner_only

_IMPORT = _Path(__file__).parent / "data" / "skillsart_import.json"


def _legacy_active(c: dict, snapshot: str) -> bool:
    """Annual pass within 365 days, monthly within 60 days -> active."""
    last = c.get("last_order")
    if not last:
        return False
    from datetime import date as _d
    days = (_d.fromisoformat(snapshot) - _d.fromisoformat(last)).days
    annual = (c.get("sales") or 0) >= 500 and (c.get("orders") or 0) >= 1
    return days <= (365 if annual else 60)


def import_legacy(sb: Client, uid: str, snap: dict) -> dict:
    """Pure-ish worker so a test can drive it with a fake client."""
    p = _ensure_partner(sb, uid)
    existing = (sb.table("partner_customers").select("customer_id")
                .eq("partner_id", uid).like("customer_id", "skillsart:%").execute()).data or []
    have = {e["customer_id"] for e in existing}
    snapshot = snap.get("snapshot_at") or _now()[:10]
    by_name = {}
    for o in snap["orders"]:
        by_name.setdefault(o["referral"], []).append(o)
    added_c = 0
    name_to_cid: dict[str, str] = {}
    for c in snap["clients"]:
        cid = f"skillsart:{c['id']}"
        name_to_cid.setdefault(c["name"], cid)
        if cid in have:
            continue
        orders = by_name.get(c["name"], [])
        sb.table("partner_customers").insert({
            "partner_id": uid, "customer_id": cid,
            "email": c["name"],                       # display name; no email was exported
            "active": _legacy_active(c, snapshot),
            "price": float(orders[-1]["total"]) if orders else 0.0,
            "months_active": len(orders),
            "created_at": (c.get("first_order") or snapshot) + "T00:00:00+00:00",
        }).execute()
        added_c += 1
    done = (sb.table("partner_commissions").select("customer_id")
            .eq("partner_id", uid).like("customer_id", "skillsart:%").limit(1).execute()).data or []
    added_o, total = 0, 0.0
    if not done:
        for o in snap["orders"]:
            cid = name_to_cid.get(o["referral"], f"skillsart:name:{o['referral']}")
            amt = round(float(o["commission_fixed"]) + float(o["total"]) * float(o["commission_pct"]) / 100, 2)
            sb.table("partner_commissions").insert({
                "partner_id": uid, "customer_id": cid, "amount": amt, "base": amt, "bonus": 0.0,
                "month_index": 1, "status": "cleared",
                "created_at": o["date"] + "T00:00:00+00:00",
            }).execute()
            added_o += 1
            total += amt
        # settled on Skillsart already: paid out, not withdrawable again
        paid = float(p.get("paid_out") or 0.0) + round(total, 2)
        sb.table("partners").update({"paid_out": round(paid, 2)}).eq("user_id", uid).execute()
    return {"ok": True, "customers_added": added_c, "orders_added": added_o,
            "commission_added": round(total, 2), "already_imported": bool(done) and not added_c}


@router.post("/admin/import-skillsart")
async def admin_import_skillsart(user=Depends(get_current_user),
                                 sb: Client = Depends(get_supabase)) -> dict:
    _owner_only(user)
    if not _IMPORT.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "data/skillsart_import.json missing")
    with _IMPORT.open(encoding="utf-8") as fh:
        snap = _json.load(fh)
    try:
        return import_legacy(sb, str(user.id), snap)
    except Exception as exc:  # noqa: BLE001 — the owner needs the real reason, not a 500
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:600]}
