"""Deterministic run-local Artifact Store for the Plan-and-Execute runtime."""

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.agent.outcomes import normalize_tool_result
from app.agent.routing import RoutedTask, tool_planning_subqueries

TaskExecutionStatus = Literal[
    "pending",
    "ready",
    "running",
    "succeeded",
    "unavailable",
    "failed",
    "blocked",
]


class TaskArtifactRecord(BaseModel):
    """One schema-bounded artifact extracted from one Tool execution."""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    goal_id: str
    artifact_type: str
    usable: bool
    value: Any = None
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    source_tool_call_id: str
    source_tool_name: str
    extractor: Literal["deterministic", "schema_llm"] = "deterministic"
    reason: str = ""


def initialize_task_runtime(state: dict[str, Any]) -> None:
    """Create immutable-DAG execution state immediately after routing."""
    state["task_artifacts"] = {}
    state["task_status"] = {
        task.id: {
            "task_id": task.id,
            "goal_id": task.goal_id,
            "answer_role": task.answer_role,
            "status": "pending",
            "reason": "awaiting_runtime_scheduling",
        }
        for task in tool_planning_subqueries(state.get("route_plan"))
    }
    refresh_task_status(state)


def extract_wave_artifacts(state: dict[str, Any]) -> None:
    """Normalize the latest wave into task-addressed artifacts without summarizing facts."""
    waves = state.get("tool_waves", [])
    if not waves:
        return
    _extract_artifacts_for_wave(state, waves[-1])


def ensure_task_runtime(state: dict[str, Any]) -> None:
    """Hydrate old in-memory states through the same deterministic extractor path."""
    if "task_status" in state and "task_artifacts" in state:
        return
    initialize_task_runtime(state)
    for wave in state.get("tool_waves", []):
        if isinstance(wave, Mapping):
            _extract_artifacts_for_wave(state, wave)
    refresh_task_status(state)


def _extract_artifacts_for_wave(
    state: dict[str, Any], wave: Mapping[str, Any]
) -> None:
    calls = {
        str(call.get("id") or ""): call
        for call in wave.get("calls", [])
        if isinstance(call, Mapping)
    }
    tasks = {
        task.id: task for task in tool_planning_subqueries(state.get("route_plan"))
    }
    store = state.setdefault("task_artifacts", {})
    for result in wave.get("results", []):
        if not isinstance(result, Mapping):
            continue
        call_id = str(result.get("tool_call_id") or "")
        call = calls.get(call_id, {})
        task_id = str(call.get("subquery") or "").strip()
        task = tasks.get(task_id)
        if task is None:
            continue
        artifact = _extract_task_artifact(task, call, result)
        store[task_id] = artifact.model_dump(mode="json")


