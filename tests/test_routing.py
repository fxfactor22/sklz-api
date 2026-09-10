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
        assert d[cat].chat_id == chat and d[cat].token == "MAIN", cat
        assert d["general"].chat_id == "-100general"

def test_mirrors_keep_their_own_tokens():
    setup()
    d = {x.key: x for x in resolve_destinations(RoutingScope(category="metals"))}
    assert d["tg_mirror"].chat_id == "-100ali"
    assert d["tg_mirror"].token == "ALITOK"        # own bot preserved
    assert d["tg_mirror2"].token == "MAIN"         # falls back, as before
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
    src = open("signals_engine.py").read()
    body = src[src.index("def _post_telegram("):]
    body = body[:body.index("\ndef ", 1)]
    assert "os.environ" not in body, "sender still reads the environment"


def test_no_direct_routing_lookups_remain_in_the_engine():
    """After the refactor the engine resolves nothing itself."""
    src = open("signals_engine.py").read()
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
