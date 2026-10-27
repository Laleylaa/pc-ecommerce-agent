"""Strands loop with atomic initial proposals and program-owned tool/evidence state."""

import asyncio
import json

from pydantic import ValidationError
from strands import Agent
from strands.hooks import AfterToolsEvent, BeforeToolsEvent
from strands.tools.executors import SequentialToolExecutor

from app.agent.loop.control_tools import control_tools
from app.agent.loop.evidence import EvidenceRecord
from app.agent.loop.prompts.query_loop import QUERY_PROMPT
from app.agent.loop.query_context import (
    query_context,
    resolve_references,
    validate_private_access,
)
from app.agent.loop.query_schema import (
    CompletionCall,
    GoalRegistration,
    RegistrationCall,
)
from app.agent.loop.query_state import GoalBook
from app.agent.loop.strands_tools import RegisteredBusinessTools, StrandsToolBindings
from app.agent.loop.tool_adapter import AdmissionError, QueryToolAdapter
from app.core.loop_budget import LoopBudget, LoopLimit, find_loop_limit
from app.core.loop_models import QueryModel
from app.schemas.context import PreparedTurn
from app.tools.contracts import ToolCatalog, ToolRuntimeContext
from app.tools.errors import error_payload


def project_record(
    record: EvidenceRecord, limit: int = 16000, *, destination: str = "query",
) -> dict:
    """Bound model observations without re-labelling an omitted collection as empty."""
    observation = record.observation()
    original_chars = len(json.dumps(observation, ensure_ascii=False))
    if original_chars <= limit:
        projected = {**observation, "projection_truncated": False}
        return projected
    # Tool results are bounded by their own contracts. First remove only the
    # duplicated invocation arguments; domain facts must never be whitelisted away.
    observation["invocation"]["arguments"] = {}
    observation["invocation_arguments_omitted"] = True
    observation["projection_truncated"] = True
    if len(json.dumps(observation, ensure_ascii=False)) > limit:
        observation["result"] = {
            "tool_name": record.result.tool_name, "ok": False, "output": None,
            "error": error_payload("observation_budget_exceeded"),
        }
        observation["completion_blockers"] = ["observation_budget_exceeded"]
    return observation


