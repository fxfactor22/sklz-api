"""SKLZ — public live bot stats for the landing page.

Shows TODAY (and, as context, the last 30 days) from real journaled trades
on the account the owner has published. No authentication: this is the
storefront.

Honesty rules baked in:
  - the account type (demo/live/prop) is always returned and must be displayed
  - a losing day returns the loss; there is no filtering, smoothing or
    "best day" selection
  - if there are no closed trades today, it says so rather than falling back
    to yesterday's better number; the 30-day block is labelled as such
  - the equity curve is today's running P/L, not a cherry-picked window

Which account is published:
  SKLZ_PUBLIC_ACCOUNT_ID       journal_accounts.id (takes precedence), or
  SKLZ_PUBLIC_ACCOUNT_NO       the broker login (e.g. 1444538) and optionally
  SKLZ_PUBLIC_ACCOUNT_SERVER   the server name, as the Runner auto-registers
                               them on /api/journal/ingest.
If neither is set, the endpoint reports that nothing is published rather
than guessing.

/api/public/track-record returns the full record of the same account —
the same numbers the Trader Marketplace computes — so the pricing page's
"honest track record" words always point at a real page.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from supabase import Client

from db import get_supabase

router = APIRouter(prefix="/api/public", tags=["public"])

WINDOW_DAYS = 30


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


def _mode(account: dict) -> str:
    kind = (account.get("kind") or "demo").lower()
    return {"prop": "LIVE · PROP", "live": "LIVE"}.get(kind, "DEMO")


def resolve_published_account(sb: Client) -> dict | None:
    """The journal account the owner chose to publish, or None.

    Resolution order: explicit id, then broker login (+server). The login
    form is the practical one — it is what the Runner prints at start-up —
    so the owner never has to dig a UUID out of the database.
    """
    acct_id = os.environ.get("SKLZ_PUBLIC_ACCOUNT_ID", "").strip()
    acct_no = os.environ.get("SKLZ_PUBLIC_ACCOUNT_NO", "").strip()
    server = os.environ.get("SKLZ_PUBLIC_ACCOUNT_SERVER", "").strip()
    cols = "id,label,platform,broker,server,account_no,kind,connected"
    try:
        if acct_id:
            rows = (sb.table("journal_accounts").select(cols)
                    .eq("id", acct_id).execute()).data
        elif acct_no:
            q = sb.table("journal_accounts").select(cols).eq("account_no", acct_no)
            if server:
                q = q.eq("server", server)
            rows = q.order("created_at", desc=True).limit(1).execute().data
        else:
            return None
    except Exception:
        return None
    return rows[0] if rows else None


def _trades(sb: Client, acct_id: str, since_iso: str | None = None,
            limit: int = 5000) -> list[dict]:
    try:
        q = sb.table("journal_trades").select("*").eq("account_id", acct_id)
        if since_iso:
            q = q.gte("closed_at", since_iso)
        return q.order("closed_at").limit(limit).execute().data or []
    except Exception:
        return []


def _window_summary(rows: list[dict], start, today) -> dict:
    """Closed-trade summary for [start, today] with a per-day cumulative curve."""
    closed = [t for t in rows if (_parse(t.get("closed_at")) or datetime.min.replace(tzinfo=timezone.utc)).date() >= start]
    pnls = [_num(t.get("pnl")) for t in closed]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_w, gross_l = sum(wins), abs(sum(losses))
    by_day: dict[str, float] = {}
    for t, p in zip(closed, pnls):
        d = _parse(t.get("closed_at"))
        if d:
            by_day[d.date().isoformat()] = by_day.get(d.date().isoformat(), 0.0) + p
    curve, run = [], 0.0
    day = start
    while day <= today:
        run += by_day.get(day.isoformat(), 0.0)
        curve.append(round(run, 2))
        day += timedelta(days=1)
    peak, dd = 0.0, 0.0
    for v in curve:
        peak = max(peak, v)
        dd = min(dd, v - peak)
    return {
        "days": WINDOW_DAYS,
        "since": start.isoformat(),
        "trades": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(closed), 4) if closed else 0,
        "net_pnl": round(sum(pnls), 2),
        "profit_factor": round(gross_w / gross_l, 2) if gross_l else None,
        "avg_win": round(gross_w / len(wins), 2) if wins else 0,
        "avg_loss": round(-gross_l / len(losses), 2) if losses else 0,
        "max_drawdown": round(abs(dd), 2),
        "trading_days": len(by_day),
        "curve": curve,
    }


@router.get("/live-stats")
async def live_stats(sb: Client = Depends(get_supabase)) -> dict:
    """Today's real performance on the published account. Public, no auth."""
    now = datetime.now(timezone.utc)
    today = now.date()

    base = {
        "published": False,
        "date": today.isoformat(),
        "updated_at": now.isoformat(),
        "disclaimer": ("Today's results only, from real journaled trades. "
                       "Past performance does not guarantee future results."),
    }
    account = resolve_published_account(sb)
    if not account:
        return {**base, "message": "No account is published yet."}
    base["published"] = True
    acct_id = account["id"]

    acc_out = {"label": account.get("label", ""),
               "mode": _mode(account),
               "kind": (account.get("kind") or "demo").lower(),
               "platform": account.get("platform", ""),
               "broker": account.get("broker", ""),
               "account_no": account.get("account_no", "")}

    window_start = today - timedelta(days=WINDOW_DAYS - 1)
    rows = _trades(sb, acct_id, (now - timedelta(days=WINDOW_DAYS + 1)).isoformat())
    todays = [t for t in rows
              if (d := _parse(t.get("closed_at"))) and d.date() == today]
    window = _window_summary(rows, window_start, today)

    if not todays:
        return {**base,
                "account": acc_out,
                "has_trades_today": False,
                "trades": 0,
                "message": "No trades closed yet today.",
                "curve": [],
                "window": window,
                "track_record_url": "/perf.html?c=live"}

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
        "account": acc_out,
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
        "window": window,
        "track_record_url": "/perf.html?c=live",
    }


