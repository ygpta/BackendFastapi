"""Durable agent work, reviewed tools, retrieval sources and quality records."""

from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
    literal_column,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from backendfastapi.db.base import Base
from backendfastapi.db.models.mixins import TenantScoped


class PromptVersion(TenantScoped, Base):
    __tablename__ = "prompt_versions"
    __table_args__ = (
        Index(
            "uq_prompt_active_tenant",
            "tenant_id",
            unique=True,
            postgresql_where=text("active"),
            sqlite_where=text("active"),
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    content: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Document(TenantScoped, Base):
    __tablename__ = "documents"
    __table_args__ = (
        # Production retrieval is indexed in PostgreSQL; SQLite is a test fallback.
        Index(
            "ix_documents_search",
            func.to_tsvector(literal_column("'english'::regconfig"), text("content")),
            postgresql_using="gin",
        ).ddl_if(dialect="postgresql"),
        Index(
            "ix_documents_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ).ddl_if(dialect="postgresql"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    content: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(1536).with_variant(JSON(), "sqlite"), nullable=True
    )
    embedding_model: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class EmbeddingCache(TenantScoped, Base):
    __tablename__ = "embedding_cache"
    __table_args__ = (Index("ix_embedding_cache_expires_at", "expires_at"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    embedding: Mapped[list[float]] = mapped_column(JSON)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ToolGrant(TenantScoped, Base):
    __tablename__ = "tool_grants"
    __table_args__ = (UniqueConstraint("tenant_id", "tool", name="uq_tool_grant"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tool: Mapped[str] = mapped_column(String(80))
    enabled: Mapped[bool] = mapped_column(Boolean)


class AgentRun(TenantScoped, Base):
    __tablename__ = "agent_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'waiting_approval', "
            "'succeeded', 'failed', 'cancelled')",
            name="ck_agent_run_status",
        ),
        Index("ix_agent_runs_claim", "status", "lease_until", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    actor_id: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24))
    version: Mapped[int] = mapped_column(default=1)
    input: Mapped[dict] = mapped_column(JSON)
    output: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    budget_id: Mapped[str] = mapped_column(String(96))
    reserved_cost: Mapped[int] = mapped_column(BigInteger)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    attempts: Mapped[int] = mapped_column(default=0)
    # Only the current lease holder can commit results after crash recovery.
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class RunEvent(TenantScoped, Base):
    __tablename__ = "run_events"
    __table_args__ = (Index("ix_run_events_run_id_id", "run_id", "id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ToolReceipt(TenantScoped, Base):
    __tablename__ = "tool_receipts"
    __table_args__ = (
        UniqueConstraint("run_id", "tool", name="uq_tool_receipt_run_tool"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.id"))
    tool: Mapped[str] = mapped_column(String(80))
    result: Mapped[dict] = mapped_column(JSON)


class Feedback(TenantScoped, Base):
    __tablename__ = "feedback"
    __table_args__ = (
        UniqueConstraint("run_id", "actor_id", name="uq_feedback_actor_run"),
        CheckConstraint("score >= 1 AND score <= 5", name="ck_feedback_score"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.id"))
    actor_id: Mapped[str] = mapped_column(String(64))
    score: Mapped[int] = mapped_column()
    comment: Mapped[str] = mapped_column(Text)


class Evaluation(TenantScoped, Base):
    __tablename__ = "evaluations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    actor_id: Mapped[str] = mapped_column(String(64))
    results: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
