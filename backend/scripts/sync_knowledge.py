import asyncio

from app.core.config import get_settings
from app.core.database import AsyncSessionLocal
from app.services.knowledge_rag import ChromaKnowledgeService


async def main() -> None:
    async with AsyncSessionLocal() as session:
        count = await ChromaKnowledgeService(session, get_settings()).sync()
        await session.commit()
    print(f"Synced {count} knowledge documents to ChromaDB.")


if __name__ == "__main__":
    asyncio.run(main())
