"""Learning entry point: construct the app, trace a request, then follow its service."""

import logging
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from backendfastapi.agents.chat import build_chat_workflow
from backendfastapi.api.v1.endpoints import health
from backendfastapi.api.v1.router import router as v1_router
from backendfastapi.core.config import get_settings
from backendfastapi.core.logging import configure_logging
from backendfastapi.core.middleware import (
    RequestBodyLimitMiddleware,
    RequestContextMiddleware,
)
from backendfastapi.core.observability import configure_metrics
from backendfastapi.core.tracing import RequestTracingMiddleware, configure_tracing
from backendfastapi.db.session import SessionFactory, engine


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    # Learn: startup/shutdown run once per process, not once per request.
    settings = get_settings()
    configure_logging(settings.log_level)
    application.state.chat_workflow = None
    application.state.embedding_client = None
    async with AsyncExitStack() as resources:
        # Registered cleanup runs even if startup or a later resource fails.
        resources.push_async_callback(engine.dispose)
        provider = configure_tracing(settings)
        if provider:
            from starlette.concurrency import run_in_threadpool

            resources.push_async_callback(run_in_threadpool, provider.shutdown)
        http_client = None
        if settings.openai_api_key or (
            settings.chat_enabled and not settings.auth_enabled
        ):
            http_client = await resources.enter_async_context(
                httpx.AsyncClient(
                    timeout=settings.chat_timeout_seconds,
                    limits=httpx.Limits(
                        max_connections=100, max_keepalive_connections=20
                    ),
                )
            )
        if settings.openai_api_key and http_client is not None:
            from langchain_openai import OpenAIEmbeddings

            application.state.embedding_client = OpenAIEmbeddings(
                api_key=settings.openai_api_key,
                model=settings.openai_embedding_model,
                dimensions=1536,
                http_async_client=http_client,
            )
        if settings.chat_enabled and not settings.auth_enabled:
            from langchain_openai import ChatOpenAI

            model = ChatOpenAI(
                api_key=settings.chat_api_key,
                base_url=settings.chat_base_url,
                model=settings.chat_model,
                timeout=settings.chat_timeout_seconds,
                max_retries=1,
                max_tokens=settings.chat_max_output_tokens,
                http_async_client=http_client,
            )
            application.state.chat_workflow = build_chat_workflow(
                model, metrics=application.state.agent_metrics
            )
        try:
            yield
        finally:
            application.state.chat_workflow = None


def create_app() -> FastAPI:
    """Build an isolated app; dependencies and tests can supply external resources."""
    settings = get_settings()
    application = FastAPI(
        title="Support Agent API",
        version="0.1.0",
        description="Support tickets and persistent LangGraph conversations.",
        lifespan=lifespan,
        docs_url=None if settings.environment == "production" else "/docs",
        redoc_url=None,
        openapi_url=None if settings.environment == "production" else "/openapi.json",
    )
    application.state.session_factory = SessionFactory
    application.add_middleware(
        RequestBodyLimitMiddleware, max_body_bytes=settings.max_request_body_bytes
    )
    application.add_middleware(
        TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=[
            "Content-Type",
            "Authorization",
            "X-API-Key",
            "X-Request-ID",
            "Idempotency-Key",
            "Last-Event-ID",
            "traceparent",
            "tracestate",
        ],
        expose_headers=["X-Request-ID", "X-Trace-ID"],
    )

    @application.exception_handler(Exception)
    async def unhandled_error(request: Request, exc: Exception) -> JSONResponse:
        request_id = getattr(request.state, "request_id", "unknown")
        logging.getLogger(__name__).error(
            "unhandled_exception type=%s",
            type(exc).__name__,
            extra={"request_id": request_id},
        )
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error", "request_id": request_id},
            headers={"X-Request-ID": request_id},
        )

    application.include_router(health.router)
    application.include_router(v1_router, prefix="/api/v1")
    configure_metrics(application)
    # Learn: Starlette wraps user middleware in reverse registration order.
    # Tracing is registered last so request logging executes inside its active span.
    application.add_middleware(RequestContextMiddleware)
    application.add_middleware(RequestTracingMiddleware)
    return application


app = create_app()
