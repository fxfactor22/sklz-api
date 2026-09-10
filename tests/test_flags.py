"""D1.3 — flags fail closed, and a demo signal cannot reach a live channel."""
import os, sys
sys.path.insert(0, ".")
import envflags, routing


def _clear():
    for k in list(os.environ):
        if k.startswith(("TG_", "TELEGRAM_", "SKLZ_DEMO_")):
            os.environ.pop(k)


def test_every_off_spelling_is_off():
    for value in ("", "0", "false", "FALSE", "False", "no", "NO", "off",
                  "OFF", " 0 ", " false ", "n", "f"):
        os.environ["X_FLAG"] = value
        assert envflags.flag("X_FLAG") is False, repr(value)


def test_every_on_spelling_is_on():
    for value in ("1", "true", "TRUE", "True", "yes", "YES", "on", "ON",
                  " 1 ", " true "):
        os.environ["X_FLAG"] = value
        assert envflags.flag("X_FLAG") is True, repr(value)


def test_unset_is_off():
    os.environ.pop("X_FLAG", None)
    assert envflags.flag("X_FLAG") is False


def test_unknown_value_fails_closed_and_warns():
    warned = []
    os.environ["X_FLAG"] = "maybe"
    assert envflags.flag("X_FLAG", log=warned.append) is False
    assert warned and "not a recognised boolean" in warned[0]
    assert "maybe" in warned[0]


# ── TG_DEMO_ENABLED ──────────────────────────────────────────────────
def _demo_enabled(value=None, lang="en"):
    _clear()
    os.environ["TG_DEMO_CHAT"] = "@sklzlabsdemo"
    os.environ["TELEGRAM_BOT_TOKEN"] = "T"
    if value is not None:
        os.environ["TG_DEMO_ENABLED"] = value
    return routing.resolve_destinations(
        routing.RoutingScope(purpose="demo_signal", language=lang))[0]


def test_demo_delivery_disabled_for_every_off_value():
    for value in (None, "", "0", "false", "FALSE", "no", "off", " 0 "):
        assert _demo_enabled(value).enabled is False, repr(value)


def test_demo_delivery_enabled_only_for_explicit_on():
    for value in ("1", "true", "TRUE", "yes", "on"):
        assert _demo_enabled(value).enabled is True, repr(value)


def test_demo_delivery_disabled_for_a_typo():
    assert _demo_enabled("tru").enabled is False
    assert _demo_enabled("enabled").enabled is False


# ── the leak that actually happened ──────────────────────────────────
def test_an_arabic_demo_signal_cannot_reach_the_production_channel():
    """The D1 defect: language matched before purpose, so an Arabic demo
    signal resolved to @sklzlabsarabic — live, and with no enable flag."""
    _clear()
    os.environ.update({"TG_ARABIC_CHAT": "@sklzlabsarabic",
                       "TG_DEMO_CHAT": "@sklzlabsdemo",
                       "TELEGRAM_BOT_TOKEN": "T",
                       "TG_DEMO_ENABLED": "0"})
    d = routing.resolve_destinations(
        routing.RoutingScope(purpose="demo_signal", language="ar"))[0]
    assert d.key == "demo_signals"
    assert d.chat_id == "@sklzlabsdemo"
    assert "arabic" not in d.chat_id
    assert d.enabled is False


def test_the_production_arabic_channel_still_works():
    _clear()
    os.environ.update({"TG_ARABIC_CHAT": "@sklzlabsarabic",
                       "TELEGRAM_BOT_TOKEN": "T"})
    d = routing.resolve_destinations(
        routing.RoutingScope(language="ar", purpose="content"))[0]
    assert d.key == "arabic" and d.enabled is True


def test_demo_api_refuses_any_non_demo_destination():
    """A second lock, independent of routing."""
    src = open("./demo_api.py").read()
    fn = src[src.index("async def signal_send("):]
    fn = fn[:fn.index("\ndef _deliver")]
    assert 'if dest.key != "demo_signals"' in fn
    assert fn.index('dest.key != "demo_signals"') < fn.index("_deliver(dest")


def test_diagnostic_is_off_for_every_off_value():
    src = open("./demo_api.py").read()
    assert 'envflags.flag("SKLZ_DEMO_DIAG")' in src
    assert 'os.environ.get("SKLZ_DEMO_DIAG"' not in src


def test_no_raw_truthiness_remains_on_a_security_flag():
    for name in ("routing.py", "demo_api.py", "demo_sim.py"):
        src = open(f"./{name}").read()
        for bad in ('if os.environ.get("TG_DEMO_ENABLED")',
                    'if os.environ.get("SKLZ_DEMO_DIAG")',
                    'bool(os.environ.get("TG_DEMO_ENABLED"'):
            assert bad not in src, f"{name}: {bad}"
