"""Isolated runtime checks: no real model, files, network or database required."""

import asyncio
import threading
import time

import pytest
from app.tools.contracts import BoundTool, DefaultToolContractProvider, execute_bound_tool
from app.tools.knowledge import (
    KnowledgeRetrievalToolService,
    KnowledgeVectorIndex,
    KnowledgeVectorIndexChunk,
    LocalKnowledgeDocument,
)


def retrieval_service(provider):
    return KnowledgeRetrievalToolService(
        documents=[LocalKnowledgeDocument(
            id=1, title="退货政策", document_type="policy", content="退货需要订单号。",
        )],
        embedding_provider=provider,
        vector_index=KnowledgeVectorIndex(
            version=1, embedding_provider="sentence_transformers", embedding_model="test",
            documents_hash="fixture", chunk_size=420, chunk_overlap=80, query_instruction="",
            chunks=[KnowledgeVectorIndexChunk(
                document_id=1, chunk_id="1:0", text="退货政策\npolicy\n\n退货需要订单号。",
                embedding=[1.0, 0.0],
            )],
        ),
    )


@pytest.mark.asyncio
async def test_sync_embedding_obeys_formal_timeout_without_blocking_loop():
    finished = threading.Event()

    class SlowEmbedding:
        model_name = "test"

        def embed_query(self, text):
            try:
                time.sleep(0.15)
                return [1.0, 0.0]
            finally:
                finished.set()

    contract = DefaultToolContractProvider().get_contract("policy_search")
    contract = contract.model_copy(update={"timeout_seconds": 0.02})
    beats = []

    async def heartbeat():
        await asyncio.sleep(0.005)
        beats.append(not finished.is_set())

    beat = asyncio.create_task(heartbeat())
    try:
        result = await execute_bound_tool(
            BoundTool(contract, retrieval_service(SlowEmbedding()).search_policy),
            {"query": "我想退货，需要提供什么？"}, {"user_id": 7},
        )
        await beat
        assert not result.ok and result.error.code == "dependency_unavailable"
        assert result.error.next_action == "retry"
        assert beats == [True]
        assert not finished.is_set()
    finally:
        await beat
        await asyncio.to_thread(finished.wait, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_timeout_or_cancel_keeps_worker_slot_until_real_work_finishes(cancel):
    from app.tools.knowledge import _RETRIEVAL_EXECUTOR

    entered, release = threading.Event(), threading.Event()
    calls = []

    class BlockingEmbedding:
        model_name = "test"

        def embed_query(self, text):
            calls.append(text)
            entered.set()
            assert release.wait(2), "test must release worker"
            return [1.0, 0.0]

    service = retrieval_service(BlockingEmbedding())
    contract = DefaultToolContractProvider().get_contract("policy_search")
    contract = contract.model_copy(update={"timeout_seconds": 0.03 if not cancel else 1})

    async def execute():
        return await execute_bound_tool(
            BoundTool(contract, service.search_policy), {"query": "拆封了能退吗"}, {"user_id": 7},
        )

    running = asyncio.create_task(execute())
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        if cancel:
            running.cancel()
            with pytest.raises(asyncio.CancelledError):
                await running
        else:
            result = await running
            assert result.error.code == "dependency_unavailable"
            assert result.error.next_action == "retry"
        busy = await execute()
        assert not busy.ok and busy.error.code == "dependency_unavailable"
        assert busy.error.next_action == "stop"
        assert len(calls) == 1
    finally:
        release.set()
        await asyncio.gather(running, return_exceptions=True)
        # A barrier after the submitted worker also waits for its release callback.
        await asyncio.wrap_future(_RETRIEVAL_EXECUTOR.submit(lambda: None))
    recovered = await execute()
    assert recovered.ok and recovered.output["documents"][0]["source_id"] == 1
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_failed_worker_releases_capacity():
    class BrokenEmbedding:
        model_name = "test"

        def embed_query(self, text):
            raise OSError("synthetic model unavailable")

    contract = DefaultToolContractProvider().get_contract("policy_search")
    for _ in range(2):
        result = await execute_bound_tool(
            BoundTool(contract, retrieval_service(BrokenEmbedding()).search_policy),
            {"query": "退货政策"}, {"user_id": 7},
        )
        assert result.error.code == "dependency_unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize("empty", [False, True])
async def test_bm25_never_loads_vector_files_or_embedding(monkeypatch, empty):
    from app.tools.schemas import DocumentSearchInput

    def forbidden(*args):
        raise AssertionError("BM25 must not read an index or load a model")

    monkeypatch.setattr("app.tools.knowledge._load_vector_index", forbidden)
    monkeypatch.setattr("app.tools.knowledge._load_local_documents", forbidden)
    provider = type("Provider", (), {"model_name": "test", "embed_query": forbidden})()
    service = retrieval_service(provider)
    if empty:
        service._documents = []
    result = await service.search_policy(DocumentSearchInput(query="退货", retrieval_mode="bm25"))
    assert result.search_strategy == "bm25"
    assert result.result_type == ("empty" if empty else "documents")


@pytest.mark.asyncio
async def test_missing_index_reports_actual_bm25_fallback(monkeypatch):
    from app.tools.schemas import DocumentSearchInput

    monkeypatch.setattr("app.tools.knowledge._load_vector_index", lambda *args: None)
    service = retrieval_service(type("Provider", (), {"model_name": "test"})())
    service._vector_index = None
    result = await service.search_policy(DocumentSearchInput(query="退货需要什么"))
    assert result.search_strategy == "bm25"
    assert result.documents[0].snippet == "退货需要订单号。"


def test_request_never_downloads_model_but_explicit_preparation_can(monkeypatch):
    from app.tools.knowledge import (
        SentenceTransformerEmbeddingProvider,
        _load_sentence_transformer_model,
    )

    calls = []
    sentinel = object()

    def load(name, **kwargs):
        calls.append(kwargs)
        if kwargs.get("local_files_only"):
            raise OSError("model not cached")
        return sentinel

    monkeypatch.setattr("sentence_transformers.SentenceTransformer", load)
    _load_sentence_transformer_model.cache_clear()
    try:
        with pytest.raises(OSError):
            SentenceTransformerEmbeddingProvider("test")._get_model()
        assert calls == [{"local_files_only": True}]
        prepared = SentenceTransformerEmbeddingProvider("test", allow_download=True)
        assert prepared._get_model() is sentinel
        assert calls == [{"local_files_only": True}, {"local_files_only": True}, {}]
    finally:
        _load_sentence_transformer_model.cache_clear()
