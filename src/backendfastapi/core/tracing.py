"""Learn: connect one HTTP request to a distributed trace without recording secrets.

Start here with tests/test_tracing.py, then follow trace_context into the worker.
A trace connects operations; logs record events; metrics aggregate measurements.
"""

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Status, StatusCode
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from backendfastapi.core.middleware import get_route_template


def configure_tracing(settings, service_name="backendfastapi"):
    # Learn: the provider owns export resources; the app lifespan shuts it down.
    if not settings.otel_enabled:
        return None
    provider = TracerProvider(
        resource=Resource.create(
            {
                "service.name": service_name,
                "deployment.environment.name": settings.environment,
            }
        )
    )
    provider.add_span_processor(
        # Export batches off the request path instead of sending each span inline.
        BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.otel_endpoint, timeout=5))
    )
    trace.set_tracer_provider(provider)
    return provider


class RequestTracingMiddleware:
    """Wrap ASGI receive/send rather than changing individual endpoint functions."""

    def __init__(self, app: ASGIApp, tracer=None):
        self.app = app
        self.tracer = tracer

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        tracer = self.tracer or trace.get_tracer("backendfastapi.http")
        headers = {
            name.decode("latin-1"): value.decode("latin-1")
            for name, value in scope["headers"]
        }
        # Learn: traceparent links this server span to an upstream caller's trace.
        # Trace context is correlation data, never proof of identity or permission.
        context = TraceContextTextMapPropagator().extract(headers)
        with tracer.start_as_current_span(
            "HTTP request",
            context=context,
            kind=trace.SpanKind.SERVER,
            # Automatic exception details could contain provider or database secrets.
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            status_code = 500

            async def traced_send(message: Message):
                nonlocal status_code
                if message["type"] == "http.response.start":
                    status_code = message["status"]
                    if span.get_span_context().is_valid:
                        message["headers"] = list(message.get("headers", [])) + [
                            (
                                b"x-trace-id",
                                format(
                                    span.get_span_context().trace_id, "032x"
                                ).encode(),
                            )
                        ]
                await send(message)

            try:
                await self.app(scope, receive, traced_send)
            finally:
                # Learn: routing is known only after the app runs. A template such as
                # /tickets/{ticket_id} avoids exporting a customer's actual ID.
                route = get_route_template(scope) or "unmatched"
                method = (
                    scope["method"]
                    if scope["method"]
                    in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
                    else "OTHER"
                )
                span.update_name(f"{method} {route}")
                span.set_attributes(
                    {
                        "http.request.method": method,
                        "http.route": route,
                        "http.response.status_code": status_code,
                    }
                )
                if status_code >= 500:
                    span.set_status(Status(StatusCode.ERROR))
