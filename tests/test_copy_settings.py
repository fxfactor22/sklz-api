"""Copy settings are editable after setup, and a floor cannot exceed
the ceiling. Run: python3 -m pytest tests/test_copy_settings.py -q"""
import asyncio
import sys
import types

import pytest
from fastapi import HTTPException

sys.path.insert(0, ".")

import copy_api as C  # noqa: E402
from tests.test_copy_diag import SB, base_db, SLAVE, MASTER  # noqa: E402

USER = types.SimpleNamespace(id="u1")


def _cfg(**kw):
    body = dict(slave_id=SLAVE, master_id="sklz", lot_mode="fixed",
                lot_value=0.02, min_lot=0.02, max_lot=0.02, max_open=1,
                max_daily_loss_pct=25.0, max_spread_pips=10.0)
    body.update(kw)
    return C.ConfigIn(**body)


def _save(db, **kw):
    return asyncio.run(C.upsert_config(_cfg(**kw), user=USER, sb=_UpsertSB(db)))


class _UpsertSB(SB):
    def table(self, name):
        q = super().table(name)
        if name == "copy_configs":
            def upsert(row, on_conflict=""):
                self.db.setdefault("_upserts", []).append(dict(row))
                q._op = "noop"
                q.execute = lambda: types.SimpleNamespace(data=[row])
                return q
            q.upsert = upsert
        return q


# ── A. min_lot travels to the row; "sklz" resolves to the system master ─
def test_a_min_lot_is_saved_and_master_alias_resolved():
    db = base_db()
    assert _save(db)["ok"]
    row = db["_upserts"][0]
    assert row["min_lot"] == 0.02 and row["max_lot"] == 0.02
    assert row["lot_mode"] == "fixed" and row["lot_value"] == 0.02
    assert row["master_id"] == MASTER and row["enabled"] is True


# ── B. impossible settings are refused, named ───────────────────────
@pytest.mark.parametrize("bad,msg", [
    (dict(min_lot=0.05, max_lot=0.02), "min lot"),
    (dict(min_lot=-1), "min lot"),
    (dict(lot_value=0), "above zero"),
    (dict(max_lot=0), "above zero"),
    (dict(max_open=0), "max open"),
    (dict(lot_mode="yolo"), "lot mode"),
])
def test_b_bad_settings_refused(bad, msg):
    with pytest.raises(HTTPException) as e:
        _save(base_db(), **bad)
    assert e.value.status_code == 400 and msg in e.value.detail


def test_b_not_your_account():
    with pytest.raises(HTTPException) as e:
        asyncio.run(C.upsert_config(_cfg(), user=types.SimpleNamespace(id="u9"),
                                    sb=_UpsertSB(base_db())))
    assert e.value.status_code == 403


# ── C. the dashboard can read the settings back ─────────────────────
def test_c_slaves_response_carries_each_accounts_config():
    out = asyncio.run(C.my_slaves(user=USER, sb=SB(base_db())))
    assert out["slaves"][0]["id"] == SLAVE
    assert out["configs"][SLAVE]["max_lot"] == 0.03
    assert out["configs"][SLAVE]["lot_mode"] == "fixed"


def test_c_no_accounts_no_configs():
    out = asyncio.run(C.my_slaves(user=USER, sb=SB(base_db(copy_slaves=[]))))
    assert out == {"slaves": [], "configs": {}}
