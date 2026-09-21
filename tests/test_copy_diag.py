"""MT5 copy — the chain names the link that broke.

Master event -> queue -> EA poll -> execute -> report. "Not copying"
used to be six silent failures. Cases are lettered so a failure names
the guarantee it broke.
Run: python3 -m pytest tests/test_copy_diag.py -q
"""
import asyncio
import sys
import time
import types

import pytest
from fastapi import HTTPException

sys.path.insert(0, ".")

import copy_api as C  # noqa: E402

KEY = "signal-key-value"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("SIGNAL_WEBHOOK_KEY", KEY)
    monkeypatch.setenv("BOT_INGEST_KEY", "ingest-key-value")
    monkeypatch.setenv("REAL_MONEY_COPYING", "1")
    C._LAST_POLL.clear()
    C._LAST_POLL_DB.clear()


# ── a Supabase double: tables of rows, filters applied, updates kept ─
class _Q:
    def __init__(self, db, table, fail_update_cols=()):
        self.db, self.t = db, table
        self.f, self.ins, self.gte_, self.lt_ = [], [], [], []
        self._op, self._patch, self._row = "select", None, None
        self._fail = fail_update_cols

    def select(self, *a, **k): return self
    def eq(self, c, v): self.f.append((c, v)); return self
    def in_(self, c, vs): self.ins.append((c, list(vs))); return self
    def gte(self, c, v): self.gte_.append((c, v)); return self
    def lt(self, c, v): self.lt_.append((c, v)); return self
    def limit(self, *a, **k): return self
    def order(self, *a, **k): return self
    def insert(self, row): self._op, self._row = "insert", dict(row); return self
    def update(self, patch): self._op, self._patch = "update", dict(patch); return self

    def _hit(self):
        rows = self.db.setdefault(self.t, [])
        out = []
        for r in rows:
            if any(r.get(c) != v for c, v in self.f):
                continue
            if any(r.get(c) not in vs for c, vs in self.ins):
                continue
            if any((r.get(c) or "") < v for c, v in self.gte_):
                continue
            if any((r.get(c) or "") >= v for c, v in self.lt_):
                continue
            out.append(r)
        return out

    def execute(self):
        if self._op == "insert":
            self._row.setdefault("id", len(self.db.setdefault(self.t, [])) + 1)
            self.db[self.t].append(self._row)
            return types.SimpleNamespace(data=[dict(self._row)])
        if self._op == "update":
            if any(c in self._fail for c in self._patch):
                raise RuntimeError("column does not exist")
            hit = self._hit()
            for r in hit:
                r.update(self._patch)
            self.db.setdefault("_updates", []).append((self.t, dict(self._patch)))
            return types.SimpleNamespace(data=[dict(r) for r in hit])
        return types.SimpleNamespace(data=[dict(r) for r in self._hit()])


class SB:
    def __init__(self, db, fail_update_cols=()):
        self.db, self._fail = db, fail_update_cols

    def table(self, name):
        return _Q(self.db, name, self._fail)


MASTER = "53eb21cd-0000-4000-8000-000000000001"
SLAVE = "aaaaaaaa-0000-4000-8000-000000000001"
COPY_KEY = "sk_copy_" + "x" * 32
NOW = "2026-09-21T10:00:00+00:00"


def base_db(**over):
    db = {
        "copy_masters": [{"id": MASTER, "status": "published",
                          "is_system": True}],
        "copy_slaves": [{"id": SLAVE, "user_id": "u1", "label": "Mex 985995",
                         "broker": "Mexatlantic", "mt5_login": "985995",
                         "enabled": True, "copy_key": COPY_KEY,
                         "created_at": NOW}],
        "copy_configs": [{"id": 1, "slave_id": SLAVE, "master_id": MASTER,
                          "enabled": True, "lot_mode": "fixed",
                          "lot_value": 0.03, "max_lot": 0.03,
                          "max_open": 3, "max_daily_loss_pct": 4.0,
                          "max_spread_pips": 5.0}],
        "copy_events": [], "copy_queue": [], "copied_trades": [],
    }
    db.update(over)
    return db


def _req(bearer=KEY):
    h = {"authorization": f"Bearer {bearer}"} if bearer else {}
    return types.SimpleNamespace(headers=h)


def run_diag(db, bearer=KEY):
    return asyncio.run(C.diag(_req(bearer), SB(db)))


def event(**kw):
    r = {"id": 1, "master_id": MASTER, "event": "open", "symbol": "ETHUSD",
         "lots": 0.5, "sl": 4000.0, "tp": 4400.0, "master_ticket": 555,
         "created_at": NOW}
    r.update(kw)
    return r


