"""Append audit records without committing the caller's business transaction."""

from sqlalchemy.ext.asyncio import AsyncSession

from backendfastapi.db.models import AuditEvent


def audit(
    session: AsyncSession, action: str, resource_id: str, details: dict | None = None
) -> None:
    # The business change and its audit record must succeed or roll back together.
    session.add(
        AuditEvent(
            tenant_id=session.info.get("tenant_id", "development"),
            actor_id=session.info.get("actor_id", "legacy-service"),
            action=action,
            resource_id=str(resource_id),
            details=details or {},
        )
    )
