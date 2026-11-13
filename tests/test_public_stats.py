"""Public storefront stats: which account is published, the 30-day context
block, and the track-record page — all from real journaled rows, including
losses. The partner commission fix lives here too because both were found
in the same storefront review."""
import asyncio
import os
import sys
import types
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import public_stats as P  # noqa: E402
import partner  # noqa: E402
import billing  # noqa: E402


class _Q:
    def __init__(self, db, t):
        self.db, self.t, self.f, self.g = db, t, [], []

    def select(self, *a, **k): return self
    def eq(self, c, v): self.f.append((c, v)); return self
    def gte(self, c, v): self.g.append((c, v)); return self
    def limit(self, *a, **k): return self
    def order(self, *a, **k): return self

    def execute(self):
        rows = self.db.get(self.t, [])
        out = [r for r in rows if all(str(r.get(c)) == str(v) for c, v in self.f)]
        for c, v in self.g:
            out = [r for r in out if (r.get(c) or "") >= v]
        return types.SimpleNamespace(data=[dict(r) for r in out])


class SB:
    def __init__(self, db): self.db = db
    def table(self, n): return _Q(self.db, n)


def _iso(dt): return dt.isoformat().replace("+00:00", "Z")


def _db(kind="prop"):
    now = datetime.now(timezone.utc)
    acct = {"id": "acc-1", "label": "Upcomers-Server 1444538", "platform": "MT5",
            "broker": "Upcomers", "server": "Upcomers-Server", "account_no": "1444538",
            "kind": kind, "connected": True, "created_at": _iso(now)}
    trades = []
    # 12 trades over the last 20 days, mixed, including a losing streak
    for i, pnl in enumerate([30, -20, -25, 40, -18, 22, -30, 15, -12, 28, -40, 35]):
        c = now - timedelta(days=19 - i, hours=1)
        trades.append({"id": i, "account_id": "acc-1", "symbol": "XAUUSD",
                       "side": "buy", "pnl": pnl, "source": "bot",
                       "outcome": "win" if pnl > 0 else "loss",
                       "opened_at": _iso(c - timedelta(hours=2)), "closed_at": _iso(c)})
    # one trade today
    trades.append({"id": 99, "account_id": "acc-1", "symbol": "EURUSD", "side": "sell",
                   "pnl": -7.2, "source": "bot", "outcome": "loss",
                   "opened_at": _iso(now - timedelta(hours=3)),
                   "closed_at": _iso(now - timedelta(minutes=5))})
    return {"journal_accounts": [acct], "journal_trades": trades}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("SKLZ_PUBLIC_ACCOUNT_ID", "SKLZ_PUBLIC_ACCOUNT_NO", "SKLZ_PUBLIC_ACCOUNT_SERVER"):
        monkeypatch.delenv(k, raising=False)


def test_nothing_published_says_so():
    d = asyncio.run(P.live_stats(SB(_db())))
    assert d["published"] is False and "No account" in d["message"]


def test_resolves_by_login_number_and_labels_prop_as_live(monkeypatch):
    monkeypatch.setenv("SKLZ_PUBLIC_ACCOUNT_NO", "1444538")
    d = asyncio.run(P.live_stats(SB(_db())))
    assert d["published"] is True
    assert d["account"]["mode"] == "LIVE · PROP"
    assert d["has_trades_today"] is True and d["trades"] == 1
    assert d["net_pnl"] == -7.2            # a losing day is reported as a loss
    assert d["track_record_url"] == "/perf.html?c=live"


def test_window_block_counts_losses_and_has_daily_curve(monkeypatch):
    monkeypatch.setenv("SKLZ_PUBLIC_ACCOUNT_ID", "acc-1")
    d = asyncio.run(P.live_stats(SB(_db())))
    w = d["window"]
    assert w["days"] == 30 and w["trades"] == 13
    assert w["wins"] == 6 and w["losses"] == 7
    assert w["net_pnl"] == pytest.approx(17.8)
    assert len(w["curve"]) == 30
    assert w["max_drawdown"] > 0


def test_demo_account_stays_labelled_demo(monkeypatch):
    monkeypatch.setenv("SKLZ_PUBLIC_ACCOUNT_NO", "1444538")
    d = asyncio.run(P.live_stats(SB(_db(kind="demo"))))
    assert d["account"]["mode"] == "DEMO"


def test_track_record_has_monthly_rows_and_rating(monkeypatch):
    monkeypatch.setenv("SKLZ_PUBLIC_ACCOUNT_NO", "1444538")
    d = asyncio.run(P.track_record(SB(_db())))
    assert d["trade_count"] == 13
    assert d["data_source"] == "MT5-tracked"
    assert sum(m["trades"] for m in d["monthly"]) == 13
    assert d["metrics"]["trades"] == 13
    assert d["rating"]["grade"]            # rated or 'Unrated', never missing
    assert d["account"]["mode"] == "LIVE · PROP"


def test_track_record_404_when_unpublished():
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        asyncio.run(P.track_record(SB(_db())))


