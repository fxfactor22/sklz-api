"""The incident, turned into tests.

Two real BTC/ETH trades opened at the broker while the Signal Desk said
they had not been confirmed and the channel stayed silent. Nothing had
gone wrong at MT5, in the Runner, or in the database: publication was
reachable only from the prospect's browser poll, and the browser had
stopped polling.

These tests run the real functions. Cases are lettered to match the fix
brief, so a failure names the guarantee it broke.
"""
import asyncio
import json
import os
import sys
import types

import pytest

pytest.importorskip("fastapi")
sys.path.insert(0, ".")

os.environ.setdefault("SUPABASE_URL", "https://example.invalid")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "x")
os.environ["SKLZ_LIVE_DEMO_ENABLED"] = "1"
os.environ["SKLZ_DEMO_MT5_LOGIN"] = "52952532"
os.environ["TG_DEMO_CHAT"] = "-1004489542294"
os.environ["TG_DEMO_ENABLED"] = "1"
os.environ["TG_DEMO_TOKEN"] = "1234567:TEST-TOKEN-NOT-REAL"

for _name in ("supabase",):
    try:
        __import__(_name)
    except ImportError:
        _m = types.ModuleType(_name)
        _m.Client = object
        _m.create_client = lambda *a, **k: None
        sys.modules[_name] = _m

import orders_api as oa
import bot_ingest as bi

TOK = "a" * 32


# ── a Supabase double with real row identity and real UPDATE semantics ──
class _Q:
    """One query being built. Filters are remembered and then applied."""

    def __init__(self, db, table):
        self.db, self.table = db, table
        self.f, self.nulls, self.ins = [], [], []
        self._op = None
        self._patch = None
        self._row = None

    # -- filters
    def select(self, *a, **k):
        self._op = self._op or "select"; return self

    def eq(self, col, val):
        self.f.append((col, val)); return self

    def is_(self, col, _null):
        self.nulls.append(col); return self

    def in_(self, col, vals):
        self.ins.append((col, list(vals))); return self

    def lt(self, *a, **k): return self
    def gte(self, *a, **k): return self
    def limit(self, *a, **k): return self
    def order(self, *a, **k): return self

    # -- writes
    def insert(self, row):
        self._op, self._row = "insert", dict(row); return self

    def update(self, patch):
        self._op, self._patch = "update", dict(patch); return self

    # -- run
    def _match(self, r):
        for col, val in self.f:
            if r.get(col) != val:
                return False
        for col in self.nulls:
            if r.get(col) is not None:
                return False
        for col, vals in self.ins:
            if r.get(col) not in vals:
                return False
        return True

    def execute(self):
        rows = self.db.setdefault(self.table, [])
        if self._op == "insert":
            row = dict(self._row)
            row.setdefault("command_id", "gen-%d" % (len(rows) + 1))
            row.setdefault("created_at", "2026-09-13T09:00:0%d+00:00"
                           % min(len(rows), 9))
            rows.append(row)
            return types.SimpleNamespace(data=[row])
        hit = [r for r in rows if self._match(r)]
        if self._op == "update":
            for r in hit:
                r.update(self._patch)
            # PostgREST returns the rows it changed — the atomic-claim
            # pattern in get_command has depended on that since A3.
            return types.SimpleNamespace(data=[dict(r) for r in hit])
        return types.SimpleNamespace(data=[dict(r) for r in hit])


class SB:
    def __init__(self, db):
        self.db = db

    def table(self, name):
        return _Q(self.db, name)


BTC_CMD = "11111111-1111-4111-8111-111111111111"
ETH_CMD = "22222222-2222-4222-8222-222222222222"
FX_CMD  = "33333333-3333-4333-8333-333333333333"


def market_row(cid=BTC_CMD, symbol="BTCUSD", ticket=1928269073,
               price=76828.78, lots=0.01, status="succeeded", **kw):
    row = {"command_id": cid, "status": status, "ticket": ticket,
           "fill_price": price, "retcode": 10009,
           "broker_comment": "Request executed",
           "symbol": symbol, "resolved_symbol": symbol, "side": "buy",
           "lots": lots, "filled_volume": lots, "actual_account": "52952532",
           "demo_token": TOK, "demo_kind": "market", "bot_name": "sklz-demo",
           "executed_at": "2026-09-13T08:46:55+00:00",
           "created_at": "2026-09-13T08:46:40+00:00",
           "broker_confirmed_at": "2026-09-13T08:46:55+00:00",
           "runner_received_at": "2026-09-13T08:46:50+00:00",
           "demo_tg_message_id": None, "demo_tg_sent_at": None,
           "demo_close_command_id": None}
    row.update(kw)
    return row


