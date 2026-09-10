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
    business: str | None = None   # RESERVED — do not build on this yet


@dataclass
class Destination:
    """One resolved place to deliver to."""
    key: str                      # stable id for logging: "metals", "mirror2"
    kind: str = "telegram"
    chat_id: str = ""
    token: str = ""               # resolved credential, never logged
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
                token=(os.environ.get(f"{prefix}_TOKEN", "").strip()
                       or self._default_token()),
                primary=False,
                meta={"env_prefix": prefix}))
        return out

    # ---- the one entry point ----
    def resolve(self, scope: RoutingScope) -> list[Destination]:
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
                    key=name, chat_id=chat, token=token,
                    enabled=bool(chat),
                    meta={"reason": "" if chat else "not configured"}))
            return out

        # The signal path: category channel + general channel + mirrors.
        out = []
        cat_chat = self._category_chat(scope.category) if scope.category else ""
        if scope.category:
            out.append(Destination(
                key=scope.category, chat_id=cat_chat, token=token,
                enabled=bool(cat_chat),
                meta={"reason": "" if cat_chat
                      else f"no channel configured for {scope.category}"}))
        gen_chat = self._general_chat()
        if gen_chat:
            out.append(Destination(key="general", chat_id=gen_chat,
                                   token=token))
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
