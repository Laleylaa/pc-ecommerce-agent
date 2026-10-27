from pydantic import BaseModel, Field


class LocalKnowledgeDocument(BaseModel):
    id: int
    title: str
    document_type: str
    content: str
    metadata: dict = Field(default_factory=dict)

    @property
    def metadata_json(self) -> dict:
        return self.metadata