class QuerySession:
    def __init__(self, prepared: PreparedTurn, catalog: ToolCatalog, budget: LoopBudget):
        self.prepared, self.budget = prepared, budget
        self.adapter = QueryToolAdapter(
            catalog,
            ToolRuntimeContext(user_id=prepared.user_id),
            admit_goal=lambda goal_id: self.book.admit(goal_id),
            before_attempt=budget.before_tool,
            attempt_scope=budget.tool_scope,
        )
        self.book = GoalBook(prepared.message, self.adapter.evidence)
        self.bindings = StrandsToolBindings(self.adapter, observation=self.observation)
        self.completion_attempts = 0
        self.stop_reason = "missing_completion"
        self._operations: dict[str, str] = {}
        self.rejections: list[str] = []
        self.control_rejections: list[dict[str, str]] = []
        self.query_batches: list[dict] = []

    def control_state(self) -> dict:
        """Execution facts for deciding whether to query again, not a form to copy back."""
        return {
            "query_state": [
                {
                    "goal_id": goal.id,
                    "execution": self.book.execution(goal),
                    "prerequisites_met": self.book.prerequisites_met(goal),
                }
                for goal in self.book.goals.values()
            ],
        }

    def observation(self, record: EvidenceRecord) -> dict:
        return {**project_record(record), **self.control_state()}

    def visible_tools(self, specs: list[dict]) -> list[dict]:
        # Visibility saves input tokens; the existing admission checks remain authoritative.
        names = {"register_goals"}
        if not self.book.goals:
            # A first response may propose registration + queries, or non-query completion.
            names.add("query_complete")
            names.update(c.llm_name for c in self.adapter.catalog.list_contracts() if c.read_only)
        else:
            names.add("query_complete")
            for goal in self.book.goals.values():
                try:
                    self.book.admit(goal.id)
                    names.update(
                        c.llm_name for c in self.adapter.catalog.list_contracts() if c.read_only
                    )
                    break
                except AdmissionError:
                    pass
        return [spec for spec in specs if spec["name"] in names]

    def reject_control(self, name: str, exc: Exception, raw=None) -> dict:
        code = str(exc) if isinstance(exc, AdmissionError) else "invalid_control_input"
        self.control_rejections.append({"tool": name, "code": code})
        return {
            "status": "error",
            "content": [{"json": {"rejection": code, **self.control_state()}}],
        }

    def _prepare(self, use: dict) -> str:
        call = self.bindings.decode(use)
        goal_id = call.goal_id
        self.book.admit(goal_id)
        validate_private_access(
            self.prepared, self.book, self.book.goals[goal_id], use["name"], call.arguments,
        )
        operation_id = self.adapter.prepare(
            goal_id,
            use["name"],
            call.arguments,
        )
        operation = self.adapter.operation(operation_id)
        fingerprint = json.dumps(
            {
                "goal": goal_id,
                "tool": use["name"],
                "arguments": operation.arguments,
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        previous = self._operations.get(fingerprint)
        if previous:
            self.adapter.discard_prepared(operation_id)
            attempts = self.adapter.attempts(previous)
            if not attempts:
                raise AdmissionError("duplicate_pending_query")
            last = attempts[-1].result
            if last.ok or not last.error or last.error.next_action != "retry" or len(attempts) >= 2:
                raise AdmissionError("duplicate_query")
            return previous
        self._operations[fingerprint] = operation_id
        return operation_id

    def _register(self, registration: GoalRegistration, book: GoalBook) -> dict:
        references = {
            goal.id: resolve_references(self.prepared, goal)
            for goal in registration.goals
        }
        book.register(registration)
        return references

    @staticmethod
    def _control_input(use: dict) -> RegistrationCall | CompletionCall:
        schema = RegistrationCall if use["name"] == "register_goals" else CompletionCall
        try:
            return schema.model_validate(use["input"])
        except ValidationError as exc:
            raise AdmissionError("invalid_control_input") from exc

    def _prepare_initial_batch(self, uses: list[dict]) -> list[tuple[str, str]]:
        """Validate against temporary goals; only SDK execution may commit registration."""
        if self.book.goals or uses[0]["name"] != "register_goals":
            raise AdmissionError("control_batch_must_be_exclusive")
        registration = self._control_input(uses[0]).registration
        original = self.book
        staged = GoalBook(self.prepared.message, self.adapter.evidence)
        self._register(registration, staged)
        self.book = staged
        try:
            if len(uses) == 2 and uses[1]["name"] == "query_complete":
                staged.finish(self._control_input(uses[1]).completion)
                return []
            if any(
                use["name"] in {"register_goals", "query_complete"}
                for use in uses[1:]
            ):
                raise AdmissionError("control_batch_must_be_exclusive")
            return [(use["toolUseId"], self._prepare(use)) for use in uses[1:]]
        finally:
            self.book = original

    def build(self, model) -> Agent:
        controls = {"register_goals", "query_complete"}

        def before(event: BeforeToolsEvent) -> None:
            uses = [block["toolUse"] for block in event.message["content"] if "toolUse" in block]
            known = controls | {c.llm_name for c in self.adapter.catalog.list_contracts()}
            self.query_batches.append({
                "tools": [use["name"] if use["name"] in known else "unknown" for use in uses],
                "bindings": [],
            })
            has_completion = any(use["name"] == "query_complete" for use in uses)
            if has_completion:
                self.completion_attempts += 1
            previous_operations = dict(self._operations)
            try:
                self.budget.check()
                if len({use["toolUseId"] for use in uses}) != len(uses):
                    raise AdmissionError("duplicate_tool_use_id")
                has_control = any(use["name"] in controls for use in uses)
                if has_control and len(uses) == 1:
                    self._control_input(uses[0])
                    return
                # Validate the entire batch before binding any executable call.
                with self.adapter.preparing_batch():
                    pending = (
                        self._prepare_initial_batch(uses) if has_control
                        else [(use["toolUseId"], self._prepare(use)) for use in uses]
                    )
                    for tool_use_id, operation_id in pending:
                        self.bindings.validate_binding(tool_use_id, operation_id)
                    for tool_use_id, operation_id in pending:
                        self.bindings.bind(tool_use_id, operation_id)
                    self.query_batches[-1]["bindings"] = [
                        {
                            "tool_use_id": tool_use_id,
                            "operation_id": operation_id,
                            "goal_id": self.adapter.operation(operation_id).goal_id,
                        }
                        for tool_use_id, operation_id in pending
                    ]
            except (AdmissionError, LoopLimit, ValueError, TypeError) as exc:
                code = str(exc) if isinstance(exc, (AdmissionError, LoopLimit)) else "invalid_call"
                self._operations = previous_operations
                self.rejections.append(code)
                feedback = exc.feedback if isinstance(exc, AdmissionError) else None
                event.cancel = f"{code}: {feedback}" if feedback else code

        def after(event: AfterToolsEvent) -> None:
            if self.bindings.limit_reached:
                self.stop_reason = self.bindings.limit_reached
                event.end_turn = True
            elif self.book.completion is not None:
                event.end_turn = True
            elif self.completion_attempts >= 2:
                self.stop_reason = "invalid_completion"
                event.end_turn = True

        tools = [*self.bindings.tools(), *control_tools(self)]
        names = [t.tool_name.replace("-", "_") for t in tools]
        if len(set(names)) != len(names):
            raise ValueError("duplicate SDK tool name")
        agent = Agent(
            model=model,
            tools=tools,
            system_prompt=QUERY_PROMPT,
            callback_handler=None,
            hooks=[before, after],
            tool_executor=SequentialToolExecutor(),
            retry_strategy=None,
            context_manager=False,
        )

        # Admission, execution and discovery now resolve from the SDK's real registry.
        self.adapter.catalog = RegisteredBusinessTools(agent.tool_registry)
        return agent

    async def run(self, model) -> None:
        if isinstance(model, QueryModel):
            model.tool_spec_filter = self.visible_tools
        try:
            async with asyncio.timeout_at(self.budget.query_deadline):
                await self.build(model).invoke_async(
                    json.dumps(
                        query_context(self.prepared),
                        ensure_ascii=False,
                    )
                )
        except asyncio.CancelledError:
            raise
        except LoopLimit as exc:
            self.stop_reason = str(exc)
        except TimeoutError:
            self.stop_reason = "query_deadline"
        except Exception as exc:
            limit = find_loop_limit(exc)
            self.stop_reason = str(limit) if limit else "query_model_error"
        finally:
            if isinstance(model, QueryModel):
                model.tool_spec_filter = None
        # SDK free text, including a natural end_turn, is deliberately discarded.
