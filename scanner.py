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

from fastapi import APIRouter, Depends, Query
from supabase import Client

from auth import get_current_user

router = APIRouter(prefix="/api/scanner", tags=["scanner"])

_CACHE: dict = {"ts": 0.0, "data": None}
_TTL = 60                      # seconds — respect CoinGecko rate limits


def _fetch_markets(n: int = 100) -> list[dict]:
    url = ("https://api.coingecko.com/api/v3/coins/markets"
           "?vs_currency=usd&order=market_cap_desc"
           f"&per_page={n}&page=1&price_change_percentage=1h,24h,7d")
    req = urllib.request.Request(url, headers={"User-Agent": "SKLZ-Scanner/1.0"})
    with urllib.request.urlopen(req, timeout=12) as r:
        return json.loads(r.read().decode())


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
                      sort: str = Query("score"),
                      limit: int = Query(50)) -> dict:
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
