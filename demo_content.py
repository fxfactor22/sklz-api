"""Scheduled content for the demo channel.

Three categories only: timeless education, product education, and
summaries of trades that really happened. Nothing here may assert a
current market fact, because nothing here has a source for one.

The Arabic scheduler is deliberately untouched — this module mirrors its
shape (an hours list, a compose step, a policy gate, a delivery step)
rather than importing and reconfiguring it, so a change here can never
alter what @sklzlabsarabic publishes.
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import urllib.request
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status as http
from supabase import Client

import policy
import provider_rules as rules
from aio import offload
from auth import get_current_user
from db import get_supabase
from routing import RoutingScope, resolve_destinations

router = APIRouter(prefix="/api/demo-content", tags=["demo-content"])

DEMO_CHAT = "-1004489542294"          # @sklzlabsdemo, the only destination
CATEGORIES = ("educational", "product", "summary")

# Categories a UI might offer but we cannot honestly produce. Listed so
# the refusal is explicit rather than a silent omission.
DISABLED = {
    "news": "Live market/news source not configured.",
    "market_outlook": "Live market/news source not configured.",
    "eurusd_outlook": "Live market/news source not configured.",
    "gold_outlook": "Live market/news source not configured.",
    "btc_outlook": "Live market/news source not configured.",
    "central_bank": "Live market/news source not configured.",
    "economic_calendar": "Live market/news source not configured.",
    "price_commentary": "Live market/news source not configured.",
}

EDUCATIONAL_TOPICS = [
    "why a stop loss is decided before entry, not after",
    "what breakeven actually protects and what it does not",
    "how a trailing stop differs from moving a stop by hand",
    "why the seconds between your entry and a subscriber's entry matter",
    "position sizing as a function of stop distance, not conviction",
    "why the same signal produces different fills across brokers",
    "what a signal provider owes subscribers when a trade goes wrong",
    "the operational cost of running several accounts by hand",
    "why copy trading needs per-account limits rather than one setting",
    "discipline as a process, not a feeling",
]

PRODUCT_TOPICS = [
    "what happens in the seconds after a provider clicks BUY",
    "how a broker confirmation becomes a subscriber signal",
    "why one trade should be one message that updates, not five posts",
    "how trailing keeps working after the browser is closed",
    "how AI subscriber updates are built only from verified trade data",
    "what assisted VPS and MT5 setup covers",
    "how subscriber communication stops being a manual job",
]

COPIER_LINE = ("Multi-account copying available with Signal Desk "
               "implementation.")


def _hours() -> list[int]:
    raw = os.environ.get("SKLZ_DEMO_CONTENT_HOURS", "7,13,19")
    out = []
    for part in raw.split(","):
        try:
            h = int(part.strip())
        except ValueError:
            continue
        if 0 <= h <= 23:
            out.append(h)
    return sorted(set(out))[:_max_per_day()]


def _enabled() -> bool:
    return os.environ.get("SKLZ_DEMO_CONTENT_ENABLED", "0") == "1"


def _allowed() -> tuple[str, ...]:
    """Which categories may publish. Env is the whole control surface —
    a CMS for three switches would be more to maintain than it saves."""
    raw = os.environ.get("SKLZ_DEMO_CONTENT_CATEGORIES", "").strip()
    if not raw:
        return CATEGORIES
    picked = tuple(c.strip().lower() for c in raw.split(",")
                   if c.strip().lower() in CATEGORIES)
    return picked or CATEGORIES


def _max_per_day() -> int:
    try:
        n = int(os.environ.get("SKLZ_DEMO_CONTENT_MAX_PER_DAY", "4"))
    except ValueError:
        n = 4
    return max(0, min(n, 4))


def _slot_for(hour: int) -> str:
    hrs = _hours()
    if not hrs:
        return "educational"
    idx = hrs.index(hour) if hour in hrs else 0
    return ("educational", "product", "summary")[min(idx, 2)]


def _destination():
    """The demo channel, or nothing. Never a production channel."""
    dests = resolve_destinations(RoutingScope(purpose="demo_signal"))
    dest = dests[0] if dests else None
    if not dest or not dest.enabled:
        return None, "demo delivery disabled"
    if str(dest.chat_id) not in (DEMO_CHAT, "@sklzlabsdemo"):
        return None, f"refused: resolver returned {dest.chat_id}"
    return dest, ""


def _verified_summary(sb: Client) -> dict:
    """Yesterday's real demo activity, or nothing to say.

    Counts only rows the broker confirmed. If there is no activity the
    summary is skipped rather than padded — an empty day is not a story.
    """
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    try:
        rows = (sb.table("bot_orders")
                .select("symbol,side,ticket,fill_price,demo_kind,status,"
                        "executed_at,demo_closed_at,demo_close_state")
                .eq("demo_kind", "market").eq("status", "succeeded")
                .gte("executed_at", since).limit(50).execute()).data or []
    except Exception:  # noqa: BLE001
        return {}
    if not rows:
        return {}
    symbols = sorted({r.get("symbol") or "" for r in rows if r.get("symbol")})
    # A market row succeeding proves the position OPENED, nothing more.
    # And demo_closed_at is stamped when a close command settles either
    # way — including a close the broker REFUSED — so its presence is not
    # evidence of closure. Only a close that itself succeeded is.
    closed = len([r for r in rows
                  if r.get("demo_close_state") == "succeeded"])
    return {"openings_recorded_24h": len(rows), "symbols": symbols,
            "verified_closes_recorded_24h": closed}


def _ai(system: str, user: str) -> tuple[str, str]:
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        return "", "AI drafting is not configured"
    body = json.dumps({"model": "claude-sonnet-4-6", "max_tokens": 420,
                       "system": system,
                       "messages": [{"role": "user", "content": user}]})
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body.encode(),
        headers={"content-type": "application/json", "x-api-key": key,
                 "anthropic-version": "2023-06-01"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.loads(r.read().decode())
    except Exception as exc:  # noqa: BLE001
        return "", f"{type(exc).__name__}"
    text = "".join(b.get("text", "") for b in (d.get("content") or [])
                   if b.get("type") == "text").strip()
    return text, "" if text else "the model returned nothing"


SYSTEM = (
    "You write short Telegram posts for a trading-technology channel.\n"
    "ABSOLUTE RULES:\n"
    "- Never state a current price, level, market direction, news event, "
    "central-bank action or economic-calendar item. You have no source "
    "for any of them and must not invent one.\n"
    "- Never claim performance, profit, win rates or results.\n"
    "- Never give financial advice or a trade recommendation.\n"
    "- Timeless explanation only.\n"
    "Write plainly. 3-6 short lines. No hype, no emoji spam.")


def compose(sb: Client, category: str) -> tuple[str, str]:
    """Returns (text, reason_if_skipped)."""
    if category in DISABLED:
        return "", DISABLED[category]
    if category not in CATEGORIES:
        return "", f"unknown category: {category}"
    if category not in _allowed():
        return "", f"{category} is switched off by configuration"

    if category == "educational":
        topic = random.choice(EDUCATIONAL_TOPICS)
        text, err = _ai(SYSTEM, f"Write an educational post about: {topic}")
    elif category == "product":
        topic = random.choice(PRODUCT_TOPICS)
        text, err = _ai(
            SYSTEM,
            f"Write a short product-education post about: {topic}\n"
            f"This is SKLZ Signal Desk. If copy trading comes up, say "
            f"exactly: {COPIER_LINE}\n"
            f"Do not claim this channel currently executes follower trades.")
    else:
        facts = _verified_summary(sb)
        if not facts:
            return "", "no verified activity to summarise"
        text, err = _ai(
            SYSTEM,
            "Write a factual summary of demonstration activity using ONLY "
            "this data. State no profit, loss or performance of any kind.\n"
            f"DATA: {json.dumps(facts)}\n"
            "IMPORTANT: these are RECORDED EVENTS from the last 24 hours, "
            "not current state. Never say 'the remainder are still open' "
            "or infer how many positions are open now — openings minus "
            "closes does not give that, because trades can be closed "
            "outside this system.\n"
            "End with exactly: 'All activity was executed on an MT5 broker "
            "demo account using demo funds. The execution and automation "
            "are live; no real capital is involved.'")
    if err:
        return "", err

    ok, why = policy.validate(text, strict=False)
    if not ok:
        return "", f"policy:{why}"
    return text, ""


def deliver(dest, text: str) -> dict:
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{dest.token.reveal()}/sendMessage",
            data=json.dumps({"chat_id": dest.chat_id, "text": text,
                             "disable_web_page_preview": True}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=12) as r:
            d = json.loads(r.read().decode())
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}"}
    if not d.get("ok"):
        return {"error": str(d.get("description", ""))[:140]}
    mid = (d.get("result") or {}).get("message_id")
    return {"message_id": mid,
            "url": f"https://t.me/c/{DEMO_CHAT[4:]}/{mid}" if mid else ""}


def publish(sb: Client, category: str) -> dict:
    text, why = compose(sb, category)
    if not text:
        return {"ok": False, "skipped": why}
    dest, err = _destination()
    if not dest:
        return {"ok": False, "skipped": err}
    res = deliver(dest, text)
    return {"ok": bool(res.get("message_id")), "category": category,
            "text": text, **res}


# ── endpoints ───────────────────────────────────────────────────────
@router.get("/preview")
async def preview(category: str = "educational",
                  user=Depends(get_current_user),
                  sb: Client = Depends(get_supabase)) -> dict:
    """Compose without sending."""
    if not rules.is_platform_admin(user):
        raise HTTPException(http.HTTP_403_FORBIDDEN, "platform admin only")
    text, why = await offload(compose, sb, category)
    return {"ok": bool(text), "category": category, "text": text,
            "skipped": why}


@router.post("/publish")
async def publish_now(category: str = "educational",
                      user=Depends(get_current_user),
                      sb: Client = Depends(get_supabase)) -> dict:
    if not rules.is_platform_admin(user):
        raise HTTPException(http.HTTP_403_FORBIDDEN, "platform admin only")
    return await offload(publish, sb, category)


@router.get("/status")
async def content_status(user=Depends(get_current_user)) -> dict:
    if not rules.is_platform_admin(user):
        raise HTTPException(http.HTTP_403_FORBIDDEN, "platform admin only")
    return {"ok": True, "enabled": _enabled(), "hours_utc": _hours(),
            "categories": list(CATEGORIES),
            "allowed_categories": list(_allowed()),
            "disabled_categories": DISABLED,
            "destination": DEMO_CHAT,
            "max_per_day": _max_per_day(),
            "scheduled_today": len(_hours()),
            "controls": {
                "SKLZ_DEMO_CONTENT_ENABLED": "0 or 1",
                "SKLZ_DEMO_CONTENT_HOURS": "comma-separated UTC hours",
                "SKLZ_DEMO_CONTENT_CATEGORIES":
                    "comma-separated; blank = all three",
                "SKLZ_DEMO_CONTENT_MAX_PER_DAY": "1-4"}}


def _cleanup_once() -> dict:
    """Close abandoned demo positions without anyone visiting the page.

    The existing sweep was opportunistic: it ran when someone called a
    demo endpoint. On a public demo nobody may call one for hours, and
    every abandoned click leaves a position open on a $20k account.

    This reuses the SAME guarded sweep — demo-token trades only, on the
    demo Runner, on the configured account, past the interaction window.
    It adds a clock, nothing else. Untracked tickets remain untouchable.
    """
    from db import get_supabase as _gs
    import orders_api
    return {"queued": orders_api._sweep_demo_closes(_gs())}


def start(app) -> None:
    """One post per configured hour, at most four a day."""

    @app.on_event("startup")
    async def _demo_cleanup_loop():           # noqa: ANN202
        async def loop():
            await asyncio.sleep(60)           # let the app settle
            while True:
                try:
                    res = await offload(_cleanup_once)
                    if res.get("queued"):
                        print(f"[demo-cleanup] queued {res['queued']} close(s)")
                except Exception as exc:  # noqa: BLE001
                    # logged and retried on the next tick; a cleanup fault
                    # must never stop the loop or affect trading
                    print(f"[demo-cleanup] {type(exc).__name__}: {exc}")
                await asyncio.sleep(
                    max(60, int(os.environ.get(
                        "SKLZ_DEMO_CLEANUP_SECONDS", "180") or 180)))
        asyncio.create_task(loop())

    @app.on_event("startup")
    async def _demo_content_loop():           # noqa: ANN202
        async def loop():
            seen = set()
            while True:
                try:
                    if _enabled():
                        now = datetime.now(timezone.utc)
                        stamp = (now.date().isoformat(), now.hour)
                        if now.hour in _hours() and stamp not in seen:
                            seen.add(stamp)
                            if len(seen) > 40:
                                seen.clear()
                            from db import get_supabase as _gs
                            res = await offload(publish, _gs(),
                                                _slot_for(now.hour))
                            print(f"[demo-content] {stamp} -> {res}")
                except Exception as exc:  # noqa: BLE001
                    print(f"[demo-content] {type(exc).__name__}: {exc}")
                await asyncio.sleep(300)
        asyncio.create_task(loop())
