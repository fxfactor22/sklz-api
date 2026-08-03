"""SKLZ — leader verification standards and revenue share.

Listing a trader means followers trust SKLZ's judgement about them, not just
their own. So "verified" has to mean something specific and checkable, and it
has to keep meaning it after listing.

The standards here are deliberately hard to meet:

  minimum trades      enough that luck washes out of the record
  minimum months      a good month is not a track record
  verified data only  computed from a connected account, never self-reported
  ongoing limits      a leader who passes in January can blow up in March

On compensation: leaders earn a share of subscription revenue from followers
they bring, paid on RETAINED followers rather than new ones. Paying per
signup rewards attracting followers, which rewards visible aggression — the
trader who doubles an account in a month gets more followers than the one who
compounds steadily, even though the second is usually the better business.
Paying on retention rewards not losing them.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone


def min_trades() -> int:
    try:
        return int(os.environ.get("SKLZ_LEADER_MIN_TRADES", 100))
    except (TypeError, ValueError):
        return 100


def min_months() -> float:
    try:
        return float(os.environ.get("SKLZ_LEADER_MIN_MONTHS", 3))
    except (TypeError, ValueError):
        return 3.0


def max_drawdown_allowed() -> float:
    """A leader breaching this is suspended automatically."""
    try:
        return float(os.environ.get("SKLZ_LEADER_MAX_DD", 30))
    except (TypeError, ValueError):
        return 30.0


def default_share_pct() -> float:
    try:
        return float(os.environ.get("SKLZ_LEADER_SHARE_PCT", 10))
    except (TypeError, ValueError):
        return 10.0


def assess_for_listing(metrics: dict) -> dict:
    """Does this trader meet the standard for a public listing?

    `metrics` comes from trader_metrics, computed from their own connected
    account. Returns a decision plus the reasoning, so a rejection can be
    explained rather than just delivered.
    """
    trades = int(metrics.get("trades") or 0)
    months = float(metrics.get("months") or 0)
    dd = abs(float(metrics.get("max_drawdown_pct") or 0))
    pf = metrics.get("profit_factor")
    win = metrics.get("win_rate")

    reasons, blocking = [], []

    if trades < min_trades():
        blocking.append(
            f"{trades} trades — the standard is {min_trades()}. Below that, "
            f"luck and skill are hard to tell apart.")
    if months < min_months():
        blocking.append(
            f"{months:.1f} months of history — the standard is {min_months():.0f}. "
            f"A good month is not a track record.")
    if dd > max_drawdown_allowed():
        blocking.append(
            f"maximum drawdown {dd:.0f}% exceeds the {max_drawdown_allowed():.0f}% "
            f"limit — followers would have to survive that too.")

    # not blocking, but worth stating on the profile
    if pf is not None and pf < 1.1:
        reasons.append(
            f"profit factor {pf:.2f} — barely above break-even before costs.")
    if win is not None and 0.42 <= win <= 0.58 and (pf is None or pf < 1.2):
        reasons.append(
            "win rate and profit factor sit where randomness lives; this record "
            "is not yet distinguishable from a coin-flip.")

    return {
        "eligible": not blocking,
        "blocking": blocking,
        "notes": reasons,
        "standard": {"min_trades": min_trades(), "min_months": min_months(),
                     "max_drawdown_pct": max_drawdown_allowed()},
        "summary": ("Meets the listing standard." if not blocking
                    else "Does not meet the listing standard yet."),
    }


def should_suspend(metrics: dict) -> dict:
    """Ongoing check. Passing review once is not a permanent licence."""
    dd = abs(float(metrics.get("max_drawdown_pct") or 0))
    limit = max_drawdown_allowed()
    if dd > limit:
        return {"suspend": True,
                "reason": (f"drawdown reached {dd:.0f}%, past the {limit:.0f}% "
                           f"limit — listing suspended to protect followers")}
    recent = float(metrics.get("recent_30d_pnl_pct") or 0)
    if recent < -25:
        return {"suspend": True,
                "reason": (f"down {abs(recent):.0f}% in the last 30 days — "
                           f"listing suspended pending review")}
    return {"suspend": False, "reason": ""}


def monthly_earning(subscription_price: float, share_pct: float | None = None,
                    months_retained: int = 1) -> dict:
    """What a leader earns from one retained follower.

    Deliberately paid on retention: a follower who leaves after a week earns
    the leader nothing, so there is no reward for attracting people who will
    not stay.
    """
    pct = default_share_pct() if share_pct is None else float(share_pct)
    per_month = round(subscription_price * pct / 100, 2)
    return {"per_month": per_month,
            "share_pct": pct,
            "months_retained": months_retained,
            "total": round(per_month * max(0, months_retained), 2),
            "basis": ("paid monthly for as long as the follower stays "
                      "subscribed — nothing is earned on followers who leave")}
