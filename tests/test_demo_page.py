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
    """Feature lists live on the permanent product page. The demo builds
    its cards from the API so pricing and availability cannot drift."""
    for f in ("SKLZ Runner", "Master MT5 connection",
              "Automated Telegram signals", "Fixed Lot / Auto Scale",
              "Journal and analytics", "Signal Desk dashboard"):
        assert f in PAGE, f
    for f in ("Branded website", "CRM and unified inbox",
              "AI response team", "Academy", "Marketing automation"):
        assert f in PAGE, f
    assert 'id="pkgs"' in HTML and "p.stripe.setup" in HTML


def test_all_three_payment_methods():
    for m in ('data-pay="stripe"', 'data-pay="usdt"', 'data-pay="sol"'):
        assert m in HTML, m


def test_trc20_warning_is_unmissable():
    assert "USDT — TRC20 ONLY" in HTML
    assert "SOL — SOLANA NETWORK ONLY" in HTML
    assert HTML.count("Crypto transfers are irreversible") == 2


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
            ("product page", PAGE, ("s_signal_desk", "m_signal_desk",
                                    "s_signal_desk_pro", "m_signal_desk_pro",
                                    "s_pro_trader_os", "m_pro_trader_os")),
            ("demo page", HTML, ("paySum", "amtUsdt", "amtSol"))):
        for i in ids:
            m = re.search(r'id="%s"[^>]*>([^<]*)' % i, src)
            if m:
                assert not re.search(r"\d", m.group(1)), f"{label}:{i}"
    assert "/api/orders/packages" in PAGE
    assert "/api/orders/packages" in HTML


def test_lead_capture_posts_to_the_api():
    assert "/api/leads/demo-request" in PAGE
    assert 'id="l_email"' in PAGE and 'id="l_tg"' in PAGE


def test_lead_endpoint_is_validated_and_rate_limited():
    assert "leads_router" in API
    assert '_rate_ok("lead:" + ip)' in API
    assert "_EMAIL.match(email)" in API


def test_packages_endpoint_serves_the_agreed_defaults():
    """Pricing is final, so defaults are real — and still env-overridable
    so the offer can change without a frontend deploy."""
    for name, setup, monthly in (("signal_desk", 499, 49),
                                 ("signal_desk_pro", 999, 99),
                                 ("pro_trader_os", 1499, 149)):
        assert f'"key": "{name}"' in API, name
        assert f'"setup_usd": {setup}' in API, name
        assert f'"monthly_usd": {monthly}' in API, name
    assert '"custom_from_usd": 1999' in API
    assert 'SKLZ_PRICE_{k}_SETUP' in API or "SKLZ_PRICE_" in API


# ── commercial model ─────────────────────────────────────────────────
BILL = open("./billing.py").read()


def test_six_stripe_products_exist_at_the_agreed_prices():
    for key, cents, interval in (
            ("sd_setup", 49900, "None"), ("sd_monthly", 4900, '"month"'),
            ("sdpro_setup", 99900, "None"), ("sdpro_monthly", 9900, '"month"'),
            ("ptos_setup", 149900, "None"), ("ptos_monthly", 14900, '"month"')):
        import re
        m = re.search(r'"%s":\s*\("([^"]+)",\s*(\d+),\s*(\S+?)\)' % key, BILL)
        assert m, key
        assert int(m.group(2)) == cents, (key, m.group(2))
        assert m.group(3) == interval, (key, m.group(3))


def test_setup_is_one_time_and_monthly_recurring():
    """_checkout picks its mode from the interval, so these must differ."""
    assert '"mode": "subscription" if interval else "payment"' in BILL


def test_old_retail_products_are_not_reused_for_signal_desk():
    """copy_basic_monthly is SKLZ Core at $29 — not a Signal Desk offer."""
    for page in (PAGE, HTML):
        assert "copy_basic_monthly" not in page
        assert "copy_pro_monthly" not in page


def test_no_combined_today_total_is_quoted():
    for page in (PAGE, HTML):
        low = page.lower()
        assert "548" not in low and "$548" not in low
        assert "today" not in low or "setup today" in low
    assert "two separate charges" in PAGE
    assert "two separate charges" in HTML


def test_setup_and_monthly_are_labelled_distinctly():
    assert "one-time setup" in PAGE and "per month" in PAGE
    assert "one-time setup" in HTML


def test_three_tiers_on_both_pages():
    for k in ("signal_desk", "signal_desk_pro", "pro_trader_os"):
        assert k in PAGE, k
    assert 'id="pkgs"' in HTML          # demo builds them from the API


def test_custom_pro_trader_os_is_from_not_charged():
    assert "custom_from" in PAGE
    assert "quoted from" in PAGE.lower()
    # and never auto-charged
    assert "1999" not in HTML


def test_no_wallet_address_is_hard_coded_in_markup():
    """An address in HTML is an address that goes stale unseen."""
    import re
    for name, src in (("product page", PAGE), ("demo page", HTML)):
        # TRON addresses start T and are 34 chars; Solana are base58 32-44
        assert not re.search(r'"T[1-9A-HJ-NP-Za-km-z]{33}"', src), name
        assert "TQmZ4sK9" not in src and "7xKXtg2CW" not in src
    assert "usdt_address: \"\"" in HTML or 'usdt_address: ""' in HTML


def test_wallets_come_from_the_server_and_hide_when_unset():
    assert "WALLET_USDT_TRC20" in API and "WALLET_SOL" in API
    assert "TTjmrD5Vkp4jQUe2ACmo4YGcL8zGkQ3U4s" in API      # confirmed USDT
    assert "8y4eKQDLeoy1P7fAdjXk9cb1bwLCzqjUsV5KgWkQ64Ya" in API  # confirmed SOL
    assert '"available": bool(usdt)' in API
    assert "hidePay(" in HTML


def test_no_private_key_or_seed_is_ever_requested():
    for src in (PAGE, HTML, API):
        low = src.lower()
        for bad in ("private key", "seed phrase", "mnemonic", "secret key"):
            assert bad not in low, bad


def test_sol_amount_is_never_invented():
    """SOL moves and there is no rate feed in this phase."""
    assert "SKLZ_SOL_" in API
    assert '"sol": sol_setup' in API
    assert "request amount" in HTML          # shown when unset
    for old in ("0.85", "2.27"):
        assert old not in HTML, old


def test_usdt_tracks_the_usd_figure():
    assert '"usdt": setup' in API and '"usdt": monthly' in API


def test_irreversibility_warning_on_both_crypto_options():
    assert HTML.count("Crypto transfers are irreversible") == 2
    assert "USDT — TRC20 ONLY" in HTML and "SOL — SOLANA NETWORK ONLY" in HTML


def test_crypto_submission_records_which_charge():
    assert "charge:CFG.charge" in HTML
    assert "setup" in HTML and "monthly" in HTML


def test_prices_remain_configurable_by_env():
    for env in ("SKLZ_PRICE_", "SKLZ_SOL_", "SKLZ_WALLET_", "SKLZ_PKG_"):
        assert env in API, env
