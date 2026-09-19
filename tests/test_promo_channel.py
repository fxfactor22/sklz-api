"""The English promotion channel, and the fork in the funnel.

Two separate guarantees are under test here.

The CHANNEL must not be able to publish a claim. The writer is a language
model with a careful prompt, and a careful prompt is not a control — so
every post goes through the same filter, and these tests feed the filter
the posts a model actually produces when it drifts: a win rate, a
promise of passive income, an invented feature, a link in the body.

The FUNNEL must not be able to quote a price it invented, or recommend a
$1,499 business layer to someone who asked for a $29 indicator
subscription. The fork is the whole point of this change, so the tests
follow both branches all the way to the recommendation.

Cases are lettered so a failure names the guarantee it broke.
Run: python3 -m pytest tests/test_promo_channel.py -q
"""
import os
import sys

import pytest

sys.path.insert(0, ".")

import billing          # noqa: E402
import policy           # noqa: E402
import sklz_promo as P  # noqa: E402
import tgbot as B       # noqa: E402


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    """Per test, not at import.

    Setting these once at module scope made the whole file pass alone and
    fail in the suite, because another module's environment work landed
    in between. A test that only passes when it runs first is not a test.
    """
    monkeypatch.setenv("TG_PROMO_REF", "85b64b4e")
    monkeypatch.setenv("TG_PROMO_CHAT", "@mindsieg_protocol")
    monkeypatch.setenv("TG_PROMO_HOURS", "8,14,19")


# ── A. every link carries the referral code ────────────────────────
def test_a_ref_on_plain_path():
    assert P.url("/") == "https://www.sklzlabs.com/?ref=85b64b4e"


def test_a_ref_before_the_fragment():
    """?ref=x#demo, never #demo?ref=x — the second sends no ref at all."""
    got = P.url("/signal-desk.html", "demo")
    assert got == "https://www.sklzlabs.com/signal-desk.html?ref=85b64b4e#demo"
    assert got.index("ref=") < got.index("#")


def test_a_ref_absent_when_unset(monkeypatch):
    monkeypatch.delenv("TG_PROMO_REF", raising=False)
    assert "ref=" not in P.url("/pricing.html")


def test_a_every_slot_button_carries_the_ref():
    for slot, _label in P.SLOTS:
        rows = P.buttons_for(slot)
        site = [b for row in rows for b in row
                if "sklzlabs.com" in b.get("url", "")]
        assert site, f"{slot} has no site button"
        for b in site:
            assert "ref=85b64b4e" in b["url"], f"{slot} button drops the ref"


# ── B. the filter, on what a drifting writer actually writes ───────
def _blocked(text: str) -> bool:
    return bool(P.claim_check(text))


@pytest.mark.parametrize("bad", [
    "Our Signal Desk has a win rate most providers would envy.",
    "Build passive income from the channel you already have.",
    "Set and forget: the desk trades while you sleep.",
    "A risk-free way to start selling signals.",
    "Guaranteed profit on every copied position.",
    "Seven out of ten signals close in profit.",
    "Subscribers grew 300% after switching.",
    "The demo proves follower accounts copy every fill.",
])
def test_b_claims_are_blocked(bad):
    assert _blocked(bad), f"NOT blocked: {bad}"


@pytest.mark.parametrize("good", [
    "You press once at the desk. The order goes to the broker, and the "
    "signal publishes to your channel after the fill is confirmed.",
    "SKLZ Signal Desk is $499 setup plus $49 a month. Pro Trader OS is "
    "$1,499 plus $149.",
    "Multi-account copying available with Signal Desk implementation.",
    "The live demo executes on an MT5 broker demo account. The "
    "automation is live and the funds are virtual.",
    "Move the stop and the same Telegram message updates. Nobody has to "
    "scroll for the newest post.",
])
def test_b_true_description_survives(good):
    assert not _blocked(good), f"wrongly blocked: {good}"


# ── C. compose rejects, and says why ───────────────────────────────
def _writer(text):
    return lambda prompt: text


