"""Destructive integration checks: TEST_DATABASE_URL must be disposable."""

import asyncio
import os
import subprocess
import sys
from uuid import uuid4

import pytest
from fastapi import HTTPException
from langchain_core.messages import AIMessageChunk
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backendfastapi.core.config import Settings
from backendfastapi.core.identity import Principal
from backendfastapi.db.models import (
    AgentRun,
    ChatMessage,
    Conversation,
    Document,
    EmbeddingCache,
    PromptVersion,
    Ticket,
)
from backendfastapi.repositories.chat import (
    ConversationConflict,
    ConversationNotFound,
    delete_conversation,
    get_conversation,
    save_turn,
)
from backendfastapi.repositories.tickets import (
    TicketConflict,
    update_ticket_status_record,
)
from backendfastapi.schemas.platform import RunCreate
from backendfastapi.schemas.tickets import TicketStatusUpdate
from backendfastapi.services.controls import consume_rate, reserve_budget
from backendfastapi.services.dashboard import get_dashboard_summary
from backendfastapi.services.knowledge import retrieve
from backendfastapi.services.runs import submit_run
from backendfastapi.worker import process_one

pytestmark = pytest.mark.postgres


def test_postgres_migrations_and_concurrent_writers():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL to a disposable PostgreSQL database")
    environment = {
        **os.environ,
        "DATABASE_URL": url,
        "ENVIRONMENT": "test",
        "CHAT_ENABLED": "false",
        "API_KEY": "",
    }

    def migrate(*arguments):
        subprocess.run(
            [sys.executable, "-m", "alembic", *arguments],
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )

    migrate("upgrade", "head")
    migrate("downgrade", "0001_create_tickets")

    async def seed_legacy_ticket():
        engine = create_async_engine(url)
        try:
            async with engine.begin() as connection:
                return await connection.scalar(
                    text(
                        "INSERT INTO tickets (subject, message) "
                        "VALUES ('Legacy ticket', 'Created before status existed') "
                        "RETURNING id"
                    )
                )
        finally:
            await engine.dispose()

    ticket_id = asyncio.run(seed_legacy_ticket())
    migrate("upgrade", "head")
    migrate("check")

    async def exercise_database():
        engine = create_async_engine(url)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        conversation_id = uuid4()
        try:
            embed_calls = []

            async def embed_query(query):
                embed_calls.append(query)
                return [1.0] + [0.0] * 1535

            async with factory() as session:
                session.info["tenant_id"] = "semantic-test"
                session.add(
                    Document(
                        id="semantic-fusion-document",
                        tenant_id="semantic-test",
                        title="Semantic document",
                        content="a distinct lexical phrase",
                        embedding=[1.0] + [0.0] * 1535,
                        embedding_model="text-embedding-3-small",
                    ),
                    Document(
                        id="lexical-fusion-document",
                        tenant_id="semantic-test",
                        title="Lexical document",
                        content="semantic query lexical match",
                    ),
                )
                await session.commit()
                found = await retrieve(
                    session, "semantic query", limit=3, embedding_provider=embed_query
                )
                await session.commit()
                repeated = await retrieve(
                    session, "semantic query", limit=3, embedding_provider=embed_query
                )
                await session.commit()
                assert {item.title for item in found} == {
                    "Semantic document",
                    "Lexical document",
                }
                assert {item.id for item in repeated} == {item.id for item in found}
                assert embed_calls == ["semantic query"]
                assert await session.scalar(select(EmbeddingCache.id)) is not None

            async with factory() as session:
                await save_turn(session, conversation_id, "owner", None, "Hello", "Hi")

            async def competing_turn(question):
                async with factory() as session:
                    try:
                        await save_turn(
                            session, conversation_id, "owner", 1, question, "answer"
                        )
                    except ConversationConflict:
                        return "conflict"
                    return "saved"

            results = await asyncio.gather(
                competing_turn("First"), competing_turn("Second")
            )
            assert sorted(results) == ["conflict", "saved"]
            async with factory() as session:
                with pytest.raises(ConversationNotFound):
                    await get_conversation(session, conversation_id, "someone-else")
                await session.rollback()
                conversation = await get_conversation(session, conversation_id, "owner")
                assert conversation.version == 2
                count_query = (
                    select(func.count())
                    .select_from(ChatMessage)
                    .where(ChatMessage.conversation_id == conversation_id)
                )
                assert await session.scalar(count_query) == 4
                assert await delete_conversation(session, conversation_id, "owner")
                assert await session.scalar(count_query) == 0
                assert await session.get(Conversation, conversation_id) is None

            async with factory() as session:
                legacy = await session.get(Ticket, ticket_id)
                assert legacy.status == "open"
                assert legacy.version == 1
                summary = await get_dashboard_summary(session, "owner", False)
                assert summary.tickets.total >= 1
                assert summary.tickets.open >= 1
                assert summary.conversations == 0
                assert summary.messages == 0

            async def competing_status(status):
                async with factory() as session:
                    try:
                        await update_ticket_status_record(
                            session,
                            ticket_id,
                            TicketStatusUpdate(status=status, expected_version=1),
                        )
                    except TicketConflict:
                        return "conflict"
                    return "saved"

            results = await asyncio.gather(
                competing_status("in_progress"), competing_status("resolved")
            )
            assert sorted(results) == ["conflict", "saved"]
            async with factory() as session:
                legacy = await session.get(Ticket, ticket_id)
                assert legacy.version == 2
                await session.delete(legacy)
                await session.commit()

            limit_identity = uuid4().hex

            async def limited_request():
                async with factory() as session:
                    try:
                        await consume_rate(session, limit_identity, 1)
                    except HTTPException as error:
                        return error.status_code
                    return 200

            assert sorted(
                await asyncio.gather(limited_request(), limited_request())
            ) == [200, 429]
            quota_tenant = uuid4().hex
            quota_settings = Settings(_env_file=None, monthly_budget_microusd=100)

            async def reserve():
                async with factory() as session:
                    try:
                        await reserve_budget(session, quota_tenant, 60, quota_settings)
                        await session.commit()
                    except HTTPException as error:
                        return error.status_code
                    return 200

            assert sorted(await asyncio.gather(reserve(), reserve())) == [200, 429]
            tenant = uuid4().hex
            async with factory() as session:
                session.info.update(tenant_id=tenant, actor_id="legacy-service")
                document = Document(
                    id=uuid4().hex, title="Login", content="login reset instructions"
                )
                session.add(document)
                await session.commit()
                assert len(await retrieve(session, "login")) == 1
            async with factory() as session:
                session.info["tenant_id"] = "unrelated-tenant"
                assert await retrieve(session, "login") == []
            settings = Settings(
                _env_file=None, chat_enabled=True, openai_api_key="test-only"
            )
            async with factory() as session:
                session.info.update(tenant_id=tenant, actor_id="legacy-service")
                run = await submit_run(
                    session,
                    RunCreate(message="login"),
                    Principal("legacy-service", tenant, "admin"),
                    uuid4().hex,
                    settings,
                )
                run_id = run.id

            class Model:
                async def astream(self, messages):
                    yield AIMessageChunk(
                        content="Follow the login reset instructions.",
                        usage_metadata={
                            "input_tokens": 40,
                            "output_tokens": 8,
                            "total_tokens": 48,
                        },
                    )

            async with AsyncPostgresSaver.from_conn_string(
                url.replace("postgresql+asyncpg", "postgresql")
            ) as saver:
                await saver.setup()
                claimed = await asyncio.gather(
                    process_one(factory, saver, settings, lambda _: Model()),
                    process_one(factory, saver, settings, lambda _: Model()),
                )
                assert sorted(claimed) == [False, True]
                async with factory() as session:
                    result = await session.get(AgentRun, run_id)
                    assert result.status == "succeeded", result.error_code
                    assert result.output["citations"][0]["id"] == document.id
                checkpoint = await saver.aget_tuple(
                    {"configurable": {"thread_id": f"{tenant}:{run_id}"}}
                )
                assert checkpoint is not None
                await saver.adelete_thread(f"{tenant}:{run_id}")
            async with factory() as session:
                with pytest.raises(DBAPIError):
                    await session.execute(text("DELETE FROM audit_events"))
                await session.rollback()
                prompt = PromptVersion(
                    id=uuid4().hex,
                    tenant_id=tenant,
                    name="v1",
                    content="Immutable",
                    active=False,
                )
                session.add(prompt)
                await session.commit()
                with pytest.raises(DBAPIError):
                    await session.execute(
                        text("UPDATE prompt_versions SET content = 'changed'")
                    )
                await session.rollback()
        finally:
            await engine.dispose()

    asyncio.run(exercise_database())
    migrate("check")
