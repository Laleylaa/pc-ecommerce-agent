from typing import Any, TypedDict

from app.schemas.catalog import ProductCard
from app.schemas.chat import EvidenceItem
from app.schemas.context import PreparedTurn
from app.schemas.order import OrderCard


class AgentState(TypedDict, total=False):
    user_id: int
    conversation_id: int | None
    user_message_id: int
    run_id: int
    message: str
    input_rule_id: str
    request_router_call_count: int
    goal_plan: dict[str, Any]
    execution_plan: dict[str, Any]
    context_objects: dict[str, Any]
    planning_error: dict[str, Any]
    planning_deadline_monotonic: float
    answer_deadline_monotonic: float
    answer_call_count: int
    task_planner_call_count: int
    route_source: str
    route_plan: dict[str, Any]
    rewritten_query: str
    planned_subqueries: list[dict[str, Any]]
    blocked_subqueries: list[dict[str, Any]]
    task_status: dict[str, dict[str, Any]]
    task_artifacts: dict[str, dict[str, Any]]
    answer_context: dict[str, Any]
    recovery_batch: dict[str, Any]
    turn_deadline_monotonic: float
    final_answer_deadline_monotonic: float
    intent: str
    decision: dict[str, Any]
    orchestrator_call_count: int
    tool_wave_count: int
    tool_waves: list[dict[str, Any]]
    tool_results: list[dict[str, Any]]
    normalized_tool_results: list[dict[str, Any]]
    subquery_ledger: list[dict[str, Any]]
    terminal_guard_feedback: str
    terminal_guard_status: str
    boundary: dict[str, Any]
    parsed: dict[str, Any]
    history: list[dict[str, str]]
    memory: list[dict[str, Any]]
    working_memory: dict[str, Any]
    working_memory_snapshot: dict[str, Any]
    evidence: list[EvidenceItem]
    products: list[ProductCard]
    catalog_tool_succeeded: bool
    order: OrderCard | None
    answer: str
    route_answer_mode: str
    suggested_actions: list[dict[str, Any]]
    prepared_turn: PreparedTurn
    applied_memory_ids: list[int]
    memory_changes: list[dict[str, Any]]
    assistant_metadata: dict[str, Any]
