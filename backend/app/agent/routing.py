from collections.abc import Mapping
from typing import Any, Literal

from langchain_core.messages import AIMessage
from pydantic import BaseModel, ConfigDict, Field, model_validator

RouteDisposition = Literal[
    "tool_planning",
    "direct_response",
    "session_grounded_response",
    "clarification",
    "human_handoff",
    "out_of_scope",
    "security_refusal",
]
RouteCapability = Literal[
    "catalog_search",
    "catalog_compare",
    "catalog_facets",
    "order_lookup",
    "policy_search",
    "knowledge_search",
    "planner_required",
]
TaskArtifact = Literal[
    "result",
    "products",
    "ranked_product",
    "comparison",
    "facets",
    "order",
    "documents",
]
TaskAnswerRole = Literal["internal", "user_facing"]

_GOAL_ID_PATTERN = r"^(?:goal|sq)_[1-9][0-9]*$"
_TASK_ID_PATTERN = r"^(?:task|sq)_[1-9][0-9]*$"


class TaskInputRequirement(BaseModel):
    """A typed input that Runtime must bind before executing a dependent task."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64)
    source: Literal[
        "context_product",
        "context_order",
        "comparison_context",
        "catalog_candidates",
        "order_candidates",
        "task_output",
    ]
    task_id: str | None = Field(default=None, pattern=_TASK_ID_PATTERN)
    positions: list[int] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def validate_source(self) -> "TaskInputRequirement":
        if self.source == "task_output" and self.task_id is None:
            raise ValueError("task_output input requires task_id")
        if self.source != "task_output" and self.task_id is not None:
            raise ValueError("context input cannot declare task_id")
        if self.source not in {"catalog_candidates", "order_candidates"} and self.positions:
            raise ValueError("positions require a candidate context source")
        if any(position < 1 or position > 20 for position in self.positions):
            raise ValueError("candidate positions must be one-based values from 1 to 20")
        if len(self.positions) != len(set(self.positions)):
            raise ValueError("candidate positions must be unique")
        return self


class TaskResultSelector(BaseModel):
    """A deterministic projection applied to one task's Tool result."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["sales_rank"]
    rank: int = Field(ge=1, le=20)
    scope: Literal["spu", "sku"] = "spu"


