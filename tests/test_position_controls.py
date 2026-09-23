"""Position controls from the owner's dashboard: close / breakeven / modify /
positions are queued as bot_orders the Runner already knows how to execute,
owner-only, live Runners only, with a short TTL."""
import asyncio
import os
import sys
import types

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot_ingest as B  # noqa: E402


class _Q:
    def __init__(self, db, t):
        self.db, self.t, self.f, self._op, self._row = db, t, [], "select", None

    def select(self, *a, **k): return self
    def eq(self, c, v): self.f.append((c, v)); return self
    def limit(self, *a, **k): return self
    def order(self, *a, **k): return self
    def insert(self, row): self._op, self._row = "insert", dict(row); return self

    def execute(self):
        rows = self.db.setdefault(self.t, [])
        if self._op == "insert":
            self._row.setdefault("id", len(rows) + 1)
            self._row.setdefault("command_id", f"cmd-{len(rows) + 1}")
            rows.append(self._row)
            return types.SimpleNamespace(data=[dict(self._row)])
        return types.SimpleNamespace(
            data=[dict(r) for r in rows if all(r.get(c) == v for c, v in self.f)])


class SB:
    def __init__(self): self.db = {}
    def table(self, n): return _Q(self.db, n)


OWNER = types.SimpleNamespace(id="u-owner", email="fxfactor24@gmail.com")
ADMIN = types.SimpleNamespace(id="u-admin", email="someone@else.com")


def call(body, user=OWNER, sb=None):
    sb = sb or SB()
    return asyncio.run(B.position_command(B.PositionIn(**body), user, sb)), sb


def test_a_close_is_queued_for_the_runner():
    out, sb = call({"bot_name": "learning-runner", "action": "close", "ticket": 555})
    assert out["ok"] and out["command_id"] == "cmd-1"
    row = sb.db["bot_orders"][0]
    assert (row["command_type"], row["ticket"], row["status"]) == ("close", 555, "pending")
    assert row["expires_at"]                       # a stale close never fires later
    assert row["note"].startswith("[dashboard] close 555")


def test_b_breakeven_carries_no_client_price():
    out, sb = call({"bot_name": "learning-runner", "action": "breakeven",
                    "ticket": 7, "sl": 1.2345})
    row = sb.db["bot_orders"][0]
    assert row["command_type"] == "breakeven" and "sl" not in row
    # the Runner puts the stop at the broker's ACTUAL entry, never ours


def test_c_modify_needs_a_level_and_keeps_zero_as_unchanged():
    out, _ = call({"bot_name": "learning-runner", "action": "modify", "ticket": 7})
    assert not out["ok"] and "stop" in out["reason"]
    out, sb = call({"bot_name": "learning-runner", "action": "modify",
                    "ticket": 7, "sl": 2390.5})
    row = sb.db["bot_orders"][0]
    assert row["command_type"] == "modify" and row["sl"] == 2390.5 and row["tp"] == 0


def test_d_positions_read_needs_no_ticket():
    out, sb = call({"bot_name": "learning-runner", "action": "positions"})
    assert out["ok"] and sb.db["bot_orders"][0]["command_type"] == "positions"
    out, _ = call({"bot_name": "learning-runner", "action": "close"})
    assert not out["ok"] and "ticket" in out["reason"]


def test_e_owner_only_and_never_the_demo_runner(monkeypatch):
    with pytest.raises(HTTPException) as e:
        call({"bot_name": "learning-runner", "action": "close", "ticket": 1}, user=ADMIN)
    assert e.value.status_code == 403
    monkeypatch.setenv("SKLZ_DEMO_BOT_NAME", "sklz-demo")
    out, sb = call({"bot_name": "SKLZ-Demo", "action": "close", "ticket": 1})
    assert not out["ok"] and "demo" in out["reason"] and not sb.db.get("bot_orders")
    out, _ = call({"bot_name": "learning-runner", "action": "explode", "ticket": 1})
    assert not out["ok"]


def test_f_the_runner_poll_hands_the_command_over_with_ticket_and_levels():
    """get_command must pass type/ticket/sl/tp through unchanged."""
    _, sb = call({"bot_name": "learning-runner", "action": "modify",
                  "ticket": 9, "sl": 1.1, "tp": 1.3})
    # a minimal double of the claim-update path
    class Q2(_Q):
        def update(self, patch): self._op, self._patch = "update", dict(patch); return self
        def execute(self):
            if self._op == "update":
                hit = [r for r in self.db["bot_orders"]
                       if all(r.get(c) == v for c, v in self.f)]
                for r in hit: r.update(self._patch)
                return types.SimpleNamespace(data=[dict(r) for r in hit])
            return super().execute()
    class SB2(SB):
        def __init__(self, db): self.db = db
        def table(self, n): return Q2(self.db, n)
    out = asyncio.run(B.get_command("learning-runner", "", "", "r1", None, SB2(sb.db)))
    o = out["orders"][0]
    assert (o["type"], o["ticket"], o["sl"], o["tp"]) == ("modify", 9, 1.1, 1.3)
    assert sb.db["bot_orders"][0]["status"] == "dispatched"


def test_g_command_status_reads_the_runner_answer():
    _, sb = call({"bot_name": "learning-runner", "action": "positions"})
    sb.db["bot_orders"][0].update({"status": "succeeded", "positions": [
        {"ticket": 1, "symbol": "XAUUSD", "side": "buy", "volume": 0.1,
         "entry": 2400.0, "sl": 2390.0, "tp": 0, "profit": 12.5}]})
    out = asyncio.run(B.command_status("cmd-1", OWNER, sb))
    assert out["ok"] and out["state"] == "succeeded"
    assert out["positions"][0]["symbol"] == "XAUUSD"
    assert asyncio.run(B.command_status("nope", OWNER, sb))["ok"] is False
