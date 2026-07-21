"""SKLZ Journal — the honest trading journal.

What makes it beat the incumbents, by design (not feature-count):
  1. INTENT CAPTURE. Every trade stores WHY it was taken (the trader's
     reasoning), not just price/size. Broker-import journals reverse-engineer
     the setup; ours records the trader's actual thesis at entry.
  2. HONEST EDGE. Analytics separate what the trader BELIEVES works from what
     the data shows works — including surfacing setups that are coin-flips.
     No other journal tells a customer their favourite strategy has no edge.
  3. BOT-NATIVE. The learning runner writes here directly; manual entry covers
     trades taken elsewhere. One timeline, discretionary + automated.

Per-user, auth-scoped. Endpoints:
  POST /api/journal/trade            add a trade (manual or bot)
  PATCH /api/journal/trade/{id}      edit tags/notes/grade/outcome
  DELETE /api/journal/trade/{id}
  GET  /api/journal/trades           list (filters: from,to,symbol,setup,outcome)
  GET  /api/journal/analytics        the full honest analytics payload
  POST /api/journal/review           AI session review (honest about edge)
  POST /api/journal/ingest           bot bulk-push (bearer BOT_INGEST_KEY)
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel, Field
from supabase import Client

from auth import get_current_user
from db import get_supabase

router = APIRouter(prefix="/api/journal", tags=["journal"])


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _r(v, n=2):
    try:
        return round(float(v), n)
    except (TypeError, ValueError):
        return 0.0


# ────────────────────────────── models ──────────────────────────────
class TradeIn(BaseModel):
    symbol: str
    side: str = "buy"                      # buy | sell
    entry_price: float | None = None
    exit_price: float | None = None
    lots: float | None = None
    pnl: float | None = None               # realized, account currency
    r_multiple: float | None = None        # result in R (pnl / risk)
    opened_at: str | None = None
    closed_at: str | None = None
    setup: str = ""                        # e.g. "London sweep", "SMC OB"
    reason: str = ""                       # WHY — the trader's thesis
    account_no: str = ""
    server: str = ""
    account_id: str = ""
    session: str = ""                      # london|ny|asia|...
    tags: list[str] = Field(default_factory=list)
    grade: str = ""                        # A|B|C|D self-grade of execution
    mistakes: list[str] = Field(default_factory=list)
    emotion: str = ""                      # calm|fomo|revenge|hesitant|...
    source: str = "manual"                 # manual | bot | import
    screenshot_url: str = ""
    notes: str = ""


class TradePatch(BaseModel):
    setup: str | None = None
    reason: str | None = None
    tags: list[str] | None = None
    grade: str | None = None
    mistakes: list[str] | None = None
    emotion: str | None = None
    outcome: str | None = None
    exit_price: float | None = None
    pnl: float | None = None
    r_multiple: float | None = None
    notes: str | None = None


def _outcome(pnl) -> str:
    if pnl is None:
        return "open"
    return "win" if pnl > 0 else "loss" if pnl < 0 else "flat"


# ────────────────────────────── write ──────────────────────────────
@router.post("/trade")
async def add_trade(t: TradeIn, user=Depends(get_current_user),
                    sb: Client = Depends(get_supabase)) -> dict:
    row = {
        "user_id": str(user.id),
        "symbol": t.symbol.upper(), "side": t.side.lower(),
        "entry_price": t.entry_price, "exit_price": t.exit_price,
        "lots": t.lots, "pnl": t.pnl, "r_multiple": t.r_multiple,
        "opened_at": t.opened_at or _now(), "closed_at": t.closed_at,
        "setup": t.setup, "reason": t.reason, "session": t.session,
        "tags": t.tags, "grade": t.grade, "mistakes": t.mistakes,
        "emotion": t.emotion, "source": t.source,
        "screenshot_url": t.screenshot_url, "notes": t.notes,
        "outcome": _outcome(t.pnl), "created_at": _now(),
    }
    try:
        res = sb.table("journal_trades").insert(row).execute()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR,
                            f"could not save trade: {exc}") from exc
    return {"ok": True, "trade": (res.data or [row])[0]}


@router.patch("/trade/{trade_id}")
async def edit_trade(trade_id: str, patch: TradePatch,
                     user=Depends(get_current_user),
                     sb: Client = Depends(get_supabase)) -> dict:
    upd = {k: v for k, v in patch.model_dump().items() if v is not None}
    if "pnl" in upd:
        upd["outcome"] = _outcome(upd["pnl"])
    upd["updated_at"] = _now()
    try:
        res = (sb.table("journal_trades").update(upd)
               .eq("id", trade_id).eq("user_id", str(user.id)).execute())
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR,
                            f"update failed: {exc}") from exc
    if not res.data:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "trade not found")
    return {"ok": True, "trade": res.data[0]}


@router.delete("/trade/{trade_id}")
async def delete_trade(trade_id: str, user=Depends(get_current_user),
                       sb: Client = Depends(get_supabase)) -> dict:
    (sb.table("journal_trades").delete()
     .eq("id", trade_id).eq("user_id", str(user.id)).execute())
    return {"ok": True}


# ────────────────────────────── read ──────────────────────────────
@router.get("/trades")
async def list_trades(user=Depends(get_current_user),
                      sb: Client = Depends(get_supabase),
                      frm: str | None = Query(None, alias="from"),
                      to: str | None = None, symbol: str | None = None,
                      setup: str | None = None, outcome: str | None = None,
                      account_id: str | None = None,
                      limit: int = 200) -> dict:
    q = (sb.table("journal_trades").select("*").eq("user_id", str(user.id)))
    if account_id:
        q = q.eq("account_id", account_id)
    if symbol:
        q = q.eq("symbol", symbol.upper())
    if setup:
        q = q.eq("setup", setup)
    if outcome:
        q = q.eq("outcome", outcome)
    if frm:
        q = q.gte("opened_at", frm)
    if to:
        q = q.lte("opened_at", to)
    try:
        res = q.order("opened_at", desc=True).limit(min(limit, 1000)).execute()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR,
                            f"could not load trades: {exc}") from exc
    return {"trades": res.data or []}


def _load_all(sb, uid: str) -> list[dict]:
    try:
        return (sb.table("journal_trades").select("*")
                .eq("user_id", uid).order("opened_at").execute()).data or []
    except Exception:
        return []


# ────────────────────────────── analytics ──────────────────────────────
def compute_analytics(trades: list[dict]) -> dict:
    closed = [t for t in trades if t.get("outcome") in ("win", "loss", "flat")]
    n = len(closed)
    if not n:
        return {"trades": 0, "empty": True}

    wins = [t for t in closed if t["outcome"] == "win"]
    losses = [t for t in closed if t["outcome"] == "loss"]
    gross_win = sum(t.get("pnl") or 0 for t in wins)
    gross_loss = sum(t.get("pnl") or 0 for t in losses)
    total = gross_win + gross_loss
    win_rate = len(wins) / n
    avg_win = gross_win / len(wins) if wins else 0
    avg_loss = gross_loss / len(losses) if losses else 0
    pf = (gross_win / abs(gross_loss)) if gross_loss else (float("inf") if gross_win else 0)
    expectancy = total / n
    # R-based (only trades that have r_multiple)
    r_vals = [t["r_multiple"] for t in closed if t.get("r_multiple") is not None]
    avg_r = sum(r_vals) / len(r_vals) if r_vals else None

    # equity curve
    eq, curve = 0.0, []
    for t in sorted(closed, key=lambda x: x.get("closed_at") or x.get("opened_at") or ""):
        eq += t.get("pnl") or 0
        curve.append({"t": t.get("closed_at") or t.get("opened_at"), "eq": _r(eq)})
    peak, max_dd = 0.0, 0.0
    for p in curve:
        peak = max(peak, p["eq"])
        max_dd = min(max_dd, p["eq"] - peak)

    def bucket(key_fn, label):
        b: dict = {}
        for t in closed:
            k = key_fn(t) or "—"
            d = b.setdefault(k, {"n": 0, "wins": 0, "pnl": 0.0})
            d["n"] += 1
            d["wins"] += 1 if t["outcome"] == "win" else 0
            d["pnl"] += t.get("pnl") or 0
        return {k: {"n": v["n"], "win_rate": _r(v["wins"]/v["n"], 3),
                    "pnl": _r(v["pnl"])} for k, v in b.items()}

    by_setup = bucket(lambda t: t.get("setup"), "setup")
    by_session = bucket(lambda t: t.get("session"), "session")
    by_symbol = bucket(lambda t: t.get("symbol"), "symbol")
    by_source = bucket(lambda t: t.get("source"), "source")
    by_emotion = bucket(lambda t: t.get("emotion"), "emotion")

    # THE HONEST BIT: setups with enough sample whose win rate ≈ coin flip
    coin_flips = [{"setup": k, **v} for k, v in by_setup.items()
                  if v["n"] >= 8 and 0.42 <= v["win_rate"] <= 0.58]
    # and the real edges: high sample, clearly profitable
    edges = sorted(
        [{"setup": k, **v} for k, v in by_setup.items()
         if v["n"] >= 5 and v["pnl"] > 0 and v["win_rate"] > 0.5],
        key=lambda x: x["pnl"], reverse=True)

    return {
        "trades": n, "wins": len(wins), "losses": len(losses),
        "win_rate": _r(win_rate, 3), "total_pnl": _r(total),
        "profit_factor": _r(pf) if pf != float("inf") else None,
        "expectancy": _r(expectancy), "avg_win": _r(avg_win),
        "avg_loss": _r(avg_loss), "avg_r": _r(avg_r) if avg_r is not None else None,
        "max_drawdown": _r(max_dd), "equity_curve": curve,
        "by_setup": by_setup, "by_session": by_session, "by_symbol": by_symbol,
        "by_source": by_source, "by_emotion": by_emotion,
        "coin_flip_setups": coin_flips, "edge_setups": edges,
    }


@router.get("/analytics")
async def analytics(user=Depends(get_current_user),
                    sb: Client = Depends(get_supabase)) -> dict:
    return compute_analytics(_load_all(sb, str(user.id)))


# ────────────────────────────── AI review ──────────────────────────────
REVIEW_SYSTEM = """You are the SKLZ Journal review — an honest trading coach, \
not a cheerleader. You analyze a trader's own journal: trades with their stated \
REASONING, self-graded execution, emotions, and outcomes.

