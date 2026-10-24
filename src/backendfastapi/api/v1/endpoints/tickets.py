from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from backendfastapi.api.deps import get_db_session
from backendfastapi.repositories.tickets import TicketConflict, TicketNotFound
from backendfastapi.schemas.tickets import (
    TicketInput,
    TicketPage,
    TicketPreview,
    TicketResponse,
    TicketStatus,
    TicketStatusUpdate,
)
from backendfastapi.services.tickets import (
    create_ticket,
    get_ticket,
    list_tickets,
    preview_ticket,
    update_ticket_status,
)

router = APIRouter(prefix="/tickets", tags=["tickets"])


@router.post("/preview", response_model=TicketPreview)
async def preview_ticket_endpoint(ticket: TicketInput) -> TicketPreview:
    return preview_ticket(ticket)


@router.post("", response_model=TicketResponse, status_code=status.HTTP_201_CREATED)
async def create_ticket_endpoint(
    ticket: TicketInput,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    idempotency_key: Annotated[
        str | None, Header(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    ] = None,
) -> TicketResponse:
    record = await create_ticket(session, ticket, idempotency_key)
    return TicketResponse.model_validate(record)


@router.get("/{ticket_id}", response_model=TicketResponse)
async def get_ticket_endpoint(
    ticket_id: Annotated[int, Path(ge=1)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> TicketResponse:
    record = await get_ticket(session, ticket_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Ticket not found")
    return TicketResponse.model_validate(record)


@router.get("", response_model=TicketPage)
async def list_tickets_endpoint(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    before_id: Annotated[int | None, Query(ge=1)] = None,
    status: TicketStatus | None = None,
    search: Annotated[
        str | None, Query(min_length=1, max_length=120, pattern=r".*\S.*")
    ] = None,
    customer_reference: Annotated[
        str | None, Query(min_length=1, max_length=64)
    ] = None,
) -> TicketPage:
    return await list_tickets(
        session, limit, before_id, status, search, customer_reference
    )


@router.patch("/{ticket_id}", response_model=TicketResponse)
async def update_ticket_status_endpoint(
    ticket_id: Annotated[int, Path(ge=1)],
    data: TicketStatusUpdate,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> TicketResponse:
    try:
        record = await update_ticket_status(session, ticket_id, data)
    except TicketNotFound as exc:
        raise HTTPException(status_code=404, detail="Ticket not found") from exc
    except TicketConflict as exc:
        raise HTTPException(
            status_code=409, detail="Ticket changed; reload before updating"
        ) from exc
    return TicketResponse.model_validate(record)
