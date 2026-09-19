"""SKLZ Labs — the English promotion channel (@mindsieg_protocol).

WHAT THIS IS
============
A second, English-only channel whose job is narrower than the Arabic one:
it sells the business layer to people who already trade. Three posts a
day, written fresh, every outbound link carrying the referral code so the
channel's revenue is attributable instead of guessed at.

WHY IT IS ITS OWN MODULE
========================
sklz_arabic.py posts signals, daily results AND content to an audience of
retail traders in Arabic. This channel posts no signals and no results —
it is a marketing surface, not a delivery surface — and its audience is
mostly signal providers evaluating a product. Sharing one module would
mean every function taking a language and an audience argument and two
sets of rules living in the same if-statement. They share the two things
that should be shared: policy.validate, and nothing else.

THE BOT SPLIT, AND WHY
======================
Telegram allows ONE webhook per bot. The funnel conversation needs that
webhook and the SKLZ sales bot already owns it, with a tested flow behind
it. So the channel bot (@iskraglobalagency_bot) only ever PUSHES — posts
and pins — and never receives updates. The pinned Start button is a deep
link into the sales bot, carrying its own source tag. Nothing about the
ISKRA bot's existing webhook is touched.

WHAT IT WILL NOT DO
===================
No profit claim, no win rate, no ROI, no "passive income", no invented
customer. Every post is checked by policy.validate AFTER generation,
because a prompt is guidance and a filter is a guarantee. The feature
list below is the ONLY thing the writer may describe — a marketing bot
that invents a feature is worse than a marketing bot that says nothing.

CONFIG
======
    TG_PROMO_CHAT      @mindsieg_protocol  (or -100...)
    TG_PROMO_TOKEN     the channel bot's token; the bot must be an admin
    TG_PROMO_HOURS     UTC hours, default "8,14,19"
    TG_PROMO_REF       referral code appended to every link
    TG_PROMO_ENABLED   "0" disables the daily loop
    TG_FUNNEL_BOT      username of the bot the funnel runs on
    ANTHROPIC_API_KEY  for the writer
"""
from __future__ import annotations

import asyncio
import json
import os
import re as _re
import urllib.request
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request, status

import policy
from aio import offload

router = APIRouter(prefix="/api/promo", tags=["promo"])

MODEL = "claude-sonnet-4-5"
SITE = "https://www.sklzlabs.com"

_state: dict = {"last": "", "posted": 0, "skipped": 0, "last_error": "",
                "last_text": ""}


# ── configuration ───────────────────────────────────────────────────
def _chat() -> str:
    return os.environ.get("TG_PROMO_CHAT", "").strip()


def _token() -> str:
    return os.environ.get("TG_PROMO_TOKEN", "").strip()


def _ref() -> str:
    return os.environ.get("TG_PROMO_REF", "").strip()


def url(path: str = "/", fragment: str = "") -> str:
    """A site link that carries the referral code.

    The code is what makes this channel measurable, so it is built in
    here rather than pasted into copy — a link written by hand in a
    prompt is a link that will eventually be written without the ref.
    """
    base = SITE + (path if path.startswith("/") else "/" + path)
    ref = _ref()
    if ref:
        base += ("&" if "?" in base else "?") + "ref=" + ref
    return base + (("#" + fragment) if fragment else "")


def _bot_username(token: str) -> str:
    if not token:
        return ""
    try:
        with urllib.request.urlopen(
                f"https://api.telegram.org/bot{token}/getMe", timeout=8) as r:
            d = json.loads(r.read().decode())
        return "@" + d.get("result", {}).get("username", "") if d.get("ok") \
            else "invalid token"
    except Exception:  # noqa: BLE001
        return "unreachable"


# ── sending ─────────────────────────────────────────────────────────
def _api(method: str, payload: dict) -> dict:
    tok = _token()
    if not tok:
        return {"ok": False, "description": "TG_PROMO_TOKEN not set"}
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{tok}/{method}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode())
    except Exception as exc:  # noqa: BLE001
        _state["last_error"] = f"{type(exc).__name__}: {exc}"[:200]
        return {"ok": False, "description": _state["last_error"]}


