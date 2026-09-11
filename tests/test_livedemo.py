"""D5 — a cold click must never reach the funded account."""
import sys
sys.path.insert(0, ".")
API = open("./orders_api.py").read()
SQL = open("migrations/D5-migration.sql").read()


def _fn(name, end=None):
    s = API[API.index(name):]
    return s[:s.index(end)] if end and end in s else s


# ── the browser decides nothing ──────────────────────────────────────
def test_the_browser_cannot_name_anything():
    fn = _fn("async def run_live_demo(", "@demo_router.get")
    # the handler takes a token and a request. No body model at all.
    sig = fn[:fn.index(")")]
    for forbidden in ("bot_name", "account", "login", "symbol", "lots",
                      "volume", "chat", "destination", "body"):
        assert forbidden not in sig, forbidden
    # and every trade parameter is a module constant
    for const in ("DEMO_BOT_NAME", "DEMO_SYMBOL", "DEMO_LOT"):
        assert const in API, const
    assert 'DEMO_SYMBOL = "EURUSD"' in API
    assert "DEMO_LOT = 0.01" in API


def test_the_order_is_built_from_constants_not_input():
    fn = _fn("async def run_live_demo(", "@demo_router.get")
    row = fn[fn.index('row = {'):fn.index("def _insert")]
    assert '"bot_name": DEMO_BOT_NAME' in row
    assert '"symbol": DEMO_SYMBOL' in row
    assert '"lots": DEMO_LOT' in row
    assert "body." not in row and "request." not in row


# ── identity as observed, not as labelled ────────────────────────────
def test_it_refuses_unless_the_runner_proved_its_login():
    g = _fn("def _demo_guard(", "@demo_router.post")
    assert 'observed != expected' in g
    assert "refusing: the demo Runner is attached to account" in g
    # observed comes from bot_state, which only the Runner writes
    assert "_runner_identity(sb)" in g
    assert '"last_account"' in g


def test_bot_state_is_written_by_the_runner_only():
    bi = open("./bot_ingest.py").read()
    assert "_note_account(sb, bot_name, account, server)" in bi
    assert "revoke all on public.bot_state from anon, authenticated" in SQL


def test_an_unset_login_refuses_rather_than_defaulting():
    g = _fn("def _demo_guard(", "@demo_router.post")
    assert "SKLZ_DEMO_MT5_LOGIN is not configured" in g
    assert 'return _os.environ.get("SKLZ_DEMO_MT5_LOGIN", "").strip()' in API
    # no fallback value anywhere
    assert "52952532" not in API


def test_a_silent_or_stale_runner_refuses():
    g = _fn("def _demo_guard(", "@demo_router.post")
    assert "has never reported in" in g
    assert "has not reported an MT5 login" in g
    assert "DEMO_STALE_POLL_SECONDS" in g and "may be offline" in g


def test_the_feature_is_off_unless_explicitly_enabled():
    assert 'SKLZ_LIVE_DEMO_ENABLED", "0") == "1"' in API
    g = _fn("def _demo_guard(", "@demo_router.post")
    assert g.index("_demo_enabled()") < g.index("_demo_login()")


def test_the_server_is_checked_too():
    g = _fn("def _demo_guard(", "@demo_router.post")
    assert "SKLZ_DEMO_MT5_SERVER" in g
    assert "which is not" in g


# ── it cannot reach the funded runner ────────────────────────────────
def test_the_command_can_only_target_the_demo_bot():
    assert 'DEMO_BOT_NAME = "sklz-demo"' in API
    fn = _fn("async def run_live_demo(", "@demo_router.get")
    assert fn.count("DEMO_BOT_NAME") >= 1
    # no other bot name is constructible in this path
    assert "bot_name=" not in fn


def test_expiry_is_checked_before_anything_executes():
    fn = _fn("async def run_live_demo(", "@demo_router.get")
    assert fn.index("read_demo_link(token, sb)") < fn.index("_demo_guard(sb)")
    assert fn.index("_demo_guard(sb)") < fn.index('table("bot_orders").insert')


def test_runs_are_capped_per_token():
    assert "DEMO_RUNS_PER_TOKEN = 3" in API
    fn = _fn("async def run_live_demo(", "@demo_router.get")
    assert "demo_runs_exhausted" in fn
    assert fn.index("used >= DEMO_RUNS_PER_TOKEN") < fn.index("def _insert")


# ── honesty about what happened ──────────────────────────────────────
def test_state_reports_the_broker_not_a_guess():
    fn = _fn("async def demo_run_state(")
    for field in ("ticket", "fill_price", "retcode", "broker_comment",
                  "actual_account"):
        assert field in fn, field
    assert '"state": r.get("status")' in fn      # never invented


def test_latency_comes_from_recorded_timestamps():
    fn = _fn("async def demo_run_state(")
    assert "broker_confirmed_at" in fn and "created_at" in fn
    assert '"latency_ms"' in fn
    assert 'out["latency_ms"] = None' in fn      # unknown stays unknown


def test_a_prospect_cannot_read_another_tokens_run():
    fn = _fn("async def demo_run_state(")
    assert '.eq("command_id", command_id).eq("demo_token", tok)' in fn
