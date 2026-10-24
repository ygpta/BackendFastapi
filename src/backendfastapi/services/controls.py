"""Shared rate, quota and replay controls coordinated through PostgreSQL."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from backendfastapi.core.config import Settings
from backendfastapi.db.models import Budget, IdempotencyRecord, RateBucket


def insert_for(session: AsyncSession, model):
    """Use SQLite only for tests; production relies on PostgreSQL atomic upserts."""
    return (sqlite_insert if session.bind.dialect.name == "sqlite" else pg_insert)(
        model
    )


async def consume_rate(session: AsyncSession, identity: str, limit: int) -> None:
    """Commit a shared fixed-window counter independently of business mutations."""
    now = datetime.now(UTC)
    window = int(now.timestamp()) // 60
    key = hashlib.sha256(f"{identity}:{window}".encode()).hexdigest()
    count = await session.scalar(
        insert_for(session, RateBucket)
        .values(id=key, count=1, expires_at=now + timedelta(minutes=2))
        .on_conflict_do_update(
            index_elements=["id"],
            set_={"count": RateBucket.count + 1},
            where=RateBucket.count < limit,
        )
        .returning(RateBucket.count)
    )
    await session.commit()
    if count is None:
        raise HTTPException(
            429,
            "Rate limit exceeded",
            headers={"Retry-After": str(60 - int(now.timestamp()) % 60)},
        )


def cost_for(settings: Settings, input_tokens: int, output_tokens: int) -> int:
    # Integer micro-USD and upward rounding avoid floating-point budget drift.
    return (
        input_tokens * settings.model_input_price_per_million_microusd
        + output_tokens * settings.model_output_price_per_million_microusd
        + 999999
    ) // 1000000


async def reserve_budget(
    session: AsyncSession, tenant_id: str, amount: int, settings: Settings
) -> str:
    """Reserve funds atomically; the caller commits them together with the job."""
    budget_id = f"{tenant_id}:{datetime.now(UTC):%Y-%m}"
    await session.execute(
        insert_for(session, Budget)
        .values(
            id=budget_id,
            tenant_id=tenant_id,
            allowance=settings.monthly_budget_microusd,
            spent=0,
            reserved=0,
        )
        .on_conflict_do_nothing(index_elements=["id"])
    )
    reserved = await session.scalar(
        update(Budget)
        .where(
            Budget.id == budget_id,
            Budget.spent + Budget.reserved + amount <= Budget.allowance,
        )
        .values(reserved=Budget.reserved + amount)
        .returning(Budget.id)
    )
    if reserved is None:
        raise HTTPException(429, "Monthly model budget exhausted")
    return budget_id


async def settle_budget(
    session: AsyncSession, budget_id: str, reserved: int, charged: int
):
    """Release a reservation and charge usage inside the final run transaction."""
    await session.execute(
        update(Budget)
        .where(Budget.id == budget_id)
        .values(
            reserved=Budget.reserved - reserved,
            spent=Budget.spent + min(reserved, charged),
        )
    )


async def claim_idempotency(session: AsyncSession, tenant: str, key: str, data: dict):
    """Claim a replay key in the same transaction as its eventual response."""
    digest = hashlib.sha256(
        json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    record_id = uuid4().hex
    inserted = await session.scalar(
        insert_for(session, IdempotencyRecord)
        .values(
            id=record_id,
            tenant_id=tenant,
            key=key,
            request_hash=digest,
            response={},
        )
        .on_conflict_do_nothing(index_elements=["tenant_id", "key"])
        .returning(IdempotencyRecord.id)
    )
    # A concurrent insert waits for the winner's transaction before reading its result.
    if inserted:
        return await session.get(IdempotencyRecord, record_id), False
    from sqlalchemy import select

    record = await session.scalar(
        select(IdempotencyRecord).where(
            IdempotencyRecord.tenant_id == tenant, IdempotencyRecord.key == key
        )
    )
    if record.request_hash != digest:
        raise HTTPException(409, "Idempotency key already used with different input")
    return record, True
