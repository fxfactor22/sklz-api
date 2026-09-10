"""The one client for ISKRA provisioning.

Contract: SKLZ ↔ ISKRA Provider Provisioning v1.0.

Everything here is deliberately free of FastAPI and Supabase so the parts
that decide what gets signed, what gets stored and what an HTTP code
means can be tested without standing up the API.

THREE RULES THE CONTRACT PAID FOR IN HOURS
==========================================
1. Sign the RAW bytes that are transmitted — never a re-serialised object.
2. Retry with the SAME idempotency key. A new key after a timeout is how
   a customer ends up with two companies.
3. Redact stored results RECURSIVELY. The invitation lives at
   `invite.token`, one level down; ISKRA's first implementation stripped
   the top level only and stored a working key to every business it had
   just created. The contract says plainly that the same mistake is
   available on this side.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.request

BASE_URL_DEFAULT = "https://www.iskra.marketing"
TIMEOUT = 20.0
MAX_BODY_BYTES = 64 * 1024

# Stripped from anything we persist or log, at every depth.
SECRET_KEYS = {"token", "url", "token_hash", "action_link", "secret"}


def base_url() -> str:
    return os.environ.get("ISKRA_BASE_URL", BASE_URL_DEFAULT).rstrip("/")


def _secret() -> str:
    return os.environ.get("ISKRA_PROVISIONING_SECRET", "")


def secret_fingerprint() -> str:
    """sha256(secret)[0:12] — the same shape ISKRA's _diag returns."""
    s = _secret()
    return hashlib.sha256(s.encode()).hexdigest()[:12] if s else ""


def is_configured() -> bool:
    return bool(_secret())


def sign(raw_body: bytes, ts: str | None = None) -> tuple[str, str]:
    """Return (header_value, timestamp) for x-iskra-signature."""
    t = ts or str(int(time.time()))
    mac = hmac.new(_secret().encode("utf-8"),
                   (t + ".").encode("utf-8") + raw_body,
                   hashlib.sha256).hexdigest()
    return f"t={t},v1={mac}", t


