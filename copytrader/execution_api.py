"""SKLZ CopyTrader — execution control and history.

  POST /api/copy/poll              run one detect+fan-out cycle (internal key)
  GET  /api/copy/orders            my copied orders (follower view)
  GET  /api/copy/leader-trades     fills published by a leader
  GET  /api/copy/execution-mode    is the system in dry run or live?

The poll endpoint is deliberately pull-based rather than a background thread:
it can be driven by a cron, it is easy to stop, and every run is auditable.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, status
from supabase import Client

from auth import get_current_user
from db import get_supabase
from copytrader.connections_api import _load_adapter
from copytrader.executor import execution_mode, fan_out, poll_leader_fills

router = APIRouter(prefix="/api/copy", tags=["copytrader"])


def _internal_key() -> str:
    return (os.environ.get("INTERNAL_KEY", "")
            or os.environ.get("BOT_INGEST_KEY", ""))


@router.get("/execution-mode")
async def mode() -> dict:
    m = execution_mode()
    return {
        "mode": m,
        "live": m == "live",
        "description": ("LIVE — copied orders are sent to exchanges."
                        if m == "live" else
                        "DRY RUN — decisions are computed and recorded, but no "
                        "orders are sent to any exchange."),
        "how_to_change": ("Set COPY_EXECUTION_MODE=live in the environment to "
                          "enable real execution. Anything else means dry run."),
    }


@router.post("/poll")
async def poll(authorization: str = Header(default=""),
               leader_id: str | None = None,
               lookback_minutes: int = 30,
               sb: Client = Depends(get_supabase)) -> dict:
    """Detect new leader fills and fan them out. Internal-key gated."""
    expected = _internal_key()
    token = authorization.replace("Bearer ", "").strip()
    if not expected or token != expected:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "unauthorized")

    try:
        q = (sb.table("copy_leaders").select("*").eq("status", "active"))
        if leader_id:
            q = q.eq("id", leader_id)
        leaders = q.execute().data or []
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR,
                            f"could not load leaders: {str(exc)[:200]}") from exc

    log_lines: list[str] = []

    def log(m: str) -> None:
        log_lines.append(str(m))

    summary = []
    for ld in leaders:
        try:
            adapter = _load_adapter(sb, ld["user_id"], ld["connection_id"])
        except Exception as exc:  # noqa: BLE001
            summary.append({"leader": ld["display_name"],
                            "error": f"connection unavailable: {type(exc).__name__}"})
            continue
        fills = poll_leader_fills(adapter, sb, ld["id"],
                                  lookback_minutes=lookback_minutes, log=log)
        copied = 0
        for f in fills:
            res = fan_out(sb, f, lambda uid, cid: _load_adapter(sb, uid, cid), log=log)
            copied += sum(1 for r in res if r.get("status") in ("simulated", "filled"))
        summary.append({"leader": ld["display_name"],
                        "new_fills": len(fills), "follower_actions": copied})

    return {"ok": True, "mode": execution_mode(),
            "checked_leaders": len(leaders), "results": summary,
            "log": log_lines[-40:],
            "ran_at": datetime.now(timezone.utc).isoformat()}


@router.get("/orders")
async def my_orders(limit: int = 100, user=Depends(get_current_user),
                    sb: Client = Depends(get_supabase)) -> dict:
    """Every copy decision made for this follower — including the skips,
    because knowing why a trade was NOT copied matters as much as the fills."""
    uid = str(user.id)
    try:
        subs = (sb.table("copy_subscriptions").select("id,leader_id")
                .eq("follower_id", uid).execute()).data or []
    except Exception:
        subs = []
    ids = [s["id"] for s in subs]
    if not ids:
        return {"orders": [], "mode": execution_mode()}
    try:
        rows = (sb.table("copy_orders").select("*")
                .in_("subscription_id", ids)
                .order("created_at", desc=True).limit(limit).execute()).data or []
    except Exception:
        rows = []
    return {"orders": rows, "mode": execution_mode(),
            "note": ("'simulated' means the system decided what it would do but "
                     "sent nothing to an exchange (dry run).")}


@router.get("/leader-trades")
async def leader_trades(leader_id: str, limit: int = 50,
                        sb: Client = Depends(get_supabase)) -> dict:
    """PUBLIC — a leader's published fills. Transparency for prospective followers."""
    try:
        rows = (sb.table("copy_leader_trades")
                .select("symbol,side,notional,price,created_at")
                .eq("leader_id", leader_id)
                .order("created_at", desc=True).limit(limit).execute()).data or []
    except Exception:
        rows = []
    return {"trades": rows, "count": len(rows)}
