"""Bot telemetry: the VPS bot phones home; the dashboard reads it.

Ingest (bot → API), authenticated by a shared bot key:
    POST /api/bot/heartbeat   {session…, equity, stats}   → upsert session
    POST /api/bot/events      {session_id, events:[…]}     → append events
    POST /api/bot/report      {session_id, report}         → attach AI report

Read (dashboard → API), authenticated by the user's JWT:
    GET  /api/bot/sessions                → recent sessions
    GET  /api/bot/sessions/{id}/events    → recent events for one session

Single-tenant v1: one BOT_INGEST_KEY env identifies your own bot installs.
Multi-user licensing (per-customer keys) rides on the license server later.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field
from supabase import Client

from auth import get_current_user
from db import get_supabase

router = APIRouter(prefix="/api/bot", tags=["bot"])


def _ingest_key() -> str:
    return os.environ.get("BOT_INGEST_KEY", "")


def require_bot_key(authorization: str = Header(default="")) -> None:
    key = _ingest_key()
    if not key:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "bot ingest not configured (BOT_INGEST_KEY unset)")
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bot key")
    if authorization.split(" ", 1)[1].strip() != key:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid bot key")


# ------------------------------------------------------------------ schemas
class HeartbeatIn(BaseModel):
    session_id: str | None = None          # None on first beat → server creates
    bot: str
    symbol: str
    timeframe: str = ""
    mode: str = "paper"
    equity: float | None = None
    balance: float | None = None
    stats: dict = Field(default_factory=dict)


class EventIn(BaseModel):
    ts: str | None = None                  # ISO; server time if omitted
    level: str = "info"
    etype: str = ""
    message: str
    data: dict = Field(default_factory=dict)


class EventsIn(BaseModel):
    session_id: str
    events: list[EventIn] = Field(max_length=200)


class ReportIn(BaseModel):
    session_id: str
    report: dict


# ------------------------------------------------------------------ ingest


def _bot_command(sb: Client, bot_name: str) -> str:
    try:
        r = (sb.table("bot_controls").select("desired_state")
             .eq("bot_name", bot_name).execute()).data
        return (r[0].get("desired_state") or "run") if r else "run"
    except Exception:
        return "run"

@router.post("/heartbeat", dependencies=[Depends(require_bot_key)])
async def heartbeat(payload: HeartbeatIn, sb: Client = Depends(get_supabase)) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    try:
        if payload.session_id:
            sb.table("bot_sessions").update({
                "last_seen": now, "equity": payload.equity,
                "balance": payload.balance,
                "stats": payload.stats, "mode": payload.mode,
            }).eq("id", payload.session_id).execute()
            return {"ok": True, "session_id": payload.session_id,
                    "command": _bot_command(sb, payload.bot)}
        res = sb.table("bot_sessions").insert({
            "bot_key": "default", "bot": payload.bot, "symbol": payload.symbol,
            "timeframe": payload.timeframe, "mode": payload.mode,
            "equity": payload.equity, "balance": payload.balance,
            "stats": payload.stats, "last_seen": now,
        }).execute()
        sid = res.data[0]["id"] if res.data else None
        return {"ok": True, "session_id": sid,
                "command": _bot_command(sb, payload.bot)}
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 — name the real failure
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            f"bot_sessions write failed: {type(exc).__name__}: {exc}") from exc


@router.post("/events", dependencies=[Depends(require_bot_key)])
async def ingest_events(payload: EventsIn, sb: Client = Depends(get_supabase)) -> dict:
    rows = [{
        "session_id": payload.session_id,
        **({"ts": e.ts} if e.ts else {}),
        "level": e.level[:16], "etype": e.etype[:32],
        "message": e.message[:2000], "data": e.data,
    } for e in payload.events]
    if rows:
        sb.table("bot_events").insert(rows).execute()
    return {"ok": True, "ingested": len(rows)}


@router.post("/report", dependencies=[Depends(require_bot_key)])
async def attach_report(payload: ReportIn, sb: Client = Depends(get_supabase)) -> dict:
    sb.table("bot_sessions").update({"ai_report": payload.report}) \
      .eq("id", payload.session_id).execute()
    return {"ok": True}


# ------------------------------------------------------------------ dashboard
@router.get("/sessions")
async def sessions(user=Depends(get_current_user),
                   sb: Client = Depends(get_supabase)) -> dict:
    res = (sb.table("bot_sessions").select("*")
             .order("last_seen", desc=True).limit(20).execute())
    return {"sessions": res.data or []}


@router.get("/sessions/{session_id}/events")
async def session_events(session_id: str, limit: int = 100,
                         user=Depends(get_current_user),
                         sb: Client = Depends(get_supabase)) -> dict:
    limit = max(1, min(limit, 500))
    res = (sb.table("bot_events").select("*")
             .eq("session_id", session_id)
             .order("id", desc=True).limit(limit).execute())
    return {"events": res.data or []}


from auth import get_current_user  # noqa: E402


@router.post("/control")
async def control(bot_name: str, command: str,
                  user=Depends(get_current_user),
                  sb: Client = Depends(get_supabase)) -> dict:
    """Dashboard start/pause for a bot. command: run | pause.
    Delivered to the runner on its next heartbeat (within ~30s)."""
    if command not in ("run", "pause"):
        return {"ok": False, "reason": "command must be run|pause"}
    try:
        sb.table("bot_controls").upsert(
            {"bot_name": bot_name, "desired_state": command,
             "updated_by": str(user.id)},
            on_conflict="bot_name").execute()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": str(exc)[:200]}
    return {"ok": True, "bot_name": bot_name, "command": command,
            "note": "applies at next heartbeat (~30s)"}


@router.get("/command")
async def get_command(bot_name: str, _=Depends(require_bot_key),
                      sb: Client = Depends(get_supabase)) -> dict:
    """Runner polls this for its dashboard command. run | pause."""
    return {"ok": True, "command": _bot_command(sb, bot_name)}