def post(text: str, buttons: list | None = None) -> dict:
    """One message to the channel. Markdown, falling back to plain.

    Telegram rejects the WHOLE message on a parse error and says only
    "can't parse entities", so a post that would simply have looked
    slightly plainer disappears instead. Retrying without parse_mode
    costs one call and saves the post.
    """
    chat = _chat()
    if not chat:
        return {"ok": False, "description": "TG_PROMO_CHAT not set"}
    base = {"chat_id": chat, "text": text, "disable_web_page_preview": True}
    if buttons:
        base["reply_markup"] = {"inline_keyboard": buttons}
    res = _api("sendMessage", dict(base, parse_mode="Markdown"))
    if res.get("ok"):
        return res
    return _api("sendMessage", base)


# ── what the writer is allowed to say ───────────────────────────────
# Everything below is visible in the product today. A post describing
# anything NOT on this list is a post making a promise nobody has to
# keep, so the writer is given this list and told it is exhaustive.
FACTS = """SKLZ LABS — what actually exists today:

FOR SIGNAL PROVIDERS (the main audience)
- A branded website, built for you, on your own brand.
- Pro Trader OS: the operating system behind that site — Signal Desk,
  Telegram Studio, AI Assistant, Academy, Community, Audience and Leads,
  Brand and Channel, Sell, Pricing.
- Signal Desk: the position command centre. You press once. The order
  goes to the broker, and the signal is published to your Telegram
  channel automatically after the broker confirms the fill.
- One message, not many: when you move the stop or close the trade, the
  SAME Telegram message updates — STATUS: OPEN becomes TRAILING ACTIVE
  becomes CLOSED. Subscribers are never asked to scroll for the newest
  post. No reposting.
- AI Assistant: drafts subscriber communication from your verified
  trades — explain this trade, moved to breakeven, stop changed, trade
  closed, daily summary, market update. You approve before anything is
  sent. Nothing sends itself.
- Multi-account copying available with Signal Desk implementation.
- Packages: SKLZ Signal Desk $499 setup + $49/month. SKLZ Signal Desk
  Pro $999 setup + $99/month. SKLZ Pro Trader OS $1,499 setup +
  $149/month.
- A public live demo exists on the site. It executes on an MT5 broker
  DEMO account. The automation is live; the funds are virtual.

FOR TRADERS BUYING TOOLS (the secondary audience)
- SKLZ Core $29/month: MT5 copy trading on the partner broker for one
  account, four TradingView indicators, TradeGPT chart analysis,
  signals, journal, AI reviews.
- SKLZ Plus $49/month: everything in Core plus crypto trading and
  crypto copy trading.
- SKLZ Pro $79/month: everything in Plus, MT5 copy on any broker, up to
  ten accounts at once — built for people running prop-firm evaluations.
- Annual pricing saves 20 percent. Seven-day money-back guarantee on a
  first purchase. Cancel from the billing portal.

WHAT IS NOT TRUE AND MUST NEVER BE WRITTEN
- No profit, income, ROI, win rate or performance figure of any kind.
- The public demo does NOT prove follower-account copying.
- This is software and automation. It is not financial advice, not a
  managed account, and not passive income."""

