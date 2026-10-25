"""Existing runtime-context interpretation, shared with compatibility callers."""
import re
from typing import Any, Literal

from app.tools.schemas import (
    CatalogSearchInput,
    CatalogTargetIdentity,
    CatalogToolRuntimeContext,
)


def _resolved_catalog_request_context(
    query: str,
    context: CatalogToolRuntimeContext,
) -> tuple[list[CatalogTargetIdentity], Literal["sku", "spu"] | None]:
    """Resolve trusted Catalog identities from language plus runtime-owned context."""
    compact = re.sub(r"\s+", "", query.casefold())
    targets: list[CatalogTargetIdentity] = []
    scope_hints: list[Literal["sku", "spu"]] = []

    def add_target(
        *,
        sku_id: int | None,
        spu_id: int | None,
        source: Literal[
            "current_turn_artifact",
            "working_memory_reference",
            "explicit_argument",
        ],
        scope: Literal["sku", "spu"] | None = None,
    ) -> None:
        if sku_id is None and spu_id is None:
            return
        identity = (sku_id, spu_id)
        if any((item.sku_id, item.spu_id) == identity for item in targets):
            return
        targets.append(
            CatalogTargetIdentity(
                sku_id=sku_id,
                spu_id=spu_id,
                source=source,
            )
        )
        if scope is not None:
            scope_hints.append(scope)

    dependency_present = bool(context.dependency_artifacts)
    for artifact in context.dependency_artifacts:
        artifact_scope = artifact.selection_scope
        for product in artifact.products:
            add_target(
                sku_id=product.sku_id,
                spu_id=product.spu_id,
                source="current_turn_artifact",
                scope=artifact_scope or product.entity_scope,
            )
        if not artifact.products:
            for index in range(
                max(
                    len(artifact.selected_sku_ids),
                    len(artifact.selected_spu_ids),
                )
            ):
                add_target(
                    sku_id=(
                        artifact.selected_sku_ids[index]
                        if index < len(artifact.selected_sku_ids)
                        else None
                    ),
                    spu_id=(
                        artifact.selected_spu_ids[index]
                        if index < len(artifact.selected_spu_ids)
                        else None
                    ),
                    source="current_turn_artifact",
                    scope=artifact_scope,
                )

    memory = context.memory
    ordinal_positions = _catalog_ordinal_positions(query)
    collective_reference = any(
        marker in compact
        for marker in (
            "这些",
            "这几个",
            "这几款",
            "上述商品",
            "上面的商品",
            "它们",
            "两者",
            "二者",
        )
    )
    current_reference = _catalog_query_references_current_product(compact)
    displays = memory.candidate_display
    if ordinal_positions:
        for position in ordinal_positions:
            if position <= len(displays):
                item = displays[position - 1]
                add_target(
                    sku_id=item.sku_id,
                    spu_id=item.spu_id,
                    source="working_memory_reference",
                    scope=item.entity_scope,
                )
                continue
            index = position - 1
            add_target(
                sku_id=(
                    memory.candidate_sku_ids[index]
                    if index < len(memory.candidate_sku_ids)
                    else None
                ),
                spu_id=(
                    memory.candidate_spu_ids[index]
                    if index < len(memory.candidate_spu_ids)
                    else None
                ),
                source="working_memory_reference",
                scope=_catalog_memory_scope(context),
            )
    elif collective_reference and not dependency_present:
        if displays:
            for item in displays:
                add_target(
                    sku_id=item.sku_id,
                    spu_id=item.spu_id,
                    source="working_memory_reference",
                    scope=item.entity_scope,
                )
        else:
            for index in range(
                max(
                    len(memory.candidate_sku_ids),
                    len(memory.candidate_spu_ids),
                )
            ):
                add_target(
                    sku_id=(
                        memory.candidate_sku_ids[index]
                        if index < len(memory.candidate_sku_ids)
                        else None
                    ),
                    spu_id=(
                        memory.candidate_spu_ids[index]
                        if index < len(memory.candidate_spu_ids)
                        else None
                    ),
                    source="working_memory_reference",
                    scope=_catalog_memory_scope(context),
                )

    if current_reference:
        add_target(
            sku_id=memory.referenced_sku_id,
            spu_id=memory.referenced_spu_id,
            source="working_memory_reference",
            scope=_catalog_memory_scope(context),
        )
        if memory.referenced_sku_id is None and memory.referenced_spu_id is None:
            if len(displays) == 1:
                item = displays[0]
                add_target(
                    sku_id=item.sku_id,
                    spu_id=item.spu_id,
                    source="working_memory_reference",
                    scope=item.entity_scope,
                )
            elif len({item.spu_id for item in displays}) == 1:
                add_target(
                    sku_id=None,
                    spu_id=displays[0].spu_id,
                    source="working_memory_reference",
                    scope="spu",
                )
            elif len(set(memory.candidate_spu_ids)) == 1:
                add_target(
                    sku_id=(
                        memory.candidate_sku_ids[0]
                        if len(memory.candidate_sku_ids) == 1
                        else None
                    ),
                    spu_id=memory.candidate_spu_ids[0],
                    source="working_memory_reference",
                    scope="spu",
                )
            elif len(memory.candidate_sku_ids) == 1:
                add_target(
                    sku_id=memory.candidate_sku_ids[0],
                    spu_id=None,
                    source="working_memory_reference",
                    scope="sku",
                )

    unique_hints = list(dict.fromkeys(scope_hints))
    scope_hint = unique_hints[0] if len(unique_hints) == 1 else None
    if current_reference:
        targets.sort(
            key=lambda item: (
                0 if item.source == "working_memory_reference" else 1
            )
        )
    return targets[:10], scope_hint


def _catalog_query_references_current_product(query: str) -> bool:
    compact = re.sub(r"\s+", "", query.casefold())
    return any(
        marker in compact
        for marker in (
            "这个商品",
            "这款商品",
            "这个键盘",
            "这款键盘",
            "这个鼠标",
            "这款鼠标",
            "这个系列",
            "这款",
            "当前商品",
            "当前系列",
            "当前",
            "这个",
            "它的",
        )
    )


def _catalog_ordinal_positions(query: str) -> list[int]:
    positions: list[int] = []
    for match in re.finditer(r"第\s*(\d{1,2})\s*(?:个|款|件|项)?", query):
        value = int(match.group(1))
        if 1 <= value <= 20 and value not in positions:
            positions.append(value)
    chinese = {
        "一": 1,
        "二": 2,
        "两": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
        "十": 10,
    }
    for match in re.finditer(r"第([一二两三四五六七八九十])(?:个|款|件|项)?", query):
        value = chinese[match.group(1)]
        if value not in positions:
            positions.append(value)
    return positions


def _catalog_memory_scope(
    context: CatalogToolRuntimeContext,
) -> Literal["sku", "spu"] | None:
    value = context.memory.query_plan.get("selection_scope")
    return value if value in {"sku", "spu"} else None


def _catalog_context_summary(
    request: CatalogSearchInput,
) -> dict[str, Any]:
    targets, scope_hint = _resolved_catalog_request_context(
        request.query,
        request.catalog_context,
    )
    return {
        "trusted_targets_present": bool(targets),
        "trusted_target_count": len(targets),
        "scope_hint": scope_hint,
        "previous_selection_scope": _catalog_memory_scope(request.catalog_context),
        "dependency_artifact_count": len(
            request.catalog_context.dependency_artifacts
        ),
    }
