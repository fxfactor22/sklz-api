"""D1.1 — one canonical ruleset, two profiles, no weakening."""
import sys
sys.path.insert(0, ".")
import policy


# ── Arabic: claims blocked ───────────────────────────────────────────
def test_arabic_guaranteed_profit_is_blocked():
    for text in ("هذا ربح مضمون لكل المشتركين",
                 "أرباح مضمونة كل شهر",
                 "تداول بدون مخاطر معنا",
                 "استثمر معنا وسوف تضاعف رأس مالك"):
        ok, why = policy.validate(text, strict=False)
        assert not ok, text
        assert why == "banned_phrase", (text, why)


def test_arabic_performance_percentages_are_blocked():
    for text in ("نسبة نجاح 70% من الصفقات",
                 "دقة تصل إلى ٨٠٪",
                 "معدل النجاح مرتفع جداً"):
        ok, _ = policy.validate(text, strict=False)
        assert not ok, text


def test_arabic_disguised_ratio_is_blocked():
    for text in ("ثلاث صفقات من عشر قد تخسر",
                 "سبع من أصل عشر صفقات ناجحة",
                 "٣ من ١٠ صفقات تخسر"):
        ok, why = policy.validate(text, strict=False)
        assert not ok, text
        assert why == "implied_win_rate", (text, why)


# ── Arabic: the false-positive that matters ──────────────────────────
def test_legitimate_arabic_risk_percentage_is_allowed():
    """The single most useful sentence in retail trading education.

    Refusing it would make the channel worse at exactly what it is for,
    which is why the daily-content profile is strict=False.
    """
    for text in ("لا تخاطر بأكثر من ١-٢٪ من رأس المال في صفقة واحدة",
                 "قاعدة بسيطة: 1% من الحساب لكل صفقة",
                 "خصم ٢٠٪ على الاشتراك السنوي"):
        ok, why = policy.validate(text, strict=False)
        assert ok, (text, why)


def test_the_same_bare_percentage_is_refused_in_strict_fields():
    """A short mechanical field has no business carrying a percentage."""
    ok, why = policy.validate("filled — desk is running at 82% today")
    assert not ok and why == "percentage_claim"
    # and the education sentence WOULD be refused in a strict field,
    # which is correct: it does not belong in a trade comment
    ok, _ = policy.validate("١-٢٪ من رأس المال", strict=True)
    assert not ok


# ── English / Russian unchanged ──────────────────────────────────────
def test_english_rules_unchanged():
    assert not policy.is_clean("our win rate is strong", strict=False)
    assert not policy.is_clean("7 out of 10 trades hit target", strict=False)
    assert not policy.is_clean("closed +80 pips", strict=False)
    assert not policy.is_clean("guaranteed returns", strict=False)
    assert policy.is_clean("Trading leveraged products carries risk.",
                           strict=False)


def test_russian_prose_is_not_falsely_blocked():
    for text in ("Дисциплина важнее прогноза. Не рискуйте более 1-2% "
                 "капитала в одной сделке.",
                 "СИМУЛЯЦИЯ — демонстрационный сигнал, не реальная сделка"):
        ok, why = policy.validate(text, strict=False)
        assert ok, (text, why)


# ── consolidation itself ─────────────────────────────────────────────
def test_no_rule_was_lost_in_the_merge():
    """Every term the Arabic module enforced before must still be enforced."""
    previously_enforced = ["مضمون", "بدون مخاطر", "بلا مخاطر", "ربح مؤكد",
                           "أرباح مضمونة", "لا خسارة", "استثمر معنا",
                           "تضاعف", "guaranteed", "risk-free"]
    canonical = [t.lower() for t in
                 policy.BANNED_AR + policy.BANNED_LATIN_EXTRA
                 + policy.BANNED_EN]
    for term in previously_enforced:
        assert term.lower() in canonical, f"rule lost in consolidation: {term}"


def test_arabic_module_has_no_duplicate_rule_definitions():
    src = open("./sklz_arabic.py").read()
    for gone in ("_RATIO = re.compile", "_PCT_CLAIM = re.compile",
                 "_NUM = r", '"مضمون", "بدون مخاطر"'):
        assert gone not in src, f"duplicate rule still present: {gone}"
    assert "import policy" in src
    assert "policy.validate(" in src


def test_both_paths_reference_one_source():
    ar = open("./sklz_arabic.py").read()
    demo = open("./demo_api.py").read()
    sim = open("./demo_sim.py").read()
    assert "policy.validate(" in ar
    assert "policy.validate(" in demo
    assert "policy" in sim
    # and neither defines its own
    for src in (ar, demo, sim):
        assert "BANNED_EN = [" not in src
        assert "_PCT_EN = re.compile" not in src


def test_final_rendered_content_is_what_gets_validated():
    """Not the template — the string after variables land in it."""
    ar = open("./sklz_arabic.py").read()
    fn = ar[ar.index("def compose_debug("):]
    fn = fn[:fn.index("\ndef ")]
    assert "sanitize(raw)" in fn
    order = fn.index("sanitize(raw)") < fn.index("policy.validate(")
    assert order, "validation must run after rendering, not before"
