"""P1 — SKLZ → ISKRA provisioning bridge, against contract v1.0."""
import hashlib, hmac, json, os, sys, time
sys.path.insert(0, ".")
import iskra_client as iskra

SECRET = "interop-test-secret"


def setup_function():
    os.environ["ISKRA_PROVISIONING_SECRET"] = SECRET
    os.environ.pop("ISKRA_BASE_URL", None)


# ── authentication ───────────────────────────────────────────────────
def test_signature_matches_the_contract_scheme():
    body = b'{"idempotency_key":"k","provider_id":"p"}'
    header, t = iskra.sign(body, ts="1789030000")
    expect = hmac.new(SECRET.encode(),
                      b"1789030000." + body, hashlib.sha256).hexdigest()
    assert header == f"t=1789030000,v1={expect}"


def test_signature_is_over_raw_bytes_not_reserialised_json():
    """Key order must not change the signature of what was sent."""
    a = '{"b":2,"a":1}'
    b = '{"a":1,"b":2}'
    assert iskra.sign(a.encode(), "1")[0] != iskra.sign(b.encode(), "1")[0]


def test_unicode_signs_over_utf8_bytes():
    body = '{"business_name":"فالكون","note":"демо"}'.encode("utf-8")
    header, _ = iskra.sign(body, "1789030000")
    expect = hmac.new(SECRET.encode(), b"1789030000." + body,
                      hashlib.sha256).hexdigest()
    assert header.endswith(expect)


def test_fingerprint_is_sha256_prefix_and_never_the_secret():
    fp = iskra.secret_fingerprint()
    assert fp == hashlib.sha256(SECRET.encode()).hexdigest()[:12]
    assert len(fp) == 12 and SECRET not in fp


def test_missing_secret_fails_closed():
    os.environ.pop("ISKRA_PROVISIONING_SECRET", None)
    assert iskra.is_configured() is False
    assert iskra.secret_fingerprint() == ""
    try:
        iskra._request("POST", "/api/provisioning/tenant", {"x": 1})
        raise AssertionError("an unsigned provisioning call was attempted")
    except iskra.IskraError as exc:
        assert exc.status == 503
        assert exc.code == "sklz_secret_not_configured"


def test_demo_secret_is_not_reused():
    src = open("./iskra_client.py").read()
    assert "SKLZ_DEMO_SECRET" not in src
    assert "ISKRA_PROVISIONING_SECRET" in src


# ── idempotency ──────────────────────────────────────────────────────
def test_request_hash_ignores_the_key_and_sorts_fields():
    a = {"idempotency_key": "one", "b": 2, "a": 1}
    b = {"idempotency_key": "two", "a": 1, "b": 2}
    assert iskra.canonical_request_hash(a) == iskra.canonical_request_hash(b)


def test_request_hash_changes_when_the_body_changes():
    a = {"idempotency_key": "k", "business_name": "Falcon FX"}
    b = {"idempotency_key": "k", "business_name": "Falcon FX Ltd"}
    assert iskra.canonical_request_hash(a) != iskra.canonical_request_hash(b)


def test_the_intent_row_is_written_before_the_call():
    """The key must survive the crash that loses the response."""
    src = open("./provisioning.py").read()
    fn = src[src.index("async def provision("):]
    fn = fn[:fn.index("\n# ── owner invite")]
    assert fn.index('table("provisioning_intents").insert') < \
        fn.index("offload(iskra.create_tenant, payload)")


def test_a_retry_reuses_the_stored_key_and_never_mints_a_new_one():
    src = open("./provisioning.py").read()
    fn = src[src.index("async def provision("):]
    fn = fn[:fn.index("\n# ── owner invite")]
    assert 'key, intent_id = intent["idempotency_key"], intent["id"]' in fn
    assert "_new_key" in fn.split("if intent:")[1].split("else:")[1]


def test_one_live_tenant_intent_per_provider():
    sql = open("migrations/P1-migration.sql").read().lower()
    assert "provisioning_intents_one_tenant" in sql
    assert "where kind = 'tenant' and state in ('in_flight', 'succeeded')" in sql


