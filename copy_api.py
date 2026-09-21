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
import time
from datetime import datetime, timedelta, timezone

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
    from keyauth import engine_key_ok
    return engine_key_ok(key, "/api/mt5copy/event")


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
                 # These three were stored in the config, shown on the
                 # dashboard, and never put in the instruction — so the EA
                 # read them as zero and every guard disabled itself. A
                 # limit the owner believes in but that is never sent is
                 # worse than no limit at all.
                 "max_lot": float(cfg.get("max_lot") or 0),
                 "max_open": float(cfg.get("max_open") or 0),
                 "max_daily_loss_pct": float(cfg.get("max_daily_loss_pct") or 0),
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


# ── EA liveness ─────────────────────────────────────────────────────
# Nothing here recorded whether a follower's terminal was polling at
# all, so "not copying" could not be split into "never asked" and
# "asked and refused". The poll is the heartbeat. It is remembered in
# process every time and persisted to copy_slaves.last_poll_at at most
# every 30s, so the 2s poll loop does not become a write storm.
_LAST_POLL: dict[str, float] = {}       # slave_id -> unix ts
_LAST_POLL_DB: dict[str, float] = {}    # slave_id -> ts of last persist
_POLL_PERSIST_EVERY = 30.0
_POLL_OFFLINE_AFTER = 120.0             # seconds without a poll = offline


def _note_poll(sb: Client, slave_id: str) -> None:
    now = time.time()
    _LAST_POLL[slave_id] = now
    if now - _LAST_POLL_DB.get(slave_id, 0.0) < _POLL_PERSIST_EVERY:
        return
    _LAST_POLL_DB[slave_id] = now
    try:
        sb.table("copy_slaves").update(
            {"last_poll_at": datetime.now(timezone.utc).isoformat()}
        ).eq("id", slave_id).execute()
    except Exception as e:  # column missing until migration P13 runs
        _LAST_POLL_DB[slave_id] = now + 3600.0   # in-memory still works
        print(f"[mt5copy] last_poll_at not persisted ({type(e).__name__}) "
              "— run migrations/P13-copy-last-poll.sql")


@router.get("/poll")
async def poll(key: str, co: str = "",
               sb: Client = Depends(get_supabase)) -> dict:
    sl = _slave_by_key(sb, key)
    if not sl:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad copy key")
    _note_poll(sb, sl["id"])
    if not sl["enabled"]:
        return {"instructions": [], "note": "copying paused"}
    # the EA reports its terminal's ACCOUNT_COMPANY (co=). Declared broker
    # is checked at creation; this is the check that can't be typed around.
    if co:
        import sklz_tiers as ent
        owner = (sb.table("copy_slaves").select("user_id")
                 .eq("id", sl["id"]).limit(1).execute()).data
        if owner:
            e = ent.entitlements_for(sb, owner[0]["user_id"])
            if not e["any_broker"] and not ent.broker_allowed(co):
                return {"instructions": [],
                        "note": "this broker needs SKLZ Pro — your plan "
                                "copies on our partner broker only"}
    # STALE OPENS DO NOT EXECUTE. A slave EA that reconnects after an
    # outage must not fire entries queued at long-dead prices — five
    # 40-minute-old opens were found waiting for exactly that. Opens
    # older than 90 seconds expire unfetched; closes always deliver,
    # because closing a position you hold is right at any age.
    cutoff = (datetime.now(timezone.utc)
              - timedelta(seconds=90)).isoformat()
    stale = (sb.table("copy_queue").select("id,instruction")
             .eq("slave_id", sl["id"]).eq("status", "pending")
             .lt("created_at", cutoff).execute()).data or []
    stale_ids = [r["id"] for r in stale
                 if (r.get("instruction") or {}).get("event") == "open"]
    if stale_ids:
        sb.table("copy_queue").update({"status": "expired"}) \
          .in_("id", stale_ids).execute()

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
    # entitlement wall: MT5 copy is a paid feature, and each tier draws
    # its own lines — account count and broker. The error text always
    # names the unlock, because a paywall that just says "no" is a
    # support ticket, not a conversion.
    import sklz_tiers as ent
    e = ent.entitlements_for(sb, user.id)
    if e["mt5_max_accounts"] < 1:
        raise HTTPException(
            status.HTTP_402_PAYMENT_REQUIRED,
            "MT5 copy trading starts with SKLZ Core ($29/mo).")
    have = (sb.table("copy_slaves").select("id", count="exact")
            .eq("user_id", user.id).execute()).count or 0
    if have >= e["mt5_max_accounts"]:
        raise HTTPException(
            status.HTTP_402_PAYMENT_REQUIRED,
            f"Your plan includes {e['mt5_max_accounts']} MT5 account(s). "
            f"SKLZ Pro ($79/mo) unlocks multiple accounts on any broker.")
    if not e["any_broker"] and body.broker and not ent.broker_allowed(body.broker):
        raise HTTPException(
            status.HTTP_402_PAYMENT_REQUIRED,
            "Your plan copies on our partner broker only. SKLZ Pro "
            "($79/mo) unlocks any broker — or open a partner-broker "
            "account from the dashboard.")
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

    # RESUME IS ALSO REPAIR. A slave without a config row is "connected"
    # on the dashboard and invisible to the fan-out — a stuck state that
    # cost a full evening of debugging across four separate causes. If the
    # user explicitly turns copying ON and no subscription to the system
    # master exists, create one with the conservative defaults; the wizard
    # can refine it, but a Resume must never be a no-op again.
    ensured = False
    if body.enabled:
        m = (sb.table("copy_masters").select("id").eq("is_system", True)
             .limit(1).execute()).data
        if m:
            has = (sb.table("copy_configs").select("id")
                   .eq("slave_id", slave_id).eq("master_id", m[0]["id"])
                   .limit(1).execute()).data
            if not has:
                sb.table("copy_configs").insert({
                    "slave_id": slave_id, "master_id": m[0]["id"],
                    "lot_mode": "risk_pct", "lot_value": 0.5,
                    "max_lot": 1.0, "max_open": 3,
                    "max_daily_loss_pct": 4.0, "max_spread_pips": 5.0,
                    "enabled": True}).execute()
                ensured = True
    return {"ok": True, "config_created": ensured}


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


