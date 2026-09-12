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
    """A market signal still needs a confirmed fill AND a ticket. A
    positions read legitimately has neither — it answers "what is open?"
    — and gating it on a ticket kept settlement, reconciliation and
    trailing from ever running."""
    fn = _fn("async def demo_run_state(")
    assert 'r.get("status") == "succeeded" and (r.get("ticket")' in fn
    assert '_kind == "positions")' in fn
    assert fn.index('status") == "succeeded"') < fn.index("_deliver_demo_signal")
    # the market branch is still reached only with a ticket
    market = fn[fn.index('if kind == "market":'):]
    assert market.index("_deliver_demo_signal") < market.index("elif kind ==")


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
    """The single RUN LIVE DEMO button became BUY/SELL with an instrument
    selector, so a prospect can choose before trading."""
    assert "async function runLive(side)" in PAGE
    assert '"/control"' in PAGE or "/control`" in PAGE
    assert 'id="symSel"' in PAGE
    assert "runLive(\'buy\')" in PAGE
    assert "runLive(\'sell\')" in PAGE
    assert "BUY" in PAGE and "SELL" in PAGE


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


def test_the_browser_names_only_an_allowlisted_symbol():
    """The browser now chooses an instrument — but only from the list the
    server serves, and it still names no runner, account or lot."""
    fn = PAGE[PAGE.index("async function runLive(side)"):]
    fn = fn[:fn.index("\nasync function")]
    assert "TOKEN" in fn
    assert "symbol:sym" in fn                  # the one thing it may pick
    for forbidden in ("bot_name", "account", "login", "lots", "volume:",
                      "chat"):
        assert forbidden not in fn, forbidden
    # and the options come from the server, never hard-coded here
    assert "/symbols" in PAGE
    assert "sd.symbols.map" in PAGE


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
    # bound the slice to THIS function — reading to end-of-file caught a
    # later helper's legitimate write
    fn = fn[:fn.index("\n\n\n")] if "\n\n\n" in fn else fn
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
    # succeeded-only is now enforced by the query, not after the limit
    assert 'eq("status", "succeeded")' in fn
    assert "fill_price" in fn and "retcode" in fn


def test_the_status_and_kind_filters_run_before_the_limit():
    """Filtering after .limit(25) let positions reads evict real fills."""
    api = open("orders_api.py").read()
    fn = api[api.index("def _verified_facts("):]
    fn = fn[:fn.index("def _ai_draft(")]
    i_status = fn.index('eq("status", "succeeded")')
    i_kind = fn.index('in_("demo_kind"')
    i_limit = fn.index(".limit(25)")
    assert i_status < i_limit, "status must be filtered before the limit"
    assert i_kind < i_limit, "demo_kind must be filtered before the limit"


class _FactsQ:
    def __init__(self, rows):
        self.rows, self.f, self.n = rows, {}, 25

    def select(self, *a, **k):
        return self

    def eq(self, col, val):
        self.f[col] = val
        return self

    def in_(self, col, vals):
        self.f[col] = set(vals)
        return self

    def order(self, *a, **k):
        return self

    def limit(self, n):
        self.n = n
        return self

    def execute(self):
        keep = [r for r in self.rows
                if r.get("demo_token") == self.f.get("demo_token")
                and r.get("status") == self.f.get("status")
                and r.get("demo_kind") in self.f.get("demo_kind", set())]
        keep.sort(key=lambda r: r["created_at"], reverse=True)
        return type("R", (), {"data": keep[:self.n]})()


class _FactsSB:
    def __init__(self, rows):
        self.rows = rows

    def table(self, _name):
        return _FactsQ(self.rows)


def _ledger():
    """Two old fills, a failed and a pending attempt, one lifecycle event,
    then far more recent positions reads than the 25-row window holds."""
    rows = [
        {"created_at": 1, "demo_token": "qa", "demo_kind": "market",
         "status": "succeeded", "ticket": 1928008716, "symbol": "BTCUSD"},
        {"created_at": 2, "demo_token": "qa", "demo_kind": "market",
         "status": "succeeded", "ticket": 1928013968, "symbol": "BTCUSD"},
        {"created_at": 3, "demo_token": "qa", "demo_kind": "market",
         "status": "failed", "ticket": None},
        {"created_at": 4, "demo_token": "qa", "demo_kind": "market",
         "status": "pending", "ticket": None},
        {"created_at": 5, "demo_token": "qa", "demo_kind": "modify",
         "status": "succeeded", "ticket": 1928013968},
    ]
    rows += [{"created_at": 100 + i, "demo_token": "qa",
              "demo_kind": "positions", "status": "succeeded"}
             for i in range(60)]
    return rows


def test_positions_polling_cannot_hide_a_real_fill():
    """60 newer positions reads must not evict the trades."""
    import orders_api

    facts = orders_api._verified_facts(_FactsSB(_ledger()), "qa", None)
    assert {r["ticket"] for r in facts["trades"]} == {1928008716, 1928013968}


def test_a_failed_or_pending_market_row_is_not_a_verified_trade():
    import orders_api

    facts = orders_api._verified_facts(_FactsSB(_ledger()), "qa", None)
    assert all(r["status"] == "succeeded" for r in facts["trades"])
    assert None not in [r["ticket"] for r in facts["trades"]]


def test_lifecycle_events_survive_the_narrowed_query():
    """Trades and events are fetched separately so neither evicts the
    other — filtering the one query to market would have emptied this."""
    import orders_api

    facts = orders_api._verified_facts(_FactsSB(_ledger()), "qa", None)
    assert [r["demo_kind"] for r in facts["events"]] == ["modify"]


def test_a_ticket_filter_still_narrows_to_that_trade():
    import orders_api

    facts = orders_api._verified_facts(_FactsSB(_ledger()), "qa", 1928013968)
    assert [r["ticket"] for r in facts["trades"]] == [1928013968]


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


def test_a_positions_read_survives_the_result_ingest():
    """The Runner answered with a list and the ingest had nowhere to put
    it, so a successful read returned nothing."""
    bi = open("bot_ingest.py").read()
    assert "positions: list = []" in bi
    assert '"positions" in body.model_fields_set' in bi
    api = open("orders_api.py").read()
    # The list still leaves the endpoint — it is now narrowed to the
    # positions this token owns on the way out, which is a filter on the
    # same value, not a drop of it.
    assert '"positions": await offload(_owned_positions, sb, tok,' in api
    assert "def _owned_positions(" in api
    sql = open("migrations/D7-migration.sql").read()
    assert "add column if not exists positions jsonb" in sql


class _FakeQ:
    def __init__(self, rows, boom=False):
        self.rows, self.boom = rows, boom

    def select(self, *a, **k):
        return self

    def eq(self, *a, **k):
        return self

    def execute(self):
        if self.boom:
            raise RuntimeError("db unavailable")
        return type("R", (), {"data": self.rows})()


class _FakeSB:
    def __init__(self, rows, boom=False):
        self.rows, self.boom = rows, boom

    def table(self, _name):
        return _FakeQ(self.rows, self.boom)


_FOREIGN = {"ticket": 1927969355, "symbol": "EURJPY", "side": "sell"}
_OWNED = {"ticket": 1928013968, "symbol": "BTCUSD", "side": "sell"}


