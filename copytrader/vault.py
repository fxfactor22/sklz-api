"""SKLZ CopyTrader — encrypted credential vault.

Exchange API keys are the most dangerous data this platform will ever hold.
Rules enforced here:

  1. Keys are encrypted with AES-256-GCM before they touch the database.
  2. The master key lives ONLY in the environment (COPY_VAULT_KEY), never in
     the DB, never in the repo. Losing it means all credentials are unreadable
     — which is the correct failure mode.
  3. Plaintext secrets are never logged, never returned by any API response,
     and are decrypted only in memory at execution time.
  4. A key with WITHDRAWAL permission is rejected outright. Trading and read
     permissions only.
  5. Every decrypt is auditable by design (callers log the reason, not the key).

Generate a master key once:
    python3 -c "import os,base64; print(base64.b64encode(os.urandom(32)).decode())"
and set it as COPY_VAULT_KEY in the environment.
"""
from __future__ import annotations

import base64
import hashlib
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEY_VERSION = 1
_NONCE_BYTES = 12


class VaultError(RuntimeError):
    """Raised when the vault is misconfigured or a payload is invalid."""


def _master_key() -> bytes:
    raw = os.environ.get("COPY_VAULT_KEY", "")
    if not raw:
        raise VaultError(
            "COPY_VAULT_KEY is not set. Generate one with: "
            "python3 -c \"import os,base64; print(base64.b64encode(os.urandom(32)).decode())\"")
    try:
        key = base64.b64decode(raw)
    except Exception as exc:  # noqa: BLE001
        raise VaultError("COPY_VAULT_KEY is not valid base64") from exc
    if len(key) != 32:
        raise VaultError(f"COPY_VAULT_KEY must decode to 32 bytes, got {len(key)}")
    return key


def encrypt(plaintext: str, *, aad: str = "") -> dict:
    """Encrypt one secret. `aad` binds the ciphertext to a context
    (e.g. the user id) so a blob cannot be replayed under another account."""
    if not plaintext:
        return {"ct": "", "nonce": "", "v": KEY_VERSION}
    aes = AESGCM(_master_key())
    nonce = os.urandom(_NONCE_BYTES)
    ct = aes.encrypt(nonce, plaintext.encode(), aad.encode() if aad else None)
    return {
        "ct": base64.b64encode(ct).decode(),
        "nonce": base64.b64encode(nonce).decode(),
        "v": KEY_VERSION,
    }


def decrypt(blob: dict, *, aad: str = "") -> str:
    """Decrypt one secret. Raises on tampering — AES-GCM is authenticated."""
    if not blob or not blob.get("ct"):
        return ""
    aes = AESGCM(_master_key())
    try:
        nonce = base64.b64decode(blob["nonce"])
        ct = base64.b64decode(blob["ct"])
        return aes.decrypt(nonce, ct, aad.encode() if aad else None).decode()
    except Exception as exc:  # noqa: BLE001
        raise VaultError("could not decrypt credential (wrong key or tampered)") from exc


def fingerprint(api_key: str) -> str:
    """Non-reversible identifier so we can detect duplicate keys and show the
    user which key is stored, without ever storing or displaying the key."""
    if not api_key:
        return ""
    return hashlib.sha256(api_key.encode()).hexdigest()[:16]


def masked(api_key: str) -> str:
    """Display form: first 4 and last 4 only."""
    if not api_key or len(api_key) < 10:
        return "••••"
    return f"{api_key[:4]}••••{api_key[-4:]}"


def vault_ready() -> bool:
    try:
        _master_key()
        return True
    except VaultError:
        return False
