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
import os as _os
from datetime import datetime, timezone, timedelta
from pydantic import BaseModel as _BM_RESULT

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field
from supabase import Client

from auth import get_current_user


def _require_admin(user):
    """Bot control is owner-only. Not admin-only — owner."""
    admins = {e.strip().lower() for e in
              os.environ.get("OWNER_EMAIL", "fxfactor24@gmail.com").split(",")}
    if (getattr(user, "email", "") or "").lower() not in admins:
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "Bot control is limited to the account owner.")
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
    _require_admin(user)
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
            "note": "applies within a few seconds"}


LEASE_SECONDS = 45


@router.get("/command")
async def get_command(bot_name: str, account: str = "", server: str = "",
                      runner_id: str = "",
                      _=Depends(require_bot_key),
                      sb: Client = Depends(get_supabase)) -> dict:
    """Runner polls: dashboard state + any commands leased to it.

    Two changes from the original, both load-bearing:

    1. A row is CLAIMED, not merely read. The update carries its own
       `status = pending` condition, so two polls arriving together cannot
       both win the same row — the second update matches nothing.

    2. Claiming means `dispatched`, never `delivered`-as-success. The
       command is not a trade until the Runner says the broker filled it.

    Expired commands are settled here rather than handed out: a market
    order that waited through a VPS outage must never reach a broker at a
    price nobody agreed to.
    """
    orders = []
    now = datetime.now(timezone.utc)
    try:
        rows = (sb.table("bot_orders").select("*")
                .eq("bot_name", bot_name).eq("status", "pending")
                .order("created_at").limit(5).execute()).data or []
    except Exception:
        rows = []

    for r in rows:
        # expiry first — an old market order is settled, not delivered
        exp = r.get("expires_at")
        if exp:
            try:
                if datetime.fromisoformat(str(exp).replace("Z", "+00:00")) < now:
                    sb.table("bot_orders").update({
                        "status": "expired",
                        "broker_comment": "expired before a Runner collected it",
                        "result_recorded_at": now.isoformat(),
                    }).eq("id", r["id"]).eq("status", "pending").execute()
                    continue
            except (TypeError, ValueError):
                pass

        # atomic claim: the WHERE carries status=pending, so a second
        # concurrent poll updates zero rows and gets nothing back
        try:
            claimed = (sb.table("bot_orders").update({
                "status": "dispatched",
                "dispatched_at": now.isoformat(),
                "lease_owner": runner_id or f"anon:{bot_name}",
                "lease_expires_at": (now + timedelta(
                    seconds=LEASE_SECONDS)).isoformat(),
                "attempts": int(r.get("attempts") or 0) + 1,
            }).eq("id", r["id"]).eq("status", "pending").execute()).data or []
        except Exception:
            continue
        if not claimed:
            continue                      # someone else won the race

        orders.append({
            "command_id": str(r.get("command_id") or r["id"]),
            "type": r.get("command_type") or "market",
            "mode": r.get("mode") or "execute_signal_copy",
            "symbol": r.get("symbol", ""), "side": r.get("side", ""),
            "note": r.get("note", ""), "lots": r.get("lots", 0),
            "sl": r.get("sl", 0), "tp": r.get("tp", 0),
            "ticket": r.get("ticket"),
            "expires_at": r.get("expires_at"),
            "expected_account": r.get("expected_account") or "",
        })

    _settle_expired_leases(sb, bot_name, runner_id, now)

    if account:
        _note_account(sb, bot_name, account, server)

    return {"ok": True, "command": _bot_command(sb, bot_name),
            "orders": orders}


