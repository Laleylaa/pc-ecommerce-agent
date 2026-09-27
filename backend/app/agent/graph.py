import asyncio
from collections.abc import AsyncIterator
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.config import get_stream_writer
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.answer_context import (
    answerable_source_tool_call_ids,
    build_answer_context,
    resolved_answer_task_ids,
)
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
    _context_event,
    _dump_evidence,
    _json_safe_state,
    _stream_event,
)
from app.agent.execution import execute_tool_wave
from app.agent.fallback_planner import (
    fallback_planner_decision,
    fallback_routed_tool_decision,
)
from app.agent.limits import (
    MAX_ORCHESTRATOR_CALLS,
    MAX_REQUEST_ROUTER_CALLS,
    MAX_TOOL_WAVES,
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
from app.agent.responses import (
    LATE_HANDOFF_CONFIRMATION,
    _append_blocked_route_notices,
    _append_late_handoff_confirmation,
    _fallback_answer,
    _fallback_unavailable_answer,
    _route_terminal_answer,
    _suggest_actions,
)
from app.agent.responses import (
    _has_successful_tool_result as _has_successful_tool_result,
)
from app.agent.route_runtime import (
    _boundary_from_route_plan,
    _context_retry_feedback,
    _deterministic_pre_route_plan,
    _enforce_route_boundaries,
    _fallback_rewritten_query,
    _fallback_route_disposition,
    _ground_candidate_comparison,
    _intent_from_route_plan,
    _resolve_order_id,
    _reuse_comparison_context,
    _routed_query_for_call,
    _session_grounded_route_allowed,
    _split_request_subqueries,
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
    request_route_tool_definition,
    route_plan_from_ai_message,
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
from app.repositories.conversations import ConversationRepository
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
    ):
        self.session = session
        self.settings = settings
        self.contract_provider = contract_provider or DefaultToolContractProvider()
        self.tool_executor = tool_executor or RegistryToolExecutor(session, settings)
        self.context_service = context_service or ConversationContextService(session, settings)
        self.memory_service = memory_service or MemoryService()
        self.llm = (
            chat_model
            if chat_model is not None
            else build_chat_model(settings, temperature=0.2)
        )
        tool_planner_base_model = (
            chat_model
            if chat_model is not None
            else build_chat_model(settings, temperature=0.0)
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
            router_base_model = build_chat_model(settings, temperature=0.0)
        self.request_router = (
            router_base_model.bind_tools([request_route_tool_definition()])
            if router_base_model
            else None
        )
        self.tool_planner = (
            tool_planner_base_model.bind_tools(business_tools)
            if tool_planner_base_model
            else None
        )
        self.answer_synthesizer = (
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
        result: AgentState = await self._build_graph().ainvoke(
            self._initial_state(request, user_id)
        )
        return self._response_from_state(result)

    async def run_stream(self, request: ChatRequest, user_id: int) -> AsyncIterator[dict[str, Any]]:
        state = self._initial_state(request, user_id)
        try:
            async for mode, update in self._build_graph().astream(
                state,
                stream_mode=["custom", "updates"],
            ):
                if mode == "custom":
                    event_kind = update.get("kind")
                    if event_kind == "tool_call":
                        yield _stream_event(
                            "tool_call",
                            state,
                            tool_name=update["tool_name"],
                            status=update["status"],
                            input=update.get("input"),
                            output=update.get("output"),
                        )
                    continue

                for node_name, node_state in update.items():
                    state.update(node_state)
                    if node_name == "load_context":
                        yield _stream_event("run_started", state)
                    elif node_name == "request_router":
                        yield _stream_event("boundary", state, boundary=state["boundary"])
                    elif node_name == "execute_tool_wave":
                        yield _context_event(state)
                    elif node_name in {
                        "finalize_response",
                        "render_handoff_template",
                        "render_out_of_scope_template",
                        "render_security_template",
                        "render_clarification_template",
                        "render_direct_template",
                        "render_session_grounded_response",
                    }:
                        yield _stream_event("delta", state, delta=state["answer"])
                    elif node_name == "persist_turn":
                        yield _stream_event(
                            "done",
                            state,
                            response=self._response_from_state(state).model_dump(mode="json"),
                        )
        except asyncio.CancelledError:
            await self._mark_run_failed(state, "cancelled", "client disconnected")
            raise
        except Exception as exc:
            await self._mark_run_failed(state, type(exc).__name__, str(exc))
            yield _stream_event(
                "error",
                state,
                error_type=type(exc).__name__,
                message="AI 回复生成失败，请稍后重试。",
                retryable=True,
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
            "terminal_guard_replan_count": 0,
            "parsed": {},
            "products": [],
            "evidence": [],
            "order": None,
        }

    def _build_graph(self):
        return build_agent_graph(self)

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
        return state

    async def _request_route(self, state: AgentState) -> AgentState:
        call_count = state.get("request_router_call_count", 0)
        plan = _deterministic_pre_route_plan(state["message"])
        route_source = "deterministic_fast_path"
        if (
            plan is None
            and self.request_router is not None
            and call_count < MAX_REQUEST_ROUTER_CALLS
        ):
            call_count += 1
            try:
                message = await self.request_router.ainvoke(_request_router_messages(state))
                if not isinstance(message, AIMessage):
                    raise TypeError("request router returned a non-AI message")
                plan = route_plan_from_ai_message(message)
                route_source = "request_router_llm"
            except (ValidationError, ValueError, TypeError):
                plan = self._fallback_route_plan(state)
                route_source = "request_router_fallback"
            except Exception:
                plan = self._fallback_route_plan(state)
                route_source = "request_router_fallback"
        elif plan is None:
            plan = self._fallback_route_plan(state)
            route_source = "deterministic_fallback"

        retry_feedback = (
            _context_retry_feedback(
                plan,
                state.get("working_memory_snapshot", {}),
            )
            if route_source == "request_router_llm"
            else None
        )
        if (
            retry_feedback is not None
            and self.request_router is not None
            and call_count < MAX_REQUEST_ROUTER_CALLS
        ):
            original_plan = plan
            call_count += 1
            try:
                message = await self.request_router.ainvoke(
                    _request_router_messages(
                        state,
                        validation_feedback=retry_feedback,
                    )
                )
                if not isinstance(message, AIMessage):
                    raise TypeError("request router returned a non-AI message")
                plan = route_plan_from_ai_message(message)
                route_source = "request_router_llm_context_retry"
            except Exception:
                plan = original_plan

        plan = _enforce_route_boundaries(plan, original_message=state["message"])
        if not _session_grounded_route_allowed(
            plan,
            state.get("history", []),
            original_message=state["message"],
        ):
            plan = self._fallback_route_plan(state)
            route_source = "session_grounding_veto_fallback"
        plan = _reuse_comparison_context(plan, state.get("working_memory_snapshot", {}))
        plan = _ground_candidate_comparison(
            plan,
            state.get("working_memory_snapshot", {}),
            state["message"],
        )
        state["request_router_call_count"] = call_count
        state["route_source"] = route_source
        state["route_plan"] = plan.model_dump(mode="json")
        state["rewritten_query"] = plan.rewritten_query
        state["planned_subqueries"] = [
            item.model_dump(mode="json") for item in tool_planning_subqueries(plan)
        ]
        state["blocked_subqueries"] = [
            item.model_dump(mode="json") for item in blocked_subqueries(plan)
        ]
        state["boundary"] = _boundary_from_route_plan(plan)
        state["intent"] = _intent_from_route_plan(plan)
        initialize_task_runtime(state)
        return state

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
        plan = RequestRoutePlan.model_validate(state["route_plan"])
        if tool_planning_subqueries(plan):
            return "plan"
        dispositions = {item.disposition for item in plan.subqueries}
        for disposition in (
            "security_refusal",
            "human_handoff",
            "clarification",
            "out_of_scope",
            "direct_response",
            "session_grounded_response",
        ):
            if disposition in dispositions:
                return disposition
        return "clarification"

    async def _orchestrate(self, state: AgentState) -> AgentState:
        previous_call_count = state.get("orchestrator_call_count", 0)
        has_ready_tasks = bool(_ready_unattempted_tool_subqueries(state))
        recovery_decision = None if has_ready_tasks else _deterministic_recovery_decision(state)
        capability_decision = decision_from_route_capabilities(
            RequestRoutePlan.model_validate(state["route_plan"]), state
        )
        capability_decision = capability_decision or recovery_decision

        if capability_decision is not None:
            call_count = previous_call_count
            decision = capability_decision
        else:
            call_count = previous_call_count + 1

        if capability_decision is not None:
            pass
        elif call_count > MAX_ORCHESTRATOR_CALLS:
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
        state["orchestrator_call_count"] = call_count
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
        attempts = 2 if answer_phase else 1
        for attempt in range(attempts):
            prompt_state = state
            if attempt:
                prompt_state = {
                    **state,
                    "terminal_guard_feedback": (
                        "Answer Synthesizer 上一输出不合法。已有 Artifact 时必须调用一个"
                        "已绑定终止控制 Tool；不得再次调用业务 Tool。"
                    ),
                }
            try:
                message = await model.ainvoke(_orchestrator_messages(prompt_state, call_count))
                if not isinstance(message, AIMessage):
                    raise TypeError("orchestrator returned a non-AI message")
                try:
                    decision = decision_from_ai_message(message)
                except ValueError:
                    recovered = _plain_text_observation_decision(state, message)
                    if recovered is not None:
                        return recovered
                    raise
                if answer_phase and decision.type == "tool_calls":
                    raise ValueError("answer synthesizer returned a business tool call")
                return decision
            except (ValidationError, ValueError, TypeError):
                if attempt + 1 >= attempts:
                    raise
        raise RuntimeError("unreachable observation retry state")

    def _orchestrator_model_for_state(self, state: AgentState) -> Any:
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
        if state.get("tool_wave_count", 0) >= MAX_TOOL_WAVES or (
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
        refresh_task_status(state)
        rebuild_tool_projections(state)
        return state

    async def _terminal_guard(self, state: AgentState) -> AgentState:
        decision = OrchestratorDecision.model_validate(state["decision"])
        answer_context = build_answer_context(state)
        has_unsupported_capability = bool(
            answer_context["outcome_counts"].get("unsupported_capability")
        )
        observation_coverage_failure_allowed = (
            decision.observation_coverage_failure
            and _order_observation_coverage_check_allowed(state)
        )
        decision = _apply_handoff_outcome_policy(
            decision,
            answer_context,
            observation_coverage_failure_allowed=observation_coverage_failure_allowed,
        )
        state["decision"] = decision.model_dump(mode="json")
        route_boundary = _boundary_from_route_plan(
            RequestRoutePlan.model_validate(state["route_plan"])
        )
        current_boundary = str(state.get("boundary", {}).get("classification") or "")
        boundary_consistent = (
            not current_boundary
            or current_boundary == str(route_boundary.get("classification") or "")
        )
        validation = validate_terminal_decision(
            decision,
            state.get("subquery_ledger", []),
            planned_subquery_ids=[item.id for item in user_facing_tasks(state["route_plan"])],
            clarification_allowed=bool(user_clarifiable_blockers(state)),
            resolved_task_ids=resolved_answer_task_ids(state),
            usable_artifact_tool_call_ids=[
                str(artifact.get("source_tool_call_id"))
                for artifact in state.get("task_artifacts", {}).values()
                if artifact.get("usable") and artifact.get("source_tool_call_id")
            ],
            answerable_tool_call_ids=answerable_source_tool_call_ids(state),
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
            if decision.control_action in {"finish_answer", "finish_partial"}:
                used_ids = set(decision.used_tool_call_ids)
                for entry in state.get("subquery_ledger", []):
                    if str(entry.get("tool_call_id") or "") in used_ids:
                        entry["status"] = "answered"
            state["terminal_guard_status"] = "accepted"
            state.pop("terminal_guard_feedback", None)
            return state

        if answer_context["answerable_source_tool_call_ids"]:
            fallback = _terminal_fallback_decision(state, validation.reason)
            fallback = _apply_handoff_outcome_policy(fallback, answer_context)
            state["decision"] = fallback.model_dump(mode="json")
            state["intent"] = _tag_from_decision(fallback, state.get("intent"))
            state["boundary"] = _boundary_from_route_plan(
                RequestRoutePlan.model_validate(state["route_plan"])
            )
            state["terminal_guard_status"] = "fallback"
            return state

        can_replan = (
            state.get("terminal_guard_replan_count", 0) < 1
            and state.get("orchestrator_call_count", 0) < MAX_ORCHESTRATOR_CALLS
            and self._orchestrator_model_for_state(state) is not None
        )
        if can_replan:
            state["terminal_guard_replan_count"] = state.get("terminal_guard_replan_count", 0) + 1
            state["terminal_guard_feedback"] = validation.reason
            state["terminal_guard_status"] = "replan"
            return state

        fallback = _terminal_fallback_decision(state, validation.reason)
        fallback = _apply_handoff_outcome_policy(fallback, answer_context)
        state["decision"] = fallback.model_dump(mode="json")
        state["intent"] = _tag_from_decision(fallback, state.get("intent"))
        state["boundary"] = _boundary_from_route_plan(
            RequestRoutePlan.model_validate(state["route_plan"])
        )
        state["terminal_guard_status"] = "fallback"
        return state

    def _dispatch_terminal_guard(self, state: AgentState) -> str:
        if state.get("terminal_guard_status") == "replan":
            return "replan"
        return "respond"

    def _prepare_tool_call(
        self,
        state: AgentState,
        call: PlannedToolCall,
    ) -> tuple[PlannedToolCall, list[int]]:
        arguments = dict(call.arguments)
        routed_query = _routed_query_for_call(state, call)
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
            arguments.setdefault("query", effective_message)
            bound_order_id = bound_task_order_id(state, task) if task is not None else None
            arguments["order_id"] = bound_order_id or _resolve_order_id(
                effective_message,
                arguments.get("order_id"),
                state.get("working_memory_snapshot", {}),
                self.memory_service,
            )
            if arguments["order_id"] is None:
                # Order window semantics belong to the query-aware tool. A planner-supplied
                # limit must not silently turn "recent orders" into one order.
                arguments.pop("limit", None)
                arguments.pop("offset", None)
            if any(
                marker in effective_message
                for marker in (
                    "下一页",
                    "继续列",
                    "继续看",
                    "剩下",
                    "剩余订单",
                    "其他订单",
                    "还有订单",
                    "还有别的",
                )
            ):
                order_memory = state.get("working_memory_snapshot", {}).get("order", {})
                if isinstance(order_memory, dict):
                    next_offset = order_memory.get("next_offset")
                    if isinstance(next_offset, int):
                        arguments["offset"] = next_offset

        elif call.name in {"policy_search", "knowledge_search"}:
            arguments.setdefault("query", effective_message)
            arguments["limit"] = 3

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
        public_arguments = public_input.model_dump(mode="json", exclude_none=True)
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
        decision = OrchestratorDecision.model_validate(state["decision"])
        has_blocked_handoff = any(
            item.disposition == "human_handoff"
            for item in blocked_subqueries(state.get("route_plan"))
        )
        answer = decision.response.strip() or _fallback_answer(state)
        if (
            decision.control_action == "finish_unavailable"
            and decision.offer_handoff_confirmation
        ):
            answer = LATE_HANDOFF_CONFIRMATION
        elif decision.control_action == "finish_unavailable":
            answer = _fallback_unavailable_answer(state)
        elif decision.offer_handoff_confirmation:
            answer = _append_late_handoff_confirmation(answer)
        if decision.offer_handoff_confirmation or has_blocked_handoff:
            state["boundary"] = BOUNDARY_POLICY.for_classification(
                "human_handoff_required"
            ).model_dump(mode="json")
        state["answer"] = _append_blocked_route_notices(answer, state)
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
            response = await self.llm.ainvoke(messages)
        except Exception:
            return fallback
        if not isinstance(response, AIMessage) or response.tool_calls:
            return fallback
        return _ai_response_text(response).strip() or fallback

    async def _persist_turn(self, state: AgentState) -> AgentState:
        state["assistant_metadata"] = {
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
            if key not in {"prepared_turn", "working_memory", "working_memory_snapshot"}
        }
        changes = await self.context_service.complete_turn(state["prepared_turn"], outcome)
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
                    if key not in {"prepared_turn", "working_memory", "working_memory_snapshot"}
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

    async def _mark_stream_failed(self, state: AgentState, error_type: str, message: str) -> None:
        """Compatibility entry point for callers that finalize failed SSE runs."""
        await self._mark_run_failed(state, error_type, message)

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
