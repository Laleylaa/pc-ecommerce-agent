"""Structured goal routing and bounded tool-task planning; no business I/O."""

import asyncio
import json
import operator
from collections.abc import Callable, Mapping
from copy import deepcopy
from functools import reduce
from typing import Annotated, Any, Literal, TypeVar

import httpx
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from openai import APIConnectionError, APIStatusError, APITimeoutError
from pydantic import BaseModel, ConfigDict, Field, ValidationError, create_model, model_validator

from app.agent.answer_contract import AnswerText
from app.agent.limits import PLANNING_CALL_POLICY, PlanningCallPolicy
from app.agent.prompts.planning import (
    GOAL_ROUTER_PROMPT,
    PLAN_REPAIR_INSTRUCTION,
    TASK_PLANNER_PROMPT,
)
from app.agent.prompts.recovery import build_recovery_prompt
from app.agent.recovery import (
    RecoveryRequest,
    install_execution,
    merged_execution,
    recovery_context,
    validate_route_scope,
)
from app.agent.routing import RequestRoutePlan, RouteDisposition, RoutedSubquery, RoutedTask
from app.core.llm_calls import (
    invoke_before,
)
from app.core.llm_calls import (
    retryable_transport_error as _retryable_transport_error,
)
from app.core.llm_calls import (
    transport_retry_delay as _transport_retry_delay,
)
from app.core.llm_feedback import (
    OutputErrorType,
    OutputValidationError,
    ValidationIssue,
    build_feedback,
    validation_feedback,
)
from app.core.model_usage import record_model_call
from app.core.tracing import trace_event
from app.rules.product_identity import explicit_product_ids
from app.schemas.catalog_search import SearchScope
from app.tools.contracts import ToolContractProvider
from app.tools.order_identity import explicit_order_ids

PlanModel = TypeVar("PlanModel", bound=BaseModel)


class PlanValidationError(OutputValidationError):
    """A model-authored contract error eligible for one bounded repair."""


