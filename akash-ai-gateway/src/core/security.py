"""
core.security
=============

Handles all cryptographic concerns for the gateway:
    1. RSA keypair generation / persistence (asymmetric JWT signing).
    2. Access token issuance & verification (RS256).
    3. Password hashing & verification (bcrypt via passlib).
    4. FastAPI dependencies for extracting & validating the current user.

ARCHITECTURAL DECISION -- Asymmetric JWT (RS256) vs Symmetric (HS256):
    We deliberately sign tokens with an RSA private key and verify them with
    the matching public key. This means:
        - The private key never has to leave the Auth/Gateway boundary.
        - Any number of downstream microservices, edge proxies, or partner
          services can be handed the PUBLIC key to independently verify a
          token's authenticity without ever being able to forge one.
        - This is the industry-standard pattern used by Auth0, Okta, and
          most FAANG-scale internal auth systems (OIDC uses RS256 by
          default for exactly this reason).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict

import jwt
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from passlib.context import CryptContext
from pydantic import BaseModel

from src.core.config import get_settings
from src.core.logger import log_security_event

settings = get_settings()

# ---------------------------------------------------------------------------
# Password Hashing
# ---------------------------------------------------------------------------
# ARCHITECTURAL DECISION: bcrypt is used (via passlib's CryptContext) because
# it is adaptive (a configurable work-factor / cost) which lets us keep pace
# with Moore's law by raising the cost factor over time, and it has a
# built-in per-hash salt, eliminating an entire class of rainbow-table
# attacks without any extra engineering effort on our part.
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(plain_password: str) -> str:
    """Hash a plaintext password using bcrypt with an auto-generated salt."""
    return pwd_context.hash(plain_password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Constant-time comparison of a plaintext password against its hash."""
    return pwd_context.verify(plain_password, hashed_password)


# ---------------------------------------------------------------------------
# RSA Keypair Bootstrap
# ---------------------------------------------------------------------------
def _generate_rsa_keypair(private_path: Path, public_path: Path) -> None:
    """
    Generate a fresh 2048-bit RSA keypair and persist it to disk in PEM
    format. Called only once, at first boot, if no keypair exists.

    2048-bit is chosen as the pragmatic floor recommended by NIST for RSA
    keys in use beyond 2030; it balances signing/verification performance
    (relevant at gateway request volume) against cryptographic strength.
    """
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
        backend=default_backend(),
    )
    public_key = private_key.public_key()

    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )

    private_path.write_bytes(private_pem)
    public_path.write_bytes(public_pem)
    # Restrict private key permissions -- defense in depth on POSIX hosts.
    try:
        private_path.chmod(0o600)
    except (NotImplementedError, PermissionError):
        # Best-effort: some sandboxed/CI filesystems disallow chmod.
        pass


def ensure_keys_exist() -> None:
    """Idempotently ensure an RSA keypair is present on disk before boot."""
    if (
        not settings.JWT_PRIVATE_KEY_PATH.exists()
        or not settings.JWT_PUBLIC_KEY_PATH.exists()
    ):
        _generate_rsa_keypair(
            settings.JWT_PRIVATE_KEY_PATH, settings.JWT_PUBLIC_KEY_PATH
        )


def _load_private_key() -> str:
    ensure_keys_exist()
    return settings.JWT_PRIVATE_KEY_PATH.read_text()


def _load_public_key() -> str:
    ensure_keys_exist()
    return settings.JWT_PUBLIC_KEY_PATH.read_text()


# ---------------------------------------------------------------------------
# Token Data Models
# ---------------------------------------------------------------------------
class TokenPayload(BaseModel):
    """Decoded, validated shape of our JWT's claims."""

    sub: str  # subject == username
    tier: str  # "free" | "premium" | "admin"
    jti: str  # unique token id (useful for future revocation lists)
    iss: str
    exp: int
    iat: int


class CurrentUser(BaseModel):
    """Lightweight authenticated-user representation injected into routes."""

    username: str
    tier: str


# ---------------------------------------------------------------------------
# Token Issuance
# ---------------------------------------------------------------------------
def create_access_token(username: str, tier: str) -> str:
    """
    Mint a new RS256-signed JWT for an authenticated user.

    The token embeds the user's subscription `tier` directly as a claim so
    that downstream middleware (the rate limiter) can make tiering decisions
    WITHOUT a secondary database round-trip on every single request -- an
    important latency optimization at gateway scale.
    """
    now = datetime.now(timezone.utc)
    expire = now + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)

    payload: Dict[str, Any] = {
        "sub": username,
        "tier": tier,
        "jti": str(uuid.uuid4()),
        "iss": settings.JWT_ISSUER,
        "iat": int(now.timestamp()),
        "exp": int(expire.timestamp()),
    }

    private_key = _load_private_key()
    token = jwt.encode(payload, private_key, algorithm=settings.JWT_ALGORITHM)
    return token


# ---------------------------------------------------------------------------
# Token Verification
# ---------------------------------------------------------------------------
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="api/v1/auth/login", auto_error=False)


def decode_access_token(token: str) -> TokenPayload:
    """
    Verify signature + expiration + issuer of an incoming bearer token.

    Raises HTTPException(401) on ANY failure mode (expired, malformed,
    invalid signature, wrong issuer) -- we deliberately do not leak *why*
    a token failed validation to the client, to avoid giving an attacker
    an oracle for crafting forged tokens.
    """
    public_key = _load_public_key()
    try:
        decoded = jwt.decode(
            token,
            public_key,
            algorithms=[settings.JWT_ALGORITHM],
            issuer=settings.JWT_ISSUER,
            options={"require": ["exp", "iat", "sub", "tier"]},
        )
        return TokenPayload(**decoded)
    except jwt.ExpiredSignatureError:
        log_security_event(event_type="TOKEN_EXPIRED", detail="Expired JWT presented")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session expired. Please log in again.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except jwt.PyJWTError:
        log_security_event(
            event_type="TOKEN_INVALID", detail="Malformed or forged JWT presented"
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication credentials.",
            headers={"WWW-Authenticate": "Bearer"},
        )


async def get_current_user(token: str | None = Depends(oauth2_scheme)) -> CurrentUser:
    """
    FastAPI dependency: extracts + validates the bearer token and returns a
    minimal `CurrentUser`. Any route declaring `Depends(get_current_user)`
    is automatically protected -- unauthenticated requests are rejected
    with 401 before any business logic executes.
    """
    if token is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated. Missing bearer token.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    payload = decode_access_token(token)
    return CurrentUser(username=payload.sub, tier=payload.tier)
