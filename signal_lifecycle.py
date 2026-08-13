"""SKLZ — signal lifecycle: active → secured → closed, plus honest summaries.

The engine already publishes signals; this module follows them to their end.
Three writers:
  - the engine posts status changes (secured / closed) as they happen
  - the engine's track loop posts live prices; if a price crosses the
    signal's own SL or TP the signal closes itself — so outcomes are recorded
    even if the engine restarts and forgets
  - a daily task posts the summary to Telegram

The summary counts losses with the same font size as wins. That is the point
of it.
"""
from __future__ import annotations

import asyncio
import json
import os
import urllib.request
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from supabase import Client

from db import get_supabase

router = APIRouter(prefix="/api/signals", tags=["signal-lifecycle"])

# same lesson as the engine's USTEC bug: broker aliases must be covered,
# or pips are computed at 10x the wrong scale
PIP = {
    "XAUUSD": 0.1, "GOLD": 0.1, "XAGUSD": 0.01, "SILVER": 0.01,
    "USDJPY": 0.01, "EURJPY": 0.01, "GBPJPY": 0.01, "AUDJPY": 0.01,
    "CADJPY": 0.01, "CHFJPY": 0.01, "NZDJPY": 0.01,
    "US30": 1.0, "NAS100": 1.0, "USTEC": 1.0, "USTECH": 1.0,
    "SPX500": 1.0, "US500": 1.0, "GER40": 1.0, "DE40": 1.0,
    "UK100": 1.0, "JP225": 1.0, "AUS200": 1.0, "HK50": 1.0,
    "USOIL": 0.01, "UKOIL": 0.01, "WTI": 0.01, "XTIUSD": 0.01,
    "USOUSD": 0.01, "UKOUSD": 0.01, "BRENT": 0.01,
    "BTC": 1.0, "ETH": 0.1,
}


def _pip(symbol: str) -> float:
    s = (symbol or "").upper()
    for suf in ("..", ".", "m", "c", "r", "_", "#"):
        if s.endswith(suf):
            s = s[: -len(suf)]
    for k, v in PIP.items():
        if s.startswith(k):
            return v
    return 0.0001


def _pips(symbol: str, side: str, entry: float, price: float) -> float:
    d = (price - entry) if side == "buy" else (entry - price)
    return round(d / _pip(symbol), 1)


def _key_ok(key: str) -> bool:
    expected = os.environ.get("SIGNAL_WEBHOOK_KEY", "")
    ingest = os.environ.get("BOT_INGEST_KEY", "")
    return bool(key) and key in (expected, ingest)


def _auth(request_key: str) -> None:
    if not _key_ok(request_key):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad key")


def _bearer(req) -> str:
    h = req.headers.get("authorization", "")
    return h[7:] if h.lower().startswith("bearer ") else ""


# ── engine-facing endpoints ─────────────────────────────────────────
class StatusIn(BaseModel):
    symbol: str
    side: str                      # "buy" / "sell"
    status: str                    # "secured" / "closed"
    pips: float | None = None      # locked pips or result pips
    money: float | None = None     # realized, if the engine knows it


@router.post("/status")
async def push_status(body: StatusIn, request: Request,
                      sb: Client = Depends(get_supabase)) -> dict:
    _auth(_bearer(request))
    return await _push_status(body, sb)


async def _push_status(body: StatusIn, sb: Client) -> dict:
    # newest signal for this symbol+side that is still running
    res = (sb.table("signals").select("id,entry,side,symbol,status")
           .eq("symbol", body.symbol).eq("side", body.side)
           .in_("status", ["active", "secured"])
           .order("received_at", desc=True).limit(1).execute())
    rows = res.data or []
    if not rows:
        return {"ok": False, "reason": "no running signal for that symbol"}
    sig = rows[0]

    upd: dict = {"tracked_at": datetime.now(timezone.utc).isoformat()}
    if body.status == "secured":
        upd["status"] = "secured"
        if body.pips is not None:
            upd["locked_pips"] = body.pips
    elif body.status == "closed":
        upd["status"] = "closed"
        upd["closed_at"] = datetime.now(timezone.utc).isoformat()
        if body.pips is not None:
            upd["result_pips"] = body.pips
            upd["outcome"] = "win" if body.pips > 0 else "loss"
        elif body.money is not None:
            upd["outcome"] = "win" if body.money > 0 else "loss"
    else:
        return {"ok": False, "reason": f"unknown status {body.status!r}"}

    sb.table("signals").update(upd).eq("id", sig["id"]).execute()
    return {"ok": True, "signal_id": sig["id"], "applied": upd}


@router.get("/open")
async def open_signals(request: Request,
                       sb: Client = Depends(get_supabase)) -> dict:
    _auth(_bearer(request))
    res = (sb.table("signals")
           .select("id,symbol,side,entry,sl,tp1,status,received_at")
           .in_("status", ["active", "secured"])
           .order("received_at", desc=True).limit(60).execute())
    return {"signals": res.data or []}


class TrackIn(BaseModel):
    signal_id: str
    price: float


