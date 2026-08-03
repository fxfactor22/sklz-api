"""SKLZ — real order book depth for crypto.

WHY THIS EXISTS
===============
On forex there is no central exchange, so there is no consolidated order book
to read — the institutional framework has to infer delta from tick side and
score DOM as unavailable. That is 35% of the model running blind.

Crypto is different. Every major exchange publishes genuine depth and real
traded volume with aggressor flags, free. So on crypto the same framework can
run on measured data instead of approximation.

WHAT IS REAL HERE
=================
  order book depth   actual resting bids and asks, by price level
  traded volume      real size, not a count of price changes
  aggressor side     whether each trade hit the bid or lifted the offer

WHAT STILL IS NOT
=================
  iceberg orders     hidden by definition; inferable at best, never certain
  spoofing           needs per-order lifecycle, not depth snapshots
  a consolidated     one exchange's book is not "the market" — Bybit's depth
  view               is Bybit's, and other venues may look different

Those limits are stated rather than papered over, because a confident-looking
number built on a partial view is worse than an honest one.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class BookSnapshot:
    symbol: str = ""
    bids: list = field(default_factory=list)      # [[price, size], ...]
    asks: list = field(default_factory=list)
    bid_volume: float = 0.0
    ask_volume: float = 0.0
    spread: float = 0.0
    mid: float = 0.0
    imbalance: float = 0.0                        # -1 (ask heavy) .. +1 (bid heavy)
    levels: int = 0
    fetched_at: float = 0.0
    quality: str = "none"                         # none | measured
    note: str = ""


def read_book(adapter, symbol: str, depth: int = 50) -> BookSnapshot:
    """Fetch live depth from the exchange.

    `adapter` is the ccxt-backed exchange wrapper. Depth is real resting size,
    not an estimate — which is what makes crypto worth doing this on.
    """
    snap = BookSnapshot(symbol=symbol)
    try:
        ob = adapter.client.fetch_order_book(symbol, limit=depth)
    except Exception as exc:  # noqa: BLE001
        snap.note = f"could not read the book: {str(exc)[:120]}"
        return snap

    bids = [[float(p), float(s)] for p, s in (ob.get("bids") or [])[:depth]]
    asks = [[float(p), float(s)] for p, s in (ob.get("asks") or [])[:depth]]
    if not bids or not asks:
        snap.note = "exchange returned an empty book"
        return snap

    snap.bids, snap.asks = bids, asks
    snap.bid_volume = round(sum(s for _p, s in bids), 6)
    snap.ask_volume = round(sum(s for _p, s in asks), 6)
    total = snap.bid_volume + snap.ask_volume
    snap.imbalance = round((snap.bid_volume - snap.ask_volume) / total, 4) if total else 0.0
    snap.spread = round(asks[0][0] - bids[0][0], 8)
    snap.mid = round((asks[0][0] + bids[0][0]) / 2, 8)
    snap.levels = min(len(bids), len(asks))
    snap.fetched_at = time.time()
    snap.quality = "measured"
    snap.note = f"{snap.levels} levels each side from the exchange"
    return snap


def wall_ahead(snap: BookSnapshot, side: int, within_pct: float = 0.5) -> dict:
    """Is there a large resting order in the path of this trade?

    A wall is a price level holding far more size than its neighbours. Price
    tends to stall there, so breaking directly into one is a worse entry than
    breaking into thin book.
    """
    if snap.quality != "measured":
        return {"found": False, "reason": "no book data"}

    levels = snap.asks if side > 0 else snap.bids
    if not levels:
        return {"found": False, "reason": "no levels on that side"}

    ref = snap.mid or levels[0][0]
    band = [l for l in levels if abs(l[0] - ref) / ref * 100 <= within_pct]
    if len(band) < 3:
        return {"found": False, "reason": "not enough levels nearby to judge"}

    sizes = [s for _p, s in band]
    avg = sum(sizes) / len(sizes)
    biggest = max(band, key=lambda l: l[1])
    ratio = biggest[1] / avg if avg else 0

    if ratio >= 4:
        return {"found": True, "price": biggest[0], "size": biggest[1],
                "times_average": round(ratio, 1),
                "reason": (f"resting order at {biggest[0]} is {ratio:.0f}x the "
                           f"average size nearby — price will likely stall there")}
    return {"found": False, "times_average": round(ratio, 1),
            "reason": "no unusual concentration ahead"}


def book_supports(snap: BookSnapshot, side: int, threshold: float = 0.15) -> dict:
    """Does resting liquidity lean with this trade or against it?"""
    if snap.quality != "measured":
        return {"supportive": False, "available": False, "reason": "no book data"}

    lean = snap.imbalance
    supportive = (lean > threshold) if side > 0 else (lean < -threshold)
    direction = "bid" if lean > 0 else "ask"
    return {"supportive": supportive, "available": True, "imbalance": lean,
            "bid_volume": snap.bid_volume, "ask_volume": snap.ask_volume,
            "reason": (f"book leans {direction} by {abs(lean):.0%} "
                       f"({'supports' if supportive else 'does not support'} "
                       f"this direction)")}


def real_delta(adapter, symbol: str, minutes: int = 15) -> dict:
    """Genuine buy/sell volume from public trades.

    Crypto exchanges report each trade's aggressor side, so this is MEASURED
    delta — no inference from tick position, unlike the forex path.
    """
    since = int((time.time() - minutes * 60) * 1000)
    try:
        trades = adapter.client.fetch_trades(symbol, since=since, limit=1000)
    except Exception as exc:  # noqa: BLE001
        return {"quality": "none", "reason": f"could not read trades: {str(exc)[:120]}"}

    buy = sell = 0.0
    cum, running = [], 0.0
    for t in trades or []:
        amt = float(t.get("amount") or 0)
        if amt <= 0:
            continue
        if (t.get("side") or "").lower() == "buy":
            buy += amt
            running += amt
        else:
            sell += amt
            running -= amt
        cum.append(running)

    if not cum:
        return {"quality": "none", "reason": "no trades in the window"}

    total = buy + sell
    return {"quality": "measured",
            "buy_volume": round(buy, 6), "sell_volume": round(sell, 6),
            "delta": round(buy - sell, 6),
            "imbalance": round((buy - sell) / total, 4) if total else 0,
            "cumulative": cum[-500:],
            "trades_seen": len(cum),
            "reason": (f"{len(cum)} trades over {minutes} min, aggressor side "
                       f"reported by the exchange — this is measured, not inferred")}


def assess_crypto(adapter, symbol: str, side: int,
                  price_move_pct: float = 0.0) -> dict:
    """Everything the book and tape can tell us about this entry."""
    snap = read_book(adapter, symbol)
    support = book_supports(snap, side)
    wall = wall_ahead(snap, side)
    delta = real_delta(adapter, symbol)

    flags = []
    if wall.get("found"):
        flags.append(f"WALL AHEAD — {wall['reason']}")
    if support.get("available") and not support["supportive"]:
        flags.append(f"book against the trade — {support['reason']}")

    # absorption: heavy one-sided aggression that price is not responding to
    absorbed = False
    if delta.get("quality") == "measured":
        imb = delta.get("imbalance", 0)
        if abs(imb) > 0.25 and abs(price_move_pct) < 0.1:
            absorbed = True
            who = "sellers" if imb > 0 else "buyers"
            flags.append(
                f"ABSORPTION — aggression is {abs(imb):.0%} one-sided but price "
                f"has barely moved; {who} are absorbing it")

    return {"book": {"available": snap.quality == "measured",
                     "imbalance": snap.imbalance, "spread": snap.spread,
                     "levels": snap.levels, "note": snap.note},
            "support": support, "wall": wall, "delta": delta,
            "absorbed": absorbed, "flags": flags,
            "data_quality": ("measured" if snap.quality == "measured"
                             else "unavailable"),
            "caveat": ("This is one exchange's book. Other venues may differ, "
                       "and hidden or iceberg orders are not visible to anyone "
                       "reading depth.")}
