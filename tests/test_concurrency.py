"""P1.3 — blocking delivery must not stall the API."""
import ast, asyncio, sys, time
sys.path.insert(0, ".")
from aio import offload

SENDERS = {"_post_telegram", "deliver", "send_to_telegram",
           "send_to_channels", "_tg", "_send", "post_photo",
           "send_welcome", "_claude", "compose", "compose_debug",
           "_bot_username", "send_signal"}
MODULES = ("signals_engine.py", "signal_lifecycle.py", "sklz_arabic.py")


def _direct_nodes(fn):
    nested = {id(n) for f in ast.walk(fn)
              if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))
              and f is not fn for n in ast.walk(f)}
    out = []
    for node in fn.body:
        out.extend(n for n in ast.walk(node) if id(n) not in nested)
    return out


def test_no_async_path_calls_a_blocking_sender_directly():
    """The regression that produced 502s on every endpoint."""
    offenders = []
    for name in MODULES:
        tree = ast.parse(open(f"./{name}").read())
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.AsyncFunctionDef):
                continue
            nodes = _direct_nodes(fn)
            offloaded = set()
            for n in nodes:
                if isinstance(n, ast.Call):
                    f = n.func
                    nm = (f.id if isinstance(f, ast.Name)
                          else getattr(f, "attr", ""))
                    if nm == "offload":
                        for a in n.args:
                            if isinstance(a, (ast.Name, ast.Attribute)):
                                offloaded.add(getattr(a, "id",
                                                      getattr(a, "attr", "")))
            for n in nodes:
                if isinstance(n, ast.Call):
                    f = n.func
                    if isinstance(f, ast.Attribute) and \
                       isinstance(f.value, ast.Name) and \
                       f.value.id in ("router", "app", "sb", "self"):
                        continue
                    nm = (f.id if isinstance(f, ast.Name)
                          else getattr(f, "attr", ""))
                    if nm in SENDERS and nm not in offloaded:
                        offenders.append(f"{name}:{fn.name}:{nm}")
    assert not offenders, offenders


def test_four_slow_deliveries_overlap():
    """Wall time ~ the slowest call, not the sum of four."""
    async def scenario():
        calls = []

        def slow_telegram(n):
            time.sleep(0.5)          # a slow third party
            calls.append(n)
            return f"msg-{n}"

        t0 = time.monotonic()
        results = await asyncio.gather(*[offload(slow_telegram, i)
                                         for i in range(4)])
        return results, time.monotonic() - t0, calls

    results, elapsed, calls = asyncio.run(scenario())
    assert sorted(results) == ["msg-0", "msg-1", "msg-2", "msg-3"]
    assert len(calls) == 4
    assert elapsed < 1.2, f"serialised: {elapsed:.2f}s for 4x0.5s"


def test_unrelated_requests_stay_responsive_during_a_slow_send():
    async def scenario():
        served = []

        def slow_telegram():
            time.sleep(0.6)
            return True

        async def unrelated_endpoint():
            for _ in range(10):
                served.append(time.monotonic())
                await asyncio.sleep(0.02)

        t0 = time.monotonic()
        await asyncio.gather(offload(slow_telegram), unrelated_endpoint())
        return len(served), time.monotonic() - t0

    count, elapsed = asyncio.run(scenario())
    assert count == 10, f"only {count} unrelated requests were served"
    assert elapsed < 1.0, elapsed


def test_one_logical_delivery_executes_exactly_once():
    """Threads must not turn one send into two."""
    async def scenario():
        sends = []

        def deliver_once(text):
            sends.append(text)
            time.sleep(0.05)
            return True

        await offload(deliver_once, "signal-A")
        return sends

    assert asyncio.run(scenario()) == ["signal-A"]


def test_offload_has_no_timeout_that_could_orphan_a_send():
    """asyncio.wait_for cannot cancel a running thread.

    Cancelling the future while the socket call continues means Telegram
    may accept a message the caller was told had failed — and a retry
    then sends it twice. Timeouts belong at the socket, where they can
    actually stop the work.
    """
    src = open("./aio.py").read()
    # strip the module docstring, which explains the hazard by name
    code = src.split('"""', 2)[2]
    assert "wait_for" not in code, "a timeout here cannot cancel the thread"
    assert "asyncio.to_thread" in code
    body = code[code.index("async def offload("):]
    body = body.split('"""', 2)[2]          # past the function docstring
    assert "timeout" not in body


def test_exceptions_propagate_unchanged():
    async def scenario():
        def boom():
            raise ValueError("telegram refused")
        try:
            await offload(boom)
        except ValueError as exc:
            return str(exc)
        return "no exception"

    assert asyncio.run(scenario()) == "telegram refused"