def test_a_position_this_token_did_not_open_is_never_returned():
    """A foreign position on the demo master was shown to the prospect as
    their own trade, because the page binds to the first row it is given.
    Ownership is the ledger's answer, not the list order."""
    import orders_api

    sb = _FakeSB([])                      # this token owns nothing
    assert orders_api._owned_positions(sb, "tok", [_FOREIGN]) == []

    sb = _FakeSB([{"ticket": 1928013968}])
    # foreign listed FIRST — order must not confer ownership
    assert orders_api._owned_positions(
        sb, "tok", [_FOREIGN, _OWNED]) == [_OWNED]


def test_ownership_fails_closed_when_the_ledger_cannot_be_read():
    """An unreadable ledger means nothing is proven owned, so nothing is
    actionable. It must not fall back to showing everything."""
    import orders_api

    sb = _FakeSB([], boom=True)
    assert orders_api._owned_positions(sb, "tok", [_FOREIGN, _OWNED]) == []


def test_a_null_ticket_in_the_ledger_owns_nothing():
    import orders_api

    sb = _FakeSB([{"ticket": None}])
    assert orders_api._owned_positions(sb, "tok", [_FOREIGN]) == []


# ── control desk UI ──────────────────────────────────────────────────
def test_the_page_exposes_every_control():
    h = open("tests/fixtures/signal-desk-demo.html").read()
    # the modify control is now a labelled Stop/Target pair with Apply
    for label in (">Stop<", ">Target<", ">Apply<", "Move to breakeven",
                  "Close trade"):
        assert label in h, label
    assert 'id="posPanel"' in h and 'id="trailBox"' in h


def test_controls_wait_for_the_broker_before_changing_state():
    """No button may report success on its own say-so."""
    h = open("tests/fixtures/signal-desk-demo.html").read()
    fn = h[h.index("async function ctl(action"):h.index("async function ctlModify")]
    assert "await awaitCmd(q.command_id)" in fn
    assert 's.state==="succeeded"' in fn
    assert fn.index("awaitCmd") < fn.index("confirmed by the broker")


def test_the_panel_shows_only_what_the_broker_reports():
    h = open("tests/fixtures/signal-desk-demo.html").read()
    fn = h[h.index("async function refreshPositions"):]
    fn = fn[:fn.index("async function poll(")]
    for field in ("mine.ticket", "mine.symbol", "mine.side", "mine.volume",
                  "mine.entry", "mine.sl", "mine.tp", "mine.profit"):
        assert field in fn, field
    assert "s.positions" in fn          # straight from the broker read


def test_the_trailing_box_shows_the_live_stop():
    h = open("tests/fixtures/signal-desk-demo.html").read()
    assert "Trailing stop active" in h
    assert "Current stop" in h and "mine.sl" in h
    assert "/trailing" in h


def test_every_control_acts_only_on_the_tokens_own_ticket():
    h = open("tests/fixtures/signal-desk-demo.html").read()
    fn = h[h.index("async function ctl(action"):h.index("async function refreshPositions")]
    assert "ticket:LIVE_TICKET" in fn
    assert "if(!LIVE_TICKET) return;" in fn
    # and LIVE_TICKET only ever comes from a confirmed fill or a real read
    assert "LIVE_TICKET=s.ticket" in h and "LIVE_TICKET=mine.ticket" in h


def test_no_pending_order_controls_were_added():
    h = open("tests/fixtures/signal-desk-demo.html").read().lower()
    for banned in ("buy limit", "sell limit", "buy stop", "sell stop"):
        assert banned not in h, banned


def test_modify_uses_inline_fields_not_browser_prompts():
    h = open("tests/fixtures/signal-desk-demo.html").read()
    assert "prompt(" not in h
    assert 'id="inSL"' in h and 'id="inTP"' in h
    fn = h[h.index("async function ctlModify"):]
    fn = fn[:fn.index("\nasync function")]
    assert 'getElementById("inSL")' in fn


def test_the_fields_start_from_the_live_values():
    h = open("tests/fixtures/signal-desk-demo.html").read()
    assert "si.value = mine.sl" in h and "ti.value = mine.tp" in h
    # and never overwrite what the trader is typing
    assert "document.activeElement!==si" in h


def _compose():
    import ast as _a
    src = open("orders_api.py").read()
    tree = _a.parse(src)
    keep = [n for n in tree.body
            if (isinstance(n, _a.ImportFrom) and n.module == "__future__")
            or (isinstance(n, _a.FunctionDef) and n.name == "_demo_signal_text")]
    ns = {}
    exec(compile(_a.Module(body=keep, type_ignores=[]), "x", "exec"), ns)
    return ns["_demo_signal_text"]


ROW = {"symbol": "EURUSD", "side": "buy", "fill_price": 1.15927,
       "filled_volume": 0.01, "ticket": 1926681563,
       "sl": 1.155, "tp": 1.17}


def test_a_real_fill_is_never_called_simulated():
    """The execution is a real MT5 order with a real ticket. Only the
    money is virtual."""
    t = _compose()(ROW, "SKLZ QA", "OPEN")
    assert "SIMULATED" not in t.upper()
    assert "LIVE DEMO SIGNAL" in t
    assert "Automation is live; funds are virtual." in t
    assert "MT5 broker DEMO account" in t


def test_volume_is_never_printed_as_zero():
    """'Volume: 0' was worse than no line at all."""
    c = _compose()
    assert "Volume: 0.01" in c(ROW, "P", "OPEN")
    blank = dict(ROW, filled_volume=None, lots=None)
    assert "Volume:" not in c(blank, "P", "OPEN")
    zero = dict(ROW, filled_volume=0, lots=0)
    assert "Volume: 0" not in c(zero, "P", "OPEN")


def test_the_broker_position_outranks_the_stored_row():
    c = _compose()
    t = c(ROW, "P", "TRAILING ACTIVE", {"sl": 1.1598, "volume": 0.02})
    assert "Current SL: 1.1598" in t      # live value, not the stored 1.155
    assert "Volume: 0.02" in t


def test_every_lifecycle_status_renders():
    c = _compose()
    for s, badge in (("OPEN", "\U0001F7E2"), ("UPDATED", "\U0001F504"),
                     ("BREAKEVEN", "\U0001F6E1"),
                     ("TRAILING ACTIVE", "\U0001F4C8"),
                     ("CLOSED", "\u2705")):
        t = c(ROW, "P", s)
        assert f"STATUS: {s}" in t and badge in t


def test_no_pnl_is_claimed():
    c = _compose()
    for s in ("OPEN", "CLOSED", "TRAILING ACTIVE"):
        t = c(ROW, "P", s).lower()
        for banned in ("profit", "p/l", "pnl", "pips gained", "%"):
            assert banned not in t, (s, banned)


def test_a_signal_is_composed_from_the_market_row_only():
    """modify/breakeven rows hold a command, not a fill: lots 0 and
    filled_volume null. Composing from one printed 'Volume: 0'."""
    api = open("orders_api.py").read()
    fn = api[api.index("def _signal_row("):api.index("def _edit_demo_message(")]
    assert 'eq("demo_kind", "market")' in fn
    st = api[api.index("async def demo_run_state("):]
    assert 'if kind == "market":' in st
    assert "_lifecycle_edit" in st


def test_lifecycle_events_edit_rather_than_repost():
    api = open("orders_api.py").read()
    assert "editMessageText" in api
    fn = api[api.index("def _lifecycle_edit("):]
    fn = fn[:fn.index("\n\n\n")] if "\n\n\n" in fn else fn
    assert "demo_tg_message_id" in fn
    assert "sendMessage" not in fn          # never a second post


