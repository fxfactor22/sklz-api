"""Phase 1 — scheduled demo content."""
SRC = open("demo_content.py").read()


def test_only_three_categories_can_publish():
    assert 'CATEGORIES = ("educational", "product", "summary")' in SRC
    fn = SRC[SRC.index("def compose("):SRC.index("def deliver(")]
    assert "if category not in CATEGORIES" in fn


def test_market_and_news_are_disabled_explicitly():
    """A refusal the operator can read, not a silent omission."""
    for c in ("news", "market_outlook", "eurusd_outlook", "gold_outlook",
              "btc_outlook", "central_bank", "economic_calendar",
              "price_commentary"):
        assert f'"{c}"' in SRC, c
    assert "Live market/news source not configured." in SRC
    fn = SRC[SRC.index("def compose("):SRC.index("def deliver(")]
    assert fn.index("if category in DISABLED") < fn.index("_ai(")


def test_the_model_is_forbidden_from_inventing_market_facts():
    sys = SRC[SRC.index("SYSTEM = ("):SRC.index("def compose(")]
    for rule in ("Never state a current price", "news event",
                 "central-bank", "economic-calendar",
                 "must not invent one"):
        assert rule in sys, rule
    assert "Never claim performance" in sys
    assert "Never give financial advice" in sys


def test_a_summary_is_skipped_when_there_is_nothing_real():
    fn = SRC[SRC.index("def _verified_summary("):SRC.index("def _ai(")]
    assert 'eq("status", "succeeded")' in fn
    assert "if not rows:" in fn and "return {}" in fn
    comp = SRC[SRC.index("def compose("):SRC.index("def deliver(")]
    assert "no verified activity to summarise" in comp


def test_the_summary_claims_no_performance():
    comp = SRC[SRC.index("def compose("):SRC.index("def deliver(")]
    assert "State no profit, loss or performance" in comp
    fn = SRC[SRC.index("def _verified_summary("):SRC.index("def _ai(")]
    for invented in ("pnl", "profit", "win", "loss"):
        assert invented not in fn.lower().replace("succeeded", ""), invented


def test_every_post_passes_the_claim_guard():
    comp = SRC[SRC.index("def compose("):SRC.index("def deliver(")]
    assert "policy.validate(text" in comp
    assert comp.index("policy.validate") < comp.rindex("return text")


def test_the_only_destination_is_the_demo_channel():
    assert 'DEMO_CHAT = "-1004489542294"' in SRC
    fn = SRC[SRC.index("def _destination("):SRC.index("def _verified_summary(")]
    assert 'not in (DEMO_CHAT, "@sklzlabsdemo")' in fn
    assert "refused: resolver returned" in fn
    # check the CODE, not the docstring that explains the isolation
    code = SRC.split(chr(34)*3, 2)[2]
    for prod in ("sklzlabsarabic", "TG_ARABIC", "SIGNAL_CHANNEL_ID"):
        assert prod not in code, prod


def test_the_arabic_scheduler_is_not_imported_or_altered():
    """Mirrored in shape, not reconfigured — a change here cannot alter
    what the production channel publishes."""
    assert "sklz_arabic" not in SRC.split(chr(34)*3, 2)[2]
    assert "import policy" in SRC          # the shared guard IS reused


def test_at_most_four_posts_a_day():
    fn = SRC[SRC.index("def _hours("):SRC.index("def _enabled(")]
    assert "[:_max_per_day()]" in fn          # the cap is configurable
    assert "sorted(set(out))" in fn
    cap = SRC[SRC.index("def _max_per_day("):SRC.index("def _slot_for(")]
    assert "min(n, 4)" in cap                 # but never above four


def test_the_scheduler_is_off_unless_enabled():
    assert 'SKLZ_DEMO_CONTENT_ENABLED", "0") == "1"' in SRC
    loop = SRC[SRC.index("def start("):]
    assert "if _enabled():" in loop


def test_one_post_per_hour_slot():
    loop = SRC[SRC.index("def start("):]
    assert "stamp not in seen" in loop and "seen.add(stamp)" in loop


def test_copier_wording_is_exact():
    assert ('COPIER_LINE = ("Multi-account copying available with Signal '
            'Desk "\n               "implementation.")') in SRC
    comp = SRC[SRC.index("def compose("):SRC.index("def deliver(")]
    assert "Do not claim this channel currently executes follower trades." in comp


def test_endpoints_are_admin_only():
    for fn_name in ("async def preview(", "async def publish_now(",
                    "async def content_status("):
        body = SRC[SRC.index(fn_name):]
        body = body[:body.index("\n@router") if "\n@router" in body
                    else body.index("\ndef start(")]
        assert "rules.is_platform_admin(user)" in body, fn_name


def test_a_scheduler_fault_cannot_crash_the_loop():
    loop = SRC[SRC.index("def start("):]
    assert "except Exception as exc" in loop
    assert "await asyncio.sleep(300)" in loop


def _cfg():
    import ast as _a
    tree = _a.parse(open("demo_content.py").read())
    keep = [n for n in tree.body
            if (isinstance(n, _a.ImportFrom) and n.module == "__future__")
            or (isinstance(n, _a.Import) and n.names[0].name == "os")
            or (isinstance(n, _a.Assign)
                and getattr(n.targets[0], "id", "") == "CATEGORIES")
            or (isinstance(n, _a.FunctionDef)
                and n.name in ("_allowed", "_max_per_day", "_hours"))]
    ns = {}
    exec(compile(_a.Module(body=keep, type_ignores=[]), "x", "exec"), ns)
    return ns


def test_categories_can_be_switched_off():
    import os
    ns = _cfg()
    os.environ["SKLZ_DEMO_CONTENT_CATEGORIES"] = "educational"
    assert ns["_allowed"]() == ("educational",)
    os.environ["SKLZ_DEMO_CONTENT_CATEGORIES"] = "educational,summary"
    assert set(ns["_allowed"]()) == {"educational", "summary"}
    # nonsense falls back to all three rather than silencing the channel
    os.environ["SKLZ_DEMO_CONTENT_CATEGORIES"] = "news,nonsense"
    assert ns["_allowed"]() == ns["CATEGORIES"]
    os.environ.pop("SKLZ_DEMO_CONTENT_CATEGORIES")


def test_a_switched_off_category_refuses_to_compose():
    src = open("demo_content.py").read()
    fn = src[src.index("def compose("):src.index("def deliver(")]
    assert "switched off by configuration" in fn
    assert fn.index("_allowed()") < fn.index("_ai(")


def test_max_per_day_is_capped_at_four():
    import os
    ns = _cfg()
    for raw, want in (("2", 2), ("9", 4), ("0", 0), ("junk", 4)):
        os.environ["SKLZ_DEMO_CONTENT_MAX_PER_DAY"] = raw
        assert ns["_max_per_day"]() == want, raw
    os.environ.pop("SKLZ_DEMO_CONTENT_MAX_PER_DAY")


def test_status_lists_the_controls():
    src = open("demo_content.py").read()
    for k in ("SKLZ_DEMO_CONTENT_ENABLED", "SKLZ_DEMO_CONTENT_HOURS",
              "SKLZ_DEMO_CONTENT_CATEGORIES",
              "SKLZ_DEMO_CONTENT_MAX_PER_DAY"):
        assert k in src, k
    assert '"allowed_categories"' in src
