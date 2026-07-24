"""SKLZ — audience content assistant.

Publishes the things that keep a channel alive between signals: high-impact
news warnings, session opens, market holidays, and a morning note.

The rule that matters: **the AI writes, it never invents.**

  news / holidays  → pulled from a real economic calendar feed, then phrased
  session opens    → fixed schedule, factual
  morning note     → AI-written, but about mindset and process, never a
                     market call or a prediction

That distinction is the whole point. An assistant that generates plausible
"market news" from nothing is a fabrication engine, and a trading audience is
exactly the wrong place for one. Every factual claim here traces to a source.
"""
from __future__ import annotations

import json
import os
import urllib.request
from datetime import datetime, time, timedelta, timezone

CAL_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
_CACHE: dict = {"ts": 0.0, "data": None}
_TTL = 900          # 15 min


def _get(url: str, timeout: int = 15):
    req = urllib.request.Request(url, headers={"User-Agent": "SKLZ/1.0",
                                               "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def calendar(force: bool = False) -> list[dict]:
    """This week's economic events. Real data, cached."""
    import time as _t
    now = _t.time()
    if not force and _CACHE["data"] and now - _CACHE["ts"] < _TTL:
        return _CACHE["data"]
    try:
        rows = _get(CAL_URL) or []
    except Exception:
        return _CACHE["data"] or []
    _CACHE.update(ts=now, data=rows)
    return rows


def _parse(ts: str):
    try:
        return datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None


def upcoming_high_impact(within_minutes: int = 90) -> list[dict]:
    """High-impact events landing soon. This is what actually moves price."""
    now = datetime.now(timezone.utc)
    out = []
    for e in calendar():
        if (e.get("impact") or "").lower() != "high":
            continue
        d = _parse(e.get("date", ""))
        if not d:
            continue
        d = d.astimezone(timezone.utc)
        mins = (d - now).total_seconds() / 60
        if 0 <= mins <= within_minutes:
            out.append({"title": e.get("title"), "currency": e.get("country"),
                        "at": d.isoformat(), "in_minutes": round(mins),
                        "forecast": e.get("forecast") or "",
                        "previous": e.get("previous") or ""})
    return sorted(out, key=lambda x: x["in_minutes"])


def todays_holidays() -> list[dict]:
    """Market holidays today — thin liquidity, wider spreads."""
    today = datetime.now(timezone.utc).date()
    out = []
    for e in calendar():
        if (e.get("impact") or "").lower() != "holiday":
            continue
        d = _parse(e.get("date", ""))
        if d and d.astimezone(timezone.utc).date() == today:
            out.append({"title": e.get("title"), "currency": e.get("country")})
    return out


def todays_high_impact() -> list[dict]:
    """Everything high-impact scheduled today, for the morning note."""
    today = datetime.now(timezone.utc).date()
    out = []
    for e in calendar():
        if (e.get("impact") or "").lower() != "high":
            continue
        d = _parse(e.get("date", ""))
        if not d:
            continue
        d = d.astimezone(timezone.utc)
        if d.date() == today:
            out.append({"title": e.get("title"), "currency": e.get("country"),
                        "at": d.strftime("%H:%M UTC")})
    return out


# ── sessions (fixed schedule, no guessing) ──────────────────────────
SESSIONS = [
    ("Sydney", time(21, 0), time(6, 0)),
    ("Tokyo", time(0, 0), time(9, 0)),
    ("London", time(7, 0), time(16, 0)),
    ("New York", time(12, 0), time(21, 0)),
]


def session_state(now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    t = now.time()
    open_now = []
    for name, start, end in SESSIONS:
        if start < end:
            if start <= t < end:
                open_now.append(name)
        else:                              # wraps midnight
            if t >= start or t < end:
                open_now.append(name)
    overlap = "London" in open_now and "New York" in open_now
    return {"open": open_now, "london_ny_overlap": overlap,
            "utc_time": now.strftime("%H:%M")}


# ── message builders ────────────────────────────────────────────────
def news_warning() -> dict | None:
    """A warning about imminent high-impact data. Facts only."""
    events = upcoming_high_impact(90)
    if not events:
        return None
    e = events[0]
    others = len(events) - 1
    detail = [f"{e['currency']} · {e['title']} in {e['in_minutes']} minutes"]
    if e.get("forecast"):
        detail.append(f"Forecast {e['forecast']}"
                      + (f" · previous {e['previous']}" if e.get("previous") else ""))
    detail.append("")
    detail.append("Spreads widen and stops get hunted around releases like this. "
                  "If you are already in, consider whether your stop can survive "
                  "the spike. If you are not, there is no prize for entering first.")
    if others:
        detail.append(f"({others} more high-impact release"
                      f"{'s' if others > 1 else ''} in the next 90 minutes.)")
    return {"kind": "info",
            "headline": f"High-impact data soon: {e['currency']} {e['title']}",
            "detail": "\n".join(detail),
            "source": "economic calendar"}


def holiday_note() -> dict | None:
    hols = todays_holidays()
    if not hols:
        return None
    names = ", ".join(f"{h['currency']} ({h['title']})" for h in hols[:4])
    return {"kind": "info",
            "headline": f"Market holiday today: {names}",
            "detail": ("Liquidity will be thinner than usual and spreads wider. "
                       "Ranges tend to be smaller and breakouts less reliable. "
                       "Sizing down or sitting out is a legitimate choice today."),
            "source": "economic calendar"}


def session_note() -> dict:
    st = session_state()
    if st["london_ny_overlap"]:
        head = "London / New York overlap is open"
        body = ("The busiest window of the day. Liquidity is deepest and moves "
                "extend further — which cuts both ways.")
    elif "London" in st["open"]:
        head = "London session is open"
        body = "European liquidity is in. Watch for the sweep of Asian range highs and lows."
    elif "New York" in st["open"]:
        head = "New York session is open"
        body = "US data and volume dominate from here. Trends set now often carry into the close."
    elif "Tokyo" in st["open"]:
        head = "Tokyo session is open"
        body = "Ranges are typically tighter. JPY pairs and gold see the most interest."
    else:
        head = "Sydney session is open"
        body = "The quietest window. Spreads are widest and moves are thin."
    return {"kind": "info", "headline": head,
            "detail": body + f"\n\n{st['utc_time']} UTC",
            "source": "session schedule"}


MORNING_SYSTEM = """You write one short morning note for a trading community \
run by SKLZ Labs, whose entire brand is honesty about trading.

Hard rules:
- NEVER predict a direction, name a level, or suggest a trade.
- NEVER claim what the market "will" do.
- Write about process, patience, risk, discipline, or the psychology of a \
trading day. That is the point.
- No hype, no "let's get this money", no rocket emojis. SKLZ's readers are \
told the truth: most retail strategies are coin-flips, and the edge that \
exists comes from risk control and consistency.
- 2 to 3 sentences. Plain, direct, a little dry. British-leaning tone.

Return STRICT JSON only:
{"headline":"short, under 60 characters","detail":"2-3 sentences"}"""


def morning_note(context: str = "") -> dict:
    """AI-written note about mindset. Falls back to a written line if no key."""
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    events = todays_high_impact()
    hols = todays_holidays()
    ctx = []
    if events:
        ctx.append("High-impact data today: "
                   + ", ".join(f"{e['currency']} {e['title']} at {e['at']}"
                               for e in events[:4]))
    if hols:
        ctx.append("Market holiday: "
                   + ", ".join(f"{h['currency']}" for h in hols[:3]))
    if context:
        ctx.append(context)

    if not key:
        return {"kind": "announcement",
                "headline": "Morning. Process before profit.",
                "detail": ("The market does not owe you a setup today. "
                           "Wait for the one that fits your plan, size it so a "
                           "loss is survivable, and let the rest go."
                           + (("\n\n" + ctx[0]) if ctx else "")),
                "source": "template"}
    try:
        import anthropic
        cl = anthropic.Anthropic(api_key=key)
        prompt = ("Write today's morning note."
                  + (("\n\nContext you may reference:\n" + "\n".join(ctx)) if ctx else ""))
        m = cl.messages.create(model="claude-sonnet-4-5", max_tokens=400,
                               system=MORNING_SYSTEM,
                               messages=[{"role": "user", "content": prompt}])
        txt = "".join(b.text for b in m.content if b.type == "text").strip()
        txt = txt.removeprefix("```json").removeprefix("```").removesuffix("```")
        data = json.loads(txt)
        detail = data.get("detail", "")
        if ctx:
            detail += "\n\n" + ctx[0]
        return {"kind": "announcement",
                "headline": data.get("headline", "Morning note"),
                "detail": detail, "source": "ai"}
    except Exception:
        return {"kind": "announcement",
                "headline": "Morning. Process before profit.",
                "detail": ("Wait for the setup that fits your plan, size it so a "
                           "loss is survivable, and let the rest go."
                           + (("\n\n" + ctx[0]) if ctx else "")),
                "source": "template-fallback"}


def build(kind: str, context: str = "") -> dict | None:
    """One entry point. Returns a message dict or None if there is nothing to say."""
    if kind == "news":
        return news_warning()
    if kind == "holiday":
        return holiday_note()
    if kind == "session":
        return session_note()
    if kind == "morning":
        return morning_note(context)
    return None
