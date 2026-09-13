"""D9 — a permanent public showcase that is not a permanent free-for-all.

The private demo is the thing being protected here. Every test that says
"unchanged" is guarding a prospect link that was sold with a 48-hour life
and three trades, against a change made for a different kind of link.
"""
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, ".")
API = open("./orders_api.py").read()
SQL = open("migrations/D9-migration.sql").read()


def _fn(name, end=None):
    s = API[API.index(name):]
    return s[:s.index(end)] if end and end in s else s


# ── the two purposes are server data, never inference ──────────────────
def test_purpose_is_an_explicit_closed_vocabulary():
    assert 'PURPOSE_PRIVATE = "private_demo"' in API
    assert 'PURPOSE_SHOWCASE = "showcase"' in API
    assert "PURPOSES = {PURPOSE_PRIVATE, PURPOSE_SHOWCASE}" in API
    fn = _fn("async def create_demo_link(", "@demo_router.get")
    assert "if purpose not in PURPOSES:" in fn
    assert "purpose must be one of" in fn


def test_purpose_is_never_guessed_from_anything_else():
    fn = _fn("def _purpose_of(", "def _clamp_hours(")
    # it reads one column and nothing else
    assert 'row or {}).get("purpose")' in fn
    for guess in ("provider_name", "note", "token", "url", "contact",
                  "telegram_channel"):
        assert guess not in fn, guess


def test_an_unknown_or_missing_purpose_is_private_not_showcase():
    import importlib.util
    mod = _load()
    assert mod._purpose_of({}) == "private_demo"
    assert mod._purpose_of({"purpose": None}) == "private_demo"
    assert mod._purpose_of({"purpose": ""}) == "private_demo"
    assert mod._purpose_of({"purpose": "admin"}) == "private_demo"
    assert mod._purpose_of({"purpose": "SHOWCASE"}) == "showcase"
    assert mod._purpose_of({"purpose": "showcase"}) == "showcase"


# ── A / B: the private ceiling is untouched ────────────────────────────
def test_private_links_still_clamp_to_168_hours():
    mod = _load()
    assert mod._clamp_hours("private_demo", 200) == 168
    assert mod._clamp_hours("private_demo", 87600) == 168
    assert mod._clamp_hours("private_demo", 169) == 168


def test_private_default_and_floor_are_unchanged():
    mod = _load()
    assert mod._clamp_hours("private_demo", 48) == 48
    assert mod._clamp_hours("private_demo", None) == 48
    assert mod._clamp_hours("private_demo", 0) == 48      # falsy → default
    assert mod._clamp_hours("private_demo", -5) == 1      # floor
    assert mod._clamp_hours("private_demo", "nonsense") == 48


# ── C: the showcase does not expire, and says so honestly ──────────────
def test_showcase_carries_no_expiry_at_all():
    mod = _load()
    assert mod._clamp_hours("showcase", 48) is None
    assert mod._clamp_hours("showcase", 87600) is None
    fn = _fn("async def create_demo_link(", "@demo_router.get")
    assert "expires = (None if hours is None" in fn
    # no fabricated far-future date anywhere
    for magic in ("9999", "2999", "year 9999", "timedelta(days=36500"):
        assert magic not in API, magic


def test_read_skips_expiry_for_showcase_only():
    fn = _fn("async def read_demo_link(", "@demo_router.get\nasync def list_demo_links")
    assert "if purpose == PURPOSE_SHOWCASE:" in fn
    assert "exp = None" in fn
    # the private branch still raises 410 on a past date
    assert "this demo has expired" in fn


def test_revocation_still_closes_a_showcase():
    fn = _fn("async def read_demo_link(", "@demo_router.get\nasync def list_demo_links")
    revoked = fn[fn.index('if row.get("revoked")'):]
    # the revoked check comes BEFORE the purpose branch, so no purpose
    # can route around it
    assert fn.index('row.get("revoked")') < fn.index("purpose = _purpose_of(row)")
    assert "this demo has been closed" in revoked


def test_a_non_expiring_link_reports_null_not_a_big_number():
    fn = _fn("async def read_demo_link(", "@demo_router.get\nasync def list_demo_links")
    assert '"seconds_remaining": (None if exp is None' in fn
    assert '"purpose": purpose' in fn


# ── D: exactly one live showcase ───────────────────────────────────────
def test_only_one_active_showcase_can_exist():
    fn = _fn("async def create_demo_link(", "@demo_router.get")
    assert 'eq("purpose", PURPOSE_SHOWCASE).eq("revoked", False)' in fn
    assert '"showcase_exists"' in fn
    assert "HTTP_409_CONFLICT" in fn


