"""The engine's key is compared in one place, and a refusal says why.

Cases are lettered so a failure names the guarantee it broke.
Run: python3 -m pytest tests/test_keyauth.py -q
"""
import sys

import pytest

sys.path.insert(0, ".")

import keyauth as K  # noqa: E402


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("SIGNAL_WEBHOOK_KEY", "signal-key-value")
    monkeypatch.setenv("BOT_INGEST_KEY", "ingest-key-value")


# ── A. exact matches still work ────────────────────────────────────
def test_a_signal_key_accepted():
    assert K.engine_key_ok("signal-key-value", "t")


def test_a_ingest_key_accepted_where_allowed():
    assert K.engine_key_ok("ingest-key-value", "t")


def test_a_ingest_key_refused_where_only_signal_is_allowed():
    assert not K.engine_key_ok("ingest-key-value", "t",
                               names=("SIGNAL_WEBHOOK_KEY",))


# ── B. whitespace is not a different credential ────────────────────
def test_b_trailing_space_in_railway_value_is_tolerated(monkeypatch):
    """A value pasted into a dashboard can carry a trailing space. The
    engine on the VPS sends the clean key; it must still be accepted."""
    monkeypatch.setenv("SIGNAL_WEBHOOK_KEY", "signal-key-value \n")
    assert K.engine_key_ok("signal-key-value", "t")


def test_b_trailing_space_in_the_sent_key_is_tolerated():
    assert K.engine_key_ok("  signal-key-value\n", "t")


# ── C. refusals name the condition, never the value ────────────────
def test_c_wrong_key_is_refused_and_logged_with_lengths(capsys):
    assert not K.engine_key_ok("wrong-key", "/api/mt5copy/event")
    out = capsys.readouterr().out
    assert "/api/mt5copy/event" in out and "key mismatch" in out
    assert "sent 9 chars" in out
    assert "wrong-key" not in out
    assert "signal-key-value" not in out


def test_c_empty_key_is_refused_and_named(capsys):
    assert not K.engine_key_ok("", "t")
    assert "no key sent" in capsys.readouterr().out


def test_c_unset_server_key_is_refused_and_named(monkeypatch, capsys):
    monkeypatch.delenv("SIGNAL_WEBHOOK_KEY", raising=False)
    monkeypatch.delenv("BOT_INGEST_KEY", raising=False)
    assert not K.engine_key_ok("anything", "t")
    assert "is set" in capsys.readouterr().out


def test_c_whitespace_only_server_key_counts_as_unset(monkeypatch, capsys):
    monkeypatch.setenv("SIGNAL_WEBHOOK_KEY", "   ")
    monkeypatch.delenv("BOT_INGEST_KEY", raising=False)
    assert not K.engine_key_ok("   ", "t")


def test_c_a_successful_match_prints_nothing(capsys):
    K.engine_key_ok("signal-key-value", "t")
    assert capsys.readouterr().out == ""


# ── D. the three routes all go through it ──────────────────────────
def test_d_all_three_gates_use_keyauth():
    for f in ("copy_api.py", "signal_lifecycle.py", "signals_engine.py"):
        assert "from keyauth import engine_key_ok" in open(f).read(), f


def test_d_no_gate_compares_the_raw_env_any_more():
    for f in ("copy_api.py", "signal_lifecycle.py", "signals_engine.py"):
        src = open(f).read()
        assert 'key != expected' not in src, f
        assert 'key in (os.environ.get("SIGNAL_WEBHOOK_KEY"' not in src, f