# ── credentials ──────────────────────────────────────────────────────
def test_redaction_reaches_the_nested_invite_token():
    """invite.token is one level down; a top-level strip stores a key."""
    payload = {"ok": True, "tenant": {"id": "8c1f", "slug": "falcon-fx"},
               "invite": {"url": "https://iskra/join?token=inv_secret",
                          "token": "inv_secret",
                          "email": "a@b.c",
                          "expires_at": "2026-09-17T00:00:00Z"}}
    clean = iskra.redact(payload)
    blob = json.dumps(clean)
    assert "inv_secret" not in blob
    assert clean["invite"]["token"] == "[redacted]"
    assert clean["invite"]["url"] == "[redacted]"
    assert clean["invite"]["email"] == "a@b.c"      # not a credential
    assert clean["tenant"]["id"] == "8c1f"


def test_redaction_survives_deep_nesting_and_lists():
    payload = {"a": [{"b": {"c": {"token": "t", "secret": "s",
                                  "keep": "yes"}}}]}
    clean = iskra.redact(payload)
    assert clean["a"][0]["b"]["c"]["token"] == "[redacted]"
    assert clean["a"][0]["b"]["c"]["secret"] == "[redacted]"
    assert clean["a"][0]["b"]["c"]["keep"] == "yes"


def test_the_invite_is_never_persisted_or_audited():
    """The raw invite may reach the RESPONSE and nothing else.

    Checked structurally rather than by matching formatting: every line
    that mentions the raw invite must be building the response.
    """
    src = open("./provisioning.py").read()
    for line in src.splitlines():
        if 'result["invite"]' in line:
            assert line.strip().startswith('out["invite"]'), line.strip()
    # every persisted result and audit detail is redacted first
    assert '"result": iskra.redact(result) if result else None' in src
    assert '"detail": iskra.redact(detail or {})' in src
    # and nothing writes an invite into an intent or audit row
    for call in ("_settle(", "_audit("):
        for chunk in src.split(call)[1:]:
            head = chunk[:chunk.index(")\n")] if ")\n" in chunk else chunk[:200]
            assert '"invite"' not in head, head[:120]


# ── error contract ───────────────────────────────────────────────────
def test_every_contract_status_keeps_its_meaning():
    cases = {
        400: ("bad_request", False, True),
        401: ("bad_signature", False, True),
        404: ("not_provisioned", False, True),
        413: ("body_too_large", False, True),
        500: ("iskra_failed", True, True),      # retry SAME key
        502: ("replay_of_failed", True, False),  # retry NEW key
        503: ("provisioning_not_configured", True, True),
    }
    for code, (name, retryable, same_key) in cases.items():
        exc = iskra.classify(code, {"error": name})
        assert exc.code == name, code
        assert exc.retryable is retryable, code
        assert exc.same_key is same_key, code


def test_409_retryable_is_distinguished_from_409_conflict():
    running = iskra.classify(409, {"retryable": True})
    assert running.retryable is True
    changed = iskra.classify(409, {"error": "conflict"})
    assert changed.retryable is False


def test_a_401_reports_both_fingerprints():
    exc = iskra.classify(401, {"error": "bad_signature",
                               "iskra_key_fingerprint": "7da581dcae60"})
    assert "7da581dcae60" in exc.detail
    assert iskra.secret_fingerprint() in exc.detail


def test_errors_are_not_flattened_into_one_code():
    codes = {iskra.classify(s, {}).code
             for s in (400, 401, 404, 409, 413, 500, 502, 503)}
    assert len(codes) >= 7


def test_transport_failure_is_retryable_with_the_same_key():
    os.environ["ISKRA_BASE_URL"] = "https://nowhere.invalid"
    try:
        iskra.create_tenant({"idempotency_key": "k", "provider_id": "p"})
        raise AssertionError("expected a transport failure")
    except iskra.IskraError as exc:
        assert exc.code == "transport_failed"
        assert exc.retryable is True and exc.same_key is True


