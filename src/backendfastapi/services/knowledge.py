import hashlib
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from backendfastapi.db.models import Document, EmbeddingCache
from backendfastapi.services.controls import insert_for


async def retrieve(
    session: AsyncSession,
    query: str,
    limit: int = 3,
    embedding_provider: Callable[[str], Awaitable[Sequence[float]]] | None = None,
    embedding_model: str = "text-embedding-3-small",
) -> list[Document]:
    if session.bind.dialect.name == "postgresql":
        if embedding_provider is not None:
            tenant_id = session.info.get("tenant_id", "development")
            cache_id = hashlib.sha256(
                f"{tenant_id}:{embedding_model}:{query}".encode()
            ).hexdigest()
            now = datetime.now(UTC)
            await session.execute(
                delete(EmbeddingCache).where(
                    EmbeddingCache.tenant_id == tenant_id,
                    EmbeddingCache.expires_at <= now,
                )
            )
            cached = await session.get(EmbeddingCache, cache_id)
            if cached is not None and cached.expires_at > now:
                embedding = cached.embedding
            else:
                embedding = list(await embedding_provider(query))
                await session.execute(
                    insert_for(session, EmbeddingCache)
                    .values(
                        id=cache_id,
                        tenant_id=tenant_id,
                        embedding=embedding,
                        expires_at=now + timedelta(days=7),
                    )
                    .on_conflict_do_update(
                        index_elements=["id"],
                        set_={
                            "embedding": embedding,
                            "expires_at": now + timedelta(days=7),
                        },
                    )
                )
            candidate_limit = min(max(limit * 4, 10), 40)
            semantic = (
                select(
                    Document.id.label("document_id"),
                    func.row_number()
                    .over(
                        order_by=(
                            Document.embedding.cosine_distance(embedding),
                            Document.id,
                        )
                    )
                    .label("rank"),
                )
                .where(
                    Document.tenant_id == tenant_id,
                    Document.embedding.is_not(None),
                    Document.embedding_model == embedding_model,
                )
                .limit(candidate_limit)
                .cte("semantic_candidates")
            )
            vector = func.to_tsvector("english", Document.content)
            search = func.plainto_tsquery("english", query)
            lexical = (
                select(
                    Document.id.label("document_id"),
                    func.row_number()
                    .over(
                        order_by=(
                            func.ts_rank(vector, search).desc(),
                            Document.id,
                        )
                    )
                    .label("rank"),
                )
                .where(
                    Document.tenant_id == tenant_id,
                    vector.op("@@")(search),
                )
                .limit(candidate_limit)
                .cte("lexical_candidates")
            )
            candidates = union_all(
                select(semantic.c.document_id, semantic.c.rank),
                select(lexical.c.document_id, lexical.c.rank),
            ).cte("retrieval_candidates")
            fused = (
                select(
                    candidates.c.document_id,
                    func.sum(1.0 / (60 + candidates.c.rank)).label("score"),
                )
                .group_by(candidates.c.document_id)
                .cte("fused_candidates")
            )
            hybrid = (
                select(Document)
                .join(fused, fused.c.document_id == Document.id)
                .where(Document.tenant_id == tenant_id)
                .order_by(fused.c.score.desc(), Document.id)
                .limit(limit)
            )
            return list((await session.scalars(hybrid)).all())
        vector = func.to_tsvector("english", Document.content)
        search = func.plainto_tsquery("english", query)
        statement = (
            select(Document)
            .where(vector.op("@@")(search))
            .order_by(func.ts_rank(vector, search).desc(), Document.id)
        )
    else:
        # SQLite is only a test fallback; production uses PostgreSQL full-text search.
        statement = (
            select(Document)
            .where(Document.content.icontains(query, autoescape=True))
            .order_by(Document.id)
        )
    return list((await session.scalars(statement.limit(limit))).all())
