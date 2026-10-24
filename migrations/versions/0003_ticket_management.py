"""Add ticket lifecycle status and optimistic concurrency.

Revision ID: 0003
Revises: 0002
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Server defaults backfill existing rows and keep old application writers valid.
    op.add_column(
        "tickets",
        sa.Column("status", sa.String(16), nullable=False, server_default="open"),
    )
    op.add_column(
        "tickets",
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.create_check_constraint(
        "ck_ticket_status",
        "tickets",
        "status IN ('open', 'in_progress', 'resolved', 'closed')",
    )
    op.create_index("ix_tickets_status_id", "tickets", ["status", "id"])


def downgrade() -> None:
    op.drop_index("ix_tickets_status_id", table_name="tickets")
    op.drop_constraint("ck_ticket_status", "tickets", type_="check")
    op.drop_column("tickets", "version")
    op.drop_column("tickets", "status")