# ── mapping ──────────────────────────────────────────────────────────
def test_a_tenant_mismatch_fails_closed():
    src = open("./provisioning.py").read()
    fn = src[src.index("async def provision("):]
    assert 'existing.lower() != tenant_id.lower()' in fn
    assert '"mapping_conflict"' in fn
    # and it must NOT overwrite
    conflict = fn[fn.index("existing.lower() != tenant_id.lower()"):]
    conflict = conflict[:conflict.index("if not existing:")]
    assert 'update({"iskra_tenant_id"' not in conflict


def test_only_a_uuid_becomes_the_mapping():
    src = open("./provisioning.py").read()
    assert "rules.is_valid_uuid(tenant_id)" in src
    # never derived from email, name or slug
    fn = src[src.index("tenant = (result.get"):src.index("existing = ")]
    for bad in ("owner_email", "business_name", "slug"):
        assert bad not in fn


def test_lifecycle_states_match_the_contract():
    src = open("./provisioning.py").read()
    assert 'LIFECYCLE_STATES = ("active", "past_due", "suspended", "offboarded")' in src
    assert 'INVITE_ROLES = ("owner", "manager", "staff", "viewer")' in src


def test_provider_status_is_not_changed_by_a_lifecycle_call():
    """The two lifecycles are independent on purpose."""
    src = open("./provisioning.py").read()
    fn = src[src.index("async def set_lifecycle("):]
    fn = fn[:fn.index("\n# ── status")]
    assert 'table("providers").update' not in fn


def test_status_distinguishes_every_required_case():
    src = open("./provisioning.py").read()
    fn = src[src.index("async def iskra_status("):]
    for state in ("not_provisioned", "linked_active", "linked_past_due",
                  "linked_suspended", "linked_offboarded",
                  "integration_unavailable", "mapping_conflict"):
        assert state in fn, state


def test_provisioning_requires_platform_admin():
    src = open("./provisioning.py").read()
    for fn_name in ("async def provision(", "async def owner_invite(",
                    "async def set_lifecycle(", "async def iskra_diag("):
        fn = src[src.index(fn_name):]
        fn = fn[:fn.index("\n@router") if "\n@router" in fn else len(fn)]
        assert "rules.is_platform_admin(user)" in fn, fn_name


def test_no_email_is_used_as_a_relational_key():
    src = open("./provisioning.py").read()
    assert 'eq("owner_email"' not in src
    assert 'eq("email"' not in src


def test_audit_events_cover_the_required_set():
    src = open("./provisioning.py").read()
    for event in ("provisioning_requested", "provisioning_succeeded",
                  "provisioning_replay_recovered", "provisioning_failed",
                  "tenant_link_stored", "mapping_conflict",
                  "owner_invite_requested", "lifecycle_request",
                  "lifecycle_result"):
        assert f'"{event}"' in src, event


# ── P1.1: route collision, permanently ───────────────────────────────
def test_diag_path_cannot_be_read_as_a_provider_id():
    """`GET /api/providers/{provider_id}` is a single-segment catch-all
    mounted first, and it swallowed `/api/providers/_iskra-diag` whole."""
    src = open("./provisioning.py").read()
    assert '@router.get("/integration/iskra/diag")' in src
    assert '@router.get("/_iskra-diag")' not in src
    # three segments: it cannot match /{provider_id} (one) or
    # /{provider_id}/provision (two)
    path = "/integration/iskra/diag"
    assert len([p for p in path.split("/") if p]) == 3


def test_no_single_segment_static_route_remains_under_providers():
    """Any one-segment static path here is shadowed by the catch-all."""
    import re
    for name in ("provisioning.py", "providers.py"):
        src = open(f"./{name}").read()
        for m in re.finditer(r'@router\.(get|post|patch)\("(/[^"]*)"\)', src):
            path = m.group(2)
            parts = [p for p in path.split("/") if p]
            if len(parts) == 1 and not parts[0].startswith("{"):
                # /mine is defined BEFORE the catch-all in providers.py,
                # which is the one case where order genuinely saves it
                assert name == "providers.py" and parts[0] == "mine", path


