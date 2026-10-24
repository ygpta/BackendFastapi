import asyncio
from uuid import uuid4

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langgraph.graph.state import CompiledStateGraph
from sqlalchemy.ext.asyncio import AsyncSession

from backendfastapi.core.config import Settings
from backendfastapi.repositories.chat import get_conversation, get_messages, save_turn
from backendfastapi.schemas.chat import ChatRequest, ChatResponse


async def send_message(
    session: AsyncSession,
    data: ChatRequest,
    owner: str,
    workflow: CompiledStateGraph,
    settings: Settings,
) -> ChatResponse:
    conversation_id = data.conversation_id or uuid4()
    version = None
    history: list[BaseMessage] = []
    if data.conversation_id:
        conversation = await get_conversation(session, conversation_id, owner)
        version = conversation.version
        records = await get_messages(
            session, conversation_id, settings.chat_max_history_messages
        )
        if records and records[0].role == "assistant":
            records = records[1:]
        history = [
            HumanMessage(content=record.content)
            if record.role == "user"
            else AIMessage(content=record.content)
            for record in records
        ]
        # Release the read transaction before waiting on a remote model provider.
        await session.rollback()

    async with asyncio.timeout(settings.chat_timeout_seconds):
        result = await workflow.ainvoke(
            {"messages": [*history, HumanMessage(content=data.message)]},
            config={"recursion_limit": 4},
        )
    answer = result["messages"][-1].content
    await save_turn(session, conversation_id, owner, version, data.message, answer)
    return ChatResponse(conversation_id=conversation_id, message=answer)
