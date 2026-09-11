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
    # step labels now describe REAL backend states rather than a script
    for phrase in ("Request accepted", "Runner dispatched", "Broker filled",
                   "sent to Telegram", "Slave account A copied",
                   "Slave account B copied", "Slave account C skipped",
                   "Auto-close scheduled"):
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
    # payment goes through SKLZ's own checkout, nothing else
    assert "/api/billing/checkout-package" in HTML


def test_expiry_is_real_not_fake_scarcity():
    """The 48h window is now issued and enforced by the server, so the
    page no longer computes it from a browser clock."""
    assert "DEMO_HOURS = 48" in API
    assert "seconds_remaining" in API and "seconds_remaining" in HTML
    assert "remaining" in HTML
    # the browser cannot extend it
    assert "48*3600*1000" not in HTML
    assert "This private demo has ended" in HTML


def test_simulation_is_declared():
    """The page now distinguishes what is REAL from what is a preview,
    rather than calling the whole thing simulated."""
    assert "SIMULATED PREVIEW" in HTML
    assert "ILLUSTRATIVE FIGURES" in HTML
    assert "no subscriber" in HTML          # nothing pretends to have copied
    assert ">REAL<" in HTML                  # and the real parts say so
    assert "broker DEMO account" in HTML


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


def test_the_combined_total_is_quoted_because_it_is_charged():
    """Earlier the rule was: never show a combined total, because the
    checkout charged only one part. The checkout now charges setup AND
    the first month in one session, so quoting the total is the honest
    presentation — and hiding it would be the misleading one."""
    assert '"label": "due today"' in API
    assert "due today" in HTML
    # never a bare total: the parts are always alongside it
    assert '"breakdown"' in API
    assert "one-time implementation" in HTML and "per month" in HTML
    # the permanent page explains both halves rather than a single number
    assert "one-time implementation plus an ongoing monthly service" in PAGE
    assert "not one or the other" in PAGE


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
    assert 'payment_type:"setup_plus_initial_month"' in HTML
    assert "activation?.[" in HTML


def test_prices_remain_configurable_by_env():
    for env in ("SKLZ_PRICE_", "SKLZ_SOL_", "SKLZ_WALLET_", "SKLZ_PKG_"):
        assert env in API, env


# ── purchase semantics: setup AND monthly, never a choice ────────────
def test_the_either_or_toggle_is_gone():
    """A customer does not choose between implementation and service."""
    for bad in ("setCharge(", 'id="ch_setup"', 'id="ch_monthly"',
                "One-time setup</button>", "Monthly service</button>"):
        assert bad not in HTML, bad


def test_both_charges_are_presented_together():
    assert 'id="dueBox"' in HTML
    assert "one-time implementation" in HTML
    assert "per month" in HTML
    assert "due today" in HTML


def test_activation_total_is_setup_plus_first_month():
    """The checkout charges both, so the page states both."""
    assert '"usd": setup + monthly' in API
    assert '"breakdown": f"${setup:,} setup + ${monthly:,} first month"' in API
    assert '"label": "due today"' in API


def test_stripe_charges_setup_and_subscription_in_one_session():
    assert "/api/billing/checkout-package" in HTML
    assert '"mode": "subscription"' in BILL
    fn = BILL[BILL.index("async def checkout_package("):]
    fn = fn[:fn.index("@router.post(\"/checkout-public\")")]
    assert 'keys["setup"]' in fn and 'keys["monthly"]' in fn
    assert fn.count('"quantity": 1') == 2      # two line items


def test_signal_desk_can_never_be_billed_on_a_retail_price():
    assert "RETAIL_KEYS" in BILL
    for retail in ("copy_basic_monthly", "copy_pro_monthly", "bundle_monthly",
                   "suite_monthly", "gpt_monthly"):
        assert retail in BILL.split("RETAIL_KEYS = {")[1].split("}")[0], retail
    fn = BILL[BILL.index("async def checkout_package("):]
    assert "if k in RETAIL_KEYS:" in fn
    assert "mapped to a retail product" in fn
    # the package map itself points only at Signal Desk keys
    m = BILL.split("SIGNAL_DESK_PACKAGES = {")[1].split("}\n")[0]
    for retail in ("copy_", "bundle_", "suite_", "gpt_"):
        assert retail not in m, retail


