"""SKLZ Signals — Radar alerts -> enriched signals -> category Telegram channels.

Flow:
  1. TradingView (SKLZ Radar) fires a JSON alert to /api/signal/webhook/{key}
  2. We enrich: asset class, entry zone, SL, TP (ATR/structure, server-side)
  3. Store in `signals`; broadcast to the matching category Telegram channel
  4. Dashboard shows the feed; clients toggle categories their SUBSCRIPTION allows

Category channels (env): TG_CHANNEL_FOREX / _CRYPTO / _STOCKS / _METALS
Bot token (env): TELEGRAM_BOT_TOKEN
"""
from __future__ import annotations

import json
import os
import urllib.request
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from supabase import Client

from auth import get_current_user
from db import get_supabase
from routing import (RoutingScope, resolve_destinations,
                     get_resolver, CHANNEL_KEYS as _RCHK)

router = APIRouter(prefix="/api/signals", tags=["signals"])

# ── asset-class classification ───────────────────────────────────────
CRYPTO = {"BTC", "ETH", "SOL", "XRP", "ADA", "DOGE", "BNB", "LTC", "DOT", "AVAX"}
METALS = {"XAU", "XAG", "XPT", "XPD", "GOLD", "SILVER"}
FX = {"EUR", "GBP", "USD", "JPY", "CHF", "AUD", "NZD", "CAD"}
STOCK_HINT = {"US30", "NAS100", "SPX", "SP500", "GER40", "UK100", "AAPL",
              "TSLA", "NVDA", "AMD", "MSFT", "AMZN", "META", "GOOGL"}


def classify(symbol: str) -> str:
    s = symbol.upper().replace("/", "").replace("_", "")
    if any(s.startswith(c) for c in CRYPTO) or s.endswith("USDT"):
        return "crypto"
    if any(s.startswith(m) for m in METALS):
        return "metals"
    if s in STOCK_HINT or any(h in s for h in STOCK_HINT):
        return "stocks"
    if len(s) == 6 and s[:3] in FX and s[3:6] in FX:
        return "forex"
    return "forex"           # default bucket


CATEGORIES = ["forex", "crypto", "stocks", "metals"]

