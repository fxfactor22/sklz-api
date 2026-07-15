"""Account state for the dashboard: subscription, affiliate, news.

Honest defaults: nothing is invented. If a user has no subscription, the API
says so plainly and the UI shows a real "Free plan" state rather than a fake
countdown. Affiliate ranks are computed from actual referrals — zero referrals
means rank "Starter" and 0 clients, not a flattering placeholder.
"""
from __future__ import annotations

import hashlib
import os
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from supabase import Client

from auth import get_current_user
from db import get_supabase

router = APIRouter(prefix="/api/account", tags=["account"])

# Affiliate tiers — real thresholds, 50% commission on software revenue.
TIERS = [
    ("Starter",  0,   50),
    ("Bronze",   3,   50),
    ("Silver",   10,  50),
    ("Gold",     25,  50),
    ("Platinum", 60,  50),
    ("Elite",    150, 50),
]


def _rank(referrals: int) -> dict:
    name, need, rate = TIERS[0]
    nxt = None
    for i, (n, threshold, r) in enumerate(TIERS):
        if referrals >= threshold:
            name, rate = n, r
            nxt = TIERS[i + 1] if i + 1 < len(TIERS) else None
    return {
        "rank": name,
        "commission_pct": rate,
        "referrals": referrals,
        "next_rank": nxt[0] if nxt else None,
        "next_at": nxt[1] if nxt else None,
        "to_next": max(nxt[1] - referrals, 0) if nxt else 0,
    }


def _ref_code(uid: str) -> str:
    return hashlib.sha256((uid + "sklz").encode()).hexdigest()[:8].upper()


@router.get("/overview")
async def overview(user=Depends(get_current_user),
                   sb: Client = Depends(get_supabase)) -> dict:
    uid = str(user.id)

    # ---- subscription (honest: none until billing exists) ----------------
    sub = {"plan": "Free", "active": False, "days_left": None,
           "renews_on": None,
           "note": "Storefront opens soon — early users keep founder pricing."}
    try:
        r = sb.table("subscriptions").select("*").eq("user_id", uid).execute()
        if r.data:
            s = r.data[0]
            ends = s.get("current_period_end")
            days = None
            if ends:
                try:
                    dt = datetime.fromisoformat(str(ends).replace("Z", "+00:00"))
                    days = max((dt - datetime.now(timezone.utc)).days, 0)
                except Exception:
                    days = None
            sub = {"plan": s.get("plan", "Free"),
                   "active": bool(s.get("active")),
                   "days_left": days, "renews_on": ends, "note": ""}
    except Exception:
        pass

    # ---- affiliate -------------------------------------------------------
    referrals = 0
    recent: list[dict] = []
    try:
        r = sb.table("referrals").select("*").eq("referrer_id", uid).execute()
        rows = r.data or []
        referrals = len(rows)
        recent = sorted(rows, key=lambda x: x.get("created_at", ""),
                        reverse=True)[:5]
    except Exception:
        pass

    code = _ref_code(uid)

    def _mask(x: dict) -> dict:
        return {"when": x.get("created_at"),
                "what": x.get("event", "signed up"),
                "who": (x.get("email") or "a new member")[:3] + "•••"}

    all_sorted = sorted(rows, key=lambda x: x.get("created_at", ""),
                        reverse=True) if rows else []
    aff = {
        **_rank(referrals),
        "code": code,
        "link": f"https://www.sklzlabs.com/?ref={code}",
        "recent": [_mask(x) for x in recent],
        "recent_all": [_mask(x) for x in all_sorted[:30]],   # team list
        "earnings": sum(float(x.get("commission", 0) or 0) for x in rows) if rows else 0,
    }

    # ---- the user's own orders (empty until the storefront exists) --------
    orders: list[dict] = []
    try:
        r = (sb.table("orders").select("*").eq("user_id", uid)
               .order("created_at", desc=True).limit(5).execute())
        orders = [{"name": o.get("product", "Order"),
                   "when": o.get("created_at")} for o in (r.data or [])]
    except Exception:
        pass

    # ---- news / promos (from a table the team can edit) -------------------
    news: list[dict] = []
    try:
        r = (sb.table("news").select("*").order("created_at", desc=True)
               .limit(5).execute())
        news = r.data or []
    except Exception:
        pass
    if not news:
        news = [
            {"tag": "NEW", "title": "TradeGPT is live",
             "body": "Drop a chart screenshot and get structure, entry, stop, targets and the honest case against the trade."},
            {"tag": "NEW", "title": "SKLZ indicator suite",
             "body": "Four TradingView tools — Pro, Trend, Flow, Radar — with an honest performance panel no competitor shows."},
            {"tag": "SOON", "title": "Academy & live seminars",
             "body": "Structured courses from working traders. Waitlist opens with the storefront."},
        ]

    return {"subscription": sub, "affiliate": aff, "news": news,
            "orders": orders}