def test_the_database_is_the_real_guard_for_one_showcase():
    assert "demo_links_one_active_showcase" in SQL
    assert "where purpose = 'showcase' and revoked = false" in SQL
    assert "create unique index" in SQL


def test_migration_is_additive_and_defaults_history_to_private():
    assert "add column if not exists purpose text not null default 'private_demo'" in SQL
    assert "demo_links_purpose_ck" in SQL
    assert "check (purpose in ('private_demo', 'showcase'))" in SQL
    # a private link can still never be minted immortal
    assert "check (purpose = 'showcase' or expires_at is not null)" in SQL


# ── E / F: run policy ──────────────────────────────────────────────────
def test_private_run_policy_is_unchanged():
    mod = _load()
    assert mod._run_budget("private_demo") == (3, None)
    assert mod.DEMO_RUNS_PER_TOKEN == 3


def test_showcase_gets_six_per_rolling_hour():
    mod = _load()
    assert mod.SHOWCASE_RUNS_PER_HOUR == 6
    assert mod.SHOWCASE_RUN_WINDOW_MINUTES == 60
    assert mod._run_budget("showcase") == (6, 60)


def test_the_window_is_rolling_not_cumulative():
    mod = _load()
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    assert mod._window_start(None, now) is None          # private: all history
    assert mod._window_start(60, now) == now - timedelta(minutes=60)


def test_only_opening_a_position_spends_the_budget():
    fn = _fn("async def demo_control(", "@demo_router.get\nasync def demo_trailing")
    gate = fn[fn.index('if action in ("buy", "sell"):'):]
    spend = gate[:gate.index("elif action ==")]
    assert "_run_budget(purpose)" in spend
    # management actions are outside the budget branch entirely
    for free in ("modify", "breakeven", "close", "positions"):
        assert f'action == "{free}"' not in spend, free


def test_the_window_query_uses_the_real_timestamp_column():
    fn = _fn("def _market_runs(", "def _demo_login(")
    assert 'gte("created_at", since.isoformat())' in fn
    assert 'eq("demo_kind", "market")' in fn
    # D5 already indexed exactly this pair
    d5 = open("migrations/D5-migration.sql").read()
    assert "on public.bot_orders (demo_token, created_at desc)" in d5


def test_private_counting_semantics_are_left_exactly_as_they_were():
    """The private query is preserved verbatim, deliberately.

    run-live has always counted every row for the token, not only market
    rows. That is arguably a bug, but fixing it here would silently grant
    every live prospect link more trades than it was sold with.
    """
    fn = _fn("async def run_live_demo(", "def _owned_tickets(")
    assert 'if purpose == PURPOSE_SHOWCASE:' in fn
    assert 'return len((sb.table("bot_orders").select("id")' in fn
    assert '.eq("demo_token", tok).execute()).data or [])' in fn


def test_a_busy_showcase_is_not_described_as_used_up():
    mod = _load()
    busy = mod._runs_exhausted("showcase", 6, 6, 60)
    assert busy["error"] == "showcase_busy"
    assert "busy" in busy["detail"]
    assert busy["window_minutes"] == 60
    for wrong in ("permanently", "used up", "3 trades", "fresh link"):
        assert wrong not in busy["detail"], wrong

    spent = mod._runs_exhausted("private_demo", 3, 3, None)
    assert spent["error"] == "demo_runs_exhausted"
    assert "3 trades" in spent["detail"]      # unchanged wording


def test_exhaustion_is_a_429_on_both_paths():
    for name, end in (("async def run_live_demo(", "def _owned_tickets("),
                      ("async def demo_control(", "@demo_router.get\nasync def demo_trailing")):
        fn = _fn(name, end)
        assert "HTTP_429_TOO_MANY_REQUESTS" in fn
        assert "_runs_exhausted(purpose" in fn


# ── G / H: AI ──────────────────────────────────────────────────────────
def test_showcase_may_still_draft():
    fn = _fn("async def demo_ai_draft(", "class AISendIn")
    assert "PURPOSE_SHOWCASE" not in fn      # no gate at all
    assert "no verified trades yet" in fn    # the existing rule stands


def test_showcase_may_never_send_to_the_channel():
    fn = _fn("async def demo_ai_send(", "def _deliver_demo_signal_text(")
    assert "PURPOSE_SHOWCASE" in fn
    assert '"showcase_send_disabled"' in fn
    assert "HTTP_403_FORBIDDEN" in fn
    # and the gate is before any delivery work
    assert fn.index("showcase_send_disabled") < fn.index("policy.validate")