def test_setup_must_be_one_time_and_service_recurring():
    fn = BILL[BILL.index("async def checkout_package("):]
    assert 'setup_interval is not None or mon_interval != "month"' in fn


def test_crypto_order_knows_what_it_bought():
    for t in ("setup", "monthly", "setup_plus_initial_month", "renewal"):
        assert f'"{t}"' in API, t
    assert "PAYMENT_TYPES" in API
    assert '"payment_type": ptype' in API
    assert 'payment_type:"setup_plus_initial_month"' in HTML


def test_information_required_status_exists():
    assert '"information_required"' in API
    sql = open("migrations/D2-migration.sql").read()
    assert "information_required" in sql
    assert "crypto_orders_payment_type_valid" in sql


def test_crypto_renewal_expectation_is_stated():
    assert "Renewals are paid monthly in USDT or" in HTML
    assert HTML.count("Renewals are paid monthly") == 2   # both assets


def test_qr_encodes_exactly_the_configured_wallet():
    """QR value == displayed address == backend configuration, by
    construction: one variable feeds all three."""
    # backend is the only source
    assert "CFG.usdt_address=w.usdt_trc20.address" in HTML
    assert "CFG.sol_address=w.sol.address" in HTML
    # the same variable is displayed and encoded
    assert 'document.getElementById("addrUsdt").textContent=CFG.usdt_address' in HTML
    assert 'qr("qrUsdt",CFG.usdt_address)' in HTML
    assert 'document.getElementById("addrSol").textContent=CFG.sol_address' in HTML
    assert 'qr("qrSol",CFG.sol_address)' in HTML
    # and the encoder passes the value through untouched
    fn = HTML[HTML.index("function qr(el,data){"):]
    fn = fn[:fn.index("\n}")]
    assert "encodeURIComponent(data)" in fn


def test_setup_value_is_explained_on_both_pages():
    assert "What the setup fee buys" in PAGE
    assert "What the monthly fee buys" in PAGE
    assert "go-live" in PAGE.lower()
    assert "VPS · MT5 · SKLZ Runner" in HTML or "SKLZ Runner" in HTML


def test_pro_trader_os_monthly_value_is_specific():
    assert "website hosting" in PAGE.lower()
    assert "ai services allowance" in PAGE.lower()
    low = PAGE.lower()
    assert "unlimited ai" not in low


def test_catalog_map_is_read_only_and_admin_only():
    fn = BILL[BILL.index("async def catalog_map("):]
    fn = fn[:fn.index('@router.post("/admin/setup")')]
    assert "_require_admin(user)" in fn
    for write in ("Price.create", "Product.create", "Session.create",
                  ".modify(", ".delete("):
        assert write not in fn, write
    # it verifies against Stripe rather than trusting the catalog
    assert "stripe_amount" in fn and '"matches"' in fn


def test_catalog_map_never_returns_a_credential():
    fn = BILL[BILL.index("async def catalog_map("):]
    fn = fn[:fn.index('@router.post("/admin/setup")')]
    assert "STRIPE_SECRET_KEY" in fn          # only to read the mode prefix
    assert '[:8]' in fn                        # never the whole key
    # check the CODE, not the docstring that names what it must not return
    code = fn.split('"""', 2)[2]
    for secret in ("webhook", "sk_live", "whsec"):
        assert secret not in code.lower(), secret
    # the key is read ONLY as an 8-char prefix, and only to label the mode
    assert 'STRIPE_SECRET_KEY", "")[:8]' in code
    assert code.count("prefix") == 2      # the assignment and the mode line
    assert '"mode": "TEST" if "test" in prefix else "LIVE"' in code


# ── D4: private prospect links ───────────────────────────────────────
GEN = open("tests/fixtures/demo-generator.html").read()


