"""Trader performance metrics for the SKLZ marketplace.

Everything here is computed from real journaled trades. The defining feature
is HONESTY about statistical confidence: a 90% win rate over 11 trades is
noise, and this module says so rather than printing a flattering number.

SKLZ's 27M-tick research is the reason: most strategies look brilliant on a
small sample and revert to coin-flip out-of-sample. A marketplace that ranks
traders on 20 trades is a marketplace that sells luck.
"""
from __future__ import annotations

import math
from datetime import datetime

# below this many closed trades, ranking is not statistically meaningful
MIN_TRADES_MEANINGFUL = 30
MIN_TRADES_LISTED = 10


def _num(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _parse(ts: str | None):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None


def _stdev(vals: list[float]) -> float:
    n = len(vals)
    if n < 2:
        return 0.0
    mean = sum(vals) / n
    var = sum((v - mean) ** 2 for v in vals) / (n - 1)
    return math.sqrt(var)


def _max_streak(outcomes: list[str], target: str) -> int:
    best = cur = 0
    for o in outcomes:
        cur = cur + 1 if o == target else 0
        best = max(best, cur)
    return best


def _confidence(n: int) -> dict:
    """How much should anyone trust these numbers? Stated plainly."""
    if n < MIN_TRADES_LISTED:
        return {"level": "none", "label": "Not enough trades to judge",
                "note": f"Only {n} closed trades. Nothing here is meaningful yet."}
    if n < MIN_TRADES_MEANINGFUL:
        return {"level": "low", "label": "Small sample — treat as unproven",
                "note": (f"{n} closed trades. At this size, luck and skill look "
                         f"identical. Needs {MIN_TRADES_MEANINGFUL}+ to mean much.")}
    if n < 100:
        return {"level": "medium", "label": "Developing track record",
                "note": (f"{n} closed trades. Enough to see tendencies, not "
                         f"enough to be confident they persist.")}
    return {"level": "high", "label": "Substantial track record",
            "note": (f"{n} closed trades. A real sample — though past results "
                     f"still do not guarantee future ones.")}


def compute(trades: list[dict]) -> dict:
    """Full marketplace metrics for one trader's closed trades."""
    closed = [t for t in trades if t.get("outcome") in ("win", "loss", "flat")]
    closed.sort(key=lambda t: t.get("closed_at") or t.get("opened_at") or "")
    n = len(closed)
    if not n:
        return {"trades": 0, "empty": True, "confidence": _confidence(0)}

    pnls = [_num(t.get("pnl")) for t in closed]
    outcomes = [t.get("outcome") for t in closed]
    wins = [p for p, o in zip(pnls, outcomes) if o == "win"]
    losses = [p for p, o in zip(pnls, outcomes) if o == "loss"]

    gross_win = sum(wins)
    gross_loss = sum(losses)                 # negative
    net = gross_win + gross_loss
    win_rate = len(wins) / n

    profit_factor = (gross_win / abs(gross_loss)) if gross_loss else None
    expectancy = net / n
    avg_win = (gross_win / len(wins)) if wins else 0.0
    avg_loss = (gross_loss / len(losses)) if losses else 0.0

    # equity curve + drawdown
    eq, curve, peak, max_dd = 0.0, [], 0.0, 0.0
    for p in pnls:
        eq += p
        curve.append(round(eq, 2))
        peak = max(peak, eq)
        max_dd = min(max_dd, eq - peak)
    max_dd = abs(max_dd)

    # risk-adjusted: per-trade return series
    sd = _stdev(pnls)
    sharpe = (expectancy / sd) * math.sqrt(n) if sd else None
    downside = [p for p in pnls if p < 0]
    dsd = _stdev(downside) if len(downside) > 1 else 0.0
    sortino = (expectancy / dsd) * math.sqrt(n) if dsd else None

    recovery = (net / max_dd) if max_dd else None

    # consistency — share of profitable calendar months
    months: dict[str, float] = {}
    for t, p in zip(closed, pnls):
        d = _parse(t.get("closed_at") or t.get("opened_at"))
        if d:
            months.setdefault(f"{d.year}-{d.month:02d}", 0.0)
            months[f"{d.year}-{d.month:02d}"] += p
    prof_months = sum(1 for v in months.values() if v > 0)
    consistency = (prof_months / len(months)) if months else None

    # holding time
    holds = []
    for t in closed:
        a, b = _parse(t.get("opened_at")), _parse(t.get("closed_at"))
        if a and b and b > a:
            holds.append((b - a).total_seconds() / 3600.0)
    avg_hold_h = (sum(holds) / len(holds)) if holds else None

    # activity window
    first = _parse(closed[0].get("closed_at") or closed[0].get("opened_at"))
    last = _parse(closed[-1].get("closed_at") or closed[-1].get("opened_at"))
    days_active = (last - first).days + 1 if (first and last) else None

    # instrument spread
    syms: dict[str, int] = {}
    for t in closed:
        syms[t.get("symbol") or "—"] = syms.get(t.get("symbol") or "—", 0) + 1
    top_syms = sorted(syms.items(), key=lambda kv: kv[1], reverse=True)[:5]

    return {
        "trades": n,
        "win_rate": round(win_rate, 4),
        "net_pnl": round(net, 2),
        "gross_win": round(gross_win, 2),
        "gross_loss": round(gross_loss, 2),
        "profit_factor": round(profit_factor, 2) if profit_factor else None,
        "expectancy": round(expectancy, 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "max_drawdown": round(max_dd, 2),
        "recovery_factor": round(recovery, 2) if recovery else None,
        "sharpe": round(sharpe, 2) if sharpe is not None else None,
        "sortino": round(sortino, 2) if sortino is not None else None,
        "consistency": round(consistency, 3) if consistency is not None else None,
        "profitable_months": prof_months,
        "months_traded": len(months),
        "max_consecutive_wins": _max_streak(outcomes, "win"),
        "max_consecutive_losses": _max_streak(outcomes, "loss"),
        "avg_hold_hours": round(avg_hold_h, 1) if avg_hold_h else None,
        "days_active": days_active,
        "top_symbols": [{"symbol": s, "trades": c} for s, c in top_syms],
        "equity_curve": curve,
        "confidence": _confidence(n),
    }


def honest_rating(m: dict) -> dict:
    """A deterministic 0-100 rating that REFUSES to flatter small samples.

    Score is scaled by statistical confidence: a great-looking 12-trade record
    cannot outrank a solid 200-trade one, because it hasn't earned it.
    """
    if m.get("empty") or m.get("trades", 0) < MIN_TRADES_LISTED:
        return {"score": None, "grade": "Unrated",
                "summary": "Not enough closed trades to rate.",
                "flags": ["insufficient data"]}

    n = m["trades"]
    flags: list[str] = []
    parts: dict[str, float] = {}

    # profitability (0-30)
    pf = m.get("profit_factor")
    if pf is None:
        parts["profitability"] = 20.0 if m["net_pnl"] > 0 else 0.0
    else:
        parts["profitability"] = max(0.0, min(30.0, (pf - 1.0) * 30.0))

    # risk control (0-25) — drawdown relative to net profit
    dd, net = m["max_drawdown"], m["net_pnl"]
    if dd <= 0:
        parts["risk_control"] = 18.0
    elif net <= 0:
        parts["risk_control"] = 0.0
        flags.append("net negative")
    else:
        rf = net / dd
        parts["risk_control"] = max(0.0, min(25.0, rf * 8.0))
        if rf < 0.5:
            flags.append("drawdown large relative to profit")

    # consistency (0-25)
    cons = m.get("consistency")
    if cons is None:
        parts["consistency"] = 8.0
    else:
        parts["consistency"] = cons * 25.0
        if cons < 0.5 and m.get("months_traded", 0) >= 3:
            flags.append("most months unprofitable")

    # risk-adjusted return (0-20)
    sh = m.get("sharpe")
    parts["risk_adjusted"] = max(0.0, min(20.0, (sh or 0) * 6.0))

    raw = sum(parts.values())

    # THE HONEST PART: scale by sample confidence
    lvl = m["confidence"]["level"]
    damp = {"none": 0.0, "low": 0.45, "medium": 0.75, "high": 1.0}[lvl]
    score = round(raw * damp, 1)

    if lvl in ("none", "low"):
        flags.append("small sample — rating heavily discounted")

    # coin-flip detection, straight from SKLZ's founding research
    if 0.42 <= m["win_rate"] <= 0.58 and (pf is None or 0.9 <= pf <= 1.1):
        flags.append("statistically indistinguishable from a coin-flip")

    if m.get("max_consecutive_losses", 0) >= 8:
        flags.append(f"{m['max_consecutive_losses']} losses in a row at worst")

    grade = ("Unrated" if score < 20 else "Developing" if score < 40
             else "Solid" if score < 60 else "Strong" if score < 78 else "Exceptional")

    if score < 20:
        summary = "Not enough evidence of an edge yet."
    elif "statistically indistinguishable from a coin-flip" in flags:
        summary = ("Results so far look like chance rather than skill — "
                   "the win rate and profit factor sit where randomness lives.")
    elif net > 0 and (cons or 0) >= 0.6:
        summary = (f"Profitable across {m['profitable_months']} of "
                   f"{m['months_traded']} months with controlled drawdown.")
    elif net > 0:
        summary = "Net profitable, but the month-to-month record is uneven."
    else:
        summary = "Currently net negative over the recorded period."

    return {
        "score": score,
        "grade": grade,
        "components": {k: round(v, 1) for k, v in parts.items()},
        "raw_score": round(raw, 1),
        "confidence_multiplier": damp,
        "summary": summary,
        "flags": flags,
    }
