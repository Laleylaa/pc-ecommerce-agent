import json
from collections.abc import Mapping, Sequence
from math import ceil
from typing import Any

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.models import AgentRun
from app.repositories.conversations import ConversationRepository
from app.schemas.context import (
    CatalogDisplayIdentity,
    CatalogMemory,
    ContextMessage,
    EvidenceReference,
    HandoffMemory,
    HistorySelection,
    MemoryChanges,
    PolicyMemory,
    PreparedTurn,
    StructuredMemory,
    WorkingMemoryV2,
    upgrade_working_memory,
)
from app.schemas.memory import MemoryChange
from app.services.memory import MemoryService

MAX_CONTEXT_TURNS = 2
AUDIT_OMITTED_FIELDS = {"history", "memory", "working_memory"}


def estimate_message_tokens(content: str) -> int:
    """Estimate tokens without a tokenizer or model call.

    UTF-8 byte length handles Chinese and ASCII consistently. One token of fixed
    overhead accounts for the chat role/message boundary.
    """
    return 1 + max(1, ceil(len(content.encode("utf-8")) / 4))


def select_complete_turns(
    messages: Sequence[Any],
    budget_tokens: int,
    max_turns: int = MAX_CONTEXT_TURNS,
) -> HistorySelection:
    pairs: list[tuple[ContextMessage, ContextMessage]] = []
    pending_user: ContextMessage | None = None
    for raw_message in messages:
        role = _message_value(raw_message, "role")
        content = _message_value(raw_message, "content")
        if role == "user":
            pending_user = ContextMessage(role="user", content=str(content or ""))
        elif role == "assistant" and pending_user is not None:
            assistant = ContextMessage(role="assistant", content=str(content or ""))
            pairs.append((pending_user, assistant))
            pending_user = None

    selected: list[tuple[ContextMessage, ContextMessage]] = []
    estimated_tokens = 0
    for pair in reversed(pairs):
        if len(selected) >= max(0, max_turns):
            break
        pair_tokens = sum(estimate_message_tokens(item.content) for item in pair)
        if estimated_tokens + pair_tokens > max(0, budget_tokens):
            break
        selected.append(pair)
        estimated_tokens += pair_tokens

    selected.reverse()
    return HistorySelection(
        messages=[item for pair in selected for item in pair],
        estimated_token_count=estimated_tokens,
        retained_turns=len(selected),
        dropped_turns=len(pairs) - len(selected),
    )


def serialize_compact_audit(
    state: Mapping[str, Any],
    *,
    estimated_token_count: int,
    retained_turns: int,
    dropped_turns: int,
    applied_memory_ids: Sequence[int],
) -> dict[str, Any]:
    audit = {
        key: _json_value(value)
        for key, value in state.items()
        if key not in AUDIT_OMITTED_FIELDS
    }
    audit.update(
        {
            "estimated_token_count": estimated_token_count,
            "retained_turns": retained_turns,
            "dropped_turns": dropped_turns,
            "applied_memory_ids": list(dict.fromkeys(applied_memory_ids)),
        }
    )
    return audit