def test_the_link_is_the_credential_so_it_is_unguessable():
    assert "secrets.token_hex(16)" in API      # 128 bits
    fn = API[API.index("async def read_demo_link("):]
    assert "len(tok) != 32" in fn              # shape checked before lookup


def test_expiry_is_decided_by_the_server_not_the_browser():
    fn = API[API.index("async def read_demo_link("):]
    assert "if exp <= now:" in fn
    assert "HTTP_410_GONE" in fn
    # the page treats its countdown as a display only
    assert "The countdown is a display" in HTML
    assert "expired()" in HTML


def test_an_expired_or_missing_link_shows_a_closed_page():
    assert "This private demo has ended" in HTML
    assert "r.status===410" in HTML
    assert 'if(!TOKEN)' in HTML                # no token, no demo


def test_branding_comes_from_the_token_not_the_markup():
    """Falcon FX must not be hard-coded — every prospect sees their own."""
    for hard in ("Falcon FX Signals</b>", "@falconfx_signals",
                 "prepared for <b>Falcon FX"):
        assert hard not in HTML, hard
    assert 'id="brandName"' in HTML
    assert "BRAND.name" in HTML
    assert "d.provider_name" in HTML


def test_one_prospect_cannot_read_another():
    """The public path returns one row by token and offers no listing."""
    fn = API[API.index("async def read_demo_link("):]
    fn = fn[:fn.index("@demo_router.get(\"\")")]
    assert '.eq("token", tok)' in fn and ".limit(1)" in fn
    # the listing endpoint is admin-only
    lst = API[API.index('@demo_router.get("")'):]
    assert "rules.is_platform_admin(user)" in lst


def test_creating_a_link_is_admin_only():
    fn = API[API.index("async def create_demo_link("):]
    assert "rules.is_platform_admin(user)" in fn


def test_stripe_hides_itself_when_not_configured():
    """A card button that 503s in front of a prospect is worse than none."""
    assert "SKLZ_STRIPE_SIGNAL_DESK_READY" in API
    assert '"available": stripe_ready' in API
    assert "if(!(d.stripe && d.stripe.available))" in HTML
    assert 'hidePay("stripe")' in HTML
    # and crypto still works, so outreach is never blocked
    assert "usdt_trc20" in API and "sol" in API


def test_the_generator_page_needs_no_new_admin_system():
    assert "auth-token" in GEN                 # reuses the sklzlabs session
    assert "/api/demo-links" in GEN
    assert "Generate private link" in GEN


def test_opens_are_recorded_for_the_operator():
    assert "opened_count" in API and "last_opened_at" in API
    sql = open("migrations/D4-migration.sql").read()
    assert "opened_count" in sql and "first_opened_at" in sql


def test_demo_links_table_is_not_publicly_readable():
    sql = open("migrations/D4-migration.sql").read()
    assert "enable row level security" in sql
    assert "revoke all on public.demo_links from anon, authenticated" in sql


def test_the_generator_reports_why_it_failed():
    """'could not create' made an expired session look like a broken
    generator."""
    g = open("tests/fixtures/demo-generator.html").read()
    assert "Admin session expired. Please sign in again." in g
    assert "r.status===401" in g
    assert "admin token present" in g
    # distinct labels, so 403/500 are never masked as 401
    for label in ("Not permitted (403)", "Rate limited (429)",
                  "Server error ("):
        assert label in g, label


def test_the_generator_never_renders_the_token():
    g = open("tests/fixtures/demo-generator.html").read()
    body = g[g.index("async function make("):]
    assert 'm.textContent=t' not in body
    assert 'innerHTML=t' not in body


def test_a_401_clears_the_stale_session():
    g = open("tests/fixtures/demo-generator.html").read()
    fn = g[g.index("r.status===401"):]
    assert 'localStorage.removeItem("sklz_access")' in fn


def test_no_fallback_auth_path_exists():
    g = open("tests/fixtures/demo-generator.html").read()
    for banned in ("service_role", "SUPABASE_SERVICE_KEY", "railway",
                   "apikey"):
        assert banned not in g.lower(), banned
