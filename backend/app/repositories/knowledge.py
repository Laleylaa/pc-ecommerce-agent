from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import KnowledgeDocument


class KnowledgeRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def list_documents(self, limit: int = 500) -> list[KnowledgeDocument]:
        stmt = select(KnowledgeDocument).order_by(KnowledgeDocument.id).limit(limit)
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_documents_by_ids(self, document_ids: list[int]) -> list[KnowledgeDocument]:
        if not document_ids:
            return []

        stmt = select(KnowledgeDocument).where(KnowledgeDocument.id.in_(document_ids))
        rows = list((await self.session.execute(stmt)).scalars().all())
        by_id = {document.id: document for document in rows}
        return [by_id[document_id] for document_id in document_ids if document_id in by_id]

    async def mark_indexed(
        self, document: KnowledgeDocument, collection_name: str, chroma_id: str
    ) -> None:
        document.chroma_collection = collection_name
        document.chroma_id = chroma_id
        await self.session.flush()
