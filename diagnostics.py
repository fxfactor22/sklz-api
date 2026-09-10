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


def _phases() -> dict:
    """Split a request into network and origin time.

    A full query took 200ms while connecting took 23ms. Without this
    split the 200ms looks like distance, and the obvious-but-wrong
    conclusion is to move the service. The remainder is time spent
    INSIDE the database platform, which is a different fix entirely.
    """
    import socket
    import ssl
    import urllib.parse

    url = os.environ.get("SUPABASE_URL", "")
    if not url:
        return {}
    host = urllib.parse.urlparse(url).hostname or ""
    if not host:
        return {}
    out: dict = {"host": host}
    try:
        t0 = time.perf_counter()
        socket.gethostbyname(host)
        out["dns_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        t0 = time.perf_counter()
        sock = socket.create_connection((host, 443), timeout=10)
        out["tcp_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        t0 = time.perf_counter()
        ctx = ssl.create_default_context()
        ctx.wrap_socket(sock, server_hostname=host).close()
        out["tls_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        out["connect_total_ms"] = round(
            out["dns_ms"] + out["tcp_ms"] + out["tls_ms"], 1)
    except Exception as exc:  # noqa: BLE001
        out["error"] = type(exc).__name__
    return out


def _probe(sb: Client, samples: int) -> dict:
    """Blocking. Runs on a worker thread, never on the loop."""
    # Warm the connection first and DISCARD it. supabase-py pools
    # connections; the first call pays a TLS handshake that no production
    # request pays. Including it inflated an earlier probe by ~100ms per
    # call and produced impossible conclusions.
    warmup_ms = None
    try:
        t0 = time.perf_counter()
        sb.table(PROBE_TABLES[0][0]).select(PROBE_TABLES[0][1]) \
            .limit(1).execute()
        warmup_ms = round((time.perf_counter() - t0) * 1000, 2)
    except Exception:  # noqa: BLE001
        pass

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
            "truncated": truncated, "warmup_ms": warmup_ms,
            "phases": _phases(),
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
    # DEMAND, not duty. calls/sec x seconds-per-call is a REQUIRED
    # CAPACITY ratio: at 1.0 one worker is exactly saturated. Above 1.0
    # it is not "175% of the time" — that is impossible and this endpoint
    # printed it twice. It means demand exceeds what one worker can serve,
    # so work queues and latency grows without bound until something
    # gives. Below 1.0 it is the share of wall-clock time spent waiting.
    demand = BACKGROUND_CALLS_PER_SEC * p50 / 1000
    saturated = demand >= 1.0

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

    phases = raw.get("phases") or {}
    connect = phases.get("connect_total_ms")
    origin_ms = round(p50 - connect, 1) if connect is not None else None
    if origin_ms is not None and origin_ms > 50:
        note = (f"{note} Connecting takes {connect}ms but a query takes "
                f"{round(p50,1)}ms, so about {origin_ms}ms is spent inside "
                f"the database platform, not on the network or in this "
                f"code. Making calls concurrent cannot remove that; only "
                f"reducing per-call cost can.")

    return {
        "ok": True,
        "measured": "in_process",
        "warmup_discarded_ms": raw.get("warmup_ms"),
        "connection_phases": phases,
        "origin_time_ms": origin_ms,
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
            "required_workers": round(demand, 2),
            "loop_duty_pct": (round(demand * 100, 1) if not saturated
                              else None),
            "saturated": saturated,
            "meaning": (
                "Below 1.0 this is the share of wall-clock time one worker "
                "spends waiting on the database. At or above 1.0 a single "
                "worker cannot keep up: requests queue and latency grows "
                "until traffic drops. It is never a percentage above 100."
                if saturated else
                "share of wall-clock time one worker spends waiting on the "
                "database, from background traffic alone"),
            "caveat": (
                "Derived from measured p50 and counted call frequencies. "
                "Real duty differs: uvicorn may run several workers, and "
                "not every poll does maximum work.")},
        "serialisation_poll_path": {
            f"{n}_concurrent": round(n * 5 * p50) for n in (1, 3, 5, 10, 20)},
        "verdict": verdict,
        "interpretation": note,
        "writes_performed": 0,
    }
