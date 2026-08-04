"""SKLZ — stop loss and take profit on spot.

HOW THIS ACTUALLY WORKS, AND WHY IT MATTERS
===========================================
On spot there is no "attach a stop to your buy". A stop is a separate
conditional order placed after the position exists. That produces two very
different kinds of protection, and a follower must be able to tell them apart:

  EXCHANGE-SIDE   the venue holds the order. It fires whether or not SKLZ is
                  running, whether or not the internet works, whether or not
                  we exist. This is real protection.

  BOT-SIDE        we watch the price and sell when it is hit. If the poller
                  stops, the machine reboots, or the API is down, the stop
                  simply does not happen. The position is unprotected and the
                  user has no way to know.

Bot-side stops are the more dangerous of the two precisely because they LOOK
identical in a user interface. Someone sees "stop loss: $58,000" and believes
they are covered. So every stop here carries a `protection` field saying which
kind it is, and the UI is expected to show it. A user who thinks they are
protected and is not has been actively misled, which is worse than having no
feature at all.

We place exchange-side wherever the venue supports it, fall back to bot-side
only when it does not, and say so both times.
"""
from __future__ import annotations

from datetime import datetime, timezone


def supports_exchange_stops(adapter, symbol: str) -> dict:
    """Can this venue hold a conditional order for us?

    Checked per symbol rather than per exchange — support varies by market on
    several venues, and assuming from the exchange name is how you end up
    telling someone they are protected when they are not.
    """
    try:
        has = getattr(adapter.client, "has", {}) or {}
        # ccxt reports capability; several venues support stops on some
        # markets only, so the market itself is checked too
        if not (has.get("createStopLimitOrder") or has.get("createStopMarketOrder")
                or has.get("createStopOrder")):
            return {"supported": False,
                    "reason": f"{adapter.display_name} does not accept stop "
                              f"orders through this API"}
        markets = adapter.client.markets or {}
        m = markets.get(symbol) or {}
        if m and m.get("spot") is False:
            return {"supported": False, "reason": "not a spot market"}
        return {"supported": True, "reason": "the exchange will hold this order"}
    except Exception as exc:  # noqa: BLE001
        return {"supported": False,
                "reason": f"could not determine support ({type(exc).__name__})"}


def place_protection(adapter, symbol: str, amount: float,
                     stop_price: float | None = None,
                     take_profit: float | None = None,
                     log=print) -> dict:
    """Place stop loss and/or take profit for an existing spot position.

    Returns what was actually achieved, including which kind of protection,
    so nothing downstream has to guess.
    """
    out = {"stop": None, "take_profit": None, "protection": "none",
           "warnings": []}
    if not (stop_price or take_profit):
        return {**out, "reason": "nothing to place"}

    cap = supports_exchange_stops(adapter, symbol)

    if not cap["supported"]:
        out["protection"] = "bot"
        out["warnings"].append(
            f"{cap['reason']}. Your stop will be watched by SKLZ instead of "
            f"held by the exchange — which means it does NOT fire if SKLZ is "
            f"offline. Consider placing it yourself on the exchange.")
        if stop_price:
            out["stop"] = {"price": stop_price, "type": "bot_watched"}
        if take_profit:
            out["take_profit"] = {"price": take_profit, "type": "bot_watched"}
        return out

    out["protection"] = "exchange"

    if stop_price:
        try:
            order = adapter.client.create_order(
                symbol, "market", "sell", amount, None,
                {"stopLossPrice": stop_price, "reduceOnly": True})
            out["stop"] = {"price": stop_price, "type": "exchange_held",
                           "order_id": order.get("id")}
            log(f"[protect] {symbol} stop placed with the exchange at {stop_price}")
        except Exception as exc:  # noqa: BLE001
            out["protection"] = "bot"
            out["stop"] = {"price": stop_price, "type": "bot_watched"}
            out["warnings"].append(
                f"the exchange rejected the stop order ({str(exc)[:110]}), so "
                f"SKLZ will watch this level instead — it will not fire if "
                f"SKLZ is offline")

    if take_profit:
        try:
            order = adapter.client.create_order(
                symbol, "limit", "sell", amount, take_profit,
                {"reduceOnly": True})
            out["take_profit"] = {"price": take_profit, "type": "exchange_held",
                                  "order_id": order.get("id")}
            log(f"[protect] {symbol} take profit placed at {take_profit}")
        except Exception as exc:  # noqa: BLE001
            out["take_profit"] = {"price": take_profit, "type": "bot_watched"}
            out["warnings"].append(
                f"take profit could not be placed on the exchange "
                f"({str(exc)[:110]}) — SKLZ will watch it instead")

    return out


