"""
services.cost_tracker
=======================

FinOps: AI Token Cost Tracking.

ARCHITECTURAL DECISION:
    Uncontrolled AI token spend is one of the top emerging FinOps risks for
    any org exposing LLM endpoints -- a single runaway client or infinite
    retry loop can generate a five-figure bill overnight. This module
    attributes a dollar cost to EVERY request at the gateway layer (the
    earliest possible point of observability), rather than relying on a
    downstream billing reconciliation job that only surfaces costs hours or
    days later.

    We persist every priced request as an append-only JSON Lines ledger
    (`logs/cost_ledger.jsonl`). This mirrors how real FinOps pipelines
    ingest usage data (e.g. AWS Cost & Usage Reports are also delivered as
    line-oriented records) and lets the dashboard replay/aggregate spend
    trivially without a database dependency for this demo.
"""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

from src.core.config import get_settings

settings = get_settings()

# ---------------------------------------------------------------------------
# Pricing table -- $ per 1,000 tokens, modeled on realistic tiered LLM
# pricing (input tokens are cheaper than output/generation tokens across
# essentially every commercial provider, since generation is more
# compute-intensive than the prefill/encode pass).
# ---------------------------------------------------------------------------
PRICING_TABLE: Dict[str, Dict[str, float]] = {
    "akash-llm-pro-1": {"input_per_1k": 0.0030, "output_per_1k": 0.0060},
    "akash-llm-pro-1-fallback": {"input_per_1k": 0.0000, "output_per_1k": 0.0000},
    "akash-llm-lite": {"input_per_1k": 0.0005, "output_per_1k": 0.0015},
}

DEFAULT_PRICING = {"input_per_1k": 0.0030, "output_per_1k": 0.0060}


@dataclass
class UsageRecord:
    timestamp: str
    username: str
    tier: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float


@dataclass
class CostSummary:
    total_cost_usd: float = 0.0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_requests: int = 0
    cost_by_user: Dict[str, float] = field(default_factory=dict)
    cost_by_model: Dict[str, float] = field(default_factory=dict)


class CostTracker:
    """
    Thread/coroutine-safe, in-memory + append-only-file cost ledger.

    ARCHITECTURAL DECISION: We keep a hot in-memory aggregate (for
    O(1)-per-request dashboard reads) *and* durably append every raw record
    to disk. On process restart, the in-memory aggregate is rebuilt by
    replaying the ledger file -- giving us crash-safety without needing a
    full external database for this reference implementation.
    """

    def __init__(self, ledger_path: Path) -> None:
        self._ledger_path = ledger_path
        self._lock = threading.Lock()
        self._records: List[UsageRecord] = []
        self._summary = CostSummary()
        self._load_existing_ledger()

    def _load_existing_ledger(self) -> None:
        if not self._ledger_path.exists():
            return
        with self._ledger_path.open("r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    record = UsageRecord(**data)
                    self._records.append(record)
                    self._apply_to_summary(record)
                except (json.JSONDecodeError, TypeError):
                    continue  # skip corrupt lines rather than crashing startup

    def _apply_to_summary(self, record: UsageRecord) -> None:
        self._summary.total_cost_usd += record.cost_usd
        self._summary.total_input_tokens += record.input_tokens
        self._summary.total_output_tokens += record.output_tokens
        self._summary.total_requests += 1
        self._summary.cost_by_user[record.username] = (
            self._summary.cost_by_user.get(record.username, 0.0) + record.cost_usd
        )
        self._summary.cost_by_model[record.model] = (
            self._summary.cost_by_model.get(record.model, 0.0) + record.cost_usd
        )

    @staticmethod
    def calculate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
        pricing = PRICING_TABLE.get(model, DEFAULT_PRICING)
        cost = (input_tokens / 1000.0) * pricing["input_per_1k"] + (
            output_tokens / 1000.0
        ) * pricing["output_per_1k"]
        return round(cost, 6)

    def record_usage(
        self,
        *,
        username: str,
        tier: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
    ) -> UsageRecord:
        """
        Price a completed AI request and durably record it. Thread-safe:
        FastAPI may run sync dependency code in a threadpool, and multiple
        requests can call this concurrently.
        """
        cost = self.calculate_cost(model, input_tokens, output_tokens)
        record = UsageRecord(
            timestamp=datetime.now(timezone.utc).isoformat(),
            username=username,
            tier=tier,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
        )

        with self._lock:
            self._records.append(record)
            self._apply_to_summary(record)
            with self._ledger_path.open("a") as f:
                f.write(json.dumps(asdict(record)) + "\n")

        return record

    def get_summary(self) -> CostSummary:
        with self._lock:
            # Return a shallow copy so callers can't mutate internal state.
            return CostSummary(
                total_cost_usd=round(self._summary.total_cost_usd, 4),
                total_input_tokens=self._summary.total_input_tokens,
                total_output_tokens=self._summary.total_output_tokens,
                total_requests=self._summary.total_requests,
                cost_by_user=dict(self._summary.cost_by_user),
                cost_by_model=dict(self._summary.cost_by_model),
            )

    def get_recent_records(self, limit: int = 50) -> List[UsageRecord]:
        with self._lock:
            return self._records[-limit:][::-1]


cost_tracker = CostTracker(ledger_path=settings.COST_LOG_PATH)