def test_an_unchanged_edit_is_not_an_error():
    api = open("orders_api.py").read()
    fn = api[api.index("def _edit_demo_message("):api.index("def _deliver_demo_signal(")]
    assert '"not modified" in desc.lower()' in fn
    assert '"unchanged": True' in fn


def test_the_edit_can_only_reach_the_demo_channel():
    api = open("orders_api.py").read()
    fn = api[api.index("def _lifecycle_edit("):]
    assert 'not in (DEMO_TG_CHAT, "@sklzlabsdemo")' in fn
    assert "policy.validate(text" in fn


def test_trailing_edits_the_same_message_when_the_stop_moves():
    api = open("orders_api.py").read()
    fn = api[api.index("def _trailing_edit("):]
    fn = fn[:fn.index("\n\n\n")] if "\n\n\n" in fn else fn
    assert "TRAILING ACTIVE" in fn
    assert "_edit_demo_message(dest, base[\"demo_tg_message_id\"]" in fn
    assert "sendMessage" not in fn


def test_trailing_only_edits_on_an_actual_change():
    """A post that rewrites itself every 20 seconds is noise."""
    api = open("orders_api.py").read()
    fn = api[api.index("def _trailing_edit("):]
    assert "abs(float(live_sl) - float(shown_sl)) < 1e-9" in fn
    assert "continue" in fn


def test_the_new_stop_is_remembered_after_an_edit():
    api = open("orders_api.py").read()
    fn = api[api.index("def _trailing_edit("):]
    assert '{"sl": float(live_sl)}' in fn
    assert fn.index("_edit_demo_message") < fn.index('{"sl": float(live_sl)}')


def test_all_five_lifecycle_states_reach_telegram():
    api = open("orders_api.py").read()
    st = api[api.index("async def demo_run_state("):]
    assert 'if kind == "market":' in st          # OPEN
    assert 'elif kind == "positions":' in st     # TRAILING
    assert '"modify": "UPDATED"' in st
    assert '"breakeven": "BREAKEVEN"' in st
    assert '"close": "CLOSED"' in st


# ── trailing without a browser ───────────────────────────────────────
def test_trailing_notification_needs_no_browser():
    """The Runner already reports confirmed trail moves to the update
    board. The communication layer listens there."""
    u = open("updates_api.py").read()
    assert "notify_demo_trailing" in u
    assert 'kind in ("trail", "secured")' in u


def test_the_runner_never_calls_telegram():
    """Execution reports a trading fact; communication decides how to
    say it."""
    u = open("updates_api.py").read()
    fn = u[u.index("saved = _save(sb, row)"):]
    fn = fn[:fn.index("# keep the matching signal")]
    assert "api.telegram.org" not in fn
    assert "orders_api.notify_demo_trailing" in fn


def test_a_telegram_failure_cannot_affect_the_update_board():
    u = open("updates_api.py").read()
    fn = u[u.index('kind in ("trail", "secured")'):]
    fn = fn[:fn.index("# keep the matching signal")]
    assert "try:" in fn and "except Exception" in fn
    assert "notify skipped" in fn


def test_every_demo_guard_still_applies():
    api = open("orders_api.py").read()
    fn = api[api.index("async def notify_demo_trailing("):]
    for guard in ('eq("demo_kind", "market")',
                  'base.get("bot_name") != DEMO_BOT_NAME',
                  '!= _demo_login()',
                  'not base.get("demo_tg_message_id")'):
        assert guard in fn, guard
    assert 'not in (DEMO_TG_CHAT, "@sklzlabsdemo")' in fn


def test_the_same_stop_twice_is_not_a_second_edit():
    api = open("orders_api.py").read()
    fn = api[api.index("async def notify_demo_trailing("):]
    assert 'abs(float(base.get("sl") or 0) - float(new_sl)) < 1e-9' in fn
    assert '"skipped": "stop unchanged"' in fn
    # and the new stop is remembered so the next event compares to truth
    assert '{"sl": float(new_sl)}' in fn


def test_a_non_demo_ticket_is_ignored():
    api = open("orders_api.py").read()
    fn = api[api.index("async def notify_demo_trailing("):]
    assert '"skipped": "not a demo trade"' in fn
    assert '"skipped": "not the demo runner"' in fn


# ── pinned showcase ──────────────────────────────────────────────────
def _showcase():
    import ast as _a
    src = open("orders_api.py").read()
    tree = _a.parse(src)
    keep = [n for n in tree.body
            if (isinstance(n, _a.ImportFrom) and n.module == "__future__")
            or (isinstance(n, _a.FunctionDef)
                and n.name in ("_showcase_text", "_package_config", "_num_env"))
            or (isinstance(n, _a.Assign)
                and getattr(n.targets[0], "id", "") == "PACKAGE_DEFS")
            or (isinstance(n, _a.Import) and n.names[0].asname == "_os")]
    ns = {}
    exec(compile(_a.Module(body=keep, type_ignores=[]), "x", "exec"), ns)
    return ns["_showcase_text"]()


def test_the_showcase_claims_only_what_we_have_run():
    t = _showcase()
    for claim in ("Real MT5 execution", "Real broker confirmation",
                  "SL / TP modification", "Breakeven",
                  "Trailing protection", "Close automation",
                  "AI subscriber communication", "Multi-account copying"):
        assert claim in t, claim


def test_the_showcase_is_honest_about_demo_funds():
    t = _showcase()
    assert "MT5 demo funds" in t
    assert "automation infrastructure is real" in t
    assert "Not financial advice" in t and "risk of loss" in t


def test_showcase_pricing_comes_from_config_not_the_text():
    t = _showcase()
    assert "$499 setup + $49/month" in t
    src = open("orders_api.py").read()
    fn = src[src.index("def _showcase_text("):src.index('@demo_router.post("/admin/showcase")')]
    assert "_package_config()" in fn
    assert "499" not in fn          # never hard-coded in the post


def test_the_showcase_makes_no_performance_claim():
    low = _showcase().lower()
    for banned in ("profit", "win rate", "guaranteed", "returns", "%"):
        assert banned not in low, banned


def test_posting_the_showcase_is_admin_only_and_demo_pinned():
    src = open("orders_api.py").read()
    fn = src[src.index("async def post_showcase("):]
    assert "rules.is_platform_admin(user)" in fn
    assert 'not in (DEMO_TG_CHAT, "@sklzlabsdemo")' in fn
    assert "pinChatMessage" in fn
    assert "policy.validate(text" in fn


# ── ingest: snapshot presence, not truthiness ────────────────────────
def test_an_omitted_positions_field_is_not_a_snapshot():
    """`positions or None` made an empty broker answer and a result that
    carried no answer indistinguishable. Reconciliation needs them apart."""
    from bot_ingest import ResultIn

    body = ResultIn.model_validate_json('{"command_id":"c"}')
    assert "positions" not in body.model_fields_set
    stored = (body.positions
              if "positions" in body.model_fields_set else None)
    assert stored is None


def test_an_explicit_empty_snapshot_is_stored_as_empty():
    from bot_ingest import ResultIn

    body = ResultIn.model_validate_json('{"command_id":"c","positions":[]}')
    assert "positions" in body.model_fields_set
    stored = (body.positions
              if "positions" in body.model_fields_set else None)
    assert stored == []


def test_a_populated_snapshot_is_retained():
    from bot_ingest import ResultIn

    body = ResultIn.model_validate_json(
        '{"command_id":"c","positions":[{"ticket":1928027035}]}')
    stored = (body.positions
              if "positions" in body.model_fields_set else None)
    assert stored == [{"ticket": 1928027035}]