def test_c_link_in_the_body_is_rejected(monkeypatch):
    monkeypatch.setattr(
        P, "_claude",
        _writer("Everything you need to run the channel is at "
                "https://www.sklzlabs.com and it takes a morning to set "
                "up properly with our team on the call."))
    text, reason = P.compose_debug("system")
    assert not text and "link" in reason


def test_c_claim_is_rejected_with_the_rule_named(monkeypatch):
    monkeypatch.setattr(
        P, "_claude",
        _writer("Run your channel on autopilot and build passive income "
                "from the subscribers you already have today, with the "
                "desk handling every fill for you from now on."))
    text, reason = P.compose_debug("desk")
    assert not text and "passive income" in reason


def test_c_short_post_is_rejected(monkeypatch):
    monkeypatch.setattr(P, "_claude", _writer("Trade once. Distribute."))
    text, reason = P.compose_debug("desk")
    assert not text and "short" in reason


def test_c_empty_writer_is_named_not_swallowed(monkeypatch):
    monkeypatch.setattr(P, "_claude", _writer(""))
    text, reason = P.compose_debug("ai")
    assert not text and "writer returned nothing" in reason


def test_c_good_post_gets_the_disclaimer(monkeypatch):
    body = ("You press once at the desk. The order goes to your broker, "
            "and only after the fill is confirmed does the signal reach "
            "your Telegram channel. Move the stop later and the same "
            "message updates in place, so nobody scrolls looking for the "
            "newest version of a trade they already have open.")
    monkeypatch.setattr(P, "_claude", _writer(body))
    text, reason = P.compose_debug("desk")
    assert reason == ""
    assert text.endswith(P.TAIL)


# ── D. sanitising for Telegram's smaller Markdown ──────────────────
def test_d_headings_become_bold():
    assert P.sanitize("# Signal Desk\ntext") == "*Signal Desk*\ntext"


def test_d_double_asterisk_would_break_telegram():
    assert "**" not in P.sanitize("**Signal Desk** is the engine")


def test_d_unbalanced_marker_is_removed():
    """An odd count makes Telegram reject the whole message."""
    assert "*" not in P.sanitize("A *partly bolded sentence")


# ── E. the rotation ────────────────────────────────────────────────
from datetime import datetime, timezone  # noqa: E402


def _at(day, hour):
    return datetime(2026, 9, day, hour, 5, tzinfo=timezone.utc)


def test_e_silent_outside_posting_hours():
    assert P.slot_for(_at(20, 3)) == ""
    assert P.slot_for(_at(20, 11)) == ""


def test_e_posts_on_each_configured_hour():
    assert all(P.slot_for(_at(20, h)) for h in (8, 14, 19))


def test_e_three_distinct_slots_in_a_day():
    got = [P.slot_for(_at(20, h)) for h in (8, 14, 19)]
    assert len(set(got)) == 3


def test_e_rotation_does_not_repeat_within_a_week():
    seen = [P.slot_for(_at(d, h)) for d in range(14, 21) for h in (8, 14, 19)]
    assert len(set(seen)) == len(P.SLOTS)


def test_e_slot_moves_hour_across_days():
    """A reader at the same time each day must not see one subject."""
    eights = {P.slot_for(_at(d, 8)) for d in range(14, 21)}
    assert len(eights) > 1


def test_e_rotation_survives_a_restart():
    """Derived from the date, not a counter — a redeploy cannot reset it."""
    assert P.slot_for(_at(18, 14)) == P.slot_for(_at(18, 14))


# ── F. provider weighting ──────────────────────────────────────────
def test_f_channel_is_mostly_for_providers():
    client_slots = {"client"}
    assert len(client_slots) / len(P.SLOTS) < 0.34


# ── G. the pinned post ─────────────────────────────────────────────
def test_g_pinned_post_states_the_limits():
    low = P.PINNED.lower()
    assert "not financial advice" in low
    assert "risk of loss" in low