def fresh_db(rows=None):
    return {"bot_orders": list(rows or []),
            "demo_links": [{"token": TOK, "provider_name": "Salim FX",
                            "telegram_channel": "Salim FX Signals",
                            "language": "en", "logo_url": "",
                            "contact_name": "",
                            "purpose": "private_demo", "revoked": False,
                            "opened_count": 0, "first_opened_at": None,
                            "created_at": (oa.datetime.now(oa.timezone.utc)
                                           - oa.timedelta(hours=1)
                                           ).isoformat(),
                            "expires_at": (oa.datetime.now(oa.timezone.utc)
                                           + oa.timedelta(hours=47)
                                           ).isoformat()}],
            "bot_state": [{"bot_name": "sklz-demo",
                           "last_account": "52952532",
                           "last_server": "ICMarketsSC-Demo",
                           "account_seen_at": oa.datetime.now(
                               oa.timezone.utc).isoformat()}]}


class _Resp:
    def __init__(self, body): self._b = body
    def read(self): return self._b
    def __enter__(self): return self
    def __exit__(self, *a): return False


@pytest.fixture
def telegram(monkeypatch):
    """Intercept the transport only. Everything above it is real."""
    sent = []

    def fake_urlopen(req, timeout=None):
        sent.append(json.loads(req.data.decode()))
        return _Resp(b'{"ok":true,"result":{"message_id":%d}}'
                     % (4000 + len(sent)))

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return sent


def ack(sb, cid, ticket=1928269073, symbol="BTCUSD", price=76828.78):
    """A Runner acknowledgement, through the real endpoint."""
    body = bi.ResultIn(command_id=cid, ok=True, state="succeeded",
                       ticket=ticket, resolved_symbol=symbol,
                       fill_price=price, filled_volume=0.01, retcode=10009,
                       broker_comment="Request executed",
                       account="52952532", server="ICMarketsSC-Demo",
                       runner_id="runner-1",
                       runner_received_at="2026-09-13T08:46:50+00:00",
                       mt5_requested_at="2026-09-13T08:46:52+00:00",
                       broker_confirmed_at="2026-09-13T08:46:55+00:00")
    return asyncio.run(bi.post_result(body, sb=sb))


# ══════════════════════════════ A ══════════════════════════════
def test_A_ack_publishes_without_any_browser_poll(telegram):
    """The whole incident in one assertion: no page, still published."""
    db = fresh_db([market_row(status="dispatched", ticket=None,
                              demo_tg_message_id=None)])
    sb = SB(db)
    out = ack(sb, BTC_CMD)
    assert out["status"] == "succeeded"
    assert len(telegram) == 1, telegram
    assert telegram[0]["chat_id"] in ("-1004489542294", "@sklzlabsdemo")
    assert "BTCUSD" in telegram[0]["text"]
    assert "1928269073" in telegram[0]["text"]


# ══════════════════════════════ B ══════════════════════════════
def test_B_ack_stores_the_message_id(telegram):
    db = fresh_db([market_row(status="dispatched", ticket=None)])
    sb = SB(db)
    out = ack(sb, BTC_CMD)
    row = db["bot_orders"][0]
    assert row["demo_tg_message_id"] == 4001
    assert row["demo_tg_sent_at"]
    assert out["published"]["message_id"] == 4001


# ══════════════════════════════ C ══════════════════════════════
def test_C_a_later_browser_poll_sends_nothing_new(telegram):
    db = fresh_db([market_row(status="dispatched", ticket=None)])
    sb = SB(db)
    ack(sb, BTC_CMD)
    assert len(telegram) == 1
    # the page arrives afterwards and calls the very same function
    again = oa._deliver_demo_signal(sb, db["bot_orders"][0], "Salim FX")
    assert len(telegram) == 1, "the poll posted a second copy"
    assert again["replay"] is True
    assert again["message_id"] == 4001