class ConversationContextService:
    def __init__(
        self,
        session: AsyncSession,
        settings: Settings | None = None,
        repository: ConversationRepository | None = None,
        memory_service: MemoryService | None = None,
    ) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.repository = repository or ConversationRepository(session)
        self.memory_service = memory_service or MemoryService()

    async def prepare_turn(
        self, user_id: int, conversation_id: int | None, message: str
    ) -> PreparedTurn:
        conversation = await self.repository.get_or_create(user_id, conversation_id)
        raw_history = await self.repository.list_recent_messages(conversation.id, limit=64)
        total_complete_turns = await self.repository.count_complete_turns(conversation.id)
        history = select_complete_turns(
            raw_history,
            budget_tokens=self.settings.agent_context_budget_tokens,
        )
        history.dropped_turns = max(0, total_complete_turns - history.retained_turns)
        working_memory = upgrade_working_memory(
            await self.repository.get_working_memory(conversation.id)
        )
        memories = await self.repository.list_memory(user_id)
        user_message = await self.repository.add_message(conversation.id, "user", message)
        run = await self.repository.start_run(conversation.id)
        prepared = PreparedTurn(
            user_id=user_id,
            conversation_id=conversation.id,
            user_message_id=user_message.id,
            run_id=run.id,
            message=message,
            history=history.messages,
            memory=[
                StructuredMemory(
                    id=item.id,
                    scope=item.scope,
                    fact_type=item.fact_type,
                    key=item.key,
                    value=item.value,
                    value_json=item.value_json,
                    confidence=item.confidence,
                )
                for item in memories
            ],
            working_memory=working_memory,
            estimated_token_count=history.estimated_token_count,
            retained_turns=history.retained_turns,
            dropped_turns=history.dropped_turns,
        )
        # The accepted request survives a later answer/memory transaction rollback.
        await self.session.commit()
        return prepared

    async def complete_turn(
        self, prepared_turn: PreparedTurn, outcome: Mapping[str, Any] | BaseModel
    ) -> MemoryChanges:
        state = (
            outcome.model_dump(mode="python")
            if isinstance(outcome, BaseModel)
            else dict(outcome)
        )
        answer = str(state.get("answer") or "")
        await self.repository.add_message(
            prepared_turn.conversation_id,
            "assistant",
            answer,
            state.get("assistant_metadata"),
        )

        upserted_memory_ids: list[int] = []
        memory_changes: list[MemoryChange] = []
        if _allows_long_term_memory(state):
            for fact in self.memory_service.extract_long_term_facts(prepared_turn.message):
                upsert = await self.repository.upsert_memory(
                    prepared_turn.user_id,
                    fact["key"],
                    fact["value"],
                    fact["confidence"],
                    scope=fact["scope"],
                    fact_type=fact["fact_type"],
                    value_json=fact["value_json"],
                    source_message_id=prepared_turn.user_message_id,
                )
                persisted = upsert.memory
                upserted_memory_ids.append(persisted.id)
                memory_changes.append(
                    MemoryChange(
                        action="created" if upsert.created else "updated",
                        memory_id=persisted.id,
                        key=fact["key"],
                        display_value=fact["value"],
                    )
                )

        working_memory = _next_working_memory(prepared_turn.working_memory, state)
        await self.repository.update_working_memory(
            prepared_turn.conversation_id,
            working_memory.model_dump(mode="json"),
        )

        available_memory_ids = {item.id for item in prepared_turn.memory}
        applied_memory_ids = _valid_memory_ids(
            state.get("applied_memory_ids", []), available_memory_ids
        )
        await self.repository.mark_memory_used(prepared_turn.user_id, applied_memory_ids)
        audit = serialize_compact_audit(
            state,
            estimated_token_count=prepared_turn.estimated_token_count,
            retained_turns=prepared_turn.retained_turns,
            dropped_turns=prepared_turn.dropped_turns,
            applied_memory_ids=applied_memory_ids,
        )
        run = await self.session.get(AgentRun, prepared_turn.run_id)
        if run is not None:
            await self.repository.finish_run(run, str(state.get("intent") or "general"), audit)
        await self.session.commit()
        return MemoryChanges(
            working_memory=working_memory,
            upserted_memory_ids=upserted_memory_ids,
            applied_memory_ids=applied_memory_ids,
            memory_changes=memory_changes,
            audit=audit,
        )


def _next_working_memory(previous: WorkingMemoryV2, state: dict[str, Any]) -> WorkingMemoryV2:
    # The service owns memory writes. Model plans/text and caller-supplied memory are not facts;
    # only runtime projections of tool results may refresh object identities below.
    catalog = previous.catalog.model_copy(deep=True)
    parsed = state.get("parsed") if isinstance(state.get("parsed"), dict) else {}
    query_plan = parsed.get("product_search")
    if isinstance(query_plan, dict):
        catalog.query_plan = CatalogMemory(query_plan=query_plan).query_plan
    products = state.get("products") if isinstance(state.get("products"), list) else []
    if products or state.get("catalog_tool_succeeded") is True:
        catalog.candidate_spu_ids = _object_ids(products, "spu_id")
        catalog.candidate_sku_ids = _object_ids(products, "sku_id")
        catalog.candidate_display = _catalog_display_identities(products)
        if not products:
            catalog.referenced_spu_id = None
            catalog.referenced_sku_id = None
    referenced = parsed.get("referenced_product")
    if referenced is not None:
        catalog.referenced_spu_id = _optional_object_int(referenced, "spu_id")
        catalog.referenced_sku_id = _optional_object_int(referenced, "sku_id")

    order_memory = previous.order.model_copy(deep=True)
    order = state.get("order")
    order_candidates = parsed.get("order_candidates")
    order_context_ids = parsed.get("order_context_ids")
    order_query = parsed.get("order_query")
    if isinstance(order_query, dict):
        order_memory.pending_suggestions = type(order_memory).model_validate({
            "pending_suggestions": order_query.get("pending_suggestions", []),
        }).pending_suggestions
        if order_query.get("result_type") == "order_suggestions":
            order = None
            order_context_ids = []
        current_order_id = _optional_object_int(order, "id") if order is not None else None
        if current_order_id is not None:
            context_order_ids = [current_order_id]
        elif isinstance(order_context_ids, list):
            context_order_ids = list(
                dict.fromkeys(
                    item
                    for item in order_context_ids
                    if isinstance(item, int) and not isinstance(item, bool)
                )
            )
        elif isinstance(order_candidates, list):
            context_order_ids = _object_ids(order_candidates, "id")
        else:
            context_order_ids = []
        if order_query.get("result_type") != "order_count":
            order_memory.candidate_order_ids = context_order_ids
            order_memory.last_order_id = (
                context_order_ids[0] if len(context_order_ids) == 1 else None
            )
        order_memory.total_match_count = int(order_query.get("total_match_count") or 0)
        order_memory.returned_count = int(order_query.get("returned_count") or 0)
        order_memory.is_exhaustive = bool(order_query.get("is_exhaustive", True))

    policy = previous.policy.model_copy(deep=True)
    evidence = state.get("evidence") if isinstance(state.get("evidence"), list) else []
    if evidence:
        policy = PolicyMemory(
            last_query=str(state.get("message") or ""),
            evidence_refs=[
                EvidenceReference.model_validate(_object_mapping(item)) for item in evidence
            ],
        )

    handoff = previous.handoff.model_copy(deep=True)
    boundary = state.get("boundary") if isinstance(state.get("boundary"), dict) else {}
    if boundary.get("classification") == "human_handoff_required":
        draft = _build_handoff_state(state, previous)
        handoff = HandoffMemory.model_validate(draft)
    return WorkingMemoryV2(catalog=catalog, order=order_memory, policy=policy, handoff=handoff)


