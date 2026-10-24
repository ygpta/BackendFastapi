import asyncio

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from backendfastapi.api.deps import get_db_session
from backendfastapi.core.identity import get_auth_session, get_principal
from backendfastapi.db import models  # noqa: F401
from backendfastapi.db.base import Base
from backendfastapi.main import app


@pytest.fixture
def session_factory(tmp_path):
    test_engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'test.db'}", poolclass=NullPool
    )

    @event.listens_for(test_engine.sync_engine, "connect")
    def enable_foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")

    async def create_tables():
        async with test_engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    asyncio.run(create_tables())
    yield async_sessionmaker(test_engine, expire_on_commit=False)
    asyncio.run(test_engine.dispose())


@pytest.fixture
def client(session_factory):
    async def override_get_db_session(principal=Depends(get_principal)):
        async with session_factory() as session:
            session.info["tenant_id"] = principal.tenant_id
            session.info["actor_id"] = principal.user_id
            yield session

    async def override_auth_session():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db_session] = override_get_db_session
    app.dependency_overrides[get_auth_session] = override_auth_session
    original_factory = app.state.session_factory
    app.state.session_factory = session_factory
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.pop(get_db_session, None)
        app.dependency_overrides.pop(get_auth_session, None)
        app.state.session_factory = original_factory
