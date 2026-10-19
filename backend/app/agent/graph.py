import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import aclosing
from math import isfinite
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.config import get_stream_writer
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.answer_context import (
    answerable_source_tool_call_ids,
    build_answer_context,
    resolved_answer_task_ids,
)
from app.agent.answer_contract import validate_goal_coverage
from app.agent.artifacts import (
    bound_task_order_id,
    extract_wave_artifacts,
    initialize_task_runtime,
    refresh_task_status,
    user_clarifiable_blockers,
)
from app.agent.boundary import BOUNDARY_POLICY
from app.agent.capabilities import decision_from_route_capabilities
from app.agent.decisions import (
    OrchestratorDecision,
    PlannedToolCall,
    control_tool_definitions,
    decision_from_ai_message,
)
from app.agent.events import (
    _dump_evidence,
    _json_safe_state,
)
from app.agent.execution import execute_tool_wave
from app.agent.execution_details import build_execution_details
from app.agent.fallback_planner import (
    fallback_planner_decision,
    fallback_routed_tool_decision,
)
from app.agent.goal_planning import (
    GoalPlan,
    GoalReview,
    PlanValidationError,
    bind_arguments,
    definition,
    error_feedback,
    execution_definition,
    generate_plan,
    plan_tools,
    planned_task,
    route_goals,
)
from app.agent.limits import (
    ANSWER_CALL_POLICY,
    CLEANUP_TIMEOUT_SECONDS,
    MAX_ORCHESTRATOR_CALLS,
    PLANNING_CALL_POLICY,
    TOOL_CONCURRENCY_LIMIT,
    TURN_TIMEOUT_SECONDS,
    PlanningCallPolicy,
)
from app.agent.outcomes import (
    build_subquery_ledger,
    normalize_tool_result,
    validate_terminal_decision,
)
from app.agent.projections import (
    rebuild_tool_projections,
)
from app.agent.prompts import (
    GENERAL_DIRECT_RESPONSE_PROMPT,
    SESSION_GROUNDED_RESPONSE_PROMPT,
)
from app.agent.prompts.observation import build_answer_prompt
from app.agent.recovery import (
    admit_recovery,
    answer_limit,
    fail_stage,
    mark_superseded,
    merge_route,
    recovery_context,
    recovery_enabled,
    settle_batch,
    start_stage,
    wave_limit,
)
from app.agent.responses import (
    LATE_HANDOFF_CONFIRMATION as LATE_HANDOFF_CONFIRMATION,
)
from app.agent.responses import (
    _append_blocked_route_notices,
    _append_late_handoff_confirmation,
    _fallback_answer,
    _route_terminal_answer,
    _suggest_actions,
    fallback_goal_response,
)
from app.agent.responses import (
    _fallback_unavailable_answer as _fallback_unavailable_answer,
)
from app.agent.responses import (
    _has_successful_tool_result as _has_successful_tool_result,
)
from app.agent.route_runtime import (
    _boundary_from_route_plan,
    _fallback_rewritten_query,
    _fallback_route_disposition,
    _intent_from_route_plan,
    _resolve_order_id,
    _routed_query_for_call,
    _split_request_subqueries,
)
from app.agent.route_runtime import (
    _deterministic_pre_route_plan as _deterministic_pre_route_plan,
)
from app.agent.route_runtime import (
    _enforce_route_boundaries as _enforce_route_boundaries,
)
from app.agent.route_runtime import (
    _fallback_catalog_query as _fallback_catalog_query,
)
from app.agent.route_runtime import (
    _request_router_messages as _request_router_messages,
)
from app.agent.routing import (
    RequestRoutePlan,
    RoutedSubquery,
    RoutedTask,
    blocked_subqueries,
    tool_planning_subqueries,
    user_facing_tasks,
)
from app.agent.state import AgentState
from app.agent.tool_loop import (
    _all_planned_subqueries_usable,
    _clarification_decision,
    _constrain_calls_to_route_plan,
    _deterministic_recovery_decision,
    _orchestrator_business_tool_definition,
    _plain_text_observation_decision,
    _planner_requires_business_tools,
    _ready_unattempted_tool_subqueries,
    _terminal_fallback_decision,
    _unique_tool_call_ids,
)
from app.agent.tool_loop import (
    _followup_tool_call_allowed as _followup_tool_call_allowed,
)
from app.agent.tool_loop import (
    _orchestrator_messages as _orchestrator_messages,
)
from app.agent.tool_loop import (
    _state_terminal_decision as _state_terminal_decision,
)
from app.agent.tool_loop import (
    _tag_from_decision as _tag_from_decision,
)
from app.agent.workflow import build_agent_graph
from app.core.config import Settings
from app.core.llm import build_chat_model
from app.core.llm_calls import invoke_before, retryable_transport_error, transport_retry_delay
from app.core.model_usage import collect_model_calls, record_model_call
from app.core.tracing import agent_trace, trace_event
from app.repositories.conversations import ConversationRepository
from app.rules.chat_input import match_input_rule, normalized_text, rule_by_id
from app.schemas.chat import (
    BoundaryClassification,
    ChatRequest,
    ChatResponse,
    SuggestedAction,
)
from app.services.context import ConversationContextService, serialize_compact_audit
from app.services.memory import MemoryService
from app.tools.contracts import (
    DefaultToolContractProvider,
    RegistryToolExecutor,
    ToolContractProvider,
    ToolExecutor,
)

logger = logging.getLogger(__name__)


def _apply_handoff_outcome_policy(
    decision: OrchestratorDecision,
    answer_context: dict[str, Any],
    *,
    observation_coverage_failure_allowed: bool = False,
) -> OrchestratorDecision:
    """Offer handoff for Tool capability gaps or a validated order coverage failure."""
    outcome_counts = answer_context.get("outcome_counts", {})
    should_offer = (
        (
            bool(outcome_counts.get("unsupported_capability"))
            or (
                decision.observation_coverage_failure
                and observation_coverage_failure_allowed
            )
        )
        and decision.control_action in {"finish_partial", "finish_unavailable"}
    )
    if decision.offer_handoff_confirmation == should_offer:
        return decision
    return decision.model_copy(update={"offer_handoff_confirmation": should_offer})


