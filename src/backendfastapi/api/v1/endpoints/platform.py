from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backendfastapi.api.deps import get_db_session
from backendfastapi.core.config import Settings, get_settings
from backendfastapi.core.identity import Principal, get_principal, require_admin
from backendfastapi.db.models import (
    AuditEvent,
    Budget,
    Document,
    Evaluation,
    Feedback,
    PromptVersion,
    Tenant,
    ToolGrant,
)
from backendfastapi.repositories.audit import audit
from backendfastapi.schemas.platform import (
    BudgetInput,
    DocumentInput,
    DocumentResponse,
    EvaluationInput,
    FeedbackInput,
    GrantInput,
    PromptInput,
    PromptResponse,
)
from backendfastapi.services.controls import insert_for
from backendfastapi.services.knowledge import (
    embed_document as persist_document_embeddings,
)
from backendfastapi.services.knowledge import (
    retrieve,
)
from backendfastapi.services.runs import get_run

router = APIRouter(tags=["agent platform"])
Session = Annotated[AsyncSession, Depends(get_db_session)]
Actor = Annotated[Principal, Depends(get_principal)]
Admin = Annotated[Principal, Depends(require_admin)]


@router.post("/knowledge/documents", response_model=DocumentResponse, status_code=201)
async def add_document(
    data: DocumentInput,
    session: Session,
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
):
    embedding_client = getattr(request.app.state, "embedding_client", None)
    record = Document(id=uuid4().hex, **data.model_dump())
    session.add(record)
    await session.flush()
    if embedding_client is not None:
        await persist_document_embeddings(
            session,
            record,
            embedding_client,
            settings.openai_embedding_model,
            getattr(request.app.state, "provider_semaphore", None),
        )
    audit(session, "document.created", record.id)
    await session.commit()
    return record


@router.get("/knowledge/documents", response_model=list[DocumentResponse])
async def list_documents(
    session: Session, limit: Annotated[int, Query(ge=1, le=100)] = 20
):
    return list(
        (
            await session.scalars(select(Document).order_by(Document.id).limit(limit))
        ).all()
    )


@router.get("/knowledge/search", response_model=list[DocumentResponse])
async def search_documents(
    session: Session,
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    query: Annotated[str, Query(min_length=1, max_length=500)],
    limit: Annotated[int, Query(ge=1, le=10)] = 3,
):
    embedding_client = getattr(request.app.state, "embedding_client", None)
    results = await retrieve(
        session,
        query,
        limit,
        embedding_client.aembed_query if embedding_client else None,
        settings.openai_embedding_model,
        getattr(request.app.state, "provider_semaphore", None),
    )
    await session.commit()
    return results


@router.delete("/knowledge/documents/{document_id}", status_code=204)
async def remove_document(document_id: str, session: Session):
    document = await session.get(Document, document_id)
    if document is None:
        raise HTTPException(404, "Document not found")
    await session.delete(document)
    audit(session, "document.deleted", document_id)
    await session.commit()
    return Response(status_code=204)


@router.post(
    "/knowledge/documents/{document_id}/embed",
    response_model=DocumentResponse,
)
async def embed_document(
    document_id: str,
    session: Session,
    request: Request,
    admin: Admin,
    settings: Annotated[Settings, Depends(get_settings)],
):
    embedding_client = getattr(request.app.state, "embedding_client", None)
    if embedding_client is None:
        raise HTTPException(503, "Embedding provider is not configured")
    document = await session.get(Document, document_id)
    if document is None:
        raise HTTPException(404, "Document not found")
    await persist_document_embeddings(
        session,
        document,
        embedding_client,
        settings.openai_embedding_model,
        getattr(request.app.state, "provider_semaphore", None),
    )
    audit(session, "document.embedded", document.id)
    await session.commit()
    return document


@router.post("/prompts", response_model=PromptResponse, status_code=201)
async def create_prompt(data: PromptInput, session: Session, admin: Admin):
    record = PromptVersion(id=uuid4().hex, **data.model_dump(), active=False)
    session.add(record)
    audit(session, "prompt.created", record.id)
    await session.commit()
    return record


@router.get("/prompts", response_model=list[PromptResponse])
async def list_prompts(
    session: Session, limit: Annotated[int, Query(ge=1, le=100)] = 20
):
    return list(
        (
            await session.scalars(
                select(PromptVersion)
                .order_by(PromptVersion.created_at.desc(), PromptVersion.id)
                .limit(limit)
            )
        ).all()
    )


@router.post("/prompts/{prompt_id}/activate", response_model=PromptResponse)
async def activate_prompt(prompt_id: str, session: Session, admin: Admin):
    # The tenant lock serializes activation so two replicas cannot select two defaults.
    await session.scalar(
        select(Tenant).where(Tenant.id == admin.tenant_id).with_for_update()
    )
    prompt = await session.get(PromptVersion, prompt_id)
    if prompt is None:
        raise HTTPException(404, "Prompt not found")
    await session.execute(update(PromptVersion).values(active=False))
    prompt.active = True
    audit(session, "prompt.activated", prompt_id)
    await session.commit()
    return prompt