# ══════════════════════════════ D ══════════════════════════════
def test_D_ack_and_poll_racing_produce_one_message(telegram):
    """Both callers see an unpublished row; only one may send."""
    db = fresh_db([market_row()])
    sb = SB(db)
    stale = dict(db["bot_orders"][0])        # the poll's own stale copy

    first = oa.publish_demo_open(sb, BTC_CMD)
    second = oa._deliver_demo_signal(sb, stale, "Salim FX")

    assert len(telegram) == 1, telegram
    assert first.get("message_id") == 4001
    assert second.get("message_id") is None or second.get("replay") is True
    assert db["bot_orders"][0]["demo_tg_message_id"] == 4001


def test_D2_a_duplicate_runner_ack_sends_nothing(telegram):
    db = fresh_db([market_row(status="dispatched", ticket=None)])
    sb = SB(db)
    ack(sb, BTC_CMD)
    out = ack(sb, BTC_CMD)                 # the Runner re-sends
    assert out["duplicate"] is True
    assert len(telegram) == 1, telegram


def test_D3_a_dead_claim_is_taken_over_not_lost(telegram):
    """A process that died mid-send must not silence the signal."""
    old = (oa.datetime.now(oa.timezone.utc)
           - oa.timedelta(seconds=oa.CLAIM_STALE_SECONDS + 60)).isoformat()
    db = fresh_db([market_row(demo_tg_sent_at=old)])
    sb = SB(db)
    out = oa.publish_demo_open(sb, BTC_CMD)
    assert out.get("message_id") == 4001, out
    assert len(telegram) == 1


def test_D4_a_live_claim_is_respected(telegram):
    now = oa.datetime.now(oa.timezone.utc).isoformat()
    db = fresh_db([market_row(demo_tg_sent_at=now)])
    sb = SB(db)
    out = oa.publish_demo_open(sb, BTC_CMD)
    assert telegram == []
    assert out.get("pending") is True


# ══════════════════════════════ E ══════════════════════════════
def test_E_a_delivery_that_raises_cannot_produce_an_unbound_error():
    """The handler that exists to swallow a fault must not raise itself."""
    src = open("./orders_api.py").read()
    fn = src[src.index("async def demo_run_state("):]
    fn = fn[:fn.index("# ── auto-close and real demo Telegram")]
    body = fn[:fn.index("except Exception as exc:")]
    handler = fn[fn.index("except Exception as exc:"):]
    assert "tg: dict = {}" in body, "tg must be bound before the attempt"
    # bound immediately before the delivery attempt, not merely somewhere
    assert "tg: dict = {}\n      try:" in body
    assert "tg.get(" not in handler, \
        "the handler must not read a name the try may never have bound"
    assert "telegram_latency_ms" not in handler, \
        "success-only latency belongs on the success path"
    assert "trading_unaffected" in handler


def test_E2_the_broker_result_survives_a_delivery_explosion(monkeypatch,
                                                            telegram):
    """A poll whose delivery blows up still returns the fill."""
    db = fresh_db([market_row()])
    sb = SB(db)

    def boom(*a, **k):
        raise RuntimeError("telegram exploded")

    monkeypatch.setattr(oa, "_deliver_demo_signal", boom)
    out = asyncio.run(oa.demo_run_state(TOK, BTC_CMD, sb=sb))
    assert out["state"] == "succeeded"
    assert out["ticket"] == 1928269073
    assert out["fill_price"] == 76828.78
    assert out["retcode"] == 10009
    assert out["telegram"]["trading_unaffected"] is True
    assert "RuntimeError" in out["telegram"]["error"]


# ══════════════════════════════ F ══════════════════════════════
def test_F_telegram_refusal_leaves_the_execution_succeeded(monkeypatch):
    """Telegram says no; the broker still said yes."""
    db = fresh_db([market_row()])
    sb = SB(db)

    def refused(req, timeout=None):
        return _Resp(b'{"ok":false,"description":"chat not found"}')

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", refused)
    out = asyncio.run(oa.demo_run_state(TOK, BTC_CMD, sb=sb))
    assert out["state"] == "succeeded" and out["ticket"] == 1928269073
    assert "chat not found" in out["telegram"]["error"]
    # and the claim was released, so it can be retried rather than lost
    assert db["bot_orders"][0]["demo_tg_sent_at"] is None
    assert db["bot_orders"][0]["demo_tg_message_id"] is None


