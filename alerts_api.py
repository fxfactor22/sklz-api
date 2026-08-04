"""SKLZ — scanner alerts.

Tells a subscriber when the scanner finds a clean setup.

TWO THINGS THIS DELIBERATELY DOES
=================================
It does not fire twice for the same thing. A coin that stays clean for six
hours produces one alert, not seventy-two. Repeated pings for an unchanged
condition train people to ignore the channel, and then the one that mattered
gets ignored too.

And it carries the same caveat the scanner shows on screen. An alert is the
most urgency-inducing format there is — it arrives unbidden, on a phone, with
a notification sound — and urgency is what produces bad entries. Saying
"momentum setups are frequently coin-flips out of sample" inside the alert is
the difference between a tool and a hype machine.

WHAT COUNTS AS ALERT-WORTHY
===========================
A transition, not a state. The coin must have moved INTO clean-setup territory
since the last check. Something that was already clean an hour ago is not news.
"""
from __future__ import annotations

import json
import os
import urllib.request
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field
from supabase import Client

from auth import get_current_user
from db import get_supabase

router = APIRouter(prefix="/api/alerts", tags=["alerts"])

# how long before the same coin can alert again
COOLDOWN_HOURS = 6


class AlertPrefs(BaseModel):
    enabled: bool = True
    telegram_chat_id: str = Field(default="", max_length=40)
    min_score: float = Field(default=0.35, ge=0, le=1)
    only_clean: bool = True
    assets: list[str] = Field(default_factory=list)   # empty = all
    quiet_hours: bool = True                          # skip 22:00-07:00 local


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _send_telegram(chat_id: str, text: str) -> bool:
    token = (os.environ.get("TG_SALES_BOT_TOKEN")
             or os.environ.get("TELEGRAM_BOT_TOKEN", ""))
    if not token or not chat_id:
        return False
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=json.dumps({"chat_id": chat_id, "text": text,
                             "parse_mode": "Markdown",
                             "disable_web_page_preview": True}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=12) as r:
            return json.loads(r.read().decode()).get("ok", False)
    except Exception:
        return False


def format_alert(coin: dict, read: dict | None = None) -> str:
    """The message itself. Honest by construction."""
    sym = coin.get("symbol", "?")
    score = coin.get("score")
    h1 = coin.get("h1")
    h24 = coin.get("h24")

    lines = [f"\U0001F4E1 *{sym}* — clean setup"]
    if score is not None:
        lines.append(f"momentum score {score:+.2f}")
    if h1 is not None and h24 is not None:
        lines.append(f"1h {h1:+.1f}%  ·  24h {h24:+.1f}%")

    if read:
        bias = (read.get("bias") or "").upper()
        conf = read.get("confidence") or ""
        if bias:
            lines.append(f"\nread: *{bias}* ({conf} confidence)")
        if read.get("headline"):
            lines.append(read["headline"])
        if read.get("flow_note"):
            lines.append(f"\nbook: {read['flow_note']}")
        if read.get("honest_flag"):
            lines.append(f"\n\u26a0 {read['honest_flag']}")

    lines.append(
        "\n_This is a condition worth looking at, not a recommendation. "
        "SKLZ research on 27M ticks found most momentum setups are coin-flips "
        "out of sample. Check it yourself._")
    return "\n".join(lines)


def _recently_alerted(sb: Client, user_id: str, symbol: str) -> bool:
    """Has this coin already alerted inside the cooldown?"""
    cutoff = (_now() - timedelta(hours=COOLDOWN_HOURS)).isoformat()
    try:
        rows = (sb.table("scanner_alerts").select("id")
                .eq("user_id", user_id).eq("symbol", symbol)
                .gte("sent_at", cutoff).limit(1).execute()).data or []
        return bool(rows)
    except Exception:
        return False


def _in_quiet_hours() -> bool:
    h = _now().hour
    return h >= 22 or h < 7