# ── admin diagnosis: where does the chain stop for each follower? ───
# Master event -> copy_events -> copy_queue(pending) -> EA poll (sent)
# -> EA executes -> report (done/failed + copied_trades). "Not copying"
# is one of six different failures along that line, and every one of
# them was silent. This names the link that broke, per follower, in a
# sentence, without ever returning a copy key.
_ADMIN_NAMES = ("SIGNAL_WEBHOOK_KEY",)


def _admin(request: Request) -> None:
    from keyauth import engine_key_ok
    if not engine_key_ok(_bearer(request), "/api/mt5copy/diag",
                         names=_ADMIN_NAMES):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "admin only")


def _age(iso: str | None, now: datetime) -> int | None:
    if not iso:
        return None
    try:
        t = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return int((now - t).total_seconds())
    except ValueError:
        return None


def _verdict(slave: dict, cfg: dict | None, q: dict, trades: list,
             events_24h: int, poll_age: int | None) -> str:
    if not slave.get("enabled"):
        alive = (poll_age is not None and poll_age <= _POLL_OFFLINE_AFTER)
        return ("PAUSED on the dashboard — press Resume. "
                + (f"The EA is polling ({poll_age}s ago) and being told "
                   "'copying paused'; it copies from the next master trade "
                   "the moment you resume." if alive else
                   "The EA is not polling either — resume AND start the "
                   "terminal."))
    if not cfg:
        return ("NO SUBSCRIPTION to the SKLZ Engine master — Resume on the "
                "dashboard creates one; the fan-out skips this account.")
    if not cfg.get("enabled"):
        return "config DISABLED — re-save the settings on the dashboard."
    if poll_age is None:
        return ("EA NOT POLLING — no poll seen since this deploy. On the "
                "follower terminal: EA attached to a chart? AutoTrading "
                "on? api.sklzlabs.com in Tools>Options>Expert Advisors>"
                "WebRequest? Experts tab shows 'SKLZ COPY slave active'?")
    if poll_age > _POLL_OFFLINE_AFTER:
        return (f"EA OFFLINE — last poll {poll_age}s ago. Terminal closed, "
                "VPS asleep, or the EA was removed from the chart.")
    fails = [t for t in trades if t.get("status") == "failed"]
    if fails:
        errs = []
        for t in fails:
            e = (t.get("error") or "").strip() or "(no error text)"
            if e not in errs:
                errs.append(e)
        return ("EA REFUSING or the broker rejecting — last errors: "
                + " | ".join(errs[:4]))
    if q.get("expired", 0) and not q.get("done", 0):
        return ("OPENS EXPIRED UNFETCHED — instructions waited >90s. The "
                "EA is polling now but was not when the master traded.")
    if events_24h == 0:
        return ("MASTER SILENT — no master events in 24h. On the VPS: is "
                "SKLZ_COPY_PUBLISH on, and does the runner log "
                "'[copy]' after an entry? Dashboard/forced entries may "
                "not publish.")
    if not any(q.values()):
        return ("EVENTS ARRIVE BUT NOTHING QUEUED for this account — the "
                "symbol is blocked/not allowed in the config, or the "
                "account was paused when the master traded.")
    if q.get("sent", 0) and not q.get("done", 0) and not fails:
        return ("EA FETCHED BUT NEVER REPORTED — instructions left the "
                "queue and no fill/failure came back. Report HTTP errors "
                "in the Experts tab (WebRequest POST blocked?).")
    return "HEALTHY — polling, queued, executed and reported."


