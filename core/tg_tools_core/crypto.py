"""Symmetric encryption for stored session strings.

The Fernet key is derived from a caller-supplied secret. If that secret changes,
previously encrypted data can no longer be decrypted (the user logs in again).
"""
from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken


def _fernet(secret: str) -> Fernet:
    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    key = base64.urlsafe_b64encode(digest)
    return Fernet(key)


def encrypt(plaintext: str, secret: str) -> str:
    return _fernet(secret).encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(token: str, secret: str) -> str | None:
    """Decrypt a token; returns None if the secret is wrong / token corrupt."""
    try:
        return _fernet(secret).decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        return None
