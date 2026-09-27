from argparse import ArgumentParser
from pathlib import Path

from app.tools.knowledge import (
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_KNOWLEDGE_DOCUMENT_PATH,
    DEFAULT_KNOWLEDGE_VECTOR_INDEX_PATH,
    build_knowledge_vector_index,
)


def main() -> None:
    parser = ArgumentParser(description="Build local vector index for tool knowledge docs.")
    parser.add_argument(
        "--documents",
        type=Path,
        default=DEFAULT_KNOWLEDGE_DOCUMENT_PATH,
        help="Path to knowledge_documents.json.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_KNOWLEDGE_VECTOR_INDEX_PATH,
        help="Path to write knowledge_vector_index.json.",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_EMBEDDING_MODEL,
        help="SentenceTransformer embedding model name.",
    )
    args = parser.parse_args()

    index = build_knowledge_vector_index(
        document_path=args.documents,
        index_path=args.output,
        embedding_model=args.model,
    )
    print(
        f"Built {args.output} with {len(index.chunks)} chunks "
        f"using {index.embedding_model}."
    )


if __name__ == "__main__":
    main()
