import asyncio
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from time import perf_counter

from fastapi import Depends, FastAPI, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    GCCollector,
    Histogram,
    PlatformCollector,
    ProcessCollector,
    generate_latest,
)
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from backendfastapi.core.middleware import get_route_template
from backendfastapi.core.security import require_api_key


class AgentMetrics:
    def __init__(self, registry: CollectorRegistry) -> None:
        self.generations = Counter(
            "agent_generations_total",
            "Model generations by outcome",
            ["outcome"],
            registry=registry,
        )
        self.duration = Histogram(
            "agent_generation_duration_seconds",
            "Model generation latency",
            buckets=(0.1, 0.5, 1, 5, 15, 30, 60, 120),
            registry=registry,
        )
        self.tokens = Counter(
            "agent_tokens_total",
            "Provider-reported token usage",
            ["direction"],
            registry=registry,
        )

    @contextmanager
    def track_generation(self) -> Iterator[None]:
        started = perf_counter()
        outcome = "success"
        try:
            yield
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except Exception:
            outcome = "error"
            raise
        finally:
            self.generations.labels(outcome=outcome).inc()
            self.duration.observe(perf_counter() - started)

    def record_usage(self, usage: Mapping[str, int] | None) -> None:
        if usage is None:
            return
        for direction in ("input", "output"):
            count = usage.get(f"{direction}_tokens")
            if isinstance(count, int) and count >= 0:
                self.tokens.labels(direction=direction).inc(count)


class HttpMetricsMiddleware:
    def __init__(self, app: ASGIApp, registry: CollectorRegistry) -> None:
        self.app = app
        labels = ("method", "handler", "status")
        self.requests = Counter(
            "http_requests_total", "Completed HTTP requests", labels, registry=registry
        )
        self.duration = Histogram(
            "http_request_duration_seconds",
            "HTTP request duration",
            labels,
            buckets=(0.01, 0.05, 0.1, 0.5, 1, 5, 15, 30, 60, 120),
            registry=registry,
        )
        self.active = Gauge(
            "http_requests_inprogress", "Active HTTP requests", registry=registry
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in {
            "/metrics",
            "/health/live",
            "/health/ready",
        }:
            await self.app(scope, receive, send)
            return
        status_code = 500
        started = perf_counter()

        async def observe_response(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        with self.active.track_inprogress():
            try:
                await self.app(scope, receive, observe_response)
            finally:
                # Routing has completed: use templates, never raw user-controlled URLs.
                handler = get_route_template(scope)
                if handler is not None:
                    method = scope["method"]
                    if method not in {
                        "GET",
                        "POST",
                        "PUT",
                        "PATCH",
                        "DELETE",
                        "HEAD",
                        "OPTIONS",
                    }:
                        method = "OTHER"
                    labels = (method, handler, str(status_code))
                    self.requests.labels(*labels).inc()
                    self.duration.labels(*labels).observe(perf_counter() - started)


def configure_metrics(application: FastAPI) -> None:
    """Per-app collectors avoid collisions in tests and app-factory deployments."""
    registry = CollectorRegistry()
    GCCollector(registry=registry)
    PlatformCollector(registry=registry)
    ProcessCollector(registry=registry)
    application.state.metrics_registry = registry
    application.state.agent_metrics = AgentMetrics(registry)
    application.add_middleware(HttpMetricsMiddleware, registry=registry)

    @application.get(
        "/metrics",
        include_in_schema=False,
        dependencies=[Depends(require_api_key)],
    )
    def metrics() -> Response:
        return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)
