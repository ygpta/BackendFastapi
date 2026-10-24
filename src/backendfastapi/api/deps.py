"""Learn: dependencies supply authenticated, request-scoped database resources."""

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from backendfastapi.core.identity import Principal, get_principal
from backendfastapi.db import tenancy  # noqa: F401
from backendfastapi.db.session import SessionFactory


async def get_db_session(
    principal: Annotated[Principal, Depends(get_principal)],
) -> AsyncIterator[AsyncSession]:
    # Learn: the verified Principal supplies tenancy; client JSON cannot choose it.
    async with SessionFactory() as session:
        session.info["tenant_id"] = principal.tenant_id
        session.info["actor_id"] = principal.user_id
        # FastAPI resumes after yield for cleanup; session exit closes/rolls back.
        # Commits remain explicit in the business operation, not in this dependency.
        yield session
