import asyncio
from uuid import uuid4

import pytest

from backendfastapi.repositories.chat import save_turn


def test_liveness(client):
    response = client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readiness_checks_database(client):
    response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_create_and_read_ticket(client):
    payload = {
        "subject": "Login issue",
        "message": "I cannot log in today.",
        "customer_reference": "CUST-123",
    }
    create_response = client.post("/api/v1/tickets", json=payload)

    assert create_response.status_code == 201
    created_ticket = create_response.json()
    assert created_ticket["id"] > 0
    assert created_ticket["subject"] == payload["subject"]
    assert created_ticket["customer_reference"] == payload["customer_reference"]
    assert "created_at" in created_ticket

    read_response = client.get(f"/api/v1/tickets/{created_ticket['id']}")

    assert read_response.status_code == 200
    assert read_response.json() == created_ticket


def test_read_missing_ticket_returns_404(client):
    response = client.get("/api/v1/tickets/999")

    assert response.status_code == 404


def test_preview_normalizes_and_returns_valid_input(client):
    response = client.post(
        "/api/v1/tickets/preview",
        json={"subject": "  Login issue  ", "message": "  I cannot log in today.  "},
    )

    assert response.status_code == 200
    assert response.json() == {
        "subject": "Login issue",
        "message": "I cannot log in today.",
        "customer_reference": None,
        "status": "validated",
    }


def test_preview_normalizes_customer_reference(client):
    response = client.post(
        "/api/v1/tickets/preview",
        json={
            "subject": "Login issue",
            "message": "I cannot log in today.",
            "customer_reference": "  CUST-123  ",
        },
    )

    assert response.status_code == 200
    assert response.json()["customer_reference"] == "CUST-123"


@pytest.mark.parametrize(
    "payload, invalid_field",
    [
        ({"subject": "   ", "message": "I cannot log in today."}, "subject"),
        ({"subject": "Login", "message": "short"}, "message"),
        ({"subject": "Login", "message": " " * 20}, "message"),
        ({"subject": "Login"}, "message"),
        ({"subject": "x" * 121, "message": "I cannot log in."}, "subject"),
        ({"subject": "Login", "message": "x" * 4001}, "message"),
        (
            {
                "subject": "Login",
                "message": "I cannot log in today.",
                "customer_reference": "   ",
            },
            "customer_reference",
        ),
        (
            {
                "subject": "Login",
                "message": "I cannot log in today.",
                "customer_reference": "x" * 65,
            },
            "customer_reference",
        ),
        (
            {"subject": "Login", "message": "I cannot log in.", "admin": True},
            "admin",
        ),
    ],
)
def test_preview_rejects_invalid_input(client, payload, invalid_field):
    response = client.post("/api/v1/tickets/preview", json=payload)

    assert response.status_code == 422
    assert any(
        error["loc"] == ["body", invalid_field] for error in response.json()["detail"]
    )


def test_openapi_describes_the_preview_contract(client):
    response = client.get("/openapi.json")

    assert response.status_code == 200
    operation = response.json()["paths"]["/api/v1/tickets/preview"]["post"]
    assert "200" in operation["responses"]
    assert "422" in operation["responses"]


def test_ticket_list_uses_stable_cursor_pagination(client):
    ids = [
        client.post(
            "/api/v1/tickets",
            json={"subject": f"Issue {index}", "message": "I cannot log in today."},
        ).json()["id"]
        for index in range(3)
    ]
    first = client.get("/api/v1/tickets", params={"limit": 2}).json()
    assert [item["id"] for item in first["items"]] == list(reversed(ids[1:]))
    assert first["next_cursor"] == ids[1]
    second = client.get(
        "/api/v1/tickets", params={"limit": 2, "before_id": first["next_cursor"]}
    ).json()
    assert [item["id"] for item in second["items"]] == [ids[0]]
    assert second["next_cursor"] is None


def test_ticket_status_update_and_filters(client):
    created = client.post(
        "/api/v1/tickets",
        json={
            "subject": "Login 100% issue",
            "message": "I cannot log in today.",
            "customer_reference": "CUST-1",
        },
    ).json()
    assert created["status"] == "open"
    assert created["version"] == 1
    updated = client.patch(
        f"/api/v1/tickets/{created['id']}",
        json={"status": "resolved", "expected_version": 1},
    )
    assert updated.status_code == 200
    assert updated.json()["status"] == "resolved"
    assert updated.json()["version"] == 2
    assert (
        client.get("/api/v1/tickets", params={"status": "open"}).json()["items"] == []
    )
    filtered = client.get(
        "/api/v1/tickets",
        params={
            "status": "resolved",
            "search": "LOGIN",
            "customer_reference": "CUST-1",
        },
    ).json()["items"]
    assert [item["id"] for item in filtered] == [created["id"]]
    assert (
        client.get(
            "/api/v1/tickets", params={"customer_reference": "someone-else"}
        ).json()["items"]
        == []
    )


def test_ticket_status_update_rejects_stale_version(client):
    created = client.post(
        "/api/v1/tickets",
        json={"subject": "Login", "message": "I cannot log in today."},
    ).json()
    url = f"/api/v1/tickets/{created['id']}"
    assert (
        client.patch(
            url, json={"status": "in_progress", "expected_version": 1}
        ).status_code
        == 200
    )
    assert (
        client.patch(url, json={"status": "closed", "expected_version": 1}).status_code
        == 409
    )
    assert client.get(url).json()["status"] == "in_progress"
    assert (
        client.patch(
            "/api/v1/tickets/999", json={"status": "closed", "expected_version": 1}
        ).status_code
        == 404
    )


def test_ticket_search_treats_wildcards_as_literal_text(client):
    client.post(
        "/api/v1/tickets",
        json={"subject": "Login", "message": "I cannot log in today."},
    )
    assert client.get("/api/v1/tickets", params={"search": "%"}).json()["items"] == []


@pytest.mark.parametrize(
    "parameters",
    [
        {"limit": 0},
        {"limit": 101},
        {"before_id": 0},
        {"status": "invalid"},
        {"search": "   "},
    ],
)
def test_ticket_list_rejects_invalid_filters(client, parameters):
    assert client.get("/api/v1/tickets", params=parameters).status_code == 422


def test_dashboard_handles_empty_database(client):
    response = client.get("/api/v1/dashboard/summary")
    assert response.status_code == 200
    data = response.json()
    assert data["tickets"] == {
        "total": 0,
        "open": 0,
        "in_progress": 0,
        "resolved": 0,
        "closed": 0,
        "created_last_24h": 0,
    }
    assert data["conversations"] == 0
    assert data["messages"] == 0


def test_dashboard_counts_status_and_scopes_chat_to_owner(client, session_factory):
    for index in range(2):
        record = client.post(
            "/api/v1/tickets",
            json={"subject": f"Issue {index}", "message": "I cannot log in today."},
        ).json()
    client.patch(
        f"/api/v1/tickets/{record['id']}",
        json={"status": "resolved", "expected_version": 1},
    )

    async def seed_conversations():
        async with session_factory() as session:
            for owner in ("development", "someone-else"):
                await save_turn(session, uuid4(), owner, None, "Hello", "Hi")

    asyncio.run(seed_conversations())
    data = client.get("/api/v1/dashboard/summary").json()
    assert data["tickets"] == {
        "total": 2,
        "open": 1,
        "in_progress": 0,
        "resolved": 1,
        "closed": 0,
        "created_last_24h": 2,
    }
    assert data["conversations"] == 1
    assert data["messages"] == 2
    assert data["chat_enabled"] is False
    assert data["generated_at"]
