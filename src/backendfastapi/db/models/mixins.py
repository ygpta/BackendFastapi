"""Columns shared by resources protected by the tenant session hooks."""

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column


class TenantScoped:
    # The default supports pre-authentication local data; request sessions stamp
    # authenticated tenant IDs before flush (see db/tenancy.py).
    tenant_id: Mapped[str] = mapped_column(
        String(64), default="development", server_default="development", index=True
    )
