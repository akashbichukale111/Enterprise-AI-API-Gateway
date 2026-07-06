"""
main
=====

FastAPI application entry point for Akash AI Pro - Secure Enterprise API
Gateway.

Wires together:
    - CORS middleware
    - Router registration
    - Custom exception handlers (uniform JSON error envelope)
    - Startup/shutdown lifecycle hooks (RSA keypair bootstrap, Redis pool)
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError

from src.api.routes import router as api_router
from src.core.config import get_settings
from src.core.logger import logger
from src.core.security import ensure_keys_exist
from src.middleware.rate_limiter import RedisConnectionManager
from src.models.schemas import ErrorResponse

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application lifespan handler.

    ARCHITECTURAL DECISION: We use the modern `lifespan` context manager
    (rather than the deprecated `@app.on_event("startup")`) because it
    correctly guarantees shutdown cleanup even when startup raises, and is
    the pattern FastAPI/Starlette now recommend going forward.
    """
    # --- Startup ---------------------------------------------------------
    ensure_keys_exist()  # Bootstrap RSA keypair on first boot if absent.
    logger.info(
        "gateway_startup",
        extra={
            "event_type": "STARTUP",
            "detail": f"{settings.APP_NAME} v{settings.APP_VERSION} starting up "
            f"in '{settings.ENVIRONMENT}' mode.",
        },
    )
    yield
    # --- Shutdown ----------------------------------------------------------
    await RedisConnectionManager.close()
    logger.info(
        "gateway_shutdown",
        extra={"event_type": "SHUTDOWN", "detail": "Gateway shutting down gracefully."},
    )


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description=(
        "A centralized, secure AI API Gateway providing JWT auth, tiered "
        "Redis rate limiting, DDoS protection, circuit-breaker resilience, "
        "and FinOps AI token cost tracking."
    ),
    lifespan=lifespan,
)

# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------
# ARCHITECTURAL DECISION: CORS origins are externalized to Settings
# (CORS_ORIGINS) rather than hardcoded, so a production deployment can lock
# this down to specific origin(s) via environment variable without a code
# change or redeploy of a new image.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Router registration
# ---------------------------------------------------------------------------
app.include_router(api_router)


# ---------------------------------------------------------------------------
# Custom exception handlers -- uniform JSON error envelope
# ---------------------------------------------------------------------------
# ARCHITECTURAL DECISION: Every error response (401, 429, 422, 500) returns
# the SAME JSON shape (`ErrorResponse`). This is a small but high-leverage
# API design decision: client SDKs can write one error-handling code path
# instead of branching on error shape per status code.
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    error_names = {
        status.HTTP_401_UNAUTHORIZED: "Unauthorized",
        status.HTTP_403_FORBIDDEN: "Forbidden",
        status.HTTP_404_NOT_FOUND: "NotFound",
        status.HTTP_429_TOO_MANY_REQUESTS: "TooManyRequests",
    }
    body = ErrorResponse(
        error=error_names.get(exc.status_code, "HTTPException"),
        detail=str(exc.detail),
        status_code=exc.status_code,
        path=str(request.url.path),
    )
    return JSONResponse(
        status_code=exc.status_code,
        content=body.model_dump(),
        headers=getattr(exc, "headers", None),
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    body = ErrorResponse(
        error="ValidationError",
        detail="; ".join(
            f"{'.'.join(str(loc) for loc in e['loc'])}: {e['msg']}"
            for e in exc.errors()
        ),
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        path=str(request.url.path),
    )
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, content=body.model_dump()
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """
    Last-resort safety net: NEVER leak a raw stack trace to a client. Log
    the full exception server-side (with stack trace, for engineers) but
    return a sanitized generic message to the caller.
    """
    logger.error(
        "unhandled_exception",
        exc_info=exc,
        extra={"event_type": "UNHANDLED_EXCEPTION", "path": str(request.url.path)},
    )
    body = ErrorResponse(
        error="InternalServerError",
        detail="An unexpected error occurred. The incident has been logged.",
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        path=str(request.url.path),
    )
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, content=body.model_dump()
    )


@app.get("/", include_in_schema=False)
async def root() -> dict:
    return {
        "service": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "docs": "/docs",
        "status": "online",
    }
