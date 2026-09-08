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

router = APIRouter(prefix="/api/arabic", tags=["arabic"])

_state: dict = {"last": {}, "posted": 0, "skipped": 0, "last_error": ""}

MODEL = "claude-sonnet-4-5"

# ── the channel ─────────────────────────────────────────────────────
def _chat() -> str:
    return os.environ.get("TG_ARABIC_CHAT", "").strip()


def _token() -> str:
    return (os.environ.get("TG_ARABIC_TOKEN", "").strip()
            or os.environ.get("TELEGRAM_BOT_TOKEN", "").strip())


def post(text: str) -> bool:
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
    if _send(dict(base, parse_mode="Markdown")):
        return True
    # Markdown refused: send it plain rather than drop the post entirely.
    return _send(base)


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

# Any of these in generated text means the post does not go out. A prompt
# is guidance; this is the guarantee.
BANNED = ["مضمون", "بدون مخاطر", "بلا مخاطر", "ربح مؤكد", "أرباح مضمونة",
          "لا خسارة", "استثمر معنا", "تضاعف", "guaranteed", "risk-free"]


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


_NUM = r"(?:[0-9٠-٩]+|واحدة|اثنتين|اثنتان|ثلاث|أربع|خمس|ست|سبع|ثمان|تسع|عشر)"
# "three trades out of ten lose" is a 70% win-rate claim in disguise.
_RATIO = re.compile(
    _NUM + r"\s*(?:صفقات|صفقة|مرات|مرة)?\s*من\s*(?:كل\s*)?"
    + _NUM + r"|" + _NUM + r"\s*من\s*أصل\s*" + _NUM)
_CLAIM_WORD = r"(?:نجاح|رابح|ربح|دقة|إصابة|خسار)"
# a percentage anywhere near a performance word, in either order
_PCT_CLAIM = re.compile(
    r"[0-9٠-٩]+\s*[%٪].{0,30}" + _CLAIM_WORD
    + r"|" + _CLAIM_WORD + r".{0,30}[0-9٠-٩]+\s*[%٪]")


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


def compose(slot: str) -> str:
    """Write one post for a slot, or return '' if it fails the rules."""
    text = _claude(PROMPTS.get(slot, PROMPTS["market"]))
    if not text:
        return ""
    text = sanitize(text)
    low = text.lower()
    if any(b.lower() in low for b in BANNED):
        return ""
    # "three trades in ten lose" is a 70% win rate claim wearing a disguise.
    # No number we cannot evidence goes on a channel selling a trading tool.
    if _RATIO.search(text) or _PCT_CLAIM.search(text):
        return ""
    if len(text) > 900 or len(text) < 40:
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


@router.post("/preview/{slot}")
async def preview(slot: str, request: Request) -> dict:
    """Write a post WITHOUT sending it — for checking tone before it's live."""
    _admin(request)
    if slot not in dict(SLOTS):
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"slot must be one of {[s for s, _ in SLOTS]}")
    text = compose(slot)
    return {"slot": slot, "text": text,
            "blocked": not text,
            "note": ("empty means the writer failed or the content rules "
                     "rejected it" if not text else "")}


@router.post("/post/{slot}")
async def post_now(slot: str, request: Request) -> dict:
    """Write and send one post immediately."""
    _admin(request)
    if slot not in dict(SLOTS):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "unknown slot")
    text = compose(slot)
    if not text:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY,
                            "the writer returned nothing usable")
    if not post(text):
        raise HTTPException(status.HTTP_502_BAD_GATEWAY,
                            "Telegram refused the message — is the bot an "
                            "admin of the channel?")
    _state["posted"] += 1
    return {"ok": True, "slot": slot, "text": text}
