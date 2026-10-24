"""P2 two-stage runtime. Available for integration; the HTTP default is unchanged."""

import asyncio
from collections.abc import Callable
from time import monotonic

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.boundary import boundary_for_classification
from app.agent.loop.independent_answer import answer, handoff
from app.agent.loop.loop_response import response_state
from app.agent.loop.query_loop import QuerySession
from app.agent.result_projection import _handoff_payload
from app.core.config import Settings
from app.core.loop_budget import LoopBudget
from app.core.loop_models import loop_models
from app.core.loop_tracing import fields, traced_turn
from app.core.model_usage import collect_model_calls
from app.rules.chat_input import match_input_rule
from app.schemas.chat import ChatRequest, ChatResponse
from app.services.context import ConversationContextService
from app.tools.catalog import LLMCatalogQueryPlanner
from app.tools.catalog_search.planner import CatalogSearchPlanner
from app.tools.contracts import ToolCatalog, build_tool_catalog
from app.tools.orders import LLMOrderQueryPlanner


class AgentLoopRuntime:
    def __init__(
        self,
        session: AsyncSession | None,
        settings: Settings,
        *,
        context_service=None,
        catalog_factory: Callable[[object], ToolCatalog] | None = None,
        transport=None,
        budget_factory: Callable[[], LoopBudget] = LoopBudget,
    ):
        self.session, self.settings = session, settings
        self.context = context_service or ConversationContextService(session, settings)
        self.catalog_factory = catalog_factory
        self.transport, self.budget_factory = transport, budget_factory

    def _catalog(self, text_model) -> ToolCatalog:
        if self.catalog_factory is not None:
            return self.catalog_factory(text_model)
        if self.session is None:
            raise ValueError("database_session_required")
        factory = async_sessionmaker(bind=self.session.bind, expire_on_commit=False)
        return build_tool_catalog(
            self.session,
            settings=self.settings,
            catalog_planner=LLMCatalogQueryPlanner(text_model),
            order_planner=LLMOrderQueryPlanner(text_model),
            catalog_search_planner=CatalogSearchPlanner(
                text_model if self.settings.catalog_llm_planner_enabled else None,
            ),
            catalog_lookup_planner=CatalogSearchPlanner(
                text_model if self.settings.catalog_llm_planner_enabled else None,
                operation="lookup",
            ),
            catalog_session_factory=factory,
            tool_session_factory=factory,
        )

    @traced_turn
    async def run(self, request: ChatRequest, user_id: int) -> ChatResponse:
        if type(user_id) is not int or user_id <= 0:
            raise ValueError("authentication_required")
        budget = self.budget_factory()
        prepared = None
        usage = None
        timings = {}
        with collect_model_calls():
            try:
                rule = match_input_rule(request.message)
                message = (
                    rule.recorded_message
                    if rule and rule.recorded_message is not None
                    else request.message
                )
                async with asyncio.timeout_at(budget.query_deadline):
                    prepared = await self.context.prepare_turn(
                        user_id, request.conversation_id, message
                    )
                fields(run_id=prepared.run_id, **{"session.id": str(prepared.conversation_id)})
                if prepared.user_id != user_id:
                    raise ValueError("prepared_identity_mismatch")
                timings["prepare_seconds"] = round(
                    asyncio.get_running_loop().time() - budget.started, 3,
                )
                if rule is not None:
                    classification = {
                        "human_handoff": "human_handoff_required",
                        "security_refusal": "security_refusal",
                    }.get(rule.disposition, "in_scope_auto")
                    response = ChatResponse(
                        conversation_id=prepared.conversation_id,
                        answer=rule.message,
                        intent="general",
                        boundary=boundary_for_classification(classification),
                        suggested_actions=(
                            [{"label": "转人工客服", "payload": _handoff_payload(message)}]
                            if rule.disposition == "human_handoff"
                            else []
                        ),
                    )
                    metadata = response.model_dump(mode="json")
                    state = {
                        **metadata,
                        "message": message,
                        "assistant_metadata": metadata,
                        "agent_loop": {
                            "stop_reason": "input_rule",
                            "input_rule": rule.id,
                            "model_calls": {},
                        },
                    }
                else:
                    async with loop_models(
                        self.settings,
                        budget,
                        transport=self.transport,
                    ) as (query_model, text_model, usage):
                        query = QuerySession(prepared, self._catalog(text_model), budget)
                        started = monotonic()
                        await query.run(query_model)
                        timings["query_seconds"] = round(monotonic() - started, 3)
                        facts = handoff(query)
                        started = monotonic()
                        draft, answer_mode = await answer(facts, text_model, budget)
                        timings["answer_seconds"] = round(monotonic() - started, 3)
                        response, state = response_state(
                            query,
                            facts,
                            draft,
                            answer_mode,
                            usage.summary(),
                        )
                fields(stop_reason=state["agent_loop"]["stop_reason"],
                       answer_mode=state["agent_loop"].get("answer_mode", "input_rule"))
                budget.closed = True
                state["agent_loop"]["timings"] = timings
                # The existing service atomically commits answer, memory and audit.
                async with asyncio.timeout_at(
                    min(
                        budget.turn_deadline,
                        asyncio.get_running_loop().time() + budget.cleanup_seconds,
                    )
                ):
                    changes = await self.context.complete_turn(prepared, state)
                response.memory_changes = changes.memory_changes
                return response
            except BaseException as exc:
                budget.closed = True
                # Cleanup must not mask the original failure or turn cancellation into a reply.
                try:
                    async with asyncio.timeout_at(
                        min(
                            budget.turn_deadline,
                            asyncio.get_running_loop().time() + budget.cleanup_seconds,
                        )
                    ):
                        await self.context.fail_turn(
                            prepared,
                            "cancelled"
                            if isinstance(exc, asyncio.CancelledError)
                            else type(exc).__name__,
                            {
                                "model_calls": dict(budget.calls),
                                "tool_attempts": budget.tool_attempts,
                                "usage": usage.summary() if usage else None,
                                "timings": timings,
                            },
                        )
                except (Exception, asyncio.CancelledError):
                    pass
                raise
