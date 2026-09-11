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


def test_the_hold_allows_interaction_then_cleans_up():
    """90s was right for fire-and-forget and wrong once the desk became
    interactive: a prospect needs time to modify, breakeven and close."""
    assert "DEMO_HOLD_SECONDS = 600" in API
    assert "sweep" in API.lower()          # abandoned positions still close


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


# ── a funded trade must never reach the demo Runner ──────────────────
def test_manual_orders_refuse_the_demo_runner():
    """A gold order meant for the funded 50k reached the demo account
    because the dashboard offered sklz-demo as a target."""
    bi = open("./bot_ingest.py").read()
    fn = bi[bi.index("async def place_order("):]
    fn = fn[:fn.index("\n@router") if "\n@router" in fn else len(fn)]
    assert 'SKLZ_DEMO_BOT_NAME' in fn
    assert "that is the demo Runner" in fn
    # the refusal happens BEFORE the insert
    assert fn.index("SKLZ_DEMO_BOT_NAME") < fn.index('table("bot_orders").insert')


def test_the_dashboard_hides_demo_runners():
    h = open("tests/fixtures/bot.html").read()
    assert "DEMO_BOT_RE" in h
    import re
    rx = re.compile(r'^sklz-demo|(^|[^a-z])demo([^a-z]|$)', re.I)
    assert rx.search("sklz-demo")
    assert not rx.search("Learning Runner [live]")
    assert "no live runner" in h          # never silently empty


# ── sales readiness: honest labelling and fresh tokens ───────────────
def test_every_step_declares_real_or_simulated():
    h = open("tests/fixtures/signal-desk-demo.html").read()
    assert h.count(">REAL<") >= 4
    assert h.count(">SIMULATED PREVIEW<") >= 2
    assert "ILLUSTRATIVE FIGURES" in h
    # the summary line says which is which
    assert "no subscriber\n          account executed anything" in h or \
        "no subscriber" in h


def test_a_fresh_token_reports_no_runs_used():
    api = open("orders_api.py").read()
    fn = api[api.index("async def list_demo_links("):]
    assert 'r["runs_used"] = used.get(r["token"], 0)' in fn
    assert '"runs_remaining"' in fn
    # counted from actual orders, so a new token can only be 0
    assert 'eq("demo_kind", "market")' in fn


def test_revoke_is_admin_only_and_keeps_history():
    api = open("orders_api.py").read()
    fn = api[api.index("async def revoke_demo_link("):]
    assert "rules.is_platform_admin(user)" in fn
    assert '"revoked": True' in fn
    assert ".delete()" not in fn


# ── Phase 1: control desk ────────────────────────────────────────────
def test_a_prospect_can_only_act_on_its_own_ticket():
    """Without this, any ticket number in a request could be closed."""
    api = open("orders_api.py").read()
    fn = api[api.index("async def _owned_ticket("):]
    fn = fn[:fn.index("@demo_router.post")]
    assert '.eq("demo_token", tok).eq("ticket", ticket)' in fn
    assert "not_your_position" in fn
    # and the fill must have happened on the demo account
    assert '!= _demo_login()' in fn and "account_mismatch" in fn


def test_control_verifies_ownership_before_queueing():
    api = open("orders_api.py").read()
    fn = api[api.index("async def demo_control("):]
    fn = fn[:fn.index("@demo_router.get")]
    assert fn.index("_owned_ticket(sb, tok") < fn.index('table("bot_orders").insert')
    # every command is bound to the demo runner
    assert '"bot_name": DEMO_BOT_NAME' in fn
    # the browser cannot name a runner or an account
    sig = fn[:fn.index(")")]
    for forbidden in ("bot_name", "account", "login"):
        assert forbidden not in sig, forbidden


def test_control_actions_map_to_existing_a3_commands():
    api = open("orders_api.py").read()
    for a in ("market", "modify", "breakeven", "close", "positions"):
        assert f'"command_type": "{a}"' in api, a
    # nothing pending was added
    # no pending ORDER TYPES were added ("pending" alone is the A3 row
    # status and appears legitimately)
    for banned in ("buy_limit", "sell_limit", "buy_stop", "sell_stop",
                   "ORDER_TYPE_BUY_LIMIT", "TRADE_ACTION_PENDING"):
        assert banned not in api, banned


def test_the_interaction_window_is_ten_minutes():
    api = open("orders_api.py").read()
    assert "DEMO_HOLD_SECONDS = 600" in api
    # the sweep still backstops abandoned positions
    assert "cutoff" in api and "DEMO_HOLD_SECONDS" in api


def test_a_closed_ticket_is_not_closed_twice():
    api = open("orders_api.py").read()
    fn = api[api.index("def _sweep_demo_closes("):]
    assert 'is_("demo_close_command_id", "null")' in fn


# ── Phase 2: trailing visibility ─────────────────────────────────────
def test_trailing_is_read_only_for_a_public_token():
    api = open("orders_api.py").read()
    fn = api[api.index("async def demo_trailing("):]
    assert '"read_only": True' in fn
    for write in ("insert(", "update(", "os.environ[", "_os.environ["):
        assert write not in fn, write


def test_the_broker_claim_is_stated_only_as_audited():
    """The engine-side proof lives in the engine's own suite
    (test_positions_command_is_read_only, and trailing.py's docstring).
    This repo can only assert what this repo says."""
    api = open("orders_api.py").read()
    assert "continues if" in api and "browser is closed" in api
    assert '"where": "broker"' in api


