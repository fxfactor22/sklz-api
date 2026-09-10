"""The canonical financial-claim ruleset.

One place that decides whether a customer-facing string may go out. It
runs on the FINAL RENDERED STRING, after variables, prices and free text
are inserted — a template with no numbers in it passes every check ever
written until a variable arrives.

Deterministic and in-process on purpose. The signal path is latency
sensitive and the demo's best moment is a visible round trip, so no
network hop and no LLM sits between a confirmed fill and its message.

Same rules as the Arabic channel's filters, generalised to Arabic and
English and to any rendered text rather than only AI output.
"""
from __future__ import annotations

import re

# Phrases that are claims however they are dressed.
BANNED_EN = [
    "guaranteed profit", "guaranteed return", "guaranteed win", "risk-free",
    "risk free", "no risk", "cannot lose", "can't lose", "sure thing",
    "win rate", "success rate", "accuracy rate", "track record",
    "backtested return", "expected return", "profit factor",
    "licensed by", "regulated by", "fca approved", "sec approved",
]

BANNED_AR = [
    "مضمون", "بدون مخاطر", "بلا مخاطر", "ربح مؤكد", "أرباح مضمونة",
    "لا خسارة", "نسبة نجاح", "نسبة الربح", "معدل النجاح",
]

_NUM_AR = (r"(?:[0-9٠-٩]+|واحدة|اثنتين|اثنتان|ثلاث|أربع|خمس|ست|سبع|ثمان"
           r"|تسع|عشر)")

# "three trades out of ten" is a 70% win rate with the % filed off.
_RATIO_AR = re.compile(
    _NUM_AR + r"\s*(?:صفقات|صفقة|مرات|مرة)?\s*من\s*(?:كل\s*)?" + _NUM_AR
    + r"|" + _NUM_AR + r"\s*من\s*أصل\s*" + _NUM_AR)
_RATIO_EN = re.compile(
    r"\b\d+\s*(?:out\s*of|of|in)\s*\d+\b\s*(?:trades?|signals?|wins?)?"
    r"|\b\d+\s*(?:trades?|signals?)\s*(?:out\s*of|of|in)\s*\d+\b",
    re.IGNORECASE)

_CLAIM_AR = r"(?:نجاح|رابح|ربح|دقة|إصابة|خسار)"
_PCT_AR = re.compile(r"[0-9٠-٩]+\s*[%٪].{0,30}" + _CLAIM_AR
                     + r"|" + _CLAIM_AR + r".{0,30}[0-9٠-٩]+\s*[%٪]")
_CLAIM_EN = r"(?:win|won|profit|success|accura|return|gain|hit rate)"
_PCT_EN = re.compile(r"\d+\s*%.{0,30}" + _CLAIM_EN
                     + r"|" + _CLAIM_EN + r".{0,30}\d+\s*%", re.IGNORECASE)

# Results, not intentions. "+80 pips" is a performance figure.
# "Take profit: 1.09900" is an ORDER FIELD and must survive — an early
# version blocked the contract's own example signal because "profit:"
# matched. The negative lookbehind is the whole difference between a
# policy that protects the product and one that blocks it.
_RESULT = re.compile(
    r"[+\-]\s*\d+(?:\.\d+)?\s*(?:pips?|points?|r\b|%)"
    r"|\bclosed\s*[+\-]?\s*\d+"
    r"|(?<!take\s)(?<!take_)\b(?:net\s+profit|pnl|p/l|balance|equity|"
    r"realized|realised)\s*[:=]\s*[+\-]?\s*[\d$]",
    re.IGNORECASE)

# A bare percentage in a short customer-facing field is a claim far more
# often than it is anything else: "desk is running at 82% today" carries
# no claim word for a proximity rule to find. Order fields never contain
# a percent sign, so refusing one here costs nothing.
_BARE_PCT = re.compile(r"\d+(?:\.\d+)?\s*[%٪]")


def validate(text: str, strict: bool = True) -> tuple[bool, str]:
    """Return (allowed, reason). Reason is a machine code when blocked."""
    if not text or not text.strip():
        return False, "empty_text"
    low = text.lower()

    for phrase in BANNED_EN:
        if phrase in low:
            return False, "banned_phrase"
    for phrase in BANNED_AR:
        if phrase in text:
            return False, "banned_phrase"

    if _RATIO_AR.search(text) or _RATIO_EN.search(text):
        return False, "implied_win_rate"
    if _PCT_AR.search(text) or _PCT_EN.search(text):
        return False, "performance_percentage"
    if _RESULT.search(text):
        return False, "performance_figure"
    if strict and _BARE_PCT.search(text):
        return False, "percentage_claim"
    return True, ""


def is_clean(text: str, strict: bool = True) -> bool:
    return validate(text, strict)[0]
