from app.models import KnowledgeDocument
from app.services.knowledge_documents import read_documents
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


async def load_policies(session: AsyncSession) -> int:
    """Import missing documents; refuse to overwrite a conflicting local document."""
    documents = read_documents()
    for source in documents:
        existing = (await session.execute(
            select(KnowledgeDocument).where(KnowledgeDocument.title == source.title)
        )).scalar_one_or_none()
        if existing:
            if (existing.content, existing.document_type, existing.metadata_json) != (
                source.content, source.document_type, source.metadata,
            ):
                raise ValueError(f"Knowledge document conflicts with demo source: {source.title}")
            continue
        session.add(KnowledgeDocument(
            title=source.title, document_type=source.document_type,
            content=source.content, metadata_json=source.metadata,
        ))
    return len(documents)