def test_offload_is_not_fire_and_forget():
    """A caller must learn the real outcome, not a promise."""
    async def scenario():
        def late():
            time.sleep(0.2)
            return "delivered"
        return await offload(late)

    assert asyncio.run(scenario()) == "delivered"
    src = open("./aio.py").read()
    assert "create_task" not in src
    assert "ensure_future" not in src


def test_summary_sends_keep_their_original_order():
    """One offload of the whole block, not one per destination."""
    src = open("./signal_lifecycle.py").read()
    fn = src[src.index("async def summary_loop("):]
    assert "def _send_all()" in fn
    assert "await offload(_send_all)" in fn
    assert fn.count("await offload(") == 1


def test_routing_and_arabic_behaviour_unchanged():
    """This phase moved WHERE work runs, not what it does."""
    src = open("./signals_engine.py").read()
    assert "offload(send_to_telegram, category, format_signal(sig))" in src
    ar = open("./sklz_arabic.py").read()
    assert 'RoutingScope(language="ar")' in ar          # same scope
    assert "os.environ.get(\"TG_ARABIC_CHAT\"" not in ar  # no env regression
    rt = open("./routing.py").read()
    env = rt[rt.index("class EnvTelegramDestinationResolver"):]
    assert env.index('purpose == "demo_signal"') < \
        env.index('scope.language == "ar"')             # D1.3 order intact


# ── P1.4: the remaining async paths ──────────────────────────────────
P14_MODULES = ("tgbot.py", "alerts_api.py", "provisioning.py")
P14_SENDERS = {"send", "_api", "_send_telegram", "diag", "create_tenant",
               "owner_invite", "lifecycle", "status",
               "probe_bad_signature", "probe_signed_invalid_body"}


def _offenders(modules, senders):
    out = []
    for name in modules:
        tree = ast.parse(open(f"./{name}").read())
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.AsyncFunctionDef):
                continue
            offloaded = set()
            for n in ast.walk(fn):
                if isinstance(n, ast.Call):
                    f = n.func
                    nm = (f.id if isinstance(f, ast.Name)
                          else getattr(f, "attr", ""))
                    if nm == "offload":
                        for a in n.args:
                            offloaded.add(getattr(a, "id",
                                                  getattr(a, "attr", "")))
            for n in ast.walk(fn):
                if isinstance(n, ast.Call):
                    f = n.func
                    if isinstance(f, ast.Attribute) and \
                       isinstance(f.value, ast.Name) and \
                       f.value.id in ("router", "app", "sb", "self"):
                        continue
                    nm = (f.id if isinstance(f, ast.Name)
                          else getattr(f, "attr", ""))
                    if nm in senders and nm not in offloaded:
                        out.append(f"{name}:{fn.name}:{nm}")
    return out


def test_no_remaining_async_path_blocks_the_loop():
    assert _offenders(P14_MODULES, P14_SENDERS) == []


def test_funnel_reply_order_is_preserved():
    """Each send is awaited in place, so a conversation cannot reorder."""
    src = open("./tgbot.py").read()
    assert "async def _asend(" in src and "async def _aapi(" in src
    fn = src[src.index("async def webhook("):]
    # every send in the handler is awaited, none fired concurrently
    assert "await _asend(" in fn
    assert "gather(" not in fn and "create_task(" not in fn


def test_webhook_still_returns_the_same_shape():
    src = open("./tgbot.py").read()
    fn = src[src.index("async def webhook("):]
    assert '{"ok": True}' in fn


def test_provisioning_pure_helpers_are_not_offloaded():
    """Hashing and redaction are CPU-only; a thread hop would be waste."""
    src = open("./provisioning.py").read()
    for pure in ("canonical_request_hash", "redact", "secret_fingerprint"):
        assert f"offload(iskra.{pure}" not in src, pure


def test_idempotency_order_is_unchanged_by_offloading():
    """Intent still lands before the network call."""
    src = open("./provisioning.py").read()
    fn = src[src.index("async def provision("):]
    fn = fn[:fn.index("\n# ── owner invite")]
    assert fn.index('table("provisioning_intents").insert') < \
        fn.index("offload(iskra.create_tenant")


def test_three_concurrent_webhook_updates_overlap():
    async def scenario():
        def slow_send(_):
            time.sleep(0.4)
            return {"ok": True}

        t0 = time.monotonic()
        await asyncio.gather(*[offload(slow_send, i) for i in range(3)])
        return time.monotonic() - t0

    assert asyncio.run(scenario()) < 0.9