def qrow(status="pending", **kw):
    r = {"id": 1, "event_id": 1, "slave_id": SLAVE, "status": status,
         "created_at": NOW, "sent_at": None,
         "instruction": {"event": "open", "symbol": "ETHUSD", "lots": 0.03,
                         "sl": 4000.0, "live": True}}
    r.update(kw)
    return r


# ── A. gate ─────────────────────────────────────────────────────────
def test_a_admin_gate_refuses_wrong_and_missing_key():
    with pytest.raises(HTTPException) as e:
        run_diag(base_db(), bearer="wrong")
    assert e.value.status_code == 401
    with pytest.raises(HTTPException):
        run_diag(base_db(), bearer="")


def test_a_ingest_key_is_not_an_admin_key():
    with pytest.raises(HTTPException):
        run_diag(base_db(), bearer="ingest-key-value")


# ── B. never leaks a copy key ───────────────────────────────────────
def test_b_copy_key_never_in_the_report():
    out = run_diag(base_db())
    assert COPY_KEY not in repr(out)
    assert "sk_copy_" not in repr(out)


# ── C. verdicts name the broken link ────────────────────────────────
def test_c_paused_slave_says_whether_the_ea_is_alive():
    db = base_db()
    db["copy_slaves"][0]["enabled"] = False
    v = run_diag(db)["followers"][0]["verdict"]
    assert v.startswith("PAUSED") and "not polling either" in v
    C._LAST_POLL[SLAVE] = time.time()
    v = run_diag(db)["followers"][0]["verdict"]
    assert v.startswith("PAUSED") and "copying paused" in v


def test_c_events_without_created_at_still_count():
    e = event(); del e["created_at"]; e["at"] = NOW
    out = run_diag(base_db(copy_events=[e]))
    assert out["master_events_24h"] == 1
    assert out["errors"] == []
    stale = event(id=2, created_at="2020-01-01T00:00:00+00:00")
    assert run_diag(base_db(copy_events=[stale]))["master_events_24h"] == 0


def test_c_no_config_row():
    db = base_db(copy_configs=[])
    v = run_diag(db)["followers"][0]["verdict"]
    assert v.startswith("NO SUBSCRIPTION")


def test_c_config_disabled():
    db = base_db()
    db["copy_configs"][0]["enabled"] = False
    assert run_diag(db)["followers"][0]["verdict"].startswith("config DISABLED")


def test_c_ea_never_polled():
    f = run_diag(base_db())["followers"][0]
    assert f["last_poll_seconds_ago"] is None
    assert f["verdict"].startswith("EA NOT POLLING")
    assert "WebRequest" in f["verdict"]


def test_c_ea_offline_uses_persisted_timestamp():
    db = base_db()
    db["copy_slaves"][0]["last_poll_at"] = "2026-01-01T00:00:00+00:00"
    f = run_diag(db)["followers"][0]
    assert f["last_poll_seconds_ago"] > C._POLL_OFFLINE_AFTER
    assert f["verdict"].startswith("EA OFFLINE")


def test_c_ea_alive_but_master_silent():
    C._LAST_POLL[SLAVE] = time.time()
    f = run_diag(base_db())["followers"][0]
    assert f["last_poll_seconds_ago"] <= 1
    assert f["verdict"].startswith("MASTER SILENT")
    assert "SKLZ_COPY_PUBLISH" in f["verdict"]


def test_c_events_but_nothing_queued_for_this_slave():
    C._LAST_POLL[SLAVE] = time.time()
    db = base_db(copy_events=[event()])
    v = run_diag(db)["followers"][0]["verdict"]
    assert v.startswith("EVENTS ARRIVE BUT NOTHING QUEUED")


def test_c_opens_expired_unfetched():
    C._LAST_POLL[SLAVE] = time.time()
    db = base_db(copy_events=[event()], copy_queue=[qrow("expired")])
    v = run_diag(db)["followers"][0]["verdict"]
    assert v.startswith("OPENS EXPIRED UNFETCHED")


def test_c_ea_fetched_but_never_reported():
    C._LAST_POLL[SLAVE] = time.time()
    db = base_db(copy_events=[event()], copy_queue=[qrow("sent", sent_at=NOW)])
    v = run_diag(db)["followers"][0]["verdict"]
    assert v.startswith("EA FETCHED BUT NEVER REPORTED")


