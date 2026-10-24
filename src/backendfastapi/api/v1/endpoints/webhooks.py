from typing import Annotated

from fastapi import APIRouter, Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession

from backendfastapi.api.deps import get_db_session
from backendfastapi.core.config import Settings, get_settings
from backendfastapi.core.identity import Principal, get_principal
from backendfastapi.schemas.platform import RunCreate, RunResponse
from backendfastapi.services.runs import submit_run

router = APIRouter(prefix="/webhooks", tags=["webhooks"])
Session = Annotated[AsyncSession, Depends(get_db_session)]
Actor = Annotated[Principal, Depends(get_principal)]


@router.post("/runs", response_model=RunResponse, status_code=202)
async def receive_run(
    data: RunCreate,
    session: Session,
    actor: Actor,
    settings: Annotated[Settings, Depends(get_settings)],
    idempotency_key: Annotated[
        str, Header(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    ],
):
    return await submit_run(session, data, actor, idempotency_key, settings)
