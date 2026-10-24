from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backendfastapi.db.models import Document


async def retrieve(session: AsyncSession, query: str, limit: int = 3) -> list[Document]:
    if session.bind.dialect.name == "postgresql":
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
