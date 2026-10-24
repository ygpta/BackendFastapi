from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from backendfastapi.api.deps import get_db_session
from backendfastapi.core.config import Settings, get_settings
from backendfastapi.core.identity import get_owner
from backendfastapi.schemas.dashboard import DashboardSummary
from backendfastapi.services.dashboard import get_dashboard_summary

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("/summary", response_model=DashboardSummary)
async def summary(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    owner: Annotated[str, Depends(get_owner)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> DashboardSummary:
    return await get_dashboard_summary(session, owner, settings.chat_enabled)
