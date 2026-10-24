import asyncio
from uuid import UUID, uuid4

import pytest
from langchain_core.messages import AIMessage
from prometheus_client import CollectorRegistry, generate_latest
from sqlalchemy import func, select

from backendfastapi.agents.chat import build_chat_workflow
from backendfastapi.core.observability import AgentMetrics
from backendfastapi.db.models import ChatMessage, Conversation
from backendfastapi.main import app
from backendfastapi.repositories.chat import ConversationConflict, save_turn


class FakeChatModel:
    def __init__(self):
        self.calls = []

    async def ainvoke(self, messages):
        self.calls.append(messages)
        return AIMessage(content=f"Reply to: {messages[-1].content}")


@pytest.fixture
def chat_model(client):
    model = FakeChatModel()
    app.state.chat_workflow = build_chat_workflow(model)
    return model


def test_chat_disabled_returns_503(client):
    response = client.post("/api/v1/chat", json={"message": "Hello"})
    assert response.status_code == 503


def test_chat_persists_and_restores_history(client, chat_model):
    first = client.post("/api/v1/chat", json={"message": "  Hello  "})
    assert first.status_code == 200
    conversation_id = first.json()["conversation_id"]
    second = client.post(
        "/api/v1/chat",
        json={"conversation_id": conversation_id, "message": "What did I say?"},
    )
    assert second.status_code == 200
    assert len(chat_model.calls) == 2
    assert [message.type for message in chat_model.calls[1]] == [
        "system",
        "human",
        "ai",
        "human",
    ]
    history = client.get(f"/api/v1/chat/{conversation_id}/messages").json()["messages"]
    assert [message["role"] for message in history] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    assert history[0]["content"] == "Hello"
    page = client.get(
        f"/api/v1/chat/{conversation_id}/messages",
        params={"limit": 2, "before_id": history[2]["id"]},
    )
    assert page.json()["messages"] == history[:2]


@pytest.mark.parametrize("message", ["", "   ", "x" * 4001])
def test_chat_validates_message(client, chat_model, message):
    assert client.post("/api/v1/chat", json={"message": message}).status_code == 422
    assert chat_model.calls == []


def test_unknown_conversation_returns_404(client, chat_model):
    conversation_id = str(uuid4())
    assert (
        client.post(
            "/api/v1/chat",
            json={"conversation_id": conversation_id, "message": "Hello"},
        ).status_code
        == 404
    )
    assert client.get(f"/api/v1/chat/{conversation_id}/messages").status_code == 404
    assert chat_model.calls == []


def test_provider_error_does_not_persist_a_partial_turn(client, session_factory):
    class EmptyModel:
        async def ainvoke(self, messages):
            return AIMessage(content="")

    app.state.chat_workflow = build_chat_workflow(EmptyModel())
    response = client.post("/api/v1/chat", json={"message": "Hello"})
    assert response.status_code == 502

    async def count_conversations():
        async with session_factory() as session:
            return await session.scalar(select(func.count()).select_from(Conversation))

    assert asyncio.run(count_conversations()) == 0


def test_provider_timeout_returns_504(client):
    class TimedOutModel:
        async def ainvoke(self, messages):
            raise TimeoutError("sensitive provider details")

    app.state.chat_workflow = build_chat_workflow(TimedOutModel())
    response = client.post("/api/v1/chat", json={"message": "Hello"})
    assert response.status_code == 504
    assert "sensitive" not in response.text


def test_delete_cascades_messages(client, chat_model, session_factory):
    conversation_id = client.post("/api/v1/chat", json={"message": "Hello"}).json()[
        "conversation_id"
    ]
    assert client.delete(f"/api/v1/chat/{conversation_id}").status_code == 204
    assert client.get(f"/api/v1/chat/{conversation_id}/messages").status_code == 404

    async def count_messages():
        async with session_factory() as session:
            return await session.scalar(select(func.count()).select_from(ChatMessage))

    assert asyncio.run(count_messages()) == 0


def test_stale_turn_is_rejected_atomically(client, chat_model, session_factory):
    conversation_id = UUID(
        client.post("/api/v1/chat", json={"message": "Hello"}).json()["conversation_id"]
    )

    async def stale_update():
        async with session_factory() as session:
            with pytest.raises(ConversationConflict):
                await save_turn(
                    session, conversation_id, "development", 0, "stale", "reply"
                )
            return await session.scalar(select(func.count()).select_from(ChatMessage))

    assert asyncio.run(stale_update()) == 2


def test_agent_metrics_report_usage_without_recording_messages():
    class UsageModel:
        async def ainvoke(self, messages):
            return AIMessage(
                content="private-reply",
                usage_metadata={
                    "input_tokens": 12,
                    "output_tokens": 3,
                    "total_tokens": 15,
                },
            )

    registry = CollectorRegistry()
    graph = build_chat_workflow(UsageModel(), metrics=AgentMetrics(registry))
    asyncio.run(graph.ainvoke({"messages": [("user", "private-prompt")]}))
    assert (
        registry.get_sample_value("agent_generations_total", {"outcome": "success"})
        == 1
    )
    assert registry.get_sample_value("agent_tokens_total", {"direction": "input"}) == 12
    assert registry.get_sample_value("agent_tokens_total", {"direction": "output"}) == 3
    output = generate_latest(registry).decode()
    assert "private-prompt" not in output
    assert "private-reply" not in output


@pytest.mark.parametrize(
    "exception, outcome", [(ValueError, "error"), (asyncio.CancelledError, "cancelled")]
)
def test_agent_metrics_count_errors_and_cancellations(exception, outcome):
    registry = CollectorRegistry()
    metrics = AgentMetrics(registry)
    with pytest.raises(exception):
        with metrics.track_generation():
            raise exception()
    assert (
        registry.get_sample_value("agent_generations_total", {"outcome": outcome}) == 1
    )
    assert registry.get_sample_value("agent_generation_duration_seconds_count") == 1
