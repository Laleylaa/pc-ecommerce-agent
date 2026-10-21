import importlib
import inspect
import json
from types import SimpleNamespace

import pytest
from app.core.config import Settings
from sqlalchemy.dialects import postgresql


def _context_module():
    return importlib.import_module("app.services.context")


def _context_schema_module():
    return importlib.import_module("app.schemas.context")


def test_context_service_exposes_async_turn_boundary() -> None:
    service_type = _context_module().ConversationContextService

    assert inspect.iscoroutinefunction(service_type.prepare_turn)
    assert inspect.iscoroutinefunction(service_type.complete_turn)


def test_history_selection_keeps_at_most_two_complete_recent_turns() -> None:
    context = _context_module()
    messages = [
        item
        for turn in range(7)
        for item in (
            {"role": "user", "content": f"user-{turn}"},
            {"role": "assistant", "content": f"assistant-{turn}"},
        )
    ]
    messages.append({"role": "user", "content": "failed-unmatched-user-message"})

    selection = context.select_complete_turns(messages, budget_tokens=10_000)

    assert [(item.role, item.content) for item in selection.messages] == [
        (role, f"{role}-{turn}")
        for turn in range(5, 7)
        for role in ("user", "assistant")
    ]
    assert selection.retained_turns == 2
    assert selection.dropped_turns == 5
    assert all(item.content != "failed-unmatched-user-message" for item in selection.messages)


def test_history_selection_obeys_deterministic_token_budget() -> None:
    context = _context_module()
    messages = [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "a2"},
        {"role": "user", "content": "u3"},
        {"role": "assistant", "content": "a3"},
    ]
    newest_pair_budget = sum(
        context.estimate_message_tokens(value) for value in ("u3", "a3")
    )

    first = context.select_complete_turns(messages, budget_tokens=newest_pair_budget)
    second = context.select_complete_turns(messages, budget_tokens=newest_pair_budget)

    assert first == second
    assert [(item.role, item.content) for item in first.messages] == [
        ("user", "u3"),
        ("assistant", "a3"),
    ]
    assert first.estimated_token_count == newest_pair_budget
    assert first.retained_turns == 1
    assert first.dropped_turns == 2


def test_v1_working_memory_upgrades_to_typed_v2_without_volatile_values() -> None:
    schemas = _context_schema_module()
    legacy = {
        "current_product_search": {
            "query": "无线鼠标",
            "category": "鼠标",
            "max_price": "500",
            "filters": {"connection_type": "Wireless"},
        },
        "recent_products": [
            {
                "spu_id": 10,
                "sku_id": 101,
                "title": "Example Mouse",
                "price": "199.00",
                "stock": 5,
                "specs": {"dpi": 12000},
            }
        ],
        "last_referenced_product": {
            "spu_id": 10,
            "sku_id": 101,
            "price": "199.00",
            "stock": 5,
        },
        "last_order_id": 202607020001,
        "last_policy_query": "退货政策",
        "recent_evidence": [
            {
                "source_type": "knowledge_document",
                "source_id": 9001,
                "title": "退货政策",
                "document_type": "policy",
                "snippet": "完整证据正文不应持久化",
            }
        ],
        "pending_handoff": {
            "order_id": 202607020001,
            "request_type": "return",
            "reason": "需要退货",
        },
        "logistics": {"carrier": "do-not-persist"},
    }

    memory = schemas.upgrade_working_memory(legacy)
    payload = memory.model_dump(mode="json", exclude_none=True)
    serialized = json.dumps(payload, ensure_ascii=False)

    assert memory.schema_version == 2
    assert memory.catalog.query_plan["query"] == "无线鼠标"
    assert memory.catalog.candidate_spu_ids == [10]
    assert memory.catalog.candidate_sku_ids == [101]
    assert memory.catalog.referenced_sku_id == 101
    assert memory.order.last_order_id == 202607020001
    assert memory.policy.evidence_refs[0].source_id == 9001
    assert memory.handoff.request_type == "return"
    for forbidden in ("199.00", "stock", "specs", "完整证据正文", "carrier"):
        assert forbidden not in serialized


