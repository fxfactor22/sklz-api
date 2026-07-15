"""Signal webhook — the AI layer TradingView cannot host.

Pine Script is sandboxed: no network calls, so no indicator can contain an AI.
The SKLZ Pro indicator instead fires a JSON alert; TradingView POSTs it here;
Claude analyses it with context the chart does not have; the verdict lands on
the user's dashboard and phone.

    TradingView alert -> POST /api/signal/webhook  (no auth: TV can't sign)
                      -> stored, analysed, pushed to the dashboard
    Dashboard         -> GET  /api/signal/recent   (user JWT)

SECURITY NOTE, said plainly: TradingView cannot add auth headers, so the
webhook takes a SECRET IN THE URL PATH instead:
    https://api.sklzlabs.com/api/signal/webhook/{SIGNAL_WEBHOOK_KEY}
Anyone with the URL can post a signal, so the key is the only gate — treat it
like a password, and rate-limit generously rather than trusting the payload.

The AI is told, in its system prompt, what our own research found: these
patterns measured at ~0 edge on real data. It is instructed to be a risk
reviewer, not a hype machine. An "AI signal service" that only ever says
"strong buy" is a liability to the customer and to us.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from supabase import Client

from auth import get_current_user
from db import get_supabase

router = APIRouter(prefix="/api/signal", tags=["signal"])

MODEL = "claude-sonnet-4-5"

SYSTEM = """You are the risk reviewer for SKLZ Labs' signal service. A chart
indicator has detected a structural setup and sent it to you.

WHAT OUR OWN RESEARCH FOUND — you must not contradict it:
  * SMC zones + FVG retests measured 0.00 sigma of directional edge over 307
    out-of-sample trades on real Gold data.
  * ICT liquidity sweep + MSS + FVG measured -0.04 sigma over 130 signals,
    with hit rates at or below coin-flip.
  * Order flow at volume nodes measured -0.04 sigma over 681 signals on 27.7M
    real ticks, with hit rates BELOW chance.
These are the patterns the indicator draws. They are useful for STRUCTURE and
PLANNING; they are not proven predictors of direction.

So your job is NOT to say "strong buy". It is to give the trader what the
chart cannot:
  1. CONTEXT — what regime is this market in, and does this setup fit it?
  2. RISK — what specifically would invalidate this idea, and what is the
     realistic worst case?
  3. THE CASE AGAINST — the strongest argument for NOT taking this trade.
  4. VERDICT — one of: WORTH WATCHING / MARGINAL / SKIP, with one sentence why.

Be concise (under 150 words), concrete, and honest. If the setup is poor, say
so. A customer who loses money on our hype does not come back."""


class SignalIn(BaseModel):
    source: str = "sklz_pro"
    symbol: str
    tf: str = ""
    side: str
    entry: float
    sl: float
    tp1: float | None = None
    tp2: float | None = None
    rr: float | None = None
    mode: str = ""
    method: str = ""
    reason: str = ""
    price: float | None = None
    atr: float | None = None


def _key() -> str:
    return os.environ.get("SIGNAL_WEBHOOK_KEY", "")


async def _analyse(sig: SignalIn) -> str:
    """Claude's review. Returns '' if the API key isn't configured."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return ""
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        payload = sig.model_dump()
        msg = client.messages.create(
            model=MODEL, max_tokens=400, system=SYSTEM,
            messages=[{"role": "user", "content":
                       "Review this setup:\n" + json.dumps(payload, indent=1)}],
        )
        return "".join(b.text for b in msg.content
                       if getattr(b, "type", "") == "text")
    except Exception as exc:  # noqa: BLE001
        return f"(AI review unavailable: {exc})"


@router.post("/webhook/{key}")
async def webhook(key: str, sig: SignalIn,
                  sb: Client = Depends(get_supabase)) -> dict:
    expected = _key()
    if not expected:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "signal webhook not configured")
    if key != expected:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad webhook key")

    review = await _analyse(sig)
    row = {
        **sig.model_dump(),
        "received_at": datetime.now(timezone.utc).isoformat(),
        "ai_review": review,
    }
    try:
        sb.table("signals").insert(row).execute()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR,
                            f"store failed: {exc}") from exc
    return {"ok": True, "reviewed": bool(review)}


@router.get("/recent")
async def recent(limit: int = 50, user=Depends(get_current_user),
                 sb: Client = Depends(get_supabase)) -> dict:
    limit = max(1, min(limit, 200))
    res = (sb.table("signals").select("*")
             .order("received_at", desc=True).limit(limit).execute())
    return {"signals": res.data or []}
