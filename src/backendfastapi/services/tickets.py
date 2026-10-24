"""Learn: use cases connect validation, replay behavior and persistence.

Trace create_ticket from its endpoint, then run test_ticket_creation_idempotency.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from backendfastapi.db.models import Ticket
from backendfastapi.repositories.tickets import (
    create_ticket_record,
    get_ticket_record,
    list_ticket_records,
    update_ticket_status_record,
)
from backendfastapi.schemas.tickets import (
    TicketInput,
    TicketPage,
    TicketPreview,
    TicketResponse,
    TicketStatus,
    TicketStatusUpdate,
)
from backendfastapi.services.controls import claim_idempotency


def preview_ticket(ticket: TicketInput) -> TicketPreview:
    return TicketPreview(
        subject=ticket.subject,
        message=ticket.message,
        customer_reference=ticket.customer_reference,
        status="validated",
    )


async def create_ticket(
    session: AsyncSession, ticket: TicketInput, key: str | None = None
):
    if key is None:
        return await create_ticket_record(session, ticket)
    record, replay = await claim_idempotency(
        session,
        session.info.get("tenant_id", "development"),
        key,
        {"operation": "ticket.create", **ticket.model_dump()},
    )
    if replay:
        return TicketResponse.model_validate(record.response)
    # Learn: commit=False keeps the ticket and replay response in one transaction.
    ticket_record = await create_ticket_record(session, ticket, commit=False)
    response = TicketResponse.model_validate(ticket_record)
    record.response = response.model_dump(mode="json")
    await session.commit()
    return response


async def get_ticket(session: AsyncSession, ticket_id: int) -> Ticket | None:
    return await get_ticket_record(session, ticket_id)


async def list_tickets(
    session: AsyncSession,
    limit: int,
    before_id: int | None,
    status: TicketStatus | None,
    search: str | None,
    customer_reference: str | None,
) -> TicketPage:
    records = await list_ticket_records(
        session, limit, before_id, status, search, customer_reference
    )
    return TicketPage(
        items=[TicketResponse.model_validate(record) for record in records[:limit]],
        next_cursor=records[limit - 1].id if len(records) > limit else None,
    )


async def update_ticket_status(
    session: AsyncSession, ticket_id: int, data: TicketStatusUpdate
) -> Ticket:
    return await update_ticket_status_record(session, ticket_id, data)