def test_mine_is_declared_before_the_catch_all():
    src = open("./providers.py").read()
    assert src.index('@router.get("/mine")') < \
        src.index('@router.get("/{provider_id}")')


def test_diag_and_proof_stay_platform_admin_only():
    src = open("./provisioning.py").read()
    for fn in ("async def iskra_diag(", "async def interop_proof("):
        body = src[src.index(fn):]
        body = body[:body.index("\n@router") if "\n@router" in body
                    else len(body)]
        assert "rules.is_platform_admin(user)" in body, fn


# ── P1.1: the two proofs ─────────────────────────────────────────────
def test_the_positive_probe_targets_an_endpoint_that_cannot_create():
    """Even if every validation passed, lifecycle makes no business."""
    src = open("./iskra_client.py").read()
    fn = src[src.index("def probe_signed_invalid_body("):]
    fn = fn[:fn.index("\ndef _raw_probe")]
    assert "/api/provisioning/lifecycle" in fn
    assert "/api/provisioning/tenant" not in fn
    assert "body: dict = {}" in fn          # no idempotency_key -> 400


def test_the_negative_probe_sends_a_wrong_mac_not_a_missing_header():
    src = open("./iskra_client.py").read()
    fn = src[src.index("def probe_bad_signature("):]
    fn = fn[:fn.index("\ndef probe_signed_invalid_body")]
    assert "'0' * 64" in fn                  # correct shape, wrong value
    assert "x-iskra-signature" in fn


def test_a_401_and_a_400_prove_different_things():
    """A passing negative test is not evidence that signing works."""
    src = open("./provisioning.py").read()
    assert "the rejection path only" in src
    assert "signing interoperates" in src


def test_the_proof_records_a_side_effect_snapshot_either_side():
    src = open("./provisioning.py").read()
    fn = src[src.index("async def interop_proof("):]
    assert fn.index("before = _side_effect_snapshot(sb)") < \
        fn.index("offload(iskra.probe_bad_signature)")
    assert "after = _side_effect_snapshot(sb)" in fn
    assert '"unchanged": before == after' in fn


def test_the_proof_refuses_to_run_without_a_local_secret():
    src = open("./provisioning.py").read()
    fn = src[src.index("async def interop_proof("):]
    assert fn.index('if not mine:') < fn.index("offload(iskra.probe_bad_signature)")
    assert "sklz_secret_missing" in fn


def test_every_configuration_verdict_exists():
    src = open("./provisioning.py").read()
    for verdict in ("match", "fingerprint_mismatch", "sklz_secret_missing",
                    "iskra_secret_missing", "iskra_unreachable"):
        assert f'"{verdict}"' in src, verdict


# ── P1.2: existing-tenant adoption ───────────────────────────────────
ADOPT_TENANT = "99a1705a-b374-4725-8381-5b150b43bfa9"


def test_adoption_uses_the_existing_signer_not_a_second_one():
    src = open("./iskra_client.py").read()
    assert src.count("def sign(") == 1
    assert "hmac.new" in src and src.count("hmac.new") <= 2
    fn = src[src.index("def adopt_existing("):]
    fn = fn[:fn.index("\ndef owner_invite")]
    assert "_request(" in fn          # the shared signed transport
    assert "hmac" not in fn           # no second implementation


def test_adoption_targets_its_own_endpoint():
    src = open("./iskra_client.py").read()
    fn = src[src.index("def adopt_existing("):]
    fn = fn[:fn.index("\ndef owner_invite")]
    assert "/api/provisioning/adopt-existing" in fn
    assert "/api/provisioning/tenant" not in fn


def test_the_harness_never_calls_create_or_invite():
    """`/tenant` is create-or-return; pointed at an existing business it
    makes a SECOND one."""
    src = open("tools/iskra_adopt_existing.py").read()
    assert "adopt_existing" in src
    assert "create_tenant" not in src
    assert "owner_invite" not in src
    assert "/api/provisioning/tenant" not in src