def test_the_ingest_uses_field_presence_not_truthiness():
    src = open("bot_ingest.py").read()
    assert '"positions" in body.model_fields_set' in src
    assert '"positions": (body.positions or None)' not in src


# ── reconciliation: a ticket the broker stopped reporting ────────────
OWNED = 1928027035
FOREIGN = 1927969355


def _snap(*tickets):
    return [{"ticket": t} for t in tickets]


def _history(*snapshots):
    return [{"created_at": i, "positions": s} for i, s in enumerate(snapshots)]


class _SnapQ:
    """bot_orders stand-in for the ticket-targeted snapshot queries."""

    def __init__(self, rows):
        self.rows, self.f, self.n = rows, {}, None
        self._negate = False
        self.orders = []

    def select(self, *a, **k):
        return self

    def eq(self, col, val):
        self.f[col] = val
        return self

    def contains(self, col, val):
        want = None
        for item in (val or []):
            want = (item or {}).get("ticket")
        self.f.setdefault("_contains", []).append((col, want))
        return self

    def gt(self, col, val):
        self.f.setdefault("_gt", []).append((col, val))
        return self

    @property
    def not_(self):
        self._negate = True
        return self

    def is_(self, col, _val):
        key = "_notnull" if self._negate else "_null"
        self.f.setdefault(key, []).append(col)
        self._negate = False
        return self

    def order(self, col="created_at", **k):
        self.orders.append((col, bool(k.get("desc"))))
        return self

    def limit(self, n):
        self.n = n
        return self

    def execute(self):
        plain = {k: v for k, v in self.f.items() if not k.startswith("_")}
        keep = [r for r in self.rows
                if all(r.get(k) == v for k, v in plain.items())]
        for col, want in self.f.get("_contains", []):
            keep = [r for r in keep
                    if isinstance(r.get(col), list)
                    and any((p or {}).get("ticket") == want for p in r[col])]
        for col in self.f.get("_notnull", []):
            keep = [r for r in keep if r.get(col) is not None]
        for col, val in self.f.get("_gt", []):
            keep = [r for r in keep if r.get(col) is not None and r[col] > val]
        for col, desc in reversed(self.orders):
            keep = sorted(keep, key=lambda r: r.get(col) or "", reverse=desc)
        return type("R", (), {"data": keep[:self.n] if self.n else keep})()


class _SnapSB:
    def __init__(self, rows):
        self.rows = rows

    def table(self, _n):
        return _SnapQ(self.rows)


def _snapshot_rows(*kinds, ticket=1928027035, foreign=1927969355):
    """Build a snapshot ledger. 'open' contains the ticket, 'gone' holds
    only the foreign position, 'empty' is an authoritative [], 'null' is
    a row that carried no snapshot at all."""
    rows = []
    for i, kind in enumerate(kinds):
        pos = {"open": [{"ticket": ticket}, {"ticket": foreign}],
               "gone": [{"ticket": foreign}],
               "empty": [],
               "null": None}[kind]
        rows.append({"command_id": f"p{i:04d}", "demo_token": "t",
                     "demo_kind": "positions", "status": "succeeded",
                     "created_at": f"2026-09-12T{i // 60:02d}:{i % 60:02d}:00Z",
                     "positions": pos})
    return rows


def test_a_ticket_still_present_is_not_vanished():
    import orders_api

    sb = _SnapSB(_snapshot_rows("open", "open", "open"))
    assert orders_api._vanished_at_broker(sb, "t", 1928027035) is False


def test_one_missing_snapshot_is_not_enough_to_close():
    import orders_api

    sb = _SnapSB(_snapshot_rows("open", "gone"))
    assert orders_api._vanished_at_broker(sb, "t", 1928027035) is False


def test_two_consecutive_missing_snapshots_confirm_the_close():
    import orders_api

    sb = _SnapSB(_snapshot_rows("open", "gone", "gone"))
    assert orders_api._vanished_at_broker(sb, "t", 1928027035) is True


def test_explicit_empty_snapshots_can_close_the_only_position():
    import orders_api

    sb = _SnapSB(_snapshot_rows("open", "empty", "empty"))
    assert orders_api._vanished_at_broker(sb, "t", 1928027035) is True


def test_a_null_snapshot_never_counts_as_a_confirmation():
    import orders_api

    sb = _SnapSB(_snapshot_rows("open", "null", "null"))
    assert orders_api._vanished_at_broker(sb, "t", 1928027035) is False
    # one real confirmation among the nulls is still only one
    sb = _SnapSB(_snapshot_rows("open", "null", "gone", "null"))
    assert orders_api._vanished_at_broker(sb, "t", 1928027035) is False


def test_a_ticket_never_observed_open_is_never_reconciled():
    import orders_api

    sb = _SnapSB(_snapshot_rows("gone", "gone", "gone"))
    assert orders_api._vanished_at_broker(sb, "t", 1928027035) is False


def test_a_reappearing_ticket_is_not_vanished():
    """The FIRST two snapshots after the last containing one decide it."""
    import orders_api

    sb = _SnapSB(_snapshot_rows("open", "gone", "open", "gone", "gone"))
    # the newest containing snapshot is rank 3; only one follows it... plus
    # one more, and both omit it, so this IS vanished
    assert orders_api._vanished_at_broker(sb, "t", 1928027035) is True
    sb = _SnapSB(_snapshot_rows("open", "gone", "open", "gone"))
    assert orders_api._vanished_at_broker(sb, "t", 1928027035) is False


def test_prior_open_proof_survives_a_saturated_window():
    """Ticket 1928027035's shape: the containing snapshot sat at rank 87
    while 86 newer authoritative snapshots omitted it, and a newest-60
    page could not see the proof at all."""
    import orders_api

    kinds = ["open"] * 13 + ["gone"] * 86      # oldest first: 99 rows
    sb = _SnapSB(_snapshot_rows(*kinds))
    assert orders_api._vanished_at_broker(sb, "t", 1928027035) is True


def test_a_hundred_later_snapshots_cannot_erase_the_proof():
    import orders_api

    kinds = ["open"] + ["gone"] * 150
    sb = _SnapSB(_snapshot_rows(*kinds))
    assert orders_api._vanished_at_broker(sb, "t", 1928027035) is True


def test_a_foreign_ticket_is_never_proven_vanished():
    """EURJPY is in every snapshot and never leaves — and it is not owned
    anyway, which is enforced a layer up."""
    import orders_api

    sb = _SnapSB(_snapshot_rows("open", "gone", "gone"))
    assert orders_api._vanished_at_broker(sb, "t", 1927969355) is False


def test_membership_is_exact_not_a_text_match():
    import orders_api

    assert orders_api._snapshot_has([{"ticket": 192802}], 1928027035) is False
    assert orders_api._snapshot_has([{"ticket": 1928027035}], 192802) is False
    assert orders_api._snapshot_has([{"ticket": "1928027035"}], 1928027035)


def test_the_query_orders_deterministically_on_both_bounds():
    """created_at carries no uniqueness guarantee here, so command_id is
    the tiebreak on the containing lookup and on the two that follow."""
    api = open("orders_api.py").read()
    for name in ("def _last_containing_snapshot(", "def _snapshots_after("):
        fn = api[api.index(name):]
        fn = fn[:fn.index("\ndef ", 10)]
        assert 'order("created_at"' in fn, name
        assert 'order("command_id"' in fn, name
    after = api[api.index("def _snapshots_after("):]
    after = after[:after.index("\ndef ", 10)]
    assert 'not_.is_("positions", "null")' in after
    assert 'gt("created_at"' in after