@router.put("/tools/{tool}")
async def grant_tool(
    tool: Literal["resolve_ticket"], data: GrantInput, session: Session, admin: Admin
):
    await session.execute(
        insert_for(session, ToolGrant)
        .values(
            id=uuid4().hex,
            tenant_id=admin.tenant_id,
            tool=tool,
            enabled=data.enabled,
        )
        .on_conflict_do_update(
            index_elements=["tenant_id", "tool"], set_={"enabled": data.enabled}
        )
    )
    audit(session, "tool.grant_changed", tool, {"enabled": data.enabled})
    await session.commit()
    return {"tool": tool, "enabled": data.enabled}


@router.get("/usage")
async def usage(
    session: Session, actor: Actor, settings: Annotated[Settings, Depends(get_settings)]
):
    period = datetime.now(UTC).strftime("%Y-%m")
    record = await session.get(Budget, f"{actor.tenant_id}:{period}")
    return {
        "period": period,
        "currency": "USD",
        "unit": "microusd",
        "allowance": record.allowance if record else settings.monthly_budget_microusd,
        "spent": record.spent if record else 0,
        "reserved": record.reserved if record else 0,
    }


@router.put("/usage/budget")
async def set_budget(data: BudgetInput, session: Session, admin: Admin):
    budget_id = f"{admin.tenant_id}:{datetime.now(UTC):%Y-%m}"
    await session.execute(
        insert_for(session, Budget)
        .values(
            id=budget_id,
            tenant_id=admin.tenant_id,
            allowance=data.allowance_microusd,
            spent=0,
            reserved=0,
        )
        .on_conflict_do_nothing(index_elements=["id"])
    )
    changed = await session.scalar(
        update(Budget)
        .where(
            Budget.id == budget_id,
            Budget.spent + Budget.reserved <= data.allowance_microusd,
        )
        .values(allowance=data.allowance_microusd)
        .returning(Budget.id)
    )
    if changed is None:
        raise HTTPException(409, "Allowance is below spent plus reserved funds")
    audit(
        session,
        "budget.changed",
        budget_id,
        {"allowance_microusd": data.allowance_microusd},
    )
    await session.commit()
    return {"id": budget_id, "allowance": data.allowance_microusd}


@router.get("/audit")
async def audit_events(
    session: Session,
    admin: Admin,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    before_id: Annotated[int | None, Query(ge=1)] = None,
):
    query = select(AuditEvent)
    if before_id is not None:
        query = query.where(AuditEvent.id < before_id)
    records = (
        await session.scalars(query.order_by(AuditEvent.id.desc()).limit(limit))
    ).all()
    return [
        {
            "id": row.id,
            "actor_id": row.actor_id,
            "action": row.action,
            "resource_id": row.resource_id,
            "details": row.details,
            "created_at": row.created_at,
        }
        for row in records
    ]


@router.post("/feedback", status_code=201)
async def feedback(data: FeedbackInput, session: Session, actor: Actor):
    run = await get_run(session, data.run_id)
    if run.status != "succeeded":
        raise HTTPException(409, "Feedback requires a successful run")
    await session.execute(
        insert_for(session, Feedback)
        .values(
            id=uuid4().hex,
            tenant_id=actor.tenant_id,
            actor_id=actor.user_id,
            **data.model_dump(),
        )
        .on_conflict_do_update(
            index_elements=["run_id", "actor_id"],
            set_={"score": data.score, "comment": data.comment},
        )
    )
    audit(session, "feedback.recorded", run.id, {"score": data.score})
    await session.commit()
    return {"run_id": run.id, "score": data.score}


@router.post("/evaluations", status_code=201)
async def evaluate(data: EvaluationInput, session: Session, admin: Admin):
    results = []
    for case in data.cases:
        run = await get_run(session, case.run_id)
        output = run.output or {}
        text = output.get("text", "").casefold()
        passed = (
            run.status == "succeeded"
            and all(item.casefold() in text for item in case.required)
            and all(item.casefold() not in text for item in case.forbidden)
            and (not case.require_citations or bool(output.get("citations")))
        )
        results.append(
            {
                "run_id": run.id,
                "prompt_version": run.input["prompt_version"],
                "passed": passed,
                "specification": case.model_dump(),
            }
        )
    record = Evaluation(
        id=uuid4().hex,
        name=data.name,
        actor_id=admin.user_id,
        results={
            "cases": results,
            "passed": sum(row["passed"] for row in results),
            "total": len(results),
        },
    )
    session.add(record)
    audit(session, "evaluation.completed", record.id)
    await session.commit()
    return {"id": record.id, **record.results}


@router.get("/evaluations/{evaluation_id}")
async def get_evaluation(evaluation_id: str, session: Session, admin: Admin):
    record = await session.get(Evaluation, evaluation_id)
    if record is None:
        raise HTTPException(404, "Evaluation not found")
    return {"id": record.id, "name": record.name, **record.results}