SYSTEM = """You write the English Telegram channel for SKLZ Labs.

SKLZ Labs sells the business system around someone else's trading. The
reader is usually a trader who already has a following, or wants one, and
is doing the distribution by hand. A smaller share are traders buying
tools for their own account.

NON-NEGOTIABLE
- Never promise profit, income, returns or results. Never quote a win
  rate, a success rate, an accuracy figure or any performance number.
- Never write "guaranteed", "risk-free", "no risk", "passive income",
  "set and forget", or any phrasing that implies trading is safe.
- Never invent a customer, a testimonial, a number of users, or a
  feature. Describe ONLY what the facts block lists.
- Never claim the public demo proves follower-account copying.
- Say "Multi-account copying available with Signal Desk implementation."
  exactly, when that subject comes up.

VOICE
Plain, specific, unhurried. You are describing a machine, not selling a
dream. The most persuasive sentence available is a true description of
what happens when someone presses the button — so use it. No hype words,
no countdowns, no scarcity, no "imagine if", no rhetorical questions
stacked three deep. One idea per post, carried to its end.

FORMAT — a post that breaks these is rejected
- Never use # for headings. Never use ** for bold: single *asterisks*
  only. No numbered lists.
- 45 to 95 words. At most two emoji, and none is fine.
- Do not write a link or a URL. A button is attached separately.
- Do not end with a call to action; the button is the call to action."""

# Five provider slots to two client slots: the channel is aimed at
# providers, and a rotation of seven against three posts a day means the
# same slot never lands at the same hour two days running.
SLOTS = [
    ("system",     "The business system"),
    ("desk",       "Signal Desk"),
    ("client",     "Tools for your own account"),
    ("whitelabel", "Your brand, your clients"),
    ("ai",         "AI assistant"),
    ("packages",   "Packages"),
    ("demo",       "The live demo"),
]

PROMPTS = {
    "system": ("Write a post about the gap between trading well and "
               "running a trading business — the website, the channel, "
               "the subscribers, the content, the support. Name what "
               "SKLZ Labs builds around the trading. Do not list every "
               "module; pick the two that matter most and be concrete."),
    "desk": ("Write a post about the Signal Desk: one press sends the "
             "order to the broker and publishes the signal to Telegram "
             "after the fill is confirmed, and the same message updates "
             "through OPEN, TRAILING ACTIVE and CLOSED instead of a new "
             "post each time. Make the no-reposting part the point."),
    "client": ("Write a post for a trader who is not running a channel "
               "and just wants better tools for their own account: copy "
               "trading on MT5, the TradingView indicators, TradeGPT "
               "chart analysis, the journal. Mention the plan that fits "
               "a prop-firm trader running several evaluations."),
    "whitelabel": ("Write a post about white label: your brand on the "
                   "site, your clients, your pricing, your channel. The "
                   "reader owns the relationship; SKLZ Labs is the "
                   "machinery underneath and stays invisible."),
    "ai": ("Write a post about the AI assistant drafting subscriber "
           "communication from verified trades — explain this trade, "
           "stop changed, trade closed, daily summary — with the trader "
           "approving before anything sends. The approval step is the "
           "point, not the drafting."),
    "packages": ("Write a post laying out the three provider packages "
                 "and who each is for: Signal Desk at $499 setup plus "
                 "$49 a month, Signal Desk Pro at $999 plus $99, Pro "
                 "Trader OS at $1,499 plus $149. Say plainly what "
                 "separates them."),
    "demo": ("Write a post about the public live demo on the site: a "
             "real order goes to an MT5 broker demo account and a real "
             "Telegram signal is published from it. State that the "
             "automation is live and the funds are virtual. Do not claim "
             "it proves follower-account copying."),
}

BUTTONS = {
    "system":     ("Ecosystem", "/"),
    "desk":       ("How Signal Desk works", "/signal-desk.html"),
    "client":     ("Plans", "/pricing.html"),
    "whitelabel": ("White label", "/signal-desk.html"),
    "ai":         ("Pro Trader OS", "/signal-desk.html"),
    "packages":   ("Packages", "/signal-desk.html"),
    "demo":       ("Open the live demo", "/signal-desk.html"),
}

# Phrases policy.validate does not cover because they claim nothing
# measurable — they just promise the wrong thing about trading.
_PROMO_BANNED = ("passive income", "set and forget", "set-and-forget",
                 "hands free", "hands-free", "while you sleep",
                 "life changing", "life-changing", "get rich",
                 "financial freedom", "quit your job", "easy money",
                 "proven system", "proves follower", "copies followers")