def test_the_history_is_never_paged_blind():
    api = open("orders_api.py").read()
    assert "_snapshot_history" not in api        # the windowed reader is gone
    fn = api[api.index("def _vanished_at_broker("):]
    fn = fn[:fn.index("\ndef ", 10)]
    assert "_last_containing_snapshot(" in fn
    assert "_snapshots_after(" in fn


class _RecQ:
    def __init__(self, store, table):
        self.store, self.table_name, self.f = store, table, {}
        self.updated = None

    def contains(self, col, val):
        want = None
        for item in (val or []):
            want = (item or {}).get("ticket")
        self.f.setdefault("_contains", []).append((col, want))
        return self

    def gt(self, col, val):
        self.f.setdefault("_gt", []).append((col, val))
        return self

    @property
    def not_(self):
        self._negate = True
        return self

    def select(self, *a, **k):
        return self

    def eq(self, col, val):
        self.f[col] = val
        return self

    def is_(self, col, _val):
        if getattr(self, "_negate", False):
            self.f.setdefault("_notnull", []).append(col)
            self._negate = False
        else:
            self.f.setdefault("_null", []).append(col)
        return self

    def order(self, col="created_at", **k):
        if not hasattr(self, "order_col"):
            self.order_col, self.order_desc = col, bool(k.get("desc"))
        return self

    def limit(self, n):
        self.n = n
        return self

    def update(self, patch):
        self.store["updates"].append(patch)
        return self

    def execute(self):
        if "updates" in self.f or self.store.get("_updating"):
            return type("R", (), {"data": []})()
        rows = self.store["rows"]
        plain = {k: v for k, v in self.f.items() if not k.startswith("_")}
        keep = [r for r in rows if all(r.get(k) == v for k, v in plain.items())]
        for col, want in self.f.get("_contains", []):
            keep = [r for r in keep
                    if isinstance(r.get(col), list)
                    and any((p or {}).get("ticket") == want for p in r[col])]
        for col in self.f.get("_notnull", []):
            keep = [r for r in keep if r.get(col) is not None]
        for col in self.f.get("_null", []):
            keep = [r for r in keep if r.get(col) is None]
        for col, val in self.f.get("_gt", []):
            keep = [r for r in keep if r.get(col) is not None and r[col] > val]
        col = getattr(self, "order_col", None)
        if col:
            keep = sorted(keep, key=lambda r: r.get(col) or 0,
                          reverse=getattr(self, "order_desc", False))
        n = getattr(self, "n", None)
        return type("R", (), {"data": keep[:n] if n else keep})()


class _RecSB:
    def __init__(self, rows):
        self.store = {"rows": rows, "updates": []}

    def table(self, name):
        return _RecQ(self.store, name)


def _ledger_rows(**overrides):
    market = {"demo_token": "t", "demo_kind": "market", "status": "succeeded",
              "ticket": OWNED, "command_id": "m1",
              "demo_tg_message_id": 41, "demo_closed_at": None,
              "demo_close_command_id": None, "symbol": "BTCUSD",
              "side": "sell", "lots": 0.01, "fill_price": 77183.5}
    market.update(overrides)
    return [market,
            {"demo_token": "t", "demo_kind": "positions",
             "status": "succeeded", "created_at": 0,
             "positions": _snap(OWNED)},
            {"demo_token": "t", "demo_kind": "positions",
             "status": "succeeded", "created_at": 1, "positions": []},
            {"demo_token": "t", "demo_kind": "positions",
             "status": "succeeded", "created_at": 2, "positions": []}]


def _run_reconcile(monkeypatch, rows, edit_result, snap=None):
    import orders_api

    calls = []

    def fake_edit(sb, tok, event, status, provider, reason=""):
        calls.append({"ticket": event.get("ticket"), "status": status,
                      "reason": reason})
        return edit_result

    monkeypatch.setattr(orders_api, "_lifecycle_edit", fake_edit)
    sb = _RecSB(rows)
    done = orders_api._reconcile_vanished(
        sb, "t", {"positions": [] if snap is None else snap}, "SKLZ Final QA")
    return done, calls, sb.store["updates"]


def test_a_vanished_owned_ticket_closes_the_same_message(monkeypatch):
    done, calls, updates = _run_reconcile(
        monkeypatch, _ledger_rows(), {"ok": True, "message_id": 41})
    assert [c["status"] for c in calls] == ["CLOSED"]
    assert calls[0]["reason"] == "Closed at broker"
    assert calls[0]["ticket"] == OWNED
    assert done and done[0]["state"] == "closed_at_broker"
    assert updates and updates[0]["demo_close_state"] == "closed_at_broker"


def test_a_foreign_ticket_disappearing_is_ignored(monkeypatch):
    """EURJPY has no market row under this token, so it is never owned."""
    rows = _ledger_rows()
    done, calls, _ = _run_reconcile(monkeypatch, rows, {"ok": True},
                                    snap=_snap(OWNED))
    assert calls == [] and done == []


def test_an_already_closed_ticket_is_not_reconciled_again(monkeypatch):
    rows = _ledger_rows(demo_closed_at="2026-09-12T00:00:00Z")
    done, calls, updates = _run_reconcile(monkeypatch, rows, {"ok": True})
    assert calls == [] and done == [] and updates == []


def test_only_a_succeeded_close_owns_the_outcome(monkeypatch):
    """A close command used to block reconciliation merely by existing.
    One that was queued against an already-vanished position can never
    resolve, and it deadlocked that ticket's post forever. Now only a
    close that actually SUCCEEDED takes precedence."""
    rows = _ledger_rows(demo_close_command_id="c9")   # no close row: unresolved
    done, calls, _ = _run_reconcile(monkeypatch, rows, {"ok": True})
    assert [c["reason"] for c in calls] == ["Closed at broker"]
    assert done and done[0]["state"] == "closed_at_broker"

    rows = _ledger_rows(demo_close_command_id="c9")
    rows.append({"command_id": "c9", "demo_token": "t", "demo_kind": "close",
                 "status": "succeeded", "ticket": OWNED})
    done2, calls2, _ = _run_reconcile(monkeypatch, rows, {"ok": True})
    assert calls2 == [] and done2 == []


def test_a_trade_that_was_never_posted_is_not_edited(monkeypatch):
    rows = _ledger_rows(demo_tg_message_id=None)
    done, calls, _ = _run_reconcile(monkeypatch, rows, {"ok": True})
    assert calls == [] and done == []


def test_a_telegram_failure_leaves_the_row_eligible(monkeypatch):
    """No stamp on failure, and the positions read still returns."""
    done, calls, updates = _run_reconcile(
        monkeypatch, _ledger_rows(), {"error": "telegram down"})
    assert len(calls) == 1          # it tried
    assert done == []               # but reported nothing closed
    assert updates == []            # and stamped nothing


def test_reconciliation_is_idempotent(monkeypatch):
    """Second pass sees the stamp the first pass wrote and does nothing."""
    rows = _ledger_rows()
    done1, calls1, _ = _run_reconcile(monkeypatch, rows, {"ok": True})
    assert len(calls1) == 1 and done1

    rows2 = _ledger_rows(demo_closed_at="2026-09-12T00:00:00Z",
                         demo_close_state="closed_at_broker")
    done2, calls2, updates2 = _run_reconcile(monkeypatch, rows2, {"ok": True})
    assert calls2 == [] and done2 == [] and updates2 == []


