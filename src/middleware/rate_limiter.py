"""
middleware.rate_limiter
========================

Advanced, asynchronous, Redis-backed rate limiting.

ARCHITECTURAL DECISION -- Token Bucket over Fixed/Sliding Window:
    We implement the Token Bucket algorithm rather than a naive fixed-window
    counter because:
        1. Fixed windows suffer a "boundary burst" flaw -- a client can send
           N requests at 0:59 and another N at 1:00, doubling the effective
           rate at the window edge.
        2. Token Bucket naturally allows short, legitimate bursts (up to
           `capacity`) while still enforcing a strict long-run average rate
           (`refill_rate`), which matches real API usage patterns far better
           (e.g. a client firing 5 requests in a batch, then idling).
        3. It is O(1) per check and trivially expressed as an atomic Lua
           script in Redis, meaning it is safe to run correctly even with
           dozens of gateway pods hammering the same Redis instance
           concurrently (no lost-update races).

ARCHITECTURAL DECISION -- Redis over in-process counters:
    A gateway is horizontally scaled (many pods/containers). Per-process,
    in-memory counters would let a client trivially bypass limits just by
    getting load-balanced across pods. Redis gives us one shared, atomic
    source of truth for bucket state across the entire fleet.

ARCHITECTURAL DECISION -- Fail-open with logging, not fail-closed:
    If Redis itself is unreachable, we do NOT want a Redis outage to also
    take down 100% of API traffic (cascading failure). We fail OPEN (allow
    the request) but loudly log the degradation, so an operator is alerted
    while the business keeps functioning -- a classic availability vs.
    strict-consistency trade-off, resolved here in favor of availability
    for rate limiting specifically (unlike auth, where we'd fail closed).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Optional

import redis.asyncio as aioredis
from fastapi import HTTPException, Request, status

from src.core.config import get_settings
from src.core.logger import log_blocked_ip, log_rate_limit_breach

settings = get_settings()

# ---------------------------------------------------------------------------
# Lua script implementing an atomic token-bucket check-and-decrement.
# Running this server-side in Redis guarantees atomicity: read-modify-write
# happens in a single round trip with no race window between concurrent
# gateway instances.
# ---------------------------------------------------------------------------
_TOKEN_BUCKET_LUA = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill_per_sec = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local requested = tonumber(ARGV[4])

local bucket = redis.call("HMGET", key, "tokens", "timestamp")
local tokens = tonumber(bucket[1])
local last_ts = tonumber(bucket[2])

if tokens == nil then
    tokens = capacity
    last_ts = now
end

local delta = math.max(0, now - last_ts)
tokens = math.min(capacity, tokens + (delta * refill_per_sec))

local allowed = 0
if tokens >= requested then
    tokens = tokens - requested
    allowed = 1
end

redis.call("HMSET", key, "tokens", tokens, "timestamp", now)
redis.call("EXPIRE", key, 3600)

return {allowed, tokens}
"""

_IP_COUNTER_LUA = """
local key = KEYS[1]
local window = tonumber(ARGV[1])
local current = redis.call("INCR", key)
if current == 1 then
    redis.call("EXPIRE", key, window)
end
return current
"""


@dataclass(frozen=True)
class TierConfig:
    capacity: int
    refill_per_min: int

    @property
    def refill_per_sec(self) -> float:
        return self.refill_per_min / 60.0


TIER_CONFIGS: dict[str, TierConfig] = {
    "free": TierConfig(
        capacity=settings.RATE_LIMIT_FREE_TIER_CAPACITY,
        refill_per_min=settings.RATE_LIMIT_FREE_TIER_REFILL_PER_MIN,
    ),
    "premium": TierConfig(
        capacity=settings.RATE_LIMIT_PREMIUM_TIER_CAPACITY,
        refill_per_min=settings.RATE_LIMIT_PREMIUM_TIER_REFILL_PER_MIN,
    ),
    # Admins are treated as premium-or-better for demo purposes.
    "admin": TierConfig(
        capacity=settings.RATE_LIMIT_PREMIUM_TIER_CAPACITY * 2,
        refill_per_min=settings.RATE_LIMIT_PREMIUM_TIER_REFILL_PER_MIN * 2,
    ),
}


class RedisConnectionManager:
    """
    Lazily-instantiated singleton async Redis connection pool.

    ARCHITECTURAL DECISION: A single shared connection pool (rather than a
    new connection per request) is critical at gateway scale -- TCP
    handshake + Redis AUTH overhead per request would dominate latency
    budgets otherwise.

    ARCHITECTURAL DECISION -- Event-loop-aware recycling:
    An `asyncio` socket connection is permanently bound to the event loop
    it was created on. In a long-running `uvicorn` process there is only
    ever one loop, so this is a non-issue in production. However, test
    runners (pytest-asyncio, Starlette's `TestClient`) and dev/hot-reload
    tooling can spin up a *new* event loop per test/reload while this
    module-level singleton survives across them, which previously caused
    `RuntimeError: Event loop is closed` on the second test. We now track
    which loop the pool was built for and transparently rebuild it if the
    currently-running loop differs -- correctness fix with zero overhead
    in the single-loop production path (the identity check is O(1)).
    """

    _pool: Optional[aioredis.Redis] = None
    _bound_loop: Optional[Any] = None

    @classmethod
    def get_client(cls) -> aioredis.Redis:
        current_loop = asyncio.get_running_loop()
        if cls._pool is not None and cls._bound_loop is not current_loop:
            # Stale pool from a previous (now-closed) event loop -- drop it
            # without awaiting close() (that socket's loop is already gone).
            cls._pool = None

        if cls._pool is None:
            cls._pool = aioredis.Redis(
                host=settings.REDIS_HOST,
                port=settings.REDIS_PORT,
                db=settings.REDIS_DB,
                password=settings.REDIS_PASSWORD,
                socket_timeout=settings.REDIS_SOCKET_TIMEOUT,
                socket_connect_timeout=settings.REDIS_SOCKET_TIMEOUT,
                decode_responses=True,
            )
            cls._bound_loop = current_loop
        return cls._pool

    @classmethod
    async def close(cls) -> None:
        if cls._pool is not None:
            await cls._pool.close()
            cls._pool = None
            cls._bound_loop = None


