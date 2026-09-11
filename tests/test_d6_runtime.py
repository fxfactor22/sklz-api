"""One focused test that EXECUTES the succeeded-order branch.

The D6 bug shipped because every test read source text. This one runs
the real functions, so a missing import fails it with NameError.
"""
import os, sys, types
import pytest

# This test imports the real app module, so it needs the app's deps.
# Skip rather than fail where they are absent (a laptop), run where
# they exist (the container).
pytest.importorskip("fastapi")

sys.path.insert(0, ".")

os.environ.setdefault("SUPABASE_URL", "https://example.invalid")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "x")
os.environ["SKLZ_LIVE_DEMO_ENABLED"] = "1"
os.environ["SKLZ_DEMO_MT5_LOGIN"] = "52952532"
os.environ["TG_DEMO_CHAT"] = "-1004489542294"
os.environ["TG_DEMO_ENABLED"] = "1"
os.environ["TG_DEMO_TOKEN"] = "1234567:TEST-TOKEN-NOT-REAL"

# Stub only what is genuinely absent, so this runs identically in CI and
# in production where the real packages exist.
for _name in ("supabase",):
    try:
        __import__(_name)
    except ImportError:
        _m = types.ModuleType(_name)
        _m.Client = object
        _m.create_client = lambda *a, **k: None
        sys.modules[_name] = _m

import orders_api as oa       # import failure here IS the bug


class _Tbl:
    """Minimal Supabase table double: records writes, returns fixtures."""
    def __init__(self, store, name):
        self.store, self.name, self._rows = store, name, []
    def select(self, *a, **k): return self
    def eq(self, *a, **k): return self
    def lt(self, *a, **k): return self
    def is_(self, *a, **k): return self
    def limit(self, *a, **k): return self
    def order(self, *a, **k): return self
    def insert(self, row):
        self.store.setdefault("inserts", []).append((self.name, row))
        self._rows = [dict(row, command_id="close-cmd-1")]
        return self
    def update(self, patch):
        self.store.setdefault("updates", []).append((self.name, patch))
        self._rows = []
        return self
    def execute(self):
        return types.SimpleNamespace(data=self._rows)


class _SB:
    def __init__(self, store, fixtures):
        self.store, self.fixtures = store, fixtures
    def table(self, name):
        t = _Tbl(self.store, name)
        t._rows = self.fixtures.get(name, [])
        return t


def test_succeeded_branch_executes_without_nameerror(monkeypatch):
    """Composition -> routing -> delivery -> auto-close, for real."""
    store = {}
    filled = {
        "command_id": "cmd-1", "status": "succeeded", "ticket": 1925280358,
        "fill_price": 1.16011, "retcode": 10009,
        "broker_comment": "Request executed", "symbol": "EURUSD",
        "resolved_symbol": "EURUSD", "side": "buy", "lots": 0.01,
        "filled_volume": 0.01, "actual_account": "52952532",
        "demo_token": "a" * 32, "demo_kind": "market",
        "executed_at": "2026-09-11T04:12:00+00:00",
        "created_at": "2026-09-11T04:11:57+00:00",
        "broker_confirmed_at": "2026-09-11T04:12:00+00:00",
        "demo_tg_message_id": None, "demo_close_command_id": None,
    }
    # bot_state must be present or the guard correctly refuses to sweep —
    # which is what the first run of this test proved.
    sb = _SB(store, {
        "bot_orders": [filled],
        "bot_state": [{"bot_name": "sklz-demo", "last_account": "52952532",
                       "last_server": "ICMarketsSC-Demo",
                       "account_seen_at": oa.datetime.now(
                           oa.timezone.utc).isoformat()}]})

    # intercept the transport only — everything above it is real code
    sent = {}

    class _Resp:
        def __init__(self, body): self._b = body
        def read(self): return self._b
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout=None):
        sent["url"] = req.full_url
        sent["body"] = json.loads(req.data.decode())
        return _Resp(b'{"ok":true,"result":{"message_id":4242}}')

    import json
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    # 1 — composition runs
    text = oa._demo_signal_text(filled, "Falcon FX")
    assert "1.16011" in text and "1925280358" in text
    assert "SIMULATED DEMO" in text

    # 2 — routing + policy + delivery run for real
    out = oa._deliver_demo_signal(sb, filled, "Falcon FX")
    assert out.get("message_id") == 4242, out
    assert out["url"] == "https://t.me/c/4489542294/4242", out

    # 3 — the destination was the demo channel and nothing else
    assert sent["body"]["chat_id"] in ("-1004489542294", "@sklzlabsdemo")
    assert "api.telegram.org" in sent["url"]

    # 4 — auto-close scheduling runs and queues a close by exact ticket
    filled["demo_tg_message_id"] = 4242
    queued = oa._sweep_demo_closes(sb)
    closes = [r for (t, r) in store.get("inserts", [])
              if r.get("command_type") == "close"]
    assert queued == 1, store
    assert closes and closes[0]["ticket"] == 1925280358
    assert closes[0]["bot_name"] == "sklz-demo"


def test_sweep_refuses_a_ticket_from_another_account():
    """Executed, not read: the wrong account must queue nothing."""
    store = {}
    wrong = {"command_id": "cmd-2", "ticket": 999, "demo_token": "b" * 32,
             "actual_account": "11111111", "demo_close_command_id": None,
             "executed_at": "2026-09-11T04:12:00+00:00"}
    sb = _SB(store, {"bot_orders": [wrong],
                     "bot_state": [{"bot_name": "sklz-demo",
                                    "last_account": "52952532",
                                    "last_server": "ICMarketsSC-Demo",
                                    "account_seen_at":
                                        oa.datetime.now(
                                            oa.timezone.utc).isoformat()}]})
    assert oa._sweep_demo_closes(sb) == 0
    assert not [r for (t, r) in store.get("inserts", [])
                if r.get("command_type") == "close"]


def test_auto_close_state_comes_from_the_close_row(monkeypatch):
    """The parent row is written once at queue time and never updated,
    so it reported 'queued' while the position had already closed."""
    store = {}
    parent = {"command_id": "cmd-1", "ticket": 1925357543,
              "demo_close_command_id": "close-1",
              "demo_close_state": "queued", "demo_closed_at": None}
    closed = {"status": "succeeded",
              "executed_at": "2026-09-11T04:16:09.158073+00:00",
              "retcode": None, "broker_comment": None,
              "actual_account": "52952532"}
    sb = _SB(store, {"bot_orders": [closed]})

    out = oa._close_state(sb, parent)
    assert out["state"] == "succeeded", out
    assert out["closed_at"].startswith("2026-09-11T04:16:09"), out
    assert out["closed_on_account"] == "52952532"

    # and it writes the truth back so later readers are not misled
    updates = [p for (t, p) in store.get("updates", [])
               if "demo_close_state" in p]
    assert updates and updates[0]["demo_close_state"] == "succeeded"
    assert updates[0]["demo_closed_at"].startswith("2026-09-11T04:16:09")


def test_close_state_without_a_close_command_says_so():
    out = oa._close_state(_SB({}, {}), {"command_id": "x"})
    assert out["state"] == "not_scheduled"


def test_the_status_endpoint_actually_uses_the_close_row():
    """The runtime test above exercises _close_state directly, so it
    cannot see whether the endpoint calls it. This checks the wiring."""
    src = open("./orders_api.py").read()
    fn = src[src.index("async def demo_run_state("):]
    assert 'out["auto_close"] = await offload(_close_state, sb, r)' in fn
    # the stale single-source read must be gone
    assert 'out["auto_close"] = {"state": r.get("demo_close_state")' not in fn