def test_v2_query_plan_keeps_only_supported_constraint_fields() -> None:
    schemas = _context_schema_module()
    memory = schemas.WorkingMemoryV2.model_validate(
        {
            "catalog": {
                "query_plan": {
                    "query": "无线鼠标",
                    "category": "mouse",
                    "min_price": 100,
                    "max_price": 500,
                    "filters": {
                        "connection_type": "Wireless",
                        "stock": 5,
                        "specs": {"dpi": 12000},
                        "logistics": {"carrier": "secret"},
                    },
                    "sort": "recommend",
                    "limit": 3,
                    "sale_price": 199,
                    "inventory": 5,
                    "shipping_details": {"carrier": "secret"},
                    "excerpt": "secret",
                    "specs": {"dpi": 12000},
                    "snippet": "secret",
                }
            }
        }
    )

    assert memory.catalog.query_plan == {
        "query": "无线鼠标",
        "category": "mouse",
        "min_price": 100,
        "max_price": 500,
        "filters": {"connection_type": "Wireless"},
        "sort": "recommend",
        "limit": 3,
    }


def test_model_text_and_supplied_memory_do_not_replace_history_objects() -> None:
    context = _context_module()
    schemas = _context_schema_module()
    previous = schemas.WorkingMemoryV2.model_validate({
        "catalog": {"candidate_sku_ids": [101]},
        "order": {"candidate_order_ids": [88001], "last_order_id": 88001},
    })
    before = previous.model_dump()
    after = context._next_working_memory(previous, {
        "message": "查询SKU999和订单88002", "answer": "SKU999属于SPU99",
        "working_memory": {"catalog": {"candidate_sku_ids": [999]}},
        "context_objects": {"product_1": {"ids": [999]}},
        "goal_plan": {"goals": [{"query": "SKU999", "context_refs": ["product_1"]}]},
    })
    assert after.model_dump() == before
    assert previous.model_dump() == before
    # An unrelated later turn preserves the same historical identities.
    assert context._next_working_memory(after, {"answer": "退货政策说明"}).model_dump() == before
    refreshed = context._next_working_memory(after, {
        "catalog_tool_succeeded": True,
        "products": [{"sku_id": 202, "spu_id": 20, "entity_scope": "spu", "title": "新型号",
                      "spu_title": "新型号", "candidate_sku_ids": [202, 203]}],
    })
    assert refreshed.catalog.candidate_spu_ids == [20]
    assert refreshed.catalog.candidate_display[0].entity_scope == "spu"
    assert refreshed.catalog.candidate_display[0].candidate_sku_ids == [202, 203]
    assert previous.model_dump() == before


def test_order_window_is_persisted_without_order_details() -> None:
    context = _context_module()
    schemas = _context_schema_module()
    memory = context._next_working_memory(
        schemas.WorkingMemoryV2(),
        {
            "parsed": {
                "order_candidates": [
                    {"id": 202607020001},
                    {"id": 202607020002},
                ],
                "order_query": {
                    "query_mode": "recent",
                    "total_match_count": 7,
                    "returned_count": 2,
                    "is_exhaustive": False,
                    "next_offset": 2,
                },
            }
        },
    )

    assert memory.order.candidate_order_ids == [202607020001, 202607020002]
    assert memory.order.total_match_count == 7
    assert memory.order.returned_count == 2
    assert memory.order.is_exhaustive is False
    assert "next_offset" not in memory.order.model_dump()
    legacy = schemas.WorkingMemoryV2.model_validate({
        "order": {"next_offset": 2, "candidate_order_ids": [202607020001]},
        "catalog": {"search_page": {"offset": 2}, "candidate_sku_ids": [101]},
    })
    assert "next_offset" not in legacy.order.model_dump()
    assert "search_page" not in legacy.catalog.model_dump()
    assert legacy.order.candidate_order_ids == [202607020001]
    assert legacy.catalog.candidate_sku_ids == [101]


