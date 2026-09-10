"""SKLZ Labs — the Arabic channel.

Everything the English channels get, in Arabic, plus a daily content rhythm
so the channel is worth following on the days the engine stays flat.

WHAT IT DOES
============
1. Signals — every signal the other channels receive, formatted in Arabic.
2. Results — the daily closed-trade summary, in Arabic, losses included.
3. Content — four scheduled posts a day, written fresh by Claude:
      market open note · education · motivation · SKLZ Labs promo

WHY THE POSTS ARE GENERATED, NOT CANNED
=======================================
A rotating bank of pre-written posts is cheaper and safer, and it also reads
like a rotating bank of pre-written posts within a fortnight. The channel is
meant to make an Arabic-speaking trader feel that somebody is actually there,
so the market note references the day's real instruments and the education
post follows what the engine has actually been doing.

WHAT IT WILL NOT DO
===================
The promo post sells the software and the honesty, never a return. No post
may promise profit, quote a win rate the tracker cannot back, or imply that
copying the engine is safe. Those rules are in the system prompt AND checked
after generation, because a prompt is guidance and a filter is a guarantee.

CONFIG
======
    TG_ARABIC_CHAT     @sklzlabsarabic  (or -100...)
    TG_ARABIC_TOKEN    optional; falls back to TELEGRAM_BOT_TOKEN
    TG_ARABIC_HOURS    UTC hours for the daily posts, default "6,11,15,19"
    ANTHROPIC_API_KEY  for the content writer
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import urllib.request
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request, status

import policy
from routing import RoutingScope, resolve_destinations

router = APIRouter(prefix="/api/arabic", tags=["arabic"])

_state: dict = {"last": {}, "posted": 0, "skipped": 0, "last_error": ""}

MODEL = "claude-sonnet-4-5"

# ── the channel ─────────────────────────────────────────────────────
def _destination():
    """The Arabic channel, resolved. One lookup, one place."""
    dests = resolve_destinations(RoutingScope(language="ar"))
    return dests[0] if dests else None


def _chat() -> str:
    d = _destination()
    return d.chat_id if d else ""


_TOKEN_VARS = ("TELEGRAM_BOT_TOKEN", "TG_SALES_BOT_TOKEN",
               "TG_MIRROR_TOKEN", "TG_MIRROR2_TOKEN", "TG_MIRROR3_TOKEN")


def _token() -> str:
    """The bot that posts to the Arabic channel.

    The three-way fallback (TG_ARABIC_TOKEN, TG_ARABIC_TOKEN_ENV naming
    another variable, then TELEGRAM_BOT_TOKEN) now lives in the resolver
    with every other destination lookup. Unchanged behaviour, one home.
    """
    d = _destination()
    return d.token.reveal() if d else ""


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


def post(text: str, buttons: list | None = None) -> bool:
    """Send one message to the Arabic channel."""
    chat, token = _chat(), _token()
    if not chat or not token:
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"

    def _send(payload: dict) -> bool:
        try:
            req = urllib.request.Request(
                url, data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read().decode()).get("ok", False)
        except Exception as exc:  # noqa: BLE001
            _state["last_error"] = f"{type(exc).__name__}: {exc}"[:200]
            return False

    base = {"chat_id": chat, "text": text, "disable_web_page_preview": True}
    if buttons:
        base["reply_markup"] = {"inline_keyboard": buttons}
    if _send(dict(base, parse_mode="Markdown")):
        return True
    # Markdown refused: send it plain rather than drop the post entirely.
    return _send(base)


WELCOME_AR = """*SKLZ Labs* \u2014 \u0627\u0644\u062a\u062f\u0627\u0648\u0644 \u0628\u0623\u062f\u0648\u0627\u062a \u062d\u0642\u064a\u0642\u064a\u0629

