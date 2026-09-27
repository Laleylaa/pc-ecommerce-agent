import asyncio
import json
import re
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from pydantic import BaseModel, ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

import app.tools.catalog as catalog_tools
import app.tools.knowledge as knowledge_tools
import app.tools.orders as order_tools
from app.core.config import Settings
from app.models import Sku, Spu
from app.repositories.catalog import CatalogRepository
from app.repositories.orders import OrderSearchPage
from app.schemas.catalog import ProductCard, ProductSearchRequest
from app.schemas.order import LogisticsCard, OrderCard, OrderItemCard
from app.tools.catalog import (
    CatalogComparePlan,
    CatalogToolService,
    LLMCatalogQueryPlanner,
    ProductQueryPlan,
    RuleBasedCatalogQueryPlanner,
    validate_catalog_compare_plan,
    validate_catalog_sql,
    validate_product_query_plan,
)
from app.tools.contracts import (
    BoundTool,
    DefaultToolContractProvider,
    RegistryToolExecutor,
    ToolCatalog,
    build_catalog_planner,
    build_order_planner,
    execute_bound_tool,
)
from app.tools.knowledge import (
    KnowledgeRetrievalToolService,
    KnowledgeVectorIndex,
    KnowledgeVectorIndexChunk,
)
from app.tools.registry import build_tool_registry
from app.tools.schemas import (
    CatalogCompareInput,
    CatalogDependencyArtifact,
    CatalogFacetInput,
    CatalogSearchInput,
    CatalogSearchOutput,
    CatalogToolRuntimeContext,
    DocumentSearchInput,
    OrderLookupInput,
)

TOOL_TEST_SETTINGS = Settings(llm_api_key="", catalog_llm_planner_enabled=False)


def test_catalog_exclusions_filter_brand_and_usage_matches() -> None:
    products = [
        ProductCard(
            spu_id=1,
            sku_id=11,
            title="Logitech Office Mouse",
            brand="Logitech",
            category="mouse",
            price="199.00",
            stock=5,
            sales_count=10,
            specs={"usage": "office"},
        ),
        ProductCard(
            spu_id=2,
            sku_id=22,
            title="Razer Gaming Mouse",
            brand="Razer",
            category="mouse",
            price="299.00",
            stock=5,
            sales_count=20,
            specs={"usage": "gaming"},
        ),
    ]

    assert [
        item.sku_id
        for item in catalog_tools._filter_excluded_preferences(products, ["Logitech"], [])
    ] == [22]
    assert [
        item.sku_id for item in catalog_tools._filter_excluded_preferences(products, [], ["gaming"])
    ] == [11]


def test_spu_aggregation_preserves_real_variant_combinations() -> None:
    products = [
        ProductCard(
            spu_id=1,
            sku_id=11,
            title="Series A Wired Black",
            brand="Brand A",
            category="mouse",
            price="100.00",
            stock=8,
            sales_count=500,
            specs={
                "connection_type": "Wired",
                "color": "Black",
                "max_dpi": "26000",
            },
        ),
        ProductCard(
            spu_id=1,
            sku_id=12,
            title="Series A Bluetooth Blue",
            brand="Brand A",
            category="mouse",
            price="120.00",
            stock=0,
            sales_count=500,
            specs={
                "connection_type": "Bluetooth",
                "color": "Blue",
                "max_dpi": "26000",
            },
        ),
        ProductCard(
            spu_id=2,
            sku_id=21,
            title="Series B Bluetooth White",
            brand="Brand B",
            category="mouse",
            price="130.00",
            stock=3,
            sales_count=400,
            specs={
                "connection_type": "Bluetooth",
                "color": "White",
                "max_dpi": "35000",
            },
        ),
    ]

    series = catalog_tools._aggregate_product_series(
        products,
        [1, 2],
        {1: "Series A", 2: "Series B"},
    )
    fields = catalog_tools._series_comparison_fields(series)
    differences = catalog_tools._series_pair_differences(series, fields)

    assert series[0].common_specs == {"max_dpi": "26000"}
    assert [
        value.value for value in series[0].option_specs["connection_type"].values
    ] == ["Bluetooth", "Wired"]
    assert {
        (variant.specs["connection_type"], variant.specs["color"])
        for variant in series[0].variants
    } == {("Wired", "Black"), ("Bluetooth", "Blue")}
    connection_difference = next(
        item for item in differences[0].fields if item.field == "connection_type"
    )
    assert connection_difference.shared_values == ["Bluetooth"]
    assert connection_difference.left_only_values == ["Wired"]


@pytest.mark.asyncio
async def test_sku_recommendation_ranking_keeps_top_selling_sku_per_spu() -> None:
    products = [
        ProductCard(
            spu_id=1,
            sku_id=11,
            title="Series A top variant",
            brand="Brand A",
            category="mouse",
            price="199.00",
            stock=10,
            sku_sales_count=900,
            sales_count=1000,
            specs={"connection_type": "Wireless"},
        ),
        ProductCard(
            spu_id=1,
            sku_id=12,
            title="Series A second variant",
            brand="Brand A",
            category="mouse",
            price="189.00",
            stock=12,
            sku_sales_count=800,
            sales_count=1000,
            specs={"connection_type": "Wireless"},
        ),
        ProductCard(
            spu_id=2,
            sku_id=21,
            title="Series B top variant",
            brand="Brand B",
            category="mouse",
            price="209.00",
            stock=8,
            sku_sales_count=700,
            sales_count=900,
            specs={"connection_type": "Wireless"},
        ),
    ]

    class FakeRepository(CatalogRepository):
        async def _eligible_products_for_ranking(self, request):
            return list(products)

    page = await FakeRepository(SimpleNamespace()).search_products_with_total(
        ProductSearchRequest(query="", category="mouse", sort="sales", limit=2),
        distinct_spu=True,
    )

    assert [product.sku_id for product in page.products] == [11, 21]
    assert page.total_count == 2


@pytest.mark.asyncio
async def test_negative_brand_search_recalls_alternatives_before_filtering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    products = [
        ProductCard(
            spu_id=1,
            sku_id=11,
            title="Logitech Mouse",
            brand="Logitech",
            category="mouse",
            price="199.00",
            stock=5,
            sales_count=10,
            specs={},
        ),
        ProductCard(
            spu_id=2,
            sku_id=22,
            title="Razer Mouse",
            brand="Razer",
            category="mouse",
            price="299.00",
            stock=5,
            sales_count=20,
            specs={},
        ),
    ]
    captured = {}

    class FakeCatalogRepository:
        def __init__(self, session):
            pass

        async def search_products(self, request):
            captured["request"] = request
            if request.query:
                return []
            return products

        async def search_product_series(self, request):
            return await self.search_products(request)

        async def search_products_with_total(self, request):
            items = await self.search_products(request)
            return SimpleNamespace(products=items, total_count=len(items))

        async def search_product_series_with_total(self, request):
            items = await self.search_product_series(request)
            return SimpleNamespace(products=items, total_count=len(items))

    monkeypatch.setattr(catalog_tools, "CatalogRepository", FakeCatalogRepository)
    async def keep_fake_series(_service, items):
        return items

    monkeypatch.setattr(
        CatalogToolService,
        "_enrich_series_products",
        keep_fake_series,
    )
    service = CatalogToolService(SimpleNamespace())

    result = await service.search(
        CatalogSearchInput(
            query="不要 Logitech 游戏鼠标",
            category="鼠标",
            excluded_brands=["Logitech"],
            excluded_usage=["gaming"],
            limit=3,
        )
    )

    assert captured["request"].query == ""
    assert captured["request"].limit >= 12
    assert captured["request"].excluded_brands == ["Logitech"]
    assert captured["request"].excluded_usage == ["gaming"]
    assert [item.brand for item in result.products] == ["Razer"]


class FakeEmbeddingProvider:
    model_name = "fake-embedding"

    def embed_query(self, text: str) -> list[float]:
        lowered = text.lower()
        if "wooting" in lowered or "brand" in lowered:
            return [1.0, 0.0]
        if "return" in lowered or "refund" in lowered:
            return [0.0, 1.0]
        return [0.5, 0.5]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self.embed_query(text) for text in texts]