def test_single_analysis_focus_replaces_stale_last_order_reference() -> None:
    context = _context_module()
    schemas = _context_schema_module()
    previous = schemas.WorkingMemoryV2.model_validate(
        {
            "order": {
                "last_order_id": 991000000101,
                "candidate_order_ids": [991000000101],
            }
        }
    )

    memory = context._next_working_memory(
        previous,
        {
            "parsed": {
                "order_candidates": [],
                "order_context_ids": [991000000103],
                "order_query": {
                    "result_type": "order_analysis",
                    "query_mode": "analysis",
                    "total_match_count": 5,
                    "returned_count": 5,
                    "is_exhaustive": True,
                    "next_offset": None,
                },
            }
        },
    )

    assert memory.order.last_order_id == 991000000103
    assert memory.order.candidate_order_ids == [991000000103]


def test_multi_order_result_clears_stale_single_order_reference() -> None:
    context = _context_module()
    schemas = _context_schema_module()
    previous = schemas.WorkingMemoryV2.model_validate(
        {"order": {"last_order_id": 991000000101}}
    )

    memory = context._next_working_memory(
        previous,
        {
            "parsed": {
                "order_candidates": [
                    {"id": 991000000102},
                    {"id": 991000000103},
                ],
                "order_context_ids": [991000000102, 991000000103],
                "order_query": {
                    "result_type": "order_candidates",
                    "query_mode": "all",
                    "total_match_count": 5,
                    "returned_count": 2,
                    "is_exhaustive": False,
                    "next_offset": 2,
                },
            }
        },
    )

    assert memory.order.last_order_id is None
    assert memory.order.candidate_order_ids == [991000000102, 991000000103]


def test_compact_audit_omits_context_payloads_and_keeps_metrics() -> None:
    context = _context_module()
    audit = context.serialize_compact_audit(
        {
            "intent": "product_recommendation",
            "history": [{"role": "user", "content": "secret history"}],
            "memory": [{"key": "preference", "value": "secret memory"}],
            "working_memory": {"catalog": {"candidate_sku_ids": [101]}},
        },
        estimated_token_count=42,
        retained_turns=2,
        dropped_turns=3,
        applied_memory_ids=[7, 9],
    )

    assert audit == {
        "intent": "product_recommendation",
        "estimated_token_count": 42,
        "retained_turns": 2,
        "dropped_turns": 3,
        "applied_memory_ids": [7, 9],
    }


def test_context_budget_defaults_to_6000_and_reads_env(monkeypatch) -> None:
    assert Settings().agent_context_budget_tokens == 6000

    monkeypatch.setenv("AGENT_CONTEXT_BUDGET_TOKENS", "321")

    assert Settings().agent_context_budget_tokens == 321


