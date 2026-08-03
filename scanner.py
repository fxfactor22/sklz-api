"""SKLZ Crypto Scanner — live conditions grid across many coins.

Pulls live market data from CoinGecko (free, no key), computes a momentum
composite across 1h/24h/7d, and ranks coins so a trader sees at a glance
what's pumping, what's breaking down, and where volume is flowing.

Honest framing: this shows CURRENT CONDITIONS and momentum — it is not a
prediction. A high score means "moving strongly now", not "will go up".

Endpoints:
  GET /api/scanner/crypto      ranked grid (cached ~60s server-side)
"""
from __future__ import annotations

import json
import time
import urllib.request

from fastapi import APIRouter, Depends, HTTPException, Query, status
from supabase import Client

from auth import get_current_user
from db import get_supabase
from entitlements import require_paid

router = APIRouter(prefix="/api/scanner", tags=["scanner"])

_CACHE: dict = {"ts": 0.0, "data": None}
_TTL = 90                      # seconds — respect CoinGecko rate limits


def _fetch_markets(n: int = 100) -> list[dict]:
    url = ("https://api.coingecko.com/api/v3/coins/markets"
           "?vs_currency=usd&order=market_cap_desc"
           f"&per_page={n}&page=1&price_change_percentage=1h,24h,7d")
    headers = {"User-Agent": "Mozilla/5.0 (SKLZ Scanner)",
               "Accept": "application/json"}
    # optional API key support if you add one later (COINGECKO_API_KEY)
    import os as _os
    k = _os.environ.get("COINGECKO_API_KEY", "")
    if k:
        headers["x-cg-demo-api-key"] = k
    last = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=12) as r:
                return json.loads(r.read().decode())
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(1.5 * (attempt + 1))
    raise last if last else RuntimeError("fetch failed")


def _score(c: dict) -> dict:
    """Composite momentum score from multi-timeframe % change + volume pressure.
    Transparent and mechanical — weights favour alignment across timeframes."""
    h1 = c.get("price_change_percentage_1h_in_currency") or 0
    h24 = c.get("price_change_percentage_24h") or 0
    d7 = c.get("price_change_percentage_7d_in_currency") or 0
    # normalized contributions (cap extremes so one crazy move doesn't dominate)
    def cap(x, lim):
        return max(-lim, min(lim, x)) / lim
    score = (cap(h1, 5) * 1.0 + cap(h24, 15) * 1.5 + cap(d7, 40) * 1.0) / 3.5
    # alignment bonus: all three same direction
    aligned = (h1 > 0 and h24 > 0 and d7 > 0) or (h1 < 0 and h24 < 0 and d7 < 0)
    if aligned:
        score *= 1.25
    score = max(-1.0, min(1.0, score))
    if score >= 0.5:
        read = "strong up"
    elif score >= 0.15:
        read = "up"
    elif score <= -0.5:
        read = "strong down"
    elif score <= -0.15:
        read = "down"
    else:
        read = "neutral"
    return {"score": round(score, 3), "read": read, "aligned": aligned}


@router.get("/crypto")
async def crypto_scan(user=Depends(get_current_user),
                      sb: Client = Depends(get_supabase),
                      sort: str = Query("score"),
                      limit: int = Query(50)) -> dict:
    require_paid(sb, user, "The Crypto Scanner")
    now = time.time()
    if not _CACHE["data"] or now - _CACHE["ts"] > _TTL:
        try:
            raw = _fetch_markets(100)
            rows = []
            for c in raw:
                sc = _score(c)
                rows.append({
                    "symbol": (c.get("symbol") or "").upper(),
                    "name": c.get("name"),
                    "price": c.get("current_price"),
                    "h1": round(c.get("price_change_percentage_1h_in_currency") or 0, 2),
                    "h24": round(c.get("price_change_percentage_24h") or 0, 2),
                    "d7": round(c.get("price_change_percentage_7d_in_currency") or 0, 2),
                    "volume": c.get("total_volume"),
                    "mcap": c.get("market_cap"),
                    "image": c.get("image"),
                    **sc,
                })
            _CACHE.update(ts=now, data=rows)
        except Exception as exc:  # noqa: BLE001
            if _CACHE["data"]:
                pass  # serve stale on failure
            else:
                return {"error": f"data source unavailable: {exc}",
                        "coins": [], "updated": None}
    rows = list(_CACHE["data"])
    # exclude stablecoins from momentum ranking (they're always ~0)
    STABLE = {"USDT", "USDC", "DAI", "BUSD", "TUSD", "USDD", "FDUSD"}
    rows = [r for r in rows if r["symbol"] not in STABLE]
    keymap = {"score": "score", "h1": "h1", "h24": "h24", "d7": "d7",
              "volume": "volume", "mcap": "mcap"}
    k = keymap.get(sort, "score")
    rows.sort(key=lambda r: (r.get(k) or 0), reverse=True)
    return {"coins": rows[:min(limit, 100)],
            "updated": _CACHE["ts"],
            "movers_up": sorted(rows, key=lambda r: r["score"], reverse=True)[:3],
            "movers_down": sorted(rows, key=lambda r: r["score"])[:3]}


