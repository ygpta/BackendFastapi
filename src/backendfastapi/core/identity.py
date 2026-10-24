"""Verify identity before creating a tenant-scoped business session."""

import hashlib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import BoundedSemaphore
from typing import Annotated
from uuid import uuid4

import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pwdlib import PasswordHash
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backendfastapi.core.config import Settings, get_settings
from backendfastapi.core.security import api_key_header, require_api_key
from backendfastapi.db.models import LoginSession, User
from backendfastapi.db.session import SessionFactory

password_hasher = PasswordHash.recommended()
# Unknown users still incur password verification work to reduce timing differences.
dummy_hash = password_hasher.hash("not-a-valid-user-password")
# Argon2 is memory-intensive; route handlers run these bounded helpers in threads.
password_slots = BoundedSemaphore(2)
bearer = HTTPBearer(auto_error=False)


def hash_password(password: str) -> str:
    with password_slots:
        return password_hasher.hash(password)


def verify_password(password: str, encoded: str) -> bool:
    with password_slots:
        return password_hasher.verify(password, encoded)


@dataclass(frozen=True)
class Principal:
    """Trusted identity passed between dependencies, never built from request JSON."""

    user_id: str
    tenant_id: str
    role: str
    session_id: str | None = None


async def get_auth_session() -> AsyncIterator[AsyncSession]:
    """Identity lookup is explicitly unscoped; business sessions are always scoped."""
    async with SessionFactory() as session:
        yield session


def digest_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_access_token(user: User, session_id: str, settings: Settings) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "sub": user.id,
            "tenant": user.tenant_id,
            "sid": session_id,
            "iss": settings.jwt_issuer,
            "aud": settings.jwt_audience,
            "iat": now,
            "exp": now + timedelta(minutes=settings.access_token_minutes),
            "jti": uuid4().hex,
        },
        settings.jwt_secret.get_secret_value(),
        algorithm="HS256",
    )


async def get_principal(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_auth_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    supplied_key: Annotated[str | None, Depends(api_key_header)],
) -> Principal:
    """Validate the token, live session and current database role on each request."""
    if not settings.auth_enabled:
        owner = require_api_key(settings, supplied_key)
        return Principal(user_id="legacy-service", tenant_id=owner, role="admin")
    if credentials is None:
        raise HTTPException(
            401, "Bearer token required", headers={"WWW-Authenticate": "Bearer"}
        )
    try:
        claims = jwt.decode(
            credentials.credentials,
            settings.jwt_secret.get_secret_value(),
            algorithms=["HS256"],
            issuer=settings.jwt_issuer,
            audience=settings.jwt_audience,
            options={"require": ["sub", "tenant", "sid", "exp", "iat", "jti"]},
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(401, "Invalid or expired token") from exc
    # JWT claims locate the account; the database decides its current permissions.
    record = await session.scalar(
        select(User)
        .join(LoginSession, LoginSession.user_id == User.id)
        .where(
            User.id == claims["sub"],
            User.tenant_id == claims["tenant"],
            User.active.is_(True),
            LoginSession.id == claims["sid"],
            LoginSession.expires_at > datetime.now(UTC),
        )
    )
    if record is None:
        raise HTTPException(401, "Session revoked or user disabled")
    if (
        request.method not in {"GET", "HEAD", "OPTIONS"}
        and record.role == "viewer"
        and not request.url.path.endswith("/auth/logout")
    ):
        raise HTTPException(403, "Read-only role")
    from backendfastapi.services.controls import consume_rate

    await consume_rate(
        session, f"api:{record.tenant_id}:{record.id}", settings.requests_per_minute
    )
    return Principal(record.id, record.tenant_id, record.role, claims["sid"])


def require_admin(principal: Annotated[Principal, Depends(get_principal)]) -> Principal:
    if principal.role != "admin":
        raise HTTPException(403, "Administrator role required")
    return principal


def get_owner(principal: Annotated[Principal, Depends(get_principal)]) -> str:
    return principal.tenant_id
