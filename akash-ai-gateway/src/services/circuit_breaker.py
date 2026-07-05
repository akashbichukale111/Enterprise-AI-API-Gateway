"""
services.circuit_breaker
=========================

A from-scratch implementation of the Circuit Breaker resilience pattern
(as popularized by Netflix's Hystrix and Michael Nygard's "Release It!").

ARCHITECTURAL DECISION -- Why a Circuit Breaker at all:
    Without one, a slow or failing upstream AI provider causes every
    incoming request to wait out a full timeout before failing. Under load,
    this exhausts gateway worker threads/connections queued behind a dying
    dependency -- a single flaky downstream service can cascade into a
    total gateway outage. The circuit breaker "trips" after repeated
    failures and starts failing FAST (no network call at all), giving the
    downstream service time to recover while the gateway keeps serving a
    graceful fallback response instead of hanging or crashing.

STATE MACHINE:

    CLOSED --(failures >= threshold)--> OPEN
    OPEN --(recovery_timeout elapses)--> HALF_OPEN
    HALF_OPEN --(trial call succeeds)--> CLOSED
    HALF_OPEN --(trial call fails)-----> OPEN

    - CLOSED: normal operation. Calls pass through. Failures are counted.
    - OPEN: calls are short-circuited immediately to a fallback -- no
      network call is attempted at all, protecting the caller's latency
      budget and the downstream service from further load while it
      recovers.
    - HALF_OPEN: after the recovery timeout, we cautiously allow a small
      number of trial calls through. Success closes the circuit again;
      any failure reopens it (with the timeout reset).
"""

from __future__ import annotations

import asyncio
import time
from enum import Enum
from typing import Any, Awaitable, Callable, Optional

from src.core.config import get_settings
from src.core.logger import log_circuit_breaker_event

settings = get_settings()


class CircuitState(str, Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class CircuitBreakerOpenError(Exception):
    """Raised when a call is rejected because the circuit is OPEN."""


class CircuitBreaker:
    """
    A reusable, async-aware circuit breaker guarding a single logical
    upstream dependency (e.g. "mock_ai_service").
    """

    def __init__(
        self,
        name: str,
        failure_threshold: int = settings.CIRCUIT_BREAKER_FAILURE_THRESHOLD,
        recovery_timeout: int = settings.CIRCUIT_BREAKER_RECOVERY_TIMEOUT_SECONDS,
        half_open_max_calls: int = settings.CIRCUIT_BREAKER_HALF_OPEN_MAX_CALLS,
    ) -> None:
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.half_open_max_calls = half_open_max_calls

        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count_half_open = 0
        self._last_failure_time: Optional[float] = None
        self._lock = asyncio.Lock()

        # Metrics -- surfaced to the dashboard via /api/v1/metrics/dashboard
        self.total_calls = 0
        self.total_failures = 0
        self.total_short_circuited = 0
        self.total_fallbacks_served = 0

    # ------------------------------------------------------------------
    # State transition helpers
    # ------------------------------------------------------------------
    def _transition_to(self, new_state: CircuitState, detail: str) -> None:
        if new_state != self._state:
            log_circuit_breaker_event(
                service=self.name, state=new_state.value, detail=detail
            )
        self._state = new_state

    def _should_attempt_reset(self) -> bool:
        return (
            self._last_failure_time is not None
            and (time.time() - self._last_failure_time) >= self.recovery_timeout
        )

    @property
    def state(self) -> CircuitState:
        # Lazily evaluate OPEN -> HALF_OPEN transition on read, so the
        # dashboard always sees a fresh, accurate state even between calls.
        if self._state == CircuitState.OPEN and self._should_attempt_reset():
            self._transition_to(
                CircuitState.HALF_OPEN,
                detail=f"Recovery timeout of {self.recovery_timeout}s elapsed; "
                f"allowing trial requests through.",
            )
            self._success_count_half_open = 0
        return self._state

    def get_metrics(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "state": self.state.value,
            "failure_count": self._failure_count,
            "failure_threshold": self.failure_threshold,
            "total_calls": self.total_calls,
            "total_failures": self.total_failures,
            "total_short_circuited": self.total_short_circuited,
            "total_fallbacks_served": self.total_fallbacks_served,
            "last_failure_time": self._last_failure_time,
        }

    # ------------------------------------------------------------------
    # Core execution path
    # ------------------------------------------------------------------
    async def call(
        self,
        func: Callable[..., Awaitable[Any]],
        *args: Any,
        fallback: Optional[Callable[..., Awaitable[Any]]] = None,
        **kwargs: Any,
    ) -> Any:
        """
        Execute `func` guarded by the circuit breaker.

        If the circuit is OPEN, `func` is never invoked -- we immediately
        invoke `fallback` (if provided) or raise `CircuitBreakerOpenError`.
        """
        self.total_calls += 1
        current_state = self.state

        if current_state == CircuitState.OPEN:
            self.total_short_circuited += 1
            if fallback is not None:
                self.total_fallbacks_served += 1
                return await fallback(*args, **kwargs)
            raise CircuitBreakerOpenError(
                f"Circuit '{self.name}' is OPEN -- short-circuiting call."
            )

        try:
            result = await func(*args, **kwargs)
        except (
            Exception
        ) as exc:  # noqa: BLE001 - intentionally broad: any upstream failure trips the breaker
            await self._record_failure(exc)
            if fallback is not None:
                self.total_fallbacks_served += 1
                return await fallback(*args, **kwargs)
            raise
        else:
            await self._record_success()
            return result

    async def _record_success(self) -> None:
        async with self._lock:
            if self._state == CircuitState.HALF_OPEN:
                self._success_count_half_open += 1
                if self._success_count_half_open >= self.half_open_max_calls:
                    self._failure_count = 0
                    self._transition_to(
                        CircuitState.CLOSED,
                        detail="Trial calls succeeded in HALF_OPEN -- closing circuit, "
                        "upstream service has recovered.",
                    )
            elif self._state == CircuitState.CLOSED:
                # A success in CLOSED state decays the failure counter,
                # preventing sporadic, non-consecutive failures from ever
                # accumulating enough to trip the breaker.
                self._failure_count = max(0, self._failure_count - 1)

    async def _record_failure(self, exc: Exception) -> None:
        async with self._lock:
            self.total_failures += 1
            self._last_failure_time = time.time()

            if self._state == CircuitState.HALF_OPEN:
                self._transition_to(
                    CircuitState.OPEN,
                    detail=f"Trial call failed in HALF_OPEN ({exc!r}) -- reopening circuit.",
                )
                return

            self._failure_count += 1
            if self._failure_count >= self.failure_threshold:
                self._transition_to(
                    CircuitState.OPEN,
                    detail=(
                        f"Failure threshold reached ({self._failure_count}/"
                        f"{self.failure_threshold}) after error {exc!r} -- tripping circuit."
                    ),
                )

    def reset(self) -> None:
        """Administrative override: force the circuit back to CLOSED."""
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count_half_open = 0
        self._last_failure_time = None


# A single shared breaker instance guarding the mock AI upstream. In a
# multi-dependency system, you would instantiate one CircuitBreaker per
# distinct downstream (e.g. one for the LLM provider, one for a vector DB).
mock_ai_circuit_breaker = CircuitBreaker(name="mock_ai_service")