class PlanningCallError(RuntimeError):
    """A bounded planning failure, with a safe diagnostic rather than an SDK response body."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class GoalAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    response: AnswerText
    covered_goal_ids: list[str] = Field(default_factory=list, max_length=8)


class GoalReview(BaseModel):
    """Summary entry can request recovery or submit the existing answer contract."""

    model_config = ConfigDict(extra="forbid")
    response: str = ""
    covered_goal_ids: list[str] = Field(default_factory=list, max_length=8)
    recovery_requests: list[RecoveryRequest] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def answer_or_recover(self):
        if self.recovery_requests:
            if self.response or self.covered_goal_ids:
                raise PlanValidationError("recovery cannot accompany a final answer")
        else:
            GoalAnswer(response=self.response, covered_goal_ids=self.covered_goal_ids)
        return self


class Goal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^goal_[1-9][0-9]*$")
    query: str = Field(min_length=1, max_length=2000, pattern=r"\S")
    source_text: str = Field(min_length=1, max_length=2000, pattern=r"\S")
    disposition: RouteDisposition
    context_refs: list[str] = Field(default_factory=list, max_length=20)
    missing_information: list[Annotated[str, Field(min_length=1, pattern=r"\S")]] = Field(
        default_factory=list, max_length=5
    )


class GoalPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rewritten_query: str = Field(min_length=1, max_length=2000, pattern=r"\S")
    goals: list[Goal] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def unique_ids(self) -> "GoalPlan":
        if len({g.id for g in self.goals}) != len(self.goals):
            raise PlanValidationError("goal IDs must be unique", path=("goals",))
        if any(g.disposition == "clarification" and not g.missing_information for g in self.goals):
            raise PlanValidationError("clarification requires missing_information", path=("goals",))
        return self


class InputBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: Literal["context", "task_output"] = Field(
        description=(
            "context validates complete arguments only; task_output fills unknown identities"
        )
    )
    ref: str
    kind: Literal["product", "order"]
    positions: list[Annotated[int, Field(strict=True, gt=0)]] = Field(
        default_factory=list, max_length=20
    )
    selection: Literal["object", "candidate_versions", "parent_model"] = "object"

    @model_validator(mode="after")
    def valid_positions(self) -> "InputBinding":
        if any(type(p) is not int or p < 1 for p in self.positions):
            raise PlanValidationError("positions must be positive integers", path=("positions",))
        if len(set(self.positions)) != len(self.positions):
            raise PlanValidationError("positions must be unique", path=("positions",))
        return self


class ToolTask(BaseModel):
    model_config = ConfigDict(extra="forbid")
    goal_id: str = Field(pattern=r"^goal_[1-9][0-9]*$")
    id: str = Field(pattern=r"^task_[1-9][0-9]*$")
    tool_name: str
    arguments: dict[str, Any]
    depends_on: list[str] = Field(default_factory=list, max_length=7)
    bindings: list[InputBinding] = Field(default_factory=list, max_length=10)
    require_exact_dependencies: bool = True
    answer_role: Literal["internal", "user_facing"] = "user_facing"


class BlockedGoal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    goal_id: str = Field(pattern=r"^goal_[1-9][0-9]*$")
    blocked_reason: str = Field(min_length=1, max_length=500, pattern=r"\S")


class ExecutionPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tasks: list[ToolTask] = Field(default_factory=list, max_length=8)
    blocked_goals: list[BlockedGoal] = Field(default_factory=list, max_length=8)


def definition(name: str, model: type[BaseModel]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "Submit one validated structured plan; no business execution.",
            "parameters": model.model_json_schema(),
        },
    }


def execution_definition(provider: ToolContractProvider) -> dict[str, Any]:
    # Reuse public argument models in the provider-visible schema, not hand-written copies.
    variants = tuple(
        create_model(
            f"Planned_{c.name}",
            __base__=ToolTask,
            tool_name=(Literal[c.name], ...),
            arguments=(c.public_input_model, ...),
        )
        for c in provider.list_contracts()
    )
    task_type = reduce(operator.or_, variants)
    plan = create_model(
        "ToolExecutionPlan",
        __base__=ExecutionPlan,
        tasks=(list[task_type], Field(default_factory=list, max_length=8)),
    )
    return definition("submit_execution_plan", plan)


def context_objects(state: Mapping[str, Any]) -> dict[str, dict]:
    """Read-only planning projection of historical memory, never current model output."""
    memory = state.get("working_memory_snapshot", state.get("working_memory", {})) or {}
    catalog = memory.get("catalog", {})
    objects = {}
    displays = catalog.get("candidate_display") or []
    for index, item in enumerate(displays, 1):
        scope = item.get("entity_scope", "sku")
        ids = [item.get(f"{scope}_id")]
        if ids[0]:
            objects[f"product_{index}"] = {
                "kind": "product",
                "entity_scope": scope,
                "ids": ids,
                "title": item.get("title"),
                "category": item.get("category"),
                "spu_id": item.get("spu_id"),
                "candidate_sku_ids": list(
                    item.get("candidate_sku_ids") or [item.get("sku_id")]
                ),
            }
    if not displays:
        scope = catalog.get("query_plan", {}).get("selection_scope") or "sku"
        for index, identity in enumerate(catalog.get(f"candidate_{scope}_ids", []), 1):
            objects[f"product_{index}"] = {
                "kind": "product", "entity_scope": scope, "ids": [identity],
            }
    preferred_scope = catalog.get("query_plan", {}).get("selection_scope") or "sku"
    for scope in (preferred_scope,):
        identity = catalog.get(f"referenced_{scope}_id")
        if identity:
            current = {"kind": "product", "entity_scope": scope, "ids": [identity]}
            # Reuse facts only for this exact known object, never the most recent candidate.
            matching = [
                obj for obj in objects.values()
                if obj.get("entity_scope") == scope and obj.get("ids") == [identity]
            ]
            if len(matching) == 1:
                current = deepcopy(matching[0])
            objects["current_product"] = current
    order = memory.get("order", {})
    for index, identity in enumerate(order.get("candidate_order_ids", []), 1):
        objects[f"order_{index}"] = {"kind": "order", "id": identity}
    for index, suggestion in enumerate(order.get("pending_suggestions", []), 1):
        objects[f"order_suggestion_{index}"] = {
            "kind": "order", "id": suggestion["id"], "requires_confirmation": True,
            "differences": deepcopy(suggestion["differences"]),
        }
    if order.get("last_order_id"):
        objects["current_order"] = {"kind": "order", "id": order["last_order_id"]}
    return objects


def prior_queries(state: Mapping[str, Any]) -> dict[str, str]:
    """Conversational context, without tool plans, counters, cursors or old defaults."""
    memory = state.get("working_memory_snapshot", {}) or {}
    catalog = memory.get("catalog", {})
    candidates = {
        "catalog": catalog.get("query_plan", {}).get("query"),
        "policy": memory.get("policy", {}).get("last_query"),
    }
    return {key: value for key, value in candidates.items() if isinstance(value, str) and value}


def scope_ids(scope: SearchScope | dict | None) -> set[tuple[str, int]]:
    if scope is None:
        return set()
    parsed = SearchScope.model_validate(scope)
    if not parsed.sku_ids and not parsed.spu_ids:
        raise PlanValidationError("explicit scope must contain IDs; omit scope for whole catalog")
    return {(kind, i) for kind in ("sku", "spu") for i in getattr(parsed, f"{kind}_ids")}


def referenced_product_ids(refs: list[str], objects: dict[str, dict]) -> set[tuple[str, int]]:
    """Identities and known relations from selected history; not a query restriction."""
    contextual = set()
    for ref in refs:
        obj = objects[ref]
        if obj.get("kind") != "product":
            continue
        contextual.update((obj["entity_scope"], identity) for identity in obj["ids"])
        if type(obj.get("spu_id")) is int and obj["spu_id"] > 0:
            contextual.add(("spu", obj["spu_id"]))
        contextual.update(
            ("sku", identity) for identity in obj.get("candidate_sku_ids", [])
            if type(identity) is int and identity > 0
        )
    return contextual


def validate_goal_identity(goal: Goal, message: str, objects: dict[str, dict]) -> None:
    if not set(goal.context_refs) <= objects.keys():
        raise PlanValidationError(f"{goal.id}.context_refs contains unavailable objects")
    allowed = explicit_product_ids(message) | referenced_product_ids(goal.context_refs, objects)
    if explicit_product_ids(goal.query) - allowed:
        raise PlanValidationError(
            f"{goal.id}: product IDs need explicit input or trusted context_refs"
        )
    order_ids = explicit_order_ids(goal.source_text)
    contextual_orders = {
        obj["id"] for ref in goal.context_refs if (obj := objects[ref]).get("kind") == "order"
    }
    if explicit_order_ids(goal.query, include_unlabelled=False) - (order_ids | contextual_orders):
        raise PlanValidationError(
            f"{goal.id}: order IDs need explicit input or trusted context_refs"
        )


def error_feedback(exc: Exception) -> dict:
    if isinstance(exc, PlanningCallError):
        return {"errors": [{"path": [], "code": exc.code, "message": str(exc)}]}
    if isinstance(exc, (ValidationError, OutputValidationError)):
        return validation_feedback(exc)
    # This is a terminal runtime diagnostic, never a model-output repair instruction.
    return {"errors": [{"path": [], "code": "execution_error", "message": str(exc)}]}


def _planning_deadline(state: dict[str, Any], policy: PlanningCallPolicy) -> float:
    # Start only when LLM1 is needed. LLM2 inherits this value through graph state.
    if "planning_deadline_monotonic" not in state:
        state["planning_deadline_monotonic"] = (
            asyncio.get_running_loop().time() + policy.total_timeout_seconds
        )
    return state["planning_deadline_monotonic"]


def _remaining_planning_time(deadline: float) -> float:
    remaining = deadline - asyncio.get_running_loop().time()
    if remaining <= 0:
        raise PlanningCallError("planning_deadline_exceeded", "Planning time budget exhausted")
    return remaining




async def generate_plan(
    model: Any,
    name: str,
    schema: type[PlanModel],
    prompt: str,
    payload: dict[str, Any],
    validate: Callable[[PlanModel], None],
    on_attempt: Callable[[], None],
    *,
    policy: PlanningCallPolicy | None = None,
    deadline: float | None = None,
) -> PlanModel:
    # Callers outside LLM1/LLM2 retain their existing output-repair behavior.
    if policy is not None and deadline is None:
        deadline = asyncio.get_running_loop().time() + policy.total_timeout_seconds
    max_attempts = policy.max_attempts if policy else 2
    max_repairs = policy.max_output_repairs if policy else 1
    transport_retries = repairs = 0
    messages = [
        SystemMessage(content=prompt),
        HumanMessage(content=json.dumps(payload, ensure_ascii=False, default=str)),
    ]
    for attempt in range(max_attempts):
        if policy is not None:
            _remaining_planning_time(deadline)
        on_attempt()
        await trace_event(
            "planning_attempt", stage=name, attempt=attempt + 1, messages=messages,
            output_schema=schema, transport_retries=transport_retries,
            repairs=repairs,
        )
        try:
            if policy is None:
                record_model_call(name)
                response = await model.ainvoke(messages)
            else:
                call_deadline = min(
                    deadline, asyncio.get_running_loop().time() + policy.call_timeout_seconds
                )
                response = await invoke_before(model, messages, call_deadline, stage=name)
                _remaining_planning_time(deadline)
        except (
            APIConnectionError, APIStatusError, httpx.TimeoutException,
            httpx.NetworkError, TimeoutError,
        ) as exc:
            await trace_event(
                "planning_transport_error", stage=name, attempt=attempt + 1,
                error_type=type(exc).__name__, status_code=getattr(exc, "status_code", None),
            )
            if policy is None:
                raise
            remaining = _remaining_planning_time(deadline)
            if not _retryable_transport_error(exc):
                raise PlanningCallError(
                    "planning_model_rejected", "Model request rejected; not retryable"
                ) from exc
            if transport_retries >= policy.max_transport_retries or attempt + 1 >= max_attempts:
                code = (
                    "planning_call_timeout"
                    if isinstance(exc, (TimeoutError, httpx.TimeoutException, APITimeoutError))
                    else "planning_transport_failed"
                )
                raise PlanningCallError(
                    code, "Planning model call failed; retry limit reached"
                ) from exc
            delay = _transport_retry_delay(exc, policy.retry_delay_seconds)
            if delay >= remaining:
                raise PlanningCallError(
                    "planning_deadline_exceeded", "Retry delay exceeds remaining planning budget"
                ) from exc
            transport_retries += 1
            await trace_event(
                "planning_transport_retry", stage=name, attempt=attempt + 1,
                delay_seconds=delay, remaining_seconds=remaining,
            )
            # Same messages for transport retries; no schema-repair feedback is added.
            try:
                async with asyncio.timeout_at(deadline):
                    await asyncio.sleep(delay)
            except TimeoutError as timeout:
                raise PlanningCallError(
                    "planning_deadline_exceeded", "Planning budget exhausted during retry delay"
                ) from timeout
            continue
        await trace_event("planning_response", stage=name, attempt=attempt + 1, response=response)
        try:
            if isinstance(response, AIMessage) and response.invalid_tool_calls:
                raise PlanValidationError(
                    "tool-call arguments are not valid JSON",
                    error_type=OutputErrorType.FORMAT_ERROR,
                )
            if not isinstance(response, AIMessage) or len(response.tool_calls) != 1:
                raise PlanValidationError(
                    f"return exactly one {name} function call",
                    error_type=OutputErrorType.FORMAT_ERROR, path=("tool_calls",),
                )
            call = response.tool_calls[0]
            if call["name"] != name:
                raise PlanValidationError(
                    f"expected {name}", error_type=OutputErrorType.FORMAT_ERROR,
                    path=("tool_calls", 0, "name"),
                )
            plan = schema.model_validate(call["args"])
            validate(plan)
            if policy is not None:
                _remaining_planning_time(deadline)
            await trace_event("planning_accepted", stage=name, attempt=attempt + 1, plan=plan)
            return plan
        except (ValidationError, OutputValidationError) as exc:
            feedback = error_feedback(exc)
            if isinstance(response, AIMessage) and response.invalid_tool_calls:
                feedback = build_feedback([
                    ValidationIssue(
                        OutputErrorType.FORMAT_ERROR,
                        call.get("error") or "JSON format is invalid",
                        ("invalid_tool_calls", index, "args"), "invalid_json",
                    )
                    for index, call in enumerate(response.invalid_tool_calls)
                ])
            await trace_event(
                "planning_validation_error", stage=name, attempt=attempt + 1,
                feedback=feedback,
                will_repair=repairs < max_repairs and attempt + 1 < max_attempts,
            )
            if repairs >= max_repairs or attempt + 1 >= max_attempts:
                raise
            repairs += 1
            # Invalid responses are data in the repair prompt, not executable ToolMessages.
            previous = (
                {
                    "tool_calls": response.tool_calls,
                    "invalid_tool_calls": response.invalid_tool_calls,
                    "content": response.content,
                }
                if isinstance(response, AIMessage)
                else None
            )
            messages.append(
                HumanMessage(
                    content=json.dumps(
                        {
                            "previous_output": previous,
                            "validation_feedback": feedback,
                            "repair_instruction": PLAN_REPAIR_INSTRUCTION.format(name=name),
                        },
                        ensure_ascii=False,
                        default=str,
                    )
                )
            )
    raise RuntimeError("unreachable")


async def route_goals(runtime: Any, state: dict[str, Any]) -> RequestRoutePlan:
    policy = getattr(runtime, "planning_call_policy", PLANNING_CALL_POLICY)
    recovery = recovery_context(state, "llm1")
    deadline = (state["recovery_batch"]["deadline"] if recovery
                else _planning_deadline(state, policy))
    objects = context_objects(state)
    state["context_objects"] = objects

    def validate(plan: GoalPlan) -> None:
        if recovery:
            validate_route_scope(plan, recovery)
        for goal in plan.goals:
            if goal.source_text not in state["message"]:
                raise PlanValidationError(f"{goal.id}.source_text must occur in current message")
            validate_goal_identity(goal, state["message"], objects)
            if goal.disposition == "session_grounded_response" and not any(
                item.get("role") == "assistant" and str(item.get("content") or "").strip()
                for item in state.get("history", [])
            ):
                raise PlanValidationError(f"{goal.id}: no historical answer available to reuse")

    def count() -> None:
        state["request_router_call_count"] = state.get("request_router_call_count", 0) + 1

    plan = await generate_plan(
        runtime.request_router,
        "route_request",
        GoalPlan,
        build_recovery_prompt("llm1") if recovery else GOAL_ROUTER_PROMPT,
        {
            "message": state["message"],
            "history": state.get("history", []),
            "prior_queries": prior_queries(state),
            "context_objects": objects,
            "explicit_preferences": state.get("memory", []),
            **({"recovery_context": recovery} if recovery else {}),
        },
        validate,
        count,
        policy=policy,
        deadline=deadline,
    )
    state["goal_plan"] = plan.model_dump(mode="json")
    # An adapter only: new-plan contract errors have already been checked above.
    return RequestRoutePlan(
        rewritten_query=plan.rewritten_query,
        subqueries=[
            RoutedSubquery(
                id=g.id,
                query=g.query,
                disposition=g.disposition,
                reason_code="goal_router",
                source_text=g.source_text,
                missing_information=g.missing_information,
                context_refs=g.context_refs,
                planning_pending=g.disposition == "tool_planning",
            )
            for g in plan.goals
        ],
    )


def validate_execution(
    plan: ExecutionPlan,
    route: RequestRoutePlan,
    provider: ToolContractProvider,
    objects: dict[str, dict[str, Any]],
    *,
    read_only: bool = False,
) -> None:
    goals = {g.id: g for g in route.subqueries if g.disposition == "tool_planning"}
    blocked = [g.goal_id for g in plan.blocked_goals]
    task_goals = {t.goal_id for t in plan.tasks}
    if (
        len(blocked) != len(set(blocked))
        or set(blocked) & task_goals
        or set(blocked) | task_goals != goals.keys()
    ):
        raise PlanValidationError(
            "execution plan must cover every admitted goal with tasks OR blocked_reason"
        )
    tasks = {t.id: t for t in plan.tasks}
    if len(tasks) != len(plan.tasks):
        raise PlanValidationError("task IDs must be unique", path=("tasks",))
    for goal_id in task_goals:
        if not any(t.goal_id == goal_id and t.answer_role == "user_facing" for t in plan.tasks):
            raise PlanValidationError("goal needs a user_facing task")
    result_limits: dict[str, int] = {}
    for index, task in enumerate(plan.tasks):
        goal = goals[task.goal_id]
        allowed_refs = goal.context_refs
        contract = provider.get_contract(task.tool_name)
        if contract is None:
            raise PlanValidationError(f"{task.id}.tool_name is not registered")
        if read_only and not contract.read_only:
            raise PlanValidationError("recovery only permits read-only tool contracts")
        if not set(allowed_refs) <= objects.keys():
            raise PlanValidationError(f"{task.id}: goal references unavailable context")
        allowed_products = (
            explicit_product_ids(goal.query)
            | explicit_product_ids(goal.source_text)
            | referenced_product_ids(allowed_refs, objects)
        )
        if explicit_product_ids(str(task.arguments.get("query", ""))) - allowed_products:
            raise PlanValidationError(f"{task.id}: query invents explicit product IDs")
        try:
            public_input = contract.public_input_model.model_validate(task.arguments)
        except ValidationError as exc:
            raise ValidationError.from_exception_data(
                exc.title,
                [
                    {**error, "loc": ("tasks", index, "arguments", *error["loc"])}
                    for error in exc.errors(include_url=False)
                ],
            ) from exc
        if task.tool_name == "catalog_search":
            # Read effective defaults from the contract without adding omitted arguments.
            result_limits[task.id] = public_input.limit
        elif task.tool_name == "order_lookup":
            # A known order is a singleton; otherwise the tool honors the caller's limit.
            explicit_order = public_input.order_id is not None or len(
                explicit_order_ids(public_input.query or "")
            ) == 1
            result_limits[task.id] = 1 if explicit_order else public_input.limit
        task.arguments = public_input.model_dump(
            mode="json", exclude_none=True, exclude_unset=True
        )
        if (
            not set(task.depends_on) <= tasks.keys()
            or task.id in task.depends_on
            or len(task.depends_on) != len(set(task.depends_on))
        ):
            raise PlanValidationError(f"{task.id}.depends_on is invalid")
        if task.tool_name == "order_lookup":
            explicit_orders = explicit_order_ids(goal.source_text)
            order_id = task.arguments.get("order_id")
            order_bindings = [b for b in task.bindings if b.kind == "order"]
            if len(order_bindings) > 1 or any(len(b.positions) > 1 for b in order_bindings):
                raise PlanValidationError(f"{task.id}: one order lookup binds one order")
            contextual_orders = {
                objects[ref]["id"] for ref in allowed_refs
                if objects[ref].get("kind") == "order"
            } & explicit_order_ids(goal.query, include_unlabelled=False)
            bound_orders = {
                objects[b.ref]["id"]
                for b in order_bindings
                if b.source == "context"
                and b.ref in allowed_refs
                and objects.get(b.ref, {}).get("kind") == "order"
            }
            if order_id is not None:
                if order_id not in explicit_orders | contextual_orders:
                    raise PlanValidationError(
                        f"{task.id}: order_id must come from source_text or the resolved goal"
                    )
                if any(b.source == "task_output" for b in order_bindings):
                    raise PlanValidationError(
                        f"{task.id}: choose explicit order_id OR task_output bindings"
                    )
            if bound_orders and {order_id} != bound_orders:
                raise PlanValidationError(
                    f"{task.id}: known order binding requires matching submitted order_id"
                )
            query_orders = explicit_order_ids(str(task.arguments.get("query", "")))
            if query_orders - (explicit_orders | contextual_orders):
                raise PlanValidationError(f"{task.id}: query invents or uses unbound order IDs")
            if query_orders and any(b.source == "task_output" for b in order_bindings):
                raise PlanValidationError(
                    f"{task.id}: choose explicit query order IDs OR task_output bindings"
                )
            if query_orders & contextual_orders and order_id is None:
                raise PlanValidationError(
                    f"{task.id}: resolved historical order requires submitted order_id"
                )
            selected_orders = {order_id} if order_id is not None else bound_orders
            if selected_orders and query_orders and query_orders != selected_orders:
                raise PlanValidationError(
                    f"{task.id}: query order IDs conflict with selected order"
                )
        task_scope = task.arguments.get("scope")
        scoped = scope_ids(task_scope)
        if scoped:
            if scoped - allowed_products:
                raise PlanValidationError(f"{task.id}: scope IDs are outside the trusted goal")
            if task.tool_name in {"catalog_search", "catalog_facets"} and any(
                b.source == "task_output" for b in task.bindings
            ):
                raise PlanValidationError(f"{task.id}: choose direct scope OR task_output bindings")
        for binding in task.bindings:
            if binding.source == "context":
                if binding.ref not in allowed_refs or objects[binding.ref]["kind"] != binding.kind:
                    raise PlanValidationError(f"{task.id}: invalid context binding {binding.ref}")
            elif binding.ref not in task.depends_on:
                raise PlanValidationError(
                    f"{task.id}: input binding must reference declared dependency"
                )
            else:
                producers = {
                    "product": {"catalog_search"},
                    "order": {"order_lookup"},
                }
                if tasks[binding.ref].tool_name not in producers[binding.kind]:
                    raise PlanValidationError(
                        f"{task.id}: dependency cannot produce {binding.kind}"
                    )
            if binding.kind == "order" and binding.selection != "object":
                raise PlanValidationError(f"{task.id}: product selection cannot apply to orders")
            if binding.source == "context" and binding.positions not in ([], [1]):
                raise PlanValidationError(f"{task.id}: context binding represents a single object")
            expected = {
                "product": {"catalog_search", "catalog_facets"},
                "order": {"order_lookup"},
            }
            if task.tool_name not in expected[binding.kind]:
                raise PlanValidationError(f"{task.id}: binding kind incompatible with tool")
        validate_context_product_bindings(task, objects)

    # All producer arguments are validated before cross-task checks, regardless of array order.
    for task in plan.tasks:
        deferred = [b for b in task.bindings if b.source == "task_output"]
        if task.tool_name in {"catalog_search", "catalog_facets"} and {
            "candidate_versions", "parent_model",
        } <= {b.selection for b in deferred}:
            raise PlanValidationError(
                f"{task.id}: search bindings cannot combine SKU and SPU scopes "
                "(candidate_versions and parent_model)"
            )
        for binding in deferred:
            limit = result_limits.get(binding.ref)
            if limit is not None and any(p > limit for p in binding.positions):
                raise PlanValidationError(
                    f"{task.id}: positions {binding.positions} exceed "
                    f"{binding.ref} {tasks[binding.ref].tool_name} limit={limit}"
                )
    visiting, depths = set(), {}

    def depth(identity: str) -> int:
        if identity in visiting:
            raise PlanValidationError("task graph must be acyclic", path=("tasks",))
        if identity not in depths:
            visiting.add(identity)
            depths[identity] = 1 + max((depth(d) for d in tasks[identity].depends_on), default=0)
            visiting.remove(identity)
        return depths[identity]

    if any(depth(t) > 2 for t in tasks):
        raise PlanValidationError("plan exceeds the two tool-wave budget")


def validate_context_product_bindings(task: ToolTask, objects: dict[str, dict]) -> None:
    """Check known objects now; task-output identities remain deferred until execution."""
    bindings = [b for b in task.bindings if b.source == "context" and b.kind == "product"]
    if not bindings:
        return
    try:
        values = bound_inputs(
            {"context_objects": objects}, task.model_copy(update={"bindings": bindings})
        )
    except ValueError as exc:
        raise PlanValidationError(f"{task.id}: invalid context binding: {exc}") from exc
    query_ids = explicit_product_ids(str(task.arguments.get("query", "")))
    if task.tool_name in {"catalog_search", "catalog_facets"}:
        query_ids |= scope_ids(task.arguments.get("scope"))
    selected_ids = {(v["entity_scope"], identity) for v in values for identity in v["ids"]}
    # Optional context bindings prove only their own objects. Other submitted IDs may
    # come from explicit input; validate_execution checks the complete goal/query scope.
    if not selected_ids <= query_ids:
        raise PlanValidationError(
            f"{task.id}: submitted query/scope must identify objects selected by context bindings"
        )


async def plan_tools(runtime: Any, state: dict[str, Any]) -> None:
    policy = getattr(runtime, "planning_call_policy", PLANNING_CALL_POLICY)
    recovery = recovery_context(state, "llm2")
    deadline = (state["recovery_batch"]["deadline"] if recovery
                else _planning_deadline(state, policy))
    route = RequestRoutePlan.model_validate(state["route_plan"])
    objects = state.get("context_objects", {})

    def count() -> None:
        state["task_planner_call_count"] = state.get("task_planner_call_count", 0) + 1

    plan = await generate_plan(
        runtime.task_planner,
        "submit_execution_plan",
        ExecutionPlan,
        build_recovery_prompt("llm2") if recovery else TASK_PLANNER_PROMPT,
        {
            "goals": [
                g.model_dump(
                    mode="json",
                    include={"id", "query", "source_text", "context_refs"},
                    exclude_none=True,
                )
                for g in route.subqueries
                if g.disposition == "tool_planning"
                and (not recovery or g.id in state["recovery_batch"]["llm2_goal_ids"])
            ],
            "context_objects": {
                ref: objects[ref]
                for g in route.subqueries
                if g.disposition == "tool_planning"
                for ref in g.context_refs
            },
            "tools": [c.as_llm_tool() for c in runtime.contract_provider.list_contracts()],
            **({"recovery_context": recovery} if recovery else {}),
        },
        lambda p: validate_execution(
            merged_execution(state, p) if recovery else p,
            route, runtime.contract_provider, objects,
            read_only=bool(recovery),
        ),
        count,
        policy=policy,
        deadline=deadline,
    )
    if recovery:
        install_execution(state, merged_execution(state, plan))
        return
    state["execution_plan"] = plan.model_dump(mode="json")
    blocked = {g.goal_id for g in plan.blocked_goals}
    for goal in route.subqueries:
        if goal.disposition != "tool_planning":
            continue
        goal.planning_pending = goal.id in blocked
        goal.tasks = [
            RoutedTask(
                id=t.id,
                goal_id=goal.id,
                canonical_query=t.arguments.get("query") or goal.query,
                capability=t.tool_name,
                depends_on=t.depends_on,
                produces="result",
                answer_role=t.answer_role,
            )
            for t in plan.tasks
            if t.goal_id == goal.id
        ]
    state["route_plan"] = RequestRoutePlan.model_validate(route.model_dump()).model_dump(
        mode="json"
    )


def planned_task(state: Mapping[str, Any], task_id: str) -> ToolTask | None:
    for task in state.get("execution_plan", {}).get("tasks", []):
        if task["id"] == task_id:
            return ToolTask.model_validate(task)
    return None


def bound_inputs(state: Mapping[str, Any], task: ToolTask) -> list[dict[str, Any]]:
    values = []
    for binding in task.bindings:
        if binding.source == "context":
            source = [deepcopy(state["context_objects"][binding.ref])]
        else:
            artifact = state.get("task_artifacts", {}).get(binding.ref, {})
            if not artifact.get("usable"):
                raise ValueError("dependency did not produce usable facts")
            value = artifact.get("value") or {}
            if binding.kind == "product":
                source = [
                    {
                        "kind": "product",
                        "entity_scope": p.get("entity_scope", "sku"),
                        "ids": [p[f"{p.get('entity_scope', 'sku')}_id"]],
                        "spu_id": p.get("spu_id"),
                        "candidate_sku_ids": p.get("candidate_sku_ids") or [p["sku_id"]],
                    }
                    for p in value.get("products", [])
                ]
            else:
                # Positions select exact repository results; suggestions require confirmation.
                if value.get("result_type") == "order_suggestions" or value.get("suggestions"):
                    raise ValueError("order suggestions require user confirmation")
                if value.get("order"):
                    orders = [value["order"]]
                elif value.get("result_type") == "order_analysis":
                    plan = value.get("query_plan") or {}
                    if plan.get("operation") == "match_orders" and (
                        not plan.get("supported") or plan.get("unresolved_requirements")
                    ):
                        raise ValueError("order matching filters have not been validated")
                    orders = value.get("analysis_orders", [])
                else:
                    orders = value.get("candidates", [])
                source = [{"kind": "order", "id": o["id"]} for o in orders]
        if binding.positions:
            if any(p > len(source) for p in binding.positions):
                raise ValueError("bound candidate position is outside result window")
            source = [source[p - 1] for p in binding.positions]
        if not source:
            raise ValueError("input binding resolved to no objects")
        if binding.kind == "product" and binding.selection != "object":
            selected = []
            for item in source:
                ids = (
                    item.get("candidate_sku_ids")
                    if binding.selection == "candidate_versions"
                    else [item.get("spu_id")]
                )
                if not ids or not all(type(i) is int and i > 0 for i in ids):
                    raise ValueError("requested version/model identities are unavailable")
                selected.append(
                    {
                        **item,
                        "ids": ids,
                        "entity_scope": (
                            "sku" if binding.selection == "candidate_versions" else "spu"
                        ),
                    }
                )
            source = selected
        values.extend(source)
    return values


def bind_arguments(
    state: Mapping[str, Any], task: ToolTask, arguments: dict[str, Any]
) -> dict[str, Any]:
    args = dict(arguments)
    # Known context is provenance only: LLM1 resolves it and LLM2 supplies final arguments.
    # Only identities unavailable until a preceding tool returns may change arguments here.
    deferred = [binding for binding in task.bindings if binding.source == "task_output"]
    if not deferred:
        return args
    values = bound_inputs(state, task.model_copy(update={"bindings": deferred}))
    if task.tool_name in {"catalog_search", "catalog_facets"} and values:
        scopes = {v["entity_scope"] for v in values}
        if len(scopes) != 1:
            raise ValueError("search cannot combine SKU and SPU scopes")
        scope = scopes.pop()
        args["scope"] = {f"{scope}_ids": list(dict.fromkeys(i for v in values for i in v["ids"]))}
    if task.tool_name == "order_lookup" and values:
        ids = {v["id"] for v in values}
        if len(ids) != 1:
            raise ValueError("one order lookup binds one order")
        args["order_id"] = ids.pop()
    return args
