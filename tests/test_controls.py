import asyncio

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from backendfastapi.core.config import Settings
from backendfastapi.db.models import Budget
from backendfastapi.services.controls import (
    claim_idempotency,
    consume_rate,
    reserve_budget,
    settle_budget,
)


def test_rate_limit_is_shared_by_sessions(session_factory):
    async def exercise():
        for _ in range(2):
            async with session_factory() as session:
                await consume_rate(session, "same-client", 2)
        async with session_factory() as session:
            with pytest.raises(HTTPException) as error:
                await consume_rate(session, "same-client", 2)
            assert error.value.status_code == 429

    asyncio.run(exercise())


def test_budget_reservations_and_idempotency(session_factory):
    async def exercise():
        settings = Settings(_env_file=None, monthly_budget_microusd=100)
        async with session_factory() as session:
            budget_id = await reserve_budget(session, "tenant", 60, settings)
            await session.commit()
        async with session_factory() as session:
            with pytest.raises(HTTPException):
                await reserve_budget(session, "tenant", 60, settings)
            await session.rollback()
            await settle_budget(session, budget_id, 60, 40)
            await session.commit()
            budget = await session.scalar(select(Budget))
            assert (budget.spent, budget.reserved) == (40, 0)
            record, replay = await claim_idempotency(
                session, "tenant", "key", {"input": 1}
            )
            assert not replay
            record.response = {"id": "run"}
            await session.commit()
        async with session_factory() as session:
            record, replay = await claim_idempotency(
                session, "tenant", "key", {"input": 1}
            )
            assert replay and record.response == {"id": "run"}
            with pytest.raises(HTTPException):
                await claim_idempotency(session, "tenant", "key", {"input": 2})

    asyncio.run(exercise())