def _build_handoff_state(state: dict[str, Any], previous: WorkingMemoryV2) -> dict[str, Any]:
    message = str(state.get("message") or "")
    request_type = "other"
    if "退款" in message:
        request_type = "refund"
    elif "退货" in message or "退" in message:
        request_type = "return"
    elif "维修" in message or "保修" in message:
        request_type = "repair"
    elif any(term in message for term in ("取消订单", "改地址", "改收货", "修改订单")):
        request_type = "order_change"
    return {
        "order_id": previous.order.last_order_id,
        "request_type": request_type,
        "reason": message,
    }


def _allows_long_term_memory(state: dict[str, Any]) -> bool:
    boundary = state.get("boundary")
    return not isinstance(boundary, dict) or boundary.get("classification") == "in_scope_auto"


def _message_value(message: Any, key: str) -> Any:
    if isinstance(message, Mapping):
        return message.get(key)
    return getattr(message, key, None)


def _object_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="python")
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def _object_ids(items: list[Any], key: str) -> list[int]:
    return list(
        dict.fromkeys(
            value
            for item in items
            if (value := _optional_object_int(item, key)) is not None
        )
    )


def _catalog_display_identities(items: list[Any]) -> list[CatalogDisplayIdentity]:
    identities: list[CatalogDisplayIdentity] = []
    for item in items:
        spu_id = _optional_object_int(item, "spu_id")
        sku_id = _optional_object_int(item, "sku_id")
        entity_scope = _message_value(item, "entity_scope")
        title = (
            _message_value(item, "spu_title")
            if entity_scope == "spu"
            else _message_value(item, "title")
        )
        if spu_id is None or sku_id is None or not title:
            continue
        identities.append(
            CatalogDisplayIdentity(
                spu_id=spu_id,
                sku_id=sku_id,
                title=str(title),
                entity_scope=(entity_scope if entity_scope in {"sku", "spu"} else "sku"),
                brand=_optional_string(_message_value(item, "brand")),
                category=_optional_string(_message_value(item, "category")),
                image_url=_optional_string(_message_value(item, "image_url")),
                candidate_sku_ids=_message_value(item, "candidate_sku_ids") or [],
                match_kind=_message_value(item, "match_kind"),
            )
        )
    return identities


def _optional_string(value: Any) -> str | None:
    return str(value) if value is not None else None


def _optional_object_int(value: Any, key: str) -> int | None:
    raw = _message_value(value, key)
    return int(raw) if raw is not None else None


def _json_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return str(value)
    return value


def _valid_memory_ids(values: Any, available_ids: set[int]) -> list[int]:
    if not isinstance(values, (list, tuple)):
        return []
    valid: list[int] = []
    for value in values:
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            memory_id = value
        elif isinstance(value, str) and value.isdigit():
            memory_id = int(value)
        else:
            continue
        if memory_id > 0 and memory_id in available_ids and memory_id not in valid:
            valid.append(memory_id)
    return valid
