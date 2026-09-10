"""Destination resolution — where a message goes, not where that is stored.

WHY THIS MODULE EXISTS
======================
Signal publishing used to ask the environment directly:

    chat = os.environ.get(f"TG_CHANNEL_{category.upper()}")
    token = os.environ.get("TELEGRAM_BOT_TOKEN")

That works for exactly one tenant. Adding a single extra channel took an
hour and a purpose-built diagnostic endpoint, because four similarly named
token variables held three different bots and nothing in the code could say
which was which. Hundreds of providers is not a bigger version of that
problem; it is a different problem.

So routing now goes through one door:

    scope  ──►  resolve_destinations(scope)  ──►  [Destination, ...]

The Signal Engine states WHERE a message needs to go — category, audience,
language — and receives resolved destinations. It never learns whether they
came from environment variables, a database, or a provider's configuration.
Swapping the backend later should touch this file and nothing else.

WHAT THIS PHASE DELIBERATELY DOES NOT DO
========================================
No database. No new tables. No provider identity. The environment resolver
below reproduces today's behaviour exactly, variable for variable, because
a compatibility refactor that changes behaviour is not a compatibility
refactor. `RoutingScope.business` exists as a placeholder and is unused —
the canonical business identity is still an open architectural question and
naming it now would prejudge the answer.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

# Categories that have their own channel today.
CHANNEL_KEYS = ("forex", "crypto", "stocks", "metals")

# The general/marketing channel accepts several historical names. Kept in
# this order; first match wins, exactly as before.
_GENERAL_NAMES = ("TG_CHANNEL_SIGNALS", "TG_CHANNEL_GENERAL",
                  "TG_CHANNEL_ALL", "TG_CHANNEL_PUBLIC",
                  "TG_CHANNEL_MARKETING", "TG_CHANNEL_MAIN")

_MIRROR_PREFIXES = ("TG_MIRROR", "TG_MIRROR2", "TG_MIRROR3")


@dataclass(frozen=True)
class RoutingScope:
    """What we know about where a message should go.

    Only fields the code can actually populate today. `business` is the
    seam for the future canonical identity — SKLZ provider, ISKRA tenant,
    or a mapped id — and is deliberately untyped and unused until that
    decision is made.
    """
    category: str = ""            # forex | crypto | stocks | metals
    language: str = "en"          # en | ar | ru
    audience: str = "all"         # all | free | vip
    purpose: str = "signal"       # signal | update | summary | content
    source: str = ""              # runner | manual | scanner
    channels: tuple = ()          # explicit channel names, when asked for
    chat_id: str = ""             # direct message target, e.g. a user's DM
    business: str | None = None   # RESERVED — do not build on this yet


class Secret:
    """A credential that refuses to render itself.

    A plain string token on a dataclass leaks through the default repr, an
    f-string, a log line, or json.dumps of asdict(). Wrapping it means the
    ONLY way to read the value is to ask for it by name — everything else,
    including accidental logging, prints a redaction.
    """
    __slots__ = ("_value",)

    def __init__(self, value: str = "") -> None:
        self._value = value or ""

    def reveal(self) -> str:
        """Explicit, greppable, and the only accessor."""
        return self._value

    def __bool__(self) -> bool:
        return bool(self._value)

    def __len__(self) -> int:
        return len(self._value)

    def __repr__(self) -> str:
        return "Secret(set)" if self._value else "Secret(empty)"

    __str__ = __repr__

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Secret):
            return self._value == other._value
        return NotImplemented

    def __hash__(self) -> int:
        return hash(("Secret", self._value))


@dataclass
class Destination:
    """One resolved place to deliver to."""
    key: str                      # stable id for logging: "metals", "mirror2"
    kind: str = "telegram"
    chat_id: str = ""
    token: Secret = field(default_factory=Secret, repr=False)
    language: str = "en"
    audience: str = "all"
    primary: bool = True          # False for mirrors
    enabled: bool = True
    meta: dict = field(default_factory=dict)

    def redacted(self) -> dict:
        """Safe to log or return in an API response."""
        return {"key": self.key, "kind": self.kind, "chat_id": self.chat_id,
                "language": self.language, "audience": self.audience,
                "primary": self.primary, "enabled": self.enabled,
                "has_token": bool(self.token)}


class DestinationResolver:
    """Interface. A database-backed resolver will implement the same shape."""

    def resolve(self, scope: RoutingScope) -> list[Destination]:
        raise NotImplementedError

    def describe(self) -> dict:
        raise NotImplementedError


class EnvTelegramDestinationResolver(DestinationResolver):
    """Today's production behaviour, reproduced exactly.

    Every variable that worked before still works. Nothing is migrated,
    renamed, or given a new fallback order.
    """

    # ---- primitives, matching the previous private helpers ----
    @staticmethod
    def _default_token() -> str:
        return os.environ.get("TELEGRAM_BOT_TOKEN", "")

    @staticmethod
    def _sales_token() -> str:
        """The bot that posts summaries and user alerts.

        Different from the signal bot today, and the fallback order is
        load-bearing: TG_SALES_BOT_TOKEN first, TELEGRAM_BOT_TOKEN second.
        """
        return (os.environ.get("TG_SALES_BOT_TOKEN")
                or os.environ.get("TELEGRAM_BOT_TOKEN", ""))

    @staticmethod
    def _summary_chat() -> str:
        return (os.environ.get("SIGNAL_CHANNEL_ID")
                or os.environ.get("ALERT_CHANNEL_ID", ""))

    @staticmethod
    def _arabic_chat() -> str:
        return os.environ.get("TG_ARABIC_CHAT", "").strip()

    @staticmethod
    def _arabic_token() -> str:
        """Three ways to name the Arabic bot, in the established order."""
        direct = os.environ.get("TG_ARABIC_TOKEN", "").strip()
        if direct:
            return direct
        named = os.environ.get("TG_ARABIC_TOKEN_ENV", "").strip()
        if named:
            return os.environ.get(named, "").strip()
        return os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()

    @staticmethod
    def _category_chat(category: str) -> str:
        return os.environ.get(f"TG_CHANNEL_{category.upper()}", "")

    @staticmethod
    def _general_chat() -> str:
        for name in _GENERAL_NAMES:
            v = os.environ.get(name, "")
            if v:
                return v
        return ""

    def _mirrors(self) -> list[Destination]:
        out = []
        for prefix in _MIRROR_PREFIXES:
            chat = os.environ.get(f"{prefix}_CHAT", "").strip()
            if not chat:
                continue
            out.append(Destination(
                key=prefix.lower(),
                chat_id=chat,
                token=Secret(os.environ.get(f"{prefix}_TOKEN", "").strip()
                             or self._default_token()),
                primary=False,
                meta={"env_prefix": prefix}))
        return out

    # ---- the one entry point ----
    def resolve(self, scope: RoutingScope) -> list[Destination]:
        # Arabic is its own destination with its own bot, whatever the
        # purpose — signal, summary, generated content or welcome post.
        if scope.language == "ar":
            chat = self._arabic_chat()
            return [Destination(key="arabic", chat_id=chat,
                                token=Secret(self._arabic_token()),
                                language="ar", enabled=bool(chat))]

        # The scanner's own channel. Separate from the summary channel even
        # though the summary falls back to the same variable — they are two
        # purposes that happen to share a default, not one destination.
        if scope.purpose == "scanner_alert":
            chat = os.environ.get("ALERT_CHANNEL_ID", "").strip()
            return [Destination(key="scanner_channel", chat_id=chat,
                                token=Secret(self._sales_token()),
                                enabled=bool(chat))]

        # The demo signal channel. Deliberately disabled unless BOTH the
        # chat and an explicit enable flag are set: §14 of the demo
        # contract says do not publish to @sklzlabsdemo until the
        # integration verification stage, and a destination that goes live
        # because someone set one variable is not disabled.
        if scope.purpose == "demo_signal":
            chat = os.environ.get("TG_DEMO_CHAT", "").strip()
            on = os.environ.get("TG_DEMO_ENABLED", "0") == "1"
            return [Destination(
                key="demo_signals", chat_id=chat,
                token=Secret(os.environ.get("TG_DEMO_TOKEN", "").strip()
                             or self._sales_token()),
                language=scope.language or "en",
                enabled=bool(chat and on),
                meta={"label": "SKLZ Demo Signals",
                      "reason": "" if (chat and on)
                      else "demo delivery disabled"})]

        # A user's direct message. The credential is ours; the chat is the
        # caller's, so it is routing context rather than configuration.
        if scope.purpose == "alert":
            return [Destination(key="alert_dm", chat_id=scope.chat_id,
                                token=Secret(self._sales_token()),
                                enabled=bool(scope.chat_id))]

        # The daily summary goes to its own channel via the sales bot, plus
        # mirror2. Deliberately NOT the signal channel set.
        if scope.purpose == "summary":
            tok = self._sales_token()
            out = [Destination(key="summary_main",
                               chat_id=self._summary_chat(),
                               token=Secret(tok),
                               enabled=bool(self._summary_chat()))]
            m2 = os.environ.get("TG_MIRROR2_CHAT", "")
            if m2:
                out.append(Destination(
                    key="tg_mirror2", chat_id=m2,
                    token=Secret(os.environ.get("TG_MIRROR2_TOKEN", "") or tok),
                    primary=False))
            return out

        token = self._default_token()

        # Explicit channel list (the send_to_channels path).
        if scope.channels:
            wanted = set(scope.channels)
            if "all" in wanted:
                wanted = set(CHANNEL_KEYS) | {"general"}
            out: list[Destination] = []
            if "all" in scope.channels or "mirrors" in set(scope.channels):
                out.extend(self._mirrors())
            wanted.discard("mirrors")
            for name in sorted(wanted):
                chat = (self._general_chat() if name == "general"
                        else self._category_chat(name))
                out.append(Destination(
                    key=name, chat_id=chat, token=Secret(token),
                    enabled=bool(chat),
                    meta={"reason": "" if chat else "not configured"}))
            return out

        # The signal path: category channel + general channel + mirrors.
        out = []
        cat_chat = self._category_chat(scope.category) if scope.category else ""
        if scope.category:
            out.append(Destination(
                key=scope.category, chat_id=cat_chat, token=Secret(token),
                enabled=bool(cat_chat),
                meta={"reason": "" if cat_chat
                      else f"no channel configured for {scope.category}"}))
        gen_chat = self._general_chat()
        if gen_chat:
            out.append(Destination(key="general", chat_id=gen_chat,
                                   token=Secret(token)))
        out.extend(self._mirrors())
        return out

    def describe(self) -> dict:
        """What is configured — for health endpoints. Never returns tokens."""
        return {
            "backend": "env",
            "default_bot_configured": bool(self._default_token()),
            "categories": {c: bool(self._category_chat(c))
                           for c in CHANNEL_KEYS},
            "general": bool(self._general_chat()),
            "mirrors": [d.redacted() for d in self._mirrors()],
            "summary_channel": bool(self._summary_chat()),
            "sales_bot_configured": bool(self._sales_token()),
            "arabic": {"chat": bool(self._arabic_chat()),
                       "bot_configured": bool(self._arabic_token())},
        }


# The single active resolver. Swapping this for a database-backed one is
# the whole point of the refactor: nothing above the resolver changes.
_RESOLVER: DestinationResolver = EnvTelegramDestinationResolver()


def get_resolver() -> DestinationResolver:
    return _RESOLVER


def set_resolver(resolver: DestinationResolver) -> None:
    """Used by tests today; by configuration once a second backend exists."""
    global _RESOLVER
    _RESOLVER = resolver


def resolve_destinations(scope: RoutingScope) -> list[Destination]:
    return _RESOLVER.resolve(scope)
