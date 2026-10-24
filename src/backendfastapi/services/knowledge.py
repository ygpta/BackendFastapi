import asyncio
import hashlib
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from backendfastapi.db.models import Document, DocumentChunk, EmbeddingCache
from backendfastapi.services.controls import insert_for

CHUNK_SIZE = 1200
CHUNK_OVERLAP = 150


@dataclass(frozen=True)
class RetrievedDocument:
    id: str
    title: str
    content: str


def split_document(content: str) -> list[str]:
    normalized = " ".join(content.split())
    chunks = []
    start = 0
    while start < len(normalized):
        end = min(start + CHUNK_SIZE, len(normalized))
        if end < len(normalized):
            boundary = normalized.rfind(" ", start + CHUNK_SIZE // 2, end)
            if boundary > start:
                end = boundary
        chunks.append(normalized[start:end])
        if end == len(normalized):
            break
        start = max(end - CHUNK_OVERLAP, start + 1)
    return chunks


async def embed_document(
    session: AsyncSession,
    document: Document,
    provider,
    embedding_model: str,
    provider_semaphore: asyncio.Semaphore | None = None,
) -> None:
    chunks = split_document(document.content)
    if provider_semaphore is None:
        vectors = await provider.aembed_documents(chunks)
    else:
        async with provider_semaphore:
            vectors = await provider.aembed_documents(chunks)
    if len(vectors) != len(chunks) or any(len(vector) != 1536 for vector in vectors):
        raise ValueError("embedding_provider_returned_invalid_dimensions")
    await session.execute(
        delete(DocumentChunk).where(
            DocumentChunk.document_id == document.id,
            DocumentChunk.tenant_id == document.tenant_id,
        )
    )
    session.add_all(
        [
            DocumentChunk(
                id=hashlib.sha256(f"{document.id}:{index}".encode()).hexdigest(),
                tenant_id=document.tenant_id,
                document_id=document.id,
                chunk_index=index,
                content=chunk,
                embedding=list(vector),
                embedding_model=embedding_model,
            )
            for index, (chunk, vector) in enumerate(zip(chunks, vectors, strict=True))
        ]
    )
    document.embedding = vectors[0] if vectors else None
    document.embedding_model = embedding_model if vectors else None


async def retrieve(
    session: AsyncSession,
    query: str,
    limit: int = 3,
    embedding_provider: Callable[[str], Awaitable[Sequence[float]]] | None = None,
    embedding_model: str = "text-embedding-3-small",
    provider_semaphore: asyncio.Semaphore | None = None,
) -> list[RetrievedDocument]:
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
                if provider_semaphore is None:
                    embedding = list(await embedding_provider(query))
                else:
                    async with provider_semaphore:
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
            candidate_limit = min(max(limit * 8, 20), 80)
            vector = func.to_tsvector("english", DocumentChunk.content)
            search = func.plainto_tsquery("english", query)
            semantic = (
                select(
                    DocumentChunk.document_id.label("document_id"),
                    DocumentChunk.id.label("chunk_id"),
                    func.row_number()
                    .over(
                        order_by=(
                            DocumentChunk.embedding.cosine_distance(embedding),
                            DocumentChunk.id,
                        )
                    )
                    .label("rank"),
                )
                .where(
                    DocumentChunk.tenant_id == tenant_id,
                    DocumentChunk.embedding.is_not(None),
                    DocumentChunk.embedding_model == embedding_model,
                )
                .order_by(
                    DocumentChunk.embedding.cosine_distance(embedding),
                    DocumentChunk.id,
                )
                .limit(candidate_limit)
                .cte("semantic_candidates")
            )
            lexical = (
                select(
                    DocumentChunk.document_id.label("document_id"),
                    DocumentChunk.id.label("chunk_id"),
                    func.row_number()
                    .over(
                        order_by=(func.ts_rank(vector, search).desc(), DocumentChunk.id)
                    )
                    .label("rank"),
                )
                .where(
                    DocumentChunk.tenant_id == tenant_id,
                    vector.op("@@")(search),
                )
                .order_by(func.ts_rank(vector, search).desc(), DocumentChunk.id)
                .limit(candidate_limit)
                .cte("lexical_candidates")
            )
            candidates = union_all(
                select(semantic.c.document_id, semantic.c.chunk_id, semantic.c.rank),
                select(lexical.c.document_id, lexical.c.chunk_id, lexical.c.rank),
            ).cte("retrieval_candidates")
            fused = (
                select(
                    candidates.c.document_id,
                    candidates.c.chunk_id,
                    func.sum(1.0 / (60 + candidates.c.rank)).label("score"),
                )
                .group_by(candidates.c.document_id, candidates.c.chunk_id)
                .cte("fused_candidates")
            )
            reranked = select(
                fused.c.document_id,
                fused.c.chunk_id,
                func.row_number()
                .over(
                    partition_by=fused.c.document_id,
                    order_by=(fused.c.score.desc(), fused.c.chunk_id),
                )
                .label("document_rank"),
                func.row_number()
                .over(order_by=(fused.c.score.desc(), fused.c.chunk_id))
                .label("global_rank"),
            ).cte("reranked_candidates")
            hybrid = (
                select(
                    Document.id.label("document_id"),
                    Document.title,
                    DocumentChunk.content,
                    reranked.c.global_rank,
                )
                .join(
                    reranked,
                    reranked.c.document_id == Document.id,
                )
                .join(DocumentChunk, DocumentChunk.id == reranked.c.chunk_id)
                .where(
                    Document.tenant_id == tenant_id,
                    reranked.c.document_rank == 1,
                )
                .order_by(reranked.c.global_rank, Document.id)
                .limit(limit)
            )
            rows = (await session.execute(hybrid)).all()
            results = [
                RetrievedDocument(
                    id=row.document_id,
                    title=row.title,
                    content=row.content,
                )
                for row in rows
            ]
            if len(results) < limit:
                legacy = (
                    select(Document)
                    .where(
                        Document.tenant_id == tenant_id,
                        vector.op("@@")(search),
                        ~select(DocumentChunk.id)
                        .where(DocumentChunk.document_id == Document.id)
                        .exists(),
                    )
                    .order_by(func.ts_rank(vector, search).desc(), Document.id)
                    .limit(limit - len(results))
                )
                results.extend(
                    RetrievedDocument(
                        id=document.id,
                        title=document.title,
                        content=document.content,
                    )
                    for document in (await session.scalars(legacy)).all()
                )
            if results:
                return results
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
    documents = list((await session.scalars(statement.limit(limit))).all())
    return [
        RetrievedDocument(id=row.id, title=row.title, content=row.content)
        for row in documents
    ]