def test_the_send_gate_cannot_be_reached_by_a_private_demo():
    fn = _fn("async def demo_ai_send(", "def _deliver_demo_signal_text(")
    assert '(link.get("purpose") or PURPOSE_PRIVATE) == PURPOSE_SHOWCASE' in fn


# ── I: no new authority ────────────────────────────────────────────────
def test_showcase_status_grants_nothing_beyond_its_own_link():
    # revoke, create and list all still demand a platform admin
    for name in ("async def revoke_demo_link(", "async def create_demo_link(",
                 "async def list_demo_links("):
        fn = _fn(name, "\n\n\n")
        assert "rules.is_platform_admin(user)" in fn, name
        assert "platform admin only" in fn, name


def test_the_demo_guard_is_untouched_by_purpose():
    fn = _fn("def _demo_guard(", "@demo_router.post")
    assert "PURPOSE" not in fn and "showcase" not in fn
    assert "observed != expected" in fn
    assert "SKLZ_DEMO_MT5_LOGIN is not configured" in fn


def test_every_action_still_passes_the_guard_before_the_budget():
    for name, end in (("async def run_live_demo(", "def _owned_tickets("),
                      ("async def demo_control(", "@demo_router.get\nasync def demo_trailing")):
        fn = _fn(name, end)
        assert fn.index("_demo_guard(sb)") < fn.index("_run_budget("), name


def test_the_symbol_and_lot_whitelist_is_untouched():
    assert "CONTROL_ACTIONS = {" in API
    fn = _fn("def _demo_symbol(", "DEMO_SL_PIPS")
    assert "DEMO_SYMBOLS[key]" in fn


# ── J: a malformed command id is not a 500 ─────────────────────────────
def test_a_malformed_command_id_is_a_deliberate_404():
    fn = _fn("async def demo_run_state(", "def _tg_message_url(")
    assert "_uuid.UUID(str(command_id))" in fn
    assert "HTTP_404_NOT_FOUND" in fn
    # and it happens before the database is asked anything
    assert fn.index("_uuid.UUID") < fn.index("def _get():")


def test_the_uuid_guard_accepts_what_postgres_would():
    import uuid as u
    mod = _load()
    good = str(u.uuid4())
    assert mod._uuid.UUID(good)          # the same call the handler makes
    for bad in ("not-a-uuid", "", "12345", "../etc/passwd"):
        try:
            mod._uuid.UUID(bad)
            raise AssertionError(f"{bad!r} should not parse")
        except (ValueError, AttributeError, TypeError):
            pass


# ── loader ─────────────────────────────────────────────────────────────
_MOD = None


def _load():
    """Import orders_api with its heavy dependencies stubbed.

    The helpers under test are pure; the module imports Supabase and the
    rest of the app, which a unit test has no business needing.
    """
    global _MOD
    if _MOD is not None:
        return _MOD
    import types
    import importlib.util

    for name in ("policy", "provider_rules", "aio", "auth", "db", "routing",
                 "supabase", "fastapi", "pydantic"):
        if name in sys.modules:
            continue
        sys.modules[name] = types.ModuleType(name)

    fastapi = sys.modules["fastapi"]

    class _R:
        def __init__(self, *a, **k): pass
        def get(self, *a, **k): return lambda f: f
        def post(self, *a, **k): return lambda f: f

    fastapi.APIRouter = _R
    fastapi.Depends = lambda *a, **k: None
    fastapi.Request = object
    fastapi.Header = lambda **k: None

    class _HTTPException(Exception):
        def __init__(self, status_code=500, detail=None):
            self.status_code, self.detail = status_code, detail

    fastapi.HTTPException = _HTTPException

    class _Status:
        def __getattr__(self, n): return int(n.split("_")[1])

    fastapi.status = _Status()

    pyd = sys.modules["pydantic"]

    class _BaseModel:
        pass

    pyd.BaseModel = _BaseModel
    sys.modules["supabase"].Client = object
    sys.modules["aio"].offload = lambda fn, *a, **k: fn(*a, **k)
    sys.modules["auth"].get_current_user = lambda *a, **k: None
    sys.modules["db"].get_supabase = lambda *a, **k: None
    sys.modules["routing"].RoutingScope = object
    sys.modules["routing"].resolve_destinations = lambda *a, **k: []
    sys.modules["policy"].validate = lambda *a, **k: (True, "")
    sys.modules["provider_rules"].is_platform_admin = lambda u: False

    spec = importlib.util.spec_from_file_location("orders_api_under_test",
                                                  "./orders_api.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _MOD = mod
    return mod
