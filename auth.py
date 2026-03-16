"""
Session cookie auth and rate limiter configuration.

Session: signed (not encrypted) HttpOnly cookie via itsdangerous.TimestampSigner.
Rate limiter: slowapi in-process counter keyed on API key or remote IP.
"""
from __future__ import annotations

import os
from typing import Optional

from fastapi import Request, Response
from itsdangerous import BadSignature, SignatureExpired, TimestampSigner
from slowapi import Limiter
from slowapi.util import get_remote_address

COOKIE_NAME = "llm_router_session"
SESSION_MAX_AGE = 86_400  # 24 hours


# ── Session ───────────────────────────────────────────────────────────────────

def _signer() -> TimestampSigner:
    return TimestampSigner(os.getenv("SECRET_KEY", "changeme"))


def create_session(response: Response, *, is_https: bool = True) -> None:
    """Sign and set session cookie on the given response."""
    token = _signer().sign("ok").decode()
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="strict",
        secure=is_https,
        max_age=SESSION_MAX_AGE,
        path="/",
    )


def verify_session(request: Request) -> bool:
    """Return True if the request carries a valid, unexpired session cookie."""
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return False
    try:
        _signer().unsign(token, max_age=SESSION_MAX_AGE)
        return True
    except (SignatureExpired, BadSignature):
        return False


def clear_session(response: Response) -> None:
    """Remove the session cookie from the response."""
    response.delete_cookie(key=COOKIE_NAME, path="/")


# ── API key extraction ────────────────────────────────────────────────────────

def get_bearer_key(request: Request) -> Optional[str]:
    """Extract virtual API key from x-api-key or Authorization: Bearer headers."""
    key = request.headers.get("x-api-key")
    if key:
        return key.strip()
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None


# ── Rate limiter ──────────────────────────────────────────────────────────────

def _rate_key(request: Request) -> str:
    """Key for rate limiter: API key if present, else remote IP."""
    key = get_bearer_key(request)
    return key or get_remote_address(request)


limiter = Limiter(key_func=_rate_key)
