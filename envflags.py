"""One parser for security-sensitive boolean environment variables.

A feature flag that decides whether real messages reach real people is
not a place for each call site to invent its own rule. `"0"`, `"false"`,
`"off"` and `"no"` are all things an operator will reasonably type
expecting "off", and any of them is truthy to a bare `bool(str)`.

Unrecognised values fail CLOSED and warn. An operator who typed
something we do not understand meant *something*, and guessing "on" for
a flag that governs outbound delivery is the wrong half of the coin.
"""
from __future__ import annotations

import os

TRUE_VALUES = {"1", "true", "yes", "on", "y", "t"}
FALSE_VALUES = {"", "0", "false", "no", "off", "n", "f"}


def flag(name: str, default: bool = False, log=print) -> bool:
    """Read a boolean environment flag, failing closed.

        unset / empty / 0 / false / FALSE / no / off  -> False
        1 / true / TRUE / yes / on                    -> True
        anything else                                 -> False, with a warning
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in TRUE_VALUES:
        return True
    if value in FALSE_VALUES:
        return False
    try:
        log(f"[flags] {name}={raw!r} is not a recognised boolean — "
            f"treating it as OFF. Use 1/true/yes/on or 0/false/no/off.")
    except Exception:  # noqa: BLE001
        pass
    return False
