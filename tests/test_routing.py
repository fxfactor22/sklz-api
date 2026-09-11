"""Behaviour must be identical to the pre-refactor system."""
import os, sys
sys.path.insert(0, ".")
import routing
from routing import RoutingScope, resolve_destinations, EnvTelegramDestinationResolver

ENV = {
    "TELEGRAM_BOT_TOKEN": "MAIN",
    "TG_CHANNEL_METALS": "-100metals", "TG_CHANNEL_FOREX": "-100forex",
    "TG_CHANNEL_CRYPTO": "-100crypto", "TG_CHANNEL_STOCKS": "-100stocks",
    "TG_CHANNEL_SIGNALS": "-100general",
    "TG_MIRROR_CHAT": "-100ali", "TG_MIRROR_TOKEN": "ALITOK",
    "TG_MIRROR2_CHAT": "-100fx2",            # no token -> falls back to MAIN
}

def setup():
    for k in list(os.environ):
        if k.startswith(("TG_", "TELEGRAM_")):
            os.environ.pop(k)
    os.environ.update(ENV)

def test_each_category_resolves_as_before():
    setup()
    for cat, chat in (("metals", "-100metals"), ("forex", "-100forex"),
                      ("crypto", "-100crypto"), ("stocks", "-100stocks")):
        d = {x.key: x for x in resolve_destinations(RoutingScope(category=cat))}
        assert d[cat].chat_id == chat and d[cat].token.reveal() == "MAIN", cat
        assert d["general"].chat_id == "-100general"

def test_mirrors_keep_their_own_tokens():
    setup()
    d = {x.key: x for x in resolve_destinations(RoutingScope(category="metals"))}
    assert d["tg_mirror"].chat_id == "-100ali"
    assert d["tg_mirror"].token.reveal() == "ALITOK"   # own bot preserved
    assert d["tg_mirror2"].token.reveal() == "MAIN"    # falls back, as before
    assert d["tg_mirror"].primary is False
    assert d["tg_mirror3"] if False else "tg_mirror3" not in d

def test_general_channel_name_fallback_order():
    setup()
    os.environ.pop("TG_CHANNEL_SIGNALS")
    os.environ["TG_CHANNEL_MARKETING"] = "-100mkt"
    r = EnvTelegramDestinationResolver()
    assert r._general_chat() == "-100mkt"

def test_missing_category_fails_the_same_way():
    setup()
    os.environ.pop("TG_CHANNEL_METALS")
    d = {x.key: x for x in resolve_destinations(RoutingScope(category="metals"))}
    assert d["metals"].enabled is False
    assert "no channel configured" in d["metals"].meta["reason"]

def test_explicit_channel_list_and_all():
    setup()
    keys = {x.key for x in resolve_destinations(
        RoutingScope(channels=("all",)))}
    assert {"forex", "crypto", "stocks", "metals", "general",
            "tg_mirror", "tg_mirror2"} <= keys
    keys = {x.key for x in resolve_destinations(
        RoutingScope(channels=("metals",)))}
    assert keys == {"metals"}, keys

def test_tokens_never_leave_in_describe_or_redacted():
    setup()
    desc = EnvTelegramDestinationResolver().describe()
    blob = repr(desc)
    assert "MAIN" not in blob and "ALITOK" not in blob
    d = resolve_destinations(RoutingScope(category="metals"))[0]
    assert "token" not in d.redacted() and d.redacted()["has_token"] is True

def test_sender_does_not_rediscover_credentials():
    """The delivery adapter must not be able to reach the environment.

    Asserted on the source rather than by calling it: the guarantee we want
    is that the code CANNOT rediscover a token, not merely that it did not
    on one input.
    """
    src = open("./signals_engine.py").read()
    body = src[src.index("def _post_telegram("):]
    body = body[:body.index("\ndef ", 1)]
    assert "os.environ" not in body, "sender still reads the environment"


def test_no_direct_routing_lookups_remain_in_the_engine():
    """After the refactor the engine resolves nothing itself."""
    src = open("./signals_engine.py").read()
    for needle in ('os.environ.get("TG_CHANNEL_',
                   'os.environ.get(f"TG_CHANNEL_',
                   'os.environ.get(f"{prefix}_CHAT'):
        assert needle not in src, needle


def test_business_scope_is_reserved_and_unused():
    setup()
    a = resolve_destinations(RoutingScope(category="metals"))
    b = resolve_destinations(RoutingScope(category="metals",
                                          business="anything"))
    assert [x.chat_id for x in a] == [x.chat_id for x in b]