# A growth figure is a performance claim wearing different clothes.
# policy.validate only blocks a percentage sitting next to a word like
# "win" or "profit", which is right for a signal but wrong for a
# marketing channel: "subscribers grew 300% after switching" claims a
# result and names no banned word at all. A discount is NOT this — the
# channel has to be able to say annual billing saves 20 percent — so the
# rule is the percentage plus a growth verb, not the percentage alone.
_GROWTH = (r"(?:grew|grow|growth|increas|boost|gain|jump|surge|rose|rise|"
           r"doubl|tripl|multipli|up\s+by|more\s+subscribers?|"
           r"convert|conversion|roi|revenue|income|profit)")
_GROWTH_PCT = _re.compile(
    r"\d+(?:\.\d+)?\s*(?:%|percent)\D{0,40}?" + _GROWTH
    + r"|" + _GROWTH + r"\D{0,40}?\d+(?:\.\d+)?\s*(?:%|percent)",
    _re.IGNORECASE)


def claim_check(text: str) -> str:
    """Everything this channel refuses, in one place. '' means allowed.

    Used by the writer AND by the endpoint that sends human-approved
    copy, because an approved string is not an exempt string.
    """
    low = text.lower()
    for phrase in _PROMO_BANNED:
        if phrase in low:
            return f"promo rule: contains '{phrase}'"
    m = _GROWTH_PCT.search(text)
    if m:
        return f"promo rule: growth figure ({m.group(0).strip()[:40]!r})"
    allowed, reason = policy.validate(text, strict=False)
    return "" if allowed else f"policy: {reason}"


