from uuid import UUID

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backendfastapi.db.models import ChatMessage, Conversation


class ConversationNotFound(Exception):
    pass


class ConversationConflict(Exception):
    pass


async def get_conversation(
    session: AsyncSession, conversation_id: UUID, owner: str
) -> Conversation:
    result = await session.scalar(
        select(Conversation).where(
            Conversation.id == conversation_id, Conversation.owner == owner
        )
    )
    if result is None:
        raise ConversationNotFound
    return result


async def get_messages(
    session: AsyncSession,
    conversation_id: UUID,
    limit: int,
    before_id: int | None = None,
) -> list[ChatMessage]:
    query = select(ChatMessage).where(ChatMessage.conversation_id == conversation_id)
    if before_id is not None:
        query = query.where(ChatMessage.id < before_id)
    records = await session.scalars(query.order_by(ChatMessage.id.desc()).limit(limit))
    return list(reversed(records.all()))


async def save_turn(
    session: AsyncSession,
    conversation_id: UUID,
    owner: str,
    expected_version: int | None,
    question: str,
    answer: str,
) -> None:
    """Persist both messages atomically; compare-and-swap prevents lost turns."""
    async with session.begin():
        if expected_version is None:
            session.add(
                Conversation(
                    id=conversation_id, owner=owner, tenant_id=owner, version=1
                )
            )
            await session.flush()
        else:
            result = await session.execute(
                update(Conversation)
                .where(
                    Conversation.id == conversation_id,
                    Conversation.owner == owner,
                    Conversation.version == expected_version,
                )
                .values(version=expected_version + 1)
            )
            if result.rowcount != 1:
                raise ConversationConflict
        session.add_all(
            [
                ChatMessage(
                    conversation_id=conversation_id,
                    tenant_id=owner,
                    role="user",
                    content=question,
                ),
                ChatMessage(
                    conversation_id=conversation_id,
                    tenant_id=owner,
                    role="assistant",
                    content=answer,
                ),
            ]
        )


async def delete_conversation(
    session: AsyncSession, conversation_id: UUID, owner: str
) -> bool:
    result = await session.execute(
        delete(Conversation).where(
            Conversation.id == conversation_id, Conversation.owner == owner
        )
    )
    await session.commit()
    return result.rowcount == 1
