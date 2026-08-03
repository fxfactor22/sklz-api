"""SKLZ — Telegram qualification bot.

Someone taps the pinned button, lands here, picks a language, answers two or
three short questions, and is routed to what actually fits them. Then a human
can pick it up.

THE APPROACH
============
This does not use urgency tactics. No countdowns, no "only 3 spots left", no
loss-aversion framing. The audience has seen all of it, and it is what every
other signals channel does — using it makes SKLZ sound like them.

What persuades here is being the only message in someone's feed that tells the
truth: naming the thing that is actually hard about their trading, saying
plainly what the tools do and do not do, and recommending nothing when nothing
fits. That is more convincing than pressure, and it is the only pitch
consistent with a platform whose whole position is honest numbers.

ON PERSONAL DATA
================
Email is asked for once, late, and only when someone has shown real interest.
Phone numbers are not collected at all — Telegram makes it easy, which makes
it easy to take more than is needed. Everything stored is deletable on request
via /forget.
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from supabase import Client

from db import get_supabase

router = APIRouter(prefix="/api/tgbot", tags=["telegram-bot"])

SITE = "https://www.sklzlabs.com"
BROKER = "https://mexatlantic.com/account/live-account?ibNum=9904917"
ADMIN = "https://t.me/skillscoin"


def _token() -> str:
    return os.environ.get("TG_SALES_BOT_TOKEN", "")


def _api(method: str, payload: dict) -> dict:
    tok = _token()
    if not tok:
        return {"ok": False, "reason": "TG_SALES_BOT_TOKEN not set"}
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{tok}/{method}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode())
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": str(exc)[:160]}


def send(chat_id: int, text: str, buttons: list | None = None,
         edit_id: int | None = None) -> dict:
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown",
               "disable_web_page_preview": True}
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": buttons}
    if edit_id:
        payload["message_id"] = edit_id
        return _api("editMessageText", payload)
    return _api("sendMessage", payload)


# ── copy, in three languages ────────────────────────────────────────
T = {
    "en": {
        "welcome": ("Welcome to *SKLZ Labs*.\n\n"
                    "Two questions and we will show you what fits. If nothing "
                    "does, we will say that too."),
        "q_trade": "What do you trade?",
        "a_forex": "Forex", "a_crypto": "Crypto",
        "a_both": "Both", "a_learning": "Still learning",
        "q_time": "How long have you been trading?",
        "t_new": "Under a year", "t_some": "1-3 years",
        "t_exp": "3+ years", "t_pro": "Professionally",
        "q_problem": "What is the actual problem right now?",
        "p_entries": "My entries",
        "p_exits": "I cut winners, hold losers",
        "p_consistency": "No consistency",
        "p_time": "No time to watch charts",
        "thanks": "Thank you. Here is the honest answer for your situation:",
        "email_ask": ("If you want the details by email, send it below. "
                      "Otherwise tap Skip \u2014 nothing is required."),
        "skip": "Skip",
        "email_ok": "Noted. We will not send you anything you did not ask for.",
        "cta_see": "See the tools",
        "cta_price": "Pricing",
        "cta_admin": "Talk to a human",
        "cta_broker": "Open a broker account",
        "forget_ok": "Deleted. Nothing about you is stored any more.",
        "restart": "Start again",
    },
    "ar": {
        "welcome": ("مرحبًا بك في *SKLZ Labs*.\n\n"
                    "سؤالان فقط ونعرض لك ما يناسبك. وإن لم يكن هناك ما يناسبك، "
                    "سنقول ذلك بصراحة."),
        "q_trade": "ما الذي تتداوله؟",
        "a_forex": "فوركس", "a_crypto": "كريبتو",
        "a_both": "الاثنان", "a_learning": "ما زلت أتعلّم",
        "q_time": "منذ متى وأنت تتداول؟",
        "t_new": "أقل من سنة", "t_some": "١-٣ سنوات",
        "t_exp": "أكثر من ٣ سنوات", "t_pro": "بشكل احترافي",
        "q_problem": "ما المشكلة الحقيقية حاليًا؟",
        "p_entries": "نقاط الدخول",
        "p_exits": "أغلق الرابحة مبكرًا وأبقي الخاسرة",
        "p_consistency": "لا يوجد ثبات في النتائج",
        "p_time": "لا وقت لمتابعة الشارت",
        "thanks": "شكرًا لك. هذه الإجابة الصادقة لحالتك:",
        "email_ask": ("إن أردت التفاصيل عبر البريد، أرسله أدناه. "
                      "أو اضغط تخطّي \u2014 لا شيء إجباري."),
        "skip": "تخطّي",
        "email_ok": "تم الحفظ. لن نرسل لك ما لم تطلبه.",
        "cta_see": "شاهد الأدوات",
        "cta_price": "الأسعار",
        "cta_admin": "تحدث مع شخص",
        "cta_broker": "افتح حساب وساطة",
        "forget_ok": "تم الحذف. لم يعد هناك أي بيانات عنك.",
        "restart": "ابدأ من جديد",
    },
    "ru": {
        "welcome": ("Добро пожаловать в *SKLZ Labs*.\n\n"
                    "Два вопроса — и мы покажем, что вам подходит. "
                    "Если ничего не подходит, мы скажем прямо."),
        "q_trade": "Чем вы торгуете?",
        "a_forex": "Форекс", "a_crypto": "Крипто",
        "a_both": "И тем, и другим", "a_learning": "Ещё учусь",
        "q_time": "Как давно вы торгуете?",
        "t_new": "Меньше года", "t_some": "1-3 года",
        "t_exp": "Более 3 лет", "t_pro": "Профессионально",
        "q_problem": "В чём сейчас настоящая проблема?",
        "p_entries": "Точки входа",
        "p_exits": "Рано закрываю прибыль, держу убытки",
        "p_consistency": "Нет стабильности",
        "p_time": "Нет времени следить за графиками",
        "thanks": "Спасибо. Вот честный ответ для вашей ситуации:",
        "email_ask": ("Если хотите детали на почту — отправьте адрес. "
                      "Или нажмите Пропустить, это необязательно."),
        "skip": "Пропустить",
        "email_ok": "Записали. Мы не пришлём ничего, о чём вы не просили.",
        "cta_see": "Посмотреть инструменты",
        "cta_price": "Цены",
        "cta_admin": "Написать человеку",
        "cta_broker": "Открыть счёт у брокера",
        "forget_ok": "Удалено. Данных о вас больше не хранится.",
        "restart": "Начать заново",
    },
}


def t(lang: str, key: str) -> str:
    return T.get(lang, T["en"]).get(key, T["en"].get(key, key))


# ── the recommendation: honest, and sometimes "nothing" ─────────────
RECO = {
    "en": {
        "entries": ("Entry timing is the most common thing people want to fix, "
                    "and the hardest to fix with indicators alone. What helps "
                    "is seeing whether a breakout has real participation behind "
                    "it. Our scanner reads live order book depth and flags when "
                    "buying is being absorbed \u2014 which is when a breakout "
                    "usually fails.\n\n"
                    "*Honest caveat:* our own research on 27 million ticks found "
                    "most entry setups are coin-flips out of sample. Better "
                    "entries help less than most people expect."),
        "exits": ("Cutting winners and holding losers is a risk problem, not an "
                  "analysis problem \u2014 and it is the one that actually "
                  "decides whether an account survives.\n\n"
                  "The journal flags this pattern in your own trades with the "
                  "numbers attached. Most people are surprised by what it shows. "
                  "That is the tool for your situation, not the signals."),
        "consistency": ("Inconsistency usually means position sizing varies, not "
                        "that the strategy is broken. The journal shows you that "
                        "directly \u2014 win rate against average win and average "
                        "loss, per setup.\n\n"
                        "If the numbers come back looking like a coin-flip, we "
                        "will tell you. That is the point of it."),
        "time": ("If you cannot watch charts, signals with real levels are the "
                 "honest fit \u2014 entry zone, stop, target, and every call "
                 "tracked to its outcome including the ones that missed.\n\n"
                 "*What we will not claim:* that following signals is passive "
                 "income. You still decide what to take and how much to risk."),
        "learning": ("If you are still learning, do not buy signals. Start with "
                     "the free lot calculator and the Academy, and paper trade "
                     "until you have a hundred trades in a journal.\n\n"
                     "We would rather say that than sell you a subscription you "
                     "are not ready to use."),
    },
    "ar": {
        "entries": ("توقيت الدخول هو أكثر ما يريد المتداولون إصلاحه، وأصعب ما "
                    "يمكن إصلاحه بالمؤشرات وحدها. ما يساعد فعلاً هو معرفة ما إذا "
                    "كان الاختراق مدعومًا بمشاركة حقيقية. ماسحنا يقرأ عمق دفتر "
                    "الأوامر ويحذّر عندما يُمتص الشراء \u2014 وهي اللحظة التي "
                    "يفشل فيها الاختراق عادة.\n\n"
                    "*ملاحظة صادقة:* بحثنا على ٢٧ مليون تِك وجد أن معظم إعدادات "
                    "الدخول عشوائية خارج العينة."),
        "exits": ("إغلاق الصفقات الرابحة مبكرًا والاحتفاظ بالخاسرة مشكلة إدارة "
                  "مخاطر، لا مشكلة تحليل \u2014 وهي التي تحدد بقاء الحساب.\n\n"
                  "السجل يكشف هذا النمط في صفقاتك بالأرقام. هذه هي الأداة "
                  "المناسبة لحالتك، وليست الإشارات."),
        "consistency": ("عدم الثبات غالبًا سببه تفاوت حجم الصفقات، لا فشل "
                        "الاستراتيجية. السجل يوضح ذلك مباشرة.\n\n"
                        "وإن أظهرت الأرقام أن النتائج عشوائية، سنقول ذلك."),
        "time": ("إن لم يكن لديك وقت لمتابعة الشارت، فالإشارات بمستويات حقيقية "
                 "هي الخيار الصادق \u2014 منطقة دخول ووقف وهدف، وكل إشارة "
                 "متابعة حتى نتيجتها.\n\n"
                 "*ما لن ندّعيه:* أن اتباع الإشارات دخل سلبي."),
        "learning": ("إن كنت ما زلت تتعلّم، لا تشترِ إشارات. ابدأ بحاسبة اللوت "
                     "المجانية والأكاديمية، وتدرّب حتى تجمع مئة صفقة في سجل.\n\n"
                     "نفضّل أن نقول ذلك على أن نبيعك اشتراكًا لست مستعدًا له."),
    },
    "ru": {
        "entries": ("Тайминг входа — то, что чаще всего хотят исправить, и то, "
                    "что сложнее всего исправить одними индикаторами. Помогает "
                    "понимание того, есть ли за пробоем реальное участие. Наш "
                    "сканер читает глубину стакана и отмечает, когда покупки "
                    "поглощаются — именно тогда пробой обычно и не удаётся.\n\n"
                    "*Честная оговорка:* наше исследование на 27 млн тиков "
                    "показало, что большинство входных сетапов вне выборки "
                    "неотличимы от случайности."),
        "exits": ("Рано закрывать прибыль и держать убытки — это проблема риска, "
                  "а не анализа, и именно она решает, выживет ли счёт.\n\n"
                  "Журнал показывает этот паттерн в ваших сделках с цифрами. "
                  "Это подходящий инструмент, а не сигналы."),
        "consistency": ("Нестабильность обычно означает разный размер позиции, "
                        "а не поломанную стратегию. Журнал показывает это "
                        "напрямую.\n\n"
                        "Если цифры окажутся похожи на подбрасывание монеты, "
                        "мы так и скажем."),
        "time": ("Если нет времени на графики, честный вариант — сигналы с "
                 "реальными уровнями: зона входа, стоп, цель, и каждый сигнал "
                 "отслеживается до результата.\n\n"
                 "*Чего мы не обещаем:* что следование сигналам — пассивный доход."),
        "learning": ("Если вы ещё учитесь — не покупайте сигналы. Начните с "
                     "бесплатного калькулятора лота и Академии, и торгуйте на "
                     "демо, пока не наберёте сто сделок в журнале.\n\n"
                     "Мы предпочтём сказать это, чем продать подписку, которой "
                     "вы пока не воспользуетесь."),
    },
}


def reco(lang: str, problem: str, experience: str) -> str:
    if experience == "new" or problem == "learning":
        return RECO.get(lang, RECO["en"])["learning"]
    return RECO.get(lang, RECO["en"]).get(problem, RECO["en"]["entries"])


# ── state ───────────────────────────────────────────────────────────
def _get_lead(sb: Client, chat_id: int) -> dict:
    try:
        rows = (sb.table("tg_leads").select("*")
                .eq("chat_id", chat_id).execute()).data or []
        return rows[0] if rows else {}
    except Exception:
        return {}


def _save_lead(sb: Client, chat_id: int, patch: dict) -> None:
    patch = {**patch, "chat_id": chat_id,
             "updated_at": datetime.now(timezone.utc).isoformat()}
    try:
        sb.table("tg_leads").upsert(patch, on_conflict="chat_id").execute()
    except Exception:
        pass


# ── flow ────────────────────────────────────────────────────────────
def _lang_buttons() -> list:
    return [[{"text": "English", "callback_data": "lang:en"}],
            [{"text": "\u0627\u0644\u0639\u0631\u0628\u064a\u0629", "callback_data": "lang:ar"}],
            [{"text": "\u0420\u0443\u0441\u0441\u043a\u0438\u0439", "callback_data": "lang:ru"}]]


def _trade_buttons(lang: str) -> list:
    return [[{"text": t(lang, "a_forex"), "callback_data": "trade:forex"},
             {"text": t(lang, "a_crypto"), "callback_data": "trade:crypto"}],
            [{"text": t(lang, "a_both"), "callback_data": "trade:both"},
             {"text": t(lang, "a_learning"), "callback_data": "trade:learning"}]]


def _time_buttons(lang: str) -> list:
    return [[{"text": t(lang, "t_new"), "callback_data": "exp:new"},
             {"text": t(lang, "t_some"), "callback_data": "exp:some"}],
            [{"text": t(lang, "t_exp"), "callback_data": "exp:exp"},
             {"text": t(lang, "t_pro"), "callback_data": "exp:pro"}]]


def _problem_buttons(lang: str) -> list:
    return [[{"text": t(lang, "p_entries"), "callback_data": "prob:entries"}],
            [{"text": t(lang, "p_exits"), "callback_data": "prob:exits"}],
            [{"text": t(lang, "p_consistency"), "callback_data": "prob:consistency"}],
            [{"text": t(lang, "p_time"), "callback_data": "prob:time"}]]


def _final_buttons(lang: str) -> list:
    return [[{"text": t(lang, "cta_price"), "url": f"{SITE}/pricing.html"}],
            [{"text": t(lang, "cta_see"), "url": SITE}],
            [{"text": t(lang, "cta_admin"), "url": ADMIN}],
            [{"text": t(lang, "cta_broker"), "url": BROKER}]]


@router.post("/webhook/{secret}")
async def webhook(secret: str, request: Request,
                  sb: Client = Depends(get_supabase)) -> dict:
    """Telegram posts every update here."""
    expected = os.environ.get("TG_SALES_WEBHOOK_SECRET", "")
    if not expected or secret != expected:
        return {"ok": False}

    try:
        upd = await request.json()
    except Exception:
        return {"ok": True}

    # ── button presses ──
    cq = upd.get("callback_query")
    if cq:
        chat_id = cq["message"]["chat"]["id"]
        msg_id = cq["message"]["message_id"]
        data = cq.get("data") or ""
        _api("answerCallbackQuery", {"callback_query_id": cq["id"]})
        lead = _get_lead(sb, chat_id)
        lang = lead.get("lang") or "en"

        if data.startswith("lang:"):
            lang = data.split(":", 1)[1]
            _save_lead(sb, chat_id, {"lang": lang,
                                     "username": (cq.get("from") or {}).get("username", ""),
                                     "step": "trade"})
            send(chat_id, t(lang, "welcome") + "\n\n*" + t(lang, "q_trade") + "*",
                 _trade_buttons(lang), edit_id=msg_id)

        elif data.startswith("trade:"):
            _save_lead(sb, chat_id, {"trades": data.split(":", 1)[1], "step": "exp"})
            send(chat_id, "*" + t(lang, "q_time") + "*",
                 _time_buttons(lang), edit_id=msg_id)

        elif data.startswith("exp:"):
            _save_lead(sb, chat_id, {"experience": data.split(":", 1)[1],
                                     "step": "problem"})
            send(chat_id, "*" + t(lang, "q_problem") + "*",
                 _problem_buttons(lang), edit_id=msg_id)

        elif data.startswith("prob:"):
            problem = data.split(":", 1)[1]
            lead = _get_lead(sb, chat_id)
            _save_lead(sb, chat_id, {"problem": problem, "step": "done"})
            answer = reco(lang, problem, lead.get("experience") or "")
            send(chat_id, t(lang, "thanks") + "\n\n" + answer,
                 _final_buttons(lang), edit_id=msg_id)
            send(chat_id, t(lang, "email_ask"),
                 [[{"text": t(lang, "skip"), "callback_data": "skip:email"}]])

        elif data == "skip:email":
            _save_lead(sb, chat_id, {"step": "closed"})
            send(chat_id, "\u2014", edit_id=msg_id)
        return {"ok": True}

    # ── plain messages ──
    m = upd.get("message") or {}
    chat_id = (m.get("chat") or {}).get("id")
    text = (m.get("text") or "").strip()
    if not chat_id:
        return {"ok": True}

    lead = _get_lead(sb, chat_id)
    lang = lead.get("lang") or "en"

    if text.startswith("/start"):
        _save_lead(sb, chat_id, {"step": "lang",
                                 "source": text.replace("/start", "").strip() or "direct",
                                 "username": (m.get("from") or {}).get("username", "")})
        send(chat_id, "*SKLZ Labs*\n\nChoose your language \u00b7 "
                      "\u0627\u062e\u062a\u0631 \u0644\u063a\u062a\u0643 \u00b7 "
                      "\u0412\u044b\u0431\u0435\u0440\u0438\u0442\u0435 \u044f\u0437\u044b\u043a",
             _lang_buttons())
        return {"ok": True}

    if text.startswith("/forget"):
        try:
            sb.table("tg_leads").delete().eq("chat_id", chat_id).execute()
        except Exception:
            pass
        send(chat_id, t(lang, "forget_ok"))
        return {"ok": True}

    # an email, offered voluntarily at the end
    if lead.get("step") == "done" and "@" in text and "." in text.split("@")[-1]:
        _save_lead(sb, chat_id, {"email": text[:120], "step": "closed"})
        send(chat_id, t(lang, "email_ok"), _final_buttons(lang))
        return {"ok": True}

    send(chat_id, "/start", _lang_buttons())
    return {"ok": True}


@router.get("/leads")
async def leads(sb: Client = Depends(get_supabase)) -> dict:
    """Who came through, and where they stopped."""
    try:
        rows = (sb.table("tg_leads").select("*")
                .order("updated_at", desc=True).limit(200).execute()).data or []
    except Exception:
        rows = []
    by_step: dict = {}
    for r in rows:
        by_step[r.get("step") or "?"] = by_step.get(r.get("step") or "?", 0) + 1
    return {"leads": rows, "count": len(rows), "by_step": by_step,
            "note": ("'closed' means they finished the flow. A large number "
                     "stuck at one step is the thing to fix.")}
