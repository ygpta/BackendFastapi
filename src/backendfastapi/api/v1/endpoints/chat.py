from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from openai import APIError, APITimeoutError, RateLimitError
from sqlalchemy.ext.asyncio import AsyncSession

from backendfastapi.api.deps import get_db_session
from backendfastapi.core.config import Settings, get_settings
from backendfastapi.core.identity import get_owner
from backendfastapi.repositories.chat import (
    ConversationConflict,
    ConversationNotFound,
    delete_conversation,
    get_conversation,
    get_messages,
)
from backendfastapi.schemas.chat import (
    ChatRequest,
    ChatResponse,
    HistoryResponse,
    MessageResponse,
)
from backendfastapi.services.chat import send_message

router = APIRouter(prefix="/chat", tags=["chat"])
Session = Annotated[AsyncSession, Depends(get_db_session)]
Owner = Annotated[str, Depends(get_owner)]


@router.post("", response_model=ChatResponse)
async def chat(
    data: ChatRequest,
    request: Request,
    session: Session,
    owner: Owner,
    settings: Annotated[Settings, Depends(get_settings)],
) -> ChatResponse:
    if settings.auth_enabled:
        raise HTTPException(
            409, "Authenticated chat uses POST /api/v1/runs with an Idempotency-Key"
        )
    workflow = getattr(request.app.state, "chat_workflow", None)
    if workflow is None:
        raise HTTPException(status_code=503, detail="Chat is not configured")
    try:
        return await send_message(session, data, owner, workflow, settings)
    except ConversationNotFound as exc:
        raise HTTPException(status_code=404, detail="Conversation not found") from exc
    except ConversationConflict as exc:
        raise HTTPException(
            status_code=409, detail="Conversation changed; reload before retrying"
        ) from exc
    except (TimeoutError, APITimeoutError) as exc:
        raise HTTPException(status_code=504, detail="Chat provider timed out") from exc
    except RateLimitError as exc:
        raise HTTPException(
            status_code=503, detail="Chat provider busy", headers={"Retry-After": "30"}
        ) from exc
    except (APIError, ValueError) as exc:
        raise HTTPException(
            status_code=502, detail="Chat provider unavailable"
        ) from exc


@router.get("/{conversation_id}/messages", response_model=HistoryResponse)
async def history(
    conversation_id: UUID,
    session: Session,
    owner: Owner,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    before_id: Annotated[int | None, Query(ge=1)] = None,
) -> HistoryResponse:
    try:
        await get_conversation(session, conversation_id, owner)
    except ConversationNotFound as exc:
        raise HTTPException(status_code=404, detail="Conversation not found") from exc
    records = await get_messages(session, conversation_id, limit, before_id)
    return HistoryResponse(
        conversation_id=conversation_id,
        messages=[MessageResponse.model_validate(record) for record in records],
    )


@router.delete("/{conversation_id}", status_code=204)
async def remove_conversation(
    conversation_id: UUID, session: Session, owner: Owner
) -> Response:
    if not await delete_conversation(session, conversation_id, owner):
        raise HTTPException(status_code=404, detail="Conversation not found")
    return Response(status_code=204)
