"""Run with `python -m backendfastapi.worker`; PostgreSQL is the durable queue.

Learn in this order: claim_run -> execute_run -> finish_run -> process_one.
Pair this with the cancellation and checkpoint-recovery tests in test_runs.py.
"""

import asyncio
import contextlib
import signal
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from time import perf_counter
from uuid import UUID, uuid4

import httpx
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from opentelemetry import trace
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from prometheus_client import Counter, Histogram, start_http_server
from sqlalchemy import or_, select, update
from sqlalchemy.engine import make_url

from backendfastapi.agents.durable import build_run_graph
from backendfastapi.core.config import get_settings
from backendfastapi.core.logging import configure_logging
from backendfastapi.core.tracing import configure_tracing
from backendfastapi.db import tenancy  # noqa: F401
from backendfastapi.db.models import (
    AgentRun,
    Budget,
    ChatMessage,
    Conversation,
    Ticket,
    ToolGrant,
    ToolReceipt,
    User,
)
from backendfastapi.db.session import SessionFactory, engine
from backendfastapi.repositories.audit import audit
from backendfastapi.services.controls import settle_budget
from backendfastapi.services.knowledge import retrieve
from backendfastapi.services.runs import event


class RunStopped(Exception):
    pass


async def claim_run(factory, settings):
    """Claim one job without blocking other workers; reclaim only expired leases."""
    async with factory() as session, session.begin():
        now = datetime.now(UTC)
        run = await session.scalar(
            select(AgentRun)
            .where(
                or_(
                    AgentRun.status == "queued",
                    (AgentRun.status == "running") & (AgentRun.lease_until < now),
                )
            )
            .order_by(AgentRun.created_at, AgentRun.id)
            # Learn: another worker skips this locked row instead of claiming it too.
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if run is None:
            return None
        if settings.auth_enabled:
            permitted = await session.scalar(
                select(User.id).where(
                    User.id == run.actor_id,
                    User.tenant_id == run.tenant_id,
                    User.active.is_(True),
                    User.role.in_(["admin", "operator"]),
                )
            )
            if permitted is None:
                run.cancel_requested = True
        if run.cancel_requested or run.attempts >= settings.worker_max_attempts:
            await settle_budget(
                session,
                run.budget_id,
                run.reserved_cost,
                run.reserved_cost if run.attempts else 0,
            )
            run.status = "cancelled" if run.cancel_requested else "failed"
            run.error_code = "attempt_limit" if not run.cancel_requested else None
            event(session, run, run.status)
            return None
        if run.attempts:
            # A crashed paid call may have been billed: charge its full reservation.
            allowed = await session.scalar(
                update(Budget)
                .where(
                    Budget.id == run.budget_id,
                    Budget.spent + Budget.reserved + run.reserved_cost
                    <= Budget.allowance,
                )
                .values(spent=Budget.spent + run.reserved_cost)
                .returning(Budget.id)
            )
            if allowed is None:
                await settle_budget(
                    session, run.budget_id, run.reserved_cost, run.reserved_cost
                )
                run.status, run.error_code = "failed", "recovery_budget_exhausted"
                event(session, run, "failed", {"code": run.error_code})
                return None
        run.status = "running"
        run.attempts += 1
        run.version += 1
        # A fresh token fences the previous lease holder out of later database writes.
        run.lease_token = uuid4().hex
        run.lease_until = now + timedelta(seconds=settings.worker_lease_seconds)
        event(session, run, "running", {"attempt": run.attempts})
        return run


async def check_lease(session, run):
    """Fence stale workers out after another process has reclaimed their job."""
    current = await session.scalar(
        select(AgentRun)
        .where(
            AgentRun.id == run.id,
            AgentRun.lease_token == run.lease_token,
            AgentRun.status == "running",
        )
        .with_for_update()
    )
    if current is None or current.cancel_requested:
        raise RunStopped
    return current


async def execute_run(
    factory,
    run,
    checkpointer,
    settings,
    model_factory=None,
    embedding_provider=None,
    ttft_observer: Callable[[float], None] | None = None,
    retrieval_observer: Callable[[float], None] | None = None,
):
    """Execute checkpointed graph nodes using short-lived database transactions."""
    progress = {"called": False}

    async def publish(kind, payload):
        async with factory() as session, session.begin():
            session.info.update(tenant_id=run.tenant_id, actor_id=run.actor_id)
            await check_lease(session, run)
            if kind == "generation_started":
                progress["called"] = True
            event(session, run, kind, payload)

    async def search(query):
        started = perf_counter()
        async with factory() as session:
            session.info["tenant_id"] = run.tenant_id
            documents = await retrieve(
                session,
                query,
                embedding_provider=(
                    embedding_provider.aembed_query if embedding_provider else None
                ),
                embedding_model=settings.openai_embedding_model,
            )
            await session.commit()
            if retrieval_observer is not None:
                retrieval_observer(perf_counter() - started)
            return [
                {"id": item.id, "title": item.title, "content": item.content[:2000]}
                for item in documents
            ]

    async def tool(arguments):
        async with factory() as session, session.begin():
            session.info.update(tenant_id=run.tenant_id, actor_id=run.actor_id)
            await check_lease(session, run)
            receipt = await session.scalar(
                select(ToolReceipt).where(ToolReceipt.run_id == run.id)
            )
            if receipt:
                # A recovered graph must not repeat an already committed tool effect.
                return receipt.result
            grant = await session.scalar(
                select(ToolGrant).where(
                    ToolGrant.tool == "resolve_ticket", ToolGrant.enabled.is_(True)
                )
            )
            approver = await session.scalar(
                select(User).where(
                    User.id == run.input.get("approved_by"),
                    User.tenant_id == run.tenant_id,
                    User.active.is_(True),
                    User.role == "admin",
                )
            )
            actor = await session.scalar(
                select(User).where(
                    User.id == run.actor_id,
                    User.tenant_id == run.tenant_id,
                    User.active.is_(True),
                    User.role.in_(["admin", "operator"]),
                )
            )
            if not grant or not approver or not actor or approver.id == actor.id:
                raise PermissionError("tool_permission_revoked")
            if arguments["name"] != "resolve_ticket":
                raise PermissionError("unknown_tool")
            ticket = await session.scalar(
                update(Ticket)
                .where(
                    Ticket.id == arguments["ticket_id"],
                    Ticket.version == arguments["expected_version"],
                )
                .values(status="resolved", version=Ticket.version + 1)
                .returning(Ticket)
            )
            if ticket is None:
                raise ValueError("tool_ticket_conflict")
            result = {
                "ticket_id": ticket.id,
                "status": ticket.status,
                "version": ticket.version,
            }
            session.add(
                ToolReceipt(
                    id=uuid4().hex,
                    tenant_id=run.tenant_id,
                    run_id=run.id,
                    tool="resolve_ticket",
                    result=result,
                )
            )
            audit(
                session,
                "tool.resolve_ticket",
                str(ticket.id),
                {"run_id": run.id, "approved_by": approver.id},
            )
            event(session, run, "tool_completed", result)
            return result

    model = (
        model_factory(run.input)
        if model_factory
        else ChatOpenAI(
            api_key=settings.openai_api_key,
            model=run.input["model"],
            max_retries=0,
            timeout=settings.chat_timeout_seconds,
            max_tokens=run.input["max_output_tokens"],
            stream_usage=True,
        )
    )
    graph = build_run_graph(
        model,
        run.input,
        search,
        tool,
        publish,
        checkpointer,
        ttft_observer,
    )
    config = {
        "configurable": {"thread_id": f"{run.tenant_id}:{run.id}"},
        "recursion_limit": 8,
    }
    state = {}
    error_code = None
    try:
        snapshot = await graph.aget_state(config)
        async with asyncio.timeout(settings.chat_timeout_seconds + 15):
            # None resumes persisted state; a new input would restart completed nodes.
            state = await graph.ainvoke(None if snapshot.values else {}, config)
    except (asyncio.CancelledError, RunStopped):
        error_code = "cancelled"
    except Exception as exc:
        # Persist a bounded classification, never provider error bodies or SQL text.
        error_code = type(exc).__name__
    finally:
        if not state:
            with contextlib.suppress(Exception):
                state = (await graph.aget_state(config)).values
    usage = state.get("usage", {})
    charged = run.reserved_cost if progress["called"] else 0
    if usage:
        charged = (
            usage.get("input_tokens", 0) * run.input["input_price"]
            + usage.get("output_tokens", 0) * run.input["output_price"]
            + 999999
        ) // 1000000
    await finish_run(factory, run, state, error_code, charged)


async def finish_run(factory, run, state, error_code, charged):
    """Commit messages, final status, audit and budget settlement together."""
    async with factory() as session, session.begin():
        session.info.update(tenant_id=run.tenant_id, actor_id=run.actor_id)
        current = await session.scalar(
            select(AgentRun)
            .where(
                AgentRun.id == run.id,
                AgentRun.lease_token == run.lease_token,
                AgentRun.status == "running",
            )
            .with_for_update()
        )
        if current is None:
            return
        cancelled = current.cancel_requested or error_code == "cancelled"
        output = {
            "text": state.get("text", ""),
            "usage": state.get("usage", {}),
            "citations": [
                {"id": item["id"], "title": item["title"]}
                for item in state.get("documents", [])
            ],
            "tool_result": state.get("tool_result", {}),
            "prompt_version": run.input["prompt_version"],
            "conversation_id": run.input["conversation_id"],
        }
        if "structured" in state:
            output["structured"] = state["structured"]
        if not cancelled and error_code is None:
            conversation_id = UUID(run.input["conversation_id"])
            version = run.input["conversation_version"]
            if version is None:
                session.add(
                    Conversation(
                        id=conversation_id,
                        tenant_id=run.tenant_id,
                        owner=run.tenant_id,
                        version=1,
                    )
                )
                await session.flush()
            else:
                changed = await session.scalar(
                    update(Conversation)
                    .where(
                        Conversation.id == conversation_id,
                        Conversation.version == version,
                    )
                    .values(version=Conversation.version + 1)
                    .returning(Conversation.id)
                )
                if changed is None:
                    error_code = "conversation_conflict"
            if error_code is None:
                session.add_all(
                    [
                        ChatMessage(
                            conversation_id=conversation_id,
                            tenant_id=run.tenant_id,
                            role=role,
                            content=content,
                        )
                        for role, content in [
                            ("user", run.input["message"]),
                            ("assistant", output["text"]),
                        ]
                    ]
                )
        receipt = await session.scalar(
            select(ToolReceipt).where(ToolReceipt.run_id == run.id)
        )
        if receipt:
            output["tool_result"] = receipt.result
        current.status = (
            "cancelled" if cancelled else ("failed" if error_code else "succeeded")
        )
        current.output, current.error_code = output, error_code
        current.version += 1
        current.lease_until = None
        await settle_budget(session, run.budget_id, run.reserved_cost, charged)
        event(session, current, current.status, {"output": output, "code": error_code})
        audit(session, f"run.{current.status}", run.id)


async def process_one(
    factory,
    checkpointer,
    settings,
    model_factory=None,
    embedding_provider=None,
    ttft_observer: Callable[[float], None] | None = None,
    retrieval_observer: Callable[[float], None] | None = None,
):
    """Monitor execution for cancellation while renewing the worker's lease."""
    run = await claim_run(factory, settings)
    if run is None:
        return False

    async def traced_execution():
        context = TraceContextTextMapPropagator().extract(
            run.input.get("trace_context", {})
        )
        with trace.get_tracer("backendfastapi.worker").start_as_current_span(
            "agent.run",
            context=context,
            record_exception=False,
            set_status_on_exception=False,
        ):
            await execute_run(
                factory,
                run,
                checkpointer,
                settings,
                model_factory,
                embedding_provider,
                ttft_observer,
                retrieval_observer,
            )

    task = asyncio.create_task(traced_execution())
    try:
        while not task.done():
            await asyncio.wait({task}, timeout=0.5)
            if task.done():
                break
            async with factory() as session, session.begin():
                current = await session.scalar(
                    select(AgentRun).where(AgentRun.id == run.id)
                )
                if current.cancel_requested or current.lease_token != run.lease_token:
                    task.cancel()
                else:
                    await session.execute(
                        update(AgentRun)
                        .where(
                            AgentRun.id == run.id,
                            AgentRun.lease_token == run.lease_token,
                        )
                        .values(
                            lease_until=datetime.now(UTC)
                            + timedelta(seconds=settings.worker_lease_seconds)
                        )
                    )
        await task
    except asyncio.CancelledError:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        raise
    return True


def checkpoint_url(settings):
    return (
        make_url(settings.database_url)
        .set(drivername="postgresql")
        .render_as_string(hide_password=False)
    )


async def main(setup=False):
    settings = get_settings()
    configure_logging(settings.log_level)
    provider = configure_tracing(settings, "backendfastapi-worker")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(name, stop.set)
    try:
        async with contextlib.AsyncExitStack() as resources:
            embedding_client = None
            http_client = None
            if not setup and (settings.openai_api_key or settings.chat_enabled):
                http_client = await resources.enter_async_context(
                    httpx.AsyncClient(
                        timeout=settings.chat_timeout_seconds,
                        limits=httpx.Limits(
                            max_connections=settings.worker_concurrency,
                            max_keepalive_connections=settings.worker_concurrency,
                        ),
                    )
                )
                if settings.openai_api_key:
                    embedding_client = OpenAIEmbeddings(
                        api_key=settings.openai_api_key,
                        model=settings.openai_embedding_model,
                        dimensions=1536,
                        http_async_client=http_client,
                    )
            model = None
            if not setup and settings.chat_enabled:
                if http_client is None:
                    raise ValueError("Chat requires an initialized HTTP client")
                model = ChatOpenAI(
                    api_key=settings.chat_api_key,
                    base_url=settings.chat_base_url,
                    model=settings.chat_model,
                    max_retries=0,
                    timeout=settings.chat_timeout_seconds,
                    max_tokens=settings.chat_max_output_tokens,
                    stream_usage=True,
                    http_async_client=http_client,
                )
            saver = await resources.enter_async_context(
                AsyncPostgresSaver.from_conn_string(checkpoint_url(settings))
            )
            if setup:
                await saver.setup()
                return
            start_http_server(9001)
            completed = Counter("worker_runs_total", "Processed durable runs")
            duration = Histogram(
                "worker_run_duration_seconds", "Durable run execution duration"
            )

            time_to_first_token = Histogram(
                "agent_time_to_first_token_seconds",
                "Time from generation start to the first streamed token",
                buckets=(0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60),
            )
            retrieval_duration = Histogram(
                "agent_retrieval_duration_seconds",
                "Semantic or lexical retrieval duration",
                buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10),
            )

            async def worker_loop():
                while not stop.is_set():
                    with duration.time():
                        processed = await process_one(
                            SessionFactory,
                            saver,
                            settings,
                            model_factory=(lambda _: model) if model else None,
                            embedding_provider=embedding_client,
                            ttft_observer=time_to_first_token.observe,
                            retrieval_observer=retrieval_duration.observe,
                        )
                    if processed:
                        completed.inc()
                    else:
                        with contextlib.suppress(TimeoutError):
                            await asyncio.wait_for(
                                stop.wait(), timeout=settings.worker_poll_seconds
                            )

            tasks = [
                asyncio.create_task(worker_loop())
                for _ in range(settings.worker_concurrency)
            ]
            await asyncio.gather(*tasks)
    finally:
        if provider:
            await asyncio.to_thread(provider.shutdown)
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main(setup="--setup" in sys.argv))