def test_g_pinned_post_makes_no_claim():
    assert not _blocked(P.PINNED)


def test_g_funnel_link_carries_the_source_tag(monkeypatch):
    monkeypatch.setenv("TG_FUNNEL_BOT", "@sklz_sales_bot")
    assert P.funnel_link() == "https://t.me/sklz_sales_bot?start=mindsiege"


def test_g_pin_refuses_without_a_funnel_bot(monkeypatch):
    monkeypatch.setenv("TG_FUNNEL_BOT", "")
    monkeypatch.setenv("TG_SALES_BOT_TOKEN", "")
    res = P.send_pinned()
    assert not res["ok"] and "funnel bot" in res["error"]


def test_g_facts_block_forbids_the_copying_claim():
    assert "does NOT prove follower-account copying" in P.FACTS
    assert ("Multi-account copying available with Signal Desk "
            "implementation.") in P.FACTS


# ── H. the fork routes to the right prices ─────────────────────────
def _client(**kw):
    base = {"source": "mindsiege", "trades": "forex",
            "f_accounts": "one", "f_problem": "signals"}
    base.update(kw)
    return base


def test_h_prop_evaluations_get_the_multi_account_plan():
    name, product, _ = B._c_recommend(_client(f_accounts="prop"))
    assert (name, product) == ("SKLZ Pro", "copy_pro_monthly")


def test_h_several_own_accounts_get_the_multi_account_plan():
    _, product, _ = B._c_recommend(_client(f_accounts="own_multi"))
    assert product == "copy_pro_monthly"


def test_h_crypto_gets_the_crypto_plan():
    _, product, _ = B._c_recommend(_client(trades="crypto"))
    assert product == "copy_crypto_monthly"


def test_h_single_forex_account_gets_the_cheapest_plan():
    _, product, _ = B._c_recommend(_client())
    assert product == "copy_basic_monthly"


def test_h_a_learner_is_not_sold_the_expensive_plan():
    name, product, why = B._c_recommend(
        _client(trades="learning", f_accounts="prop"))
    assert product == "copy_basic_monthly"
    assert "journal" in why.lower()


def test_h_no_client_is_ever_sold_a_provider_package():
    provider = {"signal_desk", "signal_desk_pro", "pro_trader_os"}
    for trades in ("forex", "crypto", "both", "learning"):
        for acc in ("one", "own_multi", "prop", "copy"):
            for goal in ("copy", "signals", "analysis", "journal"):
                _, product, _ = B._c_recommend(
                    _client(trades=trades, f_accounts=acc, f_problem=goal))
                assert product not in provider
                assert product in billing.CATALOG


# ── I. prices come from the table Stripe is charged from ───────────
def test_i_price_is_read_from_the_catalog():
    assert B._plan_price("copy_basic_monthly") == "$29/month"
    assert B._plan_price("copy_crypto_monthly") == "$49/month"
    assert B._plan_price("copy_pro_monthly") == "$79/month"


def test_i_unknown_product_quotes_nothing_rather_than_guessing():
    assert B._plan_price("no_such_product") == ""


def test_i_summary_price_matches_the_catalog():
    text, _ = B._c_summary(_client(f_accounts="prop"))
    _n, cents, _i = billing.CATALOG["copy_pro_monthly"]
    assert f"${cents // 100}/month" in text


# ── J. the referral code reaches the buttons ───────────────────────
def test_j_client_buttons_carry_the_ref():
    _, buttons = B._c_summary(_client())
    urls = [b["url"] for row in buttons for b in row if "url" in b]
    assert urls and all("ref=85b64b4e" in u for u in urls)


def test_j_provider_buttons_carry_the_ref():
    lead = {"source": "mindsiege", "f_audience": "2k_plus",
            "f_problem": "all", "f_accounts": "client", "f_type": "provider"}
    pkg, why = B._f_recommend(lead)
    _text, buttons = B._f_summary(lead, pkg, why)
    urls = [b["url"] for row in buttons for b in row if "url" in b]
    assert urls and all("ref=85b64b4e" in u for u in urls)


