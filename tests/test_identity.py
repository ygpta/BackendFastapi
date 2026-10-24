import pytest

from backendfastapi.core.config import Settings, get_settings
from backendfastapi.main import app
from tests.helpers import headers, register


@pytest.fixture
def identity_client(client):
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None,
        auth_enabled=True,
        registration_enabled=True,
        jwt_secret="x" * 64,
    )
    yield client
    app.dependency_overrides.pop(get_settings, None)


def test_login_refresh_logout(identity_client):
    tokens = register(identity_client)
    login = identity_client.post(
        "/api/v1/auth/login",
        json={
            "tenant_id": tokens["tenant_id"],
            "email": "admin@example.com",
            "password": "strong-password-123",
        },
    )
    assert login.status_code == 200
    assert (
        identity_client.get("/api/v1/auth/me", headers=headers(tokens)).json()["role"]
        == "admin"
    )
    refreshed = identity_client.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert refreshed.status_code == 200
    assert (
        identity_client.post(
            "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
        ).status_code
        == 401
    )
    assert (
        identity_client.post("/api/v1/auth/logout", headers=headers(tokens)).status_code
        == 204
    )
    assert (
        identity_client.get("/api/v1/auth/me", headers=headers(tokens)).status_code
        == 401
    )


def test_tenant_isolation_and_roles(identity_client):
    first = register(identity_client, "First")
    second = register(identity_client, "Second")
    created = identity_client.post(
        "/api/v1/tickets",
        headers=headers(first),
        json={
            "subject": "Tenant private",
            "message": "This belongs only to the first tenant.",
        },
    )
    assert created.status_code == 201, created.text
    ticket_id = created.json()["id"]
    assert (
        identity_client.get(
            f"/api/v1/tickets/{ticket_id}", headers=headers(second)
        ).status_code
        == 404
    )
    assert (
        identity_client.patch(
            f"/api/v1/tickets/{ticket_id}",
            headers=headers(second),
            json={"status": "closed", "expected_version": 1},
        ).status_code
        == 404
    )
    assert (
        identity_client.get("/api/v1/tickets", headers=headers(second)).json()["items"]
        == []
    )
    assert (
        identity_client.get(
            "/api/v1/dashboard/summary", headers=headers(second)
        ).json()["tickets"]["total"]
        == 0
    )
    viewer = identity_client.post(
        "/api/v1/auth/users",
        headers=headers(first),
        json={
            "email": "viewer@example.com",
            "password": "strong-password-123",
            "role": "viewer",
        },
    )
    assert viewer.status_code == 201
    login = identity_client.post(
        "/api/v1/auth/login",
        json={
            "tenant_id": first["tenant_id"],
            "email": "viewer@example.com",
            "password": "strong-password-123",
        },
    ).json()
    assert (
        identity_client.get("/api/v1/tickets", headers=headers(login)).status_code
        == 200
    )
    assert (
        identity_client.post(
            "/api/v1/tickets",
            headers=headers(login),
            json={"subject": "Denied", "message": "Viewers cannot create tickets."},
        ).status_code
        == 403
    )
    assert (
        identity_client.post(
            "/api/v1/auth/users",
            headers=headers(login),
            json={"email": "other@example.com", "password": "strong-password-123"},
        ).status_code
        == 403
    )
