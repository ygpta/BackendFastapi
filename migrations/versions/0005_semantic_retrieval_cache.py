"""semantic_retrieval_cache

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-24

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.add_column("documents", sa.Column("embedding", Vector(1536), nullable=True))
    op.add_column(
        "documents", sa.Column("embedding_model", sa.String(length=120), nullable=True)
    )
    op.create_index(
        "ix_documents_embedding_hnsw",
        "documents",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )
    op.create_table(
        "embedding_cache",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("embedding", sa.JSON(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "tenant_id",
            sa.String(length=64),
            server_default="development",
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_embedding_cache_expires_at"),
        "embedding_cache",
        ["expires_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_embedding_cache_tenant_id"),
        "embedding_cache",
        ["tenant_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_documents_embedding_hnsw", table_name="documents")
    op.drop_index(op.f("ix_embedding_cache_tenant_id"), table_name="embedding_cache")
    op.drop_index(op.f("ix_embedding_cache_expires_at"), table_name="embedding_cache")
    op.drop_table("embedding_cache")
    op.drop_column("documents", "embedding_model")
    op.drop_column("documents", "embedding")
