"""Tests for the copy engine. Every guard must fail closed."""
from engine import (
    CopyDecision, FollowerConfig, FollowerState, LeaderTrade,
    client_order_id, decide, portfolio_health, risk_fraction,
)

MARKET = {"min_amount": 0.0001, "min_cost": 10.0,
          "amount_precision": 6, "active": True}


def cfg(**kw):
    base = dict(follower_id="f1", leader_id="l1", allocation=1000.0,
                risk_level="medium")
    base.update(kw)
    return FollowerConfig(**base)


def state(**kw):
    base = dict(free_quote=1000.0)
    base.update(kw)
    return FollowerState(**base)


def trade(**kw):
    base = dict(trade_id="t1", symbol="BTC/USDT", side="buy",
                leader_notional=1000.0, leader_equity=10000.0)
    base.update(kw)
    return LeaderTrade(**base)


# ───────────────────────── sizing ─────────────────────────
def test_risk_levels_are_ordered():
    assert risk_fraction(cfg(risk_level="low")) < risk_fraction(cfg(risk_level="medium"))
    assert risk_fraction(cfg(risk_level="medium")) < risk_fraction(cfg(risk_level="high"))


def test_custom_risk_is_capped():
    c = cfg(risk_level="custom", custom_risk_pct=0.95)
    assert risk_fraction(c) <= 0.35


def test_mirrors_leader_weight_when_smaller_than_risk_cap():
    # leader risked 5% of equity; follower medium cap is 10% -> use 5%
    d = decide(trade(leader_notional=500, leader_equity=10000),
               cfg(), state(), MARKET, price=50000)
    assert d.copy
    assert abs(d.notional - 50.0) < 1e-6          # 5% of 1000


def test_caps_when_leader_risks_more_than_follower_allows():
    d = decide(trade(leader_notional=5000, leader_equity=10000),   # 50%
               cfg(risk_level="low"), state(), MARKET, price=50000)
    assert d.copy
    assert abs(d.notional - 50.0) < 1e-6          # low = 5% of 1000
    assert any("capped" in w for w in d.warnings)


# ───────────────────────── guards ─────────────────────────
def test_paused_blocks():
    assert decide(trade(), cfg(paused=True), state(), MARKET, 50000).copy is False


def test_emergency_stop_blocks():
    d = decide(trade(), cfg(), state(emergency_stopped=True), MARKET, 50000)
    assert d.copy is False and "emergency" in d.reason


def test_blacklist_blocks():
    d = decide(trade(), cfg(blacklist=["BTC"]), state(), MARKET, 50000)
    assert d.copy is False and "blacklist" in d.reason


def test_whitelist_excludes_others():
    d = decide(trade(), cfg(whitelist=["ETH"]), state(), MARKET, 50000)
    assert d.copy is False and "whitelist" in d.reason


def test_daily_loss_limit_stops_trading():
    d = decide(trade(), cfg(), state(realized_pnl_today=-100.0), MARKET, 50000)
    assert d.copy is False and "daily loss" in d.reason


def test_max_open_positions():
    d = decide(trade(), cfg(max_open_positions=3),
               state(open_positions=3), MARKET, 50000)
    assert d.copy is False and "max open positions" in d.reason


def test_concentration_limit_reduces_size():
    # already 250 in BTC, cap is 30% of 1000 = 300 -> only 50 more allowed
    d = decide(trade(leader_notional=1000, leader_equity=10000),
               cfg(), state(exposure_by_asset={"BTC": 250.0}), MARKET, 50000)
    assert d.copy
    assert abs(d.notional - 50.0) < 1e-6
    assert any("concentration" in w for w in d.warnings)


def test_concentration_limit_blocks_when_full():
    d = decide(trade(), cfg(), state(exposure_by_asset={"BTC": 300.0}),
               MARKET, 50000)
    assert d.copy is False and "exposure already at limit" in d.reason


def test_never_exceeds_allocation():
    # 900 already deployed across assets -> only 100 of room left
    st = state(exposure_by_asset={"ETH": 500.0, "SOL": 400.0}, free_quote=5000.0)
    d = decide(trade(leader_notional=10000, leader_equity=10000),
               cfg(), st, MARKET, 50000)
    assert d.copy
    assert d.notional <= 100.0 + 1e-9


def test_limited_by_actual_cash():
    d = decide(trade(), cfg(), state(free_quote=25.0), MARKET, 50000)
    assert d.copy
    assert d.notional <= 25.0
    assert any("available" in w for w in d.warnings)


# ───────────────────────── exchange minimums ─────────────────────────
def test_below_min_cost_is_rejected():
    d = decide(trade(leader_notional=50, leader_equity=100000),  # tiny weight
               cfg(allocation=100.0), state(free_quote=100.0), MARKET, 50000)
    assert d.copy is False and "minimum" in d.reason


def test_inactive_market_rejected():
    m = dict(MARKET, active=False)
    d = decide(trade(), cfg(), state(), m, 50000)
    assert d.copy is False and "not active" in d.reason


# ───────────────────────── sells ─────────────────────────
def test_sell_closes_held_position():
    d = decide(trade(side="sell"), cfg(),
               state(exposure_by_asset={"BTC": 200.0}), MARKET, 50000)
    assert d.copy and abs(d.notional - 200.0) < 1e-6


def test_sell_without_position_is_skipped():
    d = decide(trade(side="sell"), cfg(), state(), MARKET, 50000)
    assert d.copy is False and "no BTC position" in d.reason


# ───────────────────────── idempotency ─────────────────────────
def test_order_id_is_deterministic():
    a = client_order_id(trade(), cfg())
    b = client_order_id(trade(), cfg())
    assert a == b and a.startswith("sklz")


def test_order_id_differs_per_follower_and_trade():
    a = client_order_id(trade(), cfg(follower_id="f1"))
    b = client_order_id(trade(), cfg(follower_id="f2"))
    c = client_order_id(trade(trade_id="t2"), cfg(follower_id="f1"))
    assert len({a, b, c}) == 3


# ───────────────────────── health ─────────────────────────
def test_portfolio_health_flags():
    h = portfolio_health(cfg(), state(exposure_by_asset={"BTC": 950.0},
                                      realized_pnl_today=-80.0))
    assert h["deployed"] == 950.0
    assert "allocation almost fully deployed" in h["flags"]
    assert "approaching daily loss limit" in h["flags"]