@pytest.mark.asyncio
async def test_prepare_and_complete_turn_own_context_persistence_and_audit() -> None:
    context = _context_module()
    memory = SimpleNamespace(
        id=7,
        scope="user",
        fact_type="preference",
        key="connection_preference",
        value="偏好无线设备",
        value_json={"preference": "wireless", "operation": "set"},
        confidence=0.8,
    )
    repository = FakeContextRepository(memory)
    session = FakeContextSession(repository.run)
    service = context.ConversationContextService(
        session,
        Settings(llm_api_key="", agent_context_budget_tokens=6000),
        repository=repository,
    )

    prepared = await service.prepare_turn(1, None, "以后不要无线")
    changes = await service.complete_turn(
        prepared,
        {
            "message": prepared.message,
            "answer": "已记录",
            "intent": "product_recommendation",
            "boundary": {"classification": "in_scope_auto"},
            "applied_memory_ids": ["invalid", True, 7.9, "7", 999, 7],
            "parsed": {
                "product_search": {
                    "query": "推荐一个鼠标",
                    "selection_scope": "spu",
                }
            },
            "products": [
                SimpleNamespace(
                    spu_id=10,
                    sku_id=101,
                    title="Test Mouse",
                    spu_title="Test Mouse Series",
                    entity_scope="spu",
                    brand="Razer",
                    category="mouse",
                    image_url="/mouse.png",
                    price="199",
                    stock=5,
                    specs={},
                )
            ],
        },
    )

    assert prepared.retained_turns == 1
    assert repository.messages[-1][1:] == ("assistant", "已记录")
    assert repository.upserts[0]["value_json"]["operation"] == "exclude"
    assert repository.marked_ids == [7]
    assert repository.updated_working_memory["schema_version"] == 2
    assert repository.updated_working_memory["catalog"]["candidate_display"] == [
        {
            "spu_id": 10,
            "sku_id": 101,
            "title": "Test Mouse Series",
            "entity_scope": "spu",
            "brand": "Razer",
            "category": "mouse",
            "image_url": "/mouse.png",
            "candidate_sku_ids": [],
            "match_kind": None,
        }
    ]
    assert repository.updated_working_memory["catalog"]["query_plan"][
        "selection_scope"
    ] == "spu"
    serialized = json.dumps(repository.updated_working_memory, ensure_ascii=False)
    assert "199" not in serialized
    assert "stock" not in serialized
    assert "specs" not in serialized
    assert repository.finished_state == changes.audit
    assert "history" not in changes.audit
    assert "memory" not in changes.audit
    assert "working_memory" not in changes.audit
    assert changes.applied_memory_ids == [7]
    assert [item.model_dump(mode="json") for item in changes.memory_changes] == [
        {
            "action": "updated",
            "memory_id": 88,
            "key": "connection_preference",
            "display_value": "不偏好无线设备",
        }
    ]
    assert session.committed is True


def test_memory_upsert_statement_is_atomic_for_active_identity() -> None:
    repositories = importlib.import_module("app.repositories.conversations")
    statement = repositories._memory_upsert_statement(
        user_id=1,
        key="brand_preference",
        value="偏好 罗技 品牌",
        confidence=0.8,
        scope="user",
        fact_type="preference",
        value_json={"brand": "罗技"},
        source_message_id=9,
        expires_at=None,
        now=SimpleNamespace(),
    )
    sql = str(statement.compile(dialect=postgresql.dialect()))

    assert "ON CONFLICT" in sql
    assert "disabled_at IS NULL" in sql
    assert "xmax = 0" in sql


def test_disable_memory_statement_is_one_atomic_guarded_update() -> None:
    repositories = importlib.import_module("app.repositories.conversations")
    statement = repositories._disable_memory_statement(
        user_id=7,
        memory_id=11,
        now=SimpleNamespace(),
    )
    sql = str(statement.compile(dialect=postgresql.dialect()))

    assert sql.startswith("UPDATE memory_fact SET")
    assert "RETURNING memory_fact.id" in sql
    assert "memory_fact.user_id" in sql
    assert "memory_fact.disabled_at IS NULL" in sql
    assert "memory_fact.expires_at IS NULL OR memory_fact.expires_at >" in sql
    assert "memory_fact.origin" in sql
    assert "memory_fact.value_json IS NOT NULL" in sql


@pytest.mark.asyncio
async def test_disable_memory_returns_false_when_atomic_update_returns_no_id() -> None:
    repositories = importlib.import_module("app.repositories.conversations")

    class Result:
        def __init__(self, value: int | None) -> None:
            self.value = value

        def scalar_one_or_none(self) -> int | None:
            return self.value

    class Session:
        def __init__(self) -> None:
            self.results = iter([Result(11), Result(None)])
            self.statements = []

        async def execute(self, statement):
            self.statements.append(statement)
            return next(self.results)

    session = Session()
    repository = repositories.ConversationRepository(session)

    assert await repository.disable_memory(7, 11) is True
    assert await repository.disable_memory(7, 11) is False
    assert len(session.statements) == 2


