"""Envelope encryption for provider credentials (OAuth tokens, SMTP passwords) with AES-256-GCM.

The data key comes from TOKEN_ENCRYPTION_KEY (base64, 32 bytes). Without it, no secret-bearing integration can be
configured. Ciphertext layout: base64( key_id(1 byte) | nonce(12) | ciphertext+tag ).
"""

from __future__ import annotations

import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import get_settings


class EncryptionUnavailable(Exception):
    pass


def _key() -> bytes:
    raw = get_settings().token_encryption_key
    if not raw:
        raise EncryptionUnavailable("TOKEN_ENCRYPTION_KEY is not configured; secret-bearing integrations are disabled")
    try:
        key = base64.b64decode(raw)
    except ValueError as exc:  # pragma: no cover - configuration error
        raise EncryptionUnavailable("TOKEN_ENCRYPTION_KEY must be base64") from exc
    if len(key) != 32:
        raise EncryptionUnavailable("TOKEN_ENCRYPTION_KEY must decode to 32 bytes")
    return key


def encrypt(plaintext: str, *, aad: str) -> str:
    nonce = os.urandom(12)
    ct = AESGCM(_key()).encrypt(nonce, plaintext.encode("utf-8"), aad.encode("utf-8"))
    return base64.b64encode(b"\x01" + nonce + ct).decode("ascii")


def decrypt(token: str, *, aad: str) -> str:
    blob = base64.b64decode(token)
    if blob[:1] != b"\x01":
        raise ValueError("unknown key id")
    nonce, ct = blob[1:13], blob[13:]
    return AESGCM(_key()).decrypt(nonce, ct, aad.encode("utf-8")).decode("utf-8")
