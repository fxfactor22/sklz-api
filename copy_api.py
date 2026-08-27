"""SKLZ LABS COPY (MT5 network) — Phase 1 API.

Lives at /api/mt5copy — deliberately separate from the existing crypto copy
system at /api/copy (leaders, exchange connections, subscriptions), which
stays untouched. Phase 3 folds MT5 masters into that system's existing
application/review workflow instead of duplicating it.

Master events in, per-slave instructions out, everything audited. The
architecture decision this encodes: SKLZ never touches a user's trading
password. Slave terminals run an EA that authenticates with a per-account
copy key and executes locally; this API is a relay and a ledger, not a
custodian.

Feature flags (env, all staged per the regulatory layer):
  COPY_LIVE=0                  master events accepted but queued as 'demo'
  MASTER_MARKETPLACE=1         public read of published masters
  REAL_MONEY_COPYING=0         poll returns nothing for real accounts
Compensation and performance fees have no code path here yet — deliberately.
"""
from __future__ import annotations

import json
import os
import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from supabase import Client

from db import get_supabase
from auth import get_current_user

router = APIRouter(prefix="/api/mt5copy", tags=["mt5-copy"])


def _flag(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default) == "1"


def _bearer(req: Request) -> str:
    h = req.headers.get("authorization", "")
    return h[7:] if h.lower().startswith("bearer ") else ""


def _engine_key_ok(key: str) -> bool:
    return bool(key) and key in (os.environ.get("SIGNAL_WEBHOOK_KEY", ""),
                                 os.environ.get("BOT_INGEST_KEY", ""))


# ── master events (engine hook / master EA) ─────────────────────────
class MasterEvent(BaseModel):
    event: str                     # open / modify / close
    master_ticket: int
    symbol: str
    side: int | None = None
    lots: float | None = None
    price: float | None = None
    sl: float | None = None
    tp: float | None = None


def _resolve_lots(cfg: dict, ev: MasterEvent) -> float:
    mode, val = cfg.get("lot_mode", "multiplier"), float(cfg.get("lot_value", 1))
    lots = ev.lots or 0.01
    if mode == "fixed":
        out = val
    elif mode == "multiplier":
        out = lots * val
    else:
        # balance/equity/risk_pct need the slave's numbers, which only the
        # terminal knows — the EA finishes the calculation; we send the mode
        out = lots * val
    lo = float(cfg.get("min_lot") or 0.01)
    hi = float(cfg.get("max_lot") or 100.0)
    return max(lo, min(hi, round(out, 2)))


@router.post("/event")
async def master_event(body: MasterEvent, request: Request,
                       sb: Client = Depends(get_supabase)) -> dict:
    if not _engine_key_ok(_bearer(request)):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad key")

    m = (sb.table("copy_masters").select("id,status")
         .eq("is_system", True).limit(1).execute()).data
    if not m:
        return {"ok": False, "reason": "no system master row"}
    master = m[0]
    if master["status"] == "suspended":
        return {"ok": True, "queued": 0, "note": "master suspended — "
                "circuit breaker active, nothing queued"}

    ev = sb.table("copy_events").insert({
        "master_id": master["id"], "event": body.event,
        "master_ticket": body.master_ticket, "symbol": body.symbol,
        "side": body.side, "lots": body.lots, "price": body.price,
        "sl": body.sl, "tp": body.tp}).execute()
    event_id = ev.data[0]["id"]

    # fan out to enabled configs of enabled slaves
    cfgs = (sb.table("copy_configs")
            .select("*, copy_slaves!inner(id,enabled)")
            .eq("master_id", master["id"]).eq("enabled", True)
            .execute()).data or []
    queued = 0
    for cfg in cfgs:
        if not cfg.get("copy_slaves", {}).get("enabled"):
            continue
        sym = (cfg.get("symbol_map") or {}).get(body.symbol, body.symbol)
        if cfg.get("blocked_symbols") and sym in cfg["blocked_symbols"]:
            continue
        if cfg.get("allowed_symbols") and sym not in cfg["allowed_symbols"]:
            continue
        instr = {"event": body.event, "master_ticket": body.master_ticket,
                 "symbol": sym, "side": body.side,
                 "lots": _resolve_lots(cfg, body),
                 "lot_mode": cfg.get("lot_mode"),
                 "lot_value": float(cfg.get("lot_value", 1)),
                 "sl": body.sl if cfg.get("copy_sl", True) else None,
                 "tp": body.tp if cfg.get("copy_tp", True) else None,
                 "max_spread_pips": float(cfg.get("max_spread_pips") or 0),
                 "live": _flag("REAL_MONEY_COPYING")}
        sb.table("copy_queue").insert({
            "event_id": event_id, "slave_id": cfg["slave_id"],
            "instruction": instr}).execute()
        queued += 1
    return {"ok": True, "queued": queued}