@router.get("/track-record")
async def track_record(sb: Client = Depends(get_supabase)) -> dict:
    """PUBLIC — the full record of the published account.

    Same payload shape as /api/journal/public/{code} (so perf.html renders
    it unchanged) plus the marketplace metrics and honest rating, a monthly
    table, and a data-source label. Nothing is filtered: a losing month is
    a row like any other.
    """
    from journal import compute_analytics
    from trader_metrics import compute, honest_rating

    account = resolve_published_account(sb)
    if not account:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no account is published")
    trades = _trades(sb, account["id"])

    bot = sum(1 for t in trades if t.get("source") in ("bot", "dashboard"))
    manual = len(trades) - bot
    source = ("MT5-tracked" if account.get("connected") and bot >= manual
              else "manually logged" if manual > bot else "mixed")

    months: dict[str, dict] = {}
    for t in trades:
        if t.get("outcome") not in ("win", "loss", "flat"):
            continue
        d = _parse(t.get("closed_at") or t.get("opened_at"))
        if not d:
            continue
        key = d.strftime("%Y-%m")
        m = months.setdefault(key, {"month": key, "trades": 0, "wins": 0,
                                    "losses": 0, "pnl": 0.0})
        p = _num(t.get("pnl"))
        m["trades"] += 1
        m["pnl"] = round(m["pnl"] + p, 2)
        if p > 0:
            m["wins"] += 1
        elif p < 0:
            m["losses"] += 1
    monthly = [months[k] for k in sorted(months)]

    first = next((t.get("closed_at") or t.get("opened_at") for t in trades), None)
    metrics = compute(trades)
    return {
        "account": {"label": account.get("label"), "platform": account.get("platform"),
                    "broker": account.get("broker"), "server": account.get("server"),
                    "account_no": account.get("account_no"),
                    "kind": account.get("kind"), "mode": _mode(account),
                    "connected": account.get("connected", False)},
        "data_source": source,
        "analytics": compute_analytics(trades),
        "metrics": metrics,
        "rating": honest_rating(metrics),
        "monthly": monthly,
        "trade_count": len(trades),
        "first_trade_at": first,
        "generated": datetime.now(timezone.utc).isoformat(),
        "disclaimer": ("Every number on this page is computed from the journaled "
                       "trades of this one account, including losses. Past "
                       "performance does not guarantee future results."),
    }


# ------------------------------------------------------------------ engine pulse
# A heartbeat, not a P/L. The home page shows that the engine is alive and
# working (scanning, how many positions open, entries today) without
# publishing an account's money. Nothing here comes from journal_accounts;
# it is the same heartbeat the Bot Monitor reads, with equity, balance and
# the open-position list stripped out.
_PULSE_CACHE: dict = {"at": None, "body": None}


@router.get("/engine-pulse")
async def engine_pulse(sb: Client = Depends(get_supabase)) -> dict:
    """Is the SKLZ Master engine alive right now? Public, no auth, cached 15 s."""
    now = datetime.now(timezone.utc)
    c = _PULSE_CACHE
    if c["at"] and (now - c["at"]).total_seconds() < 15 and c["body"]:
        return c["body"]
    want = (os.environ.get("SKLZ_PUBLIC_BOT_NAME") or "SKLZ Master").strip().lower()
    body = {"alive": False, "bot": None, "mode": None, "last_seen": None,
            "age_sec": None, "open_now": 0, "opened_today": 0, "closed_today": 0,
            "setups_armed": 0, "auto": None, "updated_at": now.isoformat(),
            "note": "Heartbeat only. No account figures are published here."}
    try:
        rows = (sb.table("bot_sessions")
                  .select("bot,mode,last_seen,stats")
                  .order("last_seen", desc=True).limit(20).execute()).data or []
    except Exception:  # noqa: BLE001 — storefront never 500s on a heartbeat
        rows = []
    sess = next((r for r in rows if (r.get("bot") or "").strip().lower() == want), None) \
        or (rows[0] if rows else None)
    if sess:
        st = sess.get("stats") or {}
        ls = sess.get("last_seen")
        age = None
        if ls:
            try:
                age = int((now - datetime.fromisoformat(ls.replace("Z", "+00:00"))).total_seconds())
            except ValueError:
                age = None
        body.update({
            "bot": sess.get("bot"), "mode": sess.get("mode"), "last_seen": ls,
            "age_sec": age, "alive": age is not None and age < 180,
            "open_now": int(st.get("open_now") or 0),
            "opened_today": int(st.get("positions_opened") or 0),
            "closed_today": int(st.get("positions_closed") or 0),
            "setups_armed": int(st.get("signals") or 0),
            "auto": st.get("auto"),
        })
    c["at"], c["body"] = now, body
    return body