Your differentiator vs every other journal: you tell the truth about edge.
- If a setup has a decent sample and hovers at coin-flip (≈45-55% with no \
positive expectancy), SAY SO plainly — the trader is better off knowing.
- Separate what the trader BELIEVES works (their notes/setups) from what the \
DATA shows works (win rate + expectancy by setup).
- Praise what genuinely works. Flag emotional patterns (revenge/FOMO trades \
losing money). Note execution grades vs outcomes.
- Never promise profits. Never encourage more risk. Small samples (<8 in a \
bucket) → say the sample is too small to conclude.

Return STRICT JSON:
{
 "headline":"one honest sentence on the trader's edge",
 "what_works":"setups/sessions with real evidence, with numbers",
 "what_doesnt":"coin-flip or losing setups they may believe in, with numbers",
 "psychology":"emotional/behavioural patterns in the data",
 "execution":"what their self-grades vs outcomes reveal",
 "actions":["3-5 concrete, specific changes"],
 "one_thing":"the single highest-impact change this week"
}"""


@router.post("/review")
async def ai_review(user=Depends(get_current_user),
                    sb: Client = Depends(get_supabase)) -> dict:
    trades = _load_all(sb, str(user.id))
    stats = compute_analytics(trades)
    if stats.get("empty"):
        return {"review": {"headline": "No closed trades yet — log some trades "
                           "with your reasoning and I'll find your edge."}}
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        return {"review": _deterministic_review(stats)}

    import json
    # trim trades for the prompt: keep the fields that matter for coaching
    sample = [{"symbol": t.get("symbol"), "setup": t.get("setup"),
               "reason": t.get("reason"), "outcome": t.get("outcome"),
               "pnl": t.get("pnl"), "r": t.get("r_multiple"),
               "session": t.get("session"), "emotion": t.get("emotion"),
               "grade": t.get("grade"), "mistakes": t.get("mistakes")}
              for t in trades if t.get("outcome") in ("win", "loss", "flat")][-120:]
    prompt = (f"Analytics:\n{json.dumps(stats, indent=2, default=str)}\n\n"
              f"Trades with reasoning:\n{json.dumps(sample, indent=2, default=str)}\n\n"
              f"Return the honest review JSON.")
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=key)
        msg = client.messages.create(
            model="claude-sonnet-4-6", max_tokens=1600,
            system=REVIEW_SYSTEM,
            messages=[{"role": "user", "content": prompt}])
        text = "".join(b.text for b in msg.content if b.type == "text").strip()
        text = text.removeprefix("```json").removeprefix("```").removesuffix("```")
        return {"review": json.loads(text), "stats": stats}
    except Exception as exc:  # noqa: BLE001
        rev = _deterministic_review(stats)
        rev["headline"] = f"(AI unavailable: {type(exc).__name__}) " + rev["headline"]
        return {"review": rev, "stats": stats}


def _deterministic_review(stats: dict) -> dict:
    edges = stats.get("edge_setups", [])
    flips = stats.get("coin_flip_setups", [])
    return {
        "headline": f"{stats['trades']} trades · {stats['win_rate']:.0%} win · "
                    f"{stats['total_pnl']:+.2f} · PF "
                    f"{stats.get('profit_factor') or '∞'}",
        "what_works": ("; ".join(f"{e['setup']} ({e['win_rate']:.0%}, "
                       f"{e['pnl']:+.0f}, n={e['n']})" for e in edges[:3])
                       or "no setup has a clear positive edge yet"),
        "what_doesnt": ("; ".join(f"{f['setup']} is a coin-flip "
                        f"({f['win_rate']:.0%}, n={f['n']})" for f in flips[:3])
                        or "no clear coin-flip setups (or samples too small)"),
        "psychology": "add ANTHROPIC_API_KEY for behavioural analysis",
        "execution": "add ANTHROPIC_API_KEY for grade-vs-outcome analysis",
        "actions": ["Log every trade with your reason and a self-grade",
                    "Do more of your positive-expectancy setups",
                    "Cut or paper-trade the coin-flip setups"],
        "one_thing": (f"Focus on {edges[0]['setup']}" if edges
                      else "Build sample size — log consistently for 2 weeks"),
    }


# ────────────────────────────── bot ingest ──────────────────────────────
class BotTradeIn(BaseModel):
    user_id: str
    trades: list[TradeIn]
    account_no: str = ""
    server: str = ""


@router.post("/ingest")
async def bot_ingest(payload: BotTradeIn,
                     authorization: str = Header(default=""),
                     sb: Client = Depends(get_supabase)) -> dict:
    """Bulk push from the learning runner. Gated by BOT_INGEST_KEY."""
    expected = os.environ.get("BOT_INGEST_KEY", "")
    token = authorization.replace("Bearer ", "").strip()
    if not expected or token != expected:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid bot key")
    # auto-register the account this bot pushes to (connected=true)
    acct_id = ""
    if payload.account_no:
        try:
            ex = (sb.table("journal_accounts").select("id")
                  .eq("user_id", payload.user_id)
                  .eq("account_no", payload.account_no)
                  .eq("server", payload.server).execute()).data
            if ex:
                acct_id = ex[0]["id"]
            else:
                ins = sb.table("journal_accounts").insert({
                    "user_id": payload.user_id,
                    "label": f"{payload.server or 'Account'} {payload.account_no}",
                    "platform": "MT5", "server": payload.server,
                    "account_no": payload.account_no, "connected": True,
                    "kind": "demo"}).execute()
                acct_id = (ins.data or [{}])[0].get("id", "")
        except Exception:
            pass
    rows = []
    for t in payload.trades:
        rows.append({
            "user_id": payload.user_id, "symbol": t.symbol.upper(),
            "side": t.side.lower(), "entry_price": t.entry_price,
            "exit_price": t.exit_price, "lots": t.lots, "pnl": t.pnl,
            "r_multiple": t.r_multiple, "opened_at": t.opened_at or _now(),
            "closed_at": t.closed_at, "setup": t.setup, "reason": t.reason,
            "session": t.session, "tags": t.tags, "source": t.source if hasattr(t, "source") else "bot",
            "account_no": payload.account_no, "server": payload.server,
            "account_id": acct_id or None,
            "outcome": _outcome(t.pnl), "created_at": _now()})
    if rows:
        try:
            sb.table("journal_trades").insert(rows).execute()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR,
                                f"ingest failed: {exc}") from exc
    return {"ok": True, "ingested": len(rows)}


# ────────────────────────── accounts (multi-account) ──────────────────────────
class AccountIn(BaseModel):
    label: str
    platform: str = "MT5"          # MT5 | MT4 | cTrader | MatchTrader | custom
    broker: str = ""
    server: str = ""
    account_no: str = ""
    kind: str = "demo"             # demo | live | prop


@router.get("/accounts")
async def list_accounts(user=Depends(get_current_user),
                        sb: Client = Depends(get_supabase)) -> dict:
    try:
        rows = (sb.table("journal_accounts").select("*")
                .eq("user_id", str(user.id))
                .order("created_at").execute()).data or []
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR,
                            f"could not load accounts: {exc}") from exc
    # attach per-account trade count + net pnl
    for a in rows:
        try:
            t = (sb.table("journal_trades").select("pnl")
                 .eq("user_id", str(user.id)).eq("account_id", a["id"]).execute()).data or []
            a["trade_count"] = len(t)
            a["net_pnl"] = round(sum((x.get("pnl") or 0) for x in t), 2)
        except Exception:
            a["trade_count"] = 0
            a["net_pnl"] = 0
    return {"accounts": rows}


@router.post("/accounts")
async def create_account(body: AccountIn, user=Depends(get_current_user),
                         sb: Client = Depends(get_supabase)) -> dict:
    # if this account_no+server already exists (e.g. auto-registered by the bot),
    # return it instead of erroring on the unique constraint
    if body.account_no:
        try:
            ex = (sb.table("journal_accounts").select("*")
                  .eq("user_id", str(user.id))
                  .eq("account_no", body.account_no)
                  .eq("server", body.server).execute()).data
            if ex:
                return {"ok": True, "account": ex[0], "existing": True}
        except Exception:
            pass
    row = {"user_id": str(user.id), **body.model_dump(), "connected": False}
    try:
        res = sb.table("journal_accounts").insert(row).execute()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR,
                            f"could not create account: {exc}") from exc
    return {"ok": True, "account": (res.data or [row])[0]}


@router.delete("/accounts/{account_id}")
async def delete_account(account_id: str, user=Depends(get_current_user),
                         sb: Client = Depends(get_supabase)) -> dict:
    sb.table("journal_accounts").delete().eq("id", account_id) \
        .eq("user_id", str(user.id)).execute()
    return {"ok": True}


@router.get("/accounts/{account_id}/analytics")
async def account_analytics(account_id: str, user=Depends(get_current_user),
                            sb: Client = Depends(get_supabase)) -> dict:
    try:
        trades = (sb.table("journal_trades").select("*")
                  .eq("user_id", str(user.id)).eq("account_id", account_id)
                  .order("opened_at").execute()).data or []
    except Exception:
        trades = []
    return {"analytics": compute_analytics(trades), "count": len(trades)}


# ────────────────────── public performance (shareable) ──────────────────────
import hashlib as _hl


def _share_code(account_id: str) -> str:
    return _hl.sha256(account_id.encode()).hexdigest()[:10]


@router.post("/accounts/{account_id}/share")
async def toggle_share(account_id: str, public: bool,
                       user=Depends(get_current_user),
                       sb: Client = Depends(get_supabase)) -> dict:
    """Make an account's performance public (or private). Returns the share link."""
    try:
        own = (sb.table("journal_accounts").select("id")
               .eq("id", account_id).eq("user_id", str(user.id)).execute()).data
        if not own:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "account not found")
        code = _share_code(account_id) if public else ""
        sb.table("journal_accounts").update(
            {"public": public, "share_code": code}).eq("id", account_id).execute()
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, str(exc)[:200]) from exc
    site = os.environ.get("SITE_URL", "https://www.sklzlabs.com")
    return {"ok": True, "public": public,
            "share_link": f"{site}/perf.html?c={code}" if public else ""}


