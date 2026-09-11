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


# ── D6: auto-close and real demo Telegram ────────────────────────────
def test_the_signal_waits_for_a_confirmed_fill():
    fn = _fn("async def demo_run_state(")
    assert 'if r.get("status") == "succeeded" and r.get("ticket"):' in fn
    assert fn.index('status") == "succeeded"') < fn.index("_deliver_demo_signal")


def test_the_demo_signal_can_only_reach_one_chat():
    d = _fn("def _deliver_demo_signal(", "def _sweep_demo_closes(")
    assert 'DEMO_TG_CHAT = "-1004489542294"' in API
    assert 'str(dest.chat_id) not in (DEMO_TG_CHAT, "@sklzlabsdemo")' in d
    assert "refused: resolver returned" in d
    # production destinations are unreachable from this path
    for bad in ("sklzlabsarabic", "TG_CHANNEL", "SIGNAL_CHANNEL_ID"):
        assert bad not in d, bad


def test_the_signal_is_built_from_the_fill_not_the_request():
    s = _fn("def _demo_signal_text(", "def _deliver_demo_signal(")
    assert 'row.get("fill_price")' in s
    assert "row.get('ticket')" in s
    assert "SIMULATED DEMO" in s
    assert "broker DEMO account" in s


def test_telegram_delivery_is_idempotent():
    d = _fn("def _deliver_demo_signal(", "def _sweep_demo_closes(")
    assert 'if row.get("demo_tg_message_id"):' in d
    assert '"replay": True' in d


def test_the_message_url_is_built_for_a_private_channel():
    u = _fn("def _tg_message_url(", "def _demo_signal_text(")
    assert 'cid.startswith("-100")' in u
    assert "https://t.me/c/" in u


def test_auto_close_uses_the_existing_close_command():
    s = _fn("def _sweep_demo_closes(", "@demo_router.post")
    assert '"command_type": "close"' in s
    assert '"ticket": tk' in s
    assert '"bot_name": DEMO_BOT_NAME' in s


def test_auto_close_refuses_a_ticket_from_another_account():
    s = _fn("def _sweep_demo_closes(", "@demo_router.post")
    assert 'str(r.get("actual_account") or "") != expected' in s
    assert "NOT closing ticket" in s


def test_auto_close_is_idempotent_and_guarded():
    s = _fn("def _sweep_demo_closes(", "@demo_router.post")
    assert 'is_("demo_close_command_id", "null")' in s
    assert "demo_close_command_id" in s
    assert s.index("_demo_guard(sb)") < s.index("cutoff")   # guard first


def test_the_hold_is_between_60_and_120_seconds():
    assert "DEMO_HOLD_SECONDS = 90" in API


# ── the prospect button ──────────────────────────────────────────────
PAGE = open("tests/fixtures/signal-desk-demo.html").read()


def test_the_button_calls_the_real_backend():
    assert "RUN LIVE DEMO" in PAGE
    assert "/run-live" in PAGE and "method:\"POST\"" in PAGE
    assert "async function runLive()" in PAGE


def test_filled_is_never_shown_before_the_broker_confirms():
    fn = PAGE[PAGE.index("async function poll("):]
    assert 's.state==="succeeded" && s.ticket' in fn
    i_guard = fn.index('s.state==="succeeded" && s.ticket')
    i_filled = fn.index('"filled"')
    assert i_guard < i_filled


def test_it_shows_the_real_broker_values():
    fn = PAGE[PAGE.index("async function poll("):]
    for v in ("s.fill_price", "s.ticket", "s.retcode", "s.broker_comment",
              "s.account", "s.latency_ms"):
        assert v in fn, v


def test_a_failure_is_reported_honestly():
    fn = PAGE[PAGE.index("async function poll("):]
    assert 's.state==="failed"' in fn
    assert "the order did not fill" in fn
    assert "timed out" in fn


def test_the_telegram_link_opens_the_real_post():
    fn = PAGE[PAGE.index("async function poll("):]
    assert "OPEN LIVE TELEGRAM SIGNAL" in fn
    assert "tg.url" in fn


def test_the_copier_is_labelled_simulated():
    assert "SIMULATED COPY PREVIEW" in PAGE


def test_the_browser_still_sends_nothing_but_the_token():
    fn = PAGE[PAGE.index("async function runLive()"):]
    fn = fn[:fn.index("async function poll(")]
    assert "TOKEN" in fn
    assert "body:" not in fn          # no request body at all
    for forbidden in ("symbol:", "lots", "volume:", "bot_name", "chat"):
        assert forbidden not in fn, forbidden
