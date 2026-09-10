"""D1 contract conformance. No production trade is possible from here."""
import hashlib, hmac, json, sys, time, importlib
sys.path.insert(0, ".")

SECRET = "demo-secret-for-tests"
import os
os.environ["SKLZ_DEMO_SECRET"] = SECRET
os.environ.setdefault("SUPABASE_URL", "https://example.invalid")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "x")

import policy


def sign(body: str, t=None):
    t = str(int(t or time.time()))
    v1 = hmac.new(SECRET.encode(), (t + ".").encode() + body.encode(),
                  hashlib.sha256).hexdigest()
    return f"t={t},v1={v1}"


# ── signature ────────────────────────────────────────────────────────
def _verify_logic(header, body, secret=SECRET, skew=300):
    """Mirrors demo_api._verify without needing FastAPI/Supabase."""
    if not secret:
        return "bad_signature"
    ts = sig = ""
    for part in header.split(","):
        k, _, v = part.strip().partition("=")
        if k == "t":
            ts = v
        elif k == "v1":
            sig = v
    if not ts or not sig:
        return "bad_signature"
    if abs(time.time() - int(ts)) > skew:
        return "bad_signature"
    expected = hmac.new(secret.encode(), (ts + ".").encode() + body.encode(),
                        hashlib.sha256).hexdigest()
    return "ok" if hmac.compare_digest(expected, sig.lower()) else "bad_signature"


def test_valid_signature_passes():
    b = json.dumps({"demo_session": "a" * 32})
    assert _verify_logic(sign(b), b) == "ok"


def test_tampered_body_fails():
    b = json.dumps({"demo_session": "a" * 32})
    h = sign(b)
    assert _verify_logic(h, b.replace("a" * 32, "b" * 32)) == "bad_signature"


def test_replayed_old_timestamp_fails():
    b = json.dumps({"x": 1})
    old = sign(b, t=time.time() - 400)
    assert _verify_logic(old, b) == "bad_signature"


def test_missing_secret_refuses_rather_than_falling_open():
    b = json.dumps({"x": 1})
    assert _verify_logic(sign(b), b, secret="") == "bad_signature"


def test_wrong_secret_fails():
    b = json.dumps({"x": 1})
    h = hmac.new(b"other", (str(int(time.time())) + ".").encode() + b.encode(),
                 hashlib.sha256).hexdigest()
    assert _verify_logic(f"t={int(time.time())},v1={h}", b) == "bad_signature"


# ── contract shape ───────────────────────────────────────────────────
def test_result_shape_matches_the_contract():
    import demo_sim
    r = demo_sim._result("filled", "key-1", symbol="EURUSD", side="buy",
                         volume=1.0, fill_price=1.0852,
                         comment="simulated fill")
    for field in ("state", "command_id", "position_id", "symbol", "side",
                  "volume", "requested_price", "fill_price", "sl", "tp",
                  "comment", "requested_at", "confirmed_at", "backend",
                  "reason"):
        assert field in r, field
    assert r["backend"] == "sim"
    # absent values are null, never 0 — zero is a valid price
    assert r["sl"] is None and r["requested_price"] is None


def test_backend_is_sim_on_every_result():
    import demo_sim
    for state in ("filled", "modified", "closed", "rejected", "error"):
        assert demo_sim._result(state, "k")["backend"] == "sim"


def test_a_claim_in_a_comment_is_replaced_not_sent():
    import demo_sim
    r = demo_sim._result("filled", "k",
                         comment="filled — desk is running at 82% today")
    assert "82%" not in r["comment"]
    assert r["comment"] == "simulated fill"


def test_rejected_and_error_are_different_states():
    import demo_sim
    assert demo_sim._reject("k", "bad_volume", "too large")["state"] == "rejected"
    assert demo_sim._result("error", "k")["state"] == "error"


def test_every_contract_reason_code_exists():
    src = open("./demo_api.py").read()
    for reason in ("unknown_symbol", "bad_side", "bad_volume",
                   "sl_wrong_side", "tp_wrong_side", "too_many_positions"):
        assert f'"{reason}"' in src, reason


def test_symbol_table_matches_the_contract_request():
    import demo_sim
    for s in ("EURUSD", "GBPUSD", "USDJPY", "XAUUSD",
              "BTCUSD", "ETHUSD", "SOLUSD"):
        assert s in demo_sim.SYMBOLS, s
    assert demo_sim.SYMBOLS["EURUSD"]["digits"] == 5
    assert demo_sim.SYMBOLS["USDJPY"]["digits"] == 3
    assert demo_sim.SYMBOLS["XAUUSD"]["pip"] == 0.1


def test_price_moves_and_stays_plausible():
    import demo_sim
    p1 = demo_sim._price("EURUSD")
    assert 1.05 < p1 < 1.12
    assert demo_sim._price("XAUUSD") > 4000


