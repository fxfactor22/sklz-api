"""SKLZ — trade update board.

Three jobs:

  1. The bot reports stop moves, secures and closes as they happen.
  2. The owner can broadcast a message to the audience by hand.
  3. Signals carry a live status so a follower can see whether a call is
     still running, has been secured, or is no longer valid.

Everything written here also goes to Telegram, so the people following in
the channel see the same updates as the people on the dashboard. A signal
that moves to break-even and is never mentioned again is how signal
services lose trust; this is the fix for that.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel
from supabase import Client

from auth import get_current_user
from db import get_supabase

router = APIRouter(prefix="/api/updates", tags=["updates"])

KINDS = ("trail", "secured", "closed", "invalid", "info", "announcement")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _internal_key() -> str:
    return (os.environ.get("INTERNAL_KEY", "")
            or os.environ.get("BOT_INGEST_KEY", ""))


def _is_admin(user) -> bool:
    admins = {e.strip().lower() for e in
              os.environ.get("ADMIN_EMAILS", "fxfactor24@gmail.com").split(",")}
    return (getattr(user, "email", "") or "").lower() in admins


# ── formatting for Telegram ─────────────────────────────────────────
ICON = {"trail": "\U0001F512", "secured": "\u2705", "closed": "\U0001F3C1",
        "invalid": "\u26A0\uFE0F", "info": "\u2139\uFE0F",
        "announcement": "\U0001F4E3"}


def _format(u: dict) -> str:
    icon = ICON.get(u.get("kind", "info"), "\u2139\uFE0F")
    lines = [f"{icon} *{u.get('headline','')}*"]
    sym = u.get("symbol")
    if sym:
        side = (u.get("side") or "").upper()
        lines.append(f"{sym}{(' ' + side) if side else ''}")
    if u.get("new_sl") is not None:
        old = u.get("old_sl")
        lines.append(f"Stop moved{f' from {old}' if old else ''} to *{u['new_sl']}*")
    if u.get("locked_pips") is not None:
        lp = u["locked_pips"]
        lines.append(f"{'Locked in' if lp >= 0 else 'Risk reduced to'} "
                     f"*{abs(lp):.0f} pips*")
    if u.get("detail"):
        lines.append("")
        lines.append(u["detail"])
    return "\n".join(lines)


def _broadcast(u: dict, log=print) -> dict:
    """Send to the general channel and the matching category channel."""
    try:
        from signals_engine import send_to_telegram, classify
    except Exception:  # noqa: BLE001
        return {"sent": False, "reason": "signals engine unavailable"}
    category = "general"
    try:
        if u.get("symbol"):
            category = classify(u["symbol"])
    except Exception:
        pass
    try:
        return send_to_telegram(category, _format(u))
    except Exception as exc:  # noqa: BLE001
        return {"sent": False, "reason": str(exc)[:120]}


def _save(sb: Client, row: dict) -> dict:
    try:
        res = sb.table("trade_updates").insert(row).execute()
        return (res.data or [row])[0]
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR,
                            f"could not save update: {str(exc)[:200]}") from exc


# ── bot-reported updates (internal key) ─────────────────────────────
class BotUpdate(BaseModel):
    kind: str = "trail"
    symbol: str = ""
    side: str = ""
    ticket: str = ""
    headline: str = ""
    detail: str = ""
    old_sl: float | None = None
    new_sl: float | None = None
    locked_pips: float | None = None
    price: float | None = None
    broadcast: bool = True


@router.post("/bot")
async def bot_update(body: BotUpdate, authorization: str = Header(default=""),
                     sb: Client = Depends(get_supabase)) -> dict:
    """The runner posts here when it moves a stop, secures or closes a trade."""
    expected = _internal_key()
    if not expected or authorization.replace("Bearer ", "").strip() != expected:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "unauthorized")

    kind = body.kind if body.kind in KINDS else "info"
    headline = body.headline or {
        "trail": f"Stop trailed on {body.symbol}",
        "secured": f"{body.symbol} secured — trade can no longer lose",
        "closed": f"{body.symbol} closed",
        "invalid": f"{body.symbol} setup no longer valid",
    }.get(kind, "Update")

    row = {"kind": kind, "symbol": body.symbol, "side": body.side,
           "ticket": body.ticket, "headline": headline, "detail": body.detail,
           "old_sl": body.old_sl, "new_sl": body.new_sl,
           "locked_pips": body.locked_pips, "price": body.price,
           "broadcast": body.broadcast, "created_at": _now()}
    saved = _save(sb, row)

    # keep the matching signal's live status in step
    if body.symbol:
        try:
            sig = (sb.table("signals").select("id")
                   .eq("symbol", body.symbol)
                   .in_("status", ["active", "secured"])
                   .order("created_at", desc=True).limit(1).execute()).data
            if sig:
                upd = {"updated_at": _now()}
                if kind in ("secured", "trail"):
                    upd["status"] = "secured"
                    if body.new_sl is not None:
                        upd["current_sl"] = body.new_sl
                    if body.locked_pips is not None:
                        upd["locked_pips"] = body.locked_pips
                elif kind == "closed":
                    upd["status"] = "closed"
                elif kind == "invalid":
                    upd["status"] = "invalid"
                if body.ticket:
                    upd["ticket"] = body.ticket
                sb.table("signals").update(upd).eq("id", sig[0]["id"]).execute()
                saved["linked_signal"] = sig[0]["id"]
        except Exception:
            pass

    tg = _broadcast(row) if body.broadcast else {"sent": False, "reason": "muted"}
    return {"ok": True, "update": saved, "telegram": tg}


# ── owner announcements ─────────────────────────────────────────────
class Announcement(BaseModel):
    headline: str
    detail: str = ""
    symbol: str = ""
    broadcast: bool = True


@router.post("/announce")
async def announce(body: Announcement, user=Depends(get_current_user),
                   sb: Client = Depends(get_supabase)) -> dict:
    """Owner posts a message to the board and the channels."""
    if not _is_admin(user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "admin only")
    row = {"kind": "announcement", "symbol": body.symbol,
           "headline": body.headline[:160], "detail": body.detail[:900],
           "broadcast": body.broadcast, "created_by": str(user.id),
           "created_at": _now()}
    saved = _save(sb, row)
    tg = _broadcast(row) if body.broadcast else {"sent": False, "reason": "muted"}
    return {"ok": True, "update": saved, "telegram": tg}


@router.get("/feed")
async def feed(limit: int = 40, sb: Client = Depends(get_supabase)) -> dict:
    """PUBLIC — the update board. Followers see the same thing the channel does."""
    try:
        rows = (sb.table("trade_updates").select("*")
                .order("created_at", desc=True).limit(min(limit, 100))
                .execute()).data or []
    except Exception:
        rows = []
    return {"updates": rows, "count": len(rows)}


@router.delete("/{update_id}")
async def delete_update(update_id: str, user=Depends(get_current_user),
                        sb: Client = Depends(get_supabase)) -> dict:
    if not _is_admin(user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "admin only")
    sb.table("trade_updates").delete().eq("id", update_id).execute()
    return {"ok": True}