def _settle_expired_leases(sb: Client, bot_name: str, runner_id: str,
                           now) -> None:
    """Decide what happens to a lease nobody reported on.

    The A3 version returned the row to `pending`, where ANY Runner sharing
    the bot_name could claim it. That is the cross-Runner duplicate: Runner
    A executes, its result is lost, the lease lapses, Runner B — which has
    no local record of the command — executes it again. Real money, twice.

    So an expired lease is never re-offered to a different installation:

      * the SAME Runner may reclaim it. Its local store already knows
        whether it executed, so it cannot double-execute.
      * a DIFFERENT Runner may not have it at all.
      * once the TTL has also passed, an unclaimed lease becomes `unknown`
        rather than `expired`, because the Runner that held it may have
        executed before going quiet. Claiming it expired would assert a
        fact we do not have.
    """
    try:
        rows = (sb.table("bot_orders").select(
            "id,command_id,lease_owner,expires_at,lease_expires_at")
            .eq("bot_name", bot_name).eq("status", "dispatched")
            .lt("lease_expires_at", now.isoformat()).execute()).data or []
    except Exception:
        return

    for r in rows:
        owner = r.get("lease_owner") or ""
        same_runner = bool(runner_id) and owner == runner_id
        ttl_gone = False
        try:
            exp = r.get("expires_at")
            if exp:
                ttl_gone = datetime.fromisoformat(
                    str(exp).replace("Z", "+00:00")) < now
        except (TypeError, ValueError):
            pass

        if same_runner and not ttl_gone:
            # safe: this installation's durable store guards re-execution
            sb.table("bot_orders").update({"status": "pending"}) \
                .eq("id", r["id"]).eq("status", "dispatched").execute()
            continue

        if ttl_gone:
            sb.table("bot_orders").update({
                "status": "unknown",
                "uncertainty_reason": (
                    "lease expired without a result and the TTL passed; the "
                    "Runner holding it may or may not have executed"),
                "result_recorded_at": now.isoformat(),
            }).eq("id", r["id"]).eq("status", "dispatched").execute()
            print(f"[bot] RECONCILIATION REQUIRED command="
                  f"{r.get('command_id')} — lease lapsed, outcome unproven")
        # otherwise: leave it dispatched. Only its owner may come back for
        # it, and the TTL will settle it if the owner never does.


def _note_account(sb: Client, bot_name: str, account: str,
                  server: str = "") -> None:
    """Record the account the Runner reports it is actually logged into.

    Observed by the terminal, never supplied by a browser. This phase only
    makes it visible; it authorises nothing.
    """
    try:
        sb.table("bot_state").upsert(
            {"bot_name": bot_name, "last_account": str(account)[:64],
             "last_server": str(server)[:96],
             "account_seen_at": datetime.now(timezone.utc).isoformat()},
            on_conflict="bot_name").execute()
    except Exception:
        pass


class ResultIn(_BM_RESULT):
    command_id: str
    ok: bool = False
    state: str = ""                 # succeeded | failed | expired | unknown
    ticket: int | None = None
    order_id: int | None = None
    resolved_symbol: str = ""
    fill_price: float | None = None
    filled_volume: float | None = None
    retcode: int | None = None
    broker_comment: str = ""
    account: str = ""
    server: str = ""
    runner_id: str = ""
    runner_received_at: str = ""
    mt5_requested_at: str = ""
    broker_confirmed_at: str = ""
    # A positions read answers with a list, not a ticket. Without this
    # the Runner's answer was accepted and silently discarded.
    positions: list = []


@router.post("/result", dependencies=[Depends(require_bot_key)])
async def post_result(body: ResultIn,
                      sb: Client = Depends(get_supabase)) -> dict:
    """Runner reports what the broker actually did.

    Idempotent by command_id: posting the same result twice records it
    once and returns the same outcome, so a Runner re-sending after a lost
    acknowledgement cannot create a second execution state, a second
    journal association, or a second anything.
    """
    now = datetime.now(timezone.utc).isoformat()
    try:
        rows = (sb.table("bot_orders").select("*")
                .eq("command_id", body.command_id).limit(1).execute()).data
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            f"result store unavailable: {exc}") from exc
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown command_id")
    row = rows[0]

    # Already settled: return what we recorded, do not overwrite it. The
    # first result is the truth; a retry is a retry.
    if row.get("status") in ("succeeded", "failed", "expired", "unknown"):
        return {"ok": True, "duplicate": True, "status": row["status"],
                "ticket": row.get("ticket"),
                "broker_comment": row.get("broker_comment") or ""}

    state = body.state or ("succeeded" if body.ok else "failed")
    if state not in ("succeeded", "failed", "expired", "unknown"):
        state = "failed"
    # 'unknown' is NOT 'failed'. Failed means we know the broker did not
    # execute. Unknown means we cannot prove either way — the Runner
    # persisted its intent and then lost the ability to confirm. Telling
    # an owner a trade failed when a position may be open is the worst
    # available answer, so the uncertainty is preserved and surfaced.
    stored = state

    expected = (row.get("expected_account") or "").strip()
    actual = (body.account or "").strip()
    mismatch = bool(expected and actual and expected != actual)

    patch = {
        "status": stored,
        "executed_at": body.broker_confirmed_at or now,
        "result_recorded_at": now,
        "ticket": body.ticket, "order_id": body.order_id,
        "resolved_symbol": body.resolved_symbol[:64] or None,
        "fill_price": body.fill_price, "filled_volume": body.filled_volume,
        "retcode": body.retcode,
        "broker_comment": (body.broker_comment or "")[:400] or None,
        "uncertainty_reason": (body.broker_comment or
                               "runner could not confirm the outcome"
                               )[:300] if state == "unknown" else None,
        "positions": (body.positions or None),
        "actual_account": actual[:64] or None,
        "account_server": (body.server or "")[:96] or None,
        "account_mismatch": mismatch,
        "runner_received_at": body.runner_received_at or None,
        "mt5_requested_at": body.mt5_requested_at or None,
        "broker_confirmed_at": body.broker_confirmed_at or None,
    }
    try:
        sb.table("bot_orders").update(patch) \
            .eq("command_id", body.command_id).execute()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            f"could not record result: {exc}") from exc

    if mismatch:
        print(f"[bot] ACCOUNT_MISMATCH command={body.command_id} "
              f"expected={expected} actual={actual}")
    if stored == "unknown":
        print(f"[bot] RECONCILIATION REQUIRED command={body.command_id} "
              f"account={actual or '?'} — outcome could not be confirmed")
    return {"ok": True, "duplicate": False, "status": stored,
            "account_mismatch": mismatch,
            "reconciliation_required": stored == "unknown",
            "note": ("Execution outcome unknown — verify the trading "
                     "account before retrying. This command will not be "
                     "executed again automatically."
                     if stored == "unknown" else "")}


