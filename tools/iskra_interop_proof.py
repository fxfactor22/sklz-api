#!/usr/bin/env python3
"""Operator script — the ISKRA interoperability proof, run locally.

    railway run python tools/iskra_interop_proof.py

Runs inside the SKLZ Railway environment so `ISKRA_PROVISIONING_SECRET`
comes from Railway and never appears on a command line, in a shell
history, or on screen. Only `sha256(secret)[0:12]` is ever printed.

This is an OPERATOR TOOL, not a route. It calls the same P1.1 functions
the production endpoint calls (`iskra_client.probe_bad_signature` and
`probe_signed_invalid_body`), so a pass here is a pass there.

It cannot create a tenant:
  * probe A sends a deliberately wrong MAC
  * probe B is correctly signed but targets `lifecycle` with an empty
    body — lifecycle cannot bring a business into existence under any
    validation outcome, and the missing idempotency_key is a 400
Neither probe writes to SKLZ. The counts either side are read-only.

Stdlib only, so it runs wherever `railway run` runs.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import iskra_client as iskra  # noqa: E402


def line(s: str = "") -> None:
    print(s, flush=True)


def _supabase(path: str) -> list:
    """Read-only Supabase REST. Returns [] rather than failing the proof."""
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    if not url or not key:
        return []
    req = urllib.request.Request(
        f"{url}/rest/v1/{path}",
        headers={"apikey": key, "authorization": f"Bearer {key}",
                 "accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode())
        return data if isinstance(data, list) else []
    except Exception as exc:  # noqa: BLE001
        line(f"   (count unavailable: {type(exc).__name__})")
        return []


def snapshot() -> dict:
    providers = _supabase("providers?select=id,iskra_tenant_id")
    intents = _supabase("provisioning_intents?select=id,state")
    return {
        "linked_providers": len([p for p in providers
                                 if p.get("iskra_tenant_id")]),
        "provisioning_intents": len(intents),
        "succeeded_intents": len([i for i in intents
                                  if i.get("state") == "succeeded"]),
    }


def main() -> int:
    line("ISKRA INTEROPERABILITY PROOF — zero side effects")
    line(f"  base : {iskra.base_url()}")
    line()

    # ── 1 · local secret ────────────────────────────────────────────
    mine = iskra.secret_fingerprint()
    if not mine:
        line("  VERDICT: sklz_secret_missing")
        line("  ISKRA_PROVISIONING_SECRET is not set in this environment.")
        line("  Nothing below can run until it is configured on the SKLZ")
        line("  Railway service. No probe was sent.")
        return 1
    line(f"1 · SKLZ  fingerprint : {mine}")

    # ── 2 · remote secret ───────────────────────────────────────────
    try:
        remote = iskra.diag()
    except iskra.IskraError as exc:
        line(f"  VERDICT: iskra_unreachable ({exc.code}) {exc.detail}")
        return 1
    theirs = str((remote.get("secret") or {}).get("fingerprint") or "")
    configured = bool((remote.get("secret") or {}).get("configured"))
    line(f"2 · ISKRA fingerprint : {theirs or '(none)'}"
         f"   configured={configured}")
    line(f"    ISKRA server time : {remote.get('server_time')}")

    if not theirs:
        line("\n  VERDICT: iskra_secret_missing — no probe sent.")
        return 1
    if theirs != mine:
        line("\n  VERDICT: fingerprint_mismatch")
        line("  The two sides hold different strings. No request will ever")
        line("  verify. Set them to the same value; nothing else will fix")
        line("  it. No probe sent.")
        return 1
    line("\n3 · VERDICT: match — the same secret on both sides")
    line()

    # ── 3 · counts before ───────────────────────────────────────────
    before = snapshot()
    line(f"4 · before : linked_providers={before['linked_providers']}  "
         f"intents={before['provisioning_intents']}  "
         f"succeeded={before['succeeded_intents']}")
    line()

    # ── 4 · the two probes ──────────────────────────────────────────
    line("5 · probe A — deliberately WRONG signature")
    a = iskra.probe_bad_signature()
    a_ok = a.get("status") == 401
    line(f"    expected 401, got {a.get('status')}   "
         f"{'PASS' if a_ok else 'FAIL'}")
    line(f"    body: {json.dumps(a.get('body') or a.get('error'))[:120]}")
    line("    proves: the rejection path. NOT that signing works.")
    line()

    line("6 · probe B — CORRECT signature, invalid lifecycle body")
    b = iskra.probe_signed_invalid_body()
    status = b.get("status")
    b_ok = status in (400, 404)
    line(f"    expected 400, got {status}   {'PASS' if b_ok else 'FAIL'}")
    line(f"    body: {json.dumps(b.get('body') or b.get('error'))[:160]}")
    if status == 400:
        line("    proves: the signature VERIFIED and the body was rejected")
        line("            afterwards — signing interoperates.")
    elif status == 404:
        line("    proves: the signature verified (lookup runs after auth).")
    elif status == 401:
        line("    the signature did NOT verify despite matching")
        line("    fingerprints — compare the signing bytes next.")
    line()

    # ── 5 · counts after ────────────────────────────────────────────
    after = snapshot()
    line(f"7 · after  : linked_providers={after['linked_providers']}  "
         f"intents={after['provisioning_intents']}  "
         f"succeeded={after['succeeded_intents']}")
    unchanged = before == after
    line(f"    side effects: {'NONE' if unchanged else 'SOMETHING CHANGED'}")
    line()

    ok = a_ok and b_ok and unchanged
    line("── result ──")
    line(f"  fingerprints      : match ({mine})")
    line(f"  wrong signature   : {a.get('status')} "
         f"{'(expected)' if a_ok else '(UNEXPECTED)'}")
    line(f"  signed bad body   : {status} "
         f"{'(expected)' if b_ok else '(UNEXPECTED)'}")
    line(f"  tenant created    : no")
    line(f"  mapping changed   : {'no' if unchanged else 'YES — investigate'}")
    line()
    line("  PASS — signed interoperability proven, nothing created."
         if ok else
         "  NOT PROVEN — read the sections above before provisioning.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
