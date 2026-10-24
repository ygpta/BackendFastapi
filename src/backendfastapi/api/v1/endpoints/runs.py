import asyncio
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from backendfastapi.api.deps import get_db_session
from backendfastapi.core.config import Settings, get_settings
from backendfastapi.core.identity import Principal, get_principal, require_admin
from backendfastapi.db.models import AgentRun, LoginSession, RunEvent, User
from backendfastapi.schemas.platform import Approval, RunCreate, RunResponse
from backendfastapi.services.runs import (
    TERMINAL,
    approve_run,
    cancel_run,
    get_run,
    submit_run,
)

router = APIRouter(prefix="/runs", tags=["durable agent runs"])
Session = Annotated[AsyncSession, Depends(get_db_session)]
Actor = Annotated[Principal, Depends(get_principal)]


@router.post("", response_model=RunResponse, status_code=202)
async def submit(
    data: RunCreate,
    session: Session,
    actor: Actor,
    settings: Annotated[Settings, Depends(get_settings)],
    idempotency_key: Annotated[
        str, Header(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    ],
):
    return await submit_run(session, data, actor, idempotency_key, settings)


@router.get("", response_model=list[RunResponse])
async def list_runs(session: Session, limit: Annotated[int, Query(ge=1, le=100)] = 20):
    return list(
        (
            await session.scalars(
                select(AgentRun)
                .order_by(AgentRun.created_at.desc(), AgentRun.id)
                .limit(limit)
            )
        ).all()
    )


@router.get("/{run_id}", response_model=RunResponse)
async def read_run(run_id: str, session: Session):
    return await get_run(session, run_id)


@router.post("/{run_id}/cancel", response_model=RunResponse)
async def cancel(run_id: str, session: Session):
    return await cancel_run(session, run_id)


@router.post("/{run_id}/approval", response_model=RunResponse)
async def approval(
    run_id: str,
    data: Approval,
    session: Session,
    actor: Annotated[Principal, Depends(require_admin)],
):
    return await approve_run(session, run_id, data, actor)


@router.get("/{run_id}/events")
async def stream_events(
    run_id: str,
    request: Request,
    session: Session,
    actor: Actor,
    last_event_id: Annotated[int, Header(ge=0)] = 0,
    follow: bool = True,
):
    await get_run(session, run_id)
    await session.rollback()
    factory = request.app.state.session_factory

    async def events():
        cursor = last_event_id
        deadline = datetime.now(UTC) + timedelta(minutes=5)
        while datetime.now(UTC) < deadline:
            if await request.is_disconnected():
                return
            async with factory() as connection:
                connection.info["tenant_id"] = actor.tenant_id
                if actor.session_id:
                    active = await connection.scalar(
                        select(User.id)
                        .join(LoginSession, LoginSession.user_id == User.id)
                        .where(
                            User.id == actor.user_id,
                            User.tenant_id == actor.tenant_id,
                            User.active.is_(True),
                            LoginSession.id == actor.session_id,
                            LoginSession.expires_at > datetime.now(UTC),
                        )
                    )
                    if active is None:
                        yield {"event": "authorization_revoked", "data": "{}"}
                        return
                run = await get_run(connection, run_id)
                records = list(
                    (
                        await connection.scalars(
                            select(RunEvent)
                            .where(
                                RunEvent.run_id == run_id,
                                RunEvent.id > cursor,
                            )
                            .order_by(RunEvent.id)
                            .limit(100)
                        )
                    ).all()
                )
                terminal = run.status in TERMINAL
            for record in records:
                import json

                cursor = record.id
                yield {
                    "id": str(record.id),
                    "event": record.kind,
                    "data": json.dumps(record.payload),
                }
            if not follow or (terminal and len(records) < 100):
                return
            await asyncio.sleep(0.5)

    return EventSourceResponse(
        events(), ping=15, send_timeout=30, headers={"Cache-Control": "no-cache"}
    )