\u0645\u0646\u0635\u0629 \u0628\u0631\u0645\u062c\u064a\u0629 \u0644\u0644\u0645\u062a\u062f\u0627\u0648\u0644\u064a\u0646: \u0646\u0633\u062e \u0627\u0644\u0635\u0641\u0642\u0627\u062a \u0639\u0644\u0649 MT5\u060c \u0645\u0624\u0634\u0631\u0627\u062a TradingView\u060c \u0648\u0645\u062d\u0644\u0644 \u0630\u0643\u0627\u0621 \u0627\u0635\u0637\u0646\u0627\u0639\u064a \u0644\u0642\u0631\u0627\u0621\u0629 \u0627\u0644\u0631\u0633\u0648\u0645 \u0627\u0644\u0628\u064a\u0627\u0646\u064a\u0629.

\u0641\u064a \u0647\u0630\u0647 \u0627\u0644\u0642\u0646\u0627\u0629:
\u2022 \u0625\u0634\u0627\u0631\u0627\u062a \u0628\u0645\u0633\u062a\u0648\u064a\u0627\u062a \u0648\u0627\u0636\u062d\u0629 \u2014 \u062f\u062e\u0648\u0644 \u0648\u0648\u0642\u0641 \u0648\u0647\u062f\u0641
\u2022 \u0645\u062a\u0627\u0628\u0639\u0629 \u0643\u0644 \u0635\u0641\u0642\u0629 \u062d\u062a\u0649 \u0625\u063a\u0644\u0627\u0642\u0647\u0627\u060c \u0627\u0644\u0631\u0627\u0628\u062d\u0629 \u0648\u0627\u0644\u062e\u0627\u0633\u0631\u0629
\u2022 \u0645\u062d\u062a\u0648\u0649 \u064a\u0648\u0645\u064a \u0639\u0646 \u0627\u0644\u0633\u0648\u0642 \u0648\u0625\u062f\u0627\u0631\u0629 \u0627\u0644\u0645\u062e\u0627\u0637\u0631\u0629

\u0644\u0627 \u0646\u0639\u062f\u0643 \u0628\u0623\u0631\u0628\u0627\u062d\u060c \u0648\u0644\u0627 \u0646\u062e\u0641\u064a \u0627\u0644\u062e\u0633\u0627\u0626\u0631. \u0627\u0644\u0623\u062f\u0648\u0627\u062a \u0644\u0643\u060c \u0648\u0627\u0644\u0642\u0631\u0627\u0631 \u0644\u0643.

\u0627\u0636\u063a\u0637 \u0627\u0644\u0632\u0631 \u0628\u0627\u0644\u0623\u0633\u0641\u0644 \u0648\u0623\u062c\u0628 \u0639\u0646 \u062b\u0644\u0627\u062b\u0629 \u0623\u0633\u0626\u0644\u0629 \u0642\u0635\u064a\u0631\u0629 \u0644\u0646\u062f\u0644\u0651\u0643 \u0639\u0644\u0649 \u0645\u0627 \u064a\u0646\u0627\u0633\u0628\u0643 \u2014 \u0648\u0625\u0646 \u0643\u0627\u0646 \u0627\u0644\u0623\u0646\u0633\u0628 \u0623\u0644\u0627 \u062a\u0634\u062a\u0631\u064a \u0634\u064a\u0626\u0627\u064b \u0627\u0644\u0622\u0646\u060c \u0633\u0646\u0642\u0648\u0644\u0647\u0627 \u0644\u0643.