def describe_protection(result: dict) -> str:
    """One honest sentence for the interface."""
    p = result.get("protection")
    if p == "exchange":
        return ("Held by the exchange — this fires even if SKLZ is offline.")
    if p == "bot":
        return ("Watched by SKLZ, NOT held by the exchange. If SKLZ stops "
                "running, this will not fire and your position is unprotected.")
    return "No protection set on this position."


def check_bot_stops(adapter, positions: list[dict], log=print) -> list[dict]:
    """Evaluate bot-watched levels. Called on every poll.

    Only handles stops that could not be placed with the exchange. Anything
    exchange-held is the venue's responsibility and is not touched here.
    """
    fired = []
    for pos in positions:
        if (pos.get("protection") or "") != "bot":
            continue
        symbol = pos.get("symbol")
        if not symbol:
            continue
        try:
            price = adapter.price(symbol)
        except Exception:
            continue
        if not price:
            continue

        stop = (pos.get("stop") or {}).get("price")
        tp = (pos.get("take_profit") or {}).get("price")
        hit = None
        if stop and price <= float(stop):
            hit = ("stop", stop)
        elif tp and price >= float(tp):
            hit = ("take_profit", tp)
        if not hit:
            continue

        kind, level = hit
        try:
            adapter.create_spot_order(symbol, "sell", float(pos["amount"]))
            fired.append({"symbol": symbol, "kind": kind, "level": level,
                          "price": price, "closed": True,
                          "at": datetime.now(timezone.utc).isoformat()})
            log(f"[protect] {symbol} {kind} hit at {price} (level {level}) — "
                f"position closed")
        except Exception as exc:  # noqa: BLE001
            fired.append({"symbol": symbol, "kind": kind, "level": level,
                          "price": price, "closed": False,
                          "error": str(exc)[:140]})
            log(f"[protect] {symbol} {kind} hit but the sell FAILED: "
                f"{str(exc)[:120]} — the position is still open")
    return fired


def partial_sell_amount(held: float, pct: float, market: dict | None = None
                        ) -> dict:
    """Work out how much to sell for a partial exit.

    Rounds down to the venue's step size: rounding up would try to sell more
    than is held and be rejected, which reads as a broken button.
    """
    pct = max(1.0, min(100.0, float(pct)))
    amount = held * pct / 100.0

    # Closing "everything" rarely works at exactly 100%. Fees are taken from
    # the asset on some venues, part of the balance may be reserved against an
    # open order, and the reported free amount can be a hair stale. Asking for
    # every last unit then gets rejected as insufficient — which reads as a
    # broken button rather than a rounding artefact. Shaving a fraction off
    # closes the position in practice and leaves only dust.
    if pct >= 100.0:
        amount = held * 0.999

    step = None
    min_amt = None
    if market:
        limits = market.get("limits") or {}
        min_amt = (limits.get("amount") or {}).get("min")
        prec = (market.get("precision") or {}).get("amount")
        # ccxt reports precision two ways depending on the exchange:
        #   an INTEGER means decimal places (6 -> steps of 0.000001)
        #   a FLOAT below 1 means the step size itself (0.001 -> steps of 0.001)
        # Treating the integer form as a step size makes every amount round to
        # zero, which reads as "the button does nothing".
        if isinstance(prec, int) or (isinstance(prec, float) and prec >= 1):
            step = 10 ** (-int(prec))
        elif isinstance(prec, float) and 0 < prec < 1:
            step = float(prec)

    if step:
        amount = (int(amount / step)) * step

    if min_amt and amount < float(min_amt):
        return {"ok": False, "amount": 0,
                "reason": (f"{pct:.0f}% of your position is {amount:.8f}, below "
                           f"the exchange minimum of {min_amt}. Sell a larger "
                           f"share, or close the whole position.")}
    if amount <= 0:
        return {"ok": False, "amount": 0,
                "reason": "that share rounds to zero at this exchange's step size"}

    remaining = held - amount
    note = f"selling {pct:.0f}% ({amount:.8f}), leaving {remaining:.8f}"
    if pct >= 100.0 and remaining > 0:
        note = (f"closing the position ({amount:.8f}). A fraction "
                f"({remaining:.8f}) is left behind because exchanges reject an "
                f"order for the exact full balance — fees and rounding make it "
                f"unfillable. What remains is dust.")
    return {"ok": True, "amount": amount, "pct": pct,
            "remaining": round(remaining, 10), "note": note}
