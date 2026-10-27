"""Read the same local knowledge source for retrieval, indexing and demo import."""

import json
from pathlib import Path

from app.schemas.knowledge import LocalKnowledgeDocument

DEFAULT_KNOWLEDGE_DOCUMENT_PATH = (
    Path(__file__).resolve().parents[2] / "data" / "knowledge_documents.json"
)


def read_documents(path: Path = DEFAULT_KNOWLEDGE_DOCUMENT_PATH) -> list[LocalKnowledgeDocument]:
    return [
        LocalKnowledgeDocument.model_validate(item)
        for item in json.loads(path.read_text(encoding="utf-8"))
    ]