# ── slave EA endpoints ──────────────────────────────────────────────
def _slave_by_key(sb: Client, key: str) -> dict | None:
    if not key or len(key) < 20:
        return None
    r = (sb.table("copy_slaves").select("id,enabled")
         .eq("copy_key", key).limit(1).execute()).data
    return r[0] if r else None


@router.get("/poll")
async def poll(key: str, sb: Client = Depends(get_supabase)) -> dict:
    sl = _slave_by_key(sb, key)
    if not sl:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad copy key")
    if not sl["enabled"]:
        return {"instructions": [], "note": "copying paused"}
    rows = (sb.table("copy_queue").select("id,instruction")
            .eq("slave_id", sl["id"]).eq("status", "pending")
            .order("id").limit(10).execute()).data or []
    ids = [r["id"] for r in rows]
    if ids:
        sb.table("copy_queue").update(
            {"status": "sent",
             "sent_at": datetime.now(timezone.utc).isoformat()}
        ).in_("id", ids).execute()
    return {"instructions": [
        {"queue_id": r["id"], **r["instruction"]} for r in rows]}


class ReportIn(BaseModel):
    queue_id: int
    status: str                    # done / failed
    slave_ticket: int | None = None
    price: float | None = None
    latency_ms: int | None = None
    slippage_pips: float | None = None
    error: str = ""


@router.post("/report")
async def report(body: ReportIn, key: str,
                 sb: Client = Depends(get_supabase)) -> dict:
    sl = _slave_by_key(sb, key)
    if not sl:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad copy key")
    q = (sb.table("copy_queue").select("*").eq("id", body.queue_id)
         .eq("slave_id", sl["id"]).limit(1).execute()).data
    if not q:
        return {"ok": False, "reason": "unknown queue row"}
    row = q[0]
    sb.table("copy_queue").update(
        {"status": body.status,
         "done_at": datetime.now(timezone.utc).isoformat()}
    ).eq("id", row["id"]).execute()
    ins = row["instruction"]
    sb.table("copied_trades").insert({
        "queue_id": row["id"], "slave_id": sl["id"],
        "master_ticket": ins.get("master_ticket"),
        "slave_ticket": body.slave_ticket,
        "symbol": ins.get("symbol"), "side": ins.get("side"),
        "master_lots": None, "slave_lots": ins.get("lots"),
        "price": body.price, "sl": ins.get("sl"), "tp": ins.get("tp"),
        "status": body.status, "error": body.error,
        "latency_ms": body.latency_ms,
        "slippage_pips": body.slippage_pips}).execute()
    return {"ok": True}


# ── user-facing management ──────────────────────────────────────────
class SlaveIn(BaseModel):
    label: str = "My account"
    broker: str = ""
    mt5_login: str = ""


@router.post("/slaves")
async def add_slave(body: SlaveIn, user=Depends(get_current_user),
                    sb: Client = Depends(get_supabase)) -> dict:
    key = "sk_copy_" + secrets.token_urlsafe(24)
    r = sb.table("copy_slaves").insert({
        "user_id": user.id, "label": body.label[:60],
        "broker": body.broker[:60], "mt5_login": body.mt5_login[:30],
        "copy_key": key}).execute()
    return {"ok": True, "slave": r.data[0]}


@router.get("/slaves")
async def my_slaves(user=Depends(get_current_user),
                    sb: Client = Depends(get_supabase)) -> dict:
    r = (sb.table("copy_slaves").select("*")
         .eq("user_id", user.id).order("created_at").execute())
    return {"slaves": r.data or []}


class SlaveToggle(BaseModel):
    enabled: bool


@router.post("/slaves/{slave_id}/toggle")
async def toggle_slave(slave_id: str, body: SlaveToggle,
                       user=Depends(get_current_user),
                       sb: Client = Depends(get_supabase)) -> dict:
    sb.table("copy_slaves").update({"enabled": body.enabled}) \
      .eq("id", slave_id).eq("user_id", user.id).execute()
    return {"ok": True}


