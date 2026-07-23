"""SKLZ — public live bot stats for the landing page.

Shows TODAY only, from real journaled trades on the account the owner has
published. No authentication: this is the storefront.

Honesty rules baked in:
  - the account type (demo/live) is always returned and must be displayed
  - a losing day returns the loss; there is no filtering, smoothing or
    "best day" selection
  - if there are no closed trades today, it says so rather than falling back
    to yesterday's better number
  - the equity curve is today's running P/L, not a cherry-picked window

Set SKLZ_PUBLIC_ACCOUNT_ID to the journal account to publish. If unset, the
endpoint reports that nothing is published rather than guessing.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from supabase import Client

from db import get_supabase

router = APIRouter(prefix="/api/public", tags=["public"])


def _num(v, d=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def _parse(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None


@router.get("/live-stats")
async def live_stats(sb: Client = Depends(get_supabase)) -> dict:
    """Today's real performance on the published account. Public, no auth."""
    acct_id = os.environ.get("SKLZ_PUBLIC_ACCOUNT_ID", "").strip()
    now = datetime.now(timezone.utc)
    today = now.date()

    base = {
        "published": bool(acct_id),
        "date": today.isoformat(),
        "updated_at": now.isoformat(),
        "disclaimer": ("Today's results only, from real journaled trades. "
                       "Past performance does not guarantee future results."),
    }
    if not acct_id:
        return {**base, "message": "No account is published yet."}

    # account context — the mode label matters more than the numbers
    try:
        acc = (sb.table("journal_accounts").select("label,platform,broker,kind,connected")
               .eq("id", acct_id).execute()).data
    except Exception:
        acc = None
    account = acc[0] if acc else {}

    # today's closed trades
    since = (now - timedelta(hours=36)).isoformat()
    try:
        rows = (sb.table("journal_trades").select("*")
                .eq("account_id", acct_id)
                .gte("closed_at", since)
                .order("closed_at").limit(300).execute()).data or []
    except Exception:
        rows = []

    todays = []
    for t in rows:
        d = _parse(t.get("closed_at"))
        if d and d.date() == today:
            todays.append(t)

    if not todays:
        return {**base,
                "account": {"label": account.get("label", ""),
                            "mode": (account.get("kind") or "demo").upper(),
                            "platform": account.get("platform", ""),
                            "broker": account.get("broker", "")},
                "has_trades_today": False,
                "trades": 0,
                "message": "No trades closed yet today.",
                "curve": []}

    pnls = [_num(t.get("pnl")) for t in todays]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    net = sum(pnls)

    curve, run = [], 0.0
    for p in pnls:
        run += p
        curve.append(round(run, 2))

    peak, dd = 0.0, 0.0
    for v in curve:
        peak = max(peak, v)
        dd = min(dd, v - peak)

    last = todays[-1]
    return {
        **base,
        "account": {"label": account.get("label", ""),
                    "mode": (account.get("kind") or "demo").upper(),
                    "platform": account.get("platform", ""),
                    "broker": account.get("broker", "")},
        "has_trades_today": True,
        "trades": len(todays),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(todays), 4) if todays else 0,
        "net_pnl": round(net, 2),
        "best": round(max(pnls), 2) if pnls else 0,
        "worst": round(min(pnls), 2) if pnls else 0,
        "max_drawdown": round(abs(dd), 2),
        "curve": curve,
        "last_trade": {
            "symbol": last.get("symbol"),
            "side": last.get("side"),
            "pnl": round(_num(last.get("pnl")), 2),
            "closed_at": last.get("closed_at"),
        },
    }