def run_alerts(sb: Client, coins: list[dict], log=print) -> dict:
    """Check the current scan against every subscriber's preferences."""
    try:
        prefs = (sb.table("alert_prefs").select("*")
                 .eq("enabled", True).execute()).data or []
    except Exception:
        prefs = []
    if not prefs:
        return {"checked": 0, "sent": 0}

    sent = skipped = 0
    for p in prefs:
        uid = p.get("user_id")
        chat = (p.get("telegram_chat_id") or "").strip()
        if not (uid and chat):
            continue
        if p.get("quiet_hours", True) and _in_quiet_hours():
            skipped += 1
            continue

        want = {a.upper() for a in (p.get("assets") or [])}
        min_score = float(p.get("min_score") or 0.35)

        for c in coins:
            sym = (c.get("symbol") or "").upper()
            if not sym:
                continue
            if want and sym not in want:
                continue
            if p.get("only_clean", True) and c.get("read") != "clean setup":
                continue
            if abs(float(c.get("score") or 0)) < min_score:
                continue
            if _recently_alerted(sb, uid, sym):
                continue

            if _send_telegram(chat, format_alert(c)):
                sent += 1
                try:
                    sb.table("scanner_alerts").insert({
                        "user_id": uid, "symbol": sym,
                        "score": c.get("score"),
                        "sent_at": _now().isoformat(),
                    }).execute()
                except Exception:
                    pass
                log(f"[alerts] {sym} -> {uid}")

    return {"checked": len(prefs), "sent": sent, "quiet_skipped": skipped}


# ── endpoints ───────────────────────────────────────────────────────
@router.get("/prefs")
async def get_prefs(user=Depends(get_current_user),
                    sb: Client = Depends(get_supabase)) -> dict:
    try:
        rows = (sb.table("alert_prefs").select("*")
                .eq("user_id", str(user.id)).execute()).data or []
    except Exception:
        rows = []
    p = rows[0] if rows else {}
    return {
        "enabled": p.get("enabled", False),
        "telegram_chat_id": p.get("telegram_chat_id", ""),
        "min_score": p.get("min_score", 0.35),
        "only_clean": p.get("only_clean", True),
        "assets": p.get("assets") or [],
        "quiet_hours": p.get("quiet_hours", True),
        "cooldown_hours": COOLDOWN_HOURS,
        "note": (f"A coin alerts at most once every {COOLDOWN_HOURS} hours. "
                 f"Repeated pings for an unchanged condition just teach you to "
                 f"ignore them."),
    }


@router.put("/prefs")
async def save_prefs(body: AlertPrefs, user=Depends(get_current_user),
                     sb: Client = Depends(get_supabase)) -> dict:
    row = {"user_id": str(user.id), **body.model_dump(),
           "updated_at": _now().isoformat()}
    try:
        sb.table("alert_prefs").upsert(row, on_conflict="user_id").execute()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR,
                            f"could not save: {str(exc)[:150]}") from exc
    return {"ok": True, "message": "Alert settings saved."}


@router.post("/test")
async def test_alert(user=Depends(get_current_user),
                     sb: Client = Depends(get_supabase)) -> dict:
    """Send one test message, so a wrong chat id is found now rather than
    when something actually matters."""
    try:
        rows = (sb.table("alert_prefs").select("telegram_chat_id")
                .eq("user_id", str(user.id)).execute()).data or []
    except Exception:
        rows = []
    chat = (rows[0].get("telegram_chat_id") if rows else "") or ""
    if not chat:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "No Telegram chat id saved yet.")

    ok = _send_telegram(chat, format_alert(
        {"symbol": "BTC", "score": 0.42, "h1": 1.2, "h24": 4.8},
        {"bias": "long", "confidence": "medium",
         "headline": "This is a test alert.",
         "honest_flag": "Momentum setups are frequently coin-flips out of sample."}))
    if not ok:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "Telegram did not accept the message. Check the chat id, and that "
            "you have sent /start to the bot at least once — Telegram will not "
            "let a bot message someone who has never contacted it.")
    return {"ok": True, "message": "Test alert sent."}


@router.post("/run")
async def run_now(authorization: str = Header(default=""),
                  sb: Client = Depends(get_supabase)) -> dict:
    """Called by the scheduler after each scan."""
    key = os.environ.get("INTERNAL_KEY", "") or os.environ.get("BOT_INGEST_KEY", "")
    if not key or authorization.replace("Bearer ", "").strip() != key:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "unauthorized")

    try:
        import scanner as SC
        coins = SC._fetch_markets(100)
        scored = [SC._score(c) for c in coins]
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_502_BAD_GATEWAY,
                            f"scan failed: {str(exc)[:150]}") from exc

    return run_alerts(sb, scored)