# ══════════════════════════════ G / H — the desk's own contract ═════
DESK = open("../site/demo/signal-desk.html").read() \
    if os.path.exists("../site/demo/signal-desk.html") else ""


@pytest.mark.skipif(not DESK, reason="site checkout not beside the api")
def test_G_the_poll_checks_the_response_before_reading_it():
    fn = DESK[DESK.index("async function poll(cid)"):]
    fn = fn[:fn.index("async function runFlow(")]
    assert "if(!r.ok)" in fn, "a 5xx must not be read as a state"
    assert "desk.deskUnreadable" in fn
    # and it must not be reported as a refusal
    assert "faults" in fn


@pytest.mark.skipif(not DESK, reason="site checkout not beside the api")
def test_H_a_slow_confirmation_is_never_called_a_failure():
    fn = DESK[DESK.index("async function poll(cid)"):]
    fn = fn[:fn.index("async function runFlow(")]
    assert "desk.stillConfirming" in fn
    assert "desk.stillConfirmingLong" in fn
    assert 'lset(2,"timed out"' not in fn, \
        "running out of patience is not a failed trade"
    assert "POLL_SLOW_TRIES" in fn and "POLL_SLOW_MS" in fn


@pytest.mark.skipif(not DESK, reason="site checkout not beside the api")
def test_G2_the_confirmed_branch_can_reach_everything_it_calls():
    """setMaster lived inside brand hydration and poll() could not see it.

    The first line of the confirmed-fill branch therefore threw
    ReferenceError on EVERY successful trade, runLive caught it, and the
    desk said it could not reach the demo desk while the broker had
    filled the order. It is a top-level declaration now.
    """
    assert "\nfunction setMaster(" in DESK, \
        "setMaster must be declared at the top level, not inside a scope"
    fn = DESK[DESK.index("async function poll(cid)"):]
    fn = fn[:fn.index("async function runFlow(")]
    assert "setMaster(s.symbol, s.fill_price)" in fn
    # exactly one declaration, and not an indented one inside some scope
    assert DESK.count("function setMaster(") == 1
    assert "\n  function setMaster(" not in DESK
    # the same trap for the other two the confirmed branch calls
    for name in ("lset", "refreshPositions"):
        assert "\nfunction %s(" % name in DESK \
            or "\nasync function %s(" % name in DESK, name


@pytest.mark.skipif(not DESK, reason="site checkout not beside the api")
def test_H2_one_click_carries_one_key():
    assert "client_key:actionKey()" in DESK.replace(" ", "")
    assert "sessionStorage" in DESK[DESK.index("const KEY_STORE"):
                                    DESK.index("async function runLive")]
    # cleared only when the broker has answered
    fn = DESK[DESK.index("async function poll(cid)"):]
    fn = fn[:fn.index("async function runFlow(")]
    assert fn.count("clearActionKey()") == 2


# ══════════════════════════════ I / J — retry protection ═══════════
def _control(sb, action="buy", symbol="BTCUSD", key=None):
    body = oa.ControlIn(action=action, symbol=symbol, client_key=key)
    return asyncio.run(oa.demo_control(TOK, body, sb=sb))


def test_I_the_same_click_retried_returns_the_same_command():
    db = fresh_db()
    sb = SB(db)
    first = _control(sb, key="click-1")
    assert first["duplicate"] is False
    before = len(db["bot_orders"])
    again = _control(sb, key="click-1")
    assert again["duplicate"] is True
    assert again["command_id"] == first["command_id"]
    assert len(db["bot_orders"]) == before, "a retry created a second order"


def test_I2_no_key_still_cannot_double_an_in_flight_order():
    """An old cached page, a second tab, a curl — same protection."""
    db = fresh_db()
    sb = SB(db)
    first = _control(sb)
    again = _control(sb)
    assert again["duplicate"] is True
    assert again["command_id"] == first["command_id"]
    assert len(db["bot_orders"]) == 1


