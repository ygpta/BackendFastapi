from datetime import datetime

from sqlalchemy import case, func, select
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession

from backendfastapi.db.models import ChatMessage, Conversation, Ticket
from backendfastapi.schemas.tickets import TicketStatus


async def get_dashboard_snapshot(
    session: AsyncSession, owner: str, since: datetime
) -> RowMapping:
    """One statement keeps all summary counts on the same database snapshot."""
    conversations = (
        select(func.count(Conversation.id))
        .where(Conversation.owner == owner)
        .scalar_subquery()
    )
    messages = (
        select(func.count(ChatMessage.id))
        .join(Conversation, ChatMessage.conversation_id == Conversation.id)
        .where(Conversation.owner == owner)
        .scalar_subquery()
    )
    result = await session.execute(
        select(
            func.count(Ticket.id).label("total"),
            *[
                func.count(case((Ticket.status == status, 1))).label(status.value)
                for status in TicketStatus
            ],
            func.count(case((Ticket.created_at >= since, 1))).label("created_last_24h"),
            conversations.label("conversations"),
            messages.label("messages"),
        )
    )
    return result.mappings().one()
