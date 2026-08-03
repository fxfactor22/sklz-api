"""SKLZ — research statistics.

The job of this module is to stop you fooling yourself.

THE PROBLEM IT SOLVES
=====================
Given any trade database, it is trivial to slice it until something looks
profitable. "Trades on Tuesday in London with ATR above median won 71%" is the
kind of finding that ends careers — it is almost always noise, and the more
slices you try, the more certain you are to find one.

So every result here comes with:

  a confidence interval    the honest range the true value could sit in
  a sample-size verdict    whether the data can support a conclusion at all
  a multiple-testing note  how many slices were tried before this one

If the sample is too small, this refuses to report an edge and says how many
more trades are needed. That refusal is the product.

WHY THE THRESHOLDS ARE WHERE THEY ARE
=====================================
For a win-rate difference of ~10 percentage points to be detectable at the
usual confidence level, you need roughly 200 observations per group. Below
~30, the confidence interval is so wide that almost any hypothesis survives.
Those are the numbers, not a preference.
"""
from __future__ import annotations

import math
from collections import defaultdict

# below this, no conclusion of any kind
MIN_FOR_ANY_CLAIM = 30
# below this, directional hints only, clearly labelled
MIN_FOR_CONFIDENCE = 200


def wilson_interval(wins: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Confidence interval for a proportion.

    Wilson rather than the naive normal approximation, because the naive one
    is badly wrong at small n — which is exactly where we are.
    """
    if n == 0:
        return (0.0, 1.0)
    p = wins / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, centre - margin), min(1.0, centre + margin))


def summarise(trades: list[dict], label: str = "all") -> dict:
    """Honest performance summary with the uncertainty attached."""
    closed = [t for t in trades if t.get("outcome") in ("win", "loss", "breakeven")]
    n = len(closed)
    if n == 0:
        return {"label": label, "trades": 0, "verdict": "no closed trades"}

    wins = [t for t in closed if t["outcome"] == "win"]
    losses = [t for t in closed if t["outcome"] == "loss"]
    pnl = sum(float(t.get("pnl") or 0) for t in closed)
    gross_win = sum(float(t.get("pnl") or 0) for t in wins)
    gross_loss = abs(sum(float(t.get("pnl") or 0) for t in losses))

    wr = len(wins) / n
    lo, hi = wilson_interval(len(wins), n)
    expectancy = pnl / n

    out = {
        "label": label,
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(wr, 4),
        "win_rate_range": [round(lo, 3), round(hi, 3)],
        "net_pnl": round(pnl, 2),
        "expectancy_per_trade": round(expectancy, 4),
        "profit_factor": round(gross_win / gross_loss, 3) if gross_loss else None,
        "avg_win": round(gross_win / len(wins), 2) if wins else 0,
        "avg_loss": round(gross_loss / len(losses), 2) if losses else 0,
    }

    # the part that matters more than any of the above
    if n < MIN_FOR_ANY_CLAIM:
        out["verdict"] = (
            f"{n} trades is not enough to conclude anything. The true win rate "
            f"could plausibly be anywhere from {lo:.0%} to {hi:.0%}. "
            f"Need at least {MIN_FOR_ANY_CLAIM} before even a hint, and "
            f"{MIN_FOR_CONFIDENCE} before a claim.")
        out["can_conclude"] = False
    elif n < MIN_FOR_CONFIDENCE:
        out["verdict"] = (
            f"{n} trades gives a directional hint only. The win rate could be "
            f"anywhere from {lo:.0%} to {hi:.0%} — that range still includes "
            f"outcomes you would treat very differently. "
            f"{MIN_FOR_CONFIDENCE - n} more trades needed for a real claim.")
        out["can_conclude"] = False
    else:
        if lo <= 0.5 <= hi:
            out["verdict"] = (
                f"{n} trades, win rate {wr:.0%} but the range {lo:.0%}-{hi:.0%} "
                f"still includes 50%. This is not distinguishable from a "
                f"coin-flip.")
            out["can_conclude"] = False
        else:
            side = "above" if lo > 0.5 else "below"
            out["verdict"] = (
                f"{n} trades, win rate {wr:.0%} (range {lo:.0%}-{hi:.0%}), "
                f"which sits entirely {side} 50%. This is a real signal — "
                f"though win rate alone does not make a system profitable.")
            out["can_conclude"] = True

    # expectancy is what actually matters, and it is often negative even at
    # a good win rate
    if expectancy < 0:
        out["expectancy_note"] = (
            f"Expectancy is {expectancy:+.4f} per trade. Whatever the win rate, "
            f"this set of trades loses money on average.")
    return out


def compare(group_a: list[dict], group_b: list[dict],
            name_a: str = "A", name_b: str = "B") -> dict:
    """Is one group genuinely different from the other, or is it noise?

    Uses a two-proportion z-test on win rate. Reports the p-value plainly and
    refuses to call small differences meaningful.
    """
    a = [t for t in group_a if t.get("outcome") in ("win", "loss", "breakeven")]
    b = [t for t in group_b if t.get("outcome") in ("win", "loss", "breakeven")]
    na, nb = len(a), len(b)
    if na < MIN_FOR_ANY_CLAIM or nb < MIN_FOR_ANY_CLAIM:
        return {"comparable": False,
                "reason": (f"{name_a} has {na} trades, {name_b} has {nb}. "
                           f"Both need at least {MIN_FOR_ANY_CLAIM} before a "
                           f"comparison means anything.")}

    wa = sum(1 for t in a if t["outcome"] == "win")
    wb = sum(1 for t in b if t["outcome"] == "win")
    pa, pb = wa / na, wb / nb
    pool = (wa + wb) / (na + nb)
    se = math.sqrt(pool * (1 - pool) * (1 / na + 1 / nb))
    if se == 0:
        return {"comparable": False, "reason": "no variance to compare"}
    z = (pa - pb) / se
    p = 2 * (1 - _normal_cdf(abs(z)))

    significant = p < 0.05
    return {
        "comparable": True,
        name_a: {"trades": na, "win_rate": round(pa, 4)},
        name_b: {"trades": nb, "win_rate": round(pb, 4)},
        "difference": round(pa - pb, 4),
        "p_value": round(p, 4),
        "significant": significant,
        "verdict": (
            f"{name_a} {pa:.0%} vs {name_b} {pb:.0%}. "
            + (f"p={p:.3f} — this difference is unlikely to be chance, but "
               f"remember how many other slices were tried before this one."
               if significant else
               f"p={p:.3f} — this difference is well within what chance "
               f"produces. Treat them as the same.")),
    }


def slice_by(trades: list[dict], field: str,
             min_group: int = MIN_FOR_ANY_CLAIM) -> dict:
    """Break trades down by any recorded field.

    Groups too small to judge are reported as such rather than ranked, which
    is what stops a 4-trade group with 100% win rate looking like a discovery.
    """
    groups: dict = defaultdict(list)
    for t in trades:
        key = t.get(field)
        if key is None:
            key = "unknown"
        groups[str(key)].append(t)

    judged, too_small = [], []
    for key, rows in groups.items():
        s = summarise(rows, label=f"{field}={key}")
        (judged if s["trades"] >= min_group else too_small).append(s)

    judged.sort(key=lambda s: s.get("expectancy_per_trade", 0), reverse=True)
    return {
        "field": field,
        "groups": judged,
        "too_small_to_judge": [
            {"label": s["label"], "trades": s["trades"]} for s in too_small],
        "note": (f"{len(too_small)} group(s) had fewer than {min_group} trades "
                 f"and are not ranked. A small group with a high win rate is "
                 f"the most common way to find an edge that is not there."
                 if too_small else ""),
        "multiple_testing_warning": (
            f"You just tested {len(groups)} groups at once. At the usual "
            f"threshold, roughly 1 in 20 will look significant by chance alone. "
            f"Treat any single standout here as a hypothesis to test on new "
            f"data, not a finding."),
    }


def research_readiness(trades: list[dict]) -> dict:
    """Can this dataset support research yet? An honest answer."""
    n = len([t for t in trades if t.get("outcome")])
    fields = set()
    for t in trades[:200]:
        fields.update(k for k, v in t.items() if v is not None)

    ready = n >= MIN_FOR_CONFIDENCE
    per_day = 20   # rough, for the estimate below
    return {
        "closed_trades": n,
        "fields_captured": len(fields),
        "ready_for_conclusions": ready,
        "needed_for_conclusions": max(0, MIN_FOR_CONFIDENCE - n),
        "estimate": (
            "Ready to draw conclusions." if ready else
            f"About {max(0, MIN_FOR_CONFIDENCE - n)} more closed trades needed "
            f"— roughly {max(1, (MIN_FOR_CONFIDENCE - n) // per_day)} more days "
            f"at the current rate. Analysing before then produces findings that "
            f"will not survive contact with new data."),
        "what_to_do_meanwhile": [
            "Keep the capture running — every trade is an observation you "
            "cannot recover later.",
            "Do not change strategy parameters yet. Each change resets the "
            "sample and you start counting again.",
            "Watch the failure classifications. A pattern in WHY trades fail "
            "shows up in the reasons long before it shows up in the win rate.",
        ],
    }


def _normal_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))