def test_sentence_transformer_model_is_shared_across_provider_instances(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = object()
    loads: list[tuple[str, bool]] = []

    def fake_sentence_transformer(model_name: str, *, local_files_only: bool = False):
        loads.append((model_name, local_files_only))
        return sentinel

    monkeypatch.setattr(
        "sentence_transformers.SentenceTransformer",
        fake_sentence_transformer,
    )
    knowledge_tools._load_sentence_transformer_model.cache_clear()
    try:
        first = knowledge_tools.SentenceTransformerEmbeddingProvider("test-shared-model")
        second = knowledge_tools.SentenceTransformerEmbeddingProvider("test-shared-model")

        assert first._get_model() is sentinel
        assert second._get_model() is sentinel
        assert loads == [("test-shared-model", True)]
    finally:
        knowledge_tools._load_sentence_transformer_model.cache_clear()


def _fake_vector_index() -> KnowledgeVectorIndex:
    return KnowledgeVectorIndex(
        version=1,
        embedding_provider="sentence_transformers",
        embedding_model=FakeEmbeddingProvider.model_name,
        documents_hash="test-documents",
        chunk_size=420,
        chunk_overlap=80,
        query_instruction="",
        chunks=[
            KnowledgeVectorIndexChunk(
                document_id=1,
                chunk_id="1:0",
                text="return refund after sales",
                embedding=[0.0, 1.0],
            ),
            KnowledgeVectorIndexChunk(
                document_id=5,
                chunk_id="5:0",
                text="Logitech Razer Wooting brand",
                embedding=[1.0, 0.0],
            ),
        ],
    )


def _knowledge_service() -> KnowledgeRetrievalToolService:
    return KnowledgeRetrievalToolService(
        embedding_provider=FakeEmbeddingProvider(),
        vector_index=_fake_vector_index(),
    )


class FakeCatalogPlanner:
    async def plan_search(self, request):
        return ProductQueryPlan(
            query=request.query,
            category="mouse",
            brands=["Razer"],
            filters={"connection_type": "wireless"},
            keywords=["Viper"],
            limit=request.limit,
            planner="fake",
        )

    async def plan_compare(self, request):
        return CatalogComparePlan(
            query=request.query,
            category="mouse",
            items=["G502", "Viper"],
            comparison_fields=["price", "stock", "max_dpi", "weight_g", "connection_type"],
            limit=request.limit,
            planner="fake",
        )


class BrokenCatalogPlanner:
    async def plan_search(self, request):
        raise ValueError("planner unavailable")

    async def plan_compare(self, request):
        raise ValueError("planner unavailable")


@pytest.mark.parametrize(
    ("query", "expected_purpose"),
    [
        ("推荐一个键盘", "recommendation"),
        ("有哪些无线键盘", "search"),
        ("这个键盘多少钱", "lookup"),
        ("K08 有哪些版本", "lookup"),
        ("销量第二的键盘", "ranking"),
        ("你最推荐哪个版本", "recommendation"),
    ],
)
@pytest.mark.asyncio
async def test_rule_catalog_planner_classifies_search_result_purpose(
    query: str,
    expected_purpose: str,
) -> None:
    plan = await RuleBasedCatalogQueryPlanner().plan_search(
        CatalogSearchInput(query=query)
    )

    assert plan.result_purpose == expected_purpose


class FakeChatResponse:
    def __init__(self, content: str):
        self.content = content


class FakeChatModel:
    def __init__(self, content: str):
        self.content = content
        self.calls = []

    async def ainvoke(self, messages):
        self.calls.append(messages)
        return FakeChatResponse(self.content)


class SequenceFakeChatModel:
    def __init__(self, contents: list[str]):
        self.contents = list(contents)
        self.calls = []

    async def ainvoke(self, messages):
        self.calls.append(messages)
        content = self.contents.pop(0)
        return FakeChatResponse(content)


@pytest.mark.asyncio
async def test_tool_registry_exposes_expected_business_tools(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    async with db_session_factory() as session:
        registry = build_tool_registry(session, settings=TOOL_TEST_SETTINGS)

    assert registry.tool_names == [
        "catalog.compare",
        "catalog.facets",
        "catalog.search",
        "knowledge.search",
        "order.lookup",
        "policy.search",
    ]


def test_tool_contracts_expose_llm_safe_metadata() -> None:
    contracts = DefaultToolContractProvider().list_contracts()
    names = [contract.llm_name for contract in contracts]

    assert names == [
        "catalog_search",
        "catalog_compare",
        "catalog_facets",
        "order_lookup",
        "policy_search",
        "knowledge_search",
    ]
    assert len(set(names)) == len(names)
    assert all(re.fullmatch(r"[a-zA-Z0-9_]+", name) for name in names)
    assert all(contract.description for contract in contracts)
    assert all(contract.read_only for contract in contracts)
    assert not any(contract.parallel_safe for contract in contracts)

    order_contract = DefaultToolContractProvider().get_contract("order_lookup")
    assert order_contract is not None
    assert order_contract.requires_auth is True
    assert order_contract.runtime_fields == ("user_id",)
    assert order_contract.public_input_model.model_json_schema()["additionalProperties"] is False
    assert "user_id" not in order_contract.public_input_model.model_json_schema()["properties"]


def test_tool_contracts_export_public_llm_schemas_only() -> None:
    contracts = DefaultToolContractProvider().list_contracts()

    for contract in contracts:
        schema = contract.as_llm_tool()
        assert schema["type"] == "function"
        assert schema["function"]["name"] == contract.llm_name
        assert schema["function"]["description"] == contract.description
        parameters = schema["function"]["parameters"]
        for runtime_field in contract.runtime_fields:
            assert runtime_field not in parameters.get("properties", {})


@pytest.mark.asyncio
async def test_tool_contracts_align_with_registry(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    async with db_session_factory() as session:
        registry = build_tool_registry(session, settings=TOOL_TEST_SETTINGS)

    contracts = DefaultToolContractProvider().list_contracts()
    assert sorted(contract.registry_name for contract in contracts) == registry.tool_names


def test_all_public_input_models_forbid_unknown_fields() -> None:
    contracts = DefaultToolContractProvider().list_contracts()

    for contract in contracts:
        schema = contract.public_input_model.model_json_schema()
        assert schema.get("additionalProperties") is False


def test_catalog_public_schemas_are_query_first() -> None:
    provider = DefaultToolContractProvider()
    search = provider.get_contract("catalog_search")
    compare = provider.get_contract("catalog_compare")
    facets = provider.get_contract("catalog_facets")
    assert search is not None
    assert compare is not None
    assert facets is not None

    search_fields = set(search.public_input_model.model_json_schema()["properties"])
    compare_fields = set(compare.public_input_model.model_json_schema()["properties"])
    facet_fields = set(facets.public_input_model.model_json_schema()["properties"])

    assert search_fields == {"query", "limit"}
    assert compare_fields == {"query", "limit"}
    assert facet_fields == {"query", "limit"}
    assert search.internal_input_model is CatalogSearchInput
    assert compare.internal_input_model is CatalogCompareInput
    assert search.runtime_fields == ("catalog_context",)
    assert compare.runtime_fields == ("catalog_context",)
    assert facets.runtime_fields == ("catalog_context",)


def test_catalog_compare_does_not_implicitly_mix_memory_into_dependency_context() -> None:
    context = CatalogToolRuntimeContext.model_validate(
        {
            "memory": {
                "query_plan": {"selection_scope": "spu"},
                "candidate_sku_ids": [101],
                "candidate_spu_ids": [10],
                "candidate_display": [
                    {
                        "sku_id": 101,
                        "spu_id": 10,
                        "title": "Current Mouse",
                        "entity_scope": "spu",
                    }
                ],
            },
            "dependency_artifacts": [
                {
                    "task_id": "task_1",
                    "artifact_type": "products",
                    "products": [
                        {
                            "sku_id": 202,
                            "spu_id": 20,
                            "title": "Second-ranked Mouse",
                            "brand": "E2E",
                            "category": "mouse",
                            "price": "299.00",
                            "stock": 10,
                            "sales_count": 800,
                            "sku_sales_count": 80,
                            "specs": {},
                        }
                    ],
                    "selection_scope": "spu",
                }
            ],
        }
    )

    targets, scope = catalog_tools._resolved_catalog_request_context(
        "和销量第二的鼠标对比一下",
        context,
    )

    assert [item.model_dump(mode="json") for item in targets] == [
        {
            "sku_id": 202,
            "spu_id": 20,
            "source": "current_turn_artifact",
        },
    ]
    assert scope == "spu"


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["catalog_search", "catalog_compare"])
async def test_catalog_contract_injects_trusted_context_after_public_validation(
    tool_name: str,
) -> None:
    contract = DefaultToolContractProvider().get_contract(tool_name)
    assert contract is not None
    captured_input: BaseModel | None = None

    async def handler(request):
        nonlocal captured_input
        captured_input = request
        if tool_name == "catalog_search":
            return CatalogSearchOutput(result_type="empty", ranking_strategy="fixture")
        from app.tools.schemas import CatalogCompareOutput

        return CatalogCompareOutput(result_type="empty")

    catalog_context = CatalogToolRuntimeContext(
        dependency_artifacts=[
            CatalogDependencyArtifact(
                task_id="task_1",
                selected_sku_ids=[101],
                selected_spu_ids=[10],
                selection_scope="sku",
            )
        ]
    )
    result = await execute_bound_tool(
        BoundTool(contract, handler),
        {"query": "测试目录查询", "limit": 3 if tool_name == "catalog_search" else 5},
        {
            "user_id": 7,
            "catalog_context": catalog_context,
        },
    )

    assert result.ok is True
    assert captured_input is not None
    assert captured_input.model_dump(mode="json")["catalog_context"][
        "dependency_artifacts"
    ] == [
        {
            "task_id": "task_1",
            "artifact_type": "",
            "products": [],
            "selected_sku_ids": [101],
            "selected_spu_ids": [10],
            "selection_scope": "sku",
            "comparison_level": None,
        }
    ]

    forged = await execute_bound_tool(
        BoundTool(contract, handler),
        {
            "query": "测试目录查询",
            "limit": 3 if tool_name == "catalog_search" else 5,
            "catalog_context": catalog_context.model_dump(mode="json"),
        },
        {"user_id": 7, "catalog_context": CatalogToolRuntimeContext()},
    )
    assert forged.ok is False
    assert forged.error is not None
    assert forged.error.code == "invalid_input"


def test_document_search_public_schema_fixes_hybrid_retrieval() -> None:
    provider = DefaultToolContractProvider()

    for name in ("policy_search", "knowledge_search"):
        contract = provider.get_contract(name)
        assert contract is not None
        fields = set(contract.public_input_model.model_json_schema()["properties"])
        assert fields == {"query", "document_type", "limit"}
        with pytest.raises(ValidationError):
            contract.public_input_model.model_validate(
                {"query": "退货政策", "retrieval_mode": "bm25"}
            )
        public_input = contract.public_input_model.model_validate({"query": "退货政策"})
        internal_input = contract.internal_input_model.model_validate(
            public_input.model_dump(mode="json", exclude_none=True)
        )
        assert internal_input.retrieval_mode == "hybrid"


def test_tool_catalog_rejects_duplicate_names_and_missing_handler() -> None:
    contract = DefaultToolContractProvider().get_contract("catalog_search")
    assert contract is not None

    async def handler(request: CatalogSearchInput) -> CatalogSearchOutput:
        return CatalogSearchOutput(result_type="empty")

    with pytest.raises(ValueError, match="duplicate tool llm_name"):
        ToolCatalog([BoundTool(contract, handler), BoundTool(contract, handler)])

    with pytest.raises(ValueError, match="missing handler"):
        ToolCatalog([BoundTool(contract, None)])  # type: ignore[arg-type]


def test_tool_catalog_rejects_handler_model_mismatch() -> None:
    contract = DefaultToolContractProvider().get_contract("catalog_search")
    assert contract is not None

    async def wrong_input_handler(request: DocumentSearchInput) -> CatalogSearchOutput:
        return CatalogSearchOutput(result_type="empty")

    async def wrong_output_handler(request: CatalogSearchInput) -> BaseModel:
        return CatalogSearchOutput(result_type="empty")

    with pytest.raises(ValueError, match="handler input model mismatch"):
        ToolCatalog([BoundTool(contract, wrong_input_handler)])

    with pytest.raises(ValueError, match="handler output model mismatch"):
        ToolCatalog([BoundTool(contract, wrong_output_handler)])


@pytest.mark.asyncio
async def test_registry_rejects_unknown_fields_without_calling_handler() -> None:
    async def never_called_handler(request):  # pragma: no cover
        raise AssertionError("handler should not be called")

    for contract in DefaultToolContractProvider().list_contracts():
        executor = RegistryToolExecutor(
            None,  # type: ignore[arg-type]
            TOOL_TEST_SETTINGS,
            catalog=ToolCatalog([BoundTool(contract, never_called_handler)]),
        )
        args = {"unexpected_field": "must fail"}
        if "query" in contract.public_input_model.model_json_schema().get("properties", {}):
            args["query"] = "test"
        result = await executor.execute(contract, args, {"user_id": 1})
        assert not result.ok
        assert result.error is not None
        assert result.error.code == "invalid_input"
        assert result.error.retryable is True
        assert result.error.recommended_action == "replan_arguments"


@pytest.mark.asyncio
async def test_registry_tool_executor_injects_runtime_and_rewrites_tool_name() -> None:
    captured_input = None
    contract = DefaultToolContractProvider().get_contract("order_lookup")
    assert contract is not None

    async def handler(request):
        nonlocal captured_input
        captured_input = request.model_dump(mode="json", exclude_none=True)
        from app.tools.schemas import OrderLookupOutput

        return OrderLookupOutput(result_type="not_found")

    executor = RegistryToolExecutor(
        None,  # type: ignore[arg-type]
        TOOL_TEST_SETTINGS,
        catalog=ToolCatalog([BoundTool(contract, handler)]),
    )

    result = await executor.execute(contract, {"order_id": 42}, {"user_id": 7})

    assert result.ok
    assert result.tool_name == "order_lookup"
    assert captured_input == {"user_id": 7, "order_id": 42, "limit": 5, "offset": 0}


@pytest.mark.asyncio
async def test_registry_tool_executor_rejects_llm_supplied_runtime_field() -> None:
    async def never_called_handler(request):  # pragma: no cover
        raise AssertionError("handler should not be called")

    contract = DefaultToolContractProvider().get_contract("order_lookup")
    assert contract is not None
    executor = RegistryToolExecutor(
        None,  # type: ignore[arg-type]
        TOOL_TEST_SETTINGS,
        catalog=ToolCatalog([BoundTool(contract, never_called_handler)]),
    )

    result = await executor.execute(
        contract,
        {"order_id": 42, "user_id": 999},
        {"user_id": 7},
    )

    assert not result.ok
    assert result.tool_name == "order_lookup"
    assert result.error is not None
    assert result.error.code == "invalid_input"


@pytest.mark.asyncio
async def test_registry_tool_executor_returns_timeout_error() -> None:
    contract = DefaultToolContractProvider().get_contract("catalog_search")
    assert contract is not None
    contract.timeout_seconds = 0.001

    async def slow_handler(request):
        await asyncio.sleep(0.05)
        raise AssertionError("timeout should cancel before completion")

    executor = RegistryToolExecutor(
        None,  # type: ignore[arg-type]
        TOOL_TEST_SETTINGS,
        catalog=ToolCatalog([BoundTool(contract, slow_handler)]),
    )

    result = await executor.execute(contract, {"query": "wireless mouse"}, {"user_id": 7})

    assert not result.ok
    assert result.tool_name == "catalog_search"
    assert result.error is not None
    assert result.error.code == "timeout"
    assert result.error.retryable is True
    assert result.error.recommended_action == "retry_once"


@pytest.mark.asyncio
async def test_registry_tool_executor_returns_execution_error_for_unknown_exception() -> None:
    contract = DefaultToolContractProvider().get_contract("catalog_search")
    assert contract is not None

    async def broken_handler(request):
        raise RuntimeError("postgresql://secret@localhost/internal")

    executor = RegistryToolExecutor(
        None,  # type: ignore[arg-type]
        TOOL_TEST_SETTINGS,
        catalog=ToolCatalog([BoundTool(contract, broken_handler)]),
    )

    result = await executor.execute(contract, {"query": "wireless mouse"}, {"user_id": 7})

    assert not result.ok
    assert result.error is not None
    assert result.error.code == "execution_error"
    assert result.error.message == "tool execution failed"
    assert result.error.retryable is False
    assert result.error.recommended_action == "stop"


@pytest.mark.asyncio
async def test_registry_tool_executor_returns_dependency_unavailable() -> None:
    contract = DefaultToolContractProvider().get_contract("catalog_search")
    assert contract is not None

    async def unavailable_handler(request):
        raise SQLAlchemyError("postgresql://secret@localhost/internal")

    executor = RegistryToolExecutor(
        None,  # type: ignore[arg-type]
        TOOL_TEST_SETTINGS,
        catalog=ToolCatalog([BoundTool(contract, unavailable_handler)]),
    )

    result = await executor.execute(contract, {"query": "wireless mouse"}, {"user_id": 7})

    assert not result.ok
    assert result.error is not None
    assert result.error.code == "dependency_unavailable"
    assert result.error.message == "tool dependency is temporarily unavailable"
    assert result.error.retryable is True
    assert result.error.recommended_action == "explain_temporary_unavailability"


def test_stable_tool_error_codes_have_recovery_semantics() -> None:
    from app.tools.contracts import _error_result

    expected = {
        "unknown_tool": (False, "stop"),
        "invalid_input": (True, "replan_arguments"),
        "unauthorized": (False, "request_authentication"),
        "forbidden": (False, "stop"),
        "timeout": (True, "retry_once"),
        "dependency_unavailable": (True, "explain_temporary_unavailability"),
        "execution_error": (False, "stop"),
    }

    for code, (retryable, recommended_action) in expected.items():
        result = _error_result("catalog_search", code)
        assert result.error is not None
        assert result.error.code == code
        assert result.error.retryable is retryable
        assert result.error.recommended_action == recommended_action


@pytest.mark.asyncio
async def test_catalog_search_returns_ranked_wireless_mouse_top_results(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    async with db_session_factory() as session:
        result = await build_tool_registry(session, settings=TOOL_TEST_SETTINGS).execute(
            "catalog.search",
            {"query": "Codex wireless mouse", "limit": 3},
        )

    assert result.ok
    assert result.output is not None
    assert result.output["result_type"] == "products"
    assert result.output["selection_scope"] == "spu"
    assert result.output["ranking_strategy"] == "spu_match_score_sales_stock_price"
    assert result.output["products"][0]["title"] == "Razer Codex Viper V3 Pro White"
    assert result.output["products"][0]["entity_scope"] == "spu"
    assert "Wireless" in result.output["products"][0]["specs"]["connection_type"]


@pytest.mark.asyncio
async def test_purchase_ready_wireless_mouse_recommendation_returns_distinct_skus(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    async with db_session_factory() as session:
        result = await CatalogToolService(session).search(
            CatalogSearchInput(
                query="帮我推荐两款无线鼠标，给出价格、库存和主要规格",
                limit=2,
            )
        )

    assert result.result_type == "products"
    assert result.result_purpose == "recommendation"
    assert result.selection_scope == "sku"
    assert result.query_plan["sort"] == "sales"
    assert result.ranking_strategy == "sales"
    assert len({product.spu_id for product in result.products}) == len(result.products)
    assert all(product.entity_scope == "sku" for product in result.products)
    assert all(
        any(
            marker in product.specs["connection_type"].casefold()
            for marker in ("wireless", "bluetooth", "wifi", "无线", "蓝牙", "2.4g")
        )
        for product in result.products
    )
    assert [
        product.sku_sales_count for product in result.products
    ] == sorted(
        (product.sku_sales_count for product in result.products),
        reverse=True,
    )


@pytest.mark.parametrize(
    "query",
    [
        "推荐一个键盘",
        "推荐一个版本的键盘",
    ],
)
@pytest.mark.asyncio
async def test_catalog_recommendation_selects_one_spu_not_one_sku(
    db_session_factory: Callable[[], AsyncSession],
    query: str,
) -> None:
    async with db_session_factory() as session:
        result = await CatalogToolService(session).search(
            CatalogSearchInput(query=query, limit=3)
        )
        assert result.products
        active_variants = (
            (
                await session.execute(
                    select(Sku).where(
                        Sku.spu_id == result.products[0].spu_id,
                        Sku.status == 1,
                    )
                )
            )
            .scalars()
            .all()
        )

    assert result.result_type == "products"
    assert result.result_purpose == "recommendation"
    assert result.selection_scope == "spu"
    assert result.query_plan["selection_scope"] == "spu"
    assert result.query_plan["result_purpose"] == "recommendation"
    assert len(result.products) == 1
    assert result.returned_count == 1
    assert result.total_match_count >= result.returned_count
    assert result.is_exhaustive is (
        result.total_match_count == result.returned_count
    )
    product = result.products[0]
    assert product.entity_scope == "spu"
    assert product.spu_title
    assert product.series_min_price is not None
    assert product.series_max_price is not None
    assert product.series_total_stock is not None
    assert product.series_sku_count == len(product.series_variants)
    assert product.series_sku_count == len(active_variants)
    assert product.series_total_stock == sum(max(0, sku.stock) for sku in active_variants)


@pytest.mark.asyncio
async def test_catalog_context_variant_listing_selects_skus_inside_one_spu(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    async with db_session_factory() as session:
        spu_id = (
            await session.execute(
                select(Sku.spu_id)
                .join(Spu, Sku.spu_id == Spu.id)
                .where(Sku.status == 1, Spu.status == 1)
                .group_by(Sku.spu_id)
                .having(func.count(Sku.id) >= 2)
                .order_by(Sku.spu_id)
                .limit(1)
            )
        ).scalar_one()
        result = await CatalogToolService(session).search(
            CatalogSearchInput(
                query="查看这个商品所有版本",
                targets=[
                    {
                        "spu_id": spu_id,
                        "source": "working_memory_reference",
                    }
                ],
                limit=5,
            )
        )

    assert result.selection_scope == "sku"
    assert result.result_purpose == "lookup"
    assert result.query_plan["selection_scope"] == "sku"
    assert result.returned_count == len(result.products)
    assert result.total_match_count >= result.returned_count
    assert result.is_exhaustive is (
        result.total_match_count == result.returned_count
    )
    assert len(result.products) >= 2
    assert {product.spu_id for product in result.products} == {spu_id}
    assert all(product.entity_scope == "sku" for product in result.products)


@pytest.mark.asyncio
async def test_catalog_context_recommendation_returns_ordered_sku_window_with_exact_total(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    async with db_session_factory() as session:
        row = (
            await session.execute(
                select(Sku.spu_id, func.count(Sku.id))
                .join(Spu, Sku.spu_id == Spu.id)
                .where(Sku.status == 1, Spu.status == 1)
                .group_by(Sku.spu_id)
                .having(func.count(Sku.id) >= 4)
                .order_by(func.count(Sku.id).desc(), Sku.spu_id)
                .limit(1)
            )
        ).one()
        spu_id, active_variant_count = row
        result = await CatalogToolService(session).search(
            CatalogSearchInput(
                query="你最推荐哪个版本",
                targets=[
                    {
                        "spu_id": spu_id,
                        "source": "working_memory_reference",
                    }
                ],
                limit=3,
            )
        )

    assert result.result_purpose == "recommendation"
    assert result.selection_scope == "sku"
    assert result.ranking_strategy == "sales"
    assert result.total_match_count == active_variant_count
    assert result.returned_count == 3
    assert result.is_exhaustive is False
    assert len(result.products) == 3
    assert {product.spu_id for product in result.products} == {spu_id}
    assert all(product.entity_scope == "sku" for product in result.products)
    assert result.products[0].sku_sales_count == max(
        product.sku_sales_count for product in result.products
    )


@pytest.mark.parametrize(
    ("query", "expected_sort", "expected_title"),
    [
        ("哪款鼠标卖的最贵", "price_desc", "Razer Codex Viper V3 Pro White"),
        ("哪款鼠标最便宜", "price_asc", "Logitech 影刃 M01 游戏鼠标 黑色标准版"),
    ],
)
@pytest.mark.asyncio
async def test_catalog_search_returns_price_extrema(
    db_session_factory: Callable[[], AsyncSession],
    query: str,
    expected_sort: str,
    expected_title: str,
) -> None:
    async with db_session_factory() as session:
        result = await CatalogToolService(session).search(CatalogSearchInput(query=query, limit=1))

    assert result.query_plan["sort"] == expected_sort
    assert result.result_purpose == "ranking"
    assert result.returned_count == 1
    assert result.total_match_count >= 1
    assert result.ranking_strategy == f"spu_{expected_sort}"
    assert len(result.products) == 1
    assert result.products[0].ranking_scope == "spu"
    assert result.products[0].ranking_metric == "price"
    assert result.products[0].title == expected_title


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "expected_connection", "expected_resolution", "expected_result_type"),
    [
        ("有没有 USB-C、60fps 的摄像头？", "USB-C", None, "products"),
        ("有没有 USB-A、4K、60fps 的摄像头？", "USB-A", "4K", "products"),
        ("有没有 4K、USB-C、60fps 的摄像头？", "USB-C", "4K", "empty"),
    ],
)
async def test_catalog_search_preserves_explicit_connection_details(
    db_session_factory: Callable[[], AsyncSession],
    query: str,
    expected_connection: str,
    expected_resolution: str | None,
    expected_result_type: str,
) -> None:
    async with db_session_factory() as session:
        result = await CatalogToolService(session).search(
            CatalogSearchInput(query=query, limit=3)
        )

    assert result.query_plan["filters"]["connection_type"] == expected_connection
    assert result.result_type == expected_result_type
    if expected_result_type == "empty":
        assert result.products == []
        return

    assert result.products
    assert all(
        product.specs["connection_type"] == expected_connection
        for product in result.products
    )
    assert all(product.specs["frame_rate"] == "60fps" for product in result.products)
    if expected_resolution is not None:
        assert all(
            product.specs["resolution"] == expected_resolution
            for product in result.products
        )


@pytest.mark.asyncio
async def test_catalog_search_explicit_near_match_relaxes_exactly_one_filter(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    async with db_session_factory() as session:
        result = await CatalogToolService(session).search(
            CatalogSearchInput(
                query="在摄像头里推荐接近 4K、USB-C、60fps 的款",
                limit=3,
            )
        )

    assert result.result_type == "products"
    assert result.result_purpose == "recommendation"
    assert result.selection_scope == "sku"
    assert result.ranking_strategy == "near_match_single_filter_balanced"
    assert result.products
    relaxation = result.query_plan["relaxation"]
    assert relaxation["applied"] is True
    assert relaxation["selection_strategy"] == "balanced_across_relaxed_filters"
    assert relaxation["within_dimension_sort"] == "sales"
    assert relaxation["exact_match_count"] == 0
    assert relaxation["requested_filters"] == {
        "connection_type": "USB-C",
        "resolution": "4K",
        "frame_rate": "60fps",
    }
    differences = relaxation["candidate_differences"]
    assert set(differences) == {str(product.sku_id) for product in result.products}
    assert all(len(differences[str(product.sku_id)]) == 1 for product in result.products)
    assert {next(iter(differences[str(product.sku_id)])) for product in result.products} >= {
        "connection_type",
        "resolution",
    }
    assert all(product.specs["frame_rate"] == "60fps" for product in result.products)
    assert any(
        product.specs["connection_type"] == "USB-C"
        and product.specs["resolution"] != "4K"
        for product in result.products
    )
    assert any(
        product.specs["connection_type"] != "USB-C"
        and product.specs["resolution"] == "4K"
        for product in result.products
    )


@pytest.mark.asyncio
async def test_catalog_search_uses_hidden_identity_to_rank_variants_inside_one_spu(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    async with db_session_factory() as session:
        spu_id = (
            await session.execute(
                select(Sku.spu_id)
                .join(Spu, Sku.spu_id == Spu.id)
                .where(Sku.status == 1, Spu.status == 1)
                .group_by(Sku.spu_id)
                .having(
                    func.count(Sku.id) >= 2,
                    func.min(Sku.price) < func.max(Sku.price),
                )
                .order_by(Sku.spu_id)
                .limit(1)
            )
        ).scalar_one_or_none()
        assert spu_id is not None
        variants = (
            (
                await session.execute(
                    select(Sku)
                    .where(Sku.spu_id == spu_id, Sku.status == 1)
                    .order_by(Sku.price.asc(), Sku.id.asc())
                )
            )
            .scalars()
            .all()
        )
        assert len(variants) >= 2
        expected = variants[0]
        referenced = variants[-1]
        contract = DefaultToolContractProvider().get_contract("catalog_search")
        assert contract is not None

        execution = await RegistryToolExecutor(session, TOOL_TEST_SETTINGS).execute(
            contract,
            {
                "query": "这个商品系列中最便宜的 SKU 版本",
                "limit": 3,
            },
                {
                    "user_id": 7,
                    "catalog_context": {
                        "memory": {
                            "referenced_sku_id": referenced.id,
                            "referenced_spu_id": spu_id,
                        },
                        "dependency_artifacts": [],
                    },
                },
        )

    assert execution.ok is True
    assert execution.output is not None
    assert execution.output["ranking_strategy"] == "sku_price_asc"
    assert execution.output["query_plan"]["ranking"] == {
        "scope": "sku",
        "metric": "price",
        "direction": "asc",
        "rank": 1,
        "count": 1,
    }
    assert [product["sku_id"] for product in execution.output["products"]] == [
        expected.id
    ]
    assert {product["spu_id"] for product in execution.output["products"]} == {
        spu_id
    }


@pytest.mark.asyncio
async def test_catalog_search_uses_query_plan_and_guard(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    async with db_session_factory() as session:
        result = await CatalogToolService(session, planner=FakeCatalogPlanner()).search(
            CatalogSearchInput(query="Find a wireless Razer mouse", limit=3)
        )

    assert result.result_type == "products"
    assert result.query_plan["planner"] == "fake"
    assert result.query_plan["brands"] == ["Razer"]
    assert result.products[0].brand == "Razer"


@pytest.mark.asyncio
async def test_llm_catalog_planner_parses_guarded_json() -> None:
    chat = FakeChatModel(
        """
        {
          "category": "mouse",
          "brands": ["Logitech"],
          "max_price": 300,
          "filters": {"wireless": "wireless"},
          "keywords": ["fps"],
          "sort": "recommend",
          "supported": true,
          "unsupported_reason": null
        }
        """
    )
    planner = LLMCatalogQueryPlanner(chat)

    plan = await planner.plan_search(CatalogSearchInput(query="FPS mouse under 300", limit=3))

    assert plan.planner == "llm"
    assert plan.query == "FPS mouse under 300"
    assert plan.category == "mouse"
    assert plan.brands == ["Logitech"]
    assert plan.max_price == 300
    assert plan.filters == {"wireless": "wireless"}
    assert chat.calls


@pytest.mark.asyncio
async def test_catalog_search_enforces_explicit_count_and_sales_ranking(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    chat = FakeChatModel(
        '{"category":"monitor","brands":[],"filters":{},'
        '"keywords":[],"sort":"recommend","supported":true,"unsupported_reason":null}'
    )
    async with db_session_factory() as session:
        result = await CatalogToolService(
            session,
            planner=LLMCatalogQueryPlanner(chat),
        ).search(
            CatalogSearchInput(
                query="查找销量最高的两款显示器并进行对比",
                limit=5,
            )
        )

    assert result.query_plan["sort"] == "sales"
    assert result.query_plan["limit"] == 2
    assert len(result.products) == 2
    assert result.products[0].sales_count >= result.products[1].sales_count


@pytest.mark.asyncio
async def test_llm_catalog_planner_applies_explicit_overrides() -> None:
    chat = FakeChatModel(
        '{"category":"keyboard","brands":["Razer"],"filters":{},'
        '"keywords":[],"sort":"recommend","supported":true,"unsupported_reason":null}'
    )
    planner = LLMCatalogQueryPlanner(chat)

    plan = await planner.plan_search(
        CatalogSearchInput(
            query="wireless gear",
            category="mouse",
            brand="Logitech",
            filters={"connection_type": "wireless"},
            limit=3,
        )
    )

    assert plan.category == "mouse"
    assert plan.brands == ["Logitech"]
    assert plan.filters == {"connection_type": "wireless"}


@pytest.mark.asyncio
async def test_rule_catalog_planner_fills_missing_fields_from_typed_preferences() -> None:
    from app.tools.catalog import RuleBasedCatalogQueryPlanner

    request = CatalogSearchInput(
        query="推荐鼠标",
        category="mouse",
        preference_defaults={
            "brands": ["Logitech"],
            "max_price": 500,
            "connection_type": "Wireless",
            "usage": "gaming",
        },
    )

    plan = await RuleBasedCatalogQueryPlanner().plan_search(request)

    assert plan.brands == ["Logitech"]
    assert plan.max_price == 500
    assert plan.filters["connection_type"] == "Wireless"
    assert plan.usage_scenario == "gaming"


@pytest.mark.asyncio
async def test_rule_catalog_planner_recognizes_sales_rank_wording() -> None:
    from app.tools.catalog import RuleBasedCatalogQueryPlanner

    plan = await RuleBasedCatalogQueryPlanner().plan_search(
        CatalogSearchInput(query="查询键盘品类销量排名第二的商品", limit=6)
    )

    assert plan.sort == "sales"
    assert plan.limit == 1
    assert plan.ranking is not None
    assert plan.ranking.model_dump() == {
        "scope": "spu",
        "metric": "sales",
        "direction": "desc",
        "rank": 2,
        "count": 1,
    }


@pytest.mark.parametrize(
    ("query", "expected_scope", "expected_metric", "expected_rank", "expected_count"),
    [
        ("最便宜的键盘", "spu", "price", 1, 1),
        ("最便宜的键盘 SKU 版本", "sku", "price", 1, 1),
        ("最便宜的红轴键盘", "spu", "price", 1, 1),
        ("红轴版本销量最高的键盘", "sku", "sales", 1, 1),
        ("库存最多的三个鼠标", "spu", "stock", 1, 3),
        ("库存最少的鼠标", "spu", "stock", 1, 1),
        ("销量排名第二的键盘", "spu", "sales", 2, 1),
        ("销量最低的键盘 SKU", "sku", "sales", 1, 1),
        ("SKU 销量前 5 名", "sku", "sales", 1, 5),
    ],
)
@pytest.mark.asyncio
async def test_rule_catalog_planner_infers_ranking_scope_and_window(
    query: str,
    expected_scope: str,
    expected_metric: str,
    expected_rank: int,
    expected_count: int,
) -> None:
    from app.tools.catalog import RuleBasedCatalogQueryPlanner

    plan = await RuleBasedCatalogQueryPlanner().plan_search(
        CatalogSearchInput(query=query, limit=3)
    )

    assert plan.ranking is not None
    assert plan.ranking.scope == expected_scope
    assert plan.ranking.metric == expected_metric
    assert plan.ranking.rank == expected_rank
    assert plan.ranking.count == expected_count
    assert plan.limit == expected_count
    if ("最少" in query or "最低" in query) and expected_metric != "price":
        assert plan.ranking.direction == "asc"


@pytest.mark.parametrize(
    ("query", "expected_scope"),
    [
        ("推荐一个键盘", "spu"),
        ("推荐一个版本的键盘", "spu"),
        ("推荐一个黑色红轴键盘", "sku"),
        ("推荐两款无线鼠标，给出价格、库存和主要规格", "sku"),
        ("K08 多少钱", "spu"),
        ("K08 黑色版本多少钱", "sku"),
        ("查看这个键盘所有版本", "sku"),
    ],
)
@pytest.mark.asyncio
async def test_rule_catalog_planner_infers_ordinary_selection_scope(
    query: str,
    expected_scope: str,
) -> None:
    from app.tools.catalog import RuleBasedCatalogQueryPlanner

    plan = await RuleBasedCatalogQueryPlanner().plan_search(
        CatalogSearchInput(query=query)
    )

    assert plan.selection_scope == expected_scope


@pytest.mark.parametrize(
    ("query", "expected_sort"),
    [
        ("推荐两个鼠标", "sales"),
        ("推荐两个无线鼠标，给出价格、库存和主要规格", "sales"),
        ("推荐最便宜的无线鼠标", "price_asc"),
        ("推荐库存最多的键盘", "stock"),
    ],
)
@pytest.mark.asyncio
async def test_rule_catalog_recommendation_defaults_to_sales_unless_order_is_explicit(
    query: str,
    expected_sort: str,
) -> None:
    plan = await RuleBasedCatalogQueryPlanner().plan_search(
        CatalogSearchInput(query=query)
    )

    assert plan.sort == expected_sort


@pytest.mark.parametrize(
    ("query", "expected_level"),
    [
        ("比较 G502 和 Viper 两个鼠标型号", "spu"),
        ("比较这两个 SKU 版本", "sku"),
        ("比较黑色红轴版本和白色蓝牙版本", "sku"),
        ("比较红轴键盘和茶轴键盘", "spu"),
        ("比较这两个鼠标系列的全部版本", "spu"),
    ],
)
@pytest.mark.asyncio
async def test_rule_catalog_compare_planner_infers_scope_from_query(
    query: str,
    expected_level: str,
) -> None:
    from app.tools.catalog import RuleBasedCatalogQueryPlanner

    plan = await RuleBasedCatalogQueryPlanner().plan_compare(
        CatalogCompareInput(query=query)
    )

    assert plan.comparison_level == expected_level


@pytest.mark.parametrize(
    ("query", "expected_sort"),
    [
        ("哪款鼠标卖的最贵", "price_desc"),
        ("价格最高的鼠标", "price_desc"),
        ("哪款鼠标最便宜", "price_asc"),
        ("价格最低的鼠标", "price_asc"),
    ],
)
@pytest.mark.asyncio
async def test_rule_catalog_planner_recognizes_price_extrema(
    query: str,
    expected_sort: str,
) -> None:
    from app.tools.catalog import RuleBasedCatalogQueryPlanner

    plan = await RuleBasedCatalogQueryPlanner().plan_search(
        CatalogSearchInput(query=query, limit=1)
    )

    assert plan.sort == expected_sort


@pytest.mark.asyncio
async def test_llm_catalog_planner_cannot_reject_supported_sales_rank() -> None:
    chat = FakeChatModel(
        '{"category":"keyboard","brands":[],"filters":{},"keywords":[],'
        '"sort":"sales","supported":false,'
        '"unsupported_reason":"Only sorting is supported"}'
    )
    planner = LLMCatalogQueryPlanner(chat)

    plan = await planner.plan_search(
        CatalogSearchInput(query="查询键盘品类中销量排名第二的SPU商品", limit=20)
    )

    assert plan.supported is True
    assert plan.unsupported_reason is None
    assert plan.sort == "sales"


@pytest.mark.asyncio
async def test_llm_catalog_search_keeps_semantic_scope_and_purpose_for_ambiguous_filters() -> None:
    chat = FakeChatModel(
        '{"category":"keyboard","brands":[],"filters":{"connection_type":"Wireless"},'
        '"keywords":[],"requested_fields":["product_identity"],"sort":"recommend",'
        '"result_purpose":"search","selection_scope":"spu","ranking":null,'
        '"supported":true,"unsupported_reason":null}'
    )

    plan = await LLMCatalogQueryPlanner(chat).plan_search(
        CatalogSearchInput(query="有哪些无线键盘", limit=5)
    )

    assert plan.result_purpose == "search"
    assert plan.selection_scope == "spu"
    assert plan.filters == {"connection_type": "Wireless"}


@pytest.mark.asyncio
async def test_catalog_search_whitelist_rejects_unknown_requested_field() -> None:
    chat = FakeChatModel(
        '{"category":"keyboard","brands":[],"filters":{},"keywords":[],'
        '"requested_fields":["product_identity","gross_margin"],"sort":"recommend",'
        '"result_purpose":"lookup","selection_scope":"spu","ranking":null,'
        '"supported":true,"unsupported_reason":null}'
    )

    plan = validate_product_query_plan(
        await LLMCatalogQueryPlanner(chat).plan_search(
            CatalogSearchInput(query="查询键盘的毛利信息", limit=3)
        )
    )

    assert plan.supported is False
    assert plan.unsupported_reason == "unsupported catalog fields: gross_margin"


def test_catalog_search_rejects_non_whitelisted_ranking_metric() -> None:
    plan = validate_product_query_plan(
        ProductQueryPlan(
            query="碳足迹最低的键盘是哪款",
            category="keyboard",
            requested_fields=["product_identity", "price"],
            result_purpose="ranking",
            ranking={
                "scope": "spu",
                "metric": "price",
                "direction": "asc",
                "rank": 1,
                "count": 1,
            },
        )
    )

    assert plan.supported is False
    assert plan.unsupported_reason == "unsupported catalog ranking metric: 碳足迹"


@pytest.mark.asyncio
async def test_catalog_compare_whitelist_rejects_unknown_requested_field() -> None:
    chat = FakeChatModel(
        '{"category":"mouse","items":["G502","Viper"],"brands":[],'
        '"comparison_fields":["price","repairability_score"],"scenario":null,'
        '"comparison_level":"spu","supported":true,"unsupported_reason":null}'
    )

    plan = validate_catalog_compare_plan(
        await LLMCatalogQueryPlanner(chat).plan_compare(
            CatalogCompareInput(query="比较 G502 和 Viper 的价格和可维修评分")
        )
    )

    assert plan.supported is False
    assert plan.unsupported_reason == (
        "unsupported comparison fields: repairability_score"
    )


@pytest.mark.asyncio
async def test_catalog_compare_whitelist_accepts_valid_fields_despite_llm_opinion() -> None:
    chat = FakeChatModel(
        '{"category":"speaker","items":[],"brands":["Bose","JBL"],'
        '"comparison_fields":["connection_type"],"scenario":null,'
        '"comparison_level":"spu","supported":false,'
        '"unsupported_reason":"No explicit fields to compare"}'
    )

    plan = validate_catalog_compare_plan(
        await LLMCatalogQueryPlanner(chat).plan_compare(
            CatalogCompareInput(query="Bose 和 JBL 的蓝牙音箱有什么区别")
        )
    )

    assert plan.supported is True
    assert plan.unsupported_reason is None


@pytest.mark.asyncio
async def test_catalog_search_explicit_variant_listing_is_a_safe_sku_fast_path() -> None:
    chat = FakeChatModel(
        '{"category":"mouse","brands":["Logitech"],"filters":{},"keywords":["G502"],'
        '"requested_fields":["variants","price"],"sort":"recommend",'
        '"result_purpose":"lookup","selection_scope":"spu","ranking":null,'
        '"supported":true,"unsupported_reason":null}'
    )

    plan = await LLMCatalogQueryPlanner(chat).plan_search(
        CatalogSearchInput(query="Logitech G502 有哪些可售版本和价格")
    )

    assert plan.result_purpose == "lookup"
    assert plan.selection_scope == "sku"


@pytest.mark.asyncio
async def test_catalog_facets_uses_llm_for_dimension_and_scope() -> None:
    chat = FakeChatModel(
        '{"facet":"spec_value","category":"keyboard","brand":null,'
        '"spec_key":"switches","min_price":null,"max_price":null,"filters":{},'
        '"count_scope":"spu","supported":true,"unsupported_reason":null}'
    )
    service = CatalogToolService(None, planner=LLMCatalogQueryPlanner(chat))  # type: ignore[arg-type]

    plan = await service._safe_plan_facets(
        CatalogFacetInput(query="键盘都能选什么轴体", limit=20)
    )

    assert plan.planner == "llm"
    assert plan.facet == "spec_value"
    assert plan.category == "keyboard"
    assert plan.spec_key == "switches"
    assert plan.count_scope == "spu"


@pytest.mark.asyncio
async def test_catalog_planner_receives_resolved_working_memory_scope_hint() -> None:
    chat = FakeChatModel(
        '{"category":null,"items":[],"brands":[],"comparison_fields":["price"],'
        '"scenario":null,"comparison_level":"sku","supported":true,'
        '"unsupported_reason":null}'
    )
    context = CatalogToolRuntimeContext.model_validate(
        {
            "memory": {
                "query_plan": {"selection_scope": "sku"},
                "candidate_display": [
                    {
                        "spu_id": 10,
                        "sku_id": 101,
                        "title": "First",
                        "entity_scope": "sku",
                    },
                    {
                        "spu_id": 11,
                        "sku_id": 102,
                        "title": "Second",
                        "entity_scope": "sku",
                    },
                ],
            }
        }
    )

    await LLMCatalogQueryPlanner(chat).plan_compare(
        CatalogCompareInput(
            query="对比第一款和第二款的价格",
            catalog_context=context,
        )
    )

    payload = json.loads(chat.calls[0][1].content)
    assert payload["catalog_context"]["trusted_target_count"] == 2
    assert payload["catalog_context"]["scope_hint"] == "sku"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "expected_usage"),
    [
        ("开会用的摄像头", "video_meeting"),
        ("直播摄像头", "live_streaming"),
    ],
)
async def test_llm_catalog_planner_fills_common_usage_when_model_omits_it(
    query: str, expected_usage: str
) -> None:
    chat = FakeChatModel(
        '{"category":"webcam","brands":[],"filters":{},'
        '"keywords":[],"sort":"recommend","supported":true,"unsupported_reason":null}'
    )
    planner = LLMCatalogQueryPlanner(chat)

    plan = await planner.plan_search(CatalogSearchInput(query=query, limit=3))

    assert plan.usage_scenario == expected_usage
    assert expected_usage in chat.calls[0][0].content


def test_usage_scenario_alias_is_normalized_and_mapped_to_concrete_specs() -> None:
    plan = validate_product_query_plan(
        ProductQueryPlan(
            query="video conference webcam",
            category="webcam",
            usage_scenario="video conferencing",
        )
    )

    mapped = catalog_tools._apply_usage_scenario_mapping(plan)
    request = catalog_tools._plan_to_product_search(mapped)

    assert mapped.usage_scenario == "video_meeting"
    assert mapped.usage_mapping["status"] == "applied"
    assert mapped.usage_mapping["source"] == "deterministic_spec_mapping"
    assert request.usage_scenario is None
    assert request.usage_required_conditions[0].key == "microphone"
    assert request.usage_required_conditions[0].values == ["是"]


def test_unconfigured_usage_category_combination_is_explicitly_unsupported() -> None:
    plan = validate_product_query_plan(
        ProductQueryPlan(
            query="办公鼠标",
            category="mouse",
            usage_scenario="office",
        )
    )

    mapped = catalog_tools._apply_usage_scenario_mapping(plan)

    assert mapped.supported is False
    assert mapped.usage_mapping == {
        "status": "unavailable",
        "rule_version": "v1",
        "scenario": "office",
        "category": "mouse",
    }


@pytest.mark.asyncio
async def test_llm_catalog_planner_fills_missing_high_confidence_query_constraints() -> None:
    chat = FakeChatModel(
        '{"category":"mouse","brands":["Logitech"],"filters":{},'
        '"keywords":[],"sort":"recommend","supported":true,"unsupported_reason":null}'
    )
    planner = LLMCatalogQueryPlanner(chat)

    plan = await planner.plan_search(
        CatalogSearchInput(query="wireless mouse under 300 from Logitech", limit=3)
    )

    assert plan.category == "mouse"
    assert plan.brands == ["Logitech"]
    assert plan.max_price == 300
    assert plan.filters["connection_type"] == "Wireless"


@pytest.mark.asyncio
async def test_llm_catalog_planner_fills_missing_localized_query_constraints() -> None:
    chat = FakeChatModel(
        '{"category":"keyboard","brands":[],"filters":{"switches":"红轴"},'
        '"keywords":[],"sort":"recommend","supported":true,"unsupported_reason":null}'
    )
    planner = LLMCatalogQueryPlanner(chat)

    plan = await planner.plan_search(CatalogSearchInput(query="三模键盘 红轴", limit=3))
    validated = validate_product_query_plan(plan)

    assert validated.category == "keyboard"
    assert validated.filters["connection_type"] == "三模"
    assert validated.filters["switches"] == "红轴"


@pytest.mark.asyncio
async def test_llm_catalog_planner_coerces_filter_values_before_validation() -> None:
    chat = FakeChatModel(
        '{"category":"webcam","brands":[],"filters":{"frame_rate":60,'
        '"microphone":true},"keywords":[],"sort":"recommend",'
        '"supported":true,"unsupported_reason":null}'
    )
    planner = LLMCatalogQueryPlanner(chat)

    plan = await planner.plan_search(
        CatalogSearchInput(query="webcam 1080p 60fps with microphone", limit=3)
    )
    validated = validate_product_query_plan(plan)

    assert validated.filters["frame_rate"] == "60fps"
    assert validated.filters["microphone"] == "Yes"
    assert validated.filters["resolution"] == "1080p"


@pytest.mark.asyncio
async def test_llm_catalog_planner_preserves_explicit_usb_c_over_generic_wired() -> None:
    chat = FakeChatModel(
        '{"category":"mouse","brands":[],"filters":{"connection_type":false},'
        '"keywords":[],"sort":"recommend","supported":true,'
        '"unsupported_reason":null}'
    )
    planner = LLMCatalogQueryPlanner(chat)

    plan = await planner.plan_search(CatalogSearchInput(query="USB-C 有线鼠标", limit=3))
    validated = validate_product_query_plan(plan)

    assert validated.filters["connection_type"] == "USB-C"


@pytest.mark.asyncio
async def test_llm_catalog_planner_retries_with_validation_error_feedback() -> None:
    chat = SequenceFakeChatModel(
        [
            '{"category":"mouse","brands":[],"filters":{"refresh_rate":"144Hz"},'
            '"keywords":[],"sort":"recommend","supported":true,'
            '"unsupported_reason":null}',
            '{"category":"monitor","brands":[],"filters":{"refresh_rate":"144Hz"},'
            '"keywords":[],"sort":"recommend","supported":true,'
            '"unsupported_reason":null}',
        ]
    )
    planner = LLMCatalogQueryPlanner(chat)

    plan = await planner.plan_search(CatalogSearchInput(query="144Hz monitor", limit=3))

    assert plan.category == "monitor"
    assert plan.filters["refresh_rate"] == "144Hz"
    assert len(chat.calls) == 2
    retry_payload = json.loads(chat.calls[1][1].content)
    feedback = retry_payload["validation_feedback"]
    assert "unsupported filters for mouse: refresh_rate" in feedback["error"]
    assert feedback["received_category"] == "mouse"
    assert "refresh_rate" not in feedback["allowed_filters_for_received_category"]
    assert feedback["normalization_hints"]["蓝牙"] == {
        "key": "connection_type",
        "value": "蓝牙",
    }
    assert retry_payload["retry_instruction"].startswith("Return corrected JSON only")


@pytest.mark.asyncio
async def test_llm_catalog_planner_keeps_current_conditions_over_preferences() -> None:
    chat = FakeChatModel(
        '{"category":"mouse","brands":["Razer"],"max_price":800,'
        '"filters":{"connection_type":"Wired"},"keywords":[],'
        '"usage_scenario":"office","sort":"recommend","supported":true,'
        '"unsupported_reason":null}'
    )
    planner = LLMCatalogQueryPlanner(chat)

    plan = await planner.plan_search(
        CatalogSearchInput(
            query="这次要 800 元以内的雷蛇有线办公鼠标",
            preference_defaults={
                "brands": ["Logitech"],
                "max_price": 500,
                "connection_type": "Wireless",
                "usage": "gaming",
            },
        )
    )

    assert plan.brands == ["Razer"]
    assert plan.max_price == 800
    assert plan.filters["connection_type"] == "Wired"
    assert plan.usage_scenario == "office"


@pytest.mark.asyncio
async def test_rule_planner_does_not_reparse_explicit_usage_exclusion_as_positive() -> None:
    from app.tools.catalog import RuleBasedCatalogQueryPlanner

    plan = await RuleBasedCatalogQueryPlanner().plan_search(
        CatalogSearchInput(
            query="不要游戏鼠标",
            excluded_usage=["gaming"],
        )
    )

    assert plan.usage_scenario is None
    assert plan.excluded_usage == ["gaming"]


@pytest.mark.asyncio
async def test_llm_planner_explicit_brand_exclusion_overrides_positive_model_output() -> None:
    chat = FakeChatModel(
        '{"category":"mouse","brands":["Logitech"],"filters":{},'
        '"keywords":[],"sort":"recommend","supported":true,"unsupported_reason":null}'
    )
    planner = LLMCatalogQueryPlanner(chat)

    plan = await planner.plan_search(
        CatalogSearchInput(
            query="不要 Logitech 鼠标",
            excluded_brands=["Logitech"],
        )
    )

    assert plan.brands == []
    assert plan.excluded_brands == ["Logitech"]


def test_build_catalog_planner_is_opt_in() -> None:
    planner = build_catalog_planner(
        SimpleNamespace(catalog_llm_planner_enabled=False)  # type: ignore[arg-type]
    )

    assert planner is None


def test_build_catalog_planner_uses_llm_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    chat = FakeChatModel('{"category":"mouse"}')
    monkeypatch.setattr("app.tools.contracts.build_chat_model", lambda settings: chat)

    planner = build_catalog_planner(
        SimpleNamespace(catalog_llm_planner_enabled=True)  # type: ignore[arg-type]
    )

    assert isinstance(planner, LLMCatalogQueryPlanner)
    assert planner.chat_model is chat


def test_build_catalog_planner_skips_when_key_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.tools.contracts.build_chat_model", lambda settings: None)

    planner = build_catalog_planner(
        SimpleNamespace(catalog_llm_planner_enabled=True)  # type: ignore[arg-type]
    )

    assert planner is None


def test_build_order_planner_uses_llm_when_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chat = FakeChatModel('{"selection":"all"}')
    monkeypatch.setattr("app.tools.contracts.build_chat_model", lambda settings: chat)

    planner = build_order_planner(Settings(llm_api_key="test"))

    assert isinstance(planner, order_tools.LLMOrderQueryPlanner)
    assert planner.chat_model is chat


def test_build_order_planner_skips_when_key_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.tools.contracts.build_chat_model", lambda settings: None)

    planner = build_order_planner(Settings(llm_api_key=""))

    assert planner is None


@pytest.mark.asyncio
async def test_catalog_search_falls_back_for_category_invalid_llm_filter(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    chat = FakeChatModel(
        '{"category":"mouse","brands":[],"filters":{"type":"FPS"},'
        '"keywords":["wireless","FPS","mouse"],"sort":"recommend",'
        '"supported":true,"unsupported_reason":null}'
    )
    async with db_session_factory() as session:
        result = await CatalogToolService(
            session,
            planner=LLMCatalogQueryPlanner(chat),
        ).search(CatalogSearchInput(query="Recommend a wireless FPS mouse under 300", limit=3))

    assert result.result_type == "products"
    assert result.query_plan["planner"] == "rule_based_fallback"
    assert result.query_plan["error_type"] == "invalid_catalog_plan"
    assert "unsupported filters for mouse" in result.query_plan["fallback_reason"]
    assert result.diagnostics[0].code == "invalid_catalog_plan"
    assert result.diagnostics[0].recommended_action == "use_result_with_caution"


@pytest.mark.asyncio
async def test_catalog_search_normalizes_localized_filter_key_and_value(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    chat = FakeChatModel(
        '{"category":"mouse","brands":[],"filters":{"\u8fde\u63a5\u65b9\u5f0f":"\u65e0\u7ebf"},'
        '"keywords":["mouse"],"sort":"recommend",'
        '"supported":true,"unsupported_reason":null}'
    )
    async with db_session_factory() as session:
        result = await CatalogToolService(
            session,
            planner=LLMCatalogQueryPlanner(chat),
        ).search(CatalogSearchInput(query="Recommend a wireless mouse", limit=3))

    assert result.result_type == "products"
    assert result.query_plan["planner"] == "llm"
    assert result.query_plan["filters"] == {"connection_type": "Wireless"}
    assert result.query_plan["normalization_debug"] == {
        "filter_aliases": [
            {
                "from": {"key": "连接方式", "value": "无线"},
                "to": {"key": "connection_type", "value": "Wireless"},
            }
        ]
    }
    assert result.query_plan["error_type"] == "normalization_applied"
    assert result.diagnostics[0].code == "normalization_applied"


@pytest.mark.parametrize(
    ("category", "raw_key", "expected_key"),
    [
        ("mouse", "重量", "weight_g"),
        ("keyboard", "轴体", "switches"),
        ("headset", "麦克风", "microphone"),
        ("monitor", "刷新率", "refresh_rate"),
        ("speaker", "功率", "power_w"),
        ("webcam", "帧率", "frame_rate"),
    ],
)
def test_catalog_filter_aliases_cover_common_category_specs(
    category: str, raw_key: str, expected_key: str
) -> None:
    plan = validate_product_query_plan(
        ProductQueryPlan(
            query="find products",
            category=category,
            filters={raw_key: "demo"},
        )
    )

    assert plan.filters == {expected_key: "demo"}


def test_catalog_filter_alias_still_respects_category_whitelist() -> None:
    with pytest.raises(ValueError, match="unsupported filters for mouse"):
        validate_product_query_plan(
            ProductQueryPlan(
                query="find mouse",
                category="mouse",
                filters={"刷新率": "144Hz"},
            )
        )


@pytest.mark.parametrize(
    ("category", "raw_key", "raw_value", "expected_filters"),
    [
        ("monitor", "刷新率", "144 赫兹", {"refresh_rate": "144Hz"}),
        ("monitor", "分辨率", "2k", {"resolution": "2560x1440"}),
        ("keyboard", "轴体", "红", {"switches": "红轴"}),
        ("keyboard", "背光", "有", {"backlit": "Yes"}),
        ("mouse", "颜色", "黑", {"color": "黑色"}),
        ("speaker", "power_w", "30W", {"power_w": "30"}),
    ],
)
def test_catalog_filter_values_are_normalized(
    category: str, raw_key: str, raw_value: str, expected_filters: dict[str, str]
) -> None:
    plan = validate_product_query_plan(
        ProductQueryPlan(
            query="find products",
            category=category,
            filters={raw_key: raw_value},
        )
    )

    assert plan.filters == expected_filters
    assert plan.normalization_debug["filter_aliases"][0]["to"] == {
        "key": next(iter(expected_filters)),
        "value": next(iter(expected_filters.values())),
    }


def test_catalog_search_prompt_contains_compact_enum_guidance() -> None:
    prompt = catalog_tools._catalog_planner_system_prompt()

    assert "connection_type preserves explicit connection details" in prompt
    assert "USB-C -> USB-C" in prompt
    assert 'speaker power_w examples: "20", "30", "40", "50"' in prompt
    assert 'not "30W"' in prompt


@pytest.mark.parametrize(
    ("alias", "expected"),
    [
        ("罗技", "Logitech"),
        ("雷蛇", "Razer"),
        ("赛睿", "SteelSeries"),
        ("脉冲星", "Pulsar"),
        ("凯酷", "Keychron"),
        ("艾酷", "Akko"),
        ("极度未知", "HyperX"),
        ("索尼", "Sony"),
        ("冠捷", "AOC"),
        ("华硕", "ASUS"),
        ("戴尔", "Dell"),
        ("乐金", "LG"),
        ("漫步者", "Edifier"),
        ("博士", "Bose"),
        ("创新", "Creative"),
        ("爱乐图", "Elgato"),
        ("圆刚", "AVerMedia"),
    ],
)
def test_catalog_brand_aliases_are_normalized(alias: str, expected: str) -> None:
    plan = validate_product_query_plan(
        ProductQueryPlan(query=f"{alias}无线鼠标", category="mouse", brands=[alias])
    )

    assert plan.brands == [expected]


@pytest.mark.asyncio
async def test_catalog_search_falls_back_when_planner_fails(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    async with db_session_factory() as session:
        result = await CatalogToolService(session, planner=BrokenCatalogPlanner()).search(
            CatalogSearchInput(query="Codex wireless mouse", limit=3)
        )

    assert result.result_type == "products"
    assert result.query_plan["planner"] == "rule_based_fallback"
    assert result.query_plan["error_type"] == "invalid_catalog_plan"
    assert "planner unavailable" in result.query_plan["fallback_reason"]
    assert result.diagnostics[0].code == "invalid_catalog_plan"


@pytest.mark.asyncio
async def test_catalog_search_returns_unsupported_query(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    async with db_session_factory() as session:
        result = await CatalogToolService(session).search(
            CatalogSearchInput(query="Which mouse has the fastest month over month growth?")
        )

    assert result.result_type == "empty"
    assert result.ranking_strategy == "unsupported_query"
    assert result.query_plan["supported"] is False
    assert result.query_plan["error_type"] == "unsupported_query"
    assert result.diagnostics[0].code == "unsupported_query"
    assert result.diagnostics[0].recommended_action == "explain_unsupported_query"


@pytest.mark.asyncio
async def test_catalog_search_empty_result_has_stable_diagnostics(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    async with db_session_factory() as session:
        result = await CatalogToolService(session).search(
            CatalogSearchInput(query="mouse", brand="NoSuchBrand", limit=3)
        )

    assert result.result_type == "empty"
    assert result.query_plan["error_type"] == "empty_result"
    assert result.diagnostics[0].code == "empty_result"
    assert result.diagnostics[0].recommended_action == "relax_filters_or_ask_followup"


@pytest.mark.asyncio
async def test_catalog_search_applies_office_keyboard_spec_mapping(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    async with db_session_factory() as session:
        contract = DefaultToolContractProvider().get_contract("catalog_search")
        assert contract is not None
        execution = await RegistryToolExecutor(session, TOOL_TEST_SETTINGS).execute(
            contract,
            {"query": "办公键盘", "limit": 3},
            {"user_id": 1},
        )

    assert execution.ok is True
    assert execution.output is not None
    assert execution.output["query_plan"]["category"] == "keyboard"
    assert execution.output["query_plan"]["usage_scenario"] == "office"
    assert execution.output["query_plan"]["usage_mapping"]["status"] == "applied"
    assert execution.output["query_plan"]["usage_mapping"]["rule_version"] == "v1"
    assert execution.output["result_type"] == "products"
    assert execution.output["products"]
    assert all(
        product["specs"]["switches"] == "静音红轴" for product in execution.output["products"]
    )
    assert execution.output["query_plan"]["error_type"] is None
    assert execution.output["diagnostics"][0]["code"] == "ok"


@pytest.mark.asyncio
async def test_catalog_search_applies_video_meeting_webcam_requirements(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    async with db_session_factory() as session:
        result = await CatalogToolService(session).search(
            CatalogSearchInput(query="开会用的摄像头", limit=3)
        )

    assert result.result_type == "products"
    assert result.query_plan["usage_scenario"] == "video_meeting"
    assert result.query_plan["usage_mapping"]["status"] == "applied"
    assert result.products
    assert all(product.specs["microphone"] == "是" for product in result.products)


@pytest.mark.asyncio
async def test_catalog_search_applies_live_streaming_webcam_requirements(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    async with db_session_factory() as session:
        result = await CatalogToolService(session).search(
            CatalogSearchInput(query="直播摄像头", limit=3)
        )

    assert result.result_type == "products"
    assert result.query_plan["usage_scenario"] == "live_streaming"
    assert result.query_plan["usage_mapping"]["status"] == "applied"
    assert result.products
    assert all(
        product.specs["resolution"] in {"1080p HDR", "1440p", "4K"}
        and product.specs["frame_rate"] in {"60fps", "90fps"}
        for product in result.products
    )


@pytest.mark.asyncio
async def test_catalog_search_reports_unavailable_usage_category_mapping() -> None:
    result = await CatalogToolService(SimpleNamespace()).search(
        CatalogSearchInput(query="办公鼠标", limit=3)
    )

    assert result.result_type == "empty"
    assert result.query_plan["usage_scenario"] == "office"
    assert result.query_plan["usage_mapping"]["status"] == "unavailable"
    assert result.query_plan["error_type"] == "usage_mapping_unavailable"
    assert result.diagnostics[0].code == "usage_mapping_unavailable"


@pytest.mark.asyncio
async def test_categoryless_usage_search_runs_category_rules_in_parallel_and_diversifies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active_queries = 0
    max_active_queries = 0
    created_sessions = 0
    category_index = {
        "keyboard": 1,
        "monitor": 2,
        "headset": 3,
        "webcam": 4,
    }

    class FakeSessionContext:
        async def __aenter__(self):
            nonlocal created_sessions
            created_sessions += 1
            return SimpleNamespace(session_number=created_sessions)

        async def __aexit__(self, exc_type, exc, traceback) -> None:
            return None

    async def fake_search_products(_repository, request):
        nonlocal active_queries, max_active_queries
        active_queries += 1
        max_active_queries = max(max_active_queries, active_queries)
        await asyncio.sleep(0.01)
        active_queries -= 1
        index = category_index[request.category]
        return [
            ProductCard(
                spu_id=index,
                sku_id=index,
                title=f"Office {request.category}",
                brand="Test",
                category=request.category,
                price="99.00",
                stock=1,
                specs={},
            )
        ]

    monkeypatch.setattr(
        catalog_tools.CatalogRepository,
        "search_products",
        fake_search_products,
    )
    monkeypatch.setattr(
        catalog_tools.CatalogRepository,
        "search_product_series",
        fake_search_products,
    )
    async def fake_search_products_with_total(repository, request):
        items = await fake_search_products(repository, request)
        return SimpleNamespace(products=items, total_count=len(items))

    monkeypatch.setattr(
        catalog_tools.CatalogRepository,
        "search_products_with_total",
        fake_search_products_with_total,
    )
    monkeypatch.setattr(
        catalog_tools.CatalogRepository,
        "search_product_series_with_total",
        fake_search_products_with_total,
    )
    async def keep_fake_series(_service, items):
        return items

    monkeypatch.setattr(
        CatalogToolService,
        "_enrich_series_products",
        keep_fake_series,
    )
    service = CatalogToolService(
        SimpleNamespace(),
        session_factory=lambda: FakeSessionContext(),  # type: ignore[arg-type]
    )

    result = await service.search(CatalogSearchInput(query="推荐办公相关产品", limit=4))

    assert [product.category for product in result.products] == [
        "keyboard",
        "monitor",
        "headset",
        "webcam",
    ]
    assert result.query_plan["usage_mapping"]["status"] == "expanded"
    assert result.query_plan["usage_mapping"]["execution"] == "parallel_independent_sessions"
    assert result.ranking_strategy == "scenario_category_diversified_mapping"
    assert created_sessions == 4
    assert max_active_queries == 3


@pytest.mark.asyncio
async def test_catalog_facets_returns_mouse_brands(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    async with db_session_factory() as session:
        result = await build_tool_registry(session, settings=TOOL_TEST_SETTINGS).execute(
            "catalog.facets",
            {"query": "what mouse brands do you sell", "facet": "brand", "category": "mouse"},
        )

    assert result.ok
    assert result.output is not None
    assert result.output["result_type"] == "facets"
    assert result.output["facet"] == "brand"
    assert all(item["count_scope"] == "spu" for item in result.output["items"])
    assert all(item["count"] == item["spu_count"] for item in result.output["items"])
    values = {item["value"] for item in result.output["items"]}
    assert {"Logitech", "Razer"} <= values


@pytest.mark.asyncio
async def test_catalog_facets_can_count_concrete_versions(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    async with db_session_factory() as session:
        result = await CatalogToolService(session).facets(
            CatalogFacetInput(
                query="键盘每种轴体有多少个版本",
                facet="spec_value",
                category="keyboard",
                spec_key="switches",
            )
        )

    assert result.result_type == "facets"
    assert result.query_plan["count_scope"] == "sku"
    assert result.items
    assert all(item.count_scope == "sku" for item in result.items)
    assert all(item.count == item.sku_count for item in result.items)


@pytest.mark.asyncio
async def test_catalog_facets_infers_brand_facet_from_query_only(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    async with db_session_factory() as session:
        result = await build_tool_registry(session, settings=TOOL_TEST_SETTINGS).execute(
            "catalog.facets",
            {"query": "what mouse brands do you sell"},
        )

    assert result.ok
    assert result.output is not None
    assert result.output["result_type"] == "facets"
    assert result.output["facet"] == "brand"
    assert result.output["category"] == "mouse"
    assert result.output["query_plan"]["planner"] == "rule_based"
    assert result.output["query_plan"]["supported"] is True
    assert result.output["query_plan"]["facet"] == "brand"
    assert result.output["query_plan"]["category"] == "mouse"
    values = {item["value"] for item in result.output["items"]}
    assert {"Logitech", "Razer"} <= values


@pytest.mark.asyncio
async def test_catalog_facets_infers_category_facet_from_query_only(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    async with db_session_factory() as session:
        result = await build_tool_registry(session, settings=TOOL_TEST_SETTINGS).execute(
            "catalog.facets",
            {"query": "what peripheral categories does Razer sell"},
        )

    assert result.ok
    assert result.output is not None
    assert result.output["result_type"] == "facets"
    assert result.output["facet"] == "category"
    assert result.output["brand"] == "Razer"
    assert result.output["items"]


@pytest.mark.asyncio
async def test_catalog_facets_returns_spec_values(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    async with db_session_factory() as session:
        result = await build_tool_registry(session, settings=TOOL_TEST_SETTINGS).execute(
            "catalog.facets",
            {
                "query": "what keyboard switches are available",
                "facet": "spec_value",
                "category": "keyboard",
                "spec_key": "switches",
            },
        )

    assert result.ok
    assert result.output is not None
    assert result.output["result_type"] == "facets"
    assert result.output["facet"] == "spec_value"
    assert result.output["query_plan"]["spec_key"] == "switches"
    assert any("Red" in item["value"] for item in result.output["items"])


@pytest.mark.asyncio
async def test_catalog_facets_returns_unsupported_formal_plan_without_querying(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    async with db_session_factory() as session:
        result = await build_tool_registry(session, settings=TOOL_TEST_SETTINGS).execute(
            "catalog.facets",
            {"query": "what monitor switches are available"},
        )

    assert result.ok
    assert result.output is not None
    assert result.output["result_type"] == "empty"
    assert result.output["query_plan"]["supported"] is False
    assert result.output["query_plan"]["facet"] == "spec_value"
    assert result.output["query_plan"]["category"] == "monitor"
    assert result.output["query_plan"]["spec_key"] == "switches"
    assert "unsupported spec_key for monitor" in result.output["query_plan"]["unsupported_reason"]


@pytest.mark.asyncio
async def test_catalog_compare_resolves_natural_language_candidates(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    async with db_session_factory() as session:
        result = await build_tool_registry(session, settings=TOOL_TEST_SETTINGS).execute(
            "catalog.compare",
            {"query": "Compare Codex G502 and Viper for FPS", "limit": 5},
        )

    assert result.ok
    assert result.output is not None
    assert result.output["comparison_level"] == "spu"
    titles = [series["title"] for series in result.output["series"]]
    assert "Logitech Codex G502 Hero" in titles
    assert "Razer Codex Viper V3 Pro" in titles
    assert result.output["comparison_fields"]


@pytest.mark.asyncio
async def test_catalog_compare_uses_compare_plan_fields(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    async with db_session_factory() as session:
        result = await CatalogToolService(session, planner=FakeCatalogPlanner()).compare(
            CatalogCompareInput(query="Compare G502 and Viper for FPS", limit=5)
        )

    assert result.result_type == "comparison"
    assert result.query_plan["compare_plan"]["planner"] == "fake"
    assert result.comparison_level == "spu"
    assert {"price_range", "availability", "max_dpi", "connection_type"} <= set(
        result.comparison_fields
    )
    assert result.series
    brands = {series.brand for series in result.series}
    assert {"Logitech", "Razer"} <= brands
    assert sum(1 for series in result.series if series.brand == "Logitech") == 1
    assert sum(1 for series in result.series if series.brand == "Razer") == 1


@pytest.mark.asyncio
async def test_spu_compare_aggregates_all_active_skus_without_inventing_variants(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    async with db_session_factory() as session:
        spus = (
            await session.execute(
                select(Spu).where(
                    Spu.title.in_(
                        [
                            "Logitech Codex G502 Hero",
                            "Razer Codex Viper V3 Pro",
                        ]
                    )
                )
            )
        ).scalars().all()
        by_title = {spu.title: spu for spu in spus}
        g502 = by_title["Logitech Codex G502 Hero"]
        viper = by_title["Razer Codex Viper V3 Pro"]
        g502_base_price = (
            await session.execute(
                select(Sku.price)
                .where(Sku.spu_id == g502.id, Sku.status == 1)
                .order_by(Sku.id)
                .limit(1)
            )
        ).scalar_one()
        session.add_all(
            [
                Sku(
                    spu_id=g502.id,
                    title="Logitech Codex G502 Hero Bluetooth Blue",
                    price="59.99",
                    stock=0,
                    sales_count=123,
                    specs_json={
                        "connection_type": "Bluetooth",
                        "max_dpi": "25600",
                        "color": "Blue",
                    },
                    status=1,
                ),
                Sku(
                    spu_id=viper.id,
                    title="Razer Codex Viper V3 Pro Bluetooth Black",
                    price="149.99",
                    stock=7,
                    sales_count=321,
                    specs_json={
                        "connection_type": "Bluetooth",
                        "max_dpi": "35000",
                        "color": "Black",
                    },
                    status=1,
                ),
            ]
        )
        await session.flush()

        result = await CatalogToolService(session).compare(
            CatalogCompareInput(
                query="compare these two product series",
                comparison_level="spu",
                spu_ids=[g502.id, viper.id],
                limit=5,
            )
        )

    assert result.result_type == "comparison"
    assert result.comparison_level == "spu"
    assert result.products == []
    assert [item.spu_id for item in result.series] == [g502.id, viper.id]

    g502_series = result.series[0]
    assert g502_series.title == "Logitech Codex G502 Hero"
    assert g502_series.sku_count == 2
    assert g502_series.in_stock_sku_count == 1
    assert g502_series.min_price == min(g502_base_price, Decimal("59.99"))
    assert g502_series.max_price == max(g502_base_price, Decimal("59.99"))
    assert g502_series.common_specs["max_dpi"] == "25600"
    connection = g502_series.option_specs["connection_type"]
    assert [item.value for item in connection.values] == ["Bluetooth", "Wired"]
    assert [item.in_stock_sku_count for item in connection.values] == [0, 1]
    assert {
        (variant.specs["connection_type"], variant.specs["color"])
        for variant in g502_series.variants
    } == {("Wired", "Black"), ("Bluetooth", "Blue")}

    difference = result.series_differences[0]
    connection_difference = next(
        item for item in difference.fields if item.field == "connection_type"
    )
    assert connection_difference.shared_values == ["Bluetooth"]
    assert connection_difference.left_only_values == ["Wired"]
    assert connection_difference.right_only_values == ["Wired, Wireless"]


@pytest.mark.asyncio
async def test_auto_spu_compare_resolves_missing_spu_id_from_trusted_sku_target(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    async with db_session_factory() as session:
        rows = (
            await session.execute(
                select(Sku.id, Spu.id)
                .join(Spu, Sku.spu_id == Spu.id)
                .where(
                    Sku.title.in_(
                        [
                            "Logitech Codex G502 Hero Black",
                            "Razer Codex Viper V3 Pro White",
                        ]
                    )
                )
                .order_by(Sku.id)
            )
        ).all()
        assert len(rows) == 2
        (left_sku_id, left_spu_id), (right_sku_id, right_spu_id) = rows

        result = await CatalogToolService(session).compare(
            CatalogCompareInput(
                query="比较这两个鼠标系列的全部版本",
                targets=[
                    {
                        "sku_id": left_sku_id,
                        "source": "working_memory_reference",
                    },
                    {
                        "sku_id": right_sku_id,
                        "spu_id": right_spu_id,
                        "source": "current_turn_artifact",
                    },
                ],
            )
        )

    assert result.result_type == "comparison"
    assert result.comparison_level == "spu"
    assert [item.spu_id for item in result.series] == [left_spu_id, right_spu_id]
    assert result.query_plan["spu_ids"] == [left_spu_id, right_spu_id]


def test_catalog_compare_rejects_ids_from_the_other_comparison_level() -> None:
    with pytest.raises(ValidationError):
        CatalogCompareInput(
            query="compare series",
            comparison_level="spu",
            sku_ids=[11, 22],
        )


@pytest.mark.asyncio
async def test_direct_sku_compare_omits_inactive_sku_and_spu(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    async with db_session_factory() as session:
        rows = (
            await session.execute(
                select(Sku.id, Spu.id)
                .join(Spu, Sku.spu_id == Spu.id)
                .where(
                    Sku.title.in_(
                        [
                            "Logitech Codex G502 Hero Black",
                            "Razer Codex Viper V3 Pro White",
                        ]
                    )
                )
                .order_by(Sku.id)
            )
        ).all()
        assert len(rows) == 2
        inactive_sku_id, _ = rows[0]
        active_sku_id, inactive_spu_id = rows[1]
        await session.execute(update(Sku).where(Sku.id == inactive_sku_id).values(status=0))
        await session.execute(update(Spu).where(Spu.id == inactive_spu_id).values(status=0))
        await session.flush()

        result = await CatalogToolService(session).compare(
            CatalogCompareInput(
                query="compare selected products",
                sku_ids=[inactive_sku_id, active_sku_id],
                limit=5,
            )
        )

    assert result.products == []


def test_direct_sku_compare_statement_requires_active_sku_and_spu() -> None:
    statement = catalog_tools._active_sku_rows_statement([11, 22])
    sql = str(statement.compile(dialect=postgresql.dialect()))

    assert "sku.status" in sql
    assert "spu.status" in sql


@pytest.mark.asyncio
async def test_llm_catalog_compare_planner_parses_guarded_json() -> None:
    chat = FakeChatModel(
        """
        {
          "category": "mouse",
          "items": ["G502", "Viper"],
          "brands": ["Logitech", "Razer"],
          "comparison_fields": ["price", "stock", "max_dpi", "weight_g"],
          "scenario": "FPS",
          "supported": true,
          "unsupported_reason": null
        }
        """
    )
    planner = LLMCatalogQueryPlanner(chat)

    plan = await planner.plan_compare(
        CatalogCompareInput(query="Compare G502 and Viper for FPS", limit=5)
    )

    assert plan.planner == "llm"
    assert plan.items == ["G502", "Viper"]
    assert plan.brands == ["Logitech", "Razer"]
    assert plan.comparison_fields == ["price", "stock", "max_dpi", "weight_g"]


@pytest.mark.asyncio
async def test_order_lookup_returns_candidates_or_single_order_with_user_isolation(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    async with db_session_factory() as session:
        registry = build_tool_registry(session, settings=TOOL_TEST_SETTINGS)
        candidates = await registry.execute("order.lookup", {"user_id": 1, "limit": 5})
        single = await registry.execute(
            "order.lookup",
            {"user_id": 1, "order_id": 202607020001},
        )
        isolated = await registry.execute(
            "order.lookup",
            {"user_id": 999, "order_id": 202607020001},
        )

    assert candidates.ok
    assert candidates.output is not None
    assert candidates.output["result_type"] == "order_candidates"
    assert candidates.output["candidates"][0]["id"] == 202607020001

    assert single.ok
    assert single.output is not None
    assert single.output["result_type"] == "single_order"
    assert single.output["order"]["id"] == 202607020001

    assert isolated.ok
    assert isolated.output is not None
    assert isolated.output["result_type"] == "not_found"


@pytest.mark.asyncio
async def test_order_lookup_extracts_order_id_from_query(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    contract = DefaultToolContractProvider().get_contract("order_lookup")
    assert contract is not None

    async with db_session_factory() as session:
        executor = RegistryToolExecutor(session, TOOL_TEST_SETTINGS)
        result = await executor.execute(
            contract,
            {"query": "please check order 202607020001"},
            {"user_id": 1},
        )

    assert result.ok
    assert result.output is not None
    assert result.output["result_type"] == "single_order"
    assert result.output["order"]["id"] == 202607020001


@pytest.mark.asyncio
async def test_order_lookup_query_without_order_id_returns_candidates(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    contract = DefaultToolContractProvider().get_contract("order_lookup")
    assert contract is not None

    async with db_session_factory() as session:
        executor = RegistryToolExecutor(session, TOOL_TEST_SETTINGS)
        result = await executor.execute(
            contract,
            {"query": "show my recent orders"},
            {"user_id": 1},
        )

    assert result.ok
    assert result.output is not None
    assert result.output["result_type"] == "order_candidates"
    assert result.output["total_match_count"] == 1
    assert result.output["returned_count"] == 1
    assert result.output["is_exhaustive"] is True


@pytest.mark.parametrize(
    ("query", "expected_selection", "expected_operation", "expected_shape", "expected_limit"),
    [
        ("我到底有多少订单呀？", "all", "total_count", "count", 5),
        ("我有几个订单？最近的列出来让我选", "recent", "list", "summary", 5),
        ("最近三笔订单给我简单列一下", "recent", "list", "summary", 3),
        ("我最近一笔订单是什么", "latest", "detail", "single_detail", 1),
        ("最新那单物流到哪儿了", "latest", "detail", "single_detail", 1),
        ("所有订单都给我列出来", "all", "list", "summary", 5),
        ("我是不是买过雷蛇鼠标，在哪个订单来着？", "all", "match_orders", "analysis_detail", 5),
        ("桌面音响我什么时候买的？", "all", "match_orders", "analysis_detail", 5),
        ("哪些订单已经发货", "all", "match_orders", "analysis_detail", 5),
        ("去年买过什么外设", "all", "match_orders", "analysis_detail", 5),
        ("所有订单的商品明细都摊开给我看看", "all", "detail", "analysis_detail", 5),
        ("我每一单都寄到哪里了，帮我弄个地址表", "all", "list", "unsupported", 5),
    ],
)
def test_order_fast_path_classifies_colloquial_queries(
    query: str,
    expected_selection: str,
    expected_operation: str,
    expected_shape: str,
    expected_limit: int,
) -> None:
    request = OrderLookupInput(user_id=1, query=query)
    plan = order_tools._deterministic_fast_plan(request, order_id=None)

    assert plan is not None
    validated = order_tools.validate_order_query_plan(plan, order_id=None)
    assert plan.selection == expected_selection
    assert plan.operation == expected_operation
    assert plan.limit == expected_limit
    assert validated.result_shape == expected_shape


@pytest.mark.parametrize(
    "query",
    [
        "桌面音响我什么时候买的？",
        "那个桌面音箱是啥时候买的",
        "我哪天买的 speaker？",
        "查询桌面音响的购买时间",
    ],
)
def test_order_fast_path_maps_product_purchase_time_to_supported_history_lookup(
    query: str,
) -> None:
    request = OrderLookupInput(user_id=1, query=query)

    plan = order_tools._deterministic_fast_plan(request, order_id=None)

    assert plan is not None
    assert plan.selection == "all"
    assert plan.operation == "match_orders"
    assert plan.requested_fields == ["purchase_history_match", "created_at"]
    validated = order_tools.validate_order_query_plan(plan, order_id=None)
    assert validated.result_shape == "analysis_detail"
    assert validated.unsupported_fields == ()
    assert {"created_at", "item_name", "order_id"}.issubset(validated.supported_fields)


def test_order_llm_prompt_maps_product_purchase_time_without_inventing_field() -> None:
    prompt = order_tools._order_planner_system_prompt()

    assert '"桌面音响我什么时候买的"' in prompt
    assert "purchase_history_match" in prompt
    assert "created_at" in prompt
    assert "Do not invent purchase_date" in prompt


def test_order_fast_path_accepts_plain_english_explicit_order_id() -> None:
    request = OrderLookupInput(
        user_id=1,
        query="please check order 202607020001",
    )
    order_id = order_tools._extract_order_id(request.query)

    plan = order_tools._deterministic_fast_plan(request, order_id=order_id)

    assert order_id == 202607020001
    assert plan is not None
    assert plan.selection == "explicit"
    assert order_tools.validate_order_query_plan(
        plan,
        order_id=order_id,
    ).result_shape == "single_detail"


@pytest.mark.asyncio
async def test_llm_order_planner_catches_colloquial_supported_request() -> None:
    planner = order_tools.LLMOrderQueryPlanner(
        FakeChatModel(
            """
            {
              "selection": "all",
              "operation": "match_orders",
              "requested_fields": ["purchase_history_match"],
              "limit": 5
            }
            """
        )
    )

    plan = await planner.plan(
        OrderLookupInput(
            user_id=1,
            query="我头前买的那副雷蛇耳机到底压在哪张单子里，帮我翻出来",
        )
    )
    validated = order_tools.validate_order_query_plan(plan, order_id=None)

    assert plan.planner == "llm"
    assert plan.operation == "match_orders"
    assert validated.result_shape == "analysis_detail"
    assert "order_items" in validated.supported_fields


@pytest.mark.asyncio
async def test_llm_order_planner_preserves_unusual_unavailable_requirement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RepositoryMustNotBeCreated:
        def __init__(self, session: AsyncSession):
            raise AssertionError("unsupported query must not reach the repository")

    monkeypatch.setattr(order_tools, "OrderRepository", RepositoryMustNotBeCreated)
    planner = order_tools.LLMOrderQueryPlanner(
        FakeChatModel(
            """
            {
              "selection": "all",
              "operation": "detail",
              "requested_fields": ["buyer_message"],
              "limit": 5
            }
            """
        )
    )
    service = order_tools.OrderToolService(None, planner=planner)  # type: ignore[arg-type]

    result = await service.lookup(
        OrderLookupInput(
            user_id=1,
            query="我下单时好像给商家嘱咐过一句话，能把当时写的内容翻出来吗？",
        )
    )

    assert result.result_type == "unsupported"
    assert result.diagnostics[0].code == "unsupported_query"
    assert result.query_plan["unsupported_fields"] == ["buyer_message"]


@pytest.mark.asyncio
async def test_fast_unsupported_address_request_does_not_query_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RepositoryMustNotBeCreated:
        def __init__(self, session: AsyncSession):
            raise AssertionError("unsupported query must not reach the repository")

    monkeypatch.setattr(order_tools, "OrderRepository", RepositoryMustNotBeCreated)
    service = order_tools.OrderToolService(None)  # type: ignore[arg-type]

    result = await service.lookup(
        OrderLookupInput(user_id=1, query="把我每一单最后寄到哪儿整理成地址表")
    )

    assert result.result_type == "unsupported"
    assert result.diagnostics[0].code == "unsupported_query"
    assert "receiver_address" in result.query_plan["unsupported_fields"]


@pytest.mark.asyncio
async def test_estimated_arrival_request_is_unsupported_without_database_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RepositoryMustNotBeCreated:
        def __init__(self, session: AsyncSession):
            raise AssertionError("ETA request must not reach the repository")

    monkeypatch.setattr(order_tools, "OrderRepository", RepositoryMustNotBeCreated)
    service = order_tools.OrderToolService(None)  # type: ignore[arg-type]

    result = await service.lookup(
        OrderLookupInput(
            user_id=1,
            order_id=991000000103,
            query="这个多久能到？",
        )
    )

    assert result.result_type == "unsupported"
    assert result.diagnostics[0].code == "unsupported_query"
    assert result.query_plan["supported_fields"] == []
    assert result.query_plan["unsupported_fields"] == ["estimated_delivery_time"]


@pytest.mark.asyncio
async def test_llm_order_planner_maps_indirect_arrival_prediction_to_unsupported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RepositoryMustNotBeCreated:
        def __init__(self, session: AsyncSession):
            raise AssertionError("LLM-classified ETA request must not reach the repository")

    monkeypatch.setattr(order_tools, "OrderRepository", RepositoryMustNotBeCreated)
    chat = FakeChatModel(
        """
        {
          "selection": "explicit",
          "operation": "detail",
          "requested_fields": ["estimated_delivery_time"],
          "limit": 1
        }
        """
    )
    planner = order_tools.LLMOrderQueryPlanner(chat)
    service = order_tools.OrderToolService(
        None,  # type: ignore[arg-type]
        planner=planner,
    )
    request = OrderLookupInput(
        user_id=1,
        order_id=991000000103,
        query="照眼下这个走法，我周末前能收着不？",
    )

    assert order_tools._deterministic_fast_plan(
        request,
        order_id=request.order_id,
    ) is None
    result = await service.lookup(request)

    assert len(chat.calls) == 1
    system_prompt = chat.calls[0][0].content
    assert "周末前能收到不" in system_prompt
    assert "Do not reinterpret" in system_prompt
    assert result.result_type == "unsupported"
    assert result.diagnostics[0].code == "unsupported_query"
    assert result.query_plan["unsupported_fields"] == ["estimated_delivery_time"]


@pytest.mark.asyncio
async def test_mixed_single_order_request_queries_supported_fields_and_reports_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested: list[tuple[int, int]] = []
    card = OrderCard(
        id=202607020001,
        status=3,
        status_label="已发货",
        total_amount=Decimal("109.00"),
        pay_amount=Decimal("109.00"),
        freight_amount=Decimal("10.00"),
        created_at=datetime(2026, 7, 20, 12, 0),
        items=[],
        logistics=None,
    )

    class FakeOrderRepository:
        def __init__(self, session: AsyncSession):
            self.session = session

        async def get_order(self, user_id: int, order_id: int) -> OrderCard | None:
            requested.append((user_id, order_id))
            return card

    monkeypatch.setattr(order_tools, "OrderRepository", FakeOrderRepository)
    service = order_tools.OrderToolService(None)  # type: ignore[arg-type]

    result = await service.lookup(
        OrderLookupInput(
            user_id=7,
            query="帮我瞅下订单 202607020001 现在啥状态，当时又是拿什么方式付的？",
        )
    )

    assert requested == [(7, 202607020001)]
    assert result.result_type == "single_order"
    assert result.order == card
    assert result.diagnostics[0].code == "partial_unsupported_query"
    assert result.query_plan["supported_fields"] == ["order_status"]
    assert result.query_plan["unsupported_fields"] == ["payment_method"]


def test_order_summary_contains_safe_amount_and_latest_shipping_snapshot() -> None:
    card = OrderCard(
        id=202607020001,
        status=3,
        status_label="已发货",
        total_amount=Decimal("109.00"),
        pay_amount=Decimal("109.00"),
        freight_amount=Decimal("10.00"),
        created_at=datetime(2026, 7, 20, 12, 0),
        items=[
            OrderItemCard(
                id=1,
                sku_id=101,
                sku_name="雷蛇无线鼠标",
                sku_specs={"连接": "无线"},
                price=Decimal("99.00"),
                quantity=1,
            )
        ],
        logistics=LogisticsCard(
            express_company="顺丰速运",
            logistic_no="SF100200300400",
            status=2,
            trace=[
                {"time": "2026-07-20 15:20", "text": "快件已揽收"},
                {
                    "time": "2026-07-21 09:10",
                    "text": "快件正在发往上海转运中心",
                },
            ],
        ),
    )

    summary = order_tools._summary(card)

    assert summary.total_amount == Decimal("109.00")
    assert summary.express_company == "顺丰速运"
    assert summary.logistic_no == "SF100200300400"
    assert summary.latest_logistics_event == {
        "time": "2026-07-21 09:10",
        "text": "快件正在发往上海转运中心",
    }
    serialized_detail = card.model_dump(mode="json")
    assert serialized_detail["total_amount"] == "109.00"
    assert serialized_detail["freight_amount"] == "10.00"
    assert serialized_detail["logistics"]["trace"][-1]["text"] == (
        "快件正在发往上海转运中心"
    )
    assert "receiver_name" not in serialized_detail
    assert "receiver_phone" not in serialized_detail
    assert "receiver_address" not in serialized_detail
    assert "pay_type" not in serialized_detail
    assert "pay_type_label" not in serialized_detail


def test_purchase_history_analysis_focuses_matching_order_ids() -> None:
    orders = [
        OrderCard(
            id=101,
            status=3,
            status_label="已发货",
            pay_amount=Decimal("117.00"),
            created_at=datetime(2026, 7, 23, 12, 0),
            items=[
                OrderItemCard(
                    id=1,
                    sku_id=11,
                    sku_name="Razer 锐影 W02 高清摄像头",
                    sku_specs={},
                    price=Decimal("117.00"),
                    quantity=1,
                )
            ],
            logistics=None,
        ),
        OrderCard(
            id=103,
            status=3,
            status_label="已发货",
            pay_amount=Decimal("149.00"),
            created_at=datetime(2026, 7, 21, 12, 0),
            items=[
                OrderItemCard(
                    id=3,
                    sku_id=13,
                    sku_name="Wooting 山岚 K03 机械键盘 粉色增强版",
                    sku_specs={"color": "粉色"},
                    price=Decimal("149.00"),
                    quantity=1,
                )
            ],
            logistics=None,
        ),
    ]
    plan = order_tools.OrderQueryPlan(
        query="我买过什么键盘吗",
        selection="all",
        operation="match_orders",
        requested_fields=["purchase_history_match"],
    )

    focused = order_tools._analysis_focus_order_ids(plan, orders)

    assert focused == [103]


@pytest.mark.asyncio
async def test_order_planner_failure_is_not_reported_as_unsupported() -> None:
    class BrokenOrderPlanner:
        async def plan(self, request: OrderLookupInput) -> order_tools.OrderQueryPlan:
            raise RuntimeError("planner unavailable")

    service = order_tools.OrderToolService(
        None,  # type: ignore[arg-type]
        planner=BrokenOrderPlanner(),
    )

    result = await service.lookup(
        OrderLookupInput(user_id=1, query="帮我找找那次比较特别的购买记录")
    )

    assert result.result_type == "planning_failed"
    assert result.diagnostics[0].code == "invalid_order_plan"
    assert all(item.code != "unsupported_query" for item in result.diagnostics)


@pytest.mark.asyncio
async def test_order_lookup_returns_exact_total_for_bounded_recent_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested_user_ids: list[int] = []
    cards = [
        OrderCard(
            id=202607020100 + index,
            status=3,
            status_label="已发货",
            total_amount=Decimal("109.00"),
            pay_amount=Decimal("99.00"),
            freight_amount=Decimal("10.00"),
            created_at=datetime(2026, 7, 20 - index, 12, 0),
            items=[],
            logistics=None,
        )
        for index in range(5)
    ]

    class FakeOrderRepository:
        def __init__(self, session: AsyncSession):
            self.session = session

        async def count_orders(self, user_id: int) -> int:
            requested_user_ids.append(user_id)
            return len(cards)

        async def list_recent_orders_page(
            self,
            user_id: int,
            *,
            limit: int,
            offset: int,
        ) -> OrderSearchPage:
            requested_user_ids.append(user_id)
            return OrderSearchPage(
                orders=cards[offset : offset + limit],
                total_count=len(cards),
                offset=offset,
            )

    monkeypatch.setattr(order_tools, "OrderRepository", FakeOrderRepository)
    service = order_tools.OrderToolService(None)  # type: ignore[arg-type]

    recent = await service.lookup(OrderLookupInput(user_id=1, query="最近两笔订单先给我看看"))
    count = await service.lookup(OrderLookupInput(user_id=1, query="我一共有几个订单"))
    remaining = await service.lookup(
        OrderLookupInput(user_id=1, query="行，剩下的也都给我吧", offset=2)
    )
    exhausted = await service.lookup(
        OrderLookupInput(user_id=1, query="还有别的吗？继续看", offset=5)
    )
    analysis = await service.lookup(
        OrderLookupInput(user_id=17, query="我买过雷蛇鼠标吗")
    )

    assert recent.result_type == "order_candidates"
    assert recent.total_match_count == 5
    assert recent.returned_count == 2
    assert recent.is_exhaustive is False
    assert recent.next_offset == 2
    assert [item.id for item in recent.candidates] == [card.id for card in cards[:2]]
    assert count.result_type == "order_count"
    assert count.total_match_count == 5
    assert count.returned_count == 0
    assert remaining.result_type == "order_candidates"
    assert remaining.query_mode == "remaining"
    assert remaining.returned_count == 3
    assert [item.id for item in remaining.candidates] == [card.id for card in cards[2:]]
    assert exhausted.result_type == "order_candidates"
    assert exhausted.query_mode == "remaining"
    assert exhausted.total_match_count == 5
    assert exhausted.returned_count == 0
    assert exhausted.is_exhaustive is True
    assert analysis.result_type == "order_analysis"
    assert analysis.query_mode == "analysis"
    assert analysis.total_match_count == 5
    assert analysis.returned_count == 5
    assert analysis.is_exhaustive is True
    assert len(analysis.analysis_orders) == 5
    assert analysis.candidates == []
    assert requested_user_ids[-1] == 17
    serialized_analysis = analysis.model_dump(mode="json")
    assert "receiver_name" not in str(serialized_analysis)
    assert "receiver_phone" not in str(serialized_analysis)
    assert "receiver_address" not in str(serialized_analysis)


@pytest.mark.asyncio
async def test_policy_and_knowledge_search_support_hybrid_retrieval() -> None:
    service = _knowledge_service()
    policy = await service.search_policy(DocumentSearchInput(query="return refund", limit=3))
    knowledge = await service.search_knowledge(
        DocumentSearchInput(query="Logitech Razer brand", limit=3)
    )

    assert policy.result_type == "documents"
    assert policy.search_strategy == "hybrid"
    assert policy.documents[0].document_type in {"policy", "store_rule", "faq"}
    assert "retrieval_debug" in policy.documents[0].metadata

    assert knowledge.result_type == "documents"
    assert knowledge.search_strategy == "hybrid"
    assert knowledge.documents[0].document_type in {
        "brand",
        "peripheral_knowledge",
        "faq",
        "store_rule",
    }
    assert any(document.document_type == "brand" for document in knowledge.documents)


@pytest.mark.asyncio
async def test_document_search_limit_is_chunk_top_k_with_minimum_two() -> None:
    documents = [
        knowledge_tools.LocalKnowledgeDocument(
            id=1,
            title="退货政策一",
            document_type="policy",
            content="退货政策支持七天无理由退货。",
        ),
        knowledge_tools.LocalKnowledgeDocument(
            id=2,
            title="退货政策二",
            document_type="faq",
            content="退货申请需要提供订单号。",
        ),
    ]
    service = KnowledgeRetrievalToolService(documents=documents)

    result = await service.search_policy(
        DocumentSearchInput(query="退货政策", retrieval_mode="bm25", limit=1)
    )

    assert len(result.documents) == 2
    assert {item.metadata["retrieval_debug"]["chunk_id"] for item in result.documents} == {
        "1:0",
        "2:0",
    }


@pytest.mark.asyncio
async def test_document_search_returns_complete_top_chunks_without_overlap_only_tail() -> None:
    first_section = "退货条件：签收七天内、包装完整且不影响二次销售。" * 10
    second_section = "退款时效：退货入库验收通过后按原支付路径退回。" * 10
    document = knowledge_tools.LocalKnowledgeDocument(
        id=1,
        title="PC 外设退货政策",
        document_type="policy",
        content=first_section + second_section,
    )
    service = KnowledgeRetrievalToolService(documents=[document])

    result = await service.search_policy(
        DocumentSearchInput(query="退货条件和退款时效", retrieval_mode="bm25", limit=3)
    )

    chunk_ids = [item.metadata["retrieval_debug"]["chunk_id"] for item in result.documents]
    assert len(result.documents) == 2
    assert set(chunk_ids) == {"1:0", "1:1"}
    assert max(len(item.snippet) for item in result.documents) == 420
    assert all(not item.snippet.endswith("...") for item in result.documents)


@pytest.mark.asyncio
async def test_policy_search_finds_customer_privacy_boundary() -> None:
    service = _knowledge_service()

    result = await service.search_policy(
        DocumentSearchInput(
            query="查询其他用户购买记录和客户名单的隐私规则",
            retrieval_mode="bm25",
            limit=3,
        )
    )

    assert result.result_type == "documents"
    assert result.documents[0].title == "用户隐私与数据访问规则"
    assert any("不得查询、披露、汇总或推断其他用户" in item.snippet for item in result.documents)


@pytest.mark.asyncio
async def test_document_search_can_select_retrieval_mode() -> None:
    service = _knowledge_service()
    bm25 = await service.search_knowledge(
        DocumentSearchInput(query="keyboard magnetic switch", retrieval_mode="bm25", limit=3)
    )
    vector = await service.search_knowledge(
        DocumentSearchInput(query="Wooting", retrieval_mode="vector", limit=3)
    )

    assert bm25.search_strategy == "bm25"
    assert bm25.result_type == "documents"
    assert bm25.documents[0].score > 0

    assert vector.search_strategy == "vector"
    assert vector.result_type == "documents"
    assert vector.documents[0].metadata["retrieval_debug"]["vector_score"] > 0
    assert vector.documents[0].snippet == "Logitech Razer Wooting brand"


@pytest.mark.asyncio
async def test_bm25_retrieval_does_not_load_embedding_model() -> None:
    class UnexpectedEmbeddingProvider(FakeEmbeddingProvider):
        def embed_query(self, text: str) -> list[float]:
            raise AssertionError("BM25 retrieval must not invoke the embedding provider")

    service = KnowledgeRetrievalToolService(
        embedding_provider=UnexpectedEmbeddingProvider(),
        vector_index=_fake_vector_index(),
    )

    result = await service.search_knowledge(
        DocumentSearchInput(
            query="Logitech 品牌",
            retrieval_mode="bm25",
            limit=3,
        )
    )

    assert result.result_type == "documents"


@pytest.mark.asyncio
async def test_tool_registry_returns_stable_errors(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    async with db_session_factory() as session:
        registry = build_tool_registry(session, settings=TOOL_TEST_SETTINGS)
        unknown = await registry.execute("missing.tool", {})
        invalid = await registry.execute("catalog.search", {"limit": 3})

    assert not unknown.ok
    assert unknown.error is not None
    assert unknown.error.code == "unknown_tool"

    assert not invalid.ok
    assert invalid.error is not None
    assert invalid.error.code == "invalid_input"


def test_catalog_sql_guard_rejects_unsafe_sql() -> None:
    validate_catalog_sql("SELECT sku.id FROM sku LIMIT 5")

    with pytest.raises(ValueError):
        validate_catalog_sql("SELECT * FROM order_info LIMIT 5")

    with pytest.raises(ValueError):
        validate_catalog_sql("DELETE FROM sku")

    with pytest.raises(ValueError):
        validate_catalog_sql("SELECT sku.id FROM sku")


def test_product_query_guard_rejects_unsupported_fields() -> None:
    with pytest.raises(ValueError):
        validate_product_query_plan(
            ProductQueryPlan(
                query="find products",
                filters={"credit_card": "anything"},
            )
        )

    with pytest.raises(ValueError):
        validate_product_query_plan(
            ProductQueryPlan(
                query="find products",
                category="order",
            )
        )
