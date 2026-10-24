import asyncio
import json
import logging

import pytest
from fastapi.testclient import TestClient
from starlette.responses import JSONResponse

from backendfastapi.core.config import Settings, get_settings
from backendfastapi.core.logging import JsonFormatter
from backendfastapi.core.middleware import RequestBodyLimitMiddleware
from backendfastapi.main import app, create_app


def test_api_key_protects_versioned_routes():
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None, api_key="a" * 32
    )
    try:
        with TestClient(app) as client:
            payload = {"subject": "Login", "message": "I cannot log in today."}
            assert (
                client.post("/api/v1/tickets/preview", json=payload).status_code == 401
            )
            assert (
                client.post(
                    "/api/v1/tickets/preview",
                    json=payload,
                    headers={"X-API-Key": "incorrect"},
                ).status_code
                == 401
            )
            assert (
                client.post(
                    "/api/v1/tickets/preview",
                    json=payload,
                    headers={"X-API-Key": "a" * 32},
                ).status_code
                == 200
            )
            assert client.get("/health/live").status_code == 200
            assert client.get("/api/v1/tickets").status_code == 401
            assert client.get("/api/v1/dashboard/summary").status_code == 401
            assert (
                client.patch(
                    "/api/v1/tickets/1",
                    json={"status": "closed", "expected_version": 1},
                ).status_code
                == 401
            )
    finally:
        app.dependency_overrides.pop(get_settings, None)


def test_request_id_and_security_headers():
    with TestClient(app) as client:
        response = client.get("/health/live", headers={"X-Request-ID": "trace-123"})
        assert response.headers["X-Request-ID"] == "trace-123"
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        response = client.get("/health/live", headers={"X-Request-ID": "bad id!"})
        assert len(response.headers["X-Request-ID"]) == 32


def test_rejects_unknown_host():
    with TestClient(app) as client:
        assert (
            client.get("/health/live", headers={"Host": "evil.example"}).status_code
            == 400
        )


def test_production_hides_documentation(monkeypatch):
    settings = Settings(
        _env_file=None,
        environment="production",
        auth_enabled=True,
        jwt_secret="x" * 64,
        api_key="a" * 32,
        database_url="postgresql+asyncpg://app:strong-password@db/app",
    )
    monkeypatch.setattr("backendfastapi.main.get_settings", lambda: settings)
    with TestClient(create_app()) as client:
        assert client.get("/docs").status_code == 404
        assert client.get("/openapi.json").status_code == 404
        assert client.get("/health/live").status_code == 200


def test_unhandled_errors_hide_sensitive_details():
    application = create_app()

    @application.get("/test-failure")
    async def fail():
        raise RuntimeError("private-provider-data")

    with TestClient(application, raise_server_exceptions=False) as client:
        response = client.get("/test-failure", headers={"X-Request-ID": "failure-123"})
        assert response.status_code == 500
        assert response.json() == {
            "detail": "Internal server error",
            "request_id": "failure-123",
        }
        assert response.headers["X-Request-ID"] == "failure-123"
        assert "private-provider-data" not in response.text


def test_json_formatter_does_not_emit_exception_details():
    exception = RuntimeError("sensitive-sql-or-provider-data")
    record = logging.LogRecord(
        "uvicorn.error",
        logging.ERROR,
        __file__,
        1,
        "Exception in ASGI application",
        (),
        (RuntimeError, exception, None),
    )
    formatted = JsonFormatter().format(record)
    assert json.loads(formatted)["level"] == "ERROR"
    assert "sensitive-sql-or-provider-data" not in formatted


def test_chat_client_initializes_and_closes_without_network(monkeypatch):
    settings = Settings(
        _env_file=None, chat_enabled=True, openai_api_key="test-not-a-real-key"
    )
    monkeypatch.setattr("backendfastapi.main.get_settings", lambda: settings)
    application = create_app()
    with TestClient(application):
        assert application.state.chat_workflow is not None
    assert application.state.chat_workflow is None


def test_metrics_are_authenticated_and_do_not_leak_paths():
    application = create_app()
    application.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None, api_key="a" * 32
    )
    with TestClient(application) as client:
        assert client.get("/metrics").status_code == 401
        client.post(
            "/api/v1/tickets/preview",
            json={"subject": "Login", "message": "I cannot log in today."},
            headers={"X-API-Key": "a" * 32},
        )
        client.get("/unknown/secret-customer-id")
        client.get("/api/v1/tickets/987654")
        response = client.get("/metrics", headers={"X-API-Key": "a" * 32})
        assert response.status_code == 200
        assert "http_requests_total" in response.text
        assert "http_request_duration_seconds" in response.text
        assert "process_resident_memory_bytes" in response.text
        assert 'handler="/api/v1/tickets/preview"' in response.text
        assert 'handler="/api/v1/tickets/{ticket_id}"' in response.text
        assert "987654" not in response.text
        assert "secret-customer-id" not in response.text
        assert "a" * 32 not in response.text


def test_request_size_limit_returns_413_with_request_id(monkeypatch):
    settings = Settings(_env_file=None, max_request_body_bytes=1024)
    monkeypatch.setattr("backendfastapi.main.get_settings", lambda: settings)
    with TestClient(create_app()) as client:
        response = client.post(
            "/api/v1/tickets/preview",
            json={"subject": "Login", "message": "x" * 2000},
            headers={"X-Request-ID": "large-request"},
        )
        assert response.status_code == 413
        assert response.headers["X-Request-ID"] == "large-request"
        assert (
            client.post(
                "/api/v1/tickets/preview",
                json={"subject": "Login", "message": "I cannot log in today."},
            ).status_code
            == 200
        )


@pytest.mark.parametrize(
    "chunks, expected_status",
    [([b"1234", b"5678"], 200), ([b"1234", b"56789"], 413)],
)
def test_chunked_body_limit_without_content_length(chunks, expected_status):
    async def exercise():
        events = iter(
            [
                {
                    "type": "http.request",
                    "body": chunk,
                    "more_body": index < len(chunks) - 1,
                }
                for index, chunk in enumerate(chunks)
            ]
        )
        sent = []

        async def receive():
            return next(events)

        async def send(message):
            sent.append(message)

        async def downstream(scope, receive, send):
            body = await receive()
            assert body["body"] == b"".join(chunks)
            await JSONResponse({"ok": True})(scope, receive, send)

        middleware = RequestBodyLimitMiddleware(downstream, max_body_bytes=8)
        await middleware(
            {"type": "http", "method": "POST", "path": "/", "headers": []},
            receive,
            send,
        )
        assert sent[0]["status"] == expected_status

    asyncio.run(exercise())