def test_live_sl_is_requested_from_the_runner_not_remembered():
    """The API must ASK for positions rather than report the SL it stored
    when the order was placed — trailing moves the broker-side stop."""
    api = open("orders_api.py").read()
    assert '"command_type": "positions"' in api
    assert '"demo_kind": "positions"' in api
    # and it is a real A3 command, queued like any other
    fn = api[api.index("async def demo_control("):]
    assert '"bot_name": DEMO_BOT_NAME' in fn


# ── Phase 3: AI communication ────────────────────────────────────────
def test_the_ai_receives_facts_as_data_and_may_not_invent():
    api = open("orders_api.py").read()
    fn = api[api.index("def _ai_draft("):]
    fn = fn[:fn.index("@demo_router.post")]
    assert "ONLY state facts present in the DATA" in fn
    assert "Never invent or estimate" in fn
    assert "BROKER DEMO ACCOUNT" in fn
    assert "json.dumps(facts" in fn


def test_facts_come_from_the_ledger_not_the_request():
    api = open("orders_api.py").read()
    fn = api[api.index("def _verified_facts("):]
    fn = fn[:fn.index("def _ai_draft(")]
    assert 'eq("demo_token", tok)' in fn
    assert '"status") == "succeeded"' in fn
    assert "fill_price" in fn and "retcode" in fn


def test_policy_gates_the_draft_and_the_edited_text():
    api = open("orders_api.py").read()
    draft = api[api.index("async def demo_ai_draft("):]
    assert "policy.validate(draft" in draft
    send = api[api.index("async def demo_ai_send("):]
    assert "policy.validate(text" in send
    assert "policy_refused" in send


def test_ai_send_can_only_reach_the_demo_channel():
    api = open("orders_api.py").read()
    fn = api[api.index("def _deliver_demo_signal_text("):]
    assert 'RoutingScope(purpose="demo_signal")' in fn
    assert 'not in (DEMO_TG_CHAT, "@sklzlabsdemo")' in fn
    assert "refused: resolver returned" in fn


def test_every_ai_message_is_marked_demo():
    api = open("orders_api.py").read()
    fn = api[api.index("async def demo_ai_send("):]
    assert 'if "DEMO" in text.upper()' in fn
    assert "DEMO ACCOUNT" in fn


# ── symbol choice, bounded by an allowlist ───────────────────────────
def _sym_fn():
    import ast as _a
    src = open("orders_api.py").read()
    tree = _a.parse(src)
    keep = [n for n in tree.body
            # `str | None` in a signature is evaluated eagerly on Python
            # 3.9 unless this import comes with it. orders_api.py has it;
            # extracting nodes without it broke the harness, not the code.
            if (isinstance(n, _a.ImportFrom) and n.module == "__future__")
            or (isinstance(n, _a.FunctionDef) and n.name == "_demo_symbol")
            or (isinstance(n, _a.Assign)
                and getattr(n.targets[0], "id", "") in ("DEMO_SYMBOL", "DEMO_LOT"))
            or (isinstance(n, _a.AnnAssign)
                and getattr(n.target, "id", "") == "DEMO_SYMBOLS")]
    ns = {}
    exec(compile(_a.Module(body=keep, type_ignores=[]), "x", "exec"), ns)
    return ns


def test_an_unlisted_symbol_never_reaches_the_broker():
    """The list is the permission. Anything else falls back rather than
    trading an instrument nobody sized."""
    ns = _sym_fn()
    f = ns["_demo_symbol"]
    for bad in ("US30", "TSLA", "'; DROP TABLE bot_orders;--", "", None,
                "EURUSD.nx", "XAUUSD "):
        sym, lot = f(bad)
        assert sym in ns["DEMO_SYMBOLS"], (bad, sym)
    assert f("US30")[0] == "EURUSD"


def test_a_listed_symbol_is_accepted_case_insensitively():
    f = _sym_fn()["_demo_symbol"]
    assert f("btcusd")[0] == "BTCUSD"
    assert f(" XAUUSD ")[0] == "XAUUSD"


def test_lot_size_is_per_symbol_and_server_chosen():
    """0.01 means very different money across instruments."""
    ns = _sym_fn()
    f, table = ns["_demo_symbol"], ns["DEMO_SYMBOLS"]
    for sym in table:
        assert f(sym)[1] == table[sym]["lot"]
    # and the browser cannot ask for a lot at all
    api = open("orders_api.py").read()
    fn = api[api.index("class ControlIn"):]
    fn = fn[:fn.index("\n\n")]
    for forbidden in ("lot", "volume", "size"):
        assert forbidden not in fn, forbidden


def test_crypto_is_offered_so_a_weekend_click_can_trade():
    table = _sym_fn()["DEMO_SYMBOLS"]
    always = [k for k, v in table.items() if v.get("always_open")]
    assert "BTCUSD" in always


def test_the_symbol_list_is_readable_by_the_demo():
    api = open("orders_api.py").read()
    assert 'async def demo_symbols(' in api
    fn = api[api.index("async def demo_symbols("):]
    assert "read_demo_link(token, sb)" in fn      # token still required


def test_positions_does_not_require_a_ticket():
    """'What is open?' is a valid question with an empty answer.
    Requiring a ticket meant the read could never be made without
    already knowing what it would return."""
    api = open("orders_api.py").read()
    fn = api[api.index("async def demo_control("):]
    fn = fn[:fn.index("@demo_router.get")]
    i_pos = fn.index('elif action == "positions":')
    i_req = fn.index("this action needs the ticket")
    assert i_pos < i_req, "positions must be handled before the ticket check"