@pytest.mark.asyncio
async def test_memory_change_action_comes_from_atomic_upsert_result() -> None:
    context = _context_module()
    existing = SimpleNamespace(
        id=7,
        scope="user",
        fact_type="preference",
        key="connection_preference",
        value="偏好无线设备",
        value_json={"preference": "wireless"},
        confidence=0.8,
    )
    repository = FakeContextRepository(existing, upsert_created=True)
    session = FakeContextSession(repository.run)
    service = context.ConversationContextService(
        session,
        Settings(llm_api_key=""),
        repository=repository,
    )

    prepared = await service.prepare_turn(1, None, "以后不要无线")
    changes = await service.complete_turn(
        prepared,
        {
            "answer": "已记录",
            "intent": "product_recommendation",
            "boundary": {"classification": "in_scope_auto"},
        },
    )

    assert changes.memory_changes[0].action == "created"


def test_applied_memory_ids_reject_booleans_and_fractional_numbers() -> None:
    context = _context_module()

    assert context._valid_memory_ids([True, 7.9], {1, 7}) == []


def test_successful_empty_catalog_result_clears_candidates_but_failure_preserves_them() -> None:
    context = _context_module()
    schemas = _context_schema_module()
    previous = schemas.WorkingMemoryV2.model_validate(
        {
            "catalog": {
                "query_plan": {"query": "old mouse"},
                "candidate_spu_ids": [10],
                "candidate_sku_ids": [101],
                "candidate_display": [{"spu_id": 10, "sku_id": 101, "title": "Old"}],
            }
        }
    )

    success = context._next_working_memory(
        previous,
        {
            "parsed": {"product_search": {"query": "no match"}},
            "products": [],
            "catalog_tool_succeeded": True,
        },
    )
    failure = context._next_working_memory(
        previous,
        {
            "products": [],
            "catalog_tool_succeeded": False,
        },
    )

    assert success.catalog.candidate_spu_ids == []
    assert success.catalog.candidate_sku_ids == []
    assert success.catalog.candidate_display == []
    assert failure.catalog.candidate_sku_ids == [101]


def test_compare_metadata_does_not_overwrite_stable_catalog_search_plan() -> None:
    context = _context_module()
    schemas = _context_schema_module()
    search_plan = {
        "query": "fps ergonomic mouse",
        "category": "mouse",
        "brands": ["Razer"],
        "min_price": 200,
        "max_price": 500,
        "filters": {
            "connection_type": "Wired",
            "max_dpi": "20000",
            "hand_orientation": "Right",
        },
        "keywords": ["fps", "lightweight"],
        "usage_scenario": "gaming",
        "sort": "price_asc",
        "limit": 6,
    }
    previous = schemas.WorkingMemoryV2.model_validate(
        {"catalog": {"query_plan": search_plan, "candidate_sku_ids": [101, 102]}}
    )

    after_compare = context._next_working_memory(
        previous,
        {
            "parsed": {
                "catalog_comparison": {
                    "query": "对比第一个和第二个",
                    "sku_ids": [101, 102],
                    "comparison_fields": ["price", "max_dpi"],
                }
            },
            "products": [],
            "catalog_tool_succeeded": True,
        },
    )

    assert after_compare.catalog.query_plan == search_plan
    assert "comparison" not in after_compare.catalog.model_dump()


