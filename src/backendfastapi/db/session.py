from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backendfastapi.core.config import get_settings

settings = get_settings()
engine = create_async_engine(
    settings.database_url,
    hide_parameters=True,
    pool_pre_ping=True,
    pool_size=settings.database_pool_size,
    max_overflow=settings.database_max_overflow,
    pool_timeout=settings.database_timeout_seconds,
    pool_recycle=1800,
    connect_args={
        "timeout": settings.database_timeout_seconds,
        "command_timeout": settings.database_timeout_seconds,
        "server_settings": {
            "statement_timeout": str(settings.database_timeout_seconds * 1000),
            "idle_in_transaction_session_timeout": "60000",
        },
    },
)
SessionFactory = async_sessionmaker(engine, expire_on_commit=False)