def _rows(errors: list, what: str, q) -> list:
    """A diagnostic must never die on the thing it is diagnosing. A
    query that fails is reported by name and the rest still runs."""
    try:
        return q.execute().data or []
    except Exception as e:
        errors.append(f"{what}: {type(e).__name__}: {str(e)[:200]}")
        return []


@router.get("/diag")
async def diag(request: Request,
               sb: Client = Depends(get_supabase)) -> dict:
    _admin(request)
    now = datetime.now(timezone.utc)
    since = (now - timedelta(hours=24)).isoformat()
    errors: list[str] = []

    m = _rows(errors, "copy_masters", sb.table("copy_masters")
              .select("id,status").eq("is_system", True).limit(1))
    master = m[0] if m else None
    events = []
    if master:
        # copy_events carries no created_at (the queue does); take the
        # newest rows by id and keep the ones stamped inside 24h under
        # whichever timestamp column the table has.
        raw = _rows(errors, "copy_events", sb.table("copy_events")
                    .select("*").eq("master_id", master["id"])
                    .order("id", desc=True).limit(200))
        for e in raw:
            ts = e.get("created_at") or e.get("at") or e.get("ts")
            if ts is None or str(ts) >= since:
                e["created_at"] = ts
                events.append(e)
        events = events[:20]
    slaves = _rows(errors, "copy_slaves",
                   sb.table("copy_slaves").select("*").order("created_at"))
    cfgs = _rows(errors, "copy_configs", sb.table("copy_configs").select("*"))
    cfg_by_slave = {}
    for c in cfgs:
        if master and c.get("master_id") == master["id"]:
            cfg_by_slave[c.get("slave_id")] = c

    out = []
    for s in slaves:
        sid = s.get("id")
        queue = _rows(errors, f"copy_queue[{sid}]",
                      sb.table("copy_queue").select("*").eq("slave_id", sid)
                      .gte("created_at", since).order("id", desc=True)
                      .limit(50))
        trades = _rows(errors, f"copied_trades[{sid}]",
                       sb.table("copied_trades").select("*")
                       .eq("slave_id", sid).order("at", desc=True).limit(10))
        counts: dict[str, int] = {}
        for r in queue:
            st = r.get("status") or "?"
            counts[st] = counts.get(st, 0) + 1
        mem = _LAST_POLL.get(sid)
        poll_age = (int(time.time() - mem) if mem
                    else _age(s.get("last_poll_at"), now))
        cfg = cfg_by_slave.get(sid)
        cfg_view = None
        if cfg:
            cfg_view = {k: cfg.get(k) for k in (
                "enabled", "lot_mode", "lot_value", "min_lot", "max_lot",
                "max_open", "max_daily_loss_pct", "max_spread_pips",
                "copy_sl", "copy_tp", "allowed_symbols", "blocked_symbols",
                "symbol_map") if k in cfg}
        out.append({
            "slave_id": sid, "label": s.get("label"),
            "broker": s.get("broker"), "mt5_login": s.get("mt5_login"),
            "enabled": s.get("enabled"),
            "last_poll_seconds_ago": poll_age,
            "config": cfg_view,
            "queue_24h": counts,
            "last_instructions": [
                {"status": r.get("status"), "created_at": r.get("created_at"),
                 "sent_at": r.get("sent_at"),
                 **{k: (r.get("instruction") or {}).get(k)
                    for k in ("event", "symbol", "lots", "sl", "live")}}
                for r in queue[:5]],
            "last_reports": [
                {k: t.get(k) for k in ("at", "status", "error", "symbol",
                                       "slave_lots", "slave_ticket")}
                for t in trades[:5]],
            "verdict": _verdict(s, cfg, counts, trades, len(events),
                                poll_age),
        })

    return {
        "checked_at": now.isoformat(),
        "errors": errors,
        "flags": {"real_money": _flag("REAL_MONEY_COPYING"),
                  "live": _flag("COPY_LIVE")},
        "master": ({"status": master.get("status")} if master
                   else {"status": "MISSING — no system master row"}),
        "master_events_24h": len(events),
        "last_master_events": [
            {k: e.get(k) for k in ("created_at", "event", "symbol", "lots",
                                   "sl", "tp", "master_ticket")}
            for e in events[:5]],
        "followers": out,
    }