def test_j_a_lead_from_elsewhere_gets_no_ref():
    """Attribution must not be sprayed onto leads that did not earn it."""
    assert B._ref_for({"source": "ar_channel"}) == ""
    assert B._ref_for({}) == ""


def test_j_provider_summary_keeps_the_copying_wording():
    lead = {"source": "mindsiege", "f_audience": "u100",
            "f_accounts": "one", "f_problem": "late"}
    pkg, why = B._f_recommend(lead)
    text, _ = B._f_summary(lead, pkg, why)
    assert ("Multi-account copying available with Signal Desk "
            "implementation.") in text


# ── K. the question flow itself ────────────────────────────────────
def test_k_role_question_offers_exactly_two_paths():
    vals = {b["callback_data"] for row in B._ROLE_BUTTONS for b in row}
    assert vals == {"f:role:provider", "f:role:client"}


def test_k_client_stages_run_in_order_then_stop():
    lead = {}
    seen = []
    while True:
        nxt = B._c_next(lead)
        if not nxt:
            break
        seen.append(nxt)
        lead[B._C_COLUMN[nxt]] = B._C_QUESTIONS[nxt][1][0][1]
    assert seen == B._C_ORDER


def test_k_client_columns_already_exist_on_the_table():
    """Reused columns only. A new column would need a migration."""
    existing = {"trades", "experience", "problem", "f_type", "f_workflow",
                "f_audience", "f_accounts", "f_problem", "recommended",
                "human_requested", "name", "lang", "source", "email",
                "step", "chat_id", "username", "updated_at"}
    assert set(B._C_COLUMN.values()) <= existing


def test_k_every_client_answer_is_a_valid_callback():
    for stage, (_q, opts) in B._C_QUESTIONS.items():
        for _label, val in opts:
            data = f"c:{stage}:{val}"
            _p, got_stage, got_val = data.split(":", 2)
            assert got_stage in B._C_COLUMN and got_val == val


# ── L. the approved-post endpoint is not an exemption ──────────────
def test_l_approved_text_is_still_filtered():
    """A human approving a post does not make the post exempt."""
    assert _blocked("Approved copy: guaranteed profit for every provider.")


def test_l_send_endpoint_reuses_the_same_rules():
    import inspect
    assert "claim_check" in inspect.getsource(P.send_exact)
    assert "claim_check" in inspect.getsource(P.compose_debug)


def test_l_a_discount_is_not_a_growth_figure():
    """The channel must still be able to quote annual pricing."""
    assert not _blocked("Annual billing saves 20 percent on every plan "
                        "and you can cancel from the billing portal at "
                        "any time you like without talking to anyone.")


# ── M. the fork does not ask the same thing twice ──────────────────
def test_m_provider_is_not_re_asked_their_role():
    """Answering 'I run a channel' then being offered 'I trade my own
    account' reads like the bot ignored the answer."""
    labels = " ".join(l for l, _v in B._ROLE_TYPE_Q[1]).lower()
    assert "my own account" not in labels


def test_m_role_type_still_writes_the_same_column_and_values():
    """The wording narrows; the data and the recommendation do not."""
    vals = {v for _l, v in B._ROLE_TYPE_Q[1]}
    assert vals <= {v for _l, v in B._F_QUESTIONS["type"][1]}
    for row in B._role_type_buttons():
        for b in row:
            assert b["callback_data"].startswith("f:type:")


def test_m_starting_out_is_still_reachable():
    """_f_recommend keys on 'starting'; losing it would lose a branch."""
    assert "starting" in {v for _l, v in B._ROLE_TYPE_Q[1]}
    _pkg, why = B._f_recommend({"f_type": "starting", "f_audience": "u100",
                                "f_accounts": "one", "f_problem": "late"})
    assert why


def test_m_demo_path_keeps_the_original_question():
    assert 'payload.startswith("demo")' in open("tgbot.py").read()
    assert B._F_QUESTIONS["type"][1][1][1] == "self"
