"""Opt-in LangChain run tracing, with a local copy independent of cloud availability.

Run IDs/parents/timings belong to LangChain. Business boundary details are custom
events on those runs; this module never changes or reconstructs agent state.
"""

import json
import logging
from collections import Counter
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from threading import Lock
from typing import Any
from uuid import UUID, uuid4

from langchain_core.callbacks.manager import adispatch_custom_event
from langchain_core.tracers.base import BaseTracer
from langchain_core.tracers.langchain import LangChainTracer
from langsmith import Client, tracing_context
from pydantic import BaseModel, SecretStr

from app.core.config import Settings

logger = logging.getLogger(__name__)
_active: ContextVar[Any] = ContextVar("agent_recording", default=None)
_attributes: ContextVar[dict | None] = ContextVar("agent_trace_attributes", default=None)
_SECRET_KEYS = {
    "api_key", "llm_api_key", "langsmith_api_key", "tavily_api_key", "authorization", "password",
    "access_token", "refresh_token", "auth_token_secret", "database_url", "redis_url",
}


def trace_data(value: Any) -> Any:
    """JSON-safe snapshots, never repr() runtime clients/settings/unknown objects.

    Key-based secret removal is not general free-text PII anonymization. Use
    synthetic eval data; inspect real conversation exports before sharing.
    """
    if isinstance(value, (Settings, SecretStr)):
        return "[redacted]"
    if isinstance(value, type) and issubclass(value, BaseModel):
        return trace_data(value.model_json_schema())
    if isinstance(value, BaseModel):
        return trace_data(value.model_dump(mode="python"))
    if isinstance(value, dict):
        return {
            str(k): "[redacted]" if str(k).lower() in _SECRET_KEYS else trace_data(v)
            for k, v in value.items()
        }
    if isinstance(value, (tuple, list, set)):
        return [trace_data(v) for v in value]
    if isinstance(value, str):
        # Model payloads/repair feedback often contain JSON inside message.content.
        if value.lstrip().startswith(("{", "[")):
            try:
                decoded = json.loads(value)
                clean = trace_data(decoded)
                if clean != decoded:
                    return json.dumps(clean, ensure_ascii=False)
            except (ValueError, RecursionError):
                pass
        return value
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (Decimal, UUID, Path)):
        return str(value)
    return f"<{type(value).__name__}>"


