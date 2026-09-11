"""Item 5 — Telegram sales funnel."""
SRC = open("tgbot.py").read()


def test_the_funnel_has_its_own_entry():
    assert 'payload.startswith("demo")' in SRC
    assert '"source": "telegram_demo_funnel"' in SRC


def test_the_arabic_path_is_untouched():
    """Different payload, different branch. They never meet."""
    assert 'payload.startswith("ar")' in SRC
    i_demo = SRC.index('payload.startswith("demo")')
    i_ar = SRC.index('payload.startswith("ar")')
    assert i_demo < i_ar          # demo returns before the Arabic branch
    fn = SRC[i_demo:i_ar]
    assert 'return {"ok": True}' in fn
    assert '"lang": "ar"' not in fn


def test_all_five_questions_are_asked():
    for stage in ("type", "workflow", "audience", "accounts", "problem"):
        assert f'"{stage}": (' in SRC, stage
    assert '_F_ORDER = ["type", "workflow", "audience", "accounts", "problem"]' in SRC


def test_state_reuses_the_existing_lead_record():
    """Upsert on chat_id — a returning prospect updates, never duplicates."""
    assert "_save_lead(sb, chat_id" in SRC
    assert 'on_conflict="chat_id"' in SRC
    fn = SRC[SRC.index('if data.startswith("f:")'):]
    fn = fn[:fn.index('if data.startswith("lang:")')]
    assert 'f"f_{stage}": val' in fn


def test_prices_come_from_the_central_config():
    fn = SRC[SRC.index("def _f_summary("):SRC.index("\n\n# ── flow")]
    assert "orders_api._package_config()" in fn
    for hard in ("499", "999", "1499", "$49", "$99", "$149"):
        assert hard not in fn, hard


def test_the_recommendation_is_explainable():
    fn = SRC[SRC.index("def _f_recommend("):SRC.index("def _f_summary(")]
    for pkg in ("pro_trader_os", "signal_desk_pro", "signal_desk"):
        assert pkg in fn, pkg
    # every branch returns a reason, not just a package
    assert fn.count("return (") == 4


def test_the_copier_wording_is_exact():
    assert ("Multi-account copying available with Signal Desk "
            '"\n            "implementation.') in SRC


def test_no_promises_are_made():
    fn = SRC[SRC.index("_F_WELCOME = ("):SRC.index("\n\n# ── flow")]
    low = fn.lower()
    for banned in ("profit", "win rate", "guarantee", "testimonial",
                   "customers use", "proven results", "%"):
        assert banned not in low, banned
    assert "Not financial advice" in fn


def test_all_four_ctas_are_offered():
    fn = SRC[SRC.index("def _f_summary("):SRC.index("\n\n# ── flow")]
    for cta in ("Try the live demo", "View packages", "Activate",
                "Talk to a human"):
        assert cta in fn, cta


def test_human_handoff_marks_the_lead():
    fn = SRC[SRC.index('if data.startswith("f:")'):]
    fn = fn[:fn.index('if data.startswith("lang:")')]
    assert '"human_requested": True' in fn
    assert '"step": "f_human"' in fn


def test_the_funnel_fields_are_additive_only():
    sql = open("migrations/D8-migration.sql").read()
    assert "add column if not exists" in sql
    for col in ("f_type", "f_workflow", "f_audience", "f_accounts",
                "f_problem", "recommended", "human_requested"):
        assert col in sql, col
    # nothing existing is dropped, renamed or re-typed
    for destructive in ("drop column", "alter column", "rename", "drop table"):
        assert destructive not in sql.lower(), destructive


def test_admin_can_see_funnel_leads():
    api = open("orders_api.py").read()
    assert "async def list_telegram_leads(" in api
    fn = api[api.index("async def list_telegram_leads("):]
    fn = fn[:fn.index('@leads_router.get("")')]
    assert "rules.is_platform_admin(user)" in fn
    assert 'table("tg_leads")' in fn
    assert '"telegram_demo_funnel"' in fn
    assert "waiting_for_human" in fn


def test_the_admin_view_reads_the_existing_table():
    """No second place for a lead to live."""
    api = open("orders_api.py").read()
    fn = api[api.index("async def list_telegram_leads("):]
    fn = fn[:fn.index('@leads_router.get("")')]
    assert "insert(" not in fn and "update(" not in fn