def refresh_task_status(state: dict[str, Any]) -> None:
    """Derive ready/blocked/terminal states from the frozen DAG and Artifact Store."""
    tasks = tool_planning_subqueries(state.get("route_plan"))
    artifacts = state.get("task_artifacts", {})
    statuses = state.setdefault("task_status", {})
    attempted = _attempted_task_outcomes(state)

    # First settle every attempted Task independently of Router output ordering.
    for task in tasks:
        existing = statuses.setdefault(
            task.id,
            {
                "task_id": task.id,
                "goal_id": task.goal_id,
                "answer_role": task.answer_role,
            },
        )
        artifact = artifacts.get(task.id) if isinstance(artifacts, Mapping) else None
        if isinstance(artifact, Mapping) and artifact.get("usable"):
            existing.update(status="succeeded", reason="artifact_usable")
            continue
        clarification = _tool_clarification_request(state, task.id)
        if clarification is not None:
            existing.update(
                status="blocked",
                reason=f"tool_requested_clarification:{clarification['code']}",
                missing_information=[clarification["missing_information"]],
                user_can_supply=True,
            )
            continue
        outcome = attempted.get(task.id)
        if outcome in {"empty", "not_found", "unsupported"}:
            existing.update(status="unavailable", reason=f"tool_outcome:{outcome}")
            continue
        if outcome in {"insufficient", "error"}:
            existing.update(status="failed", reason=f"tool_outcome:{outcome}")
            continue
        if isinstance(artifact, Mapping):
            existing.update(
                status="unavailable",
                reason=str(artifact.get("reason") or "artifact_not_usable"),
            )
            continue
        existing.update(status="pending", reason="awaiting_runtime_scheduling")

    # Then compute dependency readiness from the settled terminal states.
    for task in tasks:
        existing = statuses[task.id]
        if existing.get("status") != "pending":
            continue
        dependency_states = {
            dependency_id: str(statuses.get(dependency_id, {}).get("status") or "pending")
            for dependency_id in task.depends_on
        }
        terminal_dependency = next(
            (
                dependency_id
                for dependency_id, status in dependency_states.items()
                if status in {"unavailable", "failed", "blocked"}
            ),
            None,
        )
        if terminal_dependency is not None:
            existing.update(
                status="blocked",
                reason=f"dependency_artifact_unusable:{terminal_dependency}",
                user_can_supply=False,
            )
            continue
        if any(status != "succeeded" for status in dependency_states.values()):
            existing.update(status="pending", reason="waiting_for_dependencies")
            continue

        missing, user_can_supply = _missing_input_requirements(state, task)
        if missing:
            existing.update(
                status="blocked",
                reason=f"missing_context_artifact:{','.join(missing)}",
                missing_information=missing,
                user_can_supply=user_can_supply,
            )
            continue
        existing.pop("missing_information", None)
        existing.pop("user_can_supply", None)
        existing.update(status="ready", reason="dependencies_and_inputs_usable")


def ready_tasks(state: Mapping[str, Any]) -> list[RoutedTask]:
    statuses = state.get("task_status", {})
    if not isinstance(statuses, Mapping):
        return []
    return [
        task
        for task in tool_planning_subqueries(state.get("route_plan"))
        if statuses.get(task.id, {}).get("status") == "ready"
    ]


