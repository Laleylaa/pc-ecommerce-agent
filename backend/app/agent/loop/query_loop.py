"""Strands loop with atomic initial proposals and program-owned tool/evidence state."""

import asyncio
import json
from copy import deepcopy

from pydantic import ValidationError
from strands import Agent, tool
from strands.hooks import AfterToolsEvent, BeforeToolsEvent
from strands.tools.executors import SequentialToolExecutor

from app.agent.loop.evidence import EvidenceRecord
from app.agent.loop.prompts.query_loop import QUERY_PROMPT
from app.agent.loop.query_context import (
    catalog_context,
    query_context,
    resolve_references,
    validate_identities,
)
from app.agent.loop.query_schema import (
    CompletionCall,
    GoalRegistration,
    QueryCompletion,
    RegistrationCall,
)
from app.agent.loop.query_state import GoalBook
from app.agent.loop.strands_tools import StrandsToolBindings
from app.agent.loop.tool_adapter import AdmissionError, QueryToolAdapter
from app.core.loop_budget import LoopBudget, LoopLimit, find_loop_limit
from app.core.loop_models import QueryModel
from app.core.loop_tracing import program_event, rejection, tracing_active
from app.schemas.context import PreparedTurn
from app.tools.contracts import ToolCatalog, ToolRuntimeContext


def project_record(
    record: EvidenceRecord, limit: int = 16000, *, destination: str = "query",
) -> dict:
    """Bound model observations without re-labelling an omitted collection as empty."""
    observation = record.observation()
    original_chars = len(json.dumps(observation, ensure_ascii=False))
    if original_chars <= limit:
        projected = {**observation, "projection_truncated": False}
        _trace_projection(record, observation, projected, original_chars, limit, destination)
        return projected
    output = observation["result"].get("output") or {}
    metadata = {
        key: deepcopy(value)
        for key, value in output.items()
        if key
        in {
            "status",
            "reason_code",
            "recommended_action",
            "result_type",
            "match_kind",
            "query_state",
            "scope",
            "total_match_count",
            "returned_count",
            "is_exhaustive",
            "matching_complete",
            "limit",
            "offset",
            "next_offset",
            "data_quality",
            "candidate_count",
            "unknown_sku_count",
            "requested_count",
            "display_cap",
            "limit_capped",
            "identity_suggestions",
        }
    }
    metadata["omitted_fields"] = sorted(set(output) - set(metadata))
    observation["result"]["output"] = metadata
    observation["projection_truncated"] = True
    observation["completion_blockers"].append("projection_truncated")
    _trace_projection(record, {"result": {"output": output}}, observation,
                      original_chars, limit, destination)
    return observation


def _trace_projection(record, original, projected, original_chars, limit, destination):
    if not tracing_active():
        return
    source = original["result"].get("output") or {}
    target = projected["result"].get("output") or {}
    program_event("材料投影", outcome="truncated" if projected["projection_truncated"] else "kept",
                  destination=destination, evidence_id=record.evidence_id,
                  invocation_id=record.invocation.invocation_id,
                  operation_id=record.invocation.operation_id,
                  original_chars=original_chars,
                  projected_chars=len(json.dumps(projected, ensure_ascii=False)), limit_chars=limit,
                  omitted_fields=target.get("omitted_fields", []),
                  source_product_count=len(source.get("products", [])),
                  retained_product_count=len(target.get("products", [])))


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
        rejection(raw, str(exc), {"rejection": code, **self.control_state()})
        return {
            "status": "error",
            "content": [{"json": {"rejection": code, **self.control_state()}}],
        }

    def _prepare(self, use: dict) -> str:
        call = self.bindings.decode(use)
        goal_id = call.goal_id
        self.book.admit(goal_id)
        goal = self.book.goals[goal_id]
        validate_identities(self.prepared, self.book, goal, use["name"], call.arguments)
        context = catalog_context(self.prepared, self.book, goal)
        operation_id = self.adapter.prepare(
            goal_id,
            use["name"],
            call.arguments,
            catalog_context=context,
        )
        operation = self.adapter.operation(operation_id)
        fingerprint = json.dumps(
            {
                "goal": goal_id,
                "tool": use["name"],
                "arguments": operation.arguments,
                "context": context.model_dump(mode="json"),
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
            if last.ok or not last.error or not last.error.retryable or len(attempts) >= 2:
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
        @tool(inputSchema={"json": RegistrationCall.model_json_schema()})
        def register_goals(registration: GoalRegistration) -> dict:
            """Register all goals, then propose independent queries in this SAME response.

            If no query is needed or possible, follow with query_complete in this SAME
            response. Each business call explicitly names its goal_id; a goal may
            have multiple calls. No activation round trip is needed. Source text is verbatim.
            """
            try:
                registration = GoalRegistration.model_validate(registration)
                references = self._register(registration, self.book)
                return {
                    "accepted": True, "trusted_references": references, **self.control_state(),
                }
            except (AdmissionError, ValidationError) as exc:
                return self.reject_control(
                    "register_goals", exc, registration.model_dump(mode="json")
                    if isinstance(registration, GoalRegistration) else registration,
                )

        @tool(inputSchema={"json": CompletionCall.model_json_schema()})
        def query_complete(completion: QueryCompletion) -> dict:
            """Stop querying and hand every goal and actual result to the answer stage.

            Supply only a short reason, never goal statuses or evidence IDs. This does not
            claim success. Usually standalone; may follow initial registration if no queries
            will be made. Never mix with business queries in the same response.
            """
            try:
                self.book.finish(QueryCompletion.model_validate(completion))
                self.stop_reason = "completed"
                return {"accepted": True}
            except (AdmissionError, ValidationError) as exc:
                return self.reject_control("query_complete", exc, completion.model_dump(mode="json")
                                           if isinstance(completion, QueryCompletion)
                                           else completion)

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
                rejection(uses, str(exc.__cause__ or exc), {"cancel": code})
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

        return Agent(
            model=model,
            tools=[*self.bindings.tools(), register_goals, query_complete],
            system_prompt=QUERY_PROMPT,
            callback_handler=None,
            hooks=[before, after],
            tool_executor=SequentialToolExecutor(),
            retry_strategy=None,
            context_manager=False,
        )

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
