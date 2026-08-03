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
    """Poll every approved leader, then fan out anything new.

    poll_leader_fills works per leader and needs that leader's own exchange
    adapter, so this walks the approved list rather than making one call.
    """
    from db import get_supabase
    from copytrader import executor as EX
    from copytrader.connections_api import _load_adapter

    sb = get_supabase()
    out = {"checked_leaders": 0, "new_fills": 0, "fanned_out": 0, "skipped": 0}

    try:
        leaders = (sb.table("copy_leaders").select("*")
                   .eq("approval_status", "approved")
                   .neq("suspended", True).execute()).data or []
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"could not list leaders: {str(exc)[:140]}") from exc

    for leader in leaders:
        conn_id = leader.get("connection_id")
        uid = leader.get("user_id")
        if not conn_id or not uid:
            continue
        try:
            adapter = _load_adapter(sb, str(uid), str(conn_id))
        except Exception as exc:  # noqa: BLE001
            log(f"[copy-poll] leader {leader.get('display_name')}: "
                f"could not load exchange ({type(exc).__name__})")
            continue

        out["checked_leaders"] += 1
        try:
            fills = EX.poll_leader_fills(adapter, sb, str(leader["id"]), log=log)
        except Exception as exc:  # noqa: BLE001
            log(f"[copy-poll] leader {leader.get('display_name')}: "
                f"poll failed ({type(exc).__name__}: {exc})")
            continue

        for fill in fills or []:
            out["new_fills"] += 1
            try:
                results = EX.fan_out(sb, fill, _load_adapter, log=log)
            except Exception as exc:  # noqa: BLE001
                log(f"[copy-poll] fan-out failed for {fill.get('symbol')}: "
                    f"{type(exc).__name__}: {exc}")
                continue
            for r in results or []:
                if (r or {}).get("status") in ("placed", "simulated"):
                    out["fanned_out"] += 1
                else:
                    out["skipped"] += 1
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
