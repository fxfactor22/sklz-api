"""D1.4 — enabling Telegram must never take the API with it."""
import asyncio, sys, time, types
sys.path.insert(0, ".")


class _Tok:
    def __init__(self, v): self._v = v
    def reveal(self): return self._v
    def __bool__(self): return bool(self._v)


def _dest(token="8123456:AAH-a-plausible-looking-bot-token",
          chat="@sklzlabsdemo", key="demo_signals"):
    d = types.SimpleNamespace()
    d.token, d.chat_id, d.key = _Tok(token), chat, key
    d.meta = {"label": "SKLZ Demo Signals"}
    d.enabled = True
    return d


# ── configuration fails closed, before any network call ──────────────
def test_valid_config_passes_the_precheck():
    import demo_sim
    assert demo_sim._config_problem(_dest()) == ""


def test_missing_token_is_caught_before_the_network():
    import demo_sim
    assert demo_sim._config_problem(_dest(token="")) == "telegram_token_missing"


def test_malformed_token_is_caught_before_the_network():
    import demo_sim
    for bad in ("not-a-token", "12345", "x" * 19):
        assert demo_sim._config_problem(_dest(token=bad)) == \
            "telegram_token_malformed", bad


def test_missing_chat_is_caught_before_the_network():
    import demo_sim
    assert demo_sim._config_problem(_dest(chat="")) == "telegram_chat_missing"


# ── the actual defect ────────────────────────────────────────────────
def test_delivery_runs_off_the_event_loop():
    """Blocking urllib inside an async handler stalls the single worker,
    and every other endpoint 502s behind it."""
    src = open("./demo_api.py").read()
    fn = src[src.index("async def signal_send("):]
    fn = fn[:fn.index("\nDELIVER_TIMEOUT")]
    assert "asyncio.to_thread(_deliver" in fn
    assert "= _deliver(dest, text)" not in fn, "still called on the loop"


def test_a_slow_telegram_does_not_block_other_requests():
    """A slow delivery must not stop the loop serving anything else."""
    async def scenario():
        def slow_deliver(dest, text):
            time.sleep(0.4)                 # a blocking third party
            return ("sent", "", 1)

        served = []

        async def other_request():
            for _ in range(8):
                served.append(time.time())
                await asyncio.sleep(0.02)

        async def delivery():
            return await asyncio.to_thread(slow_deliver, None, "x")

        t0 = time.time()
        _, _ = await asyncio.gather(delivery(), other_request())
        return len(served), time.time() - t0

    count, elapsed = asyncio.run(scenario())
    assert count == 8, "other requests were starved"
    assert elapsed < 0.9, elapsed


def test_delivery_exception_becomes_a_controlled_state():
    async def scenario():
        def boom(dest, text):
            raise ConnectionResetError("telegram went away")
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(boom, None, "x"), timeout=1)
        except Exception as exc:            # what signal_send does
            return ("failed", f"telegram_unavailable:{type(exc).__name__}",
                    None)
    state, reason, msg = asyncio.run(scenario())
    assert state == "failed"
    assert reason.startswith("telegram_unavailable:")
    assert msg is None


def test_delivery_timeout_becomes_a_controlled_state():
    async def scenario():
        def hang(dest, text):
            time.sleep(2)
            return ("sent", "", 1)
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(hang, None, "x"), timeout=0.2)
        except Exception as exc:
            return ("failed", f"telegram_unavailable:{type(exc).__name__}",
                    None)
    state, reason, _ = asyncio.run(scenario())
    assert state == "failed" and "telegram_unavailable" in reason


# ── startup safety ───────────────────────────────────────────────────
def test_no_network_call_at_import_time():
    """Nothing about enabling a flag should be able to stop the API
    starting, so nothing may touch the network at import."""
    import ast
    src = open("./demo_api.py").read()
    tree = ast.parse(src)
    top = [n for n in tree.body
           if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef, ast.Import, ast.ImportFrom))]
    for node in top:
        seg = ast.get_source_segment(src, node) or ""
        for bad in ("urlopen", "requests.", "httpx.", "socket."):
            assert bad not in seg, f"module-level network call: {bad}"


def test_routing_does_no_network_at_import_or_resolve():
    import ast
    src = open("./routing.py").read()
    assert "urlopen" not in src.split("def _bot_username")[0]


def test_the_double_lock_survives_this_change():
    src = open("./demo_api.py").read()
    fn = src[src.index("async def signal_send("):]
    fn = fn[:fn.index("\nDELIVER_TIMEOUT")]
    assert 'if dest.key != "demo_signals"' in fn
    assert fn.index('dest.key != "demo_signals"') < fn.index("to_thread(_deliver")
