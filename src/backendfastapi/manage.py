"""Operator-only provisioning; passwords are entered directly in the terminal."""

import argparse
import asyncio
import getpass
from uuid import uuid4

from sqlalchemy import select

from backendfastapi.core.identity import password_hasher
from backendfastapi.db.models import Tenant, User
from backendfastapi.db.session import SessionFactory, engine


async def create_admin(arguments):
    password = getpass.getpass("New administrator password (12+ characters): ")
    confirmation = getpass.getpass("Confirm password: ")
    if password != confirmation or not 12 <= len(password) <= 128:
        raise ValueError("Passwords must match and contain 12-128 characters")
    try:
        async with SessionFactory() as session, session.begin():
            tenant_id = arguments.tenant_id or uuid4().hex
            tenant = await session.get(Tenant, tenant_id)
            if tenant is None:
                session.add(Tenant(id=tenant_id, name=arguments.tenant_name))
                await session.flush()
            exists = await session.scalar(
                select(User).where(
                    User.tenant_id == tenant_id,
                    User.email == arguments.email.casefold(),
                )
            )
            if exists:
                raise ValueError("User already exists")
            session.add(
                User(
                    id=uuid4().hex,
                    tenant_id=tenant_id,
                    email=arguments.email.casefold(),
                    password_hash=password_hasher.hash(password),
                    role="admin",
                    active=True,
                )
            )
        print(f"Administrator provisioned for tenant {tenant_id}")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant-name", required=True)
    parser.add_argument("--tenant-id")
    parser.add_argument("--email", required=True)
    asyncio.run(create_admin(parser.parse_args()))