class RoutedTask(BaseModel):
    """One immutable executable node in the turn-local Task DAG."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=_TASK_ID_PATTERN, max_length=24)
    goal_id: str = Field(pattern=_GOAL_ID_PATTERN, max_length=24)
    canonical_query: str = Field(min_length=1, max_length=1000)
    depends_on: list[str] = Field(default_factory=list, max_length=7)
    input_requirements: list[TaskInputRequirement] = Field(default_factory=list, max_length=8)
    produces: TaskArtifact
    answer_role: TaskAnswerRole
    capability: RouteCapability = "planner_required"
    result_selector: TaskResultSelector | None = None
    comparison_level: Literal["sku", "spu"] | None = None

    @property
    def query(self) -> str:
        """Compatibility view for callers migrated from the old task/subquery model."""
        return self.canonical_query

    @model_validator(mode="after")
    def validate_task(self) -> "RoutedTask":
        if len(self.depends_on) != len(set(self.depends_on)):
            raise ValueError("task dependencies must be unique")
        if self.id in self.depends_on:
            raise ValueError("task cannot depend on itself")
        for requirement in self.input_requirements:
            if requirement.task_id is not None and requirement.task_id not in self.depends_on:
                raise ValueError("task_output input must reference a declared dependency")
        if self.result_selector is not None and self.produces != "ranked_product":
            raise ValueError("result_selector requires produces=ranked_product")
        if self.comparison_level is not None and self.capability != "catalog_compare":
            raise ValueError("comparison_level is only valid for catalog_compare")
        sources = {requirement.source for requirement in self.input_requirements}
        if "catalog_candidates" in sources and self.capability not in {
            "catalog_search",
            "catalog_compare",
        }:
            raise ValueError("catalog_candidates require a catalog capability")
        if "order_candidates" in sources and self.capability != "order_lookup":
            raise ValueError("order_candidates require order_lookup")
        if "context_order" in sources and self.capability != "order_lookup":
            raise ValueError("context_order requires order_lookup")
        if self.capability == "order_lookup":
            for requirement in self.input_requirements:
                if requirement.source == "order_candidates" and len(requirement.positions) != 1:
                    raise ValueError(
                        "each order candidate lookup must select exactly one position"
                    )
        expected_artifacts: dict[str, set[TaskArtifact]] = {
            "catalog_search": {"products", "ranked_product"},
            "catalog_compare": {"comparison"},
            "catalog_facets": {"facets"},
            "order_lookup": {"order"},
            "policy_search": {"documents"},
            "knowledge_search": {"documents"},
        }
        allowed = expected_artifacts.get(self.capability)
        if allowed is not None and self.produces not in allowed:
            raise ValueError("task produces is incompatible with its concrete capability")
        return self


class RoutedSubquery(BaseModel):
    """A user business goal and its admission result.

    Only admitted goals contain executable tasks. A before-validator accepts the previous
    flattened shape so persisted route plans and the existing deterministic fallback remain
    readable while all newly serialized plans use the nested Goal -> Task structure.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=_GOAL_ID_PATTERN, max_length=24)
    query: str = Field(min_length=1, max_length=1000)
    disposition: RouteDisposition
    reason_code: str = Field(min_length=1, max_length=100)
    missing_information: list[str] = Field(default_factory=list, max_length=5)
    clarification_question: str = Field(default="", max_length=500)
    tasks: list[RoutedTask] = Field(default_factory=list, max_length=8)

    @property
    def capability(self) -> RouteCapability | None:
        return self.tasks[0].capability if len(self.tasks) == 1 else None

    @property
    def depends_on(self) -> list[str]:
        return self.tasks[0].depends_on if len(self.tasks) == 1 else []

    @property
    def input_requirements(self) -> list[TaskInputRequirement]:
        return self.tasks[0].input_requirements if len(self.tasks) == 1 else []

    @property
    def produces(self) -> TaskArtifact | None:
        return self.tasks[0].produces if len(self.tasks) == 1 else None

    @property
    def result_selector(self) -> TaskResultSelector | None:
        return self.tasks[0].result_selector if len(self.tasks) == 1 else None

    @model_validator(mode="before")
    @classmethod
    def upgrade_flattened_task(cls, value: Any) -> Any:
        if not isinstance(value, Mapping):
            return value
        data = dict(value)
        legacy_keys = {
            "capability",
            "depends_on",
            "input_requirements",
            "produces",
            "result_selector",
            "comparison_level",
            "answer_role",
            "canonical_query",
            "goal_id",
        }
        has_legacy_task = any(key in data for key in legacy_keys)
        if data.get("disposition") != "tool_planning":
            # Do not silently discard executable metadata from a denied Goal. Leaving the
            # legacy fields in place lets extra="forbid" reject capability smuggling.
            return data
        if data.get("disposition") == "tool_planning" and not data.get("tasks"):
            produces = data.get("produces") or _default_artifact_for_capability(
                data.get("capability")
            )
            data["tasks"] = [
                {
                    "id": data.get("id"),
                    "goal_id": data.get("goal_id") or data.get("id"),
                    "canonical_query": data.get("canonical_query") or data.get("query"),
                    "depends_on": data.get("depends_on") or [],
                    "input_requirements": data.get("input_requirements") or [],
                    "produces": produces,
                    "answer_role": data.get("answer_role") or "user_facing",
                    "capability": data.get("capability") or "planner_required",
                    "result_selector": data.get("result_selector"),
                    "comparison_level": data.get("comparison_level"),
                }
            ]
        if has_legacy_task:
            for key in legacy_keys:
                data.pop(key, None)
        return data

    @model_validator(mode="after")
    def validate_goal(self) -> "RoutedSubquery":
        if self.disposition == "clarification" and not self.clarification_question.strip():
            raise ValueError("clarification subquery requires clarification_question")
        if self.disposition == "tool_planning" and not self.tasks:
            raise ValueError("tool_planning goal requires at least one executable task")
        if self.tasks and not any(task.answer_role == "user_facing" for task in self.tasks):
            raise ValueError("every admitted goal requires at least one user_facing task")
        if self.disposition != "tool_planning" and self.tasks:
            raise ValueError("only tool_planning goals may contain executable tasks")
        if any(task.goal_id != self.id for task in self.tasks):
            raise ValueError("every task goal_id must reference its containing goal")
        return self


class RequestRoutePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rewritten_query: str = Field(min_length=1, max_length=2000)
    subqueries: list[RoutedSubquery] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def validate_task_graph(self) -> "RequestRoutePlan":
        goal_ids = [item.id for item in self.subqueries]
        if len(goal_ids) != len(set(goal_ids)):
            raise ValueError("goal ids must be unique")
        session_grounded = [
            item for item in self.subqueries if item.disposition == "session_grounded_response"
        ]
        if session_grounded and len(self.subqueries) != 1:
            raise ValueError("session_grounded_response requires exactly one routed goal")

        flattened = [task for goal in self.subqueries for task in goal.tasks]
        if len(flattened) > 8:
            raise ValueError("route plan supports at most 8 executable tasks")
        task_ids = [task.id for task in flattened]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("task ids must be unique")
        tasks = {task.id: task for task in flattened}
        for task in flattened:
            for dependency_id in task.depends_on:
                if dependency_id not in tasks:
                    raise ValueError("task dependency must reference an existing admitted task")

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(task_id: str) -> None:
            if task_id in visiting:
                raise ValueError("task graph must be acyclic")
            if task_id in visited:
                return
            visiting.add(task_id)
            for dependency_id in tasks[task_id].depends_on:
                visit(dependency_id)
            visiting.remove(task_id)
            visited.add(task_id)

        for task_id in tasks:
            visit(task_id)
        return self


def _default_artifact_for_capability(capability: Any) -> TaskArtifact:
    defaults: dict[str, TaskArtifact] = {
        "catalog_search": "products",
        "catalog_compare": "comparison",
        "catalog_facets": "facets",
        "order_lookup": "order",
        "policy_search": "documents",
        "knowledge_search": "documents",
    }
    return defaults.get(str(capability), "result")


def request_route_tool_definition() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "route_request",
            "description": (
                "Rewrite the current request, classify each user goal, and expand only "
                "admitted goals into an immutable executable Task DAG."
            ),
            "parameters": RequestRoutePlan.model_json_schema(),
        },
    }


def route_plan_from_ai_message(message: AIMessage) -> RequestRoutePlan:
    if len(message.tool_calls) != 1:
        raise ValueError("request router must return exactly one route_request tool call")
    call = message.tool_calls[0]
    if str(call.get("name") or "") != "route_request":
        raise ValueError("request router returned an unexpected tool call")
    arguments = call.get("args")
    if not isinstance(arguments, Mapping):
        raise TypeError("route_request arguments must be an object")
    raw_goals = arguments.get("subqueries")
    if not isinstance(raw_goals, list):
        raise TypeError("route_request subqueries must be an array")
    for raw_goal in raw_goals:
        if not isinstance(raw_goal, Mapping):
            raise TypeError("route_request subquery must be an object")
        raw_tasks = raw_goal.get("tasks")
        if raw_goal.get("disposition") == "tool_planning" and not raw_tasks:
            raise ValueError("request router must expand every admitted goal into tasks")
        if raw_goal.get("disposition") != "tool_planning" and raw_tasks:
            raise ValueError("request router cannot expand a blocked goal into tasks")
    return RequestRoutePlan.model_validate(dict(arguments))


def tool_planning_subqueries(
    plan: RequestRoutePlan | Mapping[str, Any] | None,
) -> list[RoutedTask]:
    """Return executable tasks; retained name avoids a broad compatibility break."""
    if plan is None:
        return []
    validated = (
        plan if isinstance(plan, RequestRoutePlan) else RequestRoutePlan.model_validate(dict(plan))
    )
    return [task for goal in validated.subqueries for task in goal.tasks]


def blocked_subqueries(
    plan: RequestRoutePlan | Mapping[str, Any] | None,
) -> list[RoutedSubquery]:
    if plan is None:
        return []
    validated = (
        plan if isinstance(plan, RequestRoutePlan) else RequestRoutePlan.model_validate(dict(plan))
    )
    return [item for item in validated.subqueries if item.disposition != "tool_planning"]


def ready_tool_subqueries(
    plan: RequestRoutePlan | Mapping[str, Any] | None,
    *,
    usable_task_ids: set[str] | None = None,
    attempted_task_ids: set[str] | None = None,
) -> list[RoutedTask]:
    """Return unattempted tasks whose declared dependencies have usable artifacts."""
    usable = usable_task_ids or set()
    attempted = attempted_task_ids or set()
    return [
        task
        for task in tool_planning_subqueries(plan)
        if task.id not in attempted and set(task.depends_on) <= usable
    ]


def user_facing_tasks(
    plan: RequestRoutePlan | Mapping[str, Any] | None,
) -> list[RoutedTask]:
    return [task for task in tool_planning_subqueries(plan) if task.answer_role == "user_facing"]


__all__ = [
    "RequestRoutePlan",
    "RouteCapability",
    "RouteDisposition",
    "RoutedSubquery",
    "RoutedTask",
    "TaskAnswerRole",
    "TaskArtifact",
    "TaskInputRequirement",
    "TaskResultSelector",
    "blocked_subqueries",
    "request_route_tool_definition",
    "ready_tool_subqueries",
    "route_plan_from_ai_message",
    "tool_planning_subqueries",
    "user_facing_tasks",
]