# subscription -> which categories that plan unlocks
PLAN_CATEGORIES = {
    "Free": [],
    "Indicator Suite": ["forex", "metals"],
    "Indicator Suite — Lifetime": ["forex", "metals"],
    "TradeGPT Pro": [],
    "Bundle": CATEGORIES,
    "Bundle (Founder)": CATEGORIES,
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── entry/SL/TP enrichment (server-side, ATR + structure) ────────────
def enrich_levels(payload: dict) -> dict:
    """Compute entry zone, SL, TP. Prefers levels sent in the alert; otherwise
    derives them from price + ATR. All mechanical — no prediction."""
    side = (payload.get("side") or payload.get("direction") or "buy").lower()
    side = "sell" if side in ("sell", "short", "-1") else "buy"
    price = float(payload.get("price") or payload.get("close") or 0)
    atr = float(payload.get("atr") or 0)
    # sensible fallback if no ATR provided: 0.3% of price
    if atr <= 0 and price > 0:
        atr = price * 0.003

    # explicit levels win if the alert provided them
    entry = payload.get("entry")
    sl = payload.get("sl")
    tp = payload.get("tp")
    if entry and sl and tp:
        return {"side": side, "entry": float(entry), "entry_low": float(entry),
                "entry_high": float(entry), "sl": float(sl), "tp": float(tp),
                "rr": round(abs(float(tp)-float(entry))/max(abs(float(entry)-float(sl)), 1e-9), 2)}

    d = 1 if side == "buy" else -1
    entry_mid = price
    zone = atr * 0.25                         # entry zone half-width
    sl_dist = atr * 1.5
    tp_dist = atr * 3.0                       # 2R by construction
    entry_low = round(entry_mid - zone, 6)
    entry_high = round(entry_mid + zone, 6)
    sl_v = round(entry_mid - d * sl_dist, 6)
    tp_v = round(entry_mid + d * tp_dist, 6)
    return {"side": side, "entry": round(entry_mid, 6),
            "entry_low": entry_low, "entry_high": entry_high,
            "sl": sl_v, "tp": tp_v, "rr": round(tp_dist / sl_dist, 2)}


# ── telegram ─────────────────────────────────────────────────────────
def list_channels() -> list[dict]:
    """Every channel we can post to, and whether it is configured."""
    out = []
    for k in CHANNEL_KEYS:
        out.append({"id": k, "label": k.capitalize(),
                    "configured": get_resolver().describe()["categories"].get(k, False)})
    desc = get_resolver().describe()
    out.append({"id": "general", "label": "General / marketing",
                "configured": bool(desc.get("general"))})
    for m in desc.get("mirrors", []):
        out.append({"id": m["key"], "label": f"Signal group ({m['key']})",
                    "configured": bool(m.get("chat_id"))})
    return out


def send_to_channels(channels: list[str], text: str) -> dict:
    """Post one message to an explicit set of channels.

    `channels` may contain category names, "general", or "all".
    """
    dests = resolve_destinations(
        RoutingScope(channels=tuple(channels or []), purpose="broadcast"))
    if not any(d.token for d in dests):
        return {"sent": False, "reason": "TELEGRAM_BOT_TOKEN not set",
                "results": {}}

    results, any_ok = {}, False
    for d in dests:
        if not d.enabled:
            results[d.key] = "not configured"
            continue
        try:
            ok = deliver(d, text)
        except Exception:  # noqa: BLE001
            ok = False
        results[d.key] = "sent" if ok else "failed"
        any_ok = any_ok or ok
    return {"sent": any_ok, "results": results}


def format_signal(sig: dict) -> str:
    arrow = "🟢 BUY" if sig["side"] == "buy" else "🔴 SELL"
    cat = sig["category"].upper()
    ez = (f"{sig['entry_low']} – {sig['entry_high']}"
          if sig.get("entry_low") != sig.get("entry_high") else str(sig["entry"]))
    return (
        f"*SKLZ RADAR SIGNAL* · {cat}\n"
        f"{arrow}  *{sig['symbol']}*  ({sig.get('timeframe','')})\n\n"
        f"🎯 Entry zone: `{ez}`\n"
        f"🛑 Stop loss: `{sig['sl']}`\n"
        f"✅ Take profit: `{sig['tp']}`\n"
        f"📊 R:R ≈ {sig.get('rr','—')}\n"
        + (f"\n{sig['note']}\n" if sig.get("note") else "")
        + "\n_SKLZ Labs · software only, not financial advice · trade at your own risk_"
    )


def deliver(dest, text: str) -> bool:
    """Telegram delivery adapter. Takes a resolved Destination and sends.

    It does NOT resolve routing. Given a destination with no credential it
    fails rather than reaching for the environment, because a sender that
    can rediscover tokens is a second routing system in disguise.
    """
    return _post_telegram(dest.chat_id, text, dest.token)


def _post_telegram(chat: str, text: str, token: str = "") -> bool:
    if not token or not chat:
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = json.dumps({"chat_id": chat, "text": text,
                       "parse_mode": "Markdown",
                       "disable_web_page_preview": True}).encode()
    try:
        req = urllib.request.Request(url, data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode()).get("ok", False)
    except Exception:  # noqa: BLE001
        return False


def send_to_telegram(category: str, text: str) -> dict:
    """Publish one signal. Destinations come from the resolver, not here."""
    dests = resolve_destinations(RoutingScope(category=category,
                                              purpose="signal"))
    if not any(d.token for d in dests):
        return {"sent": False, "reason": "TELEGRAM_BOT_TOKEN not set"}

    by_key = {d.key: d for d in dests}
    cat_d = by_key.get(category)
    gen_d = by_key.get("general")
    cat_chat = cat_d.chat_id if cat_d else ""
    cat_ok = deliver(cat_d, text) if (cat_d and cat_d.enabled) else False
    # every signal also goes to the public/marketing channel
    gen_ok = deliver(gen_d, text) if gen_d else None

    # and to any mirror destinations — groups that take every signal
    # regardless of category, each possibly via its own bot
    mirrors = {}
    for m in [d for d in dests if not d.primary]:
        try:
            mirrors[m.key] = deliver(m, text)
        except Exception:  # noqa: BLE001
            mirrors[m.key] = False

    return {"sent": bool(cat_ok or gen_ok or any(mirrors.values())),
            "category_channel": cat_ok,
            "general_channel": gen_ok,
            "mirrors": mirrors or None,
            "reason": None if cat_chat else f"no channel configured for {category}"}


# ── webhook: TradingView Radar -> enriched signal -> channel ─────────
@router.post("/webhook/{key}")
async def signal_webhook(key: str, payload: dict,
                         sb: Client = Depends(get_supabase)) -> dict:
    """TradingView alert endpoint. `key` is a shared secret in the alert URL."""
    expected = os.environ.get("SIGNAL_WEBHOOK_KEY", "")
    if not expected or key != expected:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad signal key")

    symbol = (payload.get("symbol") or payload.get("ticker") or "").upper()
    if not symbol:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "missing symbol")
    category = payload.get("category") or classify(symbol)
    lv = enrich_levels(payload)
    sig = {
        "symbol": symbol, "category": category,
        "timeframe": payload.get("timeframe") or payload.get("interval") or "",
        "note": payload.get("note") or payload.get("comment") or "",
        "created_at": _now(), **lv,
    }
    try:
        sb.table("signals").insert(sig).execute()
    except Exception as exc:  # noqa: BLE001
        # never lose the Telegram send just because the DB hiccuped
        print(f"[signals] db insert failed: {exc}")

    tg = send_to_telegram(category, format_signal(sig))
    # The Arabic channel gets the same signal WRITTEN in Arabic, not the
    # English text forwarded like a mirror would. This belongs here, in
    # the webhook, because this is where the structured signal exists —
    # send_to_telegram only ever sees the finished English string.
    try:
        import sklz_arabic
        tg["arabic"] = sklz_arabic.send_signal(sig)
    except Exception as exc:  # noqa: BLE001
        tg["arabic"] = False
        print(f"[signals] arabic send failed: {type(exc).__name__}: {exc}")
    return {"ok": True, "category": category, "levels": lv, "telegram": tg}