def _order_observation_coverage_check_allowed(state: AgentState) -> bool:
    """Limit Answer-level coverage overrides to order-only user-facing tasks."""
    tasks = user_facing_tasks(state.get("route_plan"))
    artifacts = state.get("task_artifacts", {})
    return bool(tasks) and all(
        task.capability == "order_lookup"
        or (
            task.capability == "planner_required"
            and isinstance(artifacts.get(task.id), dict)
            and artifacts[task.id].get("source_tool_name") == "order_lookup"
        )
        for task in tasks
    )


class AgentRuntime:
    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        *,
        contract_provider: ToolContractProvider | None = None,
        tool_executor: ToolExecutor | None = None,
        chat_model: Any | None = None,
        router_model: Any | None = None,
        context_service: ConversationContextService | None = None,
        memory_service: MemoryService | None = None,
        planning_call_policy: PlanningCallPolicy = PLANNING_CALL_POLICY,
        answer_call_policy: PlanningCallPolicy = ANSWER_CALL_POLICY,
        turn_timeout_seconds: float = TURN_TIMEOUT_SECONDS,
        cleanup_timeout_seconds: float = CLEANUP_TIMEOUT_SECONDS,
        tool_concurrency_limit: int = TOOL_CONCURRENCY_LIMIT,
    ):
        if (
            type(tool_concurrency_limit) is not int
            or not 1 <= tool_concurrency_limit <= TOOL_CONCURRENCY_LIMIT
        ):
            raise ValueError("Tool concurrency must be between 1 and 3")
        self.tool_concurrency_limit = tool_concurrency_limit
        self.session = session
        self.settings = settings
        self.planning_call_policy = planning_call_policy
        self.answer_call_policy = answer_call_policy
        self.turn_timeout_seconds = turn_timeout_seconds
        self.cleanup_timeout_seconds = cleanup_timeout_seconds
        if any(not isfinite(v) or v <= 0 for v in (turn_timeout_seconds, cleanup_timeout_seconds)):
            raise ValueError("Turn and cleanup timeouts must be finite and positive")
        self.contract_provider = contract_provider or DefaultToolContractProvider()
        self.tool_executor = tool_executor or RegistryToolExecutor(
            session, settings,
            tool_session_factory=async_sessionmaker(
                bind=getattr(session, "bind", None), expire_on_commit=False,
            ),
        )
        self.context_service = context_service or ConversationContextService(session, settings)
        self.memory_service = memory_service or MemoryService()
        self.llm = (
            chat_model if chat_model is not None else build_chat_model(
                settings, temperature=0.2, max_retries=0,
                timeout=answer_call_policy.call_timeout_seconds,
            )
        )
        tool_planner_base_model = (
            chat_model
            if chat_model is not None
            else build_chat_model(
                settings, temperature=0.0,
                max_retries=planning_call_policy.sdk_max_retries,
                timeout=planning_call_policy.call_timeout_seconds,
            )
        )
        business_tools = [
            _orchestrator_business_tool_definition(contract)
            for contract in self.contract_provider.list_contracts()
        ]
        controls_by_name = {
            definition["function"]["name"]: definition for definition in control_tool_definitions()
        }
        observation_controls = [
            controls_by_name[name]
            for name in (
                "finish_answer",
                "finish_partial",
                "finish_unavailable",
                "ask_clarification",
            )
        ]
        router_base_model = router_model
        if router_base_model is None and chat_model is None:
            router_base_model = build_chat_model(
                settings, temperature=0.0,
                max_retries=planning_call_policy.sdk_max_retries,
                timeout=planning_call_policy.call_timeout_seconds,
            )
        self.request_router = (
            router_base_model.bind_tools(
                [definition("route_request", GoalPlan)], tool_choice="required"
            )
            if router_base_model
            else None
        )
        self.tool_planner = (
            tool_planner_base_model.bind_tools(business_tools) if tool_planner_base_model else None
        )
        self.task_planner = (
            tool_planner_base_model.bind_tools(
                [execution_definition(self.contract_provider)], tool_choice="required"
            )
            if tool_planner_base_model
            else None
        )
        self.goal_summarizer = (
            self.llm.bind_tools([definition("summarize_goals", GoalReview)], tool_choice="required")
            if self.llm
            else None
        )
        self.answer_synthesizer = (
            self.llm.bind_tools(
                [*observation_controls, controls_by_name["request_recovery"]],
                tool_choice="required",
            ) if self.llm else None
        )
        self.final_answer_synthesizer = (
            self.llm.bind_tools(observation_controls, tool_choice="required") if self.llm else None
        )
        self.finish_answer_synthesizer = (
            self.llm.bind_tools(
                [controls_by_name["finish_answer"]],
                tool_choice="required",
            )
            if self.llm
            else None
        )

    async def run(self, request: ChatRequest, user_id: int) -> ChatResponse:
        with agent_trace(self.settings, source="chat"):
            state = self._initial_state(request, user_id)
            async with aclosing(self._run_graph_updates(state)) as updates:
                async for _ in updates:
                    pass
            response = self._response_from_state(state)
            await trace_event("chat_response_prepared", agent_run_id=state.get("run_id"))
            return response

    async def _run_graph_updates(self, state: AgentState) -> AsyncIterator[tuple[str, Any]]:
        """Chat execution lifecycle: retain completed nodes and never replay on failure."""
        with collect_model_calls():
            completed = False
            deadline = asyncio.get_running_loop().time() + self.turn_timeout_seconds
            state["turn_deadline_monotonic"] = deadline
            try:
                async with asyncio.timeout_at(deadline):
                    stream = self._build_graph().astream(state, stream_mode=["custom", "updates"])
                    async with aclosing(stream):
                        async for mode, update in stream:
                            if mode == "updates":
                                for node_name, values in update.items():
                                    state.update(values)
                                    if node_name == "persist_turn":
                                        completed = True
                            if asyncio.get_running_loop().time() >= deadline:
                                raise TimeoutError("Turn returned after its deadline")
                            yield mode, update
            except BaseException as exc:
                if not completed:
                    cancelled = isinstance(exc, (asyncio.CancelledError, GeneratorExit))
                    await self._cleanup_failed_turn(
                        state, "cancelled" if cancelled else type(exc).__name__,
                    )
                raise

    async def _cleanup_failed_turn(self, state: AgentState, error_type: str) -> None:
        # Roll back answer/memory writes before the independent failure-audit transaction.
        # A second failure must not replace the original error or swallow cancellation.
        await trace_event(
            "turn_cleanup_started", conversation_id=state.get("conversation_id"),
            agent_run_id=state.get("run_id"), error_type=error_type,
        )
        try:
            async with asyncio.timeout(self.cleanup_timeout_seconds):
                if self.session is not None:
                    await self.session.rollback()
                await self._mark_run_failed(state, error_type, "Turn did not complete")
        except (Exception, asyncio.CancelledError) as exc:
            await trace_event(
                "turn_cleanup_failed", agent_run_id=state.get("run_id"),
                error_type=type(exc).__name__,
            )
            logger.warning("Turn failure cleanup did not complete: %s", type(exc).__name__)
            return
        await trace_event(
            "turn_cleanup_completed", agent_run_id=state.get("run_id"),
            failure_audit_attempted=bool(state.get("run_id")),
        )


    def _initial_state(self, request: ChatRequest, user_id: int) -> AgentState:
        return {
            "user_id": user_id,
            "conversation_id": request.conversation_id,
            "message": request.message,
            "request_router_call_count": 0,
            "orchestrator_call_count": 0,
            "tool_wave_count": 0,
            "tool_waves": [],
            "tool_results": [],
            "subquery_ledger": [],
            "parsed": {},
            "products": [],
            "evidence": [],
            "order": None,
        }

    def _build_graph(self):
        return build_agent_graph(self)

    async def _apply_input_rules(self, state: AgentState) -> AgentState:
        """Decide from the current text before context preparation or any LLM call."""
        matched = match_input_rule(state["message"])
        if matched is not None:
            state["input_rule_id"] = matched.id
            # Do this before prepare_turn writes a user message, and before audit snapshots.
            # Rejected text must not enter history, planning fields or audit snapshots.
            if matched.recorded_message is not None:
                state["message"] = matched.recorded_message
        return state

    async def _load_context(self, state: AgentState) -> AgentState:
        prepared = await self.context_service.prepare_turn(
            state["user_id"], state.get("conversation_id"), state["message"]
        )
        state["prepared_turn"] = prepared
        state["conversation_id"] = prepared.conversation_id
        state["user_message_id"] = prepared.user_message_id
        state["run_id"] = prepared.run_id
        state["history"] = [item.model_dump(mode="json") for item in prepared.history]
        state["working_memory"] = prepared.working_memory.model_dump(mode="json")
        state["working_memory_snapshot"] = prepared.working_memory.model_dump(mode="json")
        state["memory"] = [item.model_dump(mode="json") for item in prepared.memory]
        await trace_event(
            "turn_context_loaded", conversation_id=prepared.conversation_id,
            agent_run_id=prepared.run_id, user_message_id=prepared.user_message_id,
        )
        return state

    async def _request_route(self, state: AgentState) -> AgentState:
        if recovery_context(state, "llm1"):
            start_stage(state, "llm1")
            try:
                merge_route(state, await route_goals(self, state))
            except Exception as exc:
                fail_stage(state, "llm1", f"recovery_planning_failed:{type(exc).__name__}")
            return state
        try:
            matched = rule_by_id(state["input_rule_id"]) if state.get("input_rule_id") else None
            if matched is not None:
                query = normalized_text(state["message"]) or "[空白输入]"
                plan = RequestRoutePlan(
                    rewritten_query=query,
                    subqueries=[RoutedSubquery(
                        id="goal_1", query=query,
                        disposition=matched.disposition, reason_code=matched.id,
                    )],
                )
                source = "input_rule"
            elif self.request_router is not None:
                plan = await route_goals(self, state)
                source = "goal_router_llm"
            else:
                raise ValueError("goal router unavailable for this request")
            # Nonmatching/contextual input belongs to LLM1, not legacy keyword overrides.
            for goal in plan.subqueries:
                goal.tasks = []
                goal.planning_pending = goal.disposition == "tool_planning"
            plan = RequestRoutePlan.model_validate(plan.model_dump())
        except Exception as exc:
            state.pop("input_rule_id", None)
            state["planning_error"] = error_feedback(exc)
            source = "goal_router_failed"
            plan = RequestRoutePlan(
                rewritten_query=state["message"],
                subqueries=[
                    RoutedSubquery(
                        id="goal_1",
                        query=state["message"],
                        disposition="tool_planning",
                        reason_code="planning_failed",
                        planning_pending=True,
                    )
                ],
            )
        state["route_source"] = source
        state["route_plan"] = plan.model_dump(mode="json")
        state["rewritten_query"] = plan.rewritten_query
        state["boundary"] = _boundary_from_route_plan(plan)
        if rule_id := state.get("input_rule_id"):
            state["boundary"].update(reason=rule_id, display_message=rule_by_id(rule_id).message)
        state["intent"] = _intent_from_route_plan(plan)
        initialize_task_runtime(state)
        return state

    async def _plan_tools(self, state: AgentState) -> AgentState:
        if recovery_context(state, "llm2"):
            start_stage(state, "llm2")
            try:
                await plan_tools(self, state)
            except Exception as exc:
                fail_stage(state, "llm2", f"recovery_planning_failed:{type(exc).__name__}")
            refresh_task_status(state)
            rebuild_tool_projections(state)
            return state
        try:
            if self.task_planner is None:
                raise ValueError("tool task planner unavailable")
            await plan_tools(self, state)
        except Exception as exc:
            state["planning_error"] = error_feedback(exc)
        initialize_task_runtime(state)
        state["boundary"] = _boundary_from_route_plan(
            RequestRoutePlan.model_validate(state["route_plan"])
        )
        return state

    def _dispatch_planned(self, state: AgentState) -> str:
        if state.get("recovery_batch"):
            return "recovery"
        return (
            "answer"
            if state.get("planning_error") or not tool_planning_subqueries(state["route_plan"])
            else "execute"
        )

    async def _summarize_goals(self, state: AgentState) -> AgentState:
        settle_batch(state)
        fallback = fallback_goal_response(state)
        if state.get("planning_error"):
            # Planning has ended: do not start another model call on its failure path.
            state["answer"] = fallback["response"]
            state["terminal_guard_status"] = "fallback"
            state["suggested_actions"] = _suggest_actions(state)
            return state
        # LLM3 is also the only answer phase when LLM2 is skipped.
        context = build_answer_context(state)
        state["answer_context"] = context
        payload = {
            "answer_context": context,
            "history": state.get("history", []),
            "planning_failed": bool(state.get("planning_error")),
        }
        answer = fallback["response"]
        state["terminal_guard_status"] = "fallback"
        if self.goal_summarizer is not None:
            try:

                def count():
                    if state.get("answer_call_count", 0) >= answer_limit(
                        state, self.answer_call_policy.max_attempts,
                    ):
                        raise ValueError("answer_attempt_budget_exhausted")
                    state["orchestrator_call_count"] = state.get("orchestrator_call_count", 0) + 1
                    state["answer_call_count"] = state.get("answer_call_count", 0) + 1

                def validate(result):
                    if result.recovery_requests:
                        if not recovery_enabled(state):
                            raise PlanValidationError("recovery is no longer available")
                        return
                    try:
                        validate_goal_coverage(result.covered_goal_ids, context["goals"])
                    except ValueError as exc:
                        raise PlanValidationError(str(exc), path=("covered_goal_ids",)) from exc

                result = await generate_plan(
                    self.goal_summarizer,
                    "summarize_goals",
                    GoalReview,
                    build_answer_prompt(
                        output_mode="summary", recovery_enabled=recovery_enabled(state),
                    ),
                    payload,
                    validate,
                    count,
                    policy=self.answer_call_policy,
                    deadline=self._answer_deadline(state),
                )
                if result.recovery_requests:
                    state["decision"] = OrchestratorDecision(
                        type="recovery",
                        recovery_requests=[r.model_dump() for r in result.recovery_requests],
                    ).model_dump(mode="json")
                    return state
                answer = result.response
                state["terminal_guard_status"] = "accepted"
            except Exception:
                pass
        state["answer"] = answer
        state["suggested_actions"] = _suggest_actions(state)
        return state

    def _answer_deadline(self, state: AgentState) -> float:
        key = ("final_answer_deadline_monotonic" if state.get("recovery_batch")
               else "answer_deadline_monotonic")
        if key not in state:
            state[key] = min(
                asyncio.get_running_loop().time() + self.answer_call_policy.total_timeout_seconds,
                state.get("turn_deadline_monotonic", float("inf")),
            )
        return state[key]

    def _fallback_route_plan(self, state: AgentState) -> RequestRoutePlan:
        rewritten = _fallback_rewritten_query(state, self.memory_service)
        segments = _split_request_subqueries(rewritten)
        subqueries: list[RoutedSubquery] = []
        for index, query in enumerate(segments, start=1):
            disposition = _fallback_route_disposition(
                query,
                state.get("working_memory_snapshot", {}),
            )
            clarification_question = (
                "你具体想咨询哪款商品、哪笔订单或哪项商城服务？"
                if disposition == "clarification"
                else ""
            )
            subqueries.append(
                RoutedSubquery(
                    id=f"sq_{index}",
                    query=query,
                    disposition=disposition,
                    reason_code=f"fallback_{disposition}",
                    missing_information=(
                        ["具体咨询对象"] if disposition == "clarification" else []
                    ),
                    clarification_question=clarification_question,
                )
            )
        return RequestRoutePlan(rewritten_query=rewritten, subqueries=subqueries)

    def _dispatch_route(self, state: AgentState) -> str:
        if state.get("recovery_batch"):
            return "recovery"
        if state.get("input_rule_id"):
            return "rule"
        plan = RequestRoutePlan.model_validate(state["route_plan"])
        if state.get("planning_error"):
            return "answer"
        return (
            "plan" if any(g.disposition == "tool_planning" for g in plan.subqueries) else "answer"
        )

    async def _render_input_rule_reply(self, state: AgentState) -> AgentState:
        rule = rule_by_id(state["input_rule_id"])
        state["answer"] = rule.message
        state["suggested_actions"] = _suggest_actions(state)
        return state

    async def _orchestrate(self, state: AgentState) -> AgentState:
        if state.get("execution_plan"):
            refresh_task_status(state)
            ready = _ready_unattempted_tool_subqueries(state)
            batch = state.get("recovery_batch")
            if batch and ready and (
                asyncio.get_running_loop().time() >= batch["deadline"]
                or state.get("tool_wave_count", 0) >= wave_limit(state)
            ):
                batch["blocked_task_ids"].extend(t.id for t in ready)
                for item in batch["items"]:
                    if item["status"] != "settled":
                        item.update(status="settled", reason="recovery_budget_exhausted")
                refresh_task_status(state)
                ready = []
            if ready:
                if state.get("tool_wave_count", 0) >= wave_limit(state):
                    state["planning_error"] = {"code": "tool_wave_budget_exhausted"}
                    state["decision"] = _state_terminal_decision(
                        state, "tool_wave_budget_exhausted"
                    ).model_dump(mode="json")
                    return state
                calls = []
                for task in ready:
                    planned = planned_task(state, task.id)
                    calls.append(
                        PlannedToolCall(
                            id=f"planned_{task.id}",
                            name=planned.tool_name,
                            arguments=planned.arguments,
                            subquery=task.id,
                            canonical_query=task.query,
                            tool_query=task.query,
                        )
                    )
                state["decision"] = OrchestratorDecision(
                    type="tool_calls", reason="validated_task_plan", tool_calls=calls
                ).model_dump(mode="json")
                return state
        settle_batch(state)
        previous_call_count = state.get("orchestrator_call_count", 0)
        has_ready_tasks = bool(_ready_unattempted_tool_subqueries(state))
        recovery_decision = None if has_ready_tasks else _deterministic_recovery_decision(state)
        capability_decision = decision_from_route_capabilities(
            RequestRoutePlan.model_validate(state["route_plan"]), state
        )
        capability_decision = capability_decision or recovery_decision
        if state.get("execution_plan"):
            capability_decision = None

        if capability_decision is not None:
            call_count = previous_call_count
            decision = capability_decision
        else:
            call_count = previous_call_count + 1

        if capability_decision is not None:
            pass
        elif call_count > answer_limit(state, MAX_ORCHESTRATOR_CALLS):
            decision = _state_terminal_decision(state, "orchestration_limit_reached")
        elif self._orchestrator_model_for_state(state) is not None:
            try:
                decision = await self._invoke_orchestrator_decision(state, call_count)
            except (ValidationError, ValueError, TypeError) as exc:
                reason = f"invalid_orchestrator_response:{type(exc).__name__}"
                decision = OrchestratorDecision(type="invalid", reason=reason)
            except Exception as exc:
                if not state.get("tool_results") and _planner_requires_business_tools(state):
                    raise
                reason = f"orchestrator_unavailable:{type(exc).__name__}"
                decision = _state_terminal_decision(state, reason)
        else:
            decision = self._fallback_planner_decision(state)

        decision = self._validate_decision_budget(state, decision, call_count)
        state["orchestrator_call_count"] = max(
            previous_call_count, state.get("orchestrator_call_count", 0)
        )
        state["decision"] = decision.model_dump(mode="json")
        state["intent"] = _tag_from_decision(decision, state.get("intent"))
        state["boundary"] = _boundary_from_route_plan(
            RequestRoutePlan.model_validate(state["route_plan"])
        )
        return state

    async def _invoke_orchestrator_decision(
        self,
        state: AgentState,
        call_count: int,
    ) -> OrchestratorDecision:
        model = self._orchestrator_model_for_state(state)
        answer_phase = not _planner_requires_business_tools(state)
        policy = self.answer_call_policy
        attempts = policy.max_attempts if answer_phase else 1
        repairs = transport_retries = 0
        answer_deadline = self._answer_deadline(state)
        for attempt in range(attempts):
            if state.get("answer_call_count", 0) >= answer_limit(
                state, self.answer_call_policy.max_attempts,
            ):
                raise ValueError("answer_attempt_budget_exhausted")
            prompt_state = state
            if repairs:
                prompt_state = {
                    **state,
                    "terminal_guard_feedback": (
                        "Answer Synthesizer 上一输出不合法。已有 Artifact 时必须调用一个"
                        "已绑定终止控制 Tool；不得再次调用业务 Tool。"
                    ),
                }
            try:
                deadline = min(
                    answer_deadline,
                    asyncio.get_running_loop().time()
                    + self.answer_call_policy.call_timeout_seconds,
                )
                if asyncio.get_running_loop().time() >= deadline:
                    raise TimeoutError("answer_deadline_exceeded")
                state["orchestrator_call_count"] = state.get("orchestrator_call_count", 0) + 1
                state["answer_call_count"] = state.get("answer_call_count", 0) + 1
                message = await invoke_before(
                    model, _orchestrator_messages(prompt_state, call_count), deadline,
                    stage="answer",
                )
                if not isinstance(message, AIMessage):
                    raise TypeError("orchestrator returned a non-AI message")
                try:
                    decision = decision_from_ai_message(message)
                except ValueError:
                    recovered = (
                        None if state.get("route_plan") or message.invalid_tool_calls
                        or message.tool_calls
                        else _plain_text_observation_decision(state, message)
                    )
                    if recovered is not None:
                        return recovered
                    raise
                if answer_phase and decision.type == "tool_calls":
                    raise ValueError("answer synthesizer returned a business tool call")
                if decision.type == "recovery" and not recovery_enabled(state):
                    raise ValueError("recovery is no longer available")
                return decision
            except (ValidationError, ValueError, TypeError):
                if repairs >= policy.max_output_repairs or attempt + 1 >= attempts:
                    raise
                repairs += 1
            except Exception as exc:
                if not retryable_transport_error(exc):
                    raise
                await trace_event(
                    "answer_transport_error", stage="control", attempt=attempt + 1,
                    error_type=type(exc).__name__, status_code=getattr(exc, "status_code", None),
                )
                delay = transport_retry_delay(exc, policy.retry_delay_seconds)
                if (
                    transport_retries >= policy.max_transport_retries
                    or attempt + 1 >= attempts
                    or state.get("answer_call_count", 0) >= answer_limit(state, policy.max_attempts)
                    or asyncio.get_running_loop().time() + delay >= answer_deadline
                ):
                    raise
                transport_retries += 1
                await trace_event(
                    "answer_transport_retry", stage="control", attempt=attempt + 1,
                    delay_seconds=delay,
                )
                async with asyncio.timeout_at(answer_deadline):
                    await asyncio.sleep(delay)
        raise RuntimeError("unreachable observation retry state")

    def _orchestrator_model_for_state(self, state: AgentState) -> Any:
        if state.get("recovery_batch"):
            return self.final_answer_synthesizer
        if state.get("execution_plan"):
            return self.answer_synthesizer
        if _planner_requires_business_tools(state):
            return self.tool_planner
        if _all_planned_subqueries_usable(state):
            if _order_observation_coverage_check_allowed(state):
                return self.answer_synthesizer
            return self.finish_answer_synthesizer
        return self.answer_synthesizer

    def _validate_decision_budget(
        self,
        state: AgentState,
        decision: OrchestratorDecision,
        call_count: int,
    ) -> OrchestratorDecision:
        if decision.type != "tool_calls":
            return decision

        known_tools = {contract.name for contract in self.contract_provider.list_contracts()}
        if not decision.tool_calls or any(
            call.name not in known_tools for call in decision.tool_calls
        ):
            if state.get("tool_results"):
                return _state_terminal_decision(state, "unknown_or_empty_tool_call")
            return _clarification_decision(
                "我暂时无法安全选择业务工具，请换一种方式描述你的需求。",
                "unknown_or_empty_tool_call",
            )
        if state.get("route_plan"):
            constrained_calls = _constrain_calls_to_route_plan(state, decision.tool_calls)
            ready_tasks = _ready_unattempted_tool_subqueries(state)
            calls_by_task = {call.subquery: call for call in reversed(constrained_calls)}
            missing_tasks = [task for task in ready_tasks if task.id not in calls_by_task]
            if missing_tasks:
                fallback = self._fallback_routed_tool_decision(state, missing_tasks)
                for call in _constrain_calls_to_route_plan(state, fallback.tool_calls):
                    calls_by_task.setdefault(call.subquery, call)
            if ready_tasks:
                constrained_calls = [
                    calls_by_task[task.id] for task in ready_tasks if task.id in calls_by_task
                ]
            if not constrained_calls:
                return _state_terminal_decision(state, "action_compiler_could_not_map_ready_tasks")
            decision = decision.model_copy(update={"tool_calls": constrained_calls})
        decision = decision.model_copy(
            update={
                "tool_calls": _unique_tool_call_ids(
                    decision.tool_calls,
                    state.get("tool_waves", []),
                    call_count,
                )
            }
        )
        if state.get("tool_wave_count", 0) >= wave_limit(state) or (
            call_count >= MAX_ORCHESTRATOR_CALLS
        ):
            return _state_terminal_decision(state, "orchestration_limit_reached")
        if state.get("tool_wave_count", 0) > 0:
            allowed_calls: list[PlannedToolCall] = []
            for call in decision.tool_calls:
                try:
                    effective_call, _ = self._prepare_tool_call(state, call)
                except ValidationError:
                    continue
                if _followup_tool_call_allowed(state, effective_call):
                    allowed_calls.append(call)
            if not allowed_calls:
                return _state_terminal_decision(state, "unnecessary_or_invalid_followup")
            decision = decision.model_copy(update={"tool_calls": allowed_calls})
        return decision

    def _dispatch_decision(self, state: AgentState) -> str:
        decision_type = state["decision"]["type"]
        if decision_type == "recovery":
            return "recovery"
        if decision_type == "tool_calls":
            return "execute"
        return "guard"

    async def _execute_tool_wave(self, state: AgentState) -> AgentState:
        return await execute_tool_wave(
            self,
            state,
            repository_factory=ConversationRepository,
            stream_writer_factory=get_stream_writer,
        )

    async def _normalize_tool_results(self, state: AgentState) -> AgentState:
        state["normalized_tool_results"] = [
            normalize_tool_result(result).model_dump(mode="json")
            for result in state.get("tool_results", [])
        ]
        return state

    async def _extract_task_artifacts(self, state: AgentState) -> AgentState:
        extract_wave_artifacts(state)
        return state

    async def _update_subquery_ledger(self, state: AgentState) -> AgentState:
        state["subquery_ledger"] = [
            entry.model_dump(mode="json")
            for entry in build_subquery_ledger(state.get("tool_waves", []))
        ]
        mark_superseded(state)
        refresh_task_status(state)
        rebuild_tool_projections(state)
        return state

    async def _recovery_guard(self, state: AgentState) -> AgentState:
        if not state.get("recovery_batch"):
            admit_recovery(state, state["decision"]["recovery_requests"])
            batch = state["recovery_batch"]
            await trace_event(
                "recovery_admission", batch_id=batch["id"], phase=batch["phase"],
                decisions=[{key: item.get(key) for key in (
                    "goal_id", "task_ids", "target_stage", "status", "reason",
                )} for item in batch["items"]],
                affected_task_ids=batch["affected_task_ids"],
                protected_task_ids=[task["id"] for task in batch["protected_tasks"]],
                wave_limit=batch["wave_limit"],
            )
        state["decision"] = {}
        return state

    async def _recovery_dispatch(self, state: AgentState) -> AgentState:
        batch = state["recovery_batch"]
        if batch["phase"] == "done":
            settle_batch(state)
        refresh_task_status(state)
        rebuild_tool_projections(state)
        await trace_event(
            "recovery_dispatch", batch_id=batch["id"], phase=batch["phase"],
            replacements=batch["replacements"],
            blocked_task_ids=batch["blocked_task_ids"],
            available_artifact_ids=list(state.get("task_artifacts", {})),
        )
        return state

    def _dispatch_recovery(self, state: AgentState) -> str:
        phase = state["recovery_batch"]["phase"]
        if phase in {"llm1", "llm2"}:
            return phase
        return "tools" if tool_planning_subqueries(state["route_plan"]) else "summary"

    def _dispatch_summary(self, state: AgentState) -> str:
        return "recovery" if state.get("decision", {}).get("type") == "recovery" else "persist"

    async def _terminal_guard(self, state: AgentState) -> AgentState:
        """Validate final output only; never rewrite it or schedule another call."""
        decision = OrchestratorDecision.model_validate(state["decision"])
        answer_context = build_answer_context(state)
        has_unsupported_capability = bool(
            answer_context["outcome_counts"].get("unsupported_capability")
        )
        observation_coverage_failure_allowed = (
            decision.observation_coverage_failure
            and _order_observation_coverage_check_allowed(state)
        )
        route_boundary = _boundary_from_route_plan(
            RequestRoutePlan.model_validate(state["route_plan"])
        )
        current_boundary = str(state.get("boundary", {}).get("classification") or "")
        boundary_consistent = not current_boundary or current_boundary == str(
            route_boundary.get("classification") or ""
        )
        validation = validate_terminal_decision(
            decision,
            state.get("subquery_ledger", []),
            planned_subquery_ids=[item.id for item in user_facing_tasks(state["route_plan"])],
            clarification_allowed=bool(user_clarifiable_blockers(state))
            or any(
                g.disposition == "clarification"
                for g in RequestRoutePlan.model_validate(state["route_plan"]).subqueries
            ),
            resolved_task_ids=resolved_answer_task_ids(state),
            usable_artifact_tool_call_ids=[
                str(artifact.get("source_tool_call_id"))
                for artifact in state.get("task_artifacts", {}).values()
                if artifact.get("usable") and artifact.get("source_tool_call_id")
            ],
            answerable_tool_call_ids=answerable_source_tool_call_ids(state),
            supporting_tool_call_ids=answer_context["supporting_source_tool_call_ids"],
            boundary_consistent=boundary_consistent,
            observation_coverage_failure_allowed=observation_coverage_failure_allowed,
            handoff_confirmation_allowed=(
                (
                    has_unsupported_capability
                    and answer_context["completion"] in {"partial", "none"}
                    and bool(answer_context["unresolved_task_ids"])
                )
                or observation_coverage_failure_allowed
            )
            and route_boundary.get("classification") == "in_scope_auto",
        )
        if validation.valid:
            try:
                if (
                    decision.control_action == "finish_answer"
                    and answer_context["completion"] == "partial"
                ):
                    raise ValueError("finish_answer_has_pending_goals")
                if decision.covered_goal_ids or any(
                    g["disposition"] != "tool_planning" for g in answer_context["goals"]
                ):
                    validate_goal_coverage(decision.covered_goal_ids, answer_context["goals"])
            except ValueError as exc:
                validation = validation.model_copy(update={"valid": False, "reason": str(exc)})
        state["terminal_guard_status"] = "accepted" if validation.valid else "rejected"
        if validation.valid:
            state.pop("terminal_guard_feedback", None)
        else:
            state["terminal_guard_feedback"] = validation.reason
        return state

    def _prepare_tool_call(
        self,
        state: AgentState,
        call: PlannedToolCall,
    ) -> tuple[PlannedToolCall, list[int]]:
        arguments = dict(call.arguments)
        planned = planned_task(state, call.subquery)
        routed_query = (
            planned.arguments.get("query")
            if planned is not None
            else _routed_query_for_call(state, call)
        )
        task = next(
            (
                item
                for item in tool_planning_subqueries(state.get("route_plan"))
                if item.id == call.subquery.strip()
            ),
            None,
        )
        if routed_query is not None:
            arguments["query"] = routed_query
        effective_message = routed_query or state["message"]

        if call.name == "order_lookup":
            if planned is None:
                arguments.setdefault("query", effective_message)
                bound_order_id = bound_task_order_id(state, task) if task is not None else None
                arguments["order_id"] = bound_order_id or _resolve_order_id(
                    effective_message,
                    arguments.get("order_id"),
                    state.get("working_memory_snapshot", {}),
                    self.memory_service,
                )

        elif call.name in {"policy_search", "knowledge_search"}:
            arguments.setdefault("query", effective_message)

        if planned is not None:
            arguments = bind_arguments(state, planned, arguments)
        contract = self.contract_provider.get_contract(call.name)
        if contract is None:
            return call.model_copy(
                update={
                    "arguments": arguments,
                    "canonical_query": routed_query or call.canonical_query,
                    "tool_query": str(arguments.get("query") or call.tool_query),
                }
            ), []
        public_input = contract.public_input_model.model_validate(arguments)
        public_arguments = public_input.model_dump(
            mode="json", exclude_none=True, exclude_unset=True
        )
        return (
            call.model_copy(
                update={
                    "arguments": public_arguments,
                    "canonical_query": routed_query or call.canonical_query,
                    "tool_query": str(arguments.get("query") or call.tool_query),
                }
            ),
            [],
        )

    async def _finalize_response(self, state: AgentState) -> AgentState:
        answer_context = build_answer_context(state)
        if state.get("terminal_guard_status") == "rejected":
            decision = _terminal_fallback_decision(
                state, state.get("terminal_guard_feedback", "invalid_final_output"),
            )
            state["terminal_guard_status"] = "fallback"
            state["intent"] = _tag_from_decision(decision, state.get("intent"))
            state["boundary"] = _boundary_from_route_plan(
                RequestRoutePlan.model_validate(state["route_plan"])
            )
        else:
            decision = OrchestratorDecision.model_validate(state["decision"])
        decision = _apply_handoff_outcome_policy(
            decision, answer_context,
            observation_coverage_failure_allowed=_order_observation_coverage_check_allowed(state),
        )
        state["decision"] = decision.model_dump(mode="json")
        if state.get("terminal_guard_status") == "accepted":
            used_ids = set(decision.used_tool_call_ids)
            for entry in state.get("subquery_ledger", []):
                if str(entry.get("tool_call_id") or "") in used_ids:
                    entry["status"] = "answered"
        has_blocked_handoff = any(
            item.disposition == "human_handoff"
            for item in blocked_subqueries(state.get("route_plan"))
        )
        answer = decision.response.strip() or _fallback_answer(state)
        if decision.offer_handoff_confirmation:
            goals = RequestRoutePlan.model_validate(state["route_plan"]).subqueries
            if decision.control_action == "finish_unavailable" and all(
                g.disposition == "tool_planning" for g in goals
            ):
                answer = f"{answer.rstrip()}\n\n{LATE_HANDOFF_CONFIRMATION}"
            else:
                answer = _append_late_handoff_confirmation(answer)
        if decision.offer_handoff_confirmation or has_blocked_handoff:
            state["boundary"] = BOUNDARY_POLICY.for_classification(
                "human_handoff_required"
            ).model_dump(mode="json")
        state["answer"] = (
            answer if state.get("execution_plan") else _append_blocked_route_notices(answer, state)
        )
        state["suggested_actions"] = _suggest_actions(state)
        return state

    async def _render_handoff_template(self, state: AgentState) -> AgentState:
        boundary = BOUNDARY_POLICY.for_classification("human_handoff_required")
        state["boundary"] = boundary.model_dump(mode="json")
        state["answer"] = _route_terminal_answer(state, "human_handoff")
        state["suggested_actions"] = _suggest_actions(state)
        return state

    async def _render_out_of_scope_template(self, state: AgentState) -> AgentState:
        boundary = BOUNDARY_POLICY.for_classification("out_of_scope")
        state["boundary"] = boundary.model_dump(mode="json")
        state["answer"] = _route_terminal_answer(state, "out_of_scope")
        state["suggested_actions"] = _suggest_actions(state)
        return state

    async def _render_security_template(self, state: AgentState) -> AgentState:
        boundary = BOUNDARY_POLICY.for_classification("security_refusal")
        state["boundary"] = boundary.model_dump(mode="json")
        state["answer"] = _route_terminal_answer(state, "security_refusal")
        state["suggested_actions"] = _suggest_actions(state)
        return state

    async def _render_clarification_template(self, state: AgentState) -> AgentState:
        state["boundary"] = BOUNDARY_POLICY.for_classification("in_scope_auto").model_dump(
            mode="json"
        )
        state["answer"] = _route_terminal_answer(state, "clarification")
        state["suggested_actions"] = []
        return state

    async def _render_direct_template(self, state: AgentState) -> AgentState:
        state["boundary"] = BOUNDARY_POLICY.for_classification("in_scope_auto").model_dump(
            mode="json"
        )
        state["answer"] = await self._generate_routed_answer(
            state,
            system_prompt=GENERAL_DIRECT_RESPONSE_PROMPT,
            disposition="direct_response",
            include_history=False,
        )
        state["route_answer_mode"] = "general_direct"
        state["suggested_actions"] = _suggest_actions(state)
        return state

    async def _render_session_grounded_response(self, state: AgentState) -> AgentState:
        state["boundary"] = BOUNDARY_POLICY.for_classification("in_scope_auto").model_dump(
            mode="json"
        )
        state["answer"] = await self._generate_routed_answer(
            state,
            system_prompt=SESSION_GROUNDED_RESPONSE_PROMPT,
            disposition="session_grounded_response",
            include_history=True,
        )
        state["route_answer_mode"] = "session_grounded"
        state["suggested_actions"] = _suggest_actions(state)
        return state

    async def _generate_routed_answer(
        self,
        state: AgentState,
        *,
        system_prompt: str,
        disposition: str,
        include_history: bool,
    ) -> str:
        fallback = _route_terminal_answer(state, disposition)
        if self.llm is None:
            return fallback

        messages: list[SystemMessage | HumanMessage | AIMessage] = [
            SystemMessage(content=system_prompt)
        ]
        if include_history:
            for item in state.get("history", []):
                content = str(item.get("content") or "").strip()
                if not content:
                    continue
                if item.get("role") == "user":
                    messages.append(HumanMessage(content=content))
                elif item.get("role") == "assistant":
                    messages.append(AIMessage(content=content))
        plan = RequestRoutePlan.model_validate(state["route_plan"])
        routed_queries = [item.query for item in plan.subqueries if item.disposition == disposition]
        query = "；".join(routed_queries) or plan.rewritten_query
        messages.append(HumanMessage(content=query))
        try:
            record_model_call("answer")
            response = await self.llm.ainvoke(messages)
        except Exception:
            return fallback
        if not isinstance(response, AIMessage) or response.tool_calls:
            return fallback
        return _ai_response_text(response).strip() or fallback

    async def _persist_turn(self, state: AgentState) -> AgentState:
        details = build_execution_details(state)
        state["assistant_metadata"] = {
            "execution_details": details.model_dump(mode="json"),
            "intent": state["intent"],
            "route_source": state.get("route_source"),
            "decision": state.get(
                "decision",
                {"type": "request_route_terminal", "route_plan": state.get("route_plan")},
            ),
            "boundary": state["boundary"],
            "route_plan": state.get("route_plan"),
            "subquery_ledger": state.get("subquery_ledger", []),
            "evidence": _dump_evidence(state.get("evidence", [])),
            "products": [product.model_dump(mode="json") for product in state.get("products", [])],
            "order": (state["order"].model_dump(mode="json") if state.get("order") else None),
            "orders": state.get("parsed", {}).get("order_candidates", []),
            "order_query": state.get("parsed", {}).get("order_query"),
        }
        outcome = {
            key: value
            for key, value in state.items()
            if key not in {
                "prepared_turn", "working_memory", "working_memory_snapshot",
                "planning_deadline_monotonic",  # Ephemeral event-loop clock, not replay data.
                "answer_deadline_monotonic",
            }
        }
        changes = await self.context_service.complete_turn(state["prepared_turn"], outcome)
        await trace_event(
            "turn_persisted", conversation_id=state.get("conversation_id"),
            agent_run_id=state.get("run_id"),
            decision_type=state.get("decision", {}).get("type"),
            business_success="not_assessed",
        )
        state["working_memory"] = changes.working_memory.model_dump(mode="json")
        if changes.memory_changes:
            state["memory_changes"] = [
                item.model_dump(mode="json") for item in changes.memory_changes
            ]
        return state

    def _fallback_planner_decision(self, state: AgentState) -> OrchestratorDecision:
        return fallback_planner_decision(self, state)

    def _fallback_routed_tool_decision(
        self,
        state: AgentState,
        subqueries: list[RoutedTask],
    ) -> OrchestratorDecision:
        return fallback_routed_tool_decision(self, state, subqueries)

    async def _mark_run_failed(self, state: AgentState, error_type: str, message: str) -> None:
        if not state.get("run_id"):
            return
        prepared = state.get("prepared_turn")
        if prepared is not None:
            audit = serialize_compact_audit(
                {
                    key: value
                    for key, value in state.items()
                    if key not in {
                        "prepared_turn", "working_memory", "working_memory_snapshot",
                        "planning_deadline_monotonic",
                        "answer_deadline_monotonic",
                    }
                },
                estimated_token_count=prepared.estimated_token_count,
                retained_turns=prepared.retained_turns,
                dropped_turns=prepared.dropped_turns,
                applied_memory_ids=state.get("applied_memory_ids", []),
            )
        else:
            audit = {
                key: value
                for key, value in _json_safe_state(state).items()
                if key
                not in {
                    "history",
                    "memory",
                    "working_memory",
                    "working_memory_snapshot",
                    "prepared_turn",
                    "planning_deadline_monotonic",
                    "answer_deadline_monotonic",
                }
            }
        repo = ConversationRepository(self.session)
        await repo.fail_run(
            state["run_id"],
            state.get("intent"),
            audit,
            {"type": error_type, "message": message},
        )
        await self.session.commit()


    def _response_from_state(self, state: AgentState) -> ChatResponse:
        return ChatResponse(
            conversation_id=state["conversation_id"],
            answer=state["answer"],
            intent=state.get("intent", "general"),
            boundary=BoundaryClassification(**state["boundary"]),
            evidence=state.get("evidence", []),
            products=state.get("products", []),
            order=state.get("order"),
            orders=state.get("parsed", {}).get("order_candidates", []),
            order_query=state.get("parsed", {}).get("order_query"),
            suggested_actions=[
                SuggestedAction(**item) for item in state.get("suggested_actions", [])
            ],
            memory_changes=state.get("memory_changes"),
            execution_details=state.get("assistant_metadata", {}).get("execution_details"),
        )


def _ai_response_text(message: AIMessage) -> str:
    if isinstance(message.content, str):
        return message.content
    if not isinstance(message.content, list):
        return ""
    parts: list[str] = []
    for item in message.content:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict) and isinstance(item.get("text"), str):
            parts.append(item["text"])
    return "".join(parts)
