"""P1.5b — the in-process probe must be safe, bounded and honest."""
import ast, sys
sys.path.insert(0, ".")

SRC = open("./diagnostics.py").read()


def test_admin_only():
    fn = SRC[SRC.index("async def db_latency("):]
    assert "rules.is_platform_admin(user)" in fn
    assert fn.index("is_platform_admin") < fn.index("offload(_probe")


def test_table_list_is_fixed_and_never_from_the_request():
    """No request field may name a table."""
    fn = SRC[SRC.index("async def db_latency("):]
    assert "PROBE_TABLES" not in fn or "table=" not in fn
    sig = fn[:fn.index(")")]
    for bad in ("table", "query", "sql", "select"):
        assert bad not in sig, bad
    probe = SRC[SRC.index("def _probe("):SRC.index("@router.get")]
    assert "for table, col in PROBE_TABLES" in probe


def test_reads_only_no_writes():
    probe = SRC[SRC.index("def _probe("):SRC.index("@router.get")]
    assert ".select(col).limit(1).execute()" in probe
    for write in (".insert(", ".update(", ".delete(", ".upsert(", ".rpc("):
        assert write not in SRC, write
    assert '"writes_performed": 0' in SRC


def test_bounded_by_both_samples_and_time():
    assert "MAX_SAMPLES = 25" in SRC
    assert "TIME_BUDGET_SECONDS" in SRC
    probe = SRC[SRC.index("def _probe("):SRC.index("@router.get")]
    assert probe.count("TIME_BUDGET_SECONDS") >= 2   # outer and inner loop
    assert "truncated" in probe
    fn = SRC[SRC.index("async def db_latency("):]
    assert "min(int(samples), MAX_SAMPLES)" in fn


def test_only_one_probe_runs_at_a_time():
    fn = SRC[SRC.index("async def db_latency("):]
    assert "if _running:" in fn
    assert "HTTP_409_CONFLICT" in fn
    assert "finally:" in fn and "_running = False" in fn


def test_the_probe_does_not_block_the_event_loop():
    """A measurement that stalls the loop measures itself."""
    fn = SRC[SRC.index("async def db_latency("):]
    assert "await offload(_probe, sb, samples)" in fn
    assert "= _probe(" not in fn


def test_it_reports_where_it_ran():
    assert '"measured": "in_process"' in SRC
    assert "railway run" in SRC       # names the invalid method explicitly


def test_hot_path_counts_match_the_static_analysis():
    for path, calls in (("copy_api.poll", 5), ("bot_ingest.get_command", 3),
                        ("copy_api.master_event", 4),
                        ("journal.bot_ingest", 5), ("copy_api.report", 3)):
        assert f'("{path}", {calls},' in SRC, path


def test_verdict_thresholds_are_explicit():
    assert "p95 < 20" in SRC and "p95 < 60" in SRC
    for v in ("small", "moderate", "significant"):
        assert f'verdict = "{v}"' in SRC


def test_route_cannot_collide_with_the_provider_catch_all():
    assert 'APIRouter(prefix="/api/diagnostics"' in SRC
    assert "/api/providers" not in SRC


def test_no_secret_or_row_data_is_returned():
    """Only timings and counts leave this endpoint."""
    fn = SRC[SRC.index("async def db_latency("):]
    for leak in ("SUPABASE_SERVICE_KEY", "SUPABASE_URL", "token", "secret"):
        assert leak not in fn, leak
    probe = SRC[SRC.index("def _probe("):SRC.index("@router.get")]
    assert ".data" not in probe        # results are timed, never read


# ── P1.5c: measurement hygiene ───────────────────────────────────────
def test_a_percentage_above_100_can_never_be_reported():
    """The endpoint printed 174.8% and 241.7% of wall-clock time. Both
    are impossible; both came from presenting a demand ratio as a duty
    percentage."""
    src = open("./diagnostics.py").read()
    assert "saturated = demand >= 1.0" in src
    assert '"loop_duty_pct": (round(demand * 100, 1) if not saturated' in src
    assert '"required_workers"' in src
    # and the wording says so explicitly
    assert "never a percentage above 100" in src


def test_demand_math_is_correct_at_the_boundary():
    """Reproduces the endpoint's arithmetic."""
    def report(calls_per_sec, p50_ms):
        demand = calls_per_sec * p50_ms / 1000
        saturated = demand >= 1.0
        return {"required_workers": round(demand, 2),
                "loop_duty_pct": (round(demand * 100, 1)
                                  if not saturated else None),
                "saturated": saturated}

    # the real numbers that produced the impossible output
    bad = report(8.1, 215.8)
    assert bad["saturated"] is True
    assert bad["loop_duty_pct"] is None       # not 174.8
    assert bad["required_workers"] == 1.75    # workers, not percent

    # a healthy database
    good = report(8.1, 15.0)
    assert good["saturated"] is False
    assert good["loop_duty_pct"] == 12.2
    assert good["loop_duty_pct"] <= 100


def test_the_pooled_client_is_warmed_before_timing():
    """A TLS handshake in the first sample inflated every earlier run."""
    src = open("./diagnostics.py").read()
    probe = src[src.index("def _probe("):src.index("@router.get")]
    assert probe.index("Warm the connection") < probe.index("started =")
    assert "warmup_ms" in probe
    assert '"warmup_discarded_ms"' in src


def test_network_and_origin_time_are_separated():
    """Connecting took 23ms while a query took 200ms; without the split
    the obvious conclusion is to move the service, which is wrong."""
    src = open("./diagnostics.py").read()
    assert "def _phases(" in src
    for phase in ("dns_ms", "tcp_ms", "tls_ms", "connect_total_ms"):
        assert phase in src, phase
    assert '"origin_time_ms"' in src
    assert "the database platform" in src


def test_the_misleading_tools_probe_is_gone():
    """It opened a new TLS connection per call and measured a cost
    production does not pay."""
    import os
    assert not os.path.exists("./tools/db_latency_probe.py")


def test_conclusions_are_labelled_as_derived_not_observed():
    src = open("./diagnostics.py").read()
    assert '"caveat"' in src
    assert "Derived from measured p50" in src