_SKLZ Labs \u00b7 \u0628\u0631\u0627\u0645\u062c \u0641\u0642\u0637\u060c \u0648\u0644\u064a\u0633\u062a \u0646\u0635\u064a\u062d\u0629 \u0645\u0627\u0644\u064a\u0629 \u00b7 \u0627\u0644\u062a\u062f\u0627\u0648\u0644 \u064a\u0646\u0637\u0648\u064a \u0639\u0644\u0649 \u0645\u062e\u0627\u0637\u0631 \u062e\u0633\u0627\u0631\u0629_"""


def _bot_username_for_link() -> str:
    """The bot the funnel runs on — its @name, without the @."""
    name = os.environ.get("TG_FUNNEL_BOT", "").strip().lstrip("@")
    if name:
        return name
    got = _bot_username(_token())
    return got.lstrip("@") if got.startswith("@") else ""


def send_welcome(pin: bool = True) -> dict:
    """Post the Arabic welcome message with a Start button, and pin it.

    The button is a deep link carrying its own source tag, so every lead
    the Arabic channel produces is attributable rather than guessed at.
    """
    bot = _bot_username_for_link()
    if not bot:
        return {"ok": False, "error": "cannot resolve the funnel bot username"}
    url = f"https://t.me/{bot}?start=ar_channel"
    buttons = [[{"text": "\u0627\u0628\u062f\u0623 \u0627\u0644\u0622\u0646 \u2190", "url": url}]]

    chat, token = _chat(), _token()
    payload = {"chat_id": chat, "text": WELCOME_AR,
               "parse_mode": "Markdown",
               "disable_web_page_preview": True,
               "reply_markup": {"inline_keyboard": buttons}}
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.loads(r.read().decode())
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    if not d.get("ok"):
        return {"ok": False, "error": d.get("description", "telegram refused")}

    mid = (d.get("result") or {}).get("message_id")
    pinned = False
    if pin and mid:
        try:
            req2 = urllib.request.Request(
                f"https://api.telegram.org/bot{token}/pinChatMessage",
                data=json.dumps({"chat_id": chat, "message_id": mid,
                                 "disable_notification": True}).encode(),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req2, timeout=10) as r:
                pinned = json.loads(r.read().decode()).get("ok", False)
        except Exception:  # noqa: BLE001
            pinned = False
    return {"ok": True, "message_id": mid, "pinned": pinned, "link": url}


def post_photo(text: str, image_url: str) -> bool:
    """Send an image with the post as its caption.

    Telegram fetches and re-hosts the image at send time, so a temporary
    generator URL is fine here — the post keeps working long after the
    source link expires. Caption limit is 1024 characters; longer text is
    sent as a separate message under the photo rather than truncated,
    because a post cut mid-sentence reads worse than two messages.
    """
    chat, token = _chat(), _token()
    if not chat or not token or not image_url:
        return False
    caption, spill = text, ""
    if len(text) > 1000:
        cut = text.rfind("\n\n", 0, 1000)
        cut = cut if cut > 200 else 1000
        caption, spill = text[:cut], text[cut:].strip()
    payload = {"chat_id": chat, "photo": image_url,
               "caption": caption, "parse_mode": "Markdown"}
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendPhoto",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            ok = json.loads(r.read().decode()).get("ok", False)
    except Exception as exc:  # noqa: BLE001
        _state["last_error"] = f"photo: {type(exc).__name__}: {exc}"[:200]
        return False
    if ok and spill:
        post(spill)
    return ok


# ── signals ─────────────────────────────────────────────────────────
_CATEGORY_AR = {
    "forex": "العملات",
    "crypto": "العملات الرقمية",
    "stocks": "المؤشرات والأسهم",
    "metals": "المعادن",
}

_SIDE_AR = {"buy": "🟢 شراء", "sell": "🔴 بيع"}


def format_signal_ar(sig: dict) -> str:
    """The same signal the English channels get, in Arabic.

    Numbers stay in Western digits: every trading terminal an Arabic-speaking
    trader uses displays them that way, and a price they cannot paste into
    MT5 is worse than no price.
    """
    cat = _CATEGORY_AR.get(str(sig.get("category", "")).lower(),
                           str(sig.get("category", "")))
    side = _SIDE_AR.get(str(sig.get("side", "")).lower(), str(sig.get("side")))
    ez = (f"{sig.get('entry_low')} – {sig.get('entry_high')}"
          if sig.get("entry_low") != sig.get("entry_high")
          else str(sig.get("entry")))
    lines = [
        f"*إشارة SKLZ رادار* · {cat}",
        f"{side}  *{sig.get('symbol')}*  ({sig.get('timeframe', '')})",
        "",
        f"🎯 منطقة الدخول: `{ez}`",
        f"🛑 وقف الخسارة: `{sig.get('sl')}`",
        f"✅ جني الأرباح: `{sig.get('tp')}`",
        f"📊 نسبة العائد للمخاطرة ≈ {sig.get('rr', '—')}",
    ]
    if sig.get("note_ar"):
        lines += ["", sig["note_ar"]]
    lines += ["", "_SKLZ Labs · برامج فقط، وليست نصيحة مالية · "
                  "التداول ينطوي على مخاطر خسارة_"]
    return "\n".join(lines)


def send_signal(sig: dict) -> bool:
    if not _chat():
        return False
    return post(format_signal_ar(sig))


# ── results ─────────────────────────────────────────────────────────
def format_summary_ar(day: dict, week: dict) -> str:
    """Honest by construction, exactly like the English one."""
    lines = ["📊 *SKLZ — ملخص الإشارات اليومي*", ""]
    if day.get("closed", 0) == 0 and day.get("still_running", 0) == 0:
        lines.append("لم تُغلق أي إشارة اليوم.")
    else:
        run = day.get("still_running", 0)
        lines.append(
            f"اليوم: {day.get('wins', 0)} رابحة · {day.get('losses', 0)} خاسرة"
            + (f" · {run} ما زالت مفتوحة" if run else ""))
        lines.append(f"الصافي: *{day.get('net_pips', 0):+.1f} نقطة*")
    lines += ["",
              f"آخر ٧ أيام: {week.get('wins', 0)} رابحة / "
              f"{week.get('losses', 0)} خاسرة"
              + (f" ({week['win_rate']}%)"
                 if week.get("win_rate") is not None else ""),
              f"صافي الأسبوع: *{week.get('net_pips', 0):+.1f} نقطة*"]
    if (week.get("wins", 0) + week.get("losses", 0)) < 30:
        lines += ["", "_عينة صغيرة — تعامل مع النسب على هذا الأساس._"]
    lines += ["", "كل إشارة متابَعة حتى إغلاقها، بما في ذلك الخاسرة."]
    return "\n".join(lines)


# ── daily content ───────────────────────────────────────────────────
SLOTS = [
    ("market", "نظرة السوق"),
    ("education", "تعليم"),
    ("motivation", "تحفيز"),
    ("promo", "SKLZ Labs"),
]

SYSTEM_AR = """أنت كاتب المحتوى العربي لقناة SKLZ Labs على تيليجرام.