def test_the_tenant_id_is_a_constant_not_an_argument():
    src = open("tools/iskra_adopt_existing.py").read()
    assert f'TENANT_ID = "{ADOPT_TENANT}"' in src
    # never derived from a name, slug or email
    for bad in ("business_name", "owner_email", 'tenant_id": "sklz"'):
        assert bad not in src


def test_expect_block_matches_the_contract_and_keeps_is_test_true():
    src = open("tools/iskra_adopt_existing.py").read()
    assert '"slug": "sklz"' in src and '"name": "SKLZ Labs"' in src
    assert '"is_test": True' in src
    assert '"lifecycle_state": "active"' in src
    assert '"is_test": False' not in src        # never asserts a flip


def test_intent_is_written_before_the_signed_call():
    src = open("tools/iskra_adopt_existing.py").read()
    body = src[src.index("def main("):]
    assert body.index('_sb("POST", "provisioning_intents"') < \
        body.index("iskra.adopt_existing(payload)")


def test_a_retry_reuses_the_stored_key():
    src = open("tools/iskra_adopt_existing.py").read()
    assert 'payload["idempotency_key"] = intent["idempotency_key"]' in src
    assert 'kind=eq.adopt&state=in.(in_flight,succeeded)' in src


def test_a_changed_body_under_the_same_key_stops():
    src = open("tools/iskra_adopt_existing.py").read()
    assert 'intent["request_hash"] != want_hash' in src
    assert "Reusing its key with a changed" in src


def test_returned_tenant_must_equal_the_target_before_any_write():
    src = open("tools/iskra_adopt_existing.py").read()
    body = src[src.index("def main("):]
    assert body.index("returned.lower() != TENANT_ID.lower()") < \
        body.index('_sb("PATCH", f"providers?id=eq.')


def test_an_existing_different_mapping_fails_closed():
    src = open("tools/iskra_adopt_existing.py").read()
    assert 'existing.lower() != TENANT_ID.lower()' in src
    assert "Not overwriting" in src
    guard = src[src.index("existing.lower() != TENANT_ID.lower()"):]
    guard = guard[:guard.index("# ── 4 ·")]
    assert '_sb("PATCH"' not in guard


def test_dry_run_writes_nothing():
    src = open("tools/iskra_adopt_existing.py").read()
    body = src[src.index("if not COMMIT:"):]
    body = body[:body.index("# ── 5 ·")]
    assert "return 0" in body
    assert '_sb("POST"' not in body and '_sb("PATCH"' not in body


def test_412_and_403_are_preserved_distinctly():
    import iskra_client as k
    assert k.classify(412, {"error": "expectation_failed"}).code == \
        "expectation_failed"
    assert k.classify(412, {}).retryable is False
    assert k.classify(403, {"error": "adoption_disabled"}).code == \
        "adoption_disabled"
    src = open("tools/iskra_adopt_existing.py").read()
    assert "adoption_disabled" in src and "expectation_failed" in src


def test_every_named_409_conflict_survives_classification():
    import iskra_client as k
    for conflict in ("provider_linked_elsewhere", "tenant_linked_elsewhere",
                     "link_ended", "tenant_offboarded", "raced"):
        exc = k.classify(409, {"conflict": conflict})
        assert exc.code == conflict, conflict
        assert exc.retryable is False


def test_the_harness_prints_no_secret():
    src = open("tools/iskra_adopt_existing.py").read()
    assert "secret_fingerprint()" in src
    for bad in ("ISKRA_PROVISIONING_SECRET\")", "SUPABASE_SERVICE_KEY)",
                "print(key", "line(key"):
        assert bad not in src


def test_adoption_kind_is_allowed_by_the_ledger():
    sql = open("migrations/P12-migration.sql").read()
    assert "'tenant', 'owner_invite', 'lifecycle', 'adopt'" in sql
    assert "provisioning_intents_one_adopt" in sql
    assert "where kind = 'adopt' and state in ('in_flight', 'succeeded')" in sql
    # reuses the ledger rather than creating a parallel one
    assert "create table" not in sql.lower()