def test_I3_a_retry_does_not_spend_a_run_of_the_budget():
    db = fresh_db()
    sb = SB(db)
    for _ in range(4):
        _control(sb, key="click-1")
    assert len(db["bot_orders"]) == 1


def test_J_a_new_intentional_click_is_a_new_command():
    db = fresh_db()
    sb = SB(db)
    first = _control(sb, key="click-1")
    db["bot_orders"][0]["status"] = "succeeded"      # the broker answered
    db["bot_orders"][0]["ticket"] = 1928269073
    second = _control(sb, key="click-2")
    assert second["duplicate"] is False
    assert second["command_id"] != first["command_id"]
    assert len(db["bot_orders"]) == 2


def test_J2_a_different_symbol_is_never_a_duplicate():
    db = fresh_db()
    sb = SB(db)
    a = _control(sb, symbol="BTCUSD", key="click-1")
    b = _control(sb, symbol="ETHUSD", key="click-2")
    assert a["command_id"] != b["command_id"]
    assert len(db["bot_orders"]) == 2


def test_J3_the_command_id_is_derived_here_not_taken_from_the_browser():
    """Two tokens sending the same key must never collide."""
    one = oa._demo_command_id("a" * 32, "click-1")
    two = oa._demo_command_id("b" * 32, "click-1")
    assert one != two
    assert one == oa._demo_command_id("a" * 32, "click-1")   # deterministic
    assert one != "click-1"


# ══════════════════════════════ K / L / M — symbol agnostic ════════
@pytest.mark.parametrize("cid,symbol,ticket,price,lots", [
    (BTC_CMD, "BTCUSD", 1928269073, 76828.78, 0.01),
    (ETH_CMD, "ETHUSD", 1928269333, 2495.72, 0.1),
    (FX_CMD, "EURUSD", 1928000001, 1.08522, 0.05),
])
def test_KLM_every_symbol_publishes_on_the_ack(telegram, cid, symbol,
                                               ticket, price, lots):
    db = fresh_db([market_row(cid=cid, symbol=symbol, ticket=None,
                              price=None, lots=lots, status="dispatched")])
    sb = SB(db)
    out = ack(sb, cid, ticket=ticket, symbol=symbol, price=price)
    assert out["status"] == "succeeded"
    assert len(telegram) == 1, (symbol, telegram)
    text = telegram[0]["text"]
    assert symbol in text
    assert str(ticket) in text
    assert str(price) in text
    assert "broker DEMO account" in text


# ══════════════════════════════ no retro-publication ═══════════════
def test_deployment_cannot_publish_a_historical_row(telegram):
    """The two incident rows are already settled. Nothing may announce
    them now: post_result refuses a settled command before it reaches
    publication, and publish_demo_open scans nothing."""
    db = fresh_db([
        market_row(cid="44444444-4444-4444-8444-444444444444", symbol="BTCUSD", ticket=1928269073),
        market_row(cid="55555555-5555-4555-8555-555555555555", symbol="ETHUSD", ticket=1928269333,
                   price=2495.72, lots=0.1)])
    sb = SB(db)
    for cid in ("44444444-4444-4444-8444-444444444444",
                "55555555-5555-4555-8555-555555555555"):
        out = ack(sb, cid)
        assert out["duplicate"] is True
        assert "published" not in out or out.get("published") is None
    assert telegram == [], "a deploy announced a historical trade"


def test_publish_is_given_one_command_and_never_searches():
    src = open("./orders_api.py").read()
    fn = src[src.index("def publish_demo_open("):]
    fn = fn[:fn.index("def _deliver_demo_signal(")]
    assert 'eq("command_id", command_id)' in fn
    for scan in ("demo_closed_at", "order(", "limit(20", "gte(", "lt("):
        assert scan not in fn, scan


def test_the_ack_never_fails_because_of_telegram(monkeypatch):
    """A Runner reporting a real execution is always told it was recorded."""
    db = fresh_db([market_row(status="dispatched", ticket=None)])
    sb = SB(db)
    monkeypatch.setattr(oa, "_deliver_demo_signal",
                        lambda *a, **k: (_ for _ in ()).throw(
                            RuntimeError("telegram down")))
    out = ack(sb, BTC_CMD)
    assert out["ok"] is True and out["status"] == "succeeded"
    assert db["bot_orders"][0]["ticket"] == 1928269073
