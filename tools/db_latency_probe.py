#!/usr/bin/env python3
"""Measure Supabase call latency and what it costs the event loop.

    railway run python3 tools/db_latency_probe.py

READ ONLY. Every query is a SELECT with a small limit. Nothing is
written, nothing is deleted, no provider or tenant is touched.

WHY FROM HERE AND NOT FROM OUTSIDE
==================================
Measuring from a laptop or another datacentre measures the internet.
Attempting exactly that produced a result where six concurrent database
requests appeared FASTER than none — network variance swamping the
signal. Run inside the Railway environment, the number is the app's
actual distance to Supabase, which is the number that matters.

WHAT IT REPORTS
===============
  1. per-call latency, p50 / p95 / max, per table
  2. what that costs on the hot paths, using real DB-call counts
  3. the serialisation arithmetic: at N concurrent requests, how long
     the event loop is unavailable
"""
from __future__ import annotations

import json
import os
import statistics
import time
import urllib.parse
import urllib.request

# table -> a cheap, representative read
PROBES = [
    ("providers", "providers?select=id&limit=1"),
    ("subscriptions", "subscriptions?select=user_id&limit=1"),
    ("copy_slaves", "copy_slaves?select=id&limit=1"),
    ("copy_configs", "copy_configs?select=slave_id&limit=1"),
    ("copy_queue", "copy_queue?select=id&limit=1"),
    ("bot_orders", "bot_orders?select=id&limit=1"),
    ("journal_trades", "journal_trades?select=id&limit=1"),
    ("signals", "signals?select=id&limit=1"),
]

# measured counts from the P1.5 static analysis
HOT_PATHS = [
    ("copy_api.poll", 5, "every 2s per follower"),
    ("bot_ingest.get_command", 3, "every 2-5s per Runner"),
    ("copy_api.master_event", 4, "per master entry/exit"),
    ("journal.bot_ingest", 5, "per closed-trade batch"),
    ("copy_api.report", 3, "per copied trade"),
    ("demo_api.trade_create", 2, "per demo trade"),
]

ROUNDS = int(os.environ.get("PROBE_ROUNDS", "15"))


def _get(path: str) -> float:
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    if not url or not key:
        raise SystemExit("SUPABASE_URL / SUPABASE_SERVICE_KEY not in env")
    req = urllib.request.Request(
        f"{url}/rest/v1/{path}",
        headers={"apikey": key, "authorization": f"Bearer {key}",
                 "accept": "application/json"})
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=20) as r:
        r.read()
    return (time.perf_counter() - t0) * 1000


def pct(values: list[float], q: float) -> float:
    s = sorted(values)
    return s[min(int(len(s) * q), len(s) - 1)] if s else 0.0


def main() -> int:
    print("SUPABASE LATENCY — measured from inside the SKLZ runtime")
    print(f"  {ROUNDS} samples per table, SELECT ... limit 1, read only\n")

    all_ms: list[float] = []
    print(f"  {'table':<18} {'p50':>8} {'p95':>8} {'max':>8}")
    print("  " + "-" * 46)
    for label, path in PROBES:
        try:
            ms = [_get(path) for _ in range(ROUNDS)]
        except Exception as exc:  # noqa: BLE001
            print(f"  {label:<18} unavailable ({type(exc).__name__})")
            continue
        all_ms.extend(ms)
        print(f"  {label:<18} {pct(ms,.5):>7.1f}ms {pct(ms,.95):>7.1f}ms "
              f"{max(ms):>7.1f}ms")

    if not all_ms:
        return 1
    p50, p95 = pct(all_ms, .5), pct(all_ms, .95)
    print(f"\n  overall            {p50:>7.1f}ms {p95:>7.1f}ms "
          f"{max(all_ms):>7.1f}ms")
    print(f"  mean {statistics.mean(all_ms):.1f}ms over {len(all_ms)} calls")

    print("\n\nWHAT THIS COSTS THE EVENT LOOP")
    print("  Every Supabase call is synchronous, so for its whole duration")
    print("  the single worker serves nothing else.\n")
    print(f"  {'hot path':<26} {'calls':>6} {'p50 blocked':>13} "
          f"{'p95 blocked':>13}  frequency")
    print("  " + "-" * 82)
    for name, calls, freq in HOT_PATHS:
        print(f"  {name:<26} {calls:>6} {calls*p50:>11.0f}ms "
              f"{calls*p95:>11.0f}ms  {freq}")

    # continuous background load, from the live copy network
    per_sec = 3 * 5 / 2 + 3 / 5          # 3 followers @2s, 1 Runner @5s
    duty = per_sec * p50 / 1000
    print(f"\n  Continuous background: {per_sec:.1f} DB calls/second")
    print(f"  Loop occupied by DB alone: {duty*100:.1f}% of wall-clock time")
    if duty > 0.5:
        print("  -> over half the loop's time is spent waiting on the DB")
    elif duty > 0.15:
        print("  -> a meaningful slice; concurrency will degrade under load")
    else:
        print("  -> small today, but it scales linearly with followers")

    print("\n  Serialisation at N concurrent requests on the poll path:")
    for n in (1, 3, 5, 10, 20):
        print(f"    {n:>3} concurrent -> last request waits "
              f"{n*5*p50:>7.0f}ms before it is served")

    print("\n\nINTERPRETATION")
    if p95 < 20:
        print("  Calls are fast. The defect is real but the cost today is")
        print("  small; it becomes visible as follower count grows.")
    elif p95 < 60:
        print("  Moderate. Worth fixing before the copy network grows, not")
        print("  urgently today.")
    else:
        print("  Slow enough to matter now. The loop is spending real time")
        print("  blocked, and added concurrency will not help until this")
        print("  work moves off it.")
    print("\n  No rows were written. No provider or tenant was touched.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
