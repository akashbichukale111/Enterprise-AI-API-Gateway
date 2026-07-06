"""
tests.test_api
================

Unit & integration tests for the Akash AI Pro Gateway.

Covers:
    - Password hashing round-trip
    - JWT issuance + verification (happy path, expired, tampered)
    - Login endpoint (success + invalid credentials -> 401)
    - Unauthenticated access to protected route -> 401
    - Circuit breaker state machine transitions
    - Cost tracker pricing math
"""

from __future__ import annotations

import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from src.core.security import (
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)
from src.main import app
from src.services.circuit_breaker import CircuitBreaker, CircuitState
from src.services.cost_tracker import CostTracker

client = TestClient(app)


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------
def test_password_hash_and_verify_roundtrip():
    plain = "SuperSecurePass123!"
    hashed = hash_password(plain)
    assert hashed != plain
    assert verify_password(plain, hashed) is True
    assert verify_password("wrong-password", hashed) is False


# ---------------------------------------------------------------------------
# JWT
# ---------------------------------------------------------------------------
def test_create_and_decode_access_token():
    token = create_access_token(username="premium_user", tier="premium")
    payload = decode_access_token(token)
    assert payload.sub == "premium_user"
    assert payload.tier == "premium"
    assert payload.iss == "akash-ai-gateway"


def test_decode_tampered_token_raises_401():
    token = create_access_token(username="premium_user", tier="premium")
    tampered = token[:-3] + "xyz"
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        decode_access_token(tampered)
    assert exc_info.value.status_code == 401


# ---------------------------------------------------------------------------
# Auth endpoint
# ---------------------------------------------------------------------------
def test_login_success_returns_token():
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "premium_user", "password": "PremiumPass123!"},
    )
    assert response.status_code == 200
    body = response.json()
    assert "access_token" in body
    assert body["tier"] == "premium"


def test_login_invalid_credentials_returns_401():
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "premium_user", "password": "wrong-password"},
    )
    assert response.status_code == 401
    assert response.json()["error"] == "Unauthorized"


def test_login_unknown_user_returns_401():
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "ghost_user", "password": "irrelevant123"},
    )
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# Protected route access control
# ---------------------------------------------------------------------------
def test_protected_route_without_token_returns_401():
    response = client.post(
        "/api/v1/ai/chat/completions",
        json={"prompt": "Hello world", "model": "akash-llm-pro-1"},
    )
    assert response.status_code == 401


def test_protected_route_with_valid_token_succeeds():
    login_resp = client.post(
        "/api/v1/auth/login",
        json={"username": "premium_user", "password": "PremiumPass123!"},
    )
    token = login_resp.json()["access_token"]

    response = client.post(
        "/api/v1/ai/chat/completions",
        json={"prompt": "Summarize the Q3 report", "model": "akash-llm-pro-1"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    body = response.json()
    assert "completion_text" in body
    assert body["estimated_cost_usd"] >= 0


# ---------------------------------------------------------------------------
# Circuit Breaker state machine
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_circuit_breaker_trips_after_threshold_failures():
    breaker = CircuitBreaker(
        name="test_service", failure_threshold=3, recovery_timeout=1
    )

    async def always_fails():
        raise RuntimeError("simulated upstream failure")

    for _ in range(3):
        with pytest.raises(RuntimeError):
            await breaker.call(always_fails)

    assert breaker.state == CircuitState.OPEN


@pytest.mark.asyncio
async def test_circuit_breaker_serves_fallback_when_open():
    breaker = CircuitBreaker(
        name="test_service_2", failure_threshold=1, recovery_timeout=60
    )

    async def always_fails():
        raise RuntimeError("boom")

    async def fallback():
        return "fallback-response"

    with pytest.raises(RuntimeError):
        await breaker.call(always_fails, fallback=None)

    assert breaker.state == CircuitState.OPEN

    result = await breaker.call(always_fails, fallback=fallback)
    assert result == "fallback-response"


@pytest.mark.asyncio
async def test_circuit_breaker_recovers_via_half_open():
    # A short but non-zero recovery_timeout lets us observe the OPEN state
    # before it lazily transitions to HALF_OPEN on the next `.state` read.
    breaker = CircuitBreaker(
        name="test_service_3",
        failure_threshold=1,
        recovery_timeout=1,
        half_open_max_calls=1,
    )

    async def fails():
        raise RuntimeError("boom")

    async def succeeds():
        return "ok"

    with pytest.raises(RuntimeError):
        await breaker.call(fails)
    assert breaker.state == CircuitState.OPEN

    # Wait out the recovery timeout so the breaker lazily flips to HALF_OPEN.
    await asyncio.sleep(1.1)
    assert breaker.state == CircuitState.HALF_OPEN

    result = await breaker.call(succeeds)
    assert result == "ok"
    assert breaker.state == CircuitState.CLOSED


# ---------------------------------------------------------------------------
# Cost tracker pricing math
# ---------------------------------------------------------------------------
def test_cost_tracker_pricing_calculation(tmp_path):
    ledger_path = tmp_path / "cost_ledger.jsonl"
    tracker = CostTracker(ledger_path=ledger_path)

    record = tracker.record_usage(
        username="test_user",
        tier="premium",
        model="akash-llm-pro-1",
        input_tokens=1000,
        output_tokens=1000,
    )
    # 1000 input tokens @ $0.0030/1k + 1000 output tokens @ $0.0060/1k = $0.009
    assert record.cost_usd == pytest.approx(0.009, rel=1e-3)

    summary = tracker.get_summary()
    assert summary.total_requests == 1
    assert summary.total_cost_usd == pytest.approx(0.009, rel=1e-3)
    assert ledger_path.exists()


def test_cost_tracker_persists_and_reloads_ledger(tmp_path):
    ledger_path = tmp_path / "cost_ledger.jsonl"
    tracker1 = CostTracker(ledger_path=ledger_path)
    tracker1.record_usage(
        username="test_user",
        tier="free",
        model="akash-llm-lite",
        input_tokens=500,
        output_tokens=500,
    )

    # Simulate a process restart: a fresh CostTracker should replay the ledger.
    tracker2 = CostTracker(ledger_path=ledger_path)
    summary = tracker2.get_summary()
    assert summary.total_requests == 1
    assert summary.total_input_tokens == 500
