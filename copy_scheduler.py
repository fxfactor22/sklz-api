"""SKLZ — copy trading scheduler.

Fan-out only happens when someone calls the poll endpoint. Until now that was
manual, which means a follower could sit for hours while a leader trade went
uncopied. This runs it on a loop.

WHY IT IS BUILT THIS WAY
========================
The failure mode that matters is not the scheduler crashing — that is loud and
obvious. It is the scheduler running happily while every poll silently fails,
so nothing copies and nobody notices for days. Several things today failed
exactly like that.

So this records every run, successful or not, and exposes a health endpoint
that says plainly when the last successful poll was. If fan-out has been
broken for an hour, that should be visible without reading logs.

It also refuses to run two polls at once. Overlapping runs on the same leader
trade could double-copy a position, and idempotent client order IDs are a
safety net rather than a licence to be careless.
"""
from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timezone

_state: dict = {
    "running": False,
    "started_at": None,
    "last_run_at": None,
    "last_success_at": None,
    "last_error": "",
    "runs": 0,
    "failures": 0,
    "consecutive_failures": 0,
    "last_result": {},
}


def interval_seconds() -> int:
    """How often to poll. Short enough that a follower is not left behind,
    long enough not to hammer the exchanges."""
    try:
        return max(15, int(os.environ.get("COPY_POLL_SECONDS", "30")))
    except (TypeError, ValueError):
        return 30


def enabled() -> bool:
    return os.environ.get("COPY_POLL_ENABLED", "1") != "0"


def state() -> dict:
    """Health, in terms that say whether copying actually works right now."""
    s = dict(_state)
    now = time.time()
    last_ok = s.get("last_success_at")

    if not enabled():
        s["health"] = "disabled"
        s["message"] = ("The poll loop is switched off. No leader trade will "
                        "reach any follower until COPY_POLL_ENABLED is set.")
    elif not s["running"]:
        s["health"] = "stopped"
        s["message"] = ("The poll loop is not running. Followers are not being "
                        "copied.")
    elif last_ok is None:
        s["health"] = "never_succeeded"
        s["message"] = ("The loop is running but no poll has yet succeeded. "
                        f"Last error: {s.get('last_error') or 'none recorded'}")
    else:
        age = now - last_ok
        if age > interval_seconds() * 4:
            s["health"] = "stale"
            s["message"] = (f"Last successful poll was {age/60:.0f} minutes ago. "
                            f"Copying is probably broken. "
                            f"Last error: {s.get('last_error') or 'none'}")
        else:
            s["health"] = "ok"
            s["message"] = (f"Polling every {interval_seconds()}s. "
                            f"Last success {age:.0f}s ago.")

    for k in ("started_at", "last_run_at", "last_success_at"):
        if isinstance(s.get(k), float):
            s[k] = datetime.fromtimestamp(s[k], timezone.utc).isoformat()
    return s


async def _one_pass(log=print) -> dict:
    """Run a single poll + fan-out cycle."""
    from db import get_supabase
    from copytrader import executor as EX

    sb = get_supabase()
    fills = EX.poll_leader_fills(sb, log=log)
    out = {"checked_leaders": fills.get("checked_leaders", 0),
           "new_fills": fills.get("new_fills", 0), "fanned_out": 0}

    if fills.get("new_fills"):
        res = EX.fan_out(sb, log=log)
        out["fanned_out"] = res.get("orders", 0)
        out["skipped"] = res.get("skipped", 0)
    return out


async def loop(log=print) -> None:
    """The scheduler itself."""
    if not enabled():
        log("[copy-poll] disabled by COPY_POLL_ENABLED=0")
        return

    _state["running"] = True
    _state["started_at"] = time.time()
    log(f"[copy-poll] started — every {interval_seconds()}s")

    while True:
        try:
            _state["last_run_at"] = time.time()
            _state["runs"] += 1
            result = await _one_pass(log=log)
            _state["last_success_at"] = time.time()
            _state["last_error"] = ""
            _state["consecutive_failures"] = 0
            _state["last_result"] = result

            # only speak when something happened, so the log stays readable
            if result.get("new_fills") or result.get("fanned_out"):
                log(f"[copy-poll] {result.get('new_fills', 0)} new fill(s), "
                    f"{result.get('fanned_out', 0)} order(s) fanned out")

        except Exception as exc:  # noqa: BLE001
            _state["failures"] += 1
            _state["consecutive_failures"] += 1
            _state["last_error"] = f"{type(exc).__name__}: {exc}"[:220]
            n = _state["consecutive_failures"]
            # loud on the first failure and then occasionally, rather than
            # every 30 seconds forever
            if n == 1 or n % 10 == 0:
                log(f"[copy-poll] FAILED ({n} in a row): {_state['last_error']}")

        # back off when it keeps failing, so a broken dependency is not
        # hammered every interval
        delay = interval_seconds()
        if _state["consecutive_failures"] > 3:
            delay = min(300, delay * min(8, _state["consecutive_failures"]))
        await asyncio.sleep(delay)


def start(app, log=print) -> None:
    """Attach the loop to the FastAPI app's startup."""
    @app.on_event("startup")
    async def _start_copy_poll() -> None:  # noqa: ANN202
        if enabled():
            asyncio.create_task(loop(log=log))


# ── health endpoint ─────────────────────────────────────────────────
from fastapi import APIRouter  # noqa: E402

router = APIRouter(prefix="/api/copy", tags=["copytrading"])


@router.get("/poll-health")
async def poll_health() -> dict:
    """Is copying actually working right now?

    PUBLIC on purpose: a follower is entitled to know whether the thing that
    is supposed to be copying their trades is running.
    """
    return state()
