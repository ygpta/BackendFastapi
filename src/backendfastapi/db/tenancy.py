"""Session hooks that enforce tenant filters and immutable-record rules.

Learn: authentication identifies a user; authorization limits which rows they can use.
Follow api/deps.py into these hooks, then read test_tenant_isolation_and_roles.
"""

from sqlalchemy import event
from sqlalchemy.orm import Session, with_loader_criteria

from backendfastapi.db.models import AuditEvent, PromptVersion, TenantScoped


@event.listens_for(Session, "do_orm_execute")
def scope_queries(execution_state):
    """Apply the authenticated tenant to ORM queries, including aliased models."""
    tenant_id = execution_state.session.info.get("tenant_id")
    # This guards scoped ORM sessions, not arbitrary raw SQL or privileged sessions.
    if tenant_id and execution_state.is_orm_statement:
        execution_state.statement = execution_state.statement.options(
            with_loader_criteria(
                TenantScoped,
                lambda model: model.tenant_id == tenant_id,
                include_aliases=True,
            )
        )


@event.listens_for(Session, "before_flush")
def scope_writes(session, flush_context, instances):
    """Stamp new records and reject cross-tenant or immutable-record changes."""
    for record in session.dirty.union(session.deleted):
        if isinstance(record, AuditEvent):
            raise ValueError("Audit records are append-only")
        if isinstance(record, PromptVersion) and record in session.dirty:
            from sqlalchemy import inspect

            if inspect(record).attrs.content.history.has_changes():
                raise ValueError("Prompt content is immutable; create a new version")
    tenant_id = session.info.get("tenant_id")
    if not tenant_id:
        return
    for record in session.new.union(session.dirty).union(session.deleted):
        if isinstance(record, TenantScoped):
            if record.tenant_id is None and record in session.new:
                record.tenant_id = tenant_id
            if record.tenant_id != tenant_id:
                raise ValueError("Cross-tenant write rejected")
