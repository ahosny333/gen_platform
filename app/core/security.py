"""
app/core/security.py
─────────────────────
Security Utilities — JWT & Password Hashing
═══════════════════════════════════════════════════════════════════════════════

This module handles two separate security concerns:

  1. PASSWORD HASHING
     When a user is created, we never store the real password.
     We store a bcrypt hash. On login we compare the hash.

  2. JWT TOKENS
     After successful login, we create a signed token and return it.
     The frontend stores it and sends it in every request header.
     We verify + decode it on every protected API call.
═══════════════════════════════════════════════════════════════════════════════
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
from passlib.context import CryptContext

from app.core.config import get_settings
from app.core.logging import logger

settings = get_settings()

# ── Password Hashing Setup ─────────────────────────────────────────────────────
# CryptContext tells passlib WHICH algorithm to use.
# bcrypt is the industry standard — it's slow by design to resist brute force.
# pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
# To this (sha256_crypt works on all platforms without C compiler):
pwd_context = CryptContext(schemes=["sha256_crypt"], deprecated="auto")


def hash_password(plain_password: str) -> str:
    """
    Convert a plain text password into a bcrypt hash.

    Example:
        hash_password("mypassword123")
        → "$2b$12$KIXjJ8p9z3Qw7Y5v..."  (60-char hash, always different)

    This is called ONCE when creating a user. The hash is what gets
    stored in the database — never the original password.
    """
    return pwd_context.hash(plain_password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """
    Check if a plain password matches a stored hash.

    Example:
        verify_password("mypassword123", "$2b$12$KIXjJ8p9z3Qw7Y5v...")
        → True

        verify_password("wrongpassword", "$2b$12$KIXjJ8p9z3Qw7Y5v...")
        → False

    This is called on every login attempt.
    passlib handles the comparison safely (timing-attack resistant).
    """
    return pwd_context.verify(plain_password, hashed_password)


# ── JWT Token Handling ─────────────────────────────────────────────────────────

def create_access_token(data: dict) -> str:
    """
    Create a signed JWT token containing user data.

    What gets encoded inside the token:
        {
            "sub":  "user_01",        ← user ID (subject)
            "role": "admin",          ← user role
            "exp":  1713000000        ← expiry timestamp (Unix)
        }

    The token is signed with SECRET_KEY from .env using HS256 algorithm.
    Anyone with the token can READ it — but cannot MODIFY it without
    knowing the secret key (modification breaks the signature).

    Args:
        data: dict with at minimum {"sub": user_id, "role": role}

    Returns:
        A signed JWT string like:
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyXzAxIn0.abc123..."
    """
    payload = data.copy()

    # Set expiry time
    expire = datetime.now(timezone.utc) + timedelta(
        minutes=settings.access_token_expire_minutes
    )
    payload["exp"] = expire

    # Sign and encode the token
    token = jwt.encode(
        payload=payload,
        key=settings.secret_key,
        algorithm="HS256",
    )

    logger.debug(f"[Security] Token created for user: {data.get('sub')}")
    return token


def decode_access_token(token: str) -> Optional[dict]:
    """
    Decode and verify a JWT token from an incoming request.

    Checks performed automatically by PyJWT:
      ✅ Signature is valid (token wasn't tampered with)
      ✅ Token has not expired
      ✅ Token structure is correct

    Args:
        token: The raw JWT string from the Authorization header

    Returns:
        The decoded payload dict if valid:
            {"sub": "user_01", "role": "admin", "exp": 1713000000}
        None if token is invalid or expired.
    """
    try:
        payload = jwt.decode(
            jwt=token,
            key=settings.secret_key,
            algorithms=["HS256"],
        )
        return payload

    except jwt.ExpiredSignatureError:
        logger.warning("[Security] Token has expired")
        return None

    except jwt.InvalidTokenError as exc:
        logger.warning(f"[Security] Invalid token: {exc}")
        return None
