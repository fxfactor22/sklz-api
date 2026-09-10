"""D2 — the sales demo page and order capture."""
import re, sys
sys.path.insert(0, ".")
HTML = open("tests/fixtures/signal-desk-demo.html").read()
API = open("./orders_api.py").read()


def test_the_promise_is_the_first_thing_visible():
    head = HTML[:HTML.index("</div>", HTML.index('class="hero"'))]
    assert "Trade once." in head and "Distribute everywhere." in head


def test_all_eight_flow_steps_exist_in_order():
    steps = re.findall(r'data-s="(\d)"', HTML)
    assert steps == ["1","2","3","4","5","6","7","8"], steps
    for phrase in ("Master trade opened", "SKLZ Runner detected",
                   "Signal generated", "Telegram message sent",
                   "Slave account A copied", "Slave account B copied",
                   "Slave account C skipped", "Journal &amp; analytics"):
        assert phrase in HTML, phrase


def test_the_paused_account_is_skipped_not_forced():
    assert "copying paused by the subscriber" in HTML
    assert 'class="step skip"' in HTML


def test_both_packages_and_their_features():
    assert 'data-pkg="signal_desk"' in HTML
    assert 'data-pkg="pro_trader_os"' in HTML
    for f in ("SKLZ Runner", "Master MT5 connection", "Telegram distribution",
              "Fixed Lot", "Auto Scale", "Trading journal", "Provider dashboard"):
        assert f in HTML, f
    for f in ("Branded website", "CRM and unified inbox", "AI sales assistant",
              "Academy", "Marketing automation", "ISKRA business tools"):
        assert f in HTML, f


def test_all_three_payment_methods():
    for m in ('data-pay="stripe"', 'data-pay="usdt"', 'data-pay="sol"'):
        assert m in HTML, m


def test_trc20_warning_is_unmissable():
    assert "USDT — TRC20 ONLY" in HTML
    assert "sending on any other network loses the funds" in HTML
    assert "SOL — SOLANA NETWORK ONLY" in HTML


def test_wallets_qr_and_copy_buttons():
    assert 'id="qrUsdt"' in HTML and 'id="qrSol"' in HTML
    assert HTML.count("Copy address") == 2
    assert HTML.count("Copy amount") == 2


def test_no_stripe_connect_or_payouts():
    """Customer -> SKLZ Labs only. Checked by Stripe Connect's actual
    markers, not by the word "connect", which the page uses legitimately
    for connected trading accounts."""
    low = HTML.lower()
    for marker in ("acct_", "transfer_data", "on_behalf_of",
                   "application_fee", "payout", "stripe connect"):
        assert marker not in low, marker
    # payment goes through the existing public checkout, nothing else
    assert "/api/billing/checkout-public" in HTML


def test_expiry_is_real_not_fake_scarcity():
    assert "48*3600*1000" in HTML
    assert "remaining" in HTML
    # it must be able to expire
    assert 'Private demo — expired' in HTML


def test_simulation_is_declared():
    assert "Simulated execution" in HTML
    assert "no live\n          market order is placed" in HTML or \
        "no live" in HTML
    assert "are simulated" in HTML


def test_risk_language_present_and_no_performance_claim():
    assert "not financial advice" in HTML.lower()
    assert "risk of loss" in HTML.lower()
    for claim in ("guaranteed", "win rate", "risk-free", "profit factor"):
        assert claim not in HTML.lower(), claim


def test_the_onboarding_steps_follow_payment():
    for s in ("Verification", "Runner setup", "Channel connection",
              "First subscribers"):
        assert s in HTML, s


# ---- order capture ----
def test_order_statuses_match_the_brief():
    for s in ("awaiting_payment", "submitted", "verifying", "confirmed",
              "activation_pending", "activated"):
        assert s in API, s


def test_duplicate_tx_hash_is_one_order():
    assert "crypto_orders_tx_uniq" in API
    assert '"duplicate": True' in API
    sql = open("migrations/D2-migration.sql").read()
    assert "crypto_orders_tx_uniq" in sql and "lower(tx_hash)" in sql


