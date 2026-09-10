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
        fn.index("iskra.create_tenant(payload)")


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