def test_c_failed_reports_surface_the_ea_error_text():
    C._LAST_POLL[SLAVE] = time.time()
    db = base_db(copy_events=[event()], copy_queue=[qrow("failed")],
                 copied_trades=[
                     {"id": 1, "slave_id": SLAVE, "at": NOW, "status": "failed",
                      "error": "symbol not found: ETHUSD", "symbol": "ETHUSD"},
                     {"id": 2, "slave_id": SLAVE, "at": NOW, "status": "failed",
                      "error": "symbol not found: ETHUSD", "symbol": "ETHUSD"},
                     {"id": 3, "slave_id": SLAVE, "at": NOW, "status": "failed",
                      "error": "retcode 10019", "symbol": "XAUUSD"}])
    f = run_diag(db)["followers"][0]
    assert f["verdict"].startswith("EA REFUSING")
    assert f["verdict"].count("symbol not found") == 1     # de-duplicated
    assert "retcode 10019" in f["verdict"]
    assert f["last_reports"][0]["error"] == "symbol not found: ETHUSD"


def test_c_healthy():
    C._LAST_POLL[SLAVE] = time.time()
    db = base_db(copy_events=[event()], copy_queue=[qrow("done", sent_at=NOW)],
                 copied_trades=[{"id": 1, "slave_id": SLAVE, "at": NOW,
                                 "status": "done", "error": "",
                                 "symbol": "ETHUSD", "slave_lots": 0.03,
                                 "slave_ticket": 9001}])
    f = run_diag(db)["followers"][0]
    assert f["verdict"].startswith("HEALTHY")
    assert f["queue_24h"] == {"done": 1}
    assert f["config"]["max_lot"] == 0.03
    assert "copy_key" not in f["config"]


def test_c_report_carries_flags_master_and_events():
    out = run_diag(base_db(copy_events=[event()]))
    assert out["flags"] == {"real_money": True, "live": False}
    assert out["master"]["status"] == "published"
    assert out["master_events_24h"] == 1
    assert out["last_master_events"][0]["symbol"] == "ETHUSD"


def test_c_missing_system_master_is_named():
    out = run_diag(base_db(copy_masters=[]))
    assert "MISSING" in out["master"]["status"]


# ── D. the poll is the heartbeat ────────────────────────────────────
def _poll(db, **kw):
    return asyncio.run(C.poll(COPY_KEY, sb=SB(db, **kw)))


def test_d_poll_records_liveness_in_memory_and_persists_once():
    db = base_db()
    _poll(db)
    _poll(db)
    _poll(db)
    assert SLAVE in C._LAST_POLL
    persisted = [u for u in db.get("_updates", []) if "last_poll_at" in u[1]]
    assert len(persisted) == 1            # throttled: three polls, one write
    assert db["copy_slaves"][0]["last_poll_at"]


def test_d_paused_slave_still_counts_as_alive():
    db = base_db()
    db["copy_slaves"][0]["enabled"] = False
    r = _poll(db)
    assert r["note"] == "copying paused"
    assert SLAVE in C._LAST_POLL


def test_d_missing_column_is_tolerated_and_does_not_break_polling(capsys):
    fresh = __import__("datetime").datetime.now(
        __import__("datetime").timezone.utc).isoformat()
    db = base_db(copy_queue=[qrow("pending", created_at=fresh)])
    r = _poll(db, fail_update_cols=("last_poll_at",))
    assert r["instructions"] and r["instructions"][0]["symbol"] == "ETHUSD"
    assert SLAVE in C._LAST_POLL                    # in-memory still set
    assert "P13-copy-last-poll" in capsys.readouterr().out
    # and the failure is not retried on every 2s poll
    _poll(db, fail_update_cols=("last_poll_at",))
    assert "P13" not in capsys.readouterr().out


def test_d_bad_key_records_nothing():
    with pytest.raises(HTTPException):
        asyncio.run(C.poll("sk_copy_" + "n" * 32, sb=SB(base_db())))
    assert not C._LAST_POLL


# ── E. the diagnostic survives the thing it diagnoses ───────────────
class _BrokenTable(SB):
    """copied_trades raises (e.g. a column the query names is absent)."""
    def table(self, name):
        q = super().table(name)
        if name == "copied_trades":
            def boom():
                raise RuntimeError("column copied_trades.at does not exist")
            q.execute = boom
        return q


def test_e_a_failing_query_is_reported_not_fatal():
    C._LAST_POLL[SLAVE] = time.time()
    db = base_db(copy_events=[event()], copy_queue=[qrow("done", sent_at=NOW)])
    out = asyncio.run(C.diag(_req(), _BrokenTable(db)))
    assert out["errors"] and "copied_trades" in out["errors"][0]
    assert "does not exist" in out["errors"][0]
    f = out["followers"][0]
    assert f["queue_24h"] == {"done": 1}          # the rest still ran
    assert f["last_reports"] == []


def test_e_clean_run_has_no_errors():
    assert run_diag(base_db())["errors"] == []