# ────────────────────── AI HONEST READ ──────────────────────
# Directional bias with honest confidence — never a naked "BUY".
# Describes conditions, flags extended/late setups, gives levels.
READ_SYSTEM = """You are the SKLZ Scanner's honest read. You analyze live crypto \
momentum data and give a trader a DIRECTIONAL BIAS with HONEST confidence — never \
a naked 'buy/sell' hype call.

Non-negotiable stance (this is the SKLZ brand):
- SKLZ research on 27M ticks showed momentum/'famous' setups are often coin-flips \
out-of-sample. So when a coin is already extended (big multi-day move), SAY the \
entry is late and historically a coin-flip. Do NOT cheerlead.
- Give a bias: "long", "short", or "stand aside". Bias is a lean, not a promise.
- Always pair bias with honest confidence (low/medium/high) and WHY.
- Flag chases: if 7d move is large and price is extended, confidence is low and you \
say so plainly.
- Give mechanical levels IF they act: entry zone, invalidation, target. Never imply \
guaranteed profit. Never predict a specific price will be reached.
- Prefer "stand aside" when the picture is mixed/extended. Protecting the trader \
from a bad entry is the product.

Return STRICT JSON only:
{
 "bias": "long|short|stand aside",
 "confidence": "low|medium|high",
 "headline": "one honest sentence",
 "why": "the read: alignment, whether it's early or extended/late, with the numbers",
 "if_you_act": {"entry":"zone or price", "invalidation":"level", "target":"level"},
 "honest_flag": "the main risk — e.g. 'this is a late chase' — or '' if genuinely clean"
}"""

TOP_SYSTEM = """You are the SKLZ Scanner daily read. From live crypto momentum data, \
pick the 3 most genuinely interesting LONG-biased setups and the 3 biggest TRAPS \
(coins that look tempting but are extended/late/coin-flip). Honest, not hype. \
SKLZ's edge is telling traders the truth: many momentum setups are coin-flips \
out-of-sample, so 'traps' are as valuable as 'setups'.

Return STRICT JSON only:
{
 "setups": [{"symbol":"", "bias":"long|short", "why":"short honest reason with numbers"}],
 "traps": [{"symbol":"", "why":"why it's tempting but a likely bad entry"}],
 "note": "one honest line about overall market condition today"
}"""


def _coin_context(sym: str) -> dict | None:
    data = _CACHE.get("data") or []
    for c in data:
        if c["symbol"] == sym.upper():
            return c
    return None


@router.get("/read/{symbol}")
async def coin_read(symbol: str, user=Depends(get_current_user),
                    sb: Client = Depends(get_supabase)) -> dict:
    """On-demand honest read for one coin."""
    require_paid(sb, user, "The Scanner AI read")
    import os
    c = _coin_context(symbol)
    if not c:
        return {"error": f"{symbol} not in current scan — open the scanner first"}
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        return {"read": _fallback_read(c)}
    import json as _json
    prompt = (f"Live data for {c['symbol']} ({c.get('name')}):\n"
              f"price ${c.get('price')}, 1h {c.get('h1')}%, 24h {c.get('h24')}%, "
              f"7d {c.get('d7')}%, momentum score {c.get('score')} ({c.get('read')}), "
              f"aligned={c.get('aligned')}.\nGive the honest read JSON.")
    try:
        import anthropic
        cl = anthropic.Anthropic(api_key=key)
        m = cl.messages.create(model="claude-sonnet-4-5", max_tokens=700,
                               system=READ_SYSTEM,
                               messages=[{"role": "user", "content": prompt}])
        t = "".join(b.text for b in m.content if b.type == "text").strip()
        t = t.removeprefix("```json").removeprefix("```").removesuffix("```")
        return {"read": _json.loads(t), "coin": c}
    except Exception as exc:  # noqa: BLE001
        import sys as _s
        print(f"[scanner-ai] {type(exc).__name__}: {exc}", file=_s.stderr, flush=True)
        fb = _fallback_read(c)
        fb["headline"] = "(AI unavailable) " + fb["headline"]
        return {"read": fb, "coin": c}