def _claude(prompt: str) -> str:
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        return ""
    body = json.dumps({
        "model": MODEL,
        "max_tokens": 600,
        "system": SYSTEM + "\n\n" + FACTS,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body,
        headers={"Content-Type": "application/json", "x-api-key": key,
                 "anthropic-version": "2023-06-01"})
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            d = json.loads(r.read().decode())
        return "".join(b.get("text", "") for b in d.get("content", [])
                       if b.get("type") == "text").strip()
    except Exception as exc:  # noqa: BLE001
        _state["last_error"] = f"writer: {type(exc).__name__}"
        return ""


def sanitize(text: str) -> str:
    """Telegram speaks a much smaller Markdown dialect than Claude writes.

    A '# heading' renders literally and '**bold**' makes Telegram reject
    the entire message, so the post never appears and nothing says why.
    Convert rather than trust.
    """
    out = []
    for line in text.split("\n"):
        s = line.strip()
        if s.startswith("#"):
            s = s.lstrip("#").strip()
            s = f"*{s}*" if s else ""
        out.append(s)
    t = "\n".join(out).replace("**", "*")
    for ch in ("*", "_"):
        if t.count(ch) % 2:
            t = t.replace(ch, "")
    while "\n\n\n" in t:
        t = t.replace("\n\n\n", "\n\n")
    return t.strip()


TAIL = "\n\n_Software and automation only. Not financial advice._"


def compose_debug(slot: str) -> tuple[str, str]:
    """Write one post, and say precisely why it was rejected if it was."""
    raw = _claude(PROMPTS.get(slot, PROMPTS["system"]))
    if not raw:
        return "", "writer returned nothing (API key, model name, or timeout)"
    text = sanitize(raw)
    if "http" in text.lower() or "sklzlabs.com" in text.lower():
        return "", "writer put a link in the body; the button carries the link"
    # strict=False for the same reason the Arabic channel uses it: this is
    # long-form marketing where a price is a sentence the channel exists
    # to say. Every banned phrase, disguised ratio and performance figure
    # rule still applies, plus this channel's own growth-figure rule.
    reason = claim_check(text)
    if reason:
        return "", reason
    if len(text) < 60:
        return "", f"too short after sanitising ({len(text)} chars)"
    if len(text) > 900:
        return "", f"too long ({len(text)} chars)"
    return text + TAIL, ""


def compose(slot: str) -> str:
    return compose_debug(slot)[0]


def buttons_for(slot: str) -> list:
    label, path = BUTTONS.get(slot, BUTTONS["system"])
    return [[{"text": label, "url": url(path)}],
            [{"text": "Find what fits me", "url": funnel_link()}]]


# ── the pinned post ─────────────────────────────────────────────────
def funnel_link() -> str:
    """Deep link into the sales bot, tagged with this channel.

    The payload is what makes a lead from this channel distinguishable
    from every other lead, so the link is never written without it.
    """
    bot = os.environ.get("TG_FUNNEL_BOT", "").strip().lstrip("@")
    if not bot:
        got = _bot_username(os.environ.get("TG_SALES_BOT_TOKEN", ""))
        bot = got.lstrip("@") if got.startswith("@") else ""
    return f"https://t.me/{bot}?start=mindsiege" if bot else ""


PINNED = """*SKLZ LABS*

You handle the trading. We build the business around it.

A branded website and a trader operating system. A Signal Desk that
sends the order to your broker and publishes the signal to your Telegram
channel after the fill — then keeps that same message updated through
OPEN, TRAILING ACTIVE and CLOSED, with no reposting. An AI assistant
that drafts subscriber communication from your verified trades and sends
nothing until you approve it.

If you are not running a channel, the same platform sells you the tools
on their own — copy trading, indicators, chart analysis, journal.

Tap below. Two minutes, a few questions, and you get the package that
actually fits — including being told that none of them does.

_Software and automation only. Not financial advice. Trading involves
risk of loss._"""


def send_pinned(pin: bool = True) -> dict:
    """Post the pinned Start message and pin it."""
    link = funnel_link()
    if not link:
        return {"ok": False,
                "error": "cannot resolve the funnel bot username — set "
                         "TG_FUNNEL_BOT"}
    buttons = [[{"text": "▶ Start — find what fits me", "url": link}],
               [{"text": "See the live demo",
                 "url": url("/signal-desk.html")}],
               [{"text": "Packages and pricing", "url": url("/pricing.html")}]]
    res = post(PINNED, buttons)
    if not res.get("ok"):
        return {"ok": False, "error": res.get("description", "refused")}
    mid = (res.get("result") or {}).get("message_id")
    pinned = False
    if pin and mid:
        pinned = _api("pinChatMessage",
                      {"chat_id": _chat(), "message_id": mid,
                       "disable_notification": True}).get("ok", False)
    return {"ok": True, "message_id": mid, "pinned": pinned, "link": link}


# ── the daily loop ──────────────────────────────────────────────────
def _hours() -> list[int]:
    raw = os.environ.get("TG_PROMO_HOURS", "8,14,19")
    out = []
    for part in raw.split(","):
        try:
            h = int(part.strip())
        except ValueError:
            continue
        if 0 <= h <= 23:
            out.append(h)
    return sorted(set(out)) or [8, 14, 19]


def slot_for(now: datetime) -> str:
    """Which slot this hour gets.

    Derived from the date rather than a counter, so a restart does not
    reset the rotation to the top and repeat yesterday. Seven slots
    against three posts a day means a slot lands at a different hour
    each time it comes round.
    """
    hours = _hours()
    if now.hour not in hours:
        return ""
    day = now.toordinal()
    idx = (day * len(hours) + hours.index(now.hour)) % len(SLOTS)
    return SLOTS[idx][0]


async def content_loop(log=print) -> None:
    while True:
        try:
            now = datetime.now(timezone.utc)
            slot = slot_for(now)
            stamp = f"{now.strftime('%Y-%m-%d')}:{now.hour}"
            if slot and _state["last"] != stamp:
                text, reason = await offload(compose_debug, slot)
                if text and (await offload(post, text,
                                           buttons_for(slot))).get("ok"):
                    _state["posted"] += 1
                    _state["last_text"] = text
                    log(f"[promo] posted {slot}")
                else:
                    _state["skipped"] += 1
                    log(f"[promo] skipped {slot} — {reason or 'Telegram refused'}")
                # Marked done either way: a failed post must not retry
                # every two minutes for the rest of the hour.
                _state["last"] = stamp
        except Exception as exc:  # noqa: BLE001
            _state["last_error"] = f"{type(exc).__name__}: {exc}"[:200]
            log(f"[promo] loop error: {_state['last_error']}")
        await asyncio.sleep(120)


def start(app, log=print) -> None:
    @app.on_event("startup")
    async def _start_promo() -> None:  # noqa: ANN202
        if _chat() and _token() and os.environ.get("TG_PROMO_ENABLED", "1") != "0":
            asyncio.create_task(content_loop(log=log))


# ── endpoints ───────────────────────────────────────────────────────
def _admin(request: Request) -> None:
    key = os.environ.get("SIGNAL_WEBHOOK_KEY", "")
    got = (request.headers.get("authorization", "")
           .replace("Bearer ", "").strip())
    if not key or got != key:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "admin only")