@router.post("/track")
async def track(body: TrackIn, request: Request,
                sb: Client = Depends(get_supabase)) -> dict:
    _auth(_bearer(request))
    res = (sb.table("signals")
           .select("id,symbol,side,entry,sl,tp1,status,peak_pips")
           .eq("id", body.signal_id).limit(1).execute())
    rows = res.data or []
    if not rows:
        return {"ok": False, "reason": "unknown signal"}
    sig = rows[0]
    if sig["status"] not in ("active", "secured"):
        return {"ok": True, "note": "already closed"}

    entry, side, sym = sig["entry"], sig["side"], sig["symbol"]
    cur = _pips(sym, side, entry, body.price)
    peak = max(cur, sig.get("peak_pips") or 0)

    upd = {"last_price": body.price, "peak_pips": peak,
           "tracked_at": datetime.now(timezone.utc).isoformat()}

    # the signal closes on ITS OWN levels — even if the engine that posted it
    # has restarted and no longer remembers the position
    sl, tp = sig.get("sl"), sig.get("tp1")
    hit_sl = sl and ((side == "buy" and body.price <= sl) or
                     (side == "sell" and body.price >= sl))
    hit_tp = tp and ((side == "buy" and body.price >= tp) or
                     (side == "sell" and body.price <= tp))
    if hit_sl or hit_tp:
        level = sl if hit_sl else tp
        result = _pips(sym, side, entry, level)
        upd.update({"status": "closed",
                    "closed_at": datetime.now(timezone.utc).isoformat(),
                    "result_pips": result,
                    "outcome": "win" if result > 0 else "loss"})

    sb.table("signals").update(upd).eq("id", sig["id"]).execute()
    return {"ok": True, "pips": cur, "closed": bool(hit_sl or hit_tp)}


# ── summary ─────────────────────────────────────────────────────────
def _summarise(sb: Client, days: int) -> dict:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    res = (sb.table("signals")
           .select("status,outcome,result_pips,locked_pips,peak_pips,symbol")
           .gte("received_at", since).limit(1000).execute())
    rows = res.data or []
    closed = [r for r in rows if r["status"] == "closed"]
    wins = [r for r in closed if r.get("outcome") == "win"]
    losses = [r for r in closed if r.get("outcome") == "loss"]
    net = sum(r.get("result_pips") or 0 for r in closed)
    return {
        "days": days,
        "signals": len(rows),
        "closed": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "still_running": sum(1 for r in rows
                             if r["status"] in ("active", "secured")),
        "secured": sum(1 for r in rows if r["status"] == "secured"),
        "net_pips": round(net, 1),
        "win_rate": (round(100 * len(wins) / len(closed))
                     if closed else None),
    }


@router.get("/summary")
async def summary(days: int = 7,
                  sb: Client = Depends(get_supabase)) -> dict:
    return _summarise(sb, max(1, min(days, 90)))


# ── daily Telegram summary ──────────────────────────────────────────
def _tg(chat_id: str, token: str, text: str) -> bool:
    if not (chat_id and token):
        return False
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=json.dumps({"chat_id": chat_id, "text": text,
                             "parse_mode": "Markdown"}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=12) as r:
            return json.loads(r.read().decode()).get("ok", False)
    except Exception:
        return False


def format_summary(day: dict, week: dict) -> str:
    """Honest by construction: losses are never smaller than wins."""
    lines = ["\U0001F4CA *SKLZ signals — daily summary*", ""]
    if day["closed"] == 0 and day["still_running"] == 0:
        lines.append("No signals closed today.")
    else:
        lines.append(f"today: {day['wins']} won · {day['losses']} lost"
                     + (f" · {day['still_running']} still running"
                        if day['still_running'] else ""))
        lines.append(f"net: *{day['net_pips']:+.1f} pips*")
    lines += ["",
              f"7 days: {week['wins']}W / {week['losses']}L"
              + (f" ({week['win_rate']}%)" if week['win_rate'] is not None
                 else ""),
              f"7-day net: *{week['net_pips']:+.1f} pips*"]
    if (week["wins"] + week["losses"]) < 30:
        lines += ["", "_small sample — treat the percentages accordingly._"]
    lines += ["", "every signal tracked to its close, including the misses."]
    return "\n".join(lines)


async def summary_loop(app=None) -> None:
    """Posts once per day at SIGNAL_SUMMARY_HOUR_UTC (default 20)."""
    from db import admin_client  # fresh client, not a shared one
    sent_on: str | None = None
    while True:
        try:
            hour = int(os.environ.get("SIGNAL_SUMMARY_HOUR_UTC", "20"))
        except ValueError:
            hour = 20
        now = datetime.now(timezone.utc)
        today = now.strftime("%Y-%m-%d")
        if now.hour == hour and sent_on != today:
            try:
                sb = admin_client()
                day = _summarise(sb, 1)
                week = _summarise(sb, 7)
                text = format_summary(day, week)
                token = (os.environ.get("TG_SALES_BOT_TOKEN")
                         or os.environ.get("TELEGRAM_BOT_TOKEN", ""))
                sent = 0
                main_chat = (os.environ.get("SIGNAL_CHANNEL_ID")
                             or os.environ.get("ALERT_CHANNEL_ID", ""))
                if _tg(main_chat, token, text):
                    sent += 1
                m2_chat = os.environ.get("TG_MIRROR2_CHAT", "")
                m2_tok = os.environ.get("TG_MIRROR2_TOKEN", "") or token
                if _tg(m2_chat, m2_tok, text):
                    sent += 1
                sent_on = today
                print(f"[signal-summary] posted to {sent} channel(s)")
            except Exception as exc:  # noqa: BLE001
                print(f"[signal-summary] failed: {type(exc).__name__}: {exc}")
        await asyncio.sleep(120)


def start(app) -> None:
    asyncio.get_event_loop().create_task(summary_loop(app))