@router.get("/top")
async def top_read(user=Depends(get_current_user),
                   sb: Client = Depends(get_supabase)) -> dict:
    """Daily top-of-scanner: 3 real setups vs 3 traps."""
    require_paid(sb, user, "The Scanner AI read")
    import os
    data = _CACHE.get("data") or []
    STABLE = {"USDT", "USDC", "DAI", "BUSD", "TUSD", "FDUSD"}
    rows = [r for r in data if r["symbol"] not in STABLE][:40]
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key or not rows:
        return {"top": _fallback_top(rows)}
    import json as _json
    slim = [{"s": r["symbol"], "h1": r["h1"], "h24": r["h24"], "d7": r["d7"],
             "score": r["score"], "read": r["read"]} for r in rows]
    try:
        import anthropic
        cl = anthropic.Anthropic(api_key=key)
        m = cl.messages.create(model="claude-sonnet-4-5", max_tokens=900,
                               system=TOP_SYSTEM,
                               messages=[{"role": "user",
                                          "content": f"Live scan:\n{_json.dumps(slim)}\n\nReturn JSON."}])
        t = "".join(b.text for b in m.content if b.type == "text").strip()
        t = t.removeprefix("```json").removeprefix("```").removesuffix("```")
        return {"top": _json.loads(t)}
    except Exception as exc:  # noqa: BLE001
        import sys as _s
        print(f"[scanner-ai-top] {type(exc).__name__}: {exc}", file=_s.stderr, flush=True)
        return {"top": _fallback_top(rows)}


def _fallback_read(c: dict) -> dict:
    extended = abs(c.get("d7") or 0) > 20
    aligned = c.get("aligned")
    score = c.get("score") or 0
    if aligned and not extended and score > 0.2:
        bias, conf = "long", "medium"
    elif aligned and not extended and score < -0.2:
        bias, conf = "short", "medium"
    else:
        bias, conf = "stand aside", "low"
    flag = ""
    if extended and score > 0:
        bias, conf = "stand aside", "low"
        flag = f"late chase — already +{c.get('d7')}% on 7d"
    return {"bias": bias, "confidence": conf,
            "headline": f"{c['symbol']} is {c.get('read')} "
                        f"(1h {c.get('h1')}%, 24h {c.get('h24')}%, 7d {c.get('d7')}%)",
            "why": ("aligned across timeframes and not yet extended"
                    if (aligned and not extended)
                    else "mixed or extended — not a clean entry"),
            "if_you_act": {"entry": f"~${c.get('price')}",
                           "invalidation": "below recent structure",
                           "target": "prior swing / next level"},
            "honest_flag": flag or ("add ANTHROPIC_API_KEY for full AI read")}


def _fallback_top(rows: list) -> dict:
    ups = sorted([r for r in rows if not (abs(r.get("d7") or 0) > 25)],
                 key=lambda r: r["score"], reverse=True)[:3]
    traps = sorted([r for r in rows if (abs(r.get("d7") or 0) > 25 and r["score"] > 0)],
                   key=lambda r: r.get("d7") or 0, reverse=True)[:3]
    return {"setups": [{"symbol": r["symbol"], "bias": "long" if r["score"] > 0 else "short",
                        "why": f"{r['read']}, 7d {r['d7']}%"} for r in ups],
            "traps": [{"symbol": r["symbol"],
                       "why": f"extended +{r['d7']}% on 7d — late entry"} for r in traps],
            "note": "Deterministic read (add ANTHROPIC_API_KEY for full analysis). "
                    "Extended movers are flagged as traps, not setups."}


# ── live order flow (crypto only) ───────────────────────────────────
@router.get("/orderflow/{symbol}")
async def orderflow(symbol: str, exchange: str = "bybit",
                    side: int = 1,
                    user=Depends(get_current_user),
                    sb: Client = Depends(get_supabase)) -> dict:
    """Real order book depth and measured delta for a crypto pair.

    Unlike the forex path — where there is no central exchange and delta has
    to be inferred from tick side — this reads genuine resting liquidity and
    trades whose aggressor side the exchange reports. It is measured data.

    No API key needed: order books and public trades are public.
    """
    require_paid(sb, user, "Live order flow")

    try:
        import ccxt
        from copytrader import cryptobook as CB
    except ImportError as exc:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR,
                            f"order flow unavailable: {exc}") from exc

    sym = symbol.upper()
    if "/" not in sym:
        sym = f"{sym}/USDT"

    class _Pub:
        pass

    try:
        pub = _Pub()
        pub.client = getattr(ccxt, exchange)({"enableRateLimit": True,
                                              "timeout": 15000})
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"unknown exchange '{exchange}'") from exc

    try:
        result = CB.assess_crypto(pub, sym, 1 if side >= 0 else -1)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_502_BAD_GATEWAY,
                            f"could not read order flow: {str(exc)[:180]}") from exc

    result["symbol"] = sym
    result["exchange"] = exchange
    return result
