"""
models.schemas
================

Pydantic v2 request/response contracts for the gateway's public API.

ARCHITECTURAL DECISION: Keeping all wire-format schemas in one module
(separate from ORM/domain models, of which this demo has none) makes the
public API surface easy to audit in a single file -- important for a
security-sensitive gateway where every field exposed to clients should be
a deliberate decision, not an accident of model reuse.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator


class LoginRequest(BaseModel):
    """Credentials submitted to /api/v1/auth/login."""

    username: str = Field(..., min_length=3, max_length=50, examples=["premium_user"])
    password: str = Field(
        ..., min_length=6, max_length=128, examples=["SecurePass123!"]
    )


class LoginResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    tier: str
    expires_in_minutes: int


class ChatCompletionRequest(BaseModel):
    """Payload for the protected mock AI completion endpoint."""

    prompt: str = Field(..., min_length=1, max_length=4000)
    model: str = Field(default="akash-llm-pro-1")

    @field_validator("prompt")
    @classmethod
    def prompt_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("prompt must not be blank or whitespace-only")
        return value


class ChatCompletionResponse(BaseModel):
    completion_id: str
    model: str
    completion_text: str
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: float
    latency_ms: float
    served_by_fallback: bool


class HealthResponse(BaseModel):
    status: Literal["healthy", "degraded"]
    redis_healthy: bool
    circuit_breaker_state: str
    version: str


class ErrorResponse(BaseModel):
    """Uniform error envelope returned by all custom exception handlers."""

    error: str
    detail: str
    status_code: int
    path: Optional[str] = None