def test_a_result_carrying_no_snapshot_reconciles_nothing(monkeypatch):
    import orders_api

    called = []
    monkeypatch.setattr(orders_api, "_lifecycle_edit",
                        lambda *a, **k: called.append(1) or {"ok": True})
    out = orders_api._reconcile_vanished(
        _RecSB(_ledger_rows()), "t", {"positions": None}, "p")
    assert out == [] and called == []


def test_the_close_text_states_only_a_reason_we_can_prove():
    import orders_api

    row = {"ticket": OWNED, "symbol": "BTCUSD", "side": "sell",
           "lots": 0.01, "fill_price": 77183.5}
    text = orders_api._demo_signal_text(row, "SKLZ Final QA", "CLOSED",
                                        None, "Closed at broker")
    assert "STATUS: CLOSED" in text
    assert "Reason: Closed at broker" in text
    assert "Exit:" not in text          # no exit price is known
    assert "SL hit" not in text and "TP hit" not in text
    assert "Executed on an MT5 broker DEMO account." in text
    assert "Automation is live; funds are virtual." in text


def test_no_broker_command_is_ever_sent_by_reconciliation():
    src = open("orders_api.py").read()
    fn = src[src.index("def _reconcile_vanished("):]
    fn = fn[:fn.index("def _trailing_edit(")]
    assert "command_type" not in fn
    assert '"close"' not in fn
    assert "insert(" not in fn


# ── close-state convergence ──────────────────────────────────────────
class _ConvQ:
    """A bot_orders stand-in that honours eq/is_/lt/order and records
    updates against the row they targeted."""

    def __init__(self, store):
        self.store, self.f, self.nulls, self.patch = store, {}, [], None

    def contains(self, col, val):
        want = None
        for item in (val or []):
            want = (item or {}).get("ticket")
        self.f.setdefault("_contains", []).append((col, want))
        return self

    def gt(self, col, val):
        self.f.setdefault("_gt", []).append((col, val))
        return self

    @property
    def not_(self):
        self._negate = True
        return self

    def select(self, *a, **k):
        return self

    def eq(self, col, val):
        self.f[col] = val
        return self

    def is_(self, col, _val):
        if getattr(self, "_negate", False):
            self.f.setdefault("_notnull", []).append(col)
            self._negate = False
        else:
            self.nulls.append(col)
        return self

    def lt(self, col, val):
        self.f.setdefault("_lt", []).append((col, val))
        return self

    def order(self, col="created_at", **k):
        if not hasattr(self, "order_col"):
            self.order_col, self.order_desc = col, bool(k.get("desc"))
        return self

    def limit(self, n):
        self.n = n
        return self

    def update(self, patch):
        self.patch = patch
        return self

    def _match(self, r):
        for k, v in self.f.items():
            if k.startswith("_"):
                continue
            if r.get(k) != v:
                return False
        for col in self.nulls:
            if r.get(col) is not None:
                return False
        for col, val in self.f.get("_lt", []):
            if not (r.get(col) is not None and r[col] < val):
                return False
        for col, val in self.f.get("_gt", []):
            if not (r.get(col) is not None and r[col] > val):
                return False
        for col in self.f.get("_notnull", []):
            if r.get(col) is None:
                return False
        for col, want in self.f.get("_contains", []):
            v = r.get(col)
            if not isinstance(v, list) or not any(
                    (p or {}).get("ticket") == want for p in v):
                return False
        return True

    def execute(self):
        rows = [r for r in self.store["rows"] if self._match(r)]
        if self.patch is not None:
            for r in rows:
                r.update(self.patch)
                self.store["updates"].append(
                    {"command_id": r.get("command_id"), **self.patch})
            return type("R", (), {"data": rows})()
        col = getattr(self, "order_col", None)
        if col:
            rows = sorted(rows, key=lambda r: r.get(col) or 0,
                          reverse=getattr(self, "order_desc", False))
        return type("R", (), {"data": rows})()


class _ConvSB:
    def __init__(self, rows):
        self.store = {"rows": rows, "updates": [], "inserts": []}

    def table(self, _name):
        q = _ConvQ(self.store)
        q.insert = self._insert
        return q

    def _insert(self, row):
        self.store["inserts"].append(row)
        outer = self

        class _I:
            def execute(self_inner):
                return type("R", (), {"data": [{"command_id": "newclose"}]})()
        return _I()


TK = 1928027035


def _conv_rows(close_status=None, close_cid="cl1", closed_at=None,
               snapshots=("open", "gone", "gone")):
    rows = [{"command_id": "m1", "demo_token": "t", "demo_kind": "market",
             "status": "succeeded", "ticket": TK, "symbol": "BTCUSD",
             "side": "sell", "lots": 0.01, "fill_price": 77183.5,
             "demo_tg_message_id": 41, "demo_closed_at": closed_at,
             "demo_close_state": "queued" if close_cid else None,
             "demo_close_command_id": close_cid,
             "actual_account": "52952532",
             "executed_at": "2026-09-12T02:00:00Z"}]
    if close_cid and close_status:
        rows.append({"command_id": close_cid, "demo_token": "t",
                     "demo_kind": "close", "status": close_status,
                     "ticket": TK, "executed_at": "2026-09-12T03:00:00Z"})
    for i, kind in enumerate(snapshots):
        pos = ([{"ticket": TK}] if kind == "open"
               else None if kind == "null" else [])
        rows.append({"command_id": f"p{i}", "demo_token": "t",
                     "demo_kind": "positions", "status": "succeeded",
                     "created_at": i, "positions": pos})
    return rows


def _edits(monkeypatch, result=None):
    import orders_api
    seen = []

    def fake(sb, tok, event, status, provider, reason=""):
        base = orders_api._signal_row(sb, tok, event.get("ticket"))
        if status == "CLOSED" and base.get("demo_closed_at"):
            return {"skipped": "already closed"}
        seen.append({"ticket": event.get("ticket"), "status": status,
                     "reason": reason})
        return result or {"ok": True, "message_id": 41}

    monkeypatch.setattr(orders_api, "_lifecycle_edit", fake)
    return seen


def test_a_succeeded_autoclose_finally_reaches_telegram(monkeypatch):
    import orders_api

    sb = _ConvSB(_conv_rows(close_status="succeeded"))
    seen = _edits(monkeypatch)
    out = orders_api._settle_linked_closes(sb, "t", "p")
    assert [e["status"] for e in seen] == ["CLOSED"]
    assert seen[0]["reason"] == ""            # normal close formatting
    assert out and out[0]["state"] == "succeeded"
    m = [r for r in sb.store["rows"] if r["command_id"] == "m1"][0]
    assert m["demo_closed_at"] == "2026-09-12T03:00:00Z"
    assert m["demo_close_state"] == "succeeded"


def test_a_succeeded_autoclose_stamps_exactly_once(monkeypatch):
    import orders_api

    sb = _ConvSB(_conv_rows(close_status="succeeded"))
    seen = _edits(monkeypatch)
    orders_api._settle_linked_closes(sb, "t", "p")
    orders_api._settle_linked_closes(sb, "t", "p")
    assert len(seen) == 1
    assert len([u for u in sb.store["updates"]
                if u.get("demo_close_state") == "succeeded"]) == 1


