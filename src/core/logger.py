"""
core.logger
===========

Cloud-ready, structured JSON logging.

ARCHITECTURAL DECISION:
    Every log line is emitted as a single-line JSON object (JSONL /
    "ndjson" format) rather than free-text. This is a deliberate design
    choice for cloud-native observability:

        - AWS CloudWatch Logs Insights, Datadog, Splunk, and the ELK stack
          all natively parse JSON log lines and let you query on structured
          fields (e.g. `event_type = "RATE_LIMIT_BREACH"`) without brittle
          regex parsing of free text.
        - Because each line is independently valid JSON, a `FileHandler`
          writing to `logs/gateway_events.jsonl` today can be swapped for a
          `boto3` CloudWatch Logs `put_log_events` call in production with
          ZERO changes to any call site -- only this module changes.
        - Structured fields (ip, user, tier, path, status_code, latency_ms)
          become instantly aggregatable for dashboards & alerting.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, Optional

from src.core.config import get_settings

settings = get_settings()

LOGGER_NAME = "akash_gateway"


class JsonFormatter(logging.Formatter):
    """Renders every LogRecord as a single-line JSON document."""

    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # Merge any structured "extra" fields passed via logger.info(..., extra={...})
        reserved = logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys()
        for key, value in record.__dict__.items():
            if key not in reserved and key not in payload:
                payload[key] = value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str)


def _build_logger() -> logging.Logger:
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(settings.LOG_LEVEL)
    logger.propagate = False

    if logger.handlers:
        # Avoid duplicate handlers if this module is imported multiple times
        # (common under uvicorn's reloader / multiple workers).
        return logger

    formatter = JsonFormatter()

    # File handler -> simulates shipping to S3 / CloudWatch Logs. A rotating
    # handler caps disk usage so a runaway process can't fill the volume --
    # a real cloud shipper (e.g. Fluent Bit sidecar) would tail this file.
    file_handler = RotatingFileHandler(
        settings.LOG_FILE_PATH, maxBytes=5 * 1024 * 1024, backupCount=5
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    # Also stream to stdout -- required for container orchestrators
    # (ECS/EKS/Fargate) that scrape stdout for CloudWatch/Fluentd ingestion.
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    return logger


logger = _build_logger()


# ---------------------------------------------------------------------------
# Semantic logging helpers -- give call sites a clear, typed vocabulary
# instead of ad-hoc f-strings scattered through the codebase.
# ---------------------------------------------------------------------------
def log_api_usage(
    *,
    username: str,
    tier: str,
    path: str,
    status_code: int,
    latency_ms: float,
    ip: Optional[str] = None,
) -> None:
    """Log a completed, successfully-routed API call."""
    logger.info(
        "api_usage",
        extra={
            "event_type": "API_USAGE",
            "username": username,
            "tier": tier,
            "path": path,
            "status_code": status_code,
            "latency_ms": round(latency_ms, 2),
            "ip": ip,
        },
    )


def log_rate_limit_breach(
    *, identifier: str, tier: str, path: str, ip: Optional[str] = None
) -> None:
    """Log a rate-limit (429) rejection."""
    logger.warning(
        "rate_limit_breach",
        extra={
            "event_type": "RATE_LIMIT_BREACH",
            "identifier": identifier,
            "tier": tier,
            "path": path,
            "ip": ip,
        },
    )


def log_blocked_ip(
    *, ip: str, reason: str, request_count: Optional[int] = None
) -> None:
    """Log an IP being blocked by the DDoS heuristic guard."""
    logger.warning(
        "ip_blocked",
        extra={
            "event_type": "IP_BLOCKED",
            "ip": ip,
            "reason": reason,
            "request_count": request_count,
        },
    )


def log_security_event(
    *,
    event_type: str,
    detail: str,
    ip: Optional[str] = None,
    username: Optional[str] = None,
) -> None:
    """Log a generic auth/security-relevant event (invalid token, failed login, etc)."""
    logger.warning(
        detail,
        extra={
            "event_type": event_type,
            "detail": detail,
            "ip": ip,
            "username": username,
        },
    )


def log_circuit_breaker_event(*, service: str, state: str, detail: str) -> None:
    """Log a circuit breaker state transition (CLOSED -> OPEN -> HALF_OPEN)."""
    logger.warning(
        detail,
        extra={
            "event_type": "CIRCUIT_BREAKER_STATE_CHANGE",
            "service": service,
            "state": state,
            "detail": detail,
        },
    )
