"""SKLZ Trader Marketplace — public discovery built on real journal data.

No execution, no credentials, no copying. This is a discovery and analytics
layer: traders opt in to publish an account's performance, and everyone can
see honestly-rated results.

Design rules:
  - a trader only appears if they explicitly list an account
  - every metric is computed from real journaled trades
  - the data source is labelled (MT5-tracked vs manually logged)
  - ratings are damped by sample size; small samples cannot rank highly
  - there is no "verified" badge, because SKLZ does not verify with brokers
"""
from __future__ import annotations

import os

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from supabase import Client

from auth import get_current_user
from db import get_supabase
from trader_metrics import compute, honest_rating, MIN_TRADES_LISTED

router = APIRouter(prefix="/api/marketplace", tags=["marketplace"])

CATEGORIES = [
    "Swing", "Scalping", "Momentum", "Trend Following", "Low Risk",
    "Balanced", "Aggressive", "Gold Specialist", "FX Majors",
    "Crypto", "Indices", "Algorithmic", "Discretionary",
]


class ListingIn(BaseModel):
    account_id: str
    display_name: str
    headline: str = ""
    bio: str = ""
    category: str = "Balanced"
    country: str = ""
    listed: bool = True


def _trades_for(sb: Client, account_id: str) -> list[dict]:
    try:
        return (sb.table("journal_trades").select("*")
                .eq("account_id", account_id)
                .order("closed_at").limit(2000).execute()).data or []
    except Exception:
        return []


def _source_label(account: dict, trades: list[dict]) -> str:
    bot = sum(1 for t in trades if t.get("source") in ("bot", "dashboard"))
    manual = len(trades) - bot
    if account.get("connected") and bot >= manual:
        return "MT5-tracked"
    if manual > bot:
        return "manually logged"
    return "mixed"


def _card(sb: Client, listing: dict) -> dict | None:
    """Build a marketplace card from a listing row."""
    acct_id = listing.get("account_id")
    if not acct_id:
        return None
    try:
        acct = (sb.table("journal_accounts").select("*")
                .eq("id", acct_id).execute()).data
    except Exception:
        acct = None
    if not acct:
        return None
    acct = acct[0]
    trades = _trades_for(sb, acct_id)
    m = compute(trades)
    if m.get("trades", 0) < MIN_TRADES_LISTED:
        # not enough history to list publicly — kept private automatically
        return None
    rating = honest_rating(m)
    return {
        "slug": listing.get("slug"),
        "display_name": listing.get("display_name"),
        "headline": listing.get("headline", ""),
        "category": listing.get("category", "Balanced"),
        "country": listing.get("country", ""),
        "platform": acct.get("platform"),
        "broker": acct.get("broker"),
        "account_kind": acct.get("kind"),
        "data_source": _source_label(acct, trades),
        "metrics": m,
        "rating": rating,
    }


@router.get("/categories")
async def categories() -> dict:
    return {"categories": CATEGORIES}


@router.get("/traders")
async def traders(sort: str = Query("rating"),
                  category: str | None = None,
                  min_trades: int = Query(MIN_TRADES_LISTED),
                  limit: int = Query(50),
                  sb: Client = Depends(get_supabase)) -> dict:
    """PUBLIC — browse listed traders. No auth required."""
    try:
        rows = (sb.table("trader_listings").select("*")
                .eq("listed", True).limit(200).execute()).data or []
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR,
                            f"could not load marketplace: {exc}") from exc

    cards = []
    for r in rows:
        c = _card(sb, r)
        if not c:
            continue
        if category and c["category"] != category:
            continue
        if c["metrics"]["trades"] < min_trades:
            continue
        cards.append(c)

    def key(c):
        m, rt = c["metrics"], c["rating"]
        if sort == "return":
            return m.get("net_pnl") or 0
        if sort == "winrate":
            return m.get("win_rate") or 0
        if sort == "trades":
            return m.get("trades") or 0
        if sort == "drawdown":                 # lower is better
            return -(m.get("max_drawdown") or 0)
        if sort == "consistency":
            return m.get("consistency") or 0
        return rt.get("score") or 0            # default: honest rating

    cards.sort(key=key, reverse=True)
    return {"traders": cards[:limit], "count": len(cards),
            "sorted_by": sort,
            "note": ("Ratings are discounted for small samples. "
                     "Performance is self-published via SKLZ tracking, "
                     "not broker-verified.")}


@router.get("/trader/{slug}")
async def trader_detail(slug: str, sb: Client = Depends(get_supabase)) -> dict:
    """PUBLIC — one trader's full profile."""
    try:
        rows = (sb.table("trader_listings").select("*")
                .eq("slug", slug).eq("listed", True).execute()).data or []
    except Exception:
        rows = []
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "trader not found")
    card = _card(sb, rows[0])
    if not card:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            "not enough trade history to display")
    card["bio"] = rows[0].get("bio", "")
    return card


