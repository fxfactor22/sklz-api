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