def user_clarifiable_blockers(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    statuses = state.get("task_status", {})
    if not isinstance(statuses, Mapping):
        return []
    return [
        dict(item)
        for item in statuses.values()
        if isinstance(item, Mapping)
        and item.get("status") == "blocked"
        and item.get("user_can_supply") is True
    ]


def bound_task_sku_ids(state: Mapping[str, Any], task: RoutedTask) -> list[int]:
    """Bind compare inputs from memory and direct dependency artifacts."""
    resolved: list[int] = []
    for requirement in task.input_requirements:
        if requirement.source == "comparison_context":
            catalog = _mapping(state.get("working_memory_snapshot") or state.get("working_memory"))
            comparison = _mapping(_mapping(catalog.get("catalog")).get("comparison"))
            _extend_positive_ints(resolved, comparison.get("sku_ids"))
        elif requirement.source == "catalog_candidates":
            catalog = _working_catalog_memory(state)
            values = _selected_candidate_values(
                catalog.get("candidate_sku_ids"),
                requirement.positions,
            )
            _extend_positive_ints(resolved, values)
        elif requirement.source == "context_product":
            catalog = _mapping(state.get("working_memory_snapshot") or state.get("working_memory"))
            catalog = _mapping(catalog.get("catalog"))
            value = catalog.get("referenced_sku_id")
            if value is None:
                candidates = catalog.get("candidate_sku_ids")
                value = candidates[0] if isinstance(candidates, list) and candidates else None
            _extend_positive_ints(resolved, [value])
        elif requirement.task_id is not None:
            artifacts = _mapping(state.get("task_artifacts"))
            artifact = _mapping(artifacts.get(requirement.task_id))
            value = _mapping(artifact.get("value"))
            _extend_positive_ints(resolved, value.get("selected_sku_ids"))
            products = value.get("products")
            if isinstance(products, list):
                _extend_positive_ints(
                    resolved,
                    [item.get("sku_id") for item in products if isinstance(item, Mapping)],
                )
    for value in _direct_dependency_artifact_values(state, task):
        _extend_positive_ints(resolved, value.get("selected_sku_ids"))
        products = value.get("products")
        if isinstance(products, list):
            _extend_positive_ints(
                resolved,
                [item.get("sku_id") for item in products if isinstance(item, Mapping)],
            )
    return resolved[:10]


def bound_task_spu_ids(state: Mapping[str, Any], task: RoutedTask) -> list[int]:
    """Bind series inputs from memory and direct dependency artifacts."""
    resolved: list[int] = []
    for requirement in task.input_requirements:
        if requirement.source == "comparison_context":
            snapshot = _mapping(
                state.get("working_memory_snapshot") or state.get("working_memory")
            )
            comparison = _mapping(_mapping(snapshot.get("catalog")).get("comparison"))
            _extend_positive_ints(resolved, comparison.get("spu_ids"))
        elif requirement.source == "catalog_candidates":
            catalog = _working_catalog_memory(state)
            values = _selected_candidate_values(
                catalog.get("candidate_spu_ids"),
                requirement.positions,
            )
            _extend_positive_ints(resolved, values)
        elif requirement.source == "context_product":
            snapshot = _mapping(
                state.get("working_memory_snapshot") or state.get("working_memory")
            )
            catalog = _mapping(snapshot.get("catalog"))
            value = catalog.get("referenced_spu_id")
            if value is None:
                candidates = catalog.get("candidate_spu_ids")
                value = candidates[0] if isinstance(candidates, list) and candidates else None
            _extend_positive_ints(resolved, [value])
        elif requirement.task_id is not None:
            artifacts = _mapping(state.get("task_artifacts"))
            artifact = _mapping(artifacts.get(requirement.task_id))
            value = _mapping(artifact.get("value"))
            _extend_positive_ints(resolved, value.get("selected_spu_ids"))
            products = value.get("products")
            if isinstance(products, list):
                _extend_positive_ints(
                    resolved,
                    [item.get("spu_id") for item in products if isinstance(item, Mapping)],
                )
    for value in _direct_dependency_artifact_values(state, task):
        _extend_positive_ints(resolved, value.get("selected_spu_ids"))
        products = value.get("products")
        if isinstance(products, list):
            _extend_positive_ints(
                resolved,
                [item.get("spu_id") for item in products if isinstance(item, Mapping)],
            )
    return resolved[:10]


def bound_task_catalog_targets(
    state: Mapping[str, Any],
    task: RoutedTask,
) -> list[dict[str, Any]]:
    """Bind trusted identities from memory and every direct dependency Artifact."""
    targets: list[dict[str, Any]] = []
    for requirement in task.input_requirements:
        if requirement.source == "comparison_context":
            snapshot = _mapping(
                state.get("working_memory_snapshot") or state.get("working_memory")
            )
            comparison = _mapping(_mapping(snapshot.get("catalog")).get("comparison"))
            _append_identity_lists(
                targets,
                comparison.get("sku_ids"),
                comparison.get("spu_ids"),
                source="comparison_context",
            )
        elif requirement.source == "catalog_candidates":
            catalog = _working_catalog_memory(state)
            displays = catalog.get("candidate_display")
            if isinstance(displays, list) and displays:
                for value in _selected_candidate_values(
                    displays,
                    requirement.positions,
                ):
                    item = _mapping(value)
                    _append_catalog_target(
                        targets,
                        sku_id=_positive_int(item.get("sku_id")),
                        spu_id=_positive_int(item.get("spu_id")),
                        source="working_memory_reference",
                    )
            else:
                _append_identity_lists(
                    targets,
                    _selected_candidate_values(
                        catalog.get("candidate_sku_ids"),
                        requirement.positions,
                    ),
                    _selected_candidate_values(
                        catalog.get("candidate_spu_ids"),
                        requirement.positions,
                    ),
                    source="working_memory_reference",
                )
        elif requirement.source == "context_product":
            snapshot = _mapping(
                state.get("working_memory_snapshot") or state.get("working_memory")
            )
            catalog = _mapping(snapshot.get("catalog"))
            sku_id = _positive_int(catalog.get("referenced_sku_id"))
            spu_id = _positive_int(catalog.get("referenced_spu_id"))
            if sku_id is None and spu_id is None:
                candidate_skus = _unique_positive_int_list(
                    catalog.get("candidate_sku_ids")
                )
                candidate_spus = _unique_positive_int_list(
                    catalog.get("candidate_spu_ids")
                )
                if len(candidate_spus) == 1:
                    spu_id = candidate_spus[0]
                    sku_id = candidate_skus[0] if len(candidate_skus) == 1 else None
                elif len(candidate_skus) == 1:
                    sku_id = candidate_skus[0]
            _append_catalog_target(
                targets,
                sku_id=sku_id,
                spu_id=spu_id,
                source="working_memory_reference",
            )
        elif requirement.task_id is not None:
            artifacts = _mapping(state.get("task_artifacts"))
            artifact = _mapping(artifacts.get(requirement.task_id))
            value = _mapping(artifact.get("value"))
            products = value.get("products")
            if isinstance(products, list):
                for product in products:
                    item = _mapping(product)
                    _append_catalog_target(
                        targets,
                        sku_id=_positive_int(item.get("sku_id")),
                        spu_id=_positive_int(item.get("spu_id")),
                        source="current_turn_artifact",
                    )
            _append_identity_lists(
                targets,
                value.get("selected_sku_ids"),
                value.get("selected_spu_ids"),
                source="current_turn_artifact",
            )
    for value in _direct_dependency_artifact_values(state, task):
        _append_catalog_artifact_targets(targets, value)
    return targets[:10]


def _append_catalog_artifact_targets(
    targets: list[dict[str, Any]],
    value: Mapping[str, Any],
) -> None:
    products = value.get("products")
    if isinstance(products, list):
        for product in products:
            item = _mapping(product)
            _append_catalog_target(
                targets,
                sku_id=_positive_int(item.get("sku_id")),
                spu_id=_positive_int(item.get("spu_id")),
                source="current_turn_artifact",
            )
    _append_identity_lists(
        targets,
        value.get("selected_sku_ids"),
        value.get("selected_spu_ids"),
        source="current_turn_artifact",
    )


def _append_identity_lists(
    targets: list[dict[str, Any]],
    raw_sku_ids: Any,
    raw_spu_ids: Any,
    *,
    source: str,
) -> None:
    sku_ids = _positive_int_list(raw_sku_ids)
    spu_ids = _positive_int_list(raw_spu_ids)
    for index in range(max(len(sku_ids), len(spu_ids))):
        _append_catalog_target(
            targets,
            sku_id=sku_ids[index] if index < len(sku_ids) else None,
            spu_id=spu_ids[index] if index < len(spu_ids) else None,
            source=source,
        )


def _append_catalog_target(
    targets: list[dict[str, Any]],
    *,
    sku_id: int | None,
    spu_id: int | None,
    source: str,
) -> None:
    if sku_id is None and spu_id is None:
        return
    identity = (sku_id, spu_id)
    if any((item.get("sku_id"), item.get("spu_id")) == identity for item in targets):
        return
    target: dict[str, Any] = {"source": source}
    if sku_id is not None:
        target["sku_id"] = sku_id
    if spu_id is not None:
        target["spu_id"] = spu_id
    targets.append(target)


def _positive_int_list(value: Any) -> list[int]:
    if not isinstance(value, list):
        return []
    return [
        normalized
        for item in value
        if (normalized := _positive_int(item)) is not None
    ]


def _unique_positive_int_list(value: Any) -> list[int]:
    return list(dict.fromkeys(_positive_int_list(value)))


def bound_task_order_id(state: Mapping[str, Any], task: RoutedTask) -> int | None:
    """Bind one order id from memory or a direct dependency Artifact."""
    candidates: list[int] = []
    artifacts = _mapping(state.get("task_artifacts"))
    for requirement in task.input_requirements:
        if requirement.source == "context_order":
            snapshot = _mapping(
                state.get("working_memory_snapshot") or state.get("working_memory")
            )
            order = _mapping(snapshot.get("order"))
            _extend_positive_ints(candidates, [order.get("last_order_id")])
            continue
        if requirement.source == "order_candidates":
            snapshot = _mapping(
                state.get("working_memory_snapshot") or state.get("working_memory")
            )
            order = _mapping(snapshot.get("order"))
            _extend_positive_ints(
                candidates,
                _selected_candidate_values(
                    order.get("candidate_order_ids"),
                    requirement.positions,
                ),
            )
            continue
        if requirement.source != "task_output" or requirement.task_id is None:
            continue
        artifact = _mapping(artifacts.get(requirement.task_id))
        value = _mapping(artifact.get("value"))
        order = _mapping(value.get("order"))
        _extend_positive_ints(candidates, [order.get("id")])
        values = value.get("candidates")
        if isinstance(values, list):
            _extend_positive_ints(
                candidates,
                [item.get("id") for item in values if isinstance(item, Mapping)],
            )
    dependency_candidates: list[int] = []
    for value in _direct_dependency_artifact_values(state, task):
        _extend_order_artifact_ids(dependency_candidates, value)
    selected_dependency = _select_dependency_order_id(task, dependency_candidates)
    _extend_positive_ints(candidates, [selected_dependency])
    return candidates[0] if len(candidates) == 1 else None


def _extend_order_artifact_ids(
    candidates: list[int],
    value: Mapping[str, Any],
) -> None:
    order = _mapping(value.get("order"))
    _extend_positive_ints(candidates, [order.get("id")])
    values = value.get("candidates")
    if isinstance(values, list):
        _extend_positive_ints(
            candidates,
            [item.get("id") for item in values if isinstance(item, Mapping)],
        )
    _extend_positive_ints(candidates, value.get("focus_order_ids"))


def _select_dependency_order_id(
    task: RoutedTask,
    candidates: list[int],
) -> int | None:
    if len(candidates) == 1:
        return candidates[0]
    position = _order_candidate_position(task.query)
    if position is None or position > len(candidates):
        return None
    return candidates[position - 1]


def _order_candidate_position(query: str) -> int | None:
    compact = "".join(query.split())
    ordinal_words = {
        "第一": 1,
        "第一个": 1,
        "第二": 2,
        "第二个": 2,
        "第三": 3,
        "第三个": 3,
        "第四": 4,
        "第四个": 4,
        "第五": 5,
        "第五个": 5,
    }
    for marker, position in ordinal_words.items():
        if marker in compact:
            return position
    for position in range(1, 6):
        if f"第{position}" in compact:
            return position
    if any(marker in compact for marker in ("最新", "最近一笔", "最近一个", "第一笔")):
        return 1
    return None


def _extract_task_artifact(
    task: RoutedTask,
    call: Mapping[str, Any],
    result: Mapping[str, Any],
) -> TaskArtifactRecord:
    outcome = normalize_tool_result(result)
    call_id = str(result.get("tool_call_id") or "")
    name = str(result.get("name") or "unknown_tool")
    execution = _mapping(result.get("execution"))
    output = _mapping(execution.get("output"))
    value: Any = None
    evidence: list[dict[str, Any]] = []
    usable = outcome.has_usable_information
    reason = outcome.reason

    if usable and name == "catalog_search":
        products = [dict(item) for item in output.get("products", []) if isinstance(item, Mapping)]
        if task.produces == "ranked_product":
            query_plan = _mapping(output.get("query_plan"))
            selected = (
                products[0]
                if _mapping(query_plan.get("ranking")) and products
                else _select_ranked_product(products, task)
            )
            usable = selected is not None
            value = {
                "products": [selected] if selected is not None else [],
                "selected_sku_ids": [selected["sku_id"]] if selected is not None else [],
                "selected_spu_ids": [selected["spu_id"]] if selected is not None else [],
                "query_plan": output.get("query_plan") or {},
                **_catalog_search_window(output),
            }
            if not usable:
                reason = "deterministic_selector_did_not_produce_product"
        else:
            value = {
                "products": products,
                "selected_sku_ids": [
                    item["sku_id"] for item in products if _positive_int(item.get("sku_id"))
                ],
                "selected_spu_ids": list(
                    dict.fromkeys(
                        item["spu_id"]
                        for item in products
                        if _positive_int(item.get("spu_id"))
                    )
                ),
                "query_plan": output.get("query_plan") or {},
                **_catalog_search_window(output),
            }
    elif usable and name == "catalog_compare":
        products = [dict(item) for item in output.get("products", []) if isinstance(item, Mapping)]
        series = [dict(item) for item in output.get("series", []) if isinstance(item, Mapping)]
        value = {
            "comparison_level": output.get("comparison_level") or "sku",
            "products": products,
            "series": series,
            "series_differences": output.get("series_differences") or [],
            "selected_sku_ids": [
                item["sku_id"] for item in products if _positive_int(item.get("sku_id"))
            ],
            "selected_spu_ids": list(
                dict.fromkeys(
                    [
                        item["spu_id"]
                        for item in products
                        if _positive_int(item.get("spu_id"))
                    ]
                    + [
                        item["spu_id"]
                        for item in series
                        if _positive_int(item.get("spu_id"))
                    ]
                )
            ),
            "comparison_fields": output.get("comparison_fields") or [],
            "query_plan": output.get("query_plan") or {},
        }
    elif usable and name == "catalog_facets":
        value = {"items": output.get("items") or [], "facet": output.get("facet")}
    elif usable and name == "order_lookup":
        value = {
            "order": output.get("order"),
            "candidates": output.get("candidates") or [],
            "analysis_orders": output.get("analysis_orders") or [],
            "focus_order_ids": output.get("focus_order_ids") or [],
            "result_type": output.get("result_type"),
            "query_mode": output.get("query_mode"),
            "total_match_count": output.get("total_match_count", 0),
            "returned_count": output.get("returned_count", 0),
            "is_exhaustive": output.get("is_exhaustive", True),
            "offset": output.get("offset", 0),
            "next_offset": output.get("next_offset"),
            "query_plan": output.get("query_plan") or {},
            "diagnostics": output.get("diagnostics") or [],
        }
    elif usable and name in {"policy_search", "knowledge_search"}:
        documents = [
            dict(item)
            for item in output.get("documents", [])
            if isinstance(item, Mapping)
        ]
        value = {"documents": documents}
        evidence = [
            {
                "source_tool_call_id": call_id,
                "source_type": item.get("source_type"),
                "source_id": item.get("source_id"),
                "title": item.get("title"),
                "document_type": item.get("document_type"),
            }
            for item in documents
        ]

    if usable and not evidence:
        evidence = [{"source_tool_call_id": call_id, "source_tool_name": name}]
    artifact_type = (
        {
            "catalog_search": "products",
            "catalog_compare": "comparison",
            "catalog_facets": "facets",
            "order_lookup": "order",
            "policy_search": "documents",
            "knowledge_search": "documents",
        }.get(name, "result")
        if task.produces == "result"
        else task.produces
    )
    return TaskArtifactRecord(
        task_id=task.id,
        goal_id=task.goal_id,
        artifact_type=artifact_type,
        usable=usable,
        value=value,
        evidence=evidence,
        source_tool_call_id=call_id,
        source_tool_name=name,
        reason=reason,
    )


def _catalog_search_window(output: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "result_purpose": output.get("result_purpose") or "search",
        "selection_scope": output.get("selection_scope") or "spu",
        "total_match_count": max(0, int(output.get("total_match_count") or 0)),
        "returned_count": max(0, int(output.get("returned_count") or 0)),
        "is_exhaustive": bool(output.get("is_exhaustive", True)),
        "ranking_strategy": str(output.get("ranking_strategy") or ""),
    }


def _select_ranked_product(
    products: list[dict[str, Any]], task: RoutedTask
) -> dict[str, Any] | None:
    selector = task.result_selector
    if selector is None:
        return products[0] if products else None
    if selector.scope == "sku":
        ranked = sorted(
            products,
            key=lambda item: (
                -_non_negative_int(item.get("sku_sales_count")),
                _int(item.get("sku_id")),
            ),
        )
    else:
        representatives: dict[int, dict[str, Any]] = {}
        for product in products:
            spu_id = _positive_int(product.get("spu_id"))
            if spu_id is None:
                continue
            current = representatives.get(spu_id)
            key = (-_non_negative_int(product.get("sku_sales_count")), _int(product.get("sku_id")))
            if current is None:
                representatives[spu_id] = product
                continue
            current_key = (
                -_non_negative_int(current.get("sku_sales_count")),
                _int(current.get("sku_id")),
            )
            if key < current_key:
                representatives[spu_id] = product
        ranked = sorted(
            representatives.values(),
            key=lambda item: (
                -_non_negative_int(item.get("sales_count")),
                _int(item.get("spu_id")),
            ),
        )
    index = selector.rank - 1
    return ranked[index] if index < len(ranked) else None


def _attempted_task_outcomes(state: Mapping[str, Any]) -> dict[str, str]:
    outcomes: dict[str, str] = {}
    for entry in state.get("subquery_ledger", []):
        if not isinstance(entry, Mapping) or entry.get("status") == "superseded":
            continue
        task_id = str(entry.get("subquery") or "").strip()
        if task_id:
            outcomes[task_id] = str(entry.get("outcome") or "")
    return outcomes


def _tool_clarification_request(
    state: Mapping[str, Any],
    task_id: str,
) -> dict[str, str] | None:
    missing_by_code = {
        "invalid_context_target": "可用的具体商品或型号",
        "insufficient_spu_ids": "至少两个明确的商品系列",
        "insufficient_active_series": "至少两个当前在售的商品系列",
    }
    for wave in reversed(state.get("tool_waves", [])):
        if not isinstance(wave, Mapping):
            continue
        calls = {
            str(call.get("id") or ""): call
            for call in wave.get("calls", [])
            if isinstance(call, Mapping)
        }
        for result in reversed(wave.get("results", [])):
            if not isinstance(result, Mapping):
                continue
            call = _mapping(calls.get(str(result.get("tool_call_id") or "")))
            if str(call.get("subquery") or "").strip() != task_id:
                continue
            output = _mapping(_mapping(result.get("execution")).get("output"))
            diagnostics = output.get("diagnostics")
            if not isinstance(diagnostics, list):
                return None
            for diagnostic in diagnostics:
                item = _mapping(diagnostic)
                code = str(item.get("code") or "")
                missing_information = missing_by_code.get(code)
                if missing_information is not None:
                    return {
                        "code": code,
                        "missing_information": missing_information,
                    }
            return None
    return None


def _missing_input_requirements(
    state: Mapping[str, Any], task: RoutedTask
) -> tuple[list[str], bool]:
    missing: list[str] = []
    user_can_supply = False
    artifacts = _mapping(state.get("task_artifacts"))
    for requirement in task.input_requirements:
        if requirement.source == "task_output":
            artifact = _mapping(artifacts.get(requirement.task_id or ""))
            if not artifact.get("usable"):
                missing.append(requirement.name)
            elif task.capability == "order_lookup" and bound_task_order_id(state, task) is None:
                missing.append(requirement.name)
                user_can_supply = True
        elif requirement.source == "context_product":
            snapshot = _mapping(
                state.get("working_memory_snapshot") or state.get("working_memory")
            )
            catalog = _mapping(snapshot.get("catalog"))
            referenced = [
                _positive_int(catalog.get("referenced_sku_id")),
                _positive_int(catalog.get("referenced_spu_id")),
            ]
            candidate_skus = _positive_int_list(catalog.get("candidate_sku_ids"))
            candidate_spus = _positive_int_list(catalog.get("candidate_spu_ids"))
            has_product = any(item is not None for item in referenced) or (
                len(set(candidate_skus)) == 1 or len(set(candidate_spus)) == 1
            )
            if not has_product:
                missing.append(requirement.name)
                user_can_supply = True
        elif requirement.source == "comparison_context":
            snapshot = _mapping(
                state.get("working_memory_snapshot") or state.get("working_memory")
            )
            comparison = _mapping(_mapping(snapshot.get("catalog")).get("comparison"))
            sku_ids = _positive_int_list(comparison.get("sku_ids"))
            spu_ids = _positive_int_list(comparison.get("spu_ids"))
            if max(len(sku_ids), len(spu_ids)) < 2:
                missing.append(requirement.name)
                user_can_supply = True
        elif requirement.source == "catalog_candidates":
            selected = bound_task_catalog_targets(state, task)
            required_count = 2 if task.capability == "catalog_compare" else 1
            if len(selected) < required_count:
                missing.append(requirement.name)
                user_can_supply = True
        elif requirement.source == "order_candidates":
            if bound_task_order_id(state, task) is None:
                missing.append(requirement.name)
                user_can_supply = True
        elif requirement.source == "context_order":
            if bound_task_order_id(state, task) is None:
                missing.append(requirement.name)
                user_can_supply = True
    if (
        task.capability == "catalog_compare"
        and task.input_requirements
        and len(bound_task_catalog_targets(state, task)) < 2
    ):
        missing.append("至少两个可比较商品")
        user_can_supply = True
    return list(dict.fromkeys(missing)), user_can_supply


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _direct_dependency_artifact_values(
    state: Mapping[str, Any],
    task: RoutedTask,
) -> list[Mapping[str, Any]]:
    """Return usable values for the Task's frozen direct dependencies."""
    artifacts = _mapping(state.get("task_artifacts"))
    values: list[Mapping[str, Any]] = []
    for task_id in task.depends_on:
        artifact = _mapping(artifacts.get(task_id))
        if not artifact.get("usable"):
            continue
        value = _mapping(artifact.get("value"))
        if value:
            values.append(value)
    return values


def _working_catalog_memory(state: Mapping[str, Any]) -> Mapping[str, Any]:
    snapshot = _mapping(
        state.get("working_memory_snapshot") or state.get("working_memory")
    )
    return _mapping(snapshot.get("catalog"))


def _selected_candidate_values(value: Any, positions: list[int]) -> list[Any]:
    if not isinstance(value, list):
        return []
    if not positions:
        return list(value)
    return [
        value[position - 1]
        for position in positions
        if position <= len(value)
    ]


def _extend_positive_ints(target: list[int], values: Any) -> None:
    if not isinstance(values, list):
        return
    for value in values:
        normalized = _positive_int(value)
        if normalized is not None and normalized not in target:
            target.append(normalized)


def _positive_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _non_negative_int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


__all__ = [
    "TaskArtifactRecord",
    "bound_task_catalog_targets",
    "bound_task_order_id",
    "bound_task_spu_ids",
    "bound_task_sku_ids",
    "ensure_task_runtime",
    "extract_wave_artifacts",
    "initialize_task_runtime",
    "ready_tasks",
    "refresh_task_status",
    "user_clarifiable_blockers",
]
