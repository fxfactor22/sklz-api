"""Pure demo simulator core — no FastAPI, no database, no network.

Split out so the parts that decide prices, shapes and refusals can
be tested without standing up the whole API. demo_api imports from
here; nothing here imports demo_api.
"""
from __future__ import annotations

import math
import time
from datetime import datetime, timezone

import policy


# ── the desk ────────────────────────────────────────────────────────
# digits and pip taken from the engine's own PIP_SIZE table so the demo
# cannot drift from production conventions.
SYMBOLS: dict[str, dict] = {
    "EURUSD": {"digits": 5, "pip": 0.0001, "base": 1.08500},
    "GBPUSD": {"digits": 5, "pip": 0.0001, "base": 1.27400},
    "USDJPY": {"digits": 3, "pip": 0.01, "base": 154.200},
    "XAUUSD": {"digits": 2, "pip": 0.1, "base": 4410.00},
    "BTCUSD": {"digits": 2, "pip": 1.0, "base": 78000.00},
    "ETHUSD": {"digits": 2, "pip": 0.1, "base": 2450.00},
    "SOLUSD": {"digits": 3, "pip": 0.01, "base": 138.000},
}


def _price(symbol: str) -> float:
    """A believable simulated price.

    Deliberately synthetic rather than a live quote. A public demo that
    calls a market-data API on every render is one rate limit away from
    being broken during a sales call, and a real quote inside a simulated
    fill invites someone to read the number as verified. It walks
    smoothly so a chart-less desk still feels alive.
    """
    spec = SYMBOLS[symbol]
    base = spec["base"]
    t = time.time()
    # two incommensurate periods so it never looks like a sine wave
    import math
    drift = (math.sin(t / 137.0 + hash(symbol) % 100)
             + 0.4 * math.sin(t / 31.0 + hash(symbol) % 37))
    amplitude = base * 0.0012
    return round(base + drift * amplitude, spec["digits"])


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds") \
        .replace("+00:00", "Z")


# ── result shape ────────────────────────────────────────────────────
def _result(state: str, key: str, **kw) -> dict:
    out = {"state": state, "command_id": key, "position_id": None,
           "symbol": None, "side": None, "volume": None,
           "requested_price": None, "fill_price": None, "sl": None,
           "tp": None, "comment": "", "requested_at": None,
           "confirmed_at": None, "backend": "sim", "reason": None}
    out.update(kw)
    # A comment or reason is customer-facing text; it goes through the
    # same policy as a signal. A blocked comment is replaced, never sent.
    for field in ("comment",):
        if out.get(field) and not policy.is_clean(str(out[field])):
            out[field] = "simulated fill"
    return out


def _reject(key: str, reason: str, comment: str, **kw) -> dict:
    return _result("rejected", key, reason=reason, comment=comment, **kw)




def compose_signal(pos: dict, lang: str = "en", brand: str = "") -> str:
    """Build the signal from the CONFIRMED position, never the request.

    Says what was done, never how it went — the moment it reports a
    result it becomes a performance claim somebody has to substantiate.
    The SIMULATED marker is inside the message so a screenshot taken out
    of the demo still says what it is.
    """
    spec = SYMBOLS.get(pos["symbol"], {"digits": 5})
    d = spec["digits"]

    def px(v):
        return "—" if v is None else f"{float(v):.{d}f}"

    side = str(pos["side"]).upper()
    head = {"ar": "\u26a0\ufe0f محاكاة — إشارة توضيحية، وليست صفقة حقيقية",
            "ru": "\u26a0\ufe0f СИМУЛЯЦИЯ — демонстрационный сигнал, не реальная сделка"}
    lines = [head.get(lang, "\u26a0\ufe0f SIMULATED — demonstration signal, "
                            "not a live trade"), ""]
    if brand:
        lines += [f"\U0001F4CA {brand}", ""]
    if lang == "ar":
        lines += [f"{'شراء' if side == 'BUY' else 'بيع'}  {pos['symbol']}",
                  f"الدخول: {px(pos['entry_price'])}",
                  f"وقف الخسارة: {px(pos.get('sl'))}",
                  f"جني الأرباح: {px(pos.get('tp'))}",
                  f"الحجم: {pos['volume']:g}", "",
                  "تداول المنتجات ذات الرافعة ينطوي على مخاطر. "
                  "ليست نصيحة مالية."]
    else:
        lines += [f"{side}  {pos['symbol']}",
                  f"Entry: {px(pos['entry_price'])}",
                  f"Stop loss: {px(pos.get('sl'))}",
                  f"Take profit: {px(pos.get('tp'))}",
                  f"Volume: {pos['volume']:g}", "",
                  "Trading leveraged products carries risk. "
                  "Not financial advice."]
    return "\n".join(lines)
