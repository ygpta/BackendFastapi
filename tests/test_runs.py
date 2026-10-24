import asyncio

from langchain_core.messages import AIMessageChunk
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select

from backendfastapi.core.config import Settings, get_settings
from backendfastapi.db.models import Budget
from backendfastapi.main import app
from backendfastapi.worker import process_one


def test_run_idempotency_cancellation_and_budget(client, session_factory):
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None,
        chat_enabled=True,
        openai_api_key="test-only",
        monthly_budget_microusd=20000,
    )
    try:
        payload = {"message": "Help with login", "use_rag": False}
        headers = {"Idempotency-Key": "run-test-key-1"}
        first = client.post("/api/v1/runs", json=payload, headers=headers)
        assert first.status_code == 202, first.text
        run_id = first.json()["id"]
        assert first.json()["status"] == "queued"
        assert (
            client.post("/api/v1/runs", json=payload, headers=headers).json()["id"]
            == run_id
        )
        assert (
            client.post(
                "/api/v1/runs", json={"message": "different"}, headers=headers
            ).status_code
            == 409
        )
        stream = client.get(f"/api/v1/runs/{run_id}/events?follow=false")
        assert stream.status_code == 200 and "event: queued" in stream.text
        assert (
            client.post(f"/api/v1/runs/{run_id}/cancel").json()["status"] == "cancelled"
        )
        assert (
            client.post(f"/api/v1/runs/{run_id}/cancel").json()["status"] == "cancelled"
        )

        async def check():
            async with session_factory() as session:
                budget = await session.scalar(select(Budget))
                assert budget.reserved == 0 and budget.spent == 0

        asyncio.run(check())
    finally:
        app.dependency_overrides.pop(get_settings, None)


def test_worker_completes_and_checkpoints_run(client, session_factory):
    class Model:
        async def astream(self, messages):
            yield AIMessageChunk(
                content="A helpful reply.",
                usage_metadata={
                    "input_tokens": 30,
                    "output_tokens": 5,
                    "total_tokens": 35,
                },
            )

    settings = Settings(_env_file=None, chat_enabled=True, openai_api_key="test-only")
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        created = client.post(
            "/api/v1/runs",
            json={"message": "Hello", "use_rag": False},
            headers={"Idempotency-Key": "worker-run-key"},
        )
        assert created.status_code == 202, created.text
        run_id = created.json()["id"]
        saver = InMemorySaver()
        assert asyncio.run(
            process_one(session_factory, saver, settings, lambda _: Model())
        )
        result = client.get(f"/api/v1/runs/{run_id}").json()
        assert result["status"] == "succeeded", result
        assert result["output"]["text"] == "A helpful reply."
        assert result["output"]["usage"]["total_tokens"] == 35
        events = client.get(f"/api/v1/runs/{run_id}/events?follow=false").text
        assert "event: token" in events and "event: succeeded" in events
        assert not asyncio.run(
            process_one(session_factory, saver, settings, lambda _: Model())
        )
    finally:
        app.dependency_overrides.pop(get_settings, None)


def test_running_generation_can_be_cancelled(client, session_factory):
    from backendfastapi.db.models import AgentRun

    settings = Settings(_env_file=None, chat_enabled=True, openai_api_key="test-only")
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        run_id = client.post(
            "/api/v1/runs",
            json={"message": "Hello", "use_rag": False},
            headers={"Idempotency-Key": "cancel-live-run"},
        ).json()["id"]

        async def exercise():
            started = asyncio.Event()

            class SlowModel:
                async def astream(self, messages):
                    started.set()
                    await asyncio.Event().wait()
                    yield AIMessageChunk(content="never completed")

            task = asyncio.create_task(
                process_one(
                    session_factory, InMemorySaver(), settings, lambda _: SlowModel()
                )
            )
            await asyncio.wait_for(started.wait(), timeout=5)
            async with session_factory() as session:
                run = await session.get(AgentRun, run_id)
                run.cancel_requested = True
                await session.commit()
            await asyncio.wait_for(task, timeout=5)
            async with session_factory() as session:
                run = await session.get(AgentRun, run_id)
                budget = await session.get(Budget, run.budget_id)
                assert run.status == "cancelled"
                assert budget.reserved == 0
                assert budget.spent == run.reserved_cost

        asyncio.run(exercise())
    finally:
        app.dependency_overrides.pop(get_settings, None)


def test_checkpoint_recovery_does_not_regenerate_completed_answer(
    client, session_factory, monkeypatch
):
    from datetime import UTC, datetime, timedelta

    import pytest

    import backendfastapi.worker as worker
    from backendfastapi.db.models import AgentRun

    settings = Settings(_env_file=None, chat_enabled=True, openai_api_key="test-only")
    app.dependency_overrides[get_settings] = lambda: settings
    calls = []

    class Model:
        async def astream(self, messages):
            calls.append(True)
            yield AIMessageChunk(content="Checkpointed answer.")

    try:
        run_id = client.post(
            "/api/v1/runs",
            json={"message": "Hello", "use_rag": False},
            headers={"Idempotency-Key": "recover-worker-run"},
        ).json()["id"]
        original_finish = worker.finish_run

        async def crash_before_commit(*arguments):
            raise RuntimeError("simulated worker exit before final commit")

        async def exercise():
            saver = InMemorySaver()
            monkeypatch.setattr(worker, "finish_run", crash_before_commit)
            with pytest.raises(RuntimeError):
                await process_one(session_factory, saver, settings, lambda _: Model())
            async with session_factory() as session:
                run = await session.get(AgentRun, run_id)
                assert run.status == "running"
                run.lease_until = datetime.now(UTC) - timedelta(seconds=1)
                await session.commit()
            monkeypatch.setattr(worker, "finish_run", original_finish)
            assert await process_one(
                session_factory, saver, settings, lambda _: Model()
            )
            async with session_factory() as session:
                run = await session.get(AgentRun, run_id)
                assert run.status == "succeeded"
                assert run.attempts == 2
            assert len(calls) == 1

        asyncio.run(exercise())
    finally:
        app.dependency_overrides.pop(get_settings, None)