def test_submission_is_validated():
    assert "_EMAIL.match(email)" in API
    assert "len(tx) < 12" in API
    assert "PACKAGES" in API and "ASSETS" in API and "NETWORKS" in API


def test_public_submit_is_rate_limited():
    assert "_rate_ok(ip)" in API
    assert "HTTP_429_TOO_MANY_REQUESTS" in API


def test_admin_only_for_listing_and_status():
    for fn in ("async def list_orders(", "async def set_status("):
        body = API[API.index(fn):]
        body = body[:body.index("\n@router") if "\n@router" in body else len(body)]
        assert "rules.is_platform_admin(user)" in body, fn


def test_db_calls_are_offloaded():
    assert "await offload(_insert)" in API
    assert "await offload(_q)" in API
    assert "await offload(_upd)" in API


def test_orders_table_is_not_publicly_readable():
    sql = open("migrations/D2-migration.sql").read()
    assert "enable row level security" in sql
    assert "revoke all on public.crypto_orders from anon, authenticated" in sql


# ── permanent product page ───────────────────────────────────────────
PAGE = open("tests/fixtures/signal-desk.html").read()


def test_permanent_page_leads_with_the_promise():
    assert "Trade once." in PAGE and "Distribute everywhere." in PAGE
    assert "Request private demo" in PAGE
    assert "Explore Signal Desk" in PAGE


def test_it_states_the_pain_before_the_product():
    assert PAGE.index("Your trade happens now") < PAGE.index("Three things")
    assert "Posting by hand" in PAGE
    assert "even though your trade was right" in PAGE


def test_no_execution_promises_are_made():
    low = PAGE.lower()
    # the words may appear ONLY inside a denial
    import re
    for claim in ("guaranteed", "zero slippage", "identical fill",
                  "risk-free", "win rate"):
        for m in re.finditer(re.escape(claim), low):
            ctx = low[max(0, m.start()-70):m.start()]
            assert ("not promise" in ctx or "cannot promise" in ctx
                    or "does not" in ctx), f"{claim}: {ctx[-60:]}"
    assert "does not promise identical" in low
    assert "slippage is real" in low


def test_all_three_pillars_present():
    for p in ("Automated signals", "Copy trading", "Trading control"):
        assert p in PAGE, p


def test_setup_service_is_sold_not_assumed():
    assert "We connect everything for you" in PAGE
    for s in ("VPS prepared", "MT5 installed", "SKLZ Runner installed",
              "Master account connected", "Telegram connected",
              "Copy accounts added", "Go live"):
        assert s in PAGE, s


def test_no_unsupported_capability_claimed():
    low = PAGE.lower()
    for fake in ("pending order", "partial close", "tp1", "tp2", "tp ladder",
                 "trailing stop"):
        assert fake not in low, fake


def test_pricing_is_not_hard_coded_on_either_page():
    """§18: no invented numbers until admin pricing is finalised."""
    import re
    # PRICING must not be hard-coded. Simulated account figures on the
    # demo desk are different — they are labelled demo data, not an offer.
    for label, src, ids in (
            ("product page", PAGE, ("p_desk", "p_os")),
            ("demo page", HTML, ("pr_signal_desk", "pr_pro_trader_os",
                                 "paySum", "amtUsdt", "amtSol"))):
        for i in ids:
            m = re.search(r'id="%s"[^>]*>([^<]*)' % i, src)
            if m:
                assert not re.search(r"\d", m.group(1)), f"{label}:{i}"
    assert "Talk to us about pricing" in PAGE
    assert "/api/orders/packages" in PAGE
    assert "/api/orders/packages" in HTML


def test_lead_capture_posts_to_the_api():
    assert "/api/leads/demo-request" in PAGE
    assert 'id="l_email"' in PAGE and 'id="l_tg"' in PAGE


def test_lead_endpoint_is_validated_and_rate_limited():
    assert "leads_router" in API
    assert '_rate_ok("lead:" + ip)' in API
    assert "_EMAIL.match(email)" in API


def test_packages_endpoint_defaults_to_talk_to_us():
    assert 'display or "Talk to us about pricing"' in API
    assert "SKLZ_PRICE_SIGNAL_DESK" in API
    assert "SKLZ_PRICE_PRO_TRADER_OS" in API