def canonical_request_hash(body: dict) -> str:
    """sha256 of the body with idempotency_key removed and keys sorted.

    Mirrors ISKRA's `request_hash` so this side can tell a retry from a
    changed mind BEFORE spending a call — and refuse locally rather than
    collect a 409 for a body we could see had drifted.
    """
    trimmed = {k: v for k, v in (body or {}).items()
               if k != "idempotency_key"}
    blob = json.dumps(trimmed, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def redact(value):
    """Strip credentials recursively, whatever their depth.

    `invite.token` is one level down. A top-level-only strip looks
    correct in a review and stores a working key to somebody's company.
    """
    if isinstance(value, dict):
        return {k: ("[redacted]" if k.lower() in SECRET_KEYS
                    else redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


class IskraError(Exception):
    """A provisioning call that did not succeed, with its meaning intact."""

    def __init__(self, status: int, code: str, detail: str = "",
                 retryable: bool = False, same_key: bool = True,
                 body: dict | None = None) -> None:
        super().__init__(f"{status} {code}: {detail}"[:300])
        self.status = status
        self.code = code
        self.detail = detail
        self.retryable = retryable        # may this be retried at all
        self.same_key = same_key          # retry with the SAME key?
        self.body = body or {}


def classify(status: int, body: dict) -> IskraError:
    """Map an HTTP status onto the contract's meaning.

    Deliberately not flattened into "iskra_unavailable": the difference
    between "retry with the same key" and "fix the cause and use a new
    one" is the difference between one company and two.
    """
    code = str(body.get("error") or "").strip() or f"http_{status}"
    detail = str(body.get("detail") or body.get("message") or "")[:300]

    if status == 400:
        return IskraError(status, code or "bad_request", detail,
                          retryable=False)
    if status == 401:
        return IskraError(
            status, "bad_signature",
            f"{detail} (iskra fingerprint {body.get('iskra_key_fingerprint','?')}, "
            f"sklz fingerprint {secret_fingerprint() or 'unset'})",
            retryable=False)
    if status == 404:
        return IskraError(status, "not_provisioned",
                          detail or "no business is linked to that provider",
                          retryable=False)
    if status == 409:
        retry = bool(body.get("retryable"))
        # Adoption names WHICH side is already spoken for. Preserving that
        # matters: "409" alone sends whoever reads it to the wrong database.
        named = str(body.get("conflict") or "").strip()
        return IskraError(status, named or "conflict", detail,
                          retryable=retry, same_key=True, body=body)
    if status == 412:
        # An expectation about the tenant did not hold. Never retry — the
        # request is aimed at the wrong company, or the world changed.
        return IskraError(status, "expectation_failed", detail,
                          retryable=False, body=body)
    if status == 403:
        return IskraError(status, str(body.get("error") or "forbidden"),
                          detail or "adoption is disabled on ISKRA",
                          retryable=False, body=body)
    if status == 413:
        return IskraError(status, "body_too_large", detail, retryable=False)
    if status == 500:
        # ISKRA recorded the attempt as failed. Retrying with the SAME key
        # is what the contract asks for.
        return IskraError(status, "iskra_failed", detail, retryable=True,
                          same_key=True)
    if status == 502:
        # A replay of a request that originally failed. A new key is
        # required, AFTER fixing whatever failed the first time.
        return IskraError(status, "replay_of_failed", detail,
                          retryable=True, same_key=False)
    if status == 503:
        return IskraError(status, "provisioning_not_configured", detail,
                          retryable=True, same_key=True)
    return IskraError(status, code, detail, retryable=False)


def _request(method: str, path: str, body: dict | None,
             signed: bool = True) -> tuple[int, dict]:
    url = base_url() + path
    raw = b""
    headers = {"accept": "application/json"}
    if body is not None:
        raw = json.dumps(body, separators=(",", ":"),
                         ensure_ascii=False).encode("utf-8")
        if len(raw) > MAX_BODY_BYTES:
            raise IskraError(413, "body_too_large",
                             "request body exceeds 64 KB", retryable=False)
        headers["content-type"] = "application/json"
    if signed:
        if not is_configured():
            # Fail closed. An unsigned call to a company-creating endpoint
            # is not a degraded mode worth having.
            raise IskraError(503, "sklz_secret_not_configured",
                             "ISKRA_PROVISIONING_SECRET is not set on SKLZ",
                             retryable=True)
        header, _ = sign(raw)
        headers["x-iskra-signature"] = header

    req = urllib.request.Request(url, data=raw or None, method=method,
                                 headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            text = resp.read().decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", "replace")
        status = exc.code
    except Exception as exc:  # noqa: BLE001
        # Transport failure. The call MAY have arrived — that is exactly
        # the crash-recovery case — so this is retryable with the same key.
        raise IskraError(0, "transport_failed",
                         f"{type(exc).__name__}: {exc}"[:200],
                         retryable=True, same_key=True) from exc
    try:
        data = json.loads(text) if text else {}
    except ValueError:
        data = {"raw": text[:200]}
    if status >= 400:
        raise classify(status, data if isinstance(data, dict) else {})
    return status, data if isinstance(data, dict) else {}


def probe_bad_signature() -> dict:
    """NEGATIVE proof: a deliberately wrong signature must be refused.

    Proves the rejection path and nothing else — which is worth saying,
    because a passing negative test is often mistaken for evidence that
    signing works. It is not. See probe_signed_invalid_body.
    """
    body = {"idempotency_key": "interop-negative", "provider_id": "probe"}
    raw = json.dumps(body, separators=(",", ":")).encode("utf-8")
    t = str(int(time.time()))
    req = urllib.request.Request(
        base_url() + "/api/provisioning/lifecycle", data=raw, method="POST",
        headers={"content-type": "application/json",
                 # correct shape, wrong mac
                 "x-iskra-signature": f"t={t},v1={'0' * 64}"})
    return _raw_probe(req)


def probe_signed_invalid_body() -> dict:
    """POSITIVE proof, with no side effect available to it.

    A correctly signed request carrying a body ISKRA must reject. If the
    answer is 400 rather than 401, the signature verified — which is the
    only way to prove the signing path works without creating a company.

    The target is `lifecycle` deliberately: even if every validation
    passed, lifecycle cannot bring a business into existence. And the
    body omits `idempotency_key`, which the contract makes a 400.
    """
    body: dict = {}
    raw = json.dumps(body, separators=(",", ":")).encode("utf-8")
    header, _ = sign(raw)
    req = urllib.request.Request(
        base_url() + "/api/provisioning/lifecycle", data=raw, method="POST",
        headers={"content-type": "application/json",
                 "x-iskra-signature": header})
    return _raw_probe(req)


def _raw_probe(req) -> dict:
    """Send and report, without raising — a probe wants the status."""
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return {"status": resp.status,
                    "body": _small(resp.read())}
    except urllib.error.HTTPError as exc:
        return {"status": exc.code, "body": _small(exc.read())}
    except Exception as exc:  # noqa: BLE001
        return {"status": 0, "error": f"{type(exc).__name__}: {exc}"[:200]}


def _small(raw: bytes) -> dict:
    try:
        data = json.loads(raw.decode("utf-8", "replace") or "{}")
    except ValueError:
        return {"raw": raw.decode("utf-8", "replace")[:200]}
    return redact(data) if isinstance(data, dict) else {"raw": str(data)[:200]}


# ── the five contract endpoints ─────────────────────────────────────
def diag() -> dict:
    """Unsigned fingerprint check. The first thing to run, always."""
    _, data = _request("GET", "/api/provisioning/_diag", None, signed=False)
    return data


def create_tenant(payload: dict) -> dict:
    _, data = _request("POST", "/api/provisioning/tenant", payload)
    return data


ADOPTION_TENANT_ID = "99a1705a-b374-4725-8381-5b150b43bfa9"


def adopt_existing(payload: dict) -> dict:
    """Link a provider to a business that ALREADY exists.

    Separate from create_tenant on purpose. `/provisioning/tenant` is
    "create or return", and pointed at an existing business with no
    provider link it does not find it — it creates a second one with the
    same name and none of the history. Adoption establishes a link and
    creates nothing.
    """
    _, data = _request("POST", "/api/provisioning/adopt-existing", payload)
    return data


def owner_invite(payload: dict) -> dict:
    _, data = _request("POST", "/api/provisioning/owner-invite", payload)
    return data


def lifecycle(payload: dict) -> dict:
    _, data = _request("POST", "/api/provisioning/lifecycle", payload)
    return data


def status(provider_id: str) -> dict:
    _, data = _request("POST", "/api/provisioning/status",
                       {"provider_id": provider_id})
    return data
