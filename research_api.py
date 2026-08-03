"""SKLZ — research observation store and honest analysis."""
from __future__ import annotations

import os
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, status
from supabase import Client

from auth import get_current_user
from db import get_supabase

router = APIRouter(prefix="/api/research", tags=["research"])

FIELDS = {
    "ticket", "symbol", "side", "strategy", "captured_at", "exit_at",
    "hour_utc", "weekday", "session", "held_minutes", "entry_price",
    "exit_price", "spread", "spread_pips", "atr_m15", "atr_m15_pips",
    "atr_percentile", "volatility_regime", "range_position",
    "trend_efficiency", "regime", "quality_score", "institutional_score",
    "institutional_flags", "inputs_used", "inputs_missing", "book_imbalance",
    "delta_imbalance", "delta_quality", "absorbed", "wall_ahead",
    "equity_at_entry", "open_positions", "pnl", "result_pips", "outcome",
    "exit_reason", "max_favourable_pips", "max_adverse_pips",
    "capture_ratio", "failure_category", "failure_reasons",
}


def _internal(auth: str) -> bool:
    key = os.environ.get("INTERNAL_KEY", "") or os.environ.get("BOT_INGEST_KEY", "")
    return bool(key) and auth.replace("Bearer ", "").strip() == key


@router.post("/observation")
async def store(body: dict, authorization: str = Header(default=""),
                sb: Client = Depends(get_supabase)) -> dict:
    """The runner posts one completed trade here."""
    if not _internal(authorization):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "unauthorized")

    row = {k: v for k, v in body.items() if k in FIELDS}
    row["raw"] = body
    try:
        sb.table("trade_observations").insert(row).execute()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": str(exc)[:200]}
    return {"ok": True}


@router.get("/summary")
async def summary(user=Depends(get_current_user),
                  sb: Client = Depends(get_supabase)) -> dict:
    """Performance with the uncertainty attached, and a refusal to conclude
    from too little data."""
    try:
        import research_stats as RS
    except ImportError as exc:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR,
                            f"stats unavailable: {exc}") from exc
    try:
        rows = (sb.table("trade_observations").select("*")
                .order("captured_at", desc=True).limit(5000).execute()).data or []
    except Exception:
        rows = []
    return {"overall": RS.summarise(rows, "all trades"),
            "readiness": RS.research_readiness(rows)}


@router.get("/slice/{field}")
async def slice_field(field: str, user=Depends(get_current_user),
                      sb: Client = Depends(get_supabase)) -> dict:
    """Break results down by any captured field — with small groups refused."""
    if field not in FIELDS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"unknown field. Available: {sorted(FIELDS)}")
    import research_stats as RS
    try:
        rows = (sb.table("trade_observations").select("*")
                .order("captured_at", desc=True).limit(5000).execute()).data or []
    except Exception:
        rows = []
    return RS.slice_by(rows, field)


@router.get("/failures")
async def failures(user=Depends(get_current_user),
                   sb: Client = Depends(get_supabase)) -> dict:
    """Why trades lose, ranked. This shows a pattern long before the win rate
    does, which is why it is worth watching while the sample builds."""
    try:
        rows = (sb.table("trade_observations").select("*")
                .eq("outcome", "loss").limit(2000).execute()).data or []
    except Exception:
        rows = []
    counts: dict = {}
    for r in rows:
        c = r.get("failure_category") or "unclassified"
        counts[c] = counts.get(c, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    total = sum(counts.values())
    return {"losses": total,
            "by_category": [{"category": k, "count": v,
                             "share": round(v / total, 3) if total else 0}
                            for k, v in ranked],
            "note": ("A dominant category is the highest-value thing to fix. "
                     "'gave_back' is an exit problem; 'wrong_direction' is an "
                     "entry problem; they need different work.")}
