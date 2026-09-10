#!/usr/bin/env python3
"""Operator harness — adopt the existing ISKRA tenant.

    railway run python3 tools/iskra_adopt_existing.py            # dry run
    railway run python3 tools/iskra_adopt_existing.py --commit   # for real

Runs inside the SKLZ Railway environment, so `ISKRA_PROVISIONING_SECRET`
comes from Railway and is never typed, printed or logged. Only
`sha256(secret)[0:12]` ever appears.

WHAT THIS DOES, AND THE ORDER IT DOES IT IN
===========================================
    durable adoption intent  →  signed adopt-existing  →  verify the
    tenant id is THE expected one  →  write providers.iskra_tenant_id
    →  settle the intent  →  ordinary /status to confirm

The intent is written BEFORE the network call. If the response is lost,
the key survives and the retry reuses it: ISKRA answers with the same
relationship rather than making a second one.

WHAT IT REFUSES TO DO
=====================
  * call /provisioning/tenant — that is "create or return", and pointed
    at an existing business it makes a SECOND one
  * call any invitation endpoint
  * accept any tenant id other than the audited one
  * overwrite an existing different mapping
  * write anything at all without --commit

Stdlib only.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import iskra_client as iskra  # noqa: E402

TENANT_ID = "99a1705a-b374-4725-8381-5b150b43bfa9"
EXPECT = {"slug": "sklz", "name": "SKLZ Labs",
          "is_test": True, "lifecycle_state": "active"}
IDEMPOTENCY_KEY = "sklz-adopt-2026-09-10-001"

COMMIT = "--commit" in sys.argv


def line(s: str = "") -> None:
    print(s, flush=True)


# ── Supabase REST, read and write, service role from Railway ────────
def _sb(method: str, path: str, body=None, prefer: str = "") -> list:
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    if not url or not key:
        raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_KEY not in env")
    headers = {"apikey": key, "authorization": f"Bearer {key}",
               "accept": "application/json"}
    data = None
    if body is not None:
        headers["content-type"] = "application/json"
        headers["prefer"] = prefer or "return=representation"
        data = json.dumps(body).encode()
    req = urllib.request.Request(f"{url}/rest/v1/{path}", data=data,
                                 method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = r.read().decode()
    return json.loads(raw) if raw.strip() else []


def fail(msg: str) -> int:
    line()
    line(f"  STOP — {msg}")
    return 1


def main() -> int:
    line("ISKRA EXISTING-TENANT ADOPTION" +
         ("  [COMMIT]" if COMMIT else "  [DRY RUN — nothing will be written]"))
    line(f"  base   : {iskra.base_url()}")
    line(f"  target : {TENANT_ID}")
    line()

    # ── 1 · fingerprints, before anything else ──────────────────────
    mine = iskra.secret_fingerprint()
    if not mine:
        return fail("ISKRA_PROVISIONING_SECRET is not set in this "
                    "environment")
    try:
        remote = iskra.diag()
    except iskra.IskraError as exc:
        return fail(f"ISKRA unreachable: {exc.code} {exc.detail}")
    theirs = str((remote.get("secret") or {}).get("fingerprint") or "")
    line(f"1 · fingerprints  SKLZ {mine}   ISKRA {theirs or '(none)'}")
    if mine != theirs:
        return fail("fingerprint mismatch — the two sides hold different "
                    "secrets; no request will verify")
    line("    match")
    line()

    # ── 2 · the canonical provider ──────────────────────────────────
    rows = _sb("GET", "providers?select=id,owner_user_id,display_name,"
                      "slug,status,iskra_tenant_id&order=created_at")
    if not rows:
        return fail("no provider rows exist. Create the canonical SKLZ "
                    "Labs provider first (P0), then re-run.")
    candidates = [r for r in rows if r.get("slug") in ("sklz", "sklz-labs")]
    if len(candidates) != 1:
        line(f"2 · providers found: {len(rows)}")
        for r in rows:
            line(f"      {r['id']}  slug={r['slug']:<16} "
                 f"status={r['status']:<10} linked={r.get('iskra_tenant_id')}")
        return fail(f"{len(candidates)} providers match the SKLZ slug. "
                    "Adoption must be unambiguous — identify the canonical "
                    "row deliberately rather than by name.")
    prov = candidates[0]
    line(f"2 · provider  {prov['id']}")
    line(f"    display_name : {prov['display_name']}")
    line(f"    slug         : {prov['slug']}")
    line(f"    status       : {prov['status']}")
    line(f"    owner        : {prov['owner_user_id']}")
    line(f"    linked now   : {prov.get('iskra_tenant_id') or '(none)'}")
    line()

    # ── 3 · local mapping must not already point elsewhere ──────────
    existing = str(prov.get("iskra_tenant_id") or "")
    if existing and existing.lower() != TENANT_ID.lower():
        return fail(f"this provider is already linked to {existing}, which "
                    f"is not the adoption target. Not overwriting. "
                    f"Reconciliation required.")
    if existing:
        line("3 · already linked to the target — adoption will replay")
    else:
        line("3 · no local mapping yet — this is a first adoption")
    line()

    # ── 4 · expectations ────────────────────────────────────────────
    line("4 · assertions sent (compared by ISKRA, never written)")
    for k, v in EXPECT.items():
        line(f"      {k:<16} = {json.dumps(v)}")
    line("    is_test stays true; an assertion is not an instruction")
    line()

    payload = {"idempotency_key": IDEMPOTENCY_KEY,
               "provider_id": str(prov["id"]),
               "provider_system": "sklz",
               "tenant_id": TENANT_ID,
               "provider_label": "SKLZ Labs",
               "expect": EXPECT}

    if not COMMIT:
        line("5 · DRY RUN — stopping before the signed call.")
        line("    request that WOULD be sent:")
        line("    " + json.dumps(payload, indent=2).replace("\n", "\n    "))
        line()
        line("    re-run with --commit once ISKRA_ADOPTION_ENABLED=1")
        return 0

    # ── 5 · durable intent BEFORE the call ──────────────────────────
    open_rows = _sb("GET", f"provisioning_intents?select=id,idempotency_key,"
                           f"state,request_hash&provider_id=eq.{prov['id']}"
                           f"&kind=eq.adopt&state=in.(in_flight,succeeded)")
    want_hash = iskra.canonical_request_hash(payload)
    if open_rows:
        intent = open_rows[0]
        if intent["request_hash"] != want_hash:
            return fail("an adoption intent exists for this provider with "
                        "different details. Reusing its key with a changed "
                        "body is a conflict; resolve it deliberately.")
        payload["idempotency_key"] = intent["idempotency_key"]
        intent_id = intent["id"]
        line(f"5 · reusing the existing intent  key={intent['idempotency_key']}")
    else:
        created = _sb("POST", "provisioning_intents", {
            "provider_id": str(prov["id"]), "kind": "adopt",
            "idempotency_key": IDEMPOTENCY_KEY, "request_hash": want_hash})
        intent_id = created[0]["id"] if created else None
        line(f"5 · intent written  key={IDEMPOTENCY_KEY}  id={intent_id}")
    line()

    # ── 6 · the signed adoption ─────────────────────────────────────
    line("6 · POST /api/provisioning/adopt-existing")
    try:
        result = iskra.adopt_existing(payload)
    except iskra.IskraError as exc:
        if intent_id:
            _sb("PATCH", f"provisioning_intents?id=eq.{intent_id}",
                {"state": "failed", "error_code": exc.code,
                 "error_detail": exc.detail[:400]})
        if exc.code == "adoption_disabled":
            return fail("ISKRA_ADOPTION_ENABLED is not set on ISKRA. "
                        "Enable it, run this once, then unset it.")
        if exc.code == "expectation_failed":
            return fail(f"412 expectation_failed — {exc.detail}. The "
                        f"request is aimed at a tenant that is not what we "
                        f"believe it to be. Nothing was changed.")
        return fail(f"{exc.status} {exc.code} — {exc.detail}")

    line(f"    adopted={result.get('adopted')}  "
         f"already_linked={result.get('already_linked')}  "
         f"replayed={result.get('replayed', False)}")
    tenant = result.get("tenant") or {}
    returned = str(tenant.get("id") or "")
    line(f"    tenant returned : {returned}")
    line(f"    slug/name       : {tenant.get('slug')} / {tenant.get('name')}")
    line(f"    is_test         : {tenant.get('is_test', '(not returned)')}")
    line(f"    lifecycle       : {tenant.get('lifecycle_state')}")
    line()

    # ── 7 · the returned id must be THE id ──────────────────────────
    if returned.lower() != TENANT_ID.lower():
        if intent_id:
            _sb("PATCH", f"provisioning_intents?id=eq.{intent_id}",
                {"state": "failed", "error_code": "tenant_mismatch",
                 "error_detail": f"expected {TENANT_ID} got {returned}"})
        return fail(f"ISKRA returned {returned}, not the adoption target. "
                    f"Not writing a mapping we did not ask for.")
    line("7 · returned tenant equals the adoption target")

    # ── 8 · local mapping ───────────────────────────────────────────
    if not existing:
        _sb("PATCH", f"providers?id=eq.{prov['id']}",
            {"iskra_tenant_id": returned})
        line("8 · providers.iskra_tenant_id written")
    else:
        line("8 · local mapping already correct — nothing to write")
    if intent_id:
        _sb("PATCH", f"provisioning_intents?id=eq.{intent_id}",
            {"state": "succeeded", "result": iskra.redact(result),
             "settled_at": "now()"})
    line()

    # ── 9 · confirm through the ordinary path ───────────────────────
    line("9 · POST /api/provisioning/status (ordinary machinery)")
    try:
        st = iskra.status(str(prov["id"]))
        stt = st.get("tenant") or {}
        line(f"    resolves to : {stt.get('id')}")
        line(f"    link status : {(st.get('link') or {}).get('status')}")
        ok_status = str(stt.get("id") or "").lower() == TENANT_ID.lower()
    except iskra.IskraError as exc:
        line(f"    status call failed: {exc.code} {exc.detail}")
        ok_status = False
    line()

    line("── result ──")
    line(f"  provider          : {prov['id']}")
    line(f"  tenant            : {returned}")
    line(f"  local mapping     : written")
    line(f"  status resolves   : {'yes' if ok_status else 'NO'}")
    line(f"  tenant created    : no — adoption links, it does not create")
    line(f"  is_test changed   : no")
    line()
    line("  PASS — adoption complete." if ok_status
         else "  INCOMPLETE — the link exists but status did not confirm.")
    line()
    line("  NOW UNSET ISKRA_ADOPTION_ENABLED on ISKRA. It is a door for "
         "one job.")
    return 0 if ok_status else 1


if __name__ == "__main__":
    raise SystemExit(main())
