"""SKLZ Pro Trader demo backend — the trading truth behind ISKRA's demo.

Implements SKLZ-DEMO-BACKEND-CONTRACT verbatim: its routes, its field
names, its states, its reason codes, its signature scheme.

THE ONE RULE THIS FILE EXISTS TO PROTECT
========================================
There is ZERO PATH from an anonymous sales demo to a real brokerage
account or a real copy follower. Concretely, in this file:

  * no import of mt5io, bot_orders, copy_api, journal or signals
  * a demo position lives in demo_positions and nowhere else
  * every result carries backend="sim"
  * the Telegram destination is resolved server-side from an allowlist;
    a chat id arriving over the wire is ignored, not validated

A demo command is not a broker command, so it does not travel on the
broker command protocol however convenient that would have been.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Request, status
from supabase import Client

import envflags
import policy
from db import get_supabase
from routing import RoutingScope, resolve_destinations

router = APIRouter(prefix="/api/demo", tags=["demo"])

MAX_SKEW_SECONDS = 300
INACTIVITY_TTL_HOURS = 72
MAX_OPEN_POSITIONS = 8
MAX_VOLUME = 10.0

# Per demo_session, our own limits — deliberately not a copy of ISKRA's.
# Defence in depth: two independent limiters, neither assuming the other
# is the only one.
LIMITS = {"trade": (40, 3600), "signal": (20, 3600)}


from demo_sim import (SYMBOLS, _price, _now_iso,
                      _result, _reject, compose_signal)


# ── signature ───────────────────────────────────────────────────────
async def _verify(request: Request) -> dict:
    """x-sklz-signature: t=<unix>,v1=<hmac sha256 of "t.body">.

    Refuses when no secret is configured. Falling through to
    unauthenticated because a variable is unset is how a demo endpoint
    becomes a public trading endpoint.
    """
    secret = os.environ.get("SKLZ_DEMO_SECRET", "")
    if not secret:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                            "bad_signature")
    header = request.headers.get("x-sklz-signature", "")
    ts, sig = "", ""
    for part in header.split(","):
        k, _, v = part.strip().partition("=")
        if k == "t":
            ts = v
        elif k == "v1":
            sig = v
    if not ts or not sig:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad_signature")
    try:
        if abs(time.time() - int(ts)) > MAX_SKEW_SECONDS:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                                "bad_signature")
    except ValueError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                            "bad_signature") from None

    raw = await request.body()
    expected = hmac.new(secret.encode(), (ts + ".").encode() + raw,
                        hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig.lower()):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad_signature")

    try:
        return json.loads(raw.decode() or "{}")
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "bad_request") from None


def _session(body: dict) -> str:
    s = str(body.get("demo_session") or "").strip().lower()
    if len(s) != 32 or any(c not in "0123456789abcdef" for c in s):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "bad_request")
    return s


# ── idempotency ─────────────────────────────────────────────────────
def _claim(sb: Client, session: str, key: str, endpoint: str) -> dict | None:
    """Claim a key, or return the settled result of a previous attempt.

    Returns None when this caller owns the work. Returns a result dict
    when the key has already been handled — which is what stops a dropped
    connection from opening a second position.
    """
    if not key:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "bad_request")
    try:
        rows = (sb.table("demo_commands").select("*")
                .eq("demo_session", session)
                .eq("idempotency_key", key).limit(1).execute()).data or []
    except Exception:
        rows = []
    if rows:
        row = rows[0]
        if row.get("state") == "settled" and row.get("result"):
            out = dict(row["result"])
            out["replay"] = True
            return out
        return {"state": "pending", "command_id": key,
                "backend": "sim", "replay": True}
    try:
        sb.table("demo_commands").insert(
            {"demo_session": session, "idempotency_key": key,
             "endpoint": endpoint}).execute()
    except Exception:
        # someone inserted between our read and our write: they own it
        return {"state": "pending", "command_id": key,
                "backend": "sim", "replay": True}
    return None


def _settle(sb: Client, session: str, key: str, result: dict) -> dict:
    try:
        sb.table("demo_commands").update(
            {"state": "settled", "result": result,
             "settled_at": _now_iso()}) \
            .eq("demo_session", session).eq("idempotency_key", key).execute()
    except Exception:
        pass
    return result


# ── rate limiting ───────────────────────────────────────────────────
def _rate_ok(sb: Client, session: str, bucket: str) -> tuple[bool, int]:
    limit, window = LIMITS[bucket]
    start = datetime.now(timezone.utc).replace(minute=0, second=0,
                                               microsecond=0)
    try:
        rows = (sb.table("demo_rate").select("count")
                .eq("demo_session", session).eq("bucket", bucket)
                .eq("window_start", start.isoformat()).limit(1)
                .execute()).data or []
        used = int(rows[0]["count"]) if rows else 0
        if used >= limit:
            return False, int((start + timedelta(seconds=window)
                               - datetime.now(timezone.utc)).total_seconds())
        sb.table("demo_rate").upsert(
            {"demo_session": session, "bucket": bucket,
             "window_start": start.isoformat(), "count": used + 1},
            on_conflict="demo_session,bucket,window_start").execute()
    except Exception:
        return True, 0          # never block a demo on our own bookkeeping
    return True, 0


def _rate_limited(retry_after: int):
    return HTTPException(
        status.HTTP_429_TOO_MANY_REQUESTS,
        json.dumps({"reason": "rate_limited",
                    "retry_after": max(retry_after, 1)}))


# ── endpoints ───────────────────────────────────────────────────────
@router.post("/_diag")
async def diagnostic(request: Request) -> dict:
    """Non-secret fingerprints of what SKLZ actually received.

    Exists to settle one question with evidence instead of theory: when
    two correct-looking HMAC implementations disagree, the answer is in
    the bytes, and neither side can see the other's.

    Returns NOTHING that could help forge a signature — no secret, no
    full HMAC, only lengths, digests and 12-character prefixes. Off
    unless SKLZ_DEMO_DIAG=1, and it performs no action and touches no
    state whatever the signature says.
    """
    if not envflags.flag("SKLZ_DEMO_DIAG"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not_found")

    raw = await request.body()
    header = request.headers.get("x-sklz-signature", "")
    ts, sig = "", ""
    for part in header.split(","):
        k, _, v = part.strip().partition("=")
        if k == "t":
            ts = v
        elif k == "v1":
            sig = v

    secret = os.environ.get("SKLZ_DEMO_SECRET", "")
    payload = (ts + ".").encode() + raw
    calc = (hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
            if secret else "")

    skew = None
    try:
        skew = round(time.time() - int(ts))
    except ValueError:
        pass

    return {
        "received": {
            "header_present": bool(header),
            "header_raw_len": len(header),
            "t": ts,
            "t_len": len(ts),
            "sig_len": len(sig),
            "sig_prefix": sig[:12].lower(),
            "content_type": request.headers.get("content-type", ""),
            "content_length_header": request.headers.get("content-length", ""),
        },
        "body": {
            "byte_length": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "first_40_chars": raw[:40].decode("utf-8", "replace"),
            "last_20_chars": raw[-20:].decode("utf-8", "replace"),
        },
        "signing_payload": {
            "byte_length": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        },
        "hmac": {
            "calculated_prefix": calc[:12],
            "received_prefix": sig[:12].lower(),
            "match": bool(calc) and hmac.compare_digest(calc, sig.lower()),
        },
        "clock": {"server_unix": int(time.time()), "skew_seconds": skew,
                  "within_window": skew is not None
                  and abs(skew) <= MAX_SKEW_SECONDS},
        "secret": {"configured": bool(secret),
                   "fingerprint": hashlib.sha256(
                       secret.encode()).hexdigest()[:12] if secret else ""},
    }


@router.post("/trade/create")
async def trade_create(request: Request) -> dict:
    body = await _verify(request)
    sb = get_supabase()
    session = _session(body)
    key = str(body.get("idempotency_key") or "")
    prior = _claim(sb, session, key, "create")
    if prior is not None:
        return {"ok": True, "result": prior}

    requested_at = _now_iso()
    symbol = str(body.get("symbol") or "").upper()
    side = str(body.get("side") or "").lower()
    try:
        volume = float(body.get("volume") or 0)
    except (TypeError, ValueError):
        volume = 0.0

    if symbol not in SYMBOLS:
        return {"ok": True, "result": _settle(sb, session, key, _reject(
            key, "unknown_symbol",
            "that symbol is not on the demo desk",
            symbol=symbol, requested_at=requested_at,
            confirmed_at=_now_iso()))}
    if side not in ("buy", "sell"):
        return {"ok": True, "result": _settle(sb, session, key, _reject(
            key, "bad_side", "side must be buy or sell", symbol=symbol,
            requested_at=requested_at, confirmed_at=_now_iso()))}
    if volume <= 0 or volume > MAX_VOLUME:
        return {"ok": True, "result": _settle(sb, session, key, _reject(
            key, "bad_volume",
            f"volume must be between 0 and {MAX_VOLUME:g} on the demo desk",
            symbol=symbol, side=side, requested_at=requested_at,
            confirmed_at=_now_iso()))}

    ok_rate, retry = _rate_ok(sb, session, "trade")
    if not ok_rate:
        raise _rate_limited(retry)

    try:
        open_rows = (sb.table("demo_positions").select("position_id")
                     .eq("demo_session", session).eq("state", "open")
                     .execute()).data or []
    except Exception as exc:  # noqa: BLE001
        return {"ok": True, "result": _result(
            "error", key, reason="simulator_unavailable",
            comment="the demo desk is not responding",
            requested_at=requested_at, confirmed_at=_now_iso())}
    if len(open_rows) >= MAX_OPEN_POSITIONS:
        return {"ok": True, "result": _settle(sb, session, key, _reject(
            key, "too_many_positions",
            f"the demo desk holds {MAX_OPEN_POSITIONS} positions at once",
            symbol=symbol, side=side, requested_at=requested_at,
            confirmed_at=_now_iso()))}

    spec = SYMBOLS[symbol]
    requested = _price(symbol)
    sl = body.get("sl")
    tp = body.get("tp")
    sl = float(sl) if sl is not None else None
    tp = float(tp) if tp is not None else None

    # Wrong-side stops are refused because refusing them teaches the
    # prospect something true about the product.
    if sl is not None:
        if (side == "buy" and sl >= requested) or \
           (side == "sell" and sl <= requested):
            return {"ok": True, "result": _settle(sb, session, key, _reject(
                key, "sl_wrong_side",
                "stop loss must sit below the entry on a buy and above it "
                "on a sell", symbol=symbol, side=side, volume=volume,
                requested_price=requested, sl=sl, tp=tp,
                requested_at=requested_at, confirmed_at=_now_iso()))}
    if tp is not None:
        if (side == "buy" and tp <= requested) or \
           (side == "sell" and tp >= requested):
            return {"ok": True, "result": _settle(sb, session, key, _reject(
                key, "tp_wrong_side",
                "take profit must sit above the entry on a buy and below "
                "it on a sell", symbol=symbol, side=side, volume=volume,
                requested_price=requested, sl=sl, tp=tp,
                requested_at=requested_at, confirmed_at=_now_iso()))}

    # a small, honest slippage so the fill is not suspiciously perfect
    slip = spec["pip"] * (0.4 if side == "buy" else -0.4)
    fill = round(requested + slip, spec["digits"])
    pid = "SKLZ-" + str(secrets.randbelow(9_000_000) + 1_000_000)

    try:
        sb.table("demo_positions").insert({
            "position_id": pid, "demo_session": session, "symbol": symbol,
            "side": side, "volume": volume, "entry_price": fill,
            "sl": sl, "tp": tp, "state": "open"}).execute()
    except Exception:  # noqa: BLE001
        return {"ok": True, "result": _result(
            "error", key, reason="simulator_unavailable",
            comment="the demo desk is not responding", symbol=symbol,
            requested_at=requested_at, confirmed_at=_now_iso())}

    return {"ok": True, "result": _settle(sb, session, key, _result(
        "filled", key, position_id=pid, symbol=symbol, side=side,
        volume=volume, requested_price=requested, fill_price=fill,
        sl=sl, tp=tp, comment="simulated fill",
        requested_at=requested_at, confirmed_at=_now_iso()))}


def _position(sb: Client, session: str, pid: str) -> dict | None:
    """A position, scoped to its own session. Cross-session access is not
    an authorisation error to explain — it simply does not exist."""
    try:
        rows = (sb.table("demo_positions").select("*")
                .eq("demo_session", session)
                .eq("position_id", str(pid)).limit(1).execute()).data or []
    except Exception:
        return None
    return rows[0] if rows else None


@router.post("/trade/modify")
async def trade_modify(request: Request) -> dict:
    body = await _verify(request)
    sb = get_supabase()
    session = _session(body)
    key = str(body.get("idempotency_key") or "")
    prior = _claim(sb, session, key, "modify")
    if prior is not None:
        return {"ok": True, "result": prior}

    requested_at = _now_iso()
    pos = _position(sb, session, body.get("position_id"))
    if not pos:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown_position")
    if pos.get("state") != "open":
        return {"ok": True, "result": _settle(sb, session, key, _reject(
            key, "position_closed", "that position is already closed",
            position_id=pos["position_id"], symbol=pos["symbol"],
            requested_at=requested_at, confirmed_at=_now_iso()))}

    spec = SYMBOLS.get(pos["symbol"], {"digits": 5, "pip": 0.0001})
    entry = float(pos["entry_price"])
    side = pos["side"]
    # null means LEAVE UNCHANGED, per the contract. Not "clear".
    sl = pos.get("sl") if body.get("sl") is None else float(body["sl"])
    tp = pos.get("tp") if body.get("tp") is None else float(body["tp"])

    if sl is not None and ((side == "buy" and sl >= entry) or
                           (side == "sell" and sl <= entry)):
        return {"ok": True, "result": _settle(sb, session, key, _reject(
            key, "sl_wrong_side",
            "stop loss must sit below the entry on a buy and above it on "
            "a sell", position_id=pos["position_id"], symbol=pos["symbol"],
            side=side, sl=sl, tp=tp, requested_at=requested_at,
            confirmed_at=_now_iso()))}
    if tp is not None and ((side == "buy" and tp <= entry) or
                           (side == "sell" and tp >= entry)):
        return {"ok": True, "result": _settle(sb, session, key, _reject(
            key, "tp_wrong_side",
            "take profit must sit above the entry on a buy and below it "
            "on a sell", position_id=pos["position_id"],
            symbol=pos["symbol"], side=side, sl=sl, tp=tp,
            requested_at=requested_at, confirmed_at=_now_iso()))}

    try:
        sb.table("demo_positions").update(
            {"sl": sl, "tp": tp, "last_seen_at": _now_iso()}) \
            .eq("position_id", pos["position_id"]) \
            .eq("demo_session", session).execute()
    except Exception:  # noqa: BLE001
        return {"ok": True, "result": _result(
            "error", key, reason="simulator_unavailable",
            comment="the demo desk is not responding",
            requested_at=requested_at, confirmed_at=_now_iso())}

    return {"ok": True, "result": _settle(sb, session, key, _result(
        "modified", key, position_id=pos["position_id"],
        symbol=pos["symbol"], side=side, volume=float(pos["volume"]),
        fill_price=entry, sl=sl, tp=tp, comment="levels updated",
        requested_at=requested_at, confirmed_at=_now_iso()))}


@router.post("/trade/breakeven")
async def trade_breakeven(request: Request) -> dict:
    body = await _verify(request)
    sb = get_supabase()
    session = _session(body)
    key = str(body.get("idempotency_key") or "")
    prior = _claim(sb, session, key, "breakeven")
    if prior is not None:
        return {"ok": True, "result": prior}

    requested_at = _now_iso()
    pos = _position(sb, session, body.get("position_id"))
    if not pos:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown_position")
    if pos.get("state") != "open":
        return {"ok": True, "result": _settle(sb, session, key, _reject(
            key, "position_closed", "that position is already closed",
            position_id=pos["position_id"], requested_at=requested_at,
            confirmed_at=_now_iso()))}

    # the stop derives from the position's ACTUAL entry, never a value
    # supplied by the caller
    entry = float(pos["entry_price"])
    try:
        sb.table("demo_positions").update(
            {"sl": entry, "last_seen_at": _now_iso()}) \
            .eq("position_id", pos["position_id"]) \
            .eq("demo_session", session).execute()
    except Exception:  # noqa: BLE001
        return {"ok": True, "result": _result(
            "error", key, reason="simulator_unavailable",
            comment="the demo desk is not responding",
            requested_at=requested_at, confirmed_at=_now_iso())}

    return {"ok": True, "result": _settle(sb, session, key, _result(
        "modified", key, position_id=pos["position_id"],
        symbol=pos["symbol"], side=pos["side"],
        volume=float(pos["volume"]), fill_price=entry, sl=entry,
        tp=pos.get("tp"), comment="stop moved to entry",
        requested_at=requested_at, confirmed_at=_now_iso()))}


@router.post("/trade/close")
async def trade_close(request: Request) -> dict:
    body = await _verify(request)
    sb = get_supabase()
    session = _session(body)
    key = str(body.get("idempotency_key") or "")
    prior = _claim(sb, session, key, "close")
    if prior is not None:
        return {"ok": True, "result": prior}

    requested_at = _now_iso()
    pos = _position(sb, session, body.get("position_id"))
    if not pos:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown_position")

    # A second close is not a second economic event. It reports the state
    # calmly rather than pretending to close something already closed.
    if pos.get("state") != "open":
        return {"ok": True, "result": _settle(sb, session, key, _result(
            "closed", key, position_id=pos["position_id"],
            symbol=pos["symbol"], side=pos["side"],
            volume=float(pos["volume"]),
            fill_price=pos.get("close_price"),
            comment="position was already closed",
            requested_at=requested_at, confirmed_at=_now_iso()))}

    spec = SYMBOLS.get(pos["symbol"], {"digits": 5})
    close_px = _price(pos["symbol"])
    try:
        sb.table("demo_positions").update(
            {"state": "closed", "close_price": close_px,
             "closed_at": _now_iso(), "last_seen_at": _now_iso()}) \
            .eq("position_id", pos["position_id"]) \
            .eq("demo_session", session).eq("state", "open").execute()
    except Exception:  # noqa: BLE001
        return {"ok": True, "result": _result(
            "error", key, reason="simulator_unavailable",
            comment="the demo desk is not responding",
            requested_at=requested_at, confirmed_at=_now_iso())}

    # No P/L anywhere, including here, where it would be trivial to add.
    return {"ok": True, "result": _settle(sb, session, key, _result(
        "closed", key, position_id=pos["position_id"],
        symbol=pos["symbol"], side=pos["side"], volume=float(pos["volume"]),
        fill_price=close_px, sl=pos.get("sl"), tp=pos.get("tp"),
        comment="simulated close", requested_at=requested_at,
        confirmed_at=_now_iso()))}


@router.post("/positions")
async def positions(request: Request) -> dict:
    """A list, not a command result — deliberately not the §4 shape.

    An unreachable backend raises rather than returning [], because an
    empty array reads to the prospect as "your trade vanished".
    """
    body = await _verify(request)
    sb = get_supabase()
    session = _session(body)
    try:
        rows = (sb.table("demo_positions").select("*")
                .eq("demo_session", session).eq("state", "open")
                .order("opened_at").execute()).data or []
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "simulator_unavailable") from exc
    return {"ok": True, "backend": "sim", "positions": [
        {"position_id": r["position_id"], "symbol": r["symbol"],
         "side": r["side"], "volume": float(r["volume"]),
         "entry_price": float(r["entry_price"]),
         "sl": r.get("sl"), "tp": r.get("tp"),
         "opened_at": r["opened_at"]} for r in rows]}


@router.post("/reset")
async def reset(request: Request) -> dict:
    """Admin-initiated, session-scoped, safe on an unknown session."""
    body = await _verify(request)
    sb = get_supabase()
    session = _session(body)
    cleared = 0
    try:
        rows = (sb.table("demo_positions").select("position_id")
                .eq("demo_session", session).execute()).data or []
        cleared = len(rows)
        sb.table("demo_positions").delete() \
            .eq("demo_session", session).execute()
        sb.table("demo_commands").delete() \
            .eq("demo_session", session).execute()
        sb.table("demo_signals").delete() \
            .eq("demo_session", session).execute()
    except Exception:
        pass
    return {"ok": True, "cleared": cleared}


# ── signals: Option B — SKLZ composes AND delivers ──────────────────
@router.post("/signal/send")
async def signal_send(request: Request) -> dict:
    """Compose from the confirmed position, validate, then deliver.

    Never returns "sent" before Telegram has accepted the request:
    Telegram answers HTTP 200 with ok:false when it refuses, so the body
    is checked rather than the status code.
    """
    body = await _verify(request)
    sb = get_supabase()
    session = _session(body)
    key = str(body.get("idempotency_key") or "")
    prior = _claim(sb, session, key, "signal")
    if prior is not None:
        return {"ok": True, "signal": prior}

    ok_rate, retry = _rate_ok(sb, session, "signal")
    if not ok_rate:
        raise _rate_limited(retry)

    pos = _position(sb, session, body.get("position_id"))
    if not pos:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown_position")

    lang = str(body.get("lang") or "en").lower()[:2]
    brand = str(body.get("brand") or "")[:40]
    text = compose_signal(pos, lang, brand)

    # policy runs on the FINAL RENDERED STRING, after brand and prices
    allowed, why = policy.validate(text)
    sig_id = "SIG-" + secrets.token_hex(6)
    if not allowed:
        rec = {"signal_id": sig_id, "state": "blocked", "reason": why,
               "text": "", "destination_label": None,
               "delivered_at": None, "message_id": None}
        _store_signal(sb, session, sig_id, pos, lang, text, "blocked", why)
        return {"ok": True, "signal": _settle(sb, session, key, rec)}

    dests = resolve_destinations(RoutingScope(purpose="demo_signal",
                                              language=lang))
    dest = dests[0] if dests else None
    label = (dest.meta.get("label") if dest else None) or "SKLZ Demo Signals"

    if not dest or not dest.enabled:
        rec = {"signal_id": sig_id, "state": "disabled",
               "reason": "delivery_disabled", "text": text,
               "destination_label": label, "delivered_at": None,
               "message_id": None}
        _store_signal(sb, session, sig_id, pos, lang, text, "disabled",
                      "delivery_disabled")
        return {"ok": True, "signal": _settle(sb, session, key, rec)}

    # Independent of routing: a demo signal may only ever be delivered to
    # the demo destination. Routing already guarantees that — and it
    # guaranteed it before too, right up until a branch order let an
    # Arabic demo signal resolve to a production channel. Two locks.
    if dest.key != "demo_signals":
        rec = {"signal_id": sig_id, "state": "disabled",
               "reason": "wrong_destination", "text": text,
               "destination_label": label, "delivered_at": None,
               "message_id": None}
        print(f"[demo] REFUSED delivery: resolver returned '{dest.key}', "
              f"which is not the demo destination")
        _store_signal(sb, session, sig_id, pos, lang, text, "disabled",
                      "wrong_destination")
        return {"ok": True, "signal": _settle(sb, session, key, rec)}

    state, reason, msg_id = _deliver(dest, text)
    delivered = _now_iso() if state == "sent" else None
    _store_signal(sb, session, sig_id, pos, lang, text, state, reason,
                  msg_id, label, delivered)
    rec = {"signal_id": sig_id, "state": state, "reason": reason or None,
           "text": text, "destination_label": label,
           "delivered_at": delivered, "message_id": msg_id}
    return {"ok": True, "signal": _settle(sb, session, key, rec)}


def _deliver(dest, text: str) -> tuple[str, str, int | None]:
    """Telegram returns 200 with ok:false when it refuses. Check the body."""
    import urllib.request
    url = f"https://api.telegram.org/bot{dest.token.reveal()}/sendMessage"
    payload = {"chat_id": dest.chat_id, "text": text,
               "disable_web_page_preview": True}
    try:
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=8) as r:
            d = json.loads(r.read().decode())
    except Exception as exc:  # noqa: BLE001
        return "failed", f"telegram_unavailable:{type(exc).__name__}", None
    if not d.get("ok"):
        return "failed", str(d.get("description", "telegram_refused"))[:120], None
    return "sent", "", (d.get("result") or {}).get("message_id")


def _store_signal(sb, session, sig_id, pos, lang, text, state,
                  reason="", msg_id=None, label=None, delivered=None) -> None:
    try:
        sb.table("demo_signals").insert({
            "signal_id": sig_id, "demo_session": session,
            "position_id": pos.get("position_id"), "lang": lang,
            "text": text, "state": state, "reason": reason or None,
            "message_id": msg_id, "destination_label": label,
            "delivered_at": delivered}).execute()
    except Exception:
        pass


@router.post("/signal/state")
async def signal_state(request: Request) -> dict:
    body = await _verify(request)
    sb = get_supabase()
    session = _session(body)
    sig_id = str(body.get("signal_id") or "")
    try:
        rows = (sb.table("demo_signals").select("*")
                .eq("demo_session", session)
                .eq("signal_id", sig_id).limit(1).execute()).data or []
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "simulator_unavailable") from exc
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown_signal")
    r = rows[0]
    return {"ok": True, "state": r["state"],
            "delivered_at": r.get("delivered_at"),
            "message_id": r.get("message_id"),
            "reason": r.get("reason")}