def test_a_failed_close_with_the_ticket_still_open_is_not_closed(monkeypatch):
    """A close that failed proves the close did not happen — nothing more."""
    import orders_api

    rows = _conv_rows(close_status="failed", snapshots=("open", "open"))
    sb = _ConvSB(rows)
    seen = _edits(monkeypatch)
    orders_api._settle_linked_closes(sb, "t", "p")
    orders_api._reconcile_vanished(sb, "t", {"positions": [{"ticket": TK}]}, "p")
    assert seen == []
    m = [r for r in sb.store["rows"] if r["command_id"] == "m1"][0]
    assert m["demo_closed_at"] is None
    assert m["demo_close_state"] == "failed"


def test_a_failed_close_never_sets_demo_closed_at(monkeypatch):
    import orders_api

    sb = _ConvSB(_conv_rows(close_status="failed", snapshots=("open", "open")))
    _edits(monkeypatch)
    orders_api._settle_linked_closes(sb, "t", "p")
    assert all(u.get("demo_closed_at") is None
               for u in sb.store["updates"])


def test_the_sweep_does_not_close_what_the_broker_already_dropped():
    import orders_api

    rows = _conv_rows(close_cid=None, snapshots=("open", "gone", "gone"))
    sb = _ConvSB(rows)
    orders_api._demo_enabled = lambda: True
    queued = orders_api._sweep_demo_closes(sb)
    assert queued == 0
    assert sb.store["inserts"] == []


def test_one_missing_snapshot_does_not_stop_the_sweep(monkeypatch):
    import orders_api

    rows = _conv_rows(close_cid=None, snapshots=("open", "gone"))
    sb = _ConvSB(rows)
    monkeypatch.setattr(orders_api, "_demo_guard", lambda _sb: "")
    monkeypatch.setattr(orders_api, "_demo_login", lambda: "52952532")
    queued = orders_api._sweep_demo_closes(sb)
    assert queued == 1 and len(sb.store["inserts"]) == 1


def test_a_queued_close_on_a_present_ticket_is_left_alone(monkeypatch):
    import orders_api

    sb = _ConvSB(_conv_rows(snapshots=("open", "open")))
    seen = _edits(monkeypatch)
    orders_api._settle_linked_closes(sb, "t", "p")
    out = orders_api._reconcile_vanished(
        sb, "t", {"positions": [{"ticket": TK}]}, "p")
    assert seen == [] and out == []


def test_a_queued_close_no_longer_blocks_a_confirmed_disappearance(monkeypatch):
    """The 1928027035 deadlock: a close that can never resolve."""
    import orders_api

    sb = _ConvSB(_conv_rows(close_status="pending"))
    seen = _edits(monkeypatch)
    out = orders_api._reconcile_vanished(sb, "t", {"positions": []}, "p")
    assert [e["reason"] for e in seen] == ["Closed at broker"]
    assert out and out[0]["state"] == "closed_at_broker"


def test_a_failed_close_plus_confirmed_absence_reconciles(monkeypatch):
    import orders_api

    sb = _ConvSB(_conv_rows(close_status="failed"))
    seen = _edits(monkeypatch)
    out = orders_api._reconcile_vanished(sb, "t", {"positions": []}, "p")
    assert [e["reason"] for e in seen] == ["Closed at broker"]
    assert out and out[0]["state"] == "closed_at_broker"


def test_a_succeeded_close_outranks_the_disappearance_fallback(monkeypatch):
    """The real close owns the outcome; no 'Closed at broker' fallback."""
    import orders_api

    sb = _ConvSB(_conv_rows(close_status="succeeded"))
    seen = _edits(monkeypatch)
    out = orders_api._reconcile_vanished(sb, "t", {"positions": []}, "p")
    assert seen == [] and out == []


def test_a_late_succeeded_close_does_not_re_edit(monkeypatch):
    import orders_api

    sb = _ConvSB(_conv_rows(close_status="pending"))
    seen = _edits(monkeypatch)
    orders_api._reconcile_vanished(sb, "t", {"positions": []}, "p")
    assert len(seen) == 1
    for r in sb.store["rows"]:              # the close now lands
        if r["command_id"] == "cl1":
            r["status"] = "succeeded"
    orders_api._settle_linked_closes(sb, "t", "p")
    assert len(seen) == 1                   # still one edit
    m = [r for r in sb.store["rows"] if r["command_id"] == "m1"][0]
    assert m["demo_close_state"] == "closed_at_broker"


def test_a_late_failed_close_does_not_re_edit(monkeypatch):
    import orders_api

    sb = _ConvSB(_conv_rows(close_status="pending"))
    seen = _edits(monkeypatch)
    orders_api._reconcile_vanished(sb, "t", {"positions": []}, "p")
    for r in sb.store["rows"]:
        if r["command_id"] == "cl1":
            r["status"] = "failed"
    orders_api._settle_linked_closes(sb, "t", "p")
    assert len(seen) == 1
    m = [r for r in sb.store["rows"] if r["command_id"] == "m1"][0]
    assert m["demo_close_state"] == "closed_at_broker"


def test_the_closed_guard_lives_in_the_one_edit_path():
    """Explicit close, auto-close, settlement and reconciliation all go
    through _lifecycle_edit, so the guard belongs there once."""
    api = open("orders_api.py").read()
    fn = api[api.index("def _lifecycle_edit("):]
    fn = fn[:fn.index("def _snapshot_has(")]
    assert 'status == "CLOSED" and base.get("demo_closed_at")' in fn
    assert '"skipped": "already closed"' in fn


def test_a_foreign_ticket_is_never_settled_or_reconciled(monkeypatch):
    import orders_api

    rows = _conv_rows(close_status="succeeded")
    rows.append({"command_id": "x", "demo_token": "other",
                 "demo_kind": "market", "status": "succeeded",
                 "ticket": 1927969355, "demo_tg_message_id": 99,
                 "demo_closed_at": None, "demo_close_command_id": None})
    sb = _ConvSB(rows)
    seen = _edits(monkeypatch)
    orders_api._settle_linked_closes(sb, "t", "p")
    orders_api._reconcile_vanished(sb, "t", {"positions": []}, "p")
    assert all(e["ticket"] == TK for e in seen)


def test_null_snapshots_cannot_confirm_a_convergence_close(monkeypatch):
    import orders_api

    sb = _ConvSB(_conv_rows(close_status="pending",
                            snapshots=("open", "null", "null")))
    seen = _edits(monkeypatch)
    out = orders_api._reconcile_vanished(sb, "t", {"positions": []}, "p")
    assert seen == [] and out == []


def test_a_telegram_failure_during_settlement_stamps_nothing(monkeypatch):
    import orders_api

    sb = _ConvSB(_conv_rows(close_status="succeeded"))
    _edits(monkeypatch, result={"error": "telegram down"})
    out = orders_api._settle_linked_closes(sb, "t", "p")
    assert out == []
    m = [r for r in sb.store["rows"] if r["command_id"] == "m1"][0]
    assert m["demo_closed_at"] is None


def test_convergence_is_idempotent_across_repeated_polls(monkeypatch):
    import orders_api

    sb = _ConvSB(_conv_rows(close_status="pending"))
    seen = _edits(monkeypatch)
    for _ in range(4):
        orders_api._settle_linked_closes(sb, "t", "p")
        orders_api._reconcile_vanished(sb, "t", {"positions": []}, "p")
    assert len(seen) == 1


def test_a_broken_linked_close_lookup_does_not_break_the_read(monkeypatch):
    import orders_api

    class Boom(_ConvSB):
        def table(self, name):
            raise RuntimeError("db down")

    assert orders_api._linked_close(Boom([]), "cl1") == {}
    assert orders_api._owned_market_rows(Boom([]), "t") == []
    assert orders_api._settle_linked_closes(Boom([]), "t", "p") == []