@pytest.mark.asyncio
async def test_prepare_turn_audits_complete_turns_dropped_beyond_recent_64_messages() -> None:
    context = _context_module()

    class LongHistoryRepository(FakeContextRepository):
        async def list_recent_messages(self, _conversation_id: int, limit: int):
            assert limit == 64
            return [
                SimpleNamespace(role=role, content=f"{role}-{turn}")
                for turn in range(32)
                for role in ("user", "assistant")
            ]

        async def count_complete_turns(self, _conversation_id: int) -> int:
            return 70

    memory = SimpleNamespace(
        id=7,
        scope="user",
        fact_type="preference",
        key="connection_preference",
        value="偏好无线设备",
        value_json={"preference": "wireless"},
        confidence=0.8,
    )
    repository = LongHistoryRepository(memory)
    service = context.ConversationContextService(
        FakeContextSession(repository.run),
        Settings(llm_api_key="", agent_context_budget_tokens=10_000),
        repository=repository,
    )

    prepared = await service.prepare_turn(1, None, "new turn")

    assert prepared.retained_turns == 2
    assert prepared.dropped_turns == 68


class FakeContextRepository:
    def __init__(self, memory, *, upsert_created: bool = False) -> None:
        self.memory = memory
        self.upsert_created = upsert_created
        self.run = SimpleNamespace(id=44)
        self.messages: list[tuple[int, str, str]] = []
        self.upserts: list[dict] = []
        self.marked_ids: list[int] = []
        self.updated_working_memory: dict = {}
        self.finished_state: dict = {}

    async def get_or_create(self, _user_id: int, _conversation_id: int | None):
        return SimpleNamespace(id=33)

    async def list_recent_messages(self, _conversation_id: int, limit: int):
        assert limit == 64
        return [
            SimpleNamespace(role="user", content="上一轮"),
            SimpleNamespace(role="assistant", content="上一轮回答"),
            SimpleNamespace(role="user", content="失败消息"),
        ]

    async def count_complete_turns(self, _conversation_id: int) -> int:
        return 1

    async def get_working_memory(self, _conversation_id: int):
        return {"recent_products": [{"spu_id": 9, "sku_id": 99, "price": "99"}]}

    async def list_memory(self, _user_id: int):
        return [self.memory]

    async def add_message(self, conversation_id: int, role: str, content: str, metadata=None):
        self.messages.append((conversation_id, role, content))
        return SimpleNamespace(id=len(self.messages))

    async def start_run(self, _conversation_id: int):
        return self.run

    async def upsert_memory(
        self,
        _user_id: int,
        _key: str,
        _value: str,
        _confidence: float,
        **kwargs,
    ):
        self.upserts.append(kwargs)
        return SimpleNamespace(
            memory=SimpleNamespace(id=88),
            created=self.upsert_created,
        )

    async def update_working_memory(self, _conversation_id: int, value: dict):
        self.updated_working_memory = value

    async def mark_memory_used(self, _user_id: int, memory_ids: list[int]):
        self.marked_ids = memory_ids
        return len(memory_ids)

    async def finish_run(self, _run, _intent: str, state: dict):
        self.finished_state = state


class FakeContextSession:
    def __init__(self, run) -> None:
        self.run = run
        self.committed = False

    async def get(self, _model, _run_id: int):
        return self.run

    async def commit(self):
        self.committed = True


def test_retired_comparison_memory_loads_without_changing_other_context():
    from copy import deepcopy

    from app.schemas.context import WorkingMemoryV2

    old = {"catalog": {
        "candidate_sku_ids": [101, 102], "referenced_sku_id": 101,
        "comparison": {"query": "比较两款", "sku_ids": [101, 102], "comparison_level": "sku"},
    }, "order": {"candidate_order_ids": [901]}}
    before = deepcopy(old)
    memory = WorkingMemoryV2.model_validate(old)
    assert old == before
    assert memory.catalog.candidate_sku_ids == [101, 102]
    assert memory.catalog.referenced_sku_id == 101
    assert memory.order.candidate_order_ids == [901]
    assert "comparison" not in memory.catalog.model_dump()
