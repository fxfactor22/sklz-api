"""One comparison for every key the trading engine presents.

WHY THIS EXISTS
===============
Three routes accepted the engine's key with three private copies of the
same two lines, and none of them said why a key was refused. When the
key was rotated, the engine on the VPS kept sending the old one and the
API answered 401 for two days; the log showed the status code and
nothing else. Whether the VPS had the wrong value, the running process
had a stale environment, or the value in Railway carried a trailing
space from a paste, all looked identical: silence at the broker,
silence in the channel.

The comparison now strips both sides — an invisible character is not a
different credential — and a refusal prints which condition failed and
how long each side was. Lengths are not the secret; they are enough to
tell "not set" from "set to something else" from "one stray space".
"""
from __future__ import annotations

import os


def _expected(names: tuple[str, ...]) -> list[str]:
    return [v for v in (os.environ.get(n, "").strip() for n in names) if v]


def engine_key_ok(key: str, where: str,
                  names: tuple[str, ...] = ("SIGNAL_WEBHOOK_KEY",
                                            "BOT_INGEST_KEY")) -> bool:
    """True if `key` matches any configured value in `names`.

    `where` names the route for the log line. Values are never printed.
    """
    got = (key or "").strip()
    expected = _expected(names)
    if not expected:
        print(f"[keyauth] {where} rejected: none of {'/'.join(names)} is set",
              flush=True)
        return False
    if got and got in expected:
        return True
    print(f"[keyauth] {where} rejected: "
          f"{'no key sent' if not got else 'key mismatch'} "
          f"(sent {len(got)} chars, expected "
          f"{'/'.join(str(len(v)) for v in expected)})", flush=True)
    return False
