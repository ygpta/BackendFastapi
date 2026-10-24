"""Learn: persistence operations express SQL and detect missing or stale records."""

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backendfastapi.db.models import Ticket
from backendfastapi.repositories.audit import audit
from backendfastapi.schemas.tickets import TicketInput, TicketStatus, TicketStatusUpdate


class TicketNotFound(Exception):
    pass


class TicketConflict(Exception):
    pass


async def create_ticket_record(
    session: AsyncSession, data: TicketInput, commit: bool = True
) -> Ticket:
    record = Ticket(**data.model_dump())
    session.add(record)
    # Learn: flush obtains the generated ID but does not make the transaction durable.
    await session.flush()
    audit(session, "ticket.created", str(record.id))
    if commit:
        await session.commit()
        await session.refresh(record)
    return record


async def get_ticket_record(session: AsyncSession, ticket_id: int) -> Ticket | None:
    result = await session.execute(select(Ticket).where(Ticket.id == ticket_id))
    return result.scalar_one_or_none()


async def list_ticket_records(
    session: AsyncSession,
    limit: int,
    before_id: int | None,
    status: TicketStatus | None,
    search: str | None,
    customer_reference: str | None,
) -> list[Ticket]:
    query = select(Ticket)
    if before_id is not None:
        query = query.where(Ticket.id < before_id)
    if status is not None:
        query = query.where(Ticket.status == status)
    if search is not None:
        query = query.where(Ticket.subject.icontains(search.strip(), autoescape=True))
    if customer_reference is not None:
        query = query.where(Ticket.customer_reference == customer_reference)
    # Fetch one extra row to determine whether a next cursor exists, without COUNT(*).
    result = await session.scalars(query.order_by(Ticket.id.desc()).limit(limit + 1))
    return list(result.all())


async def update_ticket_status_record(
    session: AsyncSession, ticket_id: int, data: TicketStatusUpdate
) -> Ticket:
    """Compare-and-swap prevents a stale operator view from losing another edit."""
    async with session.begin():
        # Learn: matching the version inside UPDATE is atomic; read-then-write is not.
        record = await session.scalar(
            update(Ticket)
            .where(Ticket.id == ticket_id, Ticket.version == data.expected_version)
            .values(status=data.status, version=Ticket.version + 1)
            .returning(Ticket)
        )
        if record is None:
            if await session.get(Ticket, ticket_id) is None:
                raise TicketNotFound
            raise TicketConflict
        audit(
            session,
            "ticket.status_updated",
            str(ticket_id),
            {"status": data.status.value},
        )
        return record
