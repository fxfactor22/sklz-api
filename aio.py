"""One way to run blocking I/O without stalling the API.

WHY THIS EXISTS
===============
FastAPI serves every request on one event loop. A synchronous network
call inside an `async def` occupies that loop for the whole round trip —
TLS handshake included — and nothing else is served meanwhile. Enabling
demo Telegram delivery did exactly that: requests queued behind each
other until Railway's proxy returned 502 for every endpoint, including
ones that would have refused instantly. The service was never down; it
was never free to answer.

    result = await offload(send_to_telegram, category, text)

The caller still waits for the real outcome — that is the point. Other
requests do not wait behind it.

WHY THERE IS NO TIMEOUT ARGUMENT HERE
=====================================
`asyncio.wait_for(asyncio.to_thread(...), timeout=N)` looks like a
safety net and is a duplicate-delivery hazard: cancelling the *future*
does not stop the *thread*. The blocking call keeps running, Telegram
may well accept the message, and the caller has already been told it
failed. A retry then sends it twice.

So timeouts stay where they can actually interrupt the work — the socket
timeout inside each sender (`urlopen(..., timeout=N)`), which every
delivery path already sets. This helper changes WHERE work runs, never
whether it finishes.
"""
from __future__ import annotations

import asyncio
from typing import Any, Callable, TypeVar

T = TypeVar("T")


async def offload(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run a blocking callable on a worker thread and await its result.

    Exceptions propagate to the caller unchanged, so existing error
    handling keeps working exactly as written. No result is invented and
    nothing is fire-and-forget: a caller that awaits this learns the real
    outcome or the real exception.
    """
    return await asyncio.to_thread(fn, *args, **kwargs)