SKLZ Labs منصة برمجية للتداول: محرك آلي، نسخ صفقات على MT5، مؤشرات
TradingView، ومحلل ذكاء اصطناعي. الجمهور متداولون عرب، كثير منهم في
حسابات شركات التمويل (prop firms).

القواعد غير القابلة للتفاوض:
- لا تَعِد بأي ربح، ولا تذكر نسبة نجاح، ولا تلمّح إلى أن التداول آمن.
- لا تقل "مضمون" أو "بدون مخاطر" أو أي صيغة مشابهة.
- لا تختلق أرقاماً أو نتائج أو شهادات.
- اذكر المخاطرة بصدق عندما يكون ذلك مناسباً.
- اكتب كإنسان يفهم السوق، لا كإعلان.

- لا تذكر نسبة نجاح ولا نسبة صفقات رابحة إلى خاسرة بأي صيغة
  (مثل "ثلاث من عشر" أو "٧٠٪ من الصفقات"). لا يوجد رقم نستطيع إثباته.
- لا تصف حالة السوق اليوم كأنك تراها: لا تقل إن الذهب صاعد أو أن
  الدولار يتحرك بشكل معيّن. تحدث عن ما يستحق الانتباه وكيف يُدار،
  لا عن اتجاه تدّعي معرفته.

التنسيق (مهم، وإلا رُفض المنشور):
- ممنوع تماماً استخدام # للعناوين.
- ممنوع استخدام ** للتغميق. استخدم نجمة واحدة *هكذا* فقط.
- بدون قوائم مرقّمة أو رموز تنسيق أخرى.

