"""SKLZ — new token risk scanner.

This is deliberately NOT a "gem finder". The base rate for new token launches
is dominated by rug pulls, honeypots and insider dumps, so a scanner that
surfaces "new + pumping" is a rug delivery mechanism: it finds tokens exactly
when promoters are pushing them, which is exactly when insiders exit.

So this scores RISK, not opportunity. Every token starts guilty and has to
earn a lower risk score. The checks are the ones that actually precede losses:

  liquidity depth      can you get out, and how much would it cost to rug
  pair age             brand new pairs are where the rugs are
  sell blocking        many buys, almost no sells = honeypot behaviour
  FDV vs liquidity     how much unbacked supply is waiting overhead
  volume vs liquidity  implausible turnover suggests wash trading
  paid promotion       someone is paying for eyeballs on a new token
  price shape          vertical moves mean you are late, not early

Nothing here is a recommendation. The honest output for most tokens is
"do not touch this", and saying so plainly is the product.
"""
from __future__ import annotations

import time
import urllib.request
import json
from datetime import datetime, timezone

DEX_API = "https://api.dexscreener.com"
_CACHE: dict = {"ts": 0.0, "data": None}
_TTL = 90


def _get(url: str, timeout: int = 15):
    req = urllib.request.Request(url, headers={"User-Agent": "SKLZ-Scanner/1.0",
                                               "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _age_hours(created_ms) -> float | None:
    if not created_ms:
        return None
    try:
        return (time.time() * 1000 - float(created_ms)) / 3_600_000
    except (TypeError, ValueError):
        return None


def assess(pair: dict, boosted: bool = False) -> dict:
    """Score one pair's risk from 0 (least bad) to 100 (avoid).

    Returns the score, the individual flags, and a plain-language verdict.
    """
    flags: list[str] = []
    risk = 0.0

    liq = float(((pair.get("liquidity") or {}).get("usd")) or 0)
    fdv = float(pair.get("fdv") or 0)
    vol24 = float(((pair.get("volume") or {}).get("h24")) or 0)
    txns = pair.get("txns") or {}
    h24 = txns.get("h24") or {}
    h1 = txns.get("h1") or {}
    buys24, sells24 = int(h24.get("buys") or 0), int(h24.get("sells") or 0)
    buys1, sells1 = int(h1.get("buys") or 0), int(h1.get("sells") or 0)
    chg = pair.get("priceChange") or {}
    age_h = _age_hours(pair.get("pairCreatedAt"))

    # ── liquidity: can you actually get out? ──
    if liq < 5_000:
        risk += 30
        flags.append(f"liquidity only ${liq:,.0f} — you may not be able to sell")
    elif liq < 25_000:
        risk += 18
        flags.append(f"thin liquidity (${liq:,.0f}) — large exits will move price hard")
    elif liq < 100_000:
        risk += 8

    # ── age: newest pairs are where the rugs live ──
    if age_h is not None:
        if age_h < 6:
            risk += 22
            flags.append(f"pair is {age_h:.1f}h old — no history to judge")
        elif age_h < 48:
            risk += 14
            flags.append(f"pair is {age_h/24:.1f} days old")
        elif age_h < 24 * 14:
            risk += 6

    # ── honeypot behaviour: people buy, nobody sells ──
    if buys24 >= 30 and sells24 <= max(2, buys24 * 0.05):
        risk += 35
        flags.append(f"HONEYPOT SIGNAL: {buys24} buys but only {sells24} sells — "
                     f"selling may be blocked")
    elif buys24 >= 20 and sells24 <= buys24 * 0.15:
        risk += 18
        flags.append(f"very few sells ({sells24}) against {buys24} buys — "
                     f"check you can actually exit")

    # ── overhead supply: FDV far above what is actually pooled ──
    if fdv and liq:
        ratio = fdv / liq
        if ratio > 100:
            risk += 20
            flags.append(f"fully diluted value is {ratio:.0f}x the liquidity — "
                         f"most supply is not backed by anything")
        elif ratio > 30:
            risk += 10
            flags.append(f"FDV is {ratio:.0f}x liquidity")

    # ── implausible turnover ──
    if liq and vol24 / max(liq, 1) > 20:
        risk += 12
        flags.append(f"24h volume is {vol24/liq:.0f}x liquidity — "
                     f"possible wash trading")

    # ── paid promotion on a brand new token ──
    if boosted:
        risk += 10
        flags.append("promoter is paying for placement — someone wants eyes on this")

    # ── price shape: vertical means late ──
    c24 = float(chg.get("h24") or 0)
    c1 = float(chg.get("h1") or 0)
    if c24 > 300:
        risk += 15
        flags.append(f"up {c24:.0f}% in 24h — entering after a move like this is "
                     f"buying someone else's exit")
    elif c24 > 100:
        risk += 8
        flags.append(f"up {c24:.0f}% in 24h")
    if c24 < -60:
        risk += 12
        flags.append(f"down {abs(c24):.0f}% in 24h — may already be rugging")

    # ── sellers heading for the door ──
    if buys1 + sells1 >= 20 and sells1 > buys1 * 2:
        risk += 10
        flags.append(f"sells outpacing buys 2:1 in the last hour")

    risk = round(min(risk, 100.0), 1)

    # a blocked-sell pattern is disqualifying on its own — no combination of
    # other numbers makes "you can buy but not sell" acceptable
    honeypot = any("HONEYPOT SIGNAL" in f for f in flags)
    if honeypot:
        risk = max(risk, 85.0)

    if risk >= 70:
        band, verdict = "avoid", "Multiple rug indicators. Treat this as a trap."
    elif risk >= 45:
        band, verdict = "high", ("High risk. If you touch this at all, assume the "
                                 "position can go to zero.")
    elif risk >= 25:
        band, verdict = "elevated", ("Elevated risk, as most new tokens are. "
                                     "Nothing here says it is safe.")
    else:
        band, verdict = "lower", ("Fewer red flags than most — which is not the "
                                  "same as good. Depth and history are still thin.")

    base = pair.get("baseToken") or {}
    return {
        "symbol": base.get("symbol") or "?",
        "name": base.get("name") or "",
        "address": base.get("address") or "",
        "chain": pair.get("chainId"),
        "dex": pair.get("dexId"),
        "url": pair.get("url"),
        "price_usd": pair.get("priceUsd"),
        "liquidity_usd": round(liq, 2),
        "fdv": fdv,
        "volume_24h": round(vol24, 2),
        "age_hours": round(age_h, 1) if age_h is not None else None,
        "buys_24h": buys24, "sells_24h": sells24,
        "change_1h": c1, "change_24h": c24,
        "boosted": boosted,
        "risk_score": risk,
        "risk_band": band,
        "verdict": verdict,
        "flags": flags,
    }


def scan(limit: int = 30, chain: str | None = None) -> dict:
    """Pull the newest promoted/profiled tokens and risk-assess each."""
    now = time.time()
    if _CACHE["data"] and now - _CACHE["ts"] < _TTL:
        return _CACHE["data"]

    boosted_addrs: set[str] = set()
    candidates: list[tuple[str, str]] = []      # (chainId, tokenAddress)

    for ep, mark in (("token-boosts/latest/v1", True),
                     ("token-profiles/latest/v1", False)):
        try:
            rows = _get(f"{DEX_API}/{ep}") or []
            for r in rows if isinstance(rows, list) else []:
                addr = r.get("tokenAddress")
                cid = r.get("chainId")
                if not addr or not cid:
                    continue
                if chain and cid != chain:
                    continue
                if mark:
                    boosted_addrs.add(addr)
                if (cid, addr) not in candidates:
                    candidates.append((cid, addr))
        except Exception:  # noqa: BLE001
            continue

    assessed = []
    seen = set()
    for cid, addr in candidates[:limit]:
        if addr in seen:
            continue
        seen.add(addr)
        try:
            data = _get(f"{DEX_API}/latest/dex/tokens/{addr}")
            pairs = data.get("pairs") or []
        except Exception:  # noqa: BLE001
            continue
        if not pairs:
            continue
        # judge the deepest pool for this token
        best = max(pairs, key=lambda p: float(((p.get("liquidity") or {}).get("usd")) or 0))
        assessed.append(assess(best, boosted=addr in boosted_addrs))

    assessed.sort(key=lambda a: a["risk_score"])     # least bad first

    avoid = sum(1 for a in assessed if a["risk_band"] == "avoid")
    high = sum(1 for a in assessed if a["risk_band"] == "high")

    payload = {
        "tokens": assessed,
        "count": len(assessed),
        "avoid_count": avoid,
        "high_risk_count": high,
        "honest_note": (
            f"{avoid + high} of {len(assessed)} tokens here carry high or "
            f"disqualifying risk flags. That ratio is normal — most new tokens "
            f"lose their holders money, and many are designed to. This scanner "
            f"ranks by RISK, lowest first. A low score means fewer red flags, "
            f"not a good investment."),
        "method_note": (
            "Checks run: liquidity depth, pair age, buy/sell imbalance "
            "(honeypot behaviour), fully-diluted value against pooled liquidity, "
            "volume plausibility, paid promotion, and price shape. These catch "
            "common rug patterns. They cannot detect a contract that has not "
            "rugged yet, and no scanner can."),
        "scanned_at": datetime.now(timezone.utc).isoformat(),
    }
    _CACHE.update(ts=now, data=payload)
    return payload
