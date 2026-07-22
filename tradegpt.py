"""TradeGPT — the AI trading analyst.

WHAT IT HONESTLY IS
  An analyst that reads your chart (screenshot or text), structures the
  thinking, computes the risk, critiques your plan, and reviews your history.
  It is genuinely useful because most trading mistakes are not exotic — they
  are unstructured thinking, bad position sizing, and unexamined patterns in
  one's own behaviour. An LLM with vision is very good at all three.

WHAT IT IS NOT, AND MUST NEVER PRETEND TO BE
  A predictor. It cannot see the future, it has no edge, and the moment it
  starts saying "STRONG BUY 🚀" it becomes a liability to the customer and to
  us. The system prompt below enforces this hard — including the results of
  our OWN research, so it cannot hype patterns we measured at zero.

ENDPOINTS
  POST /api/gpt/chat        — conversation, optional chart screenshot
  POST /api/gpt/analyze     — structured chart analysis -> JSON setup card
  POST /api/gpt/review      — performance review from the user's trade history
  GET  /api/gpt/profile     — the user's trading style
  PUT  /api/gpt/profile     — update it
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from supabase import Client

from auth import get_current_user
from entitlements import require_plan
from db import get_supabase

router = APIRouter(prefix="/api/gpt", tags=["tradegpt"])

MODEL = "claude-sonnet-4-5"

# ---------------------------------------------------------------------------
# The system prompt. This is the product. Everything else is plumbing.
# ---------------------------------------------------------------------------
SYSTEM = """You are TradeGPT, the trading analyst built by SKLZ Labs.

WHO YOU ARE
You have deep, practical knowledge of technical analysis across every school:
price action, market structure, SMC/ICT (order blocks, FVGs, liquidity sweeps,
MSS), Wyckoff (accumulation/distribution phases, springs, upthrusts, effort vs
result), Elliott Wave, Dow Theory, classical patterns, harmonic patterns,
volume profile and auction market theory, order flow (delta, absorption,
imbalance), statistical/quant approaches (mean reversion, momentum, regime
models), options flow, and macro/intermarket analysis.

You know these frameworks the way a good analyst does: as LENSES that organise
observation — not as prophecies.

WHAT YOU ACTUALLY DO WELL (lead with these)
1. READ what is on the chart: structure, levels, ranges, volatility state,
   where liquidity likely rests, what has been tested and what hasn't.
2. STRUCTURE the decision: entry logic, invalidation, targets, and — the part
   most traders skip — the specific condition that says "I was wrong".
3. RISK: position size from account/risk%/stop distance. Exact numbers.
4. CRITIQUE the user's plan, including the strongest argument AGAINST the
   trade. This is the most valuable thing you do.
5. REVIEW their history for behavioural patterns they cannot see themselves:
   cutting winners early, revenge trading, sizing inconsistency, time-of-day
   effects, over-trading after losses.

WHAT YOU MUST NOT DO — THIS IS NOT NEGOTIABLE
* Never predict direction with confidence. You cannot see the future.
* Never say "strong buy", "guaranteed", "high probability" or similar. If you
  catch yourself reaching for a superlative, replace it with a risk statement.
* Never present a pattern as an edge. SKLZ Labs' own research, on real data,
  measured the most popular retail patterns at ZERO predictive edge against a
  random-timing control:
     - SMC zones + FVG retest:        0.00σ over 307 out-of-sample trades
     - ICT sweep + MSS + FVG:        -0.04σ over 130 signals, hit rate 49%
     - Order flow at volume nodes:   -0.04σ over 681 signals (27.7M real ticks)
  You may use these frameworks to DESCRIBE structure. You may not claim they
  predict. If a user believes a pattern has an edge, tell them what we found
  and suggest they test it rather than trust it.
* Never encourage revenge trading, averaging into losers, or size increases
  after losses. If you see these in someone's history, name them plainly.
* Never give financial advice. You analyse; they decide.

STYLE
Direct, concrete, numerate. Use the trader's own style (scalp/day/swing) and
risk settings. Give exact levels and exact sizes, never "around" or "roughly".
Be the analyst who tells them the trade is poor when it is poor — that is what
they are paying for. A yes-man costs them money.

