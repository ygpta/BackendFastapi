import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from backendfastapi.core.config import Settings, get_settings
from backendfastapi.core.identity import (
    Principal,
    create_access_token,
    digest_token,
    dummy_hash,
    get_auth_session,
    get_principal,
    hash_password,
    require_admin,
    verify_password,
)
from backendfastapi.db.models import LoginSession, Tenant, User
from backendfastapi.repositories.audit import audit
from backendfastapi.schemas.auth import (
    LoginRequest,
    RefreshRequest,
    TenantRegistration,
    TokenResponse,
    UserCreate,
    UserResponse,
    UserUpdate,
)
from backendfastapi.services.controls import consume_rate

router = APIRouter(prefix="/auth", tags=["identity"])
Session = Annotated[AsyncSession, Depends(get_auth_session)]
Configuration = Annotated[Settings, Depends(get_settings)]


def check_auth(settings: Settings) -> None:
    if not settings.auth_enabled:
        raise HTTPException(503, "User authentication is not configured")


async def issue_tokens(
    session: AsyncSession, user: User, settings: Settings
) -> TokenResponse:
    refresh = secrets.token_urlsafe(48)
    login = LoginSession(
        id=uuid4().hex,
        user_id=user.id,
        refresh_hash=digest_token(refresh),
        expires_at=datetime.now(UTC) + timedelta(days=settings.refresh_token_days),
    )
    session.add(login)
    session.info.update(tenant_id=user.tenant_id, actor_id=user.id)
    audit(session, "auth.session_created", login.id)
    await session.commit()
    return TokenResponse(
        access_token=create_access_token(user, login.id, settings),
        refresh_token=refresh,
        tenant_id=user.tenant_id,
        expires_in=settings.access_token_minutes * 60,
    )


@router.post("/register", response_model=TokenResponse, status_code=201)
async def register(
    data: TenantRegistration,
    request: Request,
    session: Session,
    settings: Configuration,
):
    check_auth(settings)
    if not settings.registration_enabled:
        raise HTTPException(403, "Self-service registration disabled")
    await consume_rate(
        session,
        f"registration:{request.client.host if request.client else 'unknown'}",
        settings.login_attempts_per_minute,
    )
    tenant_id = uuid4().hex
    session.add(Tenant(id=tenant_id, name=data.tenant_name))
    await session.flush()
    user = User(
        id=uuid4().hex,
        tenant_id=tenant_id,
        email=data.email.casefold(),
        role="admin",
        password_hash=await run_in_threadpool(
            hash_password, data.password.get_secret_value()
        ),
        active=True,
    )
    session.add(user)
    await session.flush()
    return await issue_tokens(session, user, settings)


@router.post("/login", response_model=TokenResponse)
async def login(
    data: LoginRequest, request: Request, session: Session, settings: Configuration
):
    check_auth(settings)
    await consume_rate(
        session,
        f"login:{request.client.host if request.client else 'unknown'}",
        settings.login_attempts_per_minute,
    )
    user = await session.scalar(
        select(User).where(
            User.tenant_id == data.tenant_id, User.email == data.email.casefold()
        )
    )
    valid = await run_in_threadpool(
        verify_password,
        data.password.get_secret_value(),
        user.password_hash if user else dummy_hash,
    )
    if not valid or user is None or not user.active:
        raise HTTPException(401, "Invalid credentials")
    return await issue_tokens(session, user, settings)


@router.post("/refresh", response_model=TokenResponse)
async def refresh(
    data: RefreshRequest, request: Request, session: Session, settings: Configuration
):
    check_auth(settings)
    await consume_rate(
        session,
        f"refresh:{request.client.host if request.client else 'unknown'}",
        settings.login_attempts_per_minute,
    )
    refresh = secrets.token_urlsafe(48)
    login = await session.scalar(
        update(LoginSession)
        .where(
            LoginSession.refresh_hash
            == digest_token(data.refresh_token.get_secret_value()),
            LoginSession.expires_at > datetime.now(UTC),
        )
        .values(refresh_hash=digest_token(refresh))
        .returning(LoginSession)
    )
    if login is None:
        raise HTTPException(401, "Invalid refresh token")
    user = await session.get(User, login.user_id)
    if user is None or not user.active:
        raise HTTPException(401, "User disabled")
    session.info.update(tenant_id=user.tenant_id, actor_id=user.id)
    audit(session, "auth.session_refreshed", login.id)
    await session.commit()
    return TokenResponse(
        access_token=create_access_token(user, login.id, settings),
        refresh_token=refresh,
        tenant_id=user.tenant_id,
        expires_in=settings.access_token_minutes * 60,
    )


@router.get("/me", response_model=UserResponse)
async def me(
    session: Session,
    principal: Annotated[Principal, Depends(get_principal)],
    settings: Configuration,
):
    check_auth(settings)
    return await session.get(User, principal.user_id)


@router.post("/logout", status_code=204)
async def logout(
    session: Session, principal: Annotated[Principal, Depends(get_principal)]
):
    await session.execute(
        delete(LoginSession).where(LoginSession.id == principal.session_id)
    )
    session.info.update(tenant_id=principal.tenant_id, actor_id=principal.user_id)
    audit(session, "auth.logout", principal.session_id or "legacy")
    await session.commit()
    return Response(status_code=204)


@router.post("/users", response_model=UserResponse, status_code=201)
async def add_user(
    data: UserCreate,
    session: Session,
    principal: Annotated[Principal, Depends(require_admin)],
):
    user = User(
        id=uuid4().hex,
        tenant_id=principal.tenant_id,
        email=data.email.casefold(),
        role=data.role,
        active=True,
        password_hash=await run_in_threadpool(
            hash_password, data.password.get_secret_value()
        ),
    )
    session.add(user)
    session.info.update(tenant_id=principal.tenant_id, actor_id=principal.user_id)
    audit(session, "user.created", user.id, {"role": user.role})
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(409, "Email already exists in tenant") from exc
    return user


@router.patch("/users/{user_id}", response_model=UserResponse)
async def update_user(
    user_id: str,
    data: UserUpdate,
    session: Session,
    principal: Annotated[Principal, Depends(require_admin)],
):
    if user_id == principal.user_id:
        raise HTTPException(409, "Use a different administrator to change your account")
    user = await session.scalar(
        select(User).where(User.id == user_id, User.tenant_id == principal.tenant_id)
    )
    if user is None:
        raise HTTPException(404, "User not found")
    user.role, user.active = data.role, data.active
    session.info.update(tenant_id=principal.tenant_id, actor_id=principal.user_id)
    audit(session, "user.updated", user.id, {"role": user.role, "active": user.active})
    await session.commit()
    return user
