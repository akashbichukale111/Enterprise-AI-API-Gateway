"""
api.routes
===========

All HTTP routes exposed by the gateway:
    - POST /api/v1/auth/login          -> issue JWT
    - POST /api/v1/ai/chat/completions -> protected, rate-limited, circuit-broken AI call
    - GET  /api/v1/health              -> liveness/readiness probe
    - GET  /api/v1/metrics/dashboard   -> aggregated metrics consumed by the Streamlit UI

ARCHITECTURAL DECISION: Demo users are an in-memory dict rather than a real
database, to keep this reference implementation fully self-contained and
runnable with `docker-compose up` without a seed/migration step. In
production this would be swapped for a call to an identity provider
(Cognito/Auth0) or a Users table -- notably, NOTHING else in the codebase
(security.py, rate_limiter.py, routes below) would need to change, since
they only depend on the abstract (username, tier) pair.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, Request, status

from src.core.logger import log_api_usage
from src.core.security import (
    CurrentUser,
    create_access_token,
    get_current_user,
    hash_password,
    verify_password,
)
from src.middleware.rate_limiter import (
    enforce_ddos_guard,
    enforce_rate_limit,
    rate_limiter,
)
from src.models.schemas import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    HealthResponse,
    LoginRequest,
    LoginResponse,
)
from src.services.circuit_breaker import mock_ai_circuit_breaker
from src.services.cost_tracker import cost_tracker
from src.services.mock_ai_service import (
    AiCompletionResult,
    call_mock_ai_service,
    fallback_ai_response,
)
from src.core.config import get_settings

settings = get_settings()
router = APIRouter(prefix="/api/v1")

# ---------------------------------------------------------------------------
# Demo user store. Passwords are bcrypt-hashed even in-memory -- a
# deliberate choice to never hold a plaintext password anywhere, even in a
# demo, reinforcing correct habits.
# ---------------------------------------------------------------------------
_DEMO_USERS: dict[str, dict[str, str]] = {
    "free_user": {
        "hashed_password": hash_password("FreeUserPass123!"),
        "tier": "free",
    },
    "premium_user": {
        "hashed_password": hash_password("PremiumPass123!"),
        "tier": "premium",
    },
    "admin": {
        "hashed_password": hash_password("AdminPass123!"),
        "tier": "admin",
    },
}


@router.post(
    "/auth/login",
    response_model=LoginResponse,
    status_code=status.HTTP_200_OK,
    summary="Authenticate and receive an RS256-signed JWT",
    dependencies=[Depends(enforce_ddos_guard)],
)
async def login(payload: LoginRequest) -> LoginResponse:
    """
    Validate credentials against the (demo) user store and issue a
    short-lived, asymmetrically-signed access token embedding the user's
    subscription tier.
    """
    from fastapi import HTTPException

    user_record = _DEMO_USERS.get(payload.username)
    if user_record is None or not verify_password(
        payload.password, user_record["hashed_password"]
    ):
        from src.core.logger import log_security_event

        log_security_event(
            event_type="LOGIN_FAILED",
            detail="Invalid username or password presented at login",
            username=payload.username,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password.",
        )

    token = create_access_token(username=payload.username, tier=user_record["tier"])
    return LoginResponse(
        access_token=token,
        tier=user_record["tier"],
        expires_in_minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES,
    )


@router.post(
    "/ai/chat/completions",
    response_model=ChatCompletionResponse,
    status_code=status.HTTP_200_OK,
    summary="Protected mock AI completion endpoint (JWT + Rate Limit + Circuit Breaker + Cost Tracking)",
    dependencies=[Depends(enforce_ddos_guard)],
)
async def create_chat_completion(
    request: Request,
    payload: ChatCompletionRequest,
    current_user: CurrentUser = Depends(get_current_user),
) -> ChatCompletionResponse:
    """
    The gateway's flagship protected route. Request lifecycle:

        1. `get_current_user` dependency already validated the JWT (401 on
           failure) before this function body even executes.
        2. Enforce the tiered token-bucket rate limit (429 on breach).
        3. Invoke the mock AI upstream THROUGH the circuit breaker, so a
           failing/timing-out upstream degrades to a graceful fallback
           instead of raising a 500/504 to the client.
        4. Price the request via the FinOps CostTracker and persist the
           usage record.
        5. Emit a structured JSON log line capturing the full request
           outcome for cloud observability.
    """
    start_time = time.perf_counter()

    await enforce_rate_limit(
        request, username=current_user.username, tier=current_user.tier
    )

    served_by_fallback = False

    async def _fallback_wrapper(prompt: str, model: str) -> AiCompletionResult:
        nonlocal served_by_fallback
        served_by_fallback = True
        return await fallback_ai_response(prompt, model)

    result: AiCompletionResult = await mock_ai_circuit_breaker.call(
        call_mock_ai_service,
        payload.prompt,
        payload.model,
        fallback=_fallback_wrapper,
    )

    usage_record = cost_tracker.record_usage(
        username=current_user.username,
        tier=current_user.tier,
        model=result.model,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
    )

    total_latency_ms = (time.perf_counter() - start_time) * 1000
    log_api_usage(
        username=current_user.username,
        tier=current_user.tier,
        path=str(request.url.path),
        status_code=status.HTTP_200_OK,
        latency_ms=total_latency_ms,
        ip=request.client.host if request.client else None,
    )

    return ChatCompletionResponse(
        completion_id=result.completion_id,
        model=result.model,
        completion_text=result.completion_text,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        estimated_cost_usd=usage_record.cost_usd,
        latency_ms=round(total_latency_ms, 2),
        served_by_fallback=served_by_fallback,
    )


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Liveness / readiness probe",
)
async def health_check() -> HealthResponse:
    """
    Kubernetes/ECS-style health endpoint. Reports `degraded` (rather than
    failing outright) when Redis is unreachable, since the gateway
    continues serving traffic via fail-open fallback paths in that case.
    """
    is_healthy = rate_limiter.redis_healthy
    return HealthResponse(
        status="healthy" if is_healthy else "degraded",
        redis_healthy=is_healthy,
        circuit_breaker_state=mock_ai_circuit_breaker.state.value,
        version=settings.APP_VERSION,
    )


@router.get(
    "/metrics/dashboard",
    summary="Aggregated metrics feed consumed by the Streamlit Enterprise Dashboard",
)
async def dashboard_metrics() -> dict:
    """
    A single aggregation endpoint purpose-built for the dashboard, avoiding
    N+1 calls from the UI. Combines FinOps cost data, circuit breaker
    health, and rate limiter health into one payload.
    """
    summary = cost_tracker.get_summary()
    recent = cost_tracker.get_recent_records(limit=25)

    return {
        "cost_summary": {
            "total_cost_usd": summary.total_cost_usd,
            "total_input_tokens": summary.total_input_tokens,
            "total_output_tokens": summary.total_output_tokens,
            "total_requests": summary.total_requests,
            "cost_by_user": summary.cost_by_user,
            "cost_by_model": summary.cost_by_model,
        },
        "recent_usage": [
            {
                "timestamp": r.timestamp,
                "username": r.username,
                "tier": r.tier,
                "model": r.model,
                "input_tokens": r.input_tokens,
                "output_tokens": r.output_tokens,
                "cost_usd": r.cost_usd,
            }
            for r in recent
        ],
        "circuit_breaker": mock_ai_circuit_breaker.get_metrics(),
        "redis_healthy": rate_limiter.redis_healthy,
    }
