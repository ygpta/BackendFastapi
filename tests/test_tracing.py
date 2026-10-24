import asyncio

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from starlette.responses import JSONResponse

from backendfastapi.core.tracing import RequestTracingMiddleware


def test_traces_never_capture_bodies_keys_or_raw_urls():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    async def exercise():
        sent = []

        async def downstream(scope, receive, send):
            await JSONResponse({"ok": True})(scope, receive, send)

        async def receive():
            return {"type": "http.request", "body": b"private-body"}

        async def send(message):
            sent.append(message)

        middleware = RequestTracingMiddleware(downstream, provider.get_tracer("test"))
        await middleware(
            {
                "type": "http",
                "method": "GET",
                "path": "/secret-user-id",
                "headers": [(b"authorization", b"Bearer private-token")],
            },
            receive,
            send,
        )
        assert any(name == b"x-trace-id" for name, value in sent[0]["headers"])

    asyncio.run(exercise())
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].attributes["http.route"] == "unmatched"
    assert "private" not in str(spans[0].attributes)
    provider.shutdown()