class LocalTrace(BaseTracer):
    """Persist standard runs incrementally, including runs that never finish."""

    run_inline = True

    def __init__(self, directory: Path, metadata: dict[str, Any]):
        super().__init__()
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=False, mode=0o700)
        self.lock = Lock()
        self.sequence = 0
        self.write_errors = 0
        self.open_runs: set[str] = set()
        self.events: Counter = Counter()
        self.run_summaries: list[dict[str, Any]] = []
        self.stop_reason: str | None = None
        self.root_run_id: str | None = None
        self.cloud_status = "disabled"
        self.client: Client | None = None
        self.setup_errors = 0
        metadata = {**metadata, "recording_id": directory.name}
        self.config = {"callbacks": [self], "metadata": metadata, "run_name": "pc_agent"}
        self.write("recording_start", metadata=metadata)

    def write(self, kind: str, **data: Any) -> None:
        try:
            with self.lock:
                self.sequence += 1
                entry = {
                    "schema_version": 1, "sequence": self.sequence,
                    "time": datetime.now(UTC).isoformat(), "kind": kind, **data,
                }
                with (self.directory / "trace.jsonl").open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(trace_data(entry), ensure_ascii=False) + "\n")
        except Exception:
            self.write_errors += 1
            logger.warning("Agent trace write failed; business execution continues")

    def _persist_run(self, run) -> None:
        pass  # Every run is already written by the hooks, not only the root.

    def _on_run_create(self, run) -> None:
        if run.parent_run_id is None:
            self.root_run_id = str(run.id)
        self.open_runs.add(str(run.id))
        self.write(
            "run_start", run_id=str(run.id), parent_run_id=str(run.parent_run_id or ""),
            trace_id=str(run.trace_id), name=run.name, run_type=run.run_type,
            inputs=run.inputs, metadata=run.extra.get("metadata", {}),
            # Native callback parameters include bound tools/tool_choice and provider
            # options. Do not serialize clients or infer parameters from Settings.
            **({"invocation_params": run.extra.get("invocation_params", {}),
                "options": run.extra.get("options", {})} if run.run_type == "llm" else {}),
        )

    def _on_run_update(self, run) -> None:
        self.open_runs.discard(str(run.id))
        run_summary = {
            "run_id": str(run.id), "name": run.name, "run_type": run.run_type,
            "duration_ms": (run.end_time - run.start_time).total_seconds() * 1000,
            "has_error": bool(run.error),
            "expected_boundary_close": bool(
                self.stop_reason and str(run.id) == self.root_run_id
                and run.error and run.error.startswith("GeneratorExit")
            ),
        }
        self.run_summaries.append(run_summary)
        self.write(
            "run_end", run_id=str(run.id), parent_run_id=str(run.parent_run_id or ""),
            trace_id=str(run.trace_id), name=run.name, run_type=run.run_type,
            outputs=run.outputs, error=run.error,
            duration_ms=(run.end_time - run.start_time).total_seconds() * 1000,
        )

    def on_custom_event(self, name, data, *, run_id, **kwargs) -> None:
        self.events[name] += 1
        self.write("event", run_id=str(run_id), name=name, data=data)

    def finish(self, status: str) -> None:
        self.write("recording_end", status=status)
        summary = {
            "schema_version": 1, "status": status, "event_counts": dict(self.events),
            "unfinished_run_ids": sorted(self.open_runs), "write_errors": self.write_errors,
            "recording_complete": not self.open_runs and self.write_errors == 0,
            "business_success": "not_assessed",
            "stop_reason": self.stop_reason,
            "trace_id": self.root_run_id,
            "runs": self.run_summaries,
            "cloud_status": self.cloud_status,
            "setup_errors": self.setup_errors,
        }
        try:
            (self.directory / "summary.json").write_text(
                json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        except Exception:
            logger.warning("Agent trace summary unavailable; inspect trace.jsonl")


class CloudTrace(LangChainTracer):
    """Keep domain events on native LangSmith node runs as well as local JSONL."""

    run_inline = True

    def on_custom_event(self, name, data, *, run_id, **kwargs) -> None:
        run = self.run_map.get(str(run_id))
        if run is not None:
            run.add_event({"name": name, "time": datetime.now(UTC), "kwargs": trace_data(data)})


def trace_config() -> dict[str, Any]:
    recording = _active.get()
    return recording.config if recording is not None else {}


@contextmanager
def trace_attributes(**data: Any):
    token = _attributes.set({**(_attributes.get() or {}), **data})
    try:
        yield
    finally:
        _attributes.reset(token)


async def trace_event(name: str, **data: Any) -> None:
    recording = _active.get()
    if recording is None:
        return
    try:
        payload = trace_data({**(_attributes.get() or {}), **data})
        try:
            await adispatch_custom_event(name, payload)
        except RuntimeError:
            # Terminal events can occur outside the graph callback scope. The
            # fallback must share the same failure isolation as native dispatch.
            recording.on_custom_event(name, payload, run_id=recording.root_run_id)
    except Exception:
        # Observability failures must not trigger planning repair or business retry.
        recording.write_errors += 1
        logger.warning("Agent trace event unavailable: %s", name)


def trace_boundary(reason: str, state: dict[str, Any]) -> None:
    recording = _active.get()
    if recording is not None:
        recording.stop_reason = reason
        recording.write(
            "evaluation_stop", reason=reason, run_id=recording.root_run_id,
            task_status=state.get("task_status"), planning_error=state.get("planning_error"),
            normalized_tool_results=state.get("normalized_tool_results"),
            task_artifacts=state.get("task_artifacts"),
        )


@contextmanager
def trace_scope(recording: LocalTrace | None, settings: Settings):
    token = _active.set(recording)
    try:
        with tracing_context(
            enabled=recording is not None and recording.client is not None,
            client=recording.client if recording else None, project_name=settings.langsmith_project,
        ):
            yield
    finally:
        _active.reset(token)


@contextmanager
def agent_trace(settings: Settings, *, activate: bool = True, **metadata: Any):
    """One request/case per directory; mode off creates no files or client."""
    if _active.get() is not None:
        yield _active.get()
        return
    recording = client = None
    if settings.agent_trace_mode != "off":
        directory = settings.agent_trace_dir / (
            datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex
        )
        try:
            recording = LocalTrace(directory, metadata)
            if settings.agent_trace_mode == "langsmith":
                if not settings.langsmith_api_key.get_secret_value():
                    raise ValueError("LangSmith key missing")
                client = Client(
                    api_key=settings.langsmith_api_key.get_secret_value(),
                    api_url=settings.langsmith_endpoint, anonymizer=trace_data,
                    omit_traced_runtime_info=True,
                )
                recording.config["callbacks"].append(
                    CloudTrace(client=client, project_name=settings.langsmith_project)
                )
                recording.client = client
                recording.cloud_status = "upload_requested_not_verified"
        except Exception as exc:
            client = None
            if recording:
                recording.setup_errors += 1
                recording.cloud_status = "setup_failed_local_only"
                recording.write("setup_error", error_type=type(exc).__name__)
            logger.warning("Agent tracing setup incomplete (%s)", type(exc).__name__)
    status = "completed"
    try:
        # Explicit local/off mode overrides unrelated global tracing environment flags.
        with trace_scope(recording, settings) if activate else nullcontext():
            yield recording
    except BaseException as exc:
        status = type(exc).__name__
        raise
    finally:
        # Events are already on disk. Bound the SDK queue drain and never let
        # upload failure change a successful response or suppress cancellation.
        if client:
            try:
                client.flush(timeout=2)
            except Exception:
                recording.cloud_status = "flush_failed_local_available"
                logger.warning("LangSmith flush incomplete; local trace remains available")
        if recording:
            recording.finish(status)