def test_the_positions_read_settles_before_it_reconciles():
    api = open("orders_api.py").read()
    fn = api[api.index("async def demo_run_state("):]
    fn = fn[:fn.index("@demo_router.post")]
    assert (fn.index("_settle_linked_closes")
            < fn.index("_reconcile_vanished")
            < fn.index("_trailing_edit"))


def test_convergence_sends_no_broker_command():
    api = open("orders_api.py").read()
    for name in ("def _settle_linked_closes(", "def _reconcile_vanished("):
        fn = api[api.index(name):]
        fn = fn[:fn.index("\ndef ", 10)]
        assert "insert(" not in fn, name
        assert "command_type" not in fn, name


# ── positions-read failure isolation ─────────────────────────────────
import asyncio


def _positions_row():
    return {"command_id": "pos1", "demo_token": "t", "demo_kind": "positions",
            "status": "succeeded", "ticket": None, "positions": [],
            "created_at": "2026-09-12T04:00:00Z",
            "broker_confirmed_at": "2026-09-12T04:00:01Z",
            "actual_account": "52952532", "symbol": "", "side": "",
            "lots": 0, "fill_price": None, "retcode": None,
            "broker_comment": None, "demo_close_command_id": None,
            "demo_closed_at": None}


def _run_positions_branch(monkeypatch, *, settle, reconcile, trailing):
    """Drive the REAL demo_run_state positions branch."""
    import orders_api

    row = _positions_row()
    calls = []

    class _SB:
        def table(self, _n):
            return self

        def select(self, *a, **k):
            return self

        def eq(self, *a, **k):
            return self

        def limit(self, *a, **k):
            return self

        def execute(self):
            return type("R", (), {"data": [row]})()

    async def fake_read_link(_tok, _sb):
        return {"provider_name": "SKLZ Final QA"}

    def wrap(name, behaviour):
        def fn(*_a, **_k):
            calls.append(name)
            if isinstance(behaviour, Exception):
                raise behaviour
            return behaviour
        return fn

    monkeypatch.setattr(orders_api, "read_demo_link", fake_read_link)
    monkeypatch.setattr(orders_api, "_settle_linked_closes",
                        wrap("settle", settle))
    monkeypatch.setattr(orders_api, "_reconcile_vanished",
                        wrap("reconcile", reconcile))
    monkeypatch.setattr(orders_api, "_trailing_edit",
                        wrap("trailing", trailing))
    monkeypatch.setattr(orders_api, "_close_state", lambda _sb, _r: {})
    monkeypatch.setattr(orders_api, "_sweep_demo_closes", lambda _sb: 0)

    out = asyncio.run(orders_api.demo_run_state("t", "pos1", _SB()))
    return out, calls


def test_settlement_failure_does_not_suppress_the_other_steps(monkeypatch):
    """The defect: settlement ran first inside one shared try, so its
    exception silently cancelled reconciliation and trailing."""
    out, calls = _run_positions_branch(
        monkeypatch,
        settle=RuntimeError("telegram exploded"),
        reconcile=[{"ticket": 1928027035, "state": "closed_at_broker"}],
        trailing={"ok": True, "message_id": 41})

    assert out["ok"] is True                      # response still succeeds
    assert calls == ["settle", "reconcile", "trailing"]
    assert out["reconciled"][0]["state"] == "closed_at_broker"
    assert out["telegram"] == {"ok": True, "message_id": 41}
    assert "settled" not in out


def test_reconciliation_failure_does_not_suppress_trailing(monkeypatch):
    out, calls = _run_positions_branch(
        monkeypatch,
        settle=[{"ticket": 1, "state": "succeeded"}],
        reconcile=RuntimeError("db blew up"),
        trailing={"ok": True, "message_id": 41})

    assert out["ok"] is True
    assert calls == ["settle", "reconcile", "trailing"]
    assert out["settled"][0]["state"] == "succeeded"
    assert out["telegram"] == {"ok": True, "message_id": 41}
    assert "reconciled" not in out


def test_trailing_failure_leaves_the_earlier_results_intact(monkeypatch):
    out, calls = _run_positions_branch(
        monkeypatch,
        settle=[{"ticket": 1, "state": "succeeded"}],
        reconcile=[{"ticket": 2, "state": "closed_at_broker"}],
        trailing=RuntimeError("edit failed"))

    assert out["ok"] is True
    assert calls == ["settle", "reconcile", "trailing"]
    assert out["settled"] and out["reconciled"]
    assert out.get("telegram") in (None, {}, )


def test_every_stage_can_fail_and_the_read_still_answers(monkeypatch):
    out, calls = _run_positions_branch(
        monkeypatch,
        settle=RuntimeError("a"), reconcile=RuntimeError("b"),
        trailing=RuntimeError("c"))

    assert out["ok"] is True and out["state"] == "succeeded"
    assert calls == ["settle", "reconcile", "trailing"]
    assert "settled" not in out and "reconciled" not in out


def test_the_happy_path_still_runs_all_three_in_order(monkeypatch):
    out, calls = _run_positions_branch(
        monkeypatch,
        settle=[{"ticket": 1, "state": "succeeded"}],
        reconcile=[], trailing={})

    assert calls == ["settle", "reconcile", "trailing"]
    assert out["settled"][0]["state"] == "succeeded"


def test_one_tickets_failure_does_not_strand_the_next(monkeypatch):
    """Settlement and reconciliation both loop; an edit that throws on
    the first ticket must not abandon the second."""
    import orders_api

    seen = []

    def flaky(sb, tok, event, status, provider, reason=""):
        tk = event.get("ticket")
        seen.append(tk)
        if tk == 1:
            raise RuntimeError("telegram down for this one")
        return {"ok": True, "message_id": 99}

    monkeypatch.setattr(orders_api, "_lifecycle_edit", flaky)

    rows = []
    for tk, cid in ((1, "clA"), (2, "clB")):
        rows.append({"command_id": f"m{tk}", "demo_token": "t",
                     "demo_kind": "market", "status": "succeeded",
                     "ticket": tk, "demo_tg_message_id": 40 + tk,
                     "demo_closed_at": None, "demo_close_state": "queued",
                     "demo_close_command_id": cid})
        rows.append({"command_id": cid, "demo_token": "t",
                     "demo_kind": "close", "status": "succeeded",
                     "ticket": tk, "executed_at": "2026-09-12T05:00:00Z"})

    out = orders_api._settle_linked_closes(_ConvSB(rows), "t", "p")
    assert seen == [1, 2]                      # both attempted
    assert [o["ticket"] for o in out] == [2]   # the healthy one still settled


def test_the_stage_helper_never_logs_the_demo_token():
    """The token IS the link's credential."""
    api = open("orders_api.py").read()
    fn = api[api.index("async def _positions_stage("):]
    fn = fn[:fn.index("def _owned_market_rows(")]
    assert "command" in fn and "{tok" not in fn
    assert "stage" in fn and "type(exc).__name__" in fn


def test_each_positions_stage_has_its_own_boundary():
    api = open("orders_api.py").read()
    fn = api[api.index("async def demo_run_state("):]
    fn = fn[:fn.index("@demo_router.post")]
    for stage in ('"settlement"', '"reconciliation"', '"trailing"'):
        assert f"_positions_stage(\n                {stage}" in fn \
            or stage in fn, stage
    assert fn.index('"settlement"') < fn.index('"reconciliation"') \
        < fn.index('"trailing"')
