"""
Fernet encryption helpers for upstream API keys.

Key derivation: SHA-256(SECRET_KEY) → 32-byte Fernet key.
This means the same SECRET_KEY always produces the same encryption key,
so you can decrypt after restarts as long as SECRET_KEY doesn't change.
"""
from __future__ import annotations

import base64
import hashlib
import os

from cryptography.fernet import Fernet, InvalidToken


def _fernet() -> Fernet:
    secret = os.getenv("SECRET_KEY", "changeme-please-set-a-real-secret-in-dotenv")
    raw = hashlib.sha256(secret.encode()).digest()          # 32 bytes
    key = base64.urlsafe_b64encode(raw)                     # Fernet needs base64
    return Fernet(key)


def encrypt(plaintext: str) -> str:
    """Encrypt a plaintext string. Returns empty string for empty input."""
    if not plaintext:
        return ""
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    """Decrypt a previously encrypted string. Returns empty string on failure."""
    if not ciphertext:
        return ""
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except (InvalidToken, Exception):
        return ""