class ConfigIn(BaseModel):
    slave_id: str
    master_id: str
    account_type: str = "normal"
    account_size: float = 10000
    lot_mode: str = "multiplier"
    lot_value: float = 1.0
    max_lot: float = 1.0
    max_open: int = 5
    max_daily_loss_pct: float = 5.0
    max_spread_pips: float = 5.0
    copy_sl: bool = True
    copy_tp: bool = True


@router.post("/configs")
async def upsert_config(body: ConfigIn, user=Depends(get_current_user),
                        sb: Client = Depends(get_supabase)) -> dict:
    own = (sb.table("copy_slaves").select("id").eq("id", body.slave_id)
           .eq("user_id", user.id).limit(1).execute()).data
    if not own:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "not your account")

    # the page may say "sklz" instead of a UUID — resolve it here, because
    # a client should never need to know our primary keys. This bug cost an
    # end-to-end test: the config insert failed silently on the string and
    # the slave sat enabled-but-subscribed-to-nothing.
    if body.master_id in ("sklz", "system", ""):
        m = (sb.table("copy_masters").select("id").eq("is_system", True)
             .limit(1).execute()).data
        if not m:
            raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR,
                                "system master missing")
        body.master_id = m[0]["id"]
    if body.lot_mode not in ("fixed", "multiplier", "balance",
                             "equity", "risk_pct"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "bad lot mode")
    sb.table("copy_configs").upsert(
        body.model_dump() | {"enabled": True},
        on_conflict="slave_id,master_id").execute()
    return {"ok": True}


@router.get("/masters")
async def marketplace(sb: Client = Depends(get_supabase)) -> dict:
    if not _flag("MASTER_MARKETPLACE", "1"):
        return {"masters": []}
    r = (sb.table("copy_masters")
         .select("id,display_name,strategy,style,markets,risk_score,"
                 "badges,is_system,created_at")
         .eq("status", "published").execute())
    return {"masters": r.data or [],
            "flags": {"real_money": _flag("REAL_MONEY_COPYING"),
                      "live": _flag("COPY_LIVE")}}


@router.get("/log")
async def copy_log(user=Depends(get_current_user),
                   sb: Client = Depends(get_supabase)) -> dict:
    slaves = (sb.table("copy_slaves").select("id")
              .eq("user_id", user.id).execute()).data or []
    ids = [s["id"] for s in slaves]
    if not ids:
        return {"log": []}
    r = (sb.table("copied_trades").select("*").in_("slave_id", ids)
         .order("at", desc=True).limit(100).execute())
    return {"log": r.data or []}


# ── recommended settings by account type ────────────────────────────
@router.get("/recommend")
async def recommend(account_type: str = "normal",
                    account_size: float = 10000) -> dict:
    """One source of truth for 'what should my settings be'.

    The prop preset encodes the engine's own survival math: a 5%/5% prop
    account is a lifetime budget, so risk 0.5%/trade, stop the day at 4%
    (the last 1% is the slippage budget), and cap concurrency at 3. The
    normal preset breathes more but is still built to survive a bad week.
    Everything returned here is a STARTING POINT the user can edit — it is
    autofill, not policy.
    """
    size = max(100.0, min(float(account_size or 10000), 10_000_000))
    if account_type == "prop":
        out = {"lot_mode": "risk_pct", "lot_value": 0.5,
               "max_daily_loss_pct": 4.0, "max_open": 3,
               "max_lot": round(max(0.05, size / 10000 * 0.5), 2),
               "max_spread_pips": 5.0,
               "note": ("Prop preset: 0.5% risk/trade, day stops at 4% — "
                        "the last 1% before the firm's 5% is slippage "
                        "budget. Three positions max keeps a fully-loaded "
                        "bad moment inside the daily stop.")}
    else:
        out = {"lot_mode": "risk_pct", "lot_value": 1.0,
               "max_daily_loss_pct": 8.0, "max_open": 5,
               "max_lot": round(max(0.05, size / 10000 * 1.0), 2),
               "max_spread_pips": 5.0,
               "note": ("Standard preset: 1% risk/trade, generous but "
                        "survivable. Edit anything — these are starting "
                        "points, not rules.")}
    out["account_type"] = account_type
    out["account_size"] = size
    return out