# ── admin manual orders from dashboard ──────────────────────────────
from pydantic import BaseModel as _BM


class OrderIn(_BM):
    bot_name: str
    symbol: str
    side: str            # buy | sell
    note: str = ""
    lots: float = 0.0
    sl: float = 0.0
    tp: float = 0.0


def _is_owner(user) -> bool:
    """Strictly the platform owner — not merely an admin.

    ADMIN_EMAILS can hold several people, and admin covers plenty of useful
    but recoverable things. Placing a live order is different: it spends real
    money and, once followers exist, propagates to other people accounts.
    That stays with one address.
    """
    owner = os.environ.get("OWNER_EMAIL", "fxfactor24@gmail.com").strip().lower()
    return (getattr(user, "email", "") or "").strip().lower() == owner


def _require_owner(user) -> None:
    if not _is_owner(user):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "This action is limited to the account owner. Administrators "
            "cannot place live orders.")


def _is_admin_user(user) -> bool:
    admins = {e.strip().lower() for e in
              os.environ.get("ADMIN_EMAILS", "fxfactor24@gmail.com").split(",")}
    return (getattr(user, "email", "") or "").lower() in admins


@router.post("/order")
async def place_order(body: OrderIn, user=Depends(get_current_user),
                      sb: Client = Depends(get_supabase)) -> dict:
    """Admin-only: queue a manual entry for the bot to execute on next poll."""
    _require_owner(user)
    if body.side not in ("buy", "sell"):
        return {"ok": False, "reason": "side must be buy|sell"}
    # The dashboard now hides demo Runners, but a stale tab or a typed
    # name must not be able to send a funded trade to a demo account.
    # A gold order intended for the funded 50k reached the demo account
    # this way; the UI fix alone would leave that path open.
    if _os.environ.get("SKLZ_DEMO_BOT_NAME", "sklz-demo").strip().lower() \
            == (body.bot_name or "").strip().lower():
        return {"ok": False,
                "reason": "that is the demo Runner — manual orders go to a "
                          "live Runner. Pick one from the list."}
    try:
        sb.table("bot_orders").insert({
            "bot_name": body.bot_name, "symbol": body.symbol.upper(),
            "side": body.side, "note": body.note[:300],
            "lots": body.lots, "sl": body.sl, "tp": body.tp,
            "status": "pending", "created_by": str(user.id)}).execute()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": str(exc)[:200]}
    return {"ok": True, "note": "order queued — bot executes within a few seconds"}


@router.get("/state")
async def bot_state(bot_name: str, user=Depends(get_current_user),
                    sb: Client = Depends(get_supabase)) -> dict:
    """Current desired state for a bot, for the dashboard to reflect."""
    _require_admin(user)
    return {"ok": True, "bot_name": bot_name, "state": _bot_command(sb, bot_name)}
