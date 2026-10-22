"""SDK boundary: derive domain specs and bind each call to a trusted operation."""

from collections.abc import Callable
from copy import deepcopy
from typing import Any

from pydantic import ValidationError
from strands.tools.tools import PythonAgentTool
from strands.types.tools import ToolResult, ToolUse

from app.agent.loop.evidence import EvidenceRecord
from app.agent.loop.query_schema import GoalToolCall
from app.agent.loop.tool_adapter import AdmissionError, QueryToolAdapter
from app.core.loop_budget import LoopLimit
from app.core.loop_tracing import fields, payload, rejection, step


class StrandsToolBindings:
    """P2 must prepare/bind the admitted batch before letting the SDK execute it.

    No goal_id, identity, condition flag or evidence-writing parameter is added to
    public domain schemas. Unbound or modified calls fail closed. A binding is
    consumed once, so repeated SDK IDs cannot execute an operation twice.
    """

    def __init__(
        self, adapter: QueryToolAdapter, *,
        observation: Callable[[EvidenceRecord], dict] | None = None,
    ) -> None:
        self.adapter = adapter
        self._observation = observation or (lambda record: record.observation())
        self._bindings: dict[str, str] = {}
        self._seen: set[str] = set()
        self.limit_reached: str | None = None

    def validate_binding(self, tool_use_id: str, operation_id: str) -> None:
        if not tool_use_id or tool_use_id in self._seen:
            raise AdmissionError("duplicate_or_empty_tool_use_id")
        self.adapter.operation(operation_id)

    def bind(self, tool_use_id: str, operation_id: str) -> None:
        self.validate_binding(tool_use_id, operation_id)
        self._bindings[tool_use_id] = operation_id
        self._seen.add(tool_use_id)

    def tools(self) -> list[PythonAgentTool]:
        tools = []
        for contract in self.adapter.catalog.list_contracts():
            if not contract.read_only:
                continue
            arguments = deepcopy(contract.public_input_model.model_json_schema())
            definitions = arguments.pop("$defs", None)
            schema = GoalToolCall.model_json_schema()
            schema["properties"]["arguments"] = arguments
            # Domain schemas use root-relative refs; preserve their resolution in the envelope.
            if definitions:
                schema["$defs"] = definitions
            tools.append(PythonAgentTool(
                contract.llm_name,
                {"name": contract.llm_name, "description": contract.description,
                 "inputSchema": {"json": schema}},
                self._execute,
            ))
        return tools

    def decode(self, tool: ToolUse) -> GoalToolCall:
        bound = self.adapter.catalog.get_by_llm_name(tool["name"])
        if bound is None or not bound.contract.read_only:
            raise AdmissionError("unknown_or_non_query_tool")
        try:
            call = GoalToolCall.model_validate(tool["input"], strict=True)
            arguments = bound.contract.public_input_model.model_validate(
                call.arguments, strict=True,
            ).model_dump(mode="json", exclude_none=True)
        except ValidationError as exc:
            raise AdmissionError("invalid_input") from exc
        return call.model_copy(update={"arguments": arguments})

    async def _execute(self, tool: ToolUse, **_: Any) -> ToolResult:
        with step(f"工具 · {tool['name']}", "TOOL") as span:
            payload("input", tool["input"])
            fields(tool_use_id=tool["toolUseId"], **{"tool.name": tool["name"]})
            result = await self._execute_bound(tool)
            if result["status"] == "error" and span is not None:
                from opentelemetry.trace import StatusCode

                span.set_status(StatusCode.ERROR, "tool failed or rejected")
            return result

    async def _execute_bound(self, tool: ToolUse) -> ToolResult:
        tool_use_id = tool["toolUseId"]
        try:
            if self.limit_reached:
                raise LoopLimit(self.limit_reached)
            operation_id = self._bindings.pop(tool_use_id, None)
            if operation_id is None:
                raise AdmissionError("unbound_tool_call")
            operation = self.adapter.operation(operation_id)
            if tool["name"] != operation.tool_name:
                raise AdmissionError("bound_tool_mismatch")
            call = self.decode(tool)
            if call.goal_id != operation.goal_id:
                raise AdmissionError("bound_goal_mismatch")
            if call.arguments != operation.arguments:
                raise AdmissionError("bound_arguments_mismatch")
            record = await self.adapter.execute(operation_id)
            return {
                "toolUseId": tool_use_id,
                "status": "success" if record.result.ok else "error",
                "content": [{"json": self._observation(record)}],
            }
        except (AdmissionError, LoopLimit) as exc:
            rejection(tool, str(exc), {"rejection": str(exc)})
            payload("output", {"rejection": str(exc)})
            if isinstance(exc, LoopLimit):
                # SDK executors turn ordinary exceptions into tool errors and continue.
                # Preserve budget termination for the session's AfterTools hook.
                self.limit_reached = str(exc)
            return {
                "toolUseId": tool_use_id, "status": "error",
                "content": [{"json": {"rejection": str(exc)}}],
            }