If a chart image is provided, describe what you actually see before drawing
conclusions from it. If the image is unclear, say so rather than guessing."""


class Profile(BaseModel):
    style: str = "Day trading"        # Scalping | Day trading | Swing
    markets: str = "XAUUSD"
    account_size: float = 10_000.0
    risk_pct: float = 1.0
    methods: str = "SMC, price action"
    language: str = "en"              # en | ar  (Arabic, incl. RTL output)
    notes: str = ""


class ChatIn(BaseModel):
    message: str
    image_base64: str | None = None       # chart screenshot
    image_media_type: str = "image/png"
    history: list[dict] = Field(default_factory=list, max_length=20)
    language: str | None = None           # overrides the profile for one turn


class AnalyzeIn(BaseModel):
    image_base64: str | None = None
    image_media_type: str = "image/png"
    text: str = ""                        # or a written description / OHLC data
    symbol: str = ""
    timeframe: str = ""
    language: str | None = None


def _client():
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "AI not configured")
    import anthropic
    return anthropic.Anthropic(api_key=key)


LANG_AR = """
LANGUAGE — RESPOND IN ARABIC (العربية)
Write your entire answer in clear, professional Modern Standard Arabic.

Terminology: use the Arabic terms traders actually use in the Gulf and Levant
markets, and put the English term in brackets the first time a technical
concept appears, because most Arabic-speaking traders learned these terms in
English:
  وقف الخسارة (Stop Loss) · جني الأرباح (Take Profit) · نسبة المخاطرة إلى العائد
  (Risk/Reward) · مناطق العرض والطلب (Supply/Demand) · فجوة القيمة العادلة (FVG)
  · كسر الهيكل (BOS) · تغيّر هيكل السوق (MSS) · اقتناص السيولة (Liquidity Sweep)
  · حجم التداول (Volume) · التذبذب (Volatility) · حجم المركز (Position Size)

Keep all NUMBERS, PRICES and SYMBOLS in Western digits and Latin letters
(4210.50, XAUUSD, 1.5R) — never transliterate them.

Everything else in the system prompt applies unchanged: never predict, never
hype, always give the case against the trade (الحجة المضادة للصفقة), and state
plainly when there is no trade worth taking.
"""


def _profile_text(p: dict) -> str:
    base = (f"TRADER PROFILE — tailor everything to this:\n"
            f"  style: {p.get('style')}\n"
            f"  markets: {p.get('markets')}\n"
            f"  account: {p.get('account_size')}\n"
            f"  risk per trade: {p.get('risk_pct')}%\n"
            f"  methods they use: {p.get('methods')}\n"
            f"  notes: {p.get('notes', '')}\n")
    if str(p.get("language", "en")).lower().startswith("ar"):
        base += LANG_AR
    return base


def _load_profile(sb: Client, uid: str) -> dict:
    try:
        r = sb.table("gpt_profiles").select("*").eq("user_id", uid).execute()
        if r.data:
            return r.data[0]
    except Exception:
        pass
    return Profile().model_dump()


def _content_blocks(text: str, img: str | None, media: str) -> list:
    blocks: list = []
    if img:
        blocks.append({"type": "image", "source": {
            "type": "base64", "media_type": media, "data": img}})
    blocks.append({"type": "text", "text": text})
    return blocks


# ---------------------------------------------------------------------- chat
@router.post("/chat")
async def chat(payload: ChatIn, user=Depends(get_current_user),
               sb: Client = Depends(get_supabase)) -> dict:
    require_plan(sb, user, {"TradeGPT Pro", "Bundle", "Bundle (Founder)"}, "TradeGPT")

    prof = _load_profile(sb, user.id)
    if payload.language:
        prof = {**prof, "language": payload.language}
    client = _client()

    messages = []
    for m in payload.history[-10:]:
        role = m.get("role")
        content = m.get("content", "")
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})

    messages.append({"role": "user", "content": _content_blocks(
        payload.message, payload.image_base64, payload.image_media_type)})

    msg = client.messages.create(
        model=MODEL, max_tokens=1600,
        system=SYSTEM + "\n\n" + _profile_text(prof),
        messages=messages,
    )
    text = "".join(b.text for b in msg.content
                   if getattr(b, "type", "") == "text")
    return {"reply": text}


# ------------------------------------------------------------------ analyze
ANALYZE_INSTRUCTION = """Analyse this chart and return ONLY a JSON object, no
preamble, no markdown fences:

{
 "observed": "what is actually visible: structure, trend, range, key levels",
 "regime": "trending | ranging | volatile | unclear",
 "levels": {"support": [..], "resistance": [..]},
 "setup": {
   "exists": true|false,
   "side": "buy|sell|none",
   "entry": number|null,
   "stop": number|null,
   "tp1": number|null,
   "tp2": number|null,
   "rr": number|null,
   "rationale": "why, in one sentence"
 },
 "position_size": {"lots": number|null, "risk_money": number|null,
                   "note": "how it was computed"},
 "case_against": "the strongest argument for NOT taking this trade",
 "invalidation": "the specific condition that proves the idea wrong",
 "confidence": "low|medium|high — and it should almost never be high",
 "verdict": "WATCH | MARGINAL | SKIP"
}