@router.get("/public/{share_code}")
async def public_performance(share_code: str,
                             sb: Client = Depends(get_supabase)) -> dict:
    """PUBLIC — no auth. Read-only performance for a shared account."""
    try:
        acct = (sb.table("journal_accounts").select("*")
                .eq("share_code", share_code).eq("public", True).execute()).data
    except Exception:
        acct = None
    if not acct:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found or not public")
    a = acct[0]
    try:
        trades = (sb.table("journal_trades").select("*")
                  .eq("account_id", a["id"]).order("opened_at").execute()).data or []
    except Exception:
        trades = []
    # honest data-source label: MT5-tracked (from bot) vs manually logged
    bot_trades = sum(1 for t in trades if t.get("source") in ("bot", "dashboard"))
    manual_trades = len(trades) - bot_trades
    source = ("MT5-tracked" if a.get("connected") and bot_trades >= manual_trades
              else "manually logged" if manual_trades > bot_trades
              else "mixed")
    return {
        "account": {"label": a["label"], "platform": a["platform"],
                    "broker": a["broker"], "server": a["server"],
                    "account_no": a["account_no"], "kind": a["kind"],
                    "connected": a.get("connected", False)},
        "data_source": source,
        "analytics": compute_analytics(trades),
        "trade_count": len(trades),
        "generated": _now(),
    }