الأسلوب: عربي فصيح واضح، جُمل قصيرة، نبرة هادئة واثقة بلا مبالغة.
اكتب منشوراً واحداً فقط، من ٤٠ إلى ٩٠ كلمة،
ويمكن استخدام رمز تعبيري واحد أو اثنين على الأكثر."""

PROMPTS = {
    "market": ("اكتب منشور نظرة على السوق لبداية اليوم. اذكر ما يستحق "
               "الانتباه اليوم بشكل عام (الذهب، العملات الرئيسية، "
               "المؤشرات) دون توقّع اتجاه محدد ودون أرقام مختلقة."),
    "education": ("اكتب منشوراً تعليمياً قصيراً عن مفهوم واحد في إدارة "
                  "المخاطر أو هيكل السوق: حجم الصفقة، وقف الخسارة، "
                  "السيولة، أو انضباط الخطة."),
    "motivation": ("اكتب منشوراً تحفيزياً واقعياً عن انضباط المتداول: "
                   "الصبر، تقبّل الخسارة، الالتزام بالخطة. بلا شعارات "
                   "فارغة ولا وعود."),
    "promo": ("اكتب منشوراً تعريفياً بخدمات SKLZ Labs: نسخ الصفقات على "
              "MT5، المؤشرات، ومحلل الذكاء الاصطناعي. ركّز على الشفافية "
              "وأن كل صفقة متابَعة حتى إغلاقها بما فيها الخاسرة. "
              "اختم بدعوة لزيارة sklzlabs.com"),
}

# The rules live in policy.py — one canonical ruleset for every channel,
# so Telegram, the website, the demo and any future surface cannot drift
# to different standards. A prompt is guidance; policy.validate is the
# guarantee. BANNED remains as a name for anything still referencing it.
BANNED = policy.BANNED_AR + policy.BANNED_LATIN_EXTRA


def _claude(prompt: str) -> str:
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        return ""
    body = json.dumps({
        "model": MODEL,
        "max_tokens": 500,
        "system": SYSTEM_AR,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body,
        headers={"Content-Type": "application/json",
                 "x-api-key": key,
                 "anthropic-version": "2023-06-01"})
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            d = json.loads(r.read().decode())
        return "".join(b.get("text", "") for b in d.get("content", [])
                       if b.get("type") == "text").strip()
    except Exception:  # noqa: BLE001
        return ""


def sanitize(text: str) -> str:
    """Make the text safe for Telegram's Markdown parser.

    Claude writes clean Markdown; Telegram speaks a much smaller dialect.
    A `# heading` renders literally and `**bold**` makes Telegram reject the
    whole message with "can't parse entities" — so the post never appears
    and nothing says why. Convert rather than trust.
    """
    out = []
    for line in text.split("\n"):
        s = line.strip()
        if s.startswith("#"):
            s = s.lstrip("#").strip()
            s = f"*{s}*" if s else ""
        out.append(s)
    t = "\n".join(out)
    t = t.replace("**", "*")
    # an odd number of * or _ breaks the parser just as thoroughly
    for ch in ("*", "_"):
        if t.count(ch) % 2:
            t = t.replace(ch, "")
    return t.strip()


def compose_debug(slot: str) -> tuple[str, str]:
    """Write one post and say precisely why it was rejected, if it was.

    "The writer returned nothing usable" covers six different failures —
    an empty model reply, a banned word, a disguised win-rate claim, a
    length limit — and telling them apart by guesswork wastes an evening.
    """
    raw = _claude(PROMPTS.get(slot, PROMPTS["market"]))
    if not raw:
        return "", "writer returned nothing (API key, model name, or timeout)"
    text = sanitize(raw)
    # strict=False: this is long-form education, where "risk 1-2% of your
    # capital" is a sentence the channel exists to say. Every other rule
    # — banned phrases, disguised ratios, percentages next to a
    # performance word — still applies.
    allowed, reason = policy.validate(text, strict=False)
    if not allowed:
        return "", f"policy: {reason}"
    if len(text) < 40:
        return "", f"too short after sanitising ({len(text)} chars)"
    if len(text) > 900:
        return "", f"too long ({len(text)} chars)"
    tail = ("\n\n_SKLZ Labs · برامج فقط، وليست نصيحة مالية_"
            if slot in ("promo", "market") else "")
    return text + tail, ""


def compose(slot: str) -> str:
    """Write one post for a slot, or return '' if it fails the rules."""
    text, _ = compose_debug(slot)
    return text


def _compose_old(slot: str) -> str:
    text = _claude(PROMPTS.get(slot, PROMPTS["market"]))
    if not text:
        return ""
    tail = ("\n\n_SKLZ Labs · برامج فقط، وليست نصيحة مالية_"
            if slot in ("promo", "market") else "")
    return text + tail


def _hours() -> list[int]:
    raw = os.environ.get("TG_ARABIC_HOURS", "6,11,15,19")
    out = []
    for part in raw.split(","):
        try:
            h = int(part.strip())
        except ValueError:
            continue
        if 0 <= h <= 23:
            out.append(h)
    return out or [6, 11, 15, 19]


async def content_loop(log=print) -> None:
    """One post per configured hour, each hour a different slot."""
    while True:
        try:
            hours = _hours()
            now = datetime.now(timezone.utc)
            today = now.strftime("%Y-%m-%d")
            if now.hour in hours:
                idx = hours.index(now.hour) % len(SLOTS)
                slot, label = SLOTS[idx]
                stamp = f"{today}:{slot}"
                if _state["last"].get(slot) != stamp:
                    text = compose(slot)
                    if text and post(text):
                        _state["last"][slot] = stamp
                        _state["posted"] += 1
                        log(f"[arabic] posted {label} ({slot})")
                    else:
                        # Mark the slot done anyway: a failed post must not
                        # retry every two minutes for an hour.
                        _state["last"][slot] = stamp
                        _state["skipped"] += 1
                        log(f"[arabic] skipped {slot} — empty, blocked by "
                            f"the content rules, or Telegram refused")
        except Exception as exc:  # noqa: BLE001
            _state["last_error"] = f"{type(exc).__name__}: {exc}"
            log(f"[arabic] loop error: {_state['last_error']}")
        await asyncio.sleep(120)


def start(app, log=print) -> None:
    @app.on_event("startup")
    async def _start_arabic() -> None:  # noqa: ANN202
        if _chat() and os.environ.get("TG_ARABIC_CONTENT", "1") != "0":
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
    """Is the Arabic channel wired and posting?"""
    return {"chat_configured": bool(_chat()),
            "token_configured": bool(_token()),
            "hours_utc": _hours(),
            "slots": [s for s, _ in SLOTS],
            "posted": _state["posted"],
            "skipped": _state["skipped"],
            "last_error": _state["last_error"]}


@router.get("/bots")
async def bots(request: Request) -> dict:
    """Which Telegram bot does each token variable belong to?

    Returns usernames only — never the tokens. Four similarly-named
    variables and no way to tell them apart is how the wrong bot ends up
    posting to the wrong channel.
    """
    _admin(request)
    out = {}
    for name in _TOKEN_VARS:
        val = os.environ.get(name, "").strip()
        out[name] = _bot_username(val) if val else "not set"
    out["_active_for_arabic"] = _bot_username(_token()) or "none"
    out["_channel"] = _chat() or "not set"
    return out


@router.post("/preview/{slot}")
async def preview(slot: str, request: Request) -> dict:
    """Write a post WITHOUT sending it — for checking tone before it's live."""
    _admin(request)
    if slot not in dict(SLOTS):
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"slot must be one of {[s for s, _ in SLOTS]}")
    text, reason = compose_debug(slot)
    return {"slot": slot, "text": text, "blocked": not text,
            "reason": reason, "chars": len(text)}


@router.post("/welcome")
async def welcome(request: Request, pin: bool = True) -> dict:
    """Post (and pin) the Arabic welcome message with the Start button."""
    _admin(request)
    res = send_welcome(pin=pin)
    if not res.get("ok"):
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, res.get("error", ""))
    return res


@router.post("/post/{slot}")
async def post_now(slot: str, request: Request,
                   image_url: str = "") -> dict:
    """Write and send one post immediately, optionally with an image."""
    _admin(request)
    if slot not in dict(SLOTS):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "unknown slot")
    text, reason = compose_debug(slot)
    if not text:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY,
                            f"post not written — {reason}")
    if image_url:
        if not post_photo(text, image_url):
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                "Telegram refused the photo — check the URL is publicly "
                "reachable and the bot can post media. "
                + _state.get("last_error", ""))
        _state["posted"] += 1
        return {"ok": True, "slot": slot, "text": text, "with_image": True}
    if not post(text):
        raise HTTPException(status.HTTP_502_BAD_GATEWAY,
                            "Telegram refused the message — is the bot an "
                            "admin of the channel?")
    _state["posted"] += 1
    return {"ok": True, "slot": slot, "text": text}
