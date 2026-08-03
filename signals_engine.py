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
def _tg_channel(category: str) -> str:
    return os.environ.get(f"TG_CHANNEL_{category.upper()}", "")


def _mirror_targets() -> list[dict]:
    """Extra destinations that receive EVERY signal, whatever its category.

    Each may use its own bot token, so a group administered by a different
    bot still works without changing the main one.

        TG_MIRROR_CHAT    -100xxxxxxxxxx
        TG_MIRROR_TOKEN   (optional; falls back to TELEGRAM_BOT_TOKEN)

    A second one can be added with TG_MIRROR2_CHAT / TG_MIRROR2_TOKEN.
    """
    out = []
    for prefix in ("TG_MIRROR", "TG_MIRROR2", "TG_MIRROR3"):
        chat = os.environ.get(f"{prefix}_CHAT", "").strip()
        if not chat:
            continue
        out.append({
            "chat": chat,
            "token": (os.environ.get(f"{prefix}_TOKEN", "").strip()
                      or os.environ.get("TELEGRAM_BOT_TOKEN", "")),
            "name": prefix.lower(),
        })
    return out


CHANNEL_KEYS = ("forex", "crypto", "stocks", "metals")


def list_channels() -> list[dict]:
    """Every channel we can post to, and whether it is configured."""
    out = []
    for k in CHANNEL_KEYS:
        out.append({"id": k, "label": k.capitalize(),
                    "configured": bool(_tg_channel(k))})
    out.append({"id": "general", "label": "General / marketing",
                "configured": bool(_general_channel())})
    for m in _mirror_targets():
        out.append({"id": m["name"], "label": f"Signal group ({m['name']})",
                    "configured": True})
    return out


def send_to_channels(channels: list[str], text: str) -> dict:
    """Post one message to an explicit set of channels.

    `channels` may contain category names, "general", or "all".
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        return {"sent": False, "reason": "TELEGRAM_BOT_TOKEN not set", "results": {}}

    wanted = set(channels or [])
    if "all" in wanted:
        wanted = set(CHANNEL_KEYS) | {"general"}

    results, any_ok = {}, False
    if "all" in (channels or []) or "mirrors" in wanted:
        for m in _mirror_targets():
            try:
                ok = _post_telegram(m["chat"], text, m["token"])
            except Exception:
                ok = False
            results[m["name"]] = "sent" if ok else "failed"
            any_ok = any_ok or ok
        wanted.discard("mirrors")
    for name in sorted(wanted):
        chat = _general_channel() if name == "general" else _tg_channel(name)
        if not chat:
            results[name] = "not configured"
            continue
        ok = _post_telegram(chat, text)
        results[name] = "sent" if ok else "failed"
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


def _post_telegram(chat: str, text: str, token: str = "") -> bool:
    token = token or os.environ.get("TELEGRAM_BOT_TOKEN", "")
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


def _general_channel() -> str:
    # public/marketing channel that receives ALL signals. Accept several names.
    for name in ("TG_CHANNEL_SIGNALS", "TG_CHANNEL_GENERAL", "TG_CHANNEL_ALL",
                 "TG_CHANNEL_PUBLIC", "TG_CHANNEL_MARKETING", "TG_CHANNEL_MAIN"):
        v = os.environ.get(name, "")
        if v:
            return v
    return ""


def send_to_telegram(category: str, text: str) -> dict:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        return {"sent": False, "reason": "TELEGRAM_BOT_TOKEN not set"}
    cat_chat = _tg_channel(category)
    gen_chat = _general_channel()
    cat_ok = _post_telegram(cat_chat, text) if cat_chat else False
    # every signal also goes to the public/marketing channel
    gen_ok = _post_telegram(gen_chat, text) if gen_chat else None

    # and to any mirror destinations — groups that take every signal
    # regardless of category, each possibly via its own bot
    mirrors = {}
    for m in _mirror_targets():
        try:
            mirrors[m["name"]] = _post_telegram(m["chat"], text, m["token"])
        except Exception:
            mirrors[m["name"]] = False

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
            "channels": {c: bool(_tg_channel(c)) for c in CATEGORIES}}


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