# ── isolation: the rule this whole file protects ────────────────────
def test_demo_backend_cannot_reach_production_trading():
    src = open("./demo_api.py").read()
    # actual usage, not mentions — the module docstring names these
    # precisely to say it must not touch them
    code = src[src.index('from __future__'):]
    for forbidden in ("import mt5io", "from mt5io", "import copy_api",
                      'table("bot_orders")', 'table("copy_queue")',
                      'table("journal_trades")', 'table("signals")',
                      'table("copied_trades")', "signal_publish"):
        assert forbidden not in code, f"demo can reach {forbidden}"


def test_no_pl_field_anywhere_in_the_result():
    import demo_sim
    r = demo_sim._result("closed", "k", fill_price=1.09)
    for banned in ("profit", "pnl", "pips", "unrealized", "result_pips"):
        assert banned not in r, banned


def test_policy_blocks_performance_but_allows_the_contract_signal():
    SIGNAL = ("⚠️ SIMULATED — demonstration signal, not a live trade\n\n"
              "BUY  EURUSD\nEntry: 1.08522\nStop loss: 1.07000\n"
              "Take profit: 1.09900\nVolume: 1\n\n"
              "Trading leveraged products carries risk. Not financial advice.")
    assert policy.is_clean(SIGNAL)
    assert not policy.is_clean("desk is running at 82% today")
    assert not policy.is_clean("closed +80 pips")
    assert not policy.is_clean("7 out of 10 trades hit target")


def test_position_lookup_is_session_scoped():
    """Cross-session access must not be an error to explain — it must
    simply return nothing."""
    src = open("./demo_api.py").read()
    fn = src[src.index("def _position("):]
    fn = fn[:fn.index("\n@router")]
    assert '.eq("demo_session", session)' in fn
    assert '.eq("position_id"' in fn


# ── signals ──────────────────────────────────────────────────────────
def test_composed_signal_matches_the_contract_example():
    import demo_sim
    pos = {"symbol": "EURUSD", "side": "buy", "volume": 1.0,
           "entry_price": 1.08522, "sl": 1.07, "tp": 1.099}
    t = demo_sim.compose_signal(pos, "en", "Falcon FX")
    assert t.startswith("⚠️ SIMULATED")          # marker INSIDE the message
    assert "Falcon FX" in t
    assert "Entry: 1.08522" in t
    assert "Volume: 1" in t
    assert policy.is_clean(t)
    for banned in ("pips", "profit:", "win", "%"):
        assert banned.lower() not in t.lower().replace("take profit:", "")


def test_signal_is_built_from_the_position_not_the_request():
    import demo_sim
    pos = {"symbol": "EURUSD", "side": "buy", "volume": 1.0,
           "entry_price": 1.08999, "sl": None, "tp": None}
    t = demo_sim.compose_signal(pos)
    assert "1.08999" in t                      # the confirmed fill
    assert "Stop loss: —" in t                 # absent, not invented


def test_arabic_signal_carries_its_own_marker():
    import demo_sim
    pos = {"symbol": "XAUUSD", "side": "sell", "volume": 0.1,
           "entry_price": 4410.25, "sl": 4420.0, "tp": 4390.0}
    t = demo_sim.compose_signal(pos, "ar", "فالكون")
    assert "محاكاة" in t and policy.is_clean(t)


def test_demo_destination_is_disabled_until_two_flags_are_set():
    import os, sys
    sys.path.insert(0, ".")
    import routing
    for k in list(os.environ):
        if k.startswith("TG_"):
            os.environ.pop(k)
    d = routing.resolve_destinations(
        routing.RoutingScope(purpose="demo_signal"))[0]
    assert d.enabled is False
    os.environ["TG_DEMO_CHAT"] = "@sklzlabsdemo"
    assert routing.resolve_destinations(
        routing.RoutingScope(purpose="demo_signal"))[0].enabled is False
    os.environ["TG_DEMO_ENABLED"] = "1"
    assert routing.resolve_destinations(
        routing.RoutingScope(purpose="demo_signal"))[0].enabled is True


def test_browser_supplied_destination_is_impossible():
    """No request field can name a chat. The resolver is the only source."""
    src = open("./demo_api.py").read()
    assert 'body.get("chat_id")' not in src
    assert 'body.get("destination")' not in src
    assert 'body.get("channel")' not in src
    # and a chat_id never leaves in a response
    assert '"chat_id": dest.chat_id' in src      # only into Telegram
    assert '"destination_label"' in src


def test_telegram_200_with_ok_false_is_a_failure():
    src = open("./demo_api.py").read()
    fn = src[src.index("def _deliver("):]
    fn = fn[:fn.index("\ndef ")]
    assert 'if not d.get("ok")' in fn, "a 200 body must be checked"
    assert 'return "failed"' in fn
