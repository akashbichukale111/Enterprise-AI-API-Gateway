"""
services.mock_ai_service
==========================

Simulates a downstream AI inference provider (e.g. an internal LLM cluster
or a third-party provider such as OpenAI/Anthropic behind the gateway).

This module deliberately injects realistic latency variance and a
configurable random failure rate so that the Circuit Breaker has real
failure conditions to react to when this codebase is run standalone/demo
mode, without requiring an actual paid LLM API key to showcase resilience
behavior end-to-end.
"""

from __future__ import annotations

import asyncio
import random
import time
import uuid
from dataclasses import dataclass

from src.core.config import get_settings

settings = get_settings()


class UpstreamServiceError(Exception):
    """Raised when the simulated upstream AI provider fails or times out."""


class UpstreamTimeoutError(UpstreamServiceError):
    """Raised specifically when the simulated call exceeds its timeout budget."""


@dataclass
class AiCompletionResult:
    completion_id: str
    model: str
    prompt: str
    completion_text: str
    input_tokens: int
    output_tokens: int
    latency_ms: float


# A small pool of canned, topic-varied completions so demo traffic looks
# organic and non-repetitive on the dashboard, without needing a live LLM.
_CANNED_COMPLETIONS = [
    "Based on the provided context, the recommended architecture pattern is a "
    "layered microservice approach with an API gateway as the single entry point.",
    "The quarterly analysis indicates a 12% increase in throughput after the "
    "caching layer was introduced, with p99 latency dropping significantly.",
    "Here is a summary of the key risk factors identified in the uploaded "
    "document, ranked by potential business impact.",
    "The generated code implements a binary search tree with insertion, "
    "deletion, and in-order traversal methods, all achieving O(log n) average complexity.",
    "Customer sentiment across the analyzed support tickets skews positive, "
    "with the primary friction point being onboarding documentation clarity.",
    "The proposed database schema normalizes the orders table to third normal "
    "form, eliminating the previously identified update anomalies.",
]


def _estimate_tokens(text: str) -> int:
    """
    Rough token estimator (~4 characters per token), matching the widely
    used approximation for English text with GPT-family tokenizers. This
    avoids pulling in a full tokenizer dependency for a mock/demo service.
    """
    return max(1, len(text) // 4)


async def call_mock_ai_service(
    prompt: str, model: str = "akash-llm-pro-1"
) -> AiCompletionResult:
    """
    Simulate an async call to an upstream AI completion service.

    Raises `UpstreamTimeoutError` or `UpstreamServiceError` at the
    configured failure rate to exercise the circuit breaker under
    realistic conditions.
    """
    start = time.perf_counter()

    simulated_latency_s = (
        random.randint(settings.MOCK_AI_MIN_LATENCY_MS, settings.MOCK_AI_MAX_LATENCY_MS)
        / 1000.0
    )

    try:
        await asyncio.wait_for(
            asyncio.sleep(simulated_latency_s), timeout=settings.MOCK_AI_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError as exc:
        raise UpstreamTimeoutError(
            f"Upstream AI service '{model}' timed out after {settings.MOCK_AI_TIMEOUT_SECONDS}s"
        ) from exc

    if random.random() < settings.MOCK_AI_FAILURE_RATE:
        raise UpstreamServiceError(
            f"Upstream AI service '{model}' returned a 5xx error"
        )

    completion_text = random.choice(_CANNED_COMPLETIONS)
    latency_ms = (time.perf_counter() - start) * 1000

    return AiCompletionResult(
        completion_id=str(uuid.uuid4()),
        model=model,
        prompt=prompt,
        completion_text=completion_text,
        input_tokens=_estimate_tokens(prompt),
        output_tokens=_estimate_tokens(completion_text),
        latency_ms=latency_ms,
    )


async def fallback_ai_response(
    prompt: str, model: str = "akash-llm-pro-1"
) -> AiCompletionResult:
    """
    Graceful degradation path invoked by the Circuit Breaker when the
    upstream is failing/OPEN. Returns instantly (no simulated latency) with
    a clearly-labeled fallback message rather than propagating a 500/504 to
    the end user.
    """
    return AiCompletionResult(
        completion_id=str(uuid.uuid4()),
        model=f"{model}-fallback",
        prompt=prompt,
        completion_text=(
            "⚠️ The AI service is currently experiencing degraded performance. "
            "This is a cached/fallback response served by the Circuit Breaker "
            "to protect system stability. Please try again shortly."
        ),
        input_tokens=_estimate_tokens(prompt),
        output_tokens=0,
        latency_ms=0.0,
    )