If no clean setup exists, say so honestly with exists=false and verdict=SKIP.
A day with no trade is a perfectly good day."""


@router.post("/analyze")
async def analyze(payload: AnalyzeIn, user=Depends(get_current_user),
                  sb: Client = Depends(get_supabase)) -> dict:
    require_plan(sb, user, {"TradeGPT Pro", "Bundle", "Bundle (Founder)"}, "TradeGPT")

    if not payload.image_base64 and not payload.text.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "send a chart image or written market data")
    prof = _load_profile(sb, user.id)
    if payload.language:
        prof = {**prof, "language": payload.language}
    client = _client()

    ctx = (f"Symbol: {payload.symbol or 'unspecified'}   "
           f"Timeframe: {payload.timeframe or 'unspecified'}\n"
           f"{payload.text}\n\n{ANALYZE_INSTRUCTION}")

    msg = client.messages.create(
        model=MODEL, max_tokens=1500,
        system=SYSTEM + "\n\n" + _profile_text(prof),
        messages=[{"role": "user", "content": _content_blocks(
            ctx, payload.image_base64, payload.image_media_type)}],
    )
    raw = "".join(b.text for b in msg.content
                  if getattr(b, "type", "") == "text").strip()
    raw = raw.replace("```json", "").replace("```", "").strip()
    try:
        data = json.loads(raw)
    except Exception:
        data = {"observed": raw, "setup": {"exists": False},
                "verdict": "SKIP",
                "note": "model did not return clean JSON"}

    try:
        sb.table("gpt_analyses").insert({
            "user_id": user.id, "symbol": payload.symbol,
            "timeframe": payload.timeframe,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "result": data,
        }).execute()
    except Exception:
        pass
    return data


# ------------------------------------------------------------------- review
class ReviewIn(BaseModel):
    trades: list[dict] = Field(default_factory=list, max_length=300)


REVIEW_INSTRUCTION = """Review this trade history as a performance coach.

Compute and state plainly:
  * win rate, profit factor, expectancy in R, average win vs average loss
  * the win/loss SIZE ratio, and the ratio NEEDED to break even at their win
    rate — a 70% win rate with small winners and big losers still loses money,
    and traders systematically miss this
  * max drawdown and the worst losing streak

Then find the BEHAVIOURAL patterns they cannot see themselves. Look hard for:
  cutting winners early; holding losers; sizing up after losses (revenge);
  sizing down after wins; time-of-day or day-of-week effects; over-trading;
  a difference between planned and actual exits.

Cite specific trades as evidence. End with the ONE change most likely to
improve results, stated as a rule that can be checked objectively.

Be blunt. If the sample is too small to conclude anything (under 30 trades),
say so first and do not over-interpret noise."""


@router.post("/review")
async def review(payload: ReviewIn, user=Depends(get_current_user),
                 sb: Client = Depends(get_supabase)) -> dict:
    require_plan(sb, user, {"TradeGPT Pro", "Bundle", "Bundle (Founder)"}, "TradeGPT")

    if not payload.trades:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "no trades supplied")
    prof = _load_profile(sb, user.id)
    client = _client()
    msg = client.messages.create(
        model=MODEL, max_tokens=1800,
        system=SYSTEM + "\n\n" + _profile_text(prof),
        messages=[{"role": "user", "content":
                   REVIEW_INSTRUCTION + "\n\nTRADES:\n" +
                   json.dumps(payload.trades[-200:], indent=1, default=str)}],
    )
    text = "".join(b.text for b in msg.content
                   if getattr(b, "type", "") == "text")
    return {"review": text, "trades_analysed": len(payload.trades)}


# ------------------------------------------------------------------ profile
@router.get("/profile")
async def get_profile(user=Depends(get_current_user),
                      sb: Client = Depends(get_supabase)) -> dict:
    return _load_profile(sb, user.id)


@router.put("/profile")
async def put_profile(p: Profile, user=Depends(get_current_user),
                      sb: Client = Depends(get_supabase)) -> dict:
    row = {"user_id": user.id, **p.model_dump(),
           "updated_at": datetime.now(timezone.utc).isoformat()}
    sb.table("gpt_profiles").upsert(row, on_conflict="user_id").execute()
    return {"ok": True, **p.model_dump()}
