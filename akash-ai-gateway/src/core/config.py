"""
core.config
===========

Centralized configuration management for the Akash AI Pro Gateway.

ARCHITECTURAL DECISION:
    We use `pydantic-settings` (BaseSettings) instead of hardcoding constants
    or scattering `os.environ.get(...)` calls across the codebase. This gives
    us:
        1. A single source of truth for every tunable parameter.
        2. Automatic type coercion + validation at process startup (fail
           fast if misconfigured, rather than failing mid-request).
        3. Native support for `.env` files, which keeps secrets out of
           version control while remaining ergonomic for local development.
        4. Easy overriding via real environment variables in production
           (Kubernetes/ECS inject env vars; no code changes required).
"""

from functools import lru_cache
from pathlib import Path
from typing import List

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent.parent
KEYS_DIR = BASE_DIR / "keys"
LOGS_DIR = BASE_DIR / "logs"

KEYS_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)


class Settings(BaseSettings):
    """
    Strongly-typed application settings.

    Every field below can be overridden via an environment variable of the
    same (uppercased) name, or via a `.env` file at the project root.
    """

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- General -------------------------------------------------------
    APP_NAME: str = "Akash AI Pro - Secure Enterprise API Gateway"
    APP_VERSION: str = "1.0.0"
    ENVIRONMENT: str = "production"  # production | staging | development
    DEBUG: bool = False

    # --- Security / JWT --------------------------------------------------
    # RS256 (asymmetric) is deliberately chosen over HS256.
    # WHY: With HS256 a single shared secret both signs and verifies tokens,
    # meaning every microservice that needs to *verify* a token also gains
    # the ability to *mint* one. With RS256, the private key stays solely on
    # the Auth/Gateway service, while any downstream microservice can be
    # handed only the public key to verify tokens -- a strict blast-radius
    # reduction if a downstream service is ever compromised.
    JWT_ALGORITHM: str = "RS256"
    JWT_PRIVATE_KEY_PATH: Path = KEYS_DIR / "private_key.pem"
    JWT_PUBLIC_KEY_PATH: Path = KEYS_DIR / "public_key.pem"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    JWT_ISSUER: str = "akash-ai-gateway"

    # --- CORS ------------------------------------------------------------
    CORS_ORIGINS: List[str] = ["*"]

    # --- Redis -------------------------------------------------------------
    REDIS_HOST: str = "localhost"
    REDIS_PORT: int = 6379
    REDIS_DB: int = 0
    REDIS_PASSWORD: str | None = None
    REDIS_SOCKET_TIMEOUT: float = 2.0  # seconds; fail fast toward fallback path

    # --- Rate Limiting (Token Bucket) ------------------------------------
    RATE_LIMIT_FREE_TIER_CAPACITY: int = 5  # max burst tokens
    RATE_LIMIT_FREE_TIER_REFILL_PER_MIN: int = 5  # tokens refilled per minute
    RATE_LIMIT_PREMIUM_TIER_CAPACITY: int = 100
    RATE_LIMIT_PREMIUM_TIER_REFILL_PER_MIN: int = 100

    # DDoS heuristic: raw (unauthenticated-aware) requests per IP per window
    DDOS_IP_WINDOW_SECONDS: int = 10
    DDOS_IP_MAX_REQUESTS: int = 30
    DDOS_IP_BLOCK_SECONDS: int = 300  # 5 minute cool-down once flagged

    # --- Circuit Breaker ---------------------------------------------------
    CIRCUIT_BREAKER_FAILURE_THRESHOLD: int = 5
    CIRCUIT_BREAKER_RECOVERY_TIMEOUT_SECONDS: int = 30
    CIRCUIT_BREAKER_HALF_OPEN_MAX_CALLS: int = 2

    # --- Mock AI Service ----------------------------------------------------
    MOCK_AI_TIMEOUT_SECONDS: float = 2.5
    MOCK_AI_FAILURE_RATE: float = 0.15  # simulate ~15% upstream failure rate
    MOCK_AI_MIN_LATENCY_MS: int = 150
    MOCK_AI_MAX_LATENCY_MS: int = 3200

    # --- FinOps / Cost Tracking ---------------------------------------------
    COST_LOG_PATH: Path = LOGS_DIR / "cost_ledger.jsonl"

    # --- Logging -----------------------------------------------------------
    LOG_FILE_PATH: Path = LOGS_DIR / "gateway_events.jsonl"
    LOG_LEVEL: str = "INFO"

    # --- Demo / seed users (for a self-contained, runnable demo) ------------
    # In a real deployment these live in a proper user store (RDS/Cognito/etc).
    DEMO_USERS_SEEDED: bool = True


@lru_cache
def get_settings() -> Settings:
    """
    Return a cached singleton Settings instance.

    ARCHITECTURAL DECISION: `lru_cache` ensures the `.env` file and
    environment are parsed exactly once per process, avoiding repeated
    disk/env reads on every request while still allowing dependency
    injection (`Depends(get_settings)`) for testability.
    """
    return Settings()