@router.get("/health")
async def health() -> dict:
    """Is the channel wired, and is the rotation where it should be?"""
    now = datetime.now(timezone.utc)
    return {"chat_configured": bool(_chat()),
            "token_configured": bool(_token()),
            "ref_configured": bool(_ref()),
            "funnel_link": funnel_link(),
            "hours_utc": _hours(),
            "slots": [s for s, _ in SLOTS],
            "slot_now": slot_for(now) or "(not a posting hour)",
            "posted": _state["posted"],
            "skipped": _state["skipped"],
            "last_error": _state["last_error"]}


@router.get("/bot")
async def bot(request: Request) -> dict:
    """Which bot is configured to post here? Username only, never a token."""
    _admin(request)
    return {"channel": _chat() or "not set",
            "posts_as": await offload(_bot_username, _token()) or "not set",
            "funnel_bot": funnel_link()}


@router.post("/preview/{slot}")
async def preview(slot: str, request: Request) -> dict:
    """Write a post WITHOUT sending it."""
    _admin(request)
    if slot not in dict(SLOTS):
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"slot must be one of {[s for s, _ in SLOTS]}")
    text, reason = await offload(compose_debug, slot)
    return {"slot": slot, "text": text, "blocked": not text,
            "reason": reason, "chars": len(text),
            "buttons": buttons_for(slot)}


@router.post("/post/{slot}")
async def post_now(slot: str, request: Request) -> dict:
    """Write and send one post immediately."""
    _admin(request)
    if slot not in dict(SLOTS):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "unknown slot")
    text, reason = await offload(compose_debug, slot)
    if not text:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY,
                            f"post not written — {reason}")
    res = await offload(post, text, buttons_for(slot))
    if not res.get("ok"):
        raise HTTPException(status.HTTP_502_BAD_GATEWAY,
                            "Telegram refused — is the bot an admin of the "
                            "channel? " + str(res.get("description", "")))
    _state["posted"] += 1
    return {"ok": True, "slot": slot, "text": text}


@router.post("/send")
async def send_exact(request: Request) -> dict:
    """Send an exact, already-approved body. Still policy-checked.

    The launch posts are reviewed by a human before they go out, and a
    reviewed post must be sent verbatim — regenerating it would send
    something nobody read. The policy check still runs, because an
    approved string is not an exempt string.
    """
    _admin(request)
    body = await request.json()
    text = (body.get("text") or "").strip()
    slot = (body.get("slot") or "system").strip()
    if not text:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "text is required")
    reason = claim_check(text)
    if reason:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, reason)
    res = await offload(post, text, buttons_for(slot))
    if not res.get("ok"):
        raise HTTPException(status.HTTP_502_BAD_GATEWAY,
                            str(res.get("description", "refused")))
    _state["posted"] += 1
    return {"ok": True, "message_id": (res.get("result") or {}).get("message_id")}


@router.post("/pin")
async def pin(request: Request, pin: bool = True) -> dict:
    """Post and pin the Start message."""
    _admin(request)
    res = await offload(send_pinned, pin=pin)
    if not res.get("ok"):
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, res.get("error", ""))
    return res
