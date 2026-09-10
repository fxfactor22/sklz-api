"""In-process diagnostics — measured where the code actually runs.

WHY THIS ENDPOINT EXISTS
========================
`railway run` executes on the operator's laptop with Railway's variables
injected. It measures the laptop's distance to Supabase, not the API's.
That produced a reading of ~300ms per call and the conclusion that the
event loop was busy 241% of wall-clock time — an impossible number, and
the giveaway that the measurement was wrong rather than the system.

This runs inside the container, so the number is the one the application
lives with.

SAFETY
======
  * platform admin only
  * a FIXED allowlist of tables; no table name comes from the request
  * SELECT <pk> LIMIT 1 — no writes, no filters, no user input in SQL
  * bounded by BOTH a sample cap and a wall-clock budget, so a slow
    database cannot turn a diagnostic into an outage
  * one run at a time
  * the probe itself runs OFF the event loop; a measurement that blocks
    the loop measures itself
"""
from __future__ import annotations

import asyncio
import os
import statistics
import time

from fastapi import APIRouter, Depends, HTTPException, status
from supabase import Client

import provider_rules as rules
from aio import offload
from auth import get_current_user
from db import get_supabase

router = APIRouter(prefix="/api/diagnostics", tags=["diagnostics"])

# Fixed. A request cannot name a table.
PROBE_TABLES: list[tuple[str, str]] = [
    ("providers", "id"),
    ("subscriptions", "user_id"),
    ("copy_slaves", "id"),
    ("copy_configs", "slave_id"),
    ("copy_queue", "id"),
    ("bot_orders", "id"),
    ("journal_trades", "id"),
    ("signals", "id"),
]

# Counted from the code in P1.5, not estimated.
HOT_PATHS = [
    ("copy_api.poll", 5, "every 2s per follower"),
    ("bot_ingest.get_command", 3, "every 2-5s per Runner"),
    ("copy_api.master_event", 4, "per master entry/exit"),
    ("journal.bot_ingest", 5, "per closed-trade batch"),
    ("copy_api.report", 3, "per copied trade"),
    ("demo_api.trade_create", 2, "per demo trade"),
]

# 3 followers polling every 2s (5 calls each) + 1 Runner every 5s (3 calls)
BACKGROUND_CALLS_PER_SEC = 3 * 5 / 2 + 3 / 5

MAX_SAMPLES = 25
DEFAULT_SAMPLES = 10
TIME_BUDGET_SECONDS = 8.0

_running = False


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    return s[min(int(len(s) * q), len(s) - 1)]


def _probe(sb: Client, samples: int) -> dict:
    """Blocking. Runs on a worker thread, never on the loop."""
    started = time.perf_counter()
    per_table: dict[str, dict] = {}
    everything: list[float] = []
    truncated = False

    for table, col in PROBE_TABLES:
        if time.perf_counter() - started > TIME_BUDGET_SECONDS:
            truncated = True
            break
        times: list[float] = []
        error = ""
        for _ in range(samples):
            if time.perf_counter() - started > TIME_BUDGET_SECONDS:
                truncated = True
                break
            t0 = time.perf_counter()
            try:
                sb.table(table).select(col).limit(1).execute()
            except Exception as exc:  # noqa: BLE001
                error = f"{type(exc).__name__}"
                break
            times.append((time.perf_counter() - t0) * 1000)
        if error and not times:
            per_table[table] = {"error": error}
            continue
        everything.extend(times)
        per_table[table] = {
            "samples": len(times),
            "p50_ms": round(_pct(times, 0.5), 2),
            "p95_ms": round(_pct(times, 0.95), 2),
            "max_ms": round(max(times), 2) if times else 0,
        }

    return {"per_table": per_table, "all": everything,
            "truncated": truncated,
            "elapsed_s": round(time.perf_counter() - started, 2)}


@router.get("/db-latency")
async def db_latency(samples: int = DEFAULT_SAMPLES,
                     user=Depends(get_current_user),
                     sb: Client = Depends(get_supabase)) -> dict:
    """Measure Supabase call latency from inside the API process."""
    global _running
    if not rules.is_platform_admin(user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "platform admin only")
    if _running:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "a latency probe is already running")
    samples = max(1, min(int(samples), MAX_SAMPLES))

    _running = True
    try:
        raw = await offload(_probe, sb, samples)
    finally:
        _running = False

    allv = raw.pop("all")
    if not allv:
        return {"ok": False, "reason": "no successful probes",
                "per_table": raw["per_table"]}

    p50, p95 = _pct(allv, 0.5), _pct(allv, 0.95)
    duty = BACKGROUND_CALLS_PER_SEC * p50 / 1000

    if p95 < 20:
        verdict = "small"
        note = ("Calls are fast. The synchronous-DB defect is real but its "
                "cost today is small; it grows linearly with followers.")
    elif p95 < 60:
        verdict = "moderate"
        note = ("Worth fixing before the copy network grows. Not urgent "
                "today.")
    else:
        verdict = "significant"
        note = ("The loop spends real time blocked. Adding concurrency "
                "will not help until this work moves off it.")

    return {
        "ok": True,
        "measured": "in_process",
        "note_on_method": (
            "Measured inside the API container. A `railway run` probe "
            "measures the operator's laptop instead and is not comparable."),
        "samples_per_table": samples,
        "elapsed_s": raw["elapsed_s"],
        "truncated": raw["truncated"],
        "per_table": raw["per_table"],
        "overall": {"p50_ms": round(p50, 2), "p95_ms": round(p95, 2),
                    "max_ms": round(max(allv), 2),
                    "mean_ms": round(statistics.mean(allv), 2),
                    "calls": len(allv)},
        "blocking_cost": [
            {"path": name, "db_calls": calls,
             "p50_blocked_ms": round(calls * p50, 1),
             "p95_blocked_ms": round(calls * p95, 1),
             "frequency": freq}
            for name, calls, freq in HOT_PATHS],
        "background": {
            "calls_per_second": round(BACKGROUND_CALLS_PER_SEC, 1),
            "loop_duty_pct": round(duty * 100, 1),
            "meaning": ("share of wall-clock time the single worker spends "
                        "waiting on the database, from copy-network traffic "
                        "alone, before any user request")},
        "serialisation_poll_path": {
            f"{n}_concurrent": round(n * 5 * p50) for n in (1, 3, 5, 10, 20)},
        "verdict": verdict,
        "interpretation": note,
        "writes_performed": 0,
    }