# ── dashboard: recent signals, filtered by the client's entitlements ─
def _admin_emails() -> set[str]:
    raw = os.environ.get("ADMIN_EMAILS", "fxfactor24@gmail.com")
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


def _plan_of(sb: Client, uid: str, email: str = "") -> str:
    # owner/admin bypass: always full Bundle, no subscription needed
    if email and email.lower() in _admin_emails():
        return "Bundle"
    try:
        r = (sb.table("subscriptions").select("plan,active")
             .eq("user_id", uid).execute()).data or []
        for row in r:
            if row.get("active"):
                return row.get("plan", "Free")
    except Exception:
        pass
    return "Free"


@router.get("/entitlements")
async def entitlements(user=Depends(get_current_user),
                       sb: Client = Depends(get_supabase)) -> dict:
    plan = _plan_of(sb, str(user.id), getattr(user, "email", ""))
    allowed = PLAN_CATEGORIES.get(plan, [])
    # load the user's saved category preferences (subset of allowed)
    prefs = allowed
    try:
        r = (sb.table("signal_prefs").select("categories")
             .eq("user_id", str(user.id)).execute()).data
        if r:
            prefs = [c for c in (r[0].get("categories") or []) if c in allowed]
    except Exception:
        pass
    return {"plan": plan, "allowed": allowed, "enabled": prefs,
            "all_categories": CATEGORIES,
            "channels": {c: get_resolver().describe()["categories"].get(c, False)
                         for c in CATEGORIES}}


class PrefsIn(BaseModel):
    categories: list[str] = Field(default_factory=list)


@router.post("/prefs")
async def save_prefs(p: PrefsIn, user=Depends(get_current_user),
                     sb: Client = Depends(get_supabase)) -> dict:
    plan = _plan_of(sb, str(user.id), getattr(user, "email", ""))
    allowed = PLAN_CATEGORIES.get(plan, [])
    chosen = [c for c in p.categories if c in allowed]     # can't enable unentitled
    try:
        sb.table("signal_prefs").upsert(
            {"user_id": str(user.id), "categories": chosen, "updated_at": _now()},
            on_conflict="user_id").execute()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR,
                            f"could not save prefs: {exc}") from exc
    # telegram join links for the newly-enabled categories (Option A gating)
    links = {c: os.environ.get(f"TG_INVITE_{c.upper()}", "") for c in chosen}
    return {"ok": True, "enabled": chosen, "join_links": links}


@router.get("/feed")
async def feed(user=Depends(get_current_user),
               sb: Client = Depends(get_supabase),
               category: str | None = Query(None), limit: int = 50) -> dict:
    plan = _plan_of(sb, str(user.id), getattr(user, "email", ""))
    allowed = PLAN_CATEGORIES.get(plan, [])
    if not allowed:
        return {"plan": plan, "allowed": [], "signals": [],
                "locked": True,
                "message": "Signals are included with the Indicator Suite and "
                           "Bundle plans. Upgrade to unlock."}
    cats = [category] if category and category in allowed else allowed
    try:
        q = (sb.table("signals").select("*")
             .in_("category", cats)
             .order("created_at", desc=True).limit(min(limit, 200)))
        rows = q.execute().data or []
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR,
                            f"could not load feed: {exc}") from exc
    return {"plan": plan, "allowed": allowed, "signals": rows}