# ── credential safety ────────────────────────────────────────────────
def test_a_token_cannot_leak_through_any_ordinary_rendering():
    """The token must survive nothing but an explicit .reveal().

    A plain string field leaked through repr(), f-strings, log lines and
    json.dumps(asdict(...)). Each of those is a one-character mistake away
    in normal code, so the guarantee has to be structural.
    """
    import dataclasses, json, logging, io
    from routing import Destination, Secret
    TOKEN = "8123456:AAH-SUPER-SECRET-BOT-TOKEN"
    d = Destination(key="metals", chat_id="-100x", token=Secret(TOKEN))

    for rendered in (repr(d), str(d), f"{d}", "%s" % (d,),
                     repr(dataclasses.asdict(d)), repr(d.redacted()),
                     repr(d.token), str(d.token)):
        assert TOKEN not in rendered, rendered

    # a log call must not leak it either
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    log = logging.getLogger("routing-secret-test")
    log.addHandler(h); log.setLevel(logging.INFO)
    log.info("sending to %s", d)
    log.info("dest=%r token=%s", d, d.token)
    assert TOKEN not in buf.getvalue()

    # serialisation must refuse rather than silently succeed
    try:
        json.dumps(dataclasses.asdict(d))
        raise AssertionError("asdict serialised a credential")
    except TypeError:
        pass

    # and the resolver's describe() never carries one
    from routing import EnvTelegramDestinationResolver
    import os
    os.environ["TELEGRAM_BOT_TOKEN"] = TOKEN
    os.environ["TG_MIRROR_CHAT"] = "-100m"
    assert TOKEN not in repr(EnvTelegramDestinationResolver().describe())

    # the value is still reachable when asked for by name
    assert d.token.reveal() == TOKEN


def test_resolved_destinations_carry_secrets_not_strings():
    setup()
    from routing import Secret
    for d in resolve_destinations(RoutingScope(category="metals")):
        assert isinstance(d.token, Secret), d.key
        assert "MAIN" not in repr(d) and "ALITOK" not in repr(d)


# ── consolidated destinations ────────────────────────────────────────
def _setup_all():
    import os
    for k in list(os.environ):
        if k.startswith(("TG_", "TELEGRAM_", "SIGNAL_CHANNEL", "ALERT_CHANNEL")):
            os.environ.pop(k)
    os.environ.update({
        "TELEGRAM_BOT_TOKEN": "MAIN", "TG_SALES_BOT_TOKEN": "SALES",
        "SIGNAL_CHANNEL_ID": "-100summary", "TG_MIRROR2_CHAT": "-100m2",
        "TG_ARABIC_CHAT": "@ar", "TG_ARABIC_TOKEN": "ARTOK",
    })


def test_summary_uses_the_sales_bot_and_its_own_channel():
    """Not the signal channel set — that difference is load-bearing."""
    _setup_all()
    d = {x.key: x for x in resolve_destinations(RoutingScope(purpose="summary"))}
    assert d["summary_main"].chat_id == "-100summary"
    assert d["summary_main"].token.reveal() == "SALES"
    assert d["tg_mirror2"].chat_id == "-100m2"
    assert d["tg_mirror2"].token.reveal() == "SALES"   # falls back to sales
    assert d["tg_mirror2"].primary is False


def test_summary_falls_back_to_alert_channel_id():
    _setup_all()
    import os
    os.environ.pop("SIGNAL_CHANNEL_ID")
    os.environ["ALERT_CHANNEL_ID"] = "-100alt"
    d = resolve_destinations(RoutingScope(purpose="summary"))[0]
    assert d.chat_id == "-100alt"


def test_alert_dm_takes_the_chat_from_the_caller():
    _setup_all()
    d = resolve_destinations(
        RoutingScope(purpose="alert", chat_id="99887"))[0]
    assert d.chat_id == "99887" and d.token.reveal() == "SALES"
    assert d.enabled is True
    # no chat -> disabled, exactly like the old `not chat_id` guard
    assert resolve_destinations(
        RoutingScope(purpose="alert", chat_id=""))[0].enabled is False


def test_arabic_token_indirection_order_preserved():
    _setup_all()
    import os
    d = resolve_destinations(RoutingScope(language="ar"))[0]
    assert d.chat_id == "@ar" and d.token.reveal() == "ARTOK"
    # TG_ARABIC_TOKEN_ENV names ANOTHER variable
    os.environ.pop("TG_ARABIC_TOKEN")
    os.environ["TG_ARABIC_TOKEN_ENV"] = "TG_SALES_BOT_TOKEN"
    assert resolve_destinations(
        RoutingScope(language="ar"))[0].token.reveal() == "SALES"
    # and with neither, the default bot
    os.environ.pop("TG_ARABIC_TOKEN_ENV")
    assert resolve_destinations(
        RoutingScope(language="ar"))[0].token.reveal() == "MAIN"


def test_arabic_wins_over_purpose():
    """Arabic summaries and Arabic content share one destination."""
    _setup_all()
    for purpose in ("signal", "summary", "content"):
        d = resolve_destinations(
            RoutingScope(language="ar", purpose=purpose))[0]
        assert d.key == "arabic" and d.chat_id == "@ar"


def test_migrated_modules_hold_no_routing_lookups():
    for path in ("signal_lifecycle.py", "alerts_api.py"):
        src = open(path).read()
        for needle in ('os.environ.get("TG_', 'os.environ.get("TELEGRAM_',
                       'os.environ.get("SIGNAL_CHANNEL_ID',
                       'os.environ.get("ALERT_CHANNEL_ID'):
            assert needle not in src, f"{path}: {needle}"
    ar = open("sklz_arabic.py").read()
    for needle in ('os.environ.get("TG_ARABIC_CHAT',
                   'os.environ.get("TG_ARABIC_TOKEN'):
        assert needle not in ar, needle
