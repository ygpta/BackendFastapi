import logging
import re
from time import perf_counter
from uuid import uuid4

from opentelemetry import trace
from starlette.responses import JSONResponse
from starlette.routing import NoMatchFound
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger("backendfastapi.access")
request_id_pattern = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def get_route_template(scope: Scope) -> str | None:
    # URL reversal preserves included-router prefixes without exposing parameter values.
    telemetry_route = getattr(scope.get("fastapi.telemetry"), "route", None)
    if telemetry_route:
        return telemetry_route
    route = scope.get("route")
    application = scope.get("app")
    if route is not None and application is not None:
        parameters = {
            name: f"{{{name}}}" for name in getattr(route, "param_convertors", {})
        }
        try:
            return str(application.url_path_for(route.name, **parameters))
        except (NoMatchFound, ValueError, AssertionError):
            pass
    return getattr(route, "path", None)


class RequestBodyLimitMiddleware:
    """Bound buffering before JSON parsing, including chunked/undeclared bodies."""

    def __init__(self, app: ASGIApp, max_body_bytes: int) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        content_length = dict(scope["headers"]).get(b"content-length")
        if content_length is not None:
            try:
                declared_length = int(content_length)
                if declared_length < 0:
                    raise ValueError
            except ValueError:
                await JSONResponse(
                    {"detail": "Invalid Content-Length"}, status_code=400
                )(scope, receive, send)
                return
            if declared_length > self.max_body_bytes:
                await JSONResponse(
                    {"detail": "Request body too large"}, status_code=413
                )(scope, receive, send)
                return

        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > self.max_body_bytes:
                await JSONResponse(
                    {"detail": "Request body too large"}, status_code=413
                )(scope, receive, send)
                return
            body.extend(chunk)
            if not message.get("more_body", False):
                break

        delivered = False

        async def replay_body() -> Message:
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay_body, send)


class RequestContextMiddleware:
    """Pure ASGI middleware keeps context intact for async and streaming routes."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope["headers"])
        supplied = headers.get(b"x-request-id", b"").decode("latin-1")
        request_id = supplied if request_id_pattern.fullmatch(supplied) else uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id
        started = perf_counter()
        status_code = 500

        async def send_with_context(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                message["headers"] = list(message.get("headers", [])) + [
                    (b"x-request-id", request_id.encode()),
                    (b"x-content-type-options", b"nosniff"),
                    (b"x-frame-options", b"DENY"),
                    (b"referrer-policy", b"no-referrer"),
                ]
            await send(message)

        try:
            await self.app(scope, receive, send_with_context)
        finally:
            logger.info(
                "request_completed",
                extra={
                    "request_id": request_id,
                    "trace_id": format(
                        trace.get_current_span().get_span_context().trace_id, "032x"
                    ),
                    "method": scope["method"],
                    "path": get_route_template(scope) or "unmatched",
                    "status_code": status_code,
                    "duration_ms": round((perf_counter() - started) * 1000, 2),
                },
            )