# ─────────────────────── trader self-management ───────────────────────
@router.get("/my-listings")
async def my_listings(user=Depends(get_current_user),
                      sb: Client = Depends(get_supabase)) -> dict:
    try:
        rows = (sb.table("trader_listings").select("*")
                .eq("user_id", str(user.id)).execute()).data or []
    except Exception:
        rows = []
    for r in rows:
        trades = _trades_for(sb, r.get("account_id"))
        r["trade_count"] = len(trades)
        r["eligible"] = len(trades) >= MIN_TRADES_LISTED
    return {"listings": rows, "min_trades_to_list": MIN_TRADES_LISTED}


@router.post("/list")
async def create_listing(body: ListingIn, user=Depends(get_current_user),
                         sb: Client = Depends(get_supabase)) -> dict:
    """Publish one of MY accounts to the marketplace."""
    uid = str(user.id)
    # ownership check — you may only list your own account
    try:
        own = (sb.table("journal_accounts").select("id")
               .eq("id", body.account_id).eq("user_id", uid).execute()).data
    except Exception:
        own = None
    if not own:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "not your account")

    import hashlib
    slug = hashlib.sha256((body.account_id + uid).encode()).hexdigest()[:10]
    row = {"user_id": uid, "account_id": body.account_id, "slug": slug,
           "display_name": body.display_name[:60],
           "headline": body.headline[:140], "bio": body.bio[:1000],
           "category": body.category, "country": body.country[:60],
           "listed": body.listed}
    try:
        existing = (sb.table("trader_listings").select("id")
                    .eq("account_id", body.account_id).execute()).data
        if existing:
            sb.table("trader_listings").update(row) \
                .eq("id", existing[0]["id"]).execute()
        else:
            sb.table("trader_listings").insert(row).execute()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR,
                            f"could not list: {exc}") from exc
    site = os.environ.get("SITE_URL") or "https://www.sklzlabs.com"
    return {"ok": True, "slug": slug,
            "public_url": f"{site}/trader.html?t={slug}"}


@router.delete("/list/{account_id}")
async def unlist(account_id: str, user=Depends(get_current_user),
                 sb: Client = Depends(get_supabase)) -> dict:
    sb.table("trader_listings").update({"listed": False}) \
        .eq("account_id", account_id).eq("user_id", str(user.id)).execute()
    return {"ok": True}


# ─────────────────────────── AI trader read ───────────────────────────
AI_SYSTEM = """You review a trader's real performance record for the SKLZ \
marketplace. SKLZ's brand is honesty: its own 27M-tick research showed most \
strategies are coin-flips out-of-sample.

Rules:
- Judge the EVIDENCE, not the headline numbers. A great record on 15 trades is \
not evidence; say so plainly.
- Name the specific strength and the specific weakness you can see in the data.
- If win rate and profit factor sit near coin-flip territory, say it directly.
- Never predict future returns. Never say "will".
- Be respectful but unflinching. A follower's money depends on this being true.

Return STRICT JSON only:
{"read":"2-3 sentence honest assessment",
 "strength":"the clearest genuine strength, or 'none evident'",
 "weakness":"the clearest risk or weakness",
 "who_its_for":"what kind of follower this suits, or 'nobody yet'",
 "trust_level":"low|medium|high"}"""


@router.get("/trader/{slug}/ai-read")
async def ai_read(slug: str, sb: Client = Depends(get_supabase)) -> dict:
    """AI assessment of a listed trader. Falls back to the deterministic
    rating summary when no API key is configured."""
    card = await trader_detail(slug, sb)          # reuse + 404 handling
    m, rt = card["metrics"], card["rating"]
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        return {"ai": {"read": rt["summary"],
                       "strength": "—", "weakness": "; ".join(rt["flags"]) or "—",
                       "who_its_for": "—",
                       "trust_level": m["confidence"]["level"]},
                "source": "deterministic"}
    import json as _json
    facts = {
        "trades": m["trades"], "win_rate": m["win_rate"],
        "profit_factor": m["profit_factor"], "net_pnl": m["net_pnl"],
        "max_drawdown": m["max_drawdown"], "sharpe": m["sharpe"],
        "consistency": m["consistency"],
        "profitable_months": f"{m['profitable_months']}/{m['months_traded']}",
        "max_consecutive_losses": m["max_consecutive_losses"],
        "confidence": m["confidence"]["label"],
        "rating": rt["score"], "flags": rt["flags"],
        "data_source": card["data_source"],
    }
    try:
        import anthropic
        cl = anthropic.Anthropic(api_key=key)
        msg = cl.messages.create(
            model="claude-sonnet-4-6", max_tokens=600, system=AI_SYSTEM,
            messages=[{"role": "user",
                       "content": f"Trader record:\n{_json.dumps(facts, indent=2)}\n\nReturn the JSON."}])
        txt = "".join(b.text for b in msg.content if b.type == "text").strip()
        txt = txt.removeprefix("```json").removeprefix("```").removesuffix("```")
        return {"ai": _json.loads(txt), "source": "ai"}
    except Exception:
        return {"ai": {"read": rt["summary"], "strength": "—",
                       "weakness": "; ".join(rt["flags"]) or "—",
                       "who_its_for": "—",
                       "trust_level": m["confidence"]["level"]},
                "source": "deterministic"}
