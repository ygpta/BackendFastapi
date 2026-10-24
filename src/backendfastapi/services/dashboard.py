from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from backendfastapi.repositories.dashboard import get_dashboard_snapshot
from backendfastapi.schemas.dashboard import DashboardSummary, TicketCounts


async def get_dashboard_summary(
    session: AsyncSession, owner: str, chat_enabled: bool
) -> DashboardSummary:
    generated_at = datetime.now(UTC)
    snapshot = await get_dashboard_snapshot(
        session, owner, generated_at - timedelta(hours=24)
    )
    return DashboardSummary(
        tickets=TicketCounts.model_validate(dict(snapshot)),
        conversations=snapshot["conversations"],
        messages=snapshot["messages"],
        chat_enabled=chat_enabled,
        generated_at=generated_at,
    )
