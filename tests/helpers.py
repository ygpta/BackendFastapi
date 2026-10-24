"""Shared HTTP setup without importing one test module from another."""


def register(client, name="Tenant"):
    response = client.post(
        "/api/v1/auth/register",
        json={
            "tenant_name": name,
            "email": "admin@example.com",
            "password": "strong-password-123",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def headers(tokens):
    return {"Authorization": f"Bearer {tokens['access_token']}"}