# ── partner commission: 50% of what actually cleared ──────────────────
@pytest.mark.parametrize("price,expected", [
    (49, 25.0), (39, 20.0), (29, 14.5), (79, 39.5),
    (278, 139.0), (470, 235.0), (758, 379.0), (0, 25.0),
])
def test_commission_is_half_of_cleared_amount(price, expected):
    assert partner._commission_for(price) == expected


@pytest.mark.parametrize("plan,ok", [
    ("SKLZ Core", True), ("SKLZ Plus (Annual)", True), ("SKLZ Pro", True),
    ("Bundle", True), ("Bundle (Founder)", True),
    ("TradeGPT Pro", False), ("Indicator Suite", False),
    ("Signal Desk", False), ("Pro Trader OS", False), ("", False),
])
def test_current_membership_plans_commission(plan, ok):
    assert billing._commissionable_plan(plan) is ok


def test_engine_pulse_is_a_heartbeat_without_money():
    now = datetime.now(timezone.utc)
    P._PULSE_CACHE.update({"at": None, "body": None})
    db = {"bot_sessions": [
        {"bot": "SKLZ Master", "mode": "live", "last_seen": _iso(now - timedelta(seconds=40)),
         "equity": 397.99, "balance": 400.0,
         "stats": {"open_now": 2, "positions_opened": 5, "positions_closed": 3,
                   "signals": 4, "auto": True,
                   "open_positions": [{"ticket": 1, "symbol": "XAUUSD.."}]}},
        {"bot": "other", "mode": "paper", "last_seen": _iso(now - timedelta(days=2)), "stats": {}},
    ]}
    out = asyncio.run(P.engine_pulse(SB(db)))
    assert out["alive"] is True and out["bot"] == "SKLZ Master" and out["mode"] == "live"
    assert out["open_now"] == 2 and out["opened_today"] == 5 and out["setups_armed"] == 4
    assert 30 <= out["age_sec"] <= 60
    txt = str(out)
    assert "397" not in txt and "balance" not in txt and "ticket" not in txt


def test_engine_pulse_dead_when_stale_or_missing():
    now = datetime.now(timezone.utc)
    P._PULSE_CACHE.update({"at": None, "body": None})
    out = asyncio.run(P.engine_pulse(SB({"bot_sessions": []})))
    assert out["alive"] is False and out["bot"] is None
    P._PULSE_CACHE.update({"at": None, "body": None})
    db = {"bot_sessions": [{"bot": "SKLZ Master", "mode": "live",
                            "last_seen": _iso(now - timedelta(minutes=20)), "stats": {"open_now": 1}}]}
    out = asyncio.run(P.engine_pulse(SB(db)))
    assert out["alive"] is False and out["open_now"] == 1


def test_skillsart_import_folds_into_partner_record():
    """Clients become partner_customers, orders become cleared commissions,
    the total is also added to paid_out (settled elsewhere), and a rerun adds nothing."""
    import json, types as _t
    snap = json.load(open("data/skillsart_import.json", encoding="utf-8"))

    class Q:
        def __init__(self, db, t): self.db, self.t, self.f, self.l = db, t, [], None; self._upd = None
        def select(self, *a, **k): return self
        def eq(self, c, v): self.f.append((c, v)); return self
        def like(self, c, v): self.l = (c, v.rstrip("%")); return self
        def limit(self, *a, **k): return self
        def order(self, *a, **k): return self
        def insert(self, row): self.db.setdefault(self.t, []).append(dict(row)); self._ins = row; return self
        def update(self, patch): self._upd = patch; return self
        def execute(self):
            rows = self.db.setdefault(self.t, [])
            if self._upd is not None:
                for r in rows:
                    if all(str(r.get(c)) == str(v) for c, v in self.f): r.update(self._upd)
                return _t.SimpleNamespace(data=[])
            if getattr(self, "_ins", None) is not None:
                return _t.SimpleNamespace(data=[self._ins])
            out = [r for r in rows if all(str(r.get(c)) == str(v) for c, v in self.f)]
            if self.l: out = [r for r in out if str(r.get(self.l[0], "")).startswith(self.l[1])]
            return _t.SimpleNamespace(data=[dict(r) for r in out])
    class SB2:
        def __init__(self): self.db = {"partners": [{"user_id": "u1", "code": "abc", "paid_out": 40.0}]}
        def table(self, n): return Q(self.db, n)

    sb = SB2()
    out = partner.import_legacy(sb, "u1", snap)
    assert out["customers_added"] == 300 and out["orders_added"] == 375
    assert abs(out["commission_added"] - 14679.93) < 0.01
    assert abs(sb.db["partners"][0]["paid_out"] - (40.0 + 14679.93)) < 0.01
    import uuid as _u
    assert all(_u.UUID(c["customer_id"]).version == 5 for c in sb.db["partner_customers"])
    assert all(e["status"] == "cleared" for e in sb.db["partner_commissions"])
    again = partner.import_legacy(sb, "u1", snap)
    assert again["customers_added"] == 0 and again["orders_added"] == 0
    assert len(sb.db["partner_customers"]) == 300 and len(sb.db["partner_commissions"]) == 375