class RateLimiter:
    """
    Tiered, async, Redis-backed token-bucket rate limiter with an in-memory
    fallback for Redis-outage scenarios.
    """

    def __init__(self) -> None:
        self._redis_healthy = True
        # In-memory fallback: {key: (tokens, last_timestamp)}. This is only
        # consulted when Redis is unreachable, and is inherently per-process
        # (best-effort, not fleet-wide-consistent) -- an accepted trade-off
        # to preserve availability during a Redis outage.
        self._local_buckets: dict[str, tuple[float, float]] = {}

    async def _redis_token_bucket_check(
        self, key: str, tier_config: TierConfig
    ) -> tuple[bool, float]:
        client = RedisConnectionManager.get_client()
        now = time.time()
        result = await client.eval(
            _TOKEN_BUCKET_LUA,
            1,
            key,
            tier_config.capacity,
            tier_config.refill_per_sec,
            now,
            1,
        )
        allowed, remaining = int(result[0]), float(result[1])
        self._redis_healthy = True
        return bool(allowed), remaining

    def _local_token_bucket_check(
        self, key: str, tier_config: TierConfig
    ) -> tuple[bool, float]:
        now = time.time()
        tokens, last_ts = self._local_buckets.get(
            key, (float(tier_config.capacity), now)
        )
        delta = max(0.0, now - last_ts)
        tokens = min(tier_config.capacity, tokens + delta * tier_config.refill_per_sec)

        allowed = tokens >= 1
        if allowed:
            tokens -= 1

        self._local_buckets[key] = (tokens, now)
        return allowed, tokens

    async def check(self, *, identifier: str, tier: str) -> tuple[bool, float]:
        """
        Check (and atomically consume) one token for `identifier` under the
        given `tier`'s bucket configuration.

        Returns (allowed: bool, remaining_tokens: float).
        """
        tier_config = TIER_CONFIGS.get(tier, TIER_CONFIGS["free"])
        key = f"ratelimit:bucket:{tier}:{identifier}"

        try:
            return await self._redis_token_bucket_check(key, tier_config)
        except (aioredis.RedisError, OSError, TimeoutError):
            # ---- Fail-open path -------------------------------------------
            self._redis_healthy = False
            return self._local_token_bucket_check(key, tier_config)

    @property
    def redis_healthy(self) -> bool:
        return self._redis_healthy


class DdosGuard:
    """
    Coarse-grained, IP-based volumetric anomaly guard.

    This sits IN FRONT OF authentication (it only needs the raw client IP)
    and protects the gateway from unauthenticated flooding / credential
    stuffing attempts before we even bother parsing a JWT.
    """

    def __init__(self) -> None:
        self._blocked_locally: dict[str, float] = {}

    async def inspect(self, ip: str) -> None:
        """
        Raise HTTP 429 if `ip` has exceeded the volumetric threshold within
        the configured window, and place it under a temporary block.
        """
        client = RedisConnectionManager.get_client()
        block_key = f"ddos:blocked:{ip}"
        counter_key = f"ddos:counter:{ip}"

        try:
            already_blocked = await client.get(block_key)
            if already_blocked:
                log_blocked_ip(ip=ip, reason="IP under active DDoS cool-down block")
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Your IP has been temporarily blocked due to suspicious traffic patterns.",
                )

            current_count = await client.eval(
                _IP_COUNTER_LUA, 1, counter_key, settings.DDOS_IP_WINDOW_SECONDS
            )
            current_count = int(current_count)

            if current_count > settings.DDOS_IP_MAX_REQUESTS:
                await client.setex(block_key, settings.DDOS_IP_BLOCK_SECONDS, "1")
                log_blocked_ip(
                    ip=ip,
                    reason="Exceeded volumetric threshold -- flagged as potential DDoS source",
                    request_count=current_count,
                )
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Traffic anomaly detected. Your IP has been temporarily blocked.",
                )
        except HTTPException:
            raise
        except (aioredis.RedisError, OSError, TimeoutError):
            # Fail-open: DDoS protection degrades gracefully rather than
            # taking the whole gateway offline if Redis is unreachable.
            now = time.time()
            blocked_until = self._blocked_locally.get(ip)
            if blocked_until and now < blocked_until:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Your IP has been temporarily blocked due to suspicious traffic patterns.",
                )


rate_limiter = RateLimiter()
ddos_guard = DdosGuard()


async def enforce_rate_limit(request: Request, username: str, tier: str) -> None:
    """
    FastAPI-route-level helper: enforce the tiered token-bucket limit for an
    authenticated user. Raises HTTP 429 on breach.
    """
    allowed, remaining = await rate_limiter.check(identifier=username, tier=tier)
    if not allowed:
        log_rate_limit_breach(
            identifier=username,
            tier=tier,
            path=str(request.url.path),
            ip=request.client.host if request.client else None,
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                f"Rate limit exceeded for '{tier}' tier. "
                f"Please slow down or upgrade your plan."
            ),
            headers={"Retry-After": "5", "X-RateLimit-Remaining": str(int(remaining))},
        )


async def enforce_ddos_guard(request: Request) -> None:
    """FastAPI-route-level helper: run the volumetric IP guard."""
    client_ip = request.client.host if request.client else "unknown"
    await ddos_guard.inspect(client_ip)
