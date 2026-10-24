"""Explicit live selection module runner; preparation uses only local files.

The production initial prompt, context, tool registry and QueryModel are reused.
Consume exactly one model response; never execute tools or return tool feedback.
"""

import argparse
import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from contextlib import ExitStack
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
RESULTS = HERE / "results"
MEASUREMENT = "initial_tool_selection"


def now():
    return datetime.now(UTC).isoformat()


def read(path):
    return json.loads(path.read_text())


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def append(path, value):
    with path.open("a") as stream:
        stream.write(json.dumps(value, ensure_ascii=False) + "\n")
        stream.flush()


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_state():
    paths = set((ROOT / "backend/app").rglob("*.py"))
    paths.update(HERE.glob("*.py"))
    paths.update(HERE.glob("*.json"))
    paths.update(HERE.glob("*.md"))
    paths.update(ROOT / name for name in (
        "AGENTS.md", "tests/README.md", "tests/REVIEW.md", "backend/pyproject.toml",
        "backend/uv.lock",
    ))
    return {str(path.relative_to(ROOT)): sha(path) for path in sorted(paths)}


def batch_path(name):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        raise ValueError("Batch must be a directory name, not a path")
    path = RESULTS / name
    if path.is_symlink():
        raise ValueError("Symlink batch directories are not supported")
    return path


def prepare(group, repeat, selected=None):
    cases = read(HERE / f"{group}_cases.json")
    contexts = read(HERE / "contexts.json")
    if cases.get("measurement") != MEASUREMENT:
        raise ValueError("Only initial tool selection suites are supported")
    ids = [case["id"] for case in cases["cases"]]
    if len(set(ids)) != len(ids):
        raise ValueError("Duplicate case IDs")
    all_ids = ids.copy()
    if selected:
        if len(set(selected)) != len(selected) or set(selected) - set(ids):
            raise ValueError("Selected case IDs must be unique and belong to this group")
        cases["cases"] = [case for case in cases["cases"] if case["id"] in selected]
        ids = [case["id"] for case in cases["cases"]]
    for case in cases["cases"]:
        contexts["contexts"][case["input"]["context"]]
    name = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + f"-{group}-{uuid4().hex[:8]}"
    batch = batch_path(name)
    batch.mkdir(parents=True, exist_ok=False)
    write(batch / "evidence/cases.json", cases)
    write(batch / "evidence/contexts.json", contexts)
    shutil.copy2(HERE / "review_prompt.md", batch / "evidence/review_prompt.md")
    state = source_state()
    for relative in state:
        dest = batch / "evidence/source" / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, dest)
    write(batch / "evidence/environment.json", {
        "batch_id": name, "group": group, "prepared_at": now(), "repeat": repeat,
        "measurement": MEASUREMENT,
        "selected_case_ids": ids, "excluded_case_ids": [i for i in all_ids if i not in ids],
        "head": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True,
        ).strip(),
        "worktree_status": subprocess.check_output(
            ["git", "status", "--short"], cwd=ROOT, text=True,
        ).splitlines(),
        "source_hashes": state,
        "frozen_hashes": {name: sha(batch / "evidence" / name)
                          for name in ("cases.json", "contexts.json", "review_prompt.md")},
        "budget": {"query_limit": 2, "tool_limit": 0, "total_limit": 2, "answer_limit": 0,
                   "query_seconds": 120},
        "model": None,
        "isolation": "one model response; zero tool execution/feedback; only configured model HTTP",
        "planned_attempts": [{"case_id": cid, "attempt": attempt}
                             for cid in ids for attempt in range(1, repeat + 1)],
    })
    print(f"Prepared only: {batch}")


async def select_once(prepared, model, budget):
    """Production first decision boundary, without invoking the SDK execution loop."""
    from app.agent.loop.query_context import query_context
    from app.agent.loop.query_loop import QuerySession
    from app.tools.contracts import static_tool_catalog

    session = QuerySession(prepared, static_tool_catalog(), budget)
    agent = session.build(model)
    # Use the same registry, control descriptions, visibility, context and model as run().
    model.tool_spec_filter = session.visible_tools
    events = []
    try:
        async for event in model.stream(
            messages=[{"role": "user", "content": [{
                "text": json.dumps(query_context(prepared), ensure_ascii=False),
            }]}],
            tool_specs=agent.tool_registry.get_all_tool_specs(),
            system_prompt=agent.system_prompt,
        ):
            events.append(event)
    finally:
        model.tool_spec_filter = None
    if session.adapter.evidence.all_records() or session.book.goals or session.query_batches:
        raise RuntimeError("selection_boundary_executed_tools")
    return {"model_events": events, "domain_executions": 0, "control_executions": 0}


def forbid_database(*args, **kwargs):
    raise RuntimeError("selection_module_forbids_database_access")


async def execute_attempt(batch, case, attempt, contexts, settings, budgets):
    import httpx
    from app.core.llm import resolve_llm_base_url
    from app.core.loop_budget import LoopBudget
    from app.core.loop_models import loop_models
    from app.schemas.context import PreparedTurn

    trace_rel = f"evidence/traces/{case['id']}-{attempt}.jsonl"
    trace = batch / trace_rel
    trace.parent.mkdir(exist_ok=True)
    # Exclusive creation prevents silently replacing earlier attempts.
    trace.touch(exist_ok=False)
    selection = None
    usage = None
    requests = 0
    successful_responses = 0
    base = httpx.URL(resolve_llm_base_url(settings))
    endpoint = base.path.rstrip("/") + "/chat/completions"

    def capture(event):
        text = json.dumps({"at": now(), **event}, ensure_ascii=False)
        if settings.llm_api_key:
            text = text.replace(settings.llm_api_key, "[REDACTED]")
        # No URL, headers, settings object or exception text is persisted.
        with trace.open("a") as stream:
            stream.write(text + "\n")
            stream.flush()

    class RecordingTransport(httpx.AsyncBaseTransport):
        def __init__(self):
            self.inner = httpx.AsyncHTTPTransport(retries=0)

        async def handle_async_request(self, request):
            nonlocal requests, successful_responses
            if (request.method != "POST" or request.url.path != endpoint
                    or (request.url.scheme, request.url.host, request.url.port)
                    != (base.scheme, base.host, base.port)):
                raise RuntimeError("selection_module_model_endpoint_only")
            body = json.loads(await request.aread())
            if successful_responses or any(
                m.get("role") not in {"system", "user"} for m in body.get("messages", [])
            ):
                raise RuntimeError("selection_forbids_followup_or_tool_feedback")
            requests += 1
            request_id = requests
            capture({"event": "request", "request_id": request_id, "body": body})
            try:
                response = await self.inner.handle_async_request(request)
                await response.aread()
                try:
                    body = response.json()
                except ValueError:
                    body = {"non_json": True}
                capture({"event": "response", "request_id": request_id,
                         "status_code": response.status_code, "body": body})
                if response.is_success:
                    successful_responses += 1
                return response
            except Exception as exc:
                capture({"event": "transport_error", "request_id": request_id,
                         "error_type": type(exc).__name__})
                raise

        async def aclose(self):
            await self.inner.aclose()

    result = {"batch_id": batch.name, "case_id": case["id"], "attempt": attempt,
              "started_at": now(), "trace_file": trace_rel, "state": "started"}
    append(batch / "evidence/executions.jsonl", result)
    try:
        context = contexts["contexts"][case["input"]["context"]]
        prepared = PreparedTurn(
            user_id=41, conversation_id=1, user_message_id=1, run_id=1,
            message=case["input"]["message"], **context,
        )
        budget = LoopBudget(**budgets)
        with ExitStack() as guards:
            for target in (
                "sqlalchemy.engine.Engine.connect", "sqlalchemy.engine.Engine.raw_connection",
                "sqlalchemy.ext.asyncio.AsyncEngine.connect",
                "sqlalchemy.ext.asyncio.AsyncSession.execute",
            ):
                guards.enter_context(patch(target, forbid_database))
            async with loop_models(settings, budget, transport=RecordingTransport()) as models:
                model, _, usage = models
                selection = await select_once(prepared, model, budget)
        result["state"] = "response_received"
    except Exception as exc:  # noqa: BLE001 - preserve every experiment failure
        result.update(state="error", error_type=type(exc).__name__)
    finally:
        result.update(ended_at=now(), http_requests=requests,
                      successful_responses=successful_responses,
                      usage=usage.summary() if usage else None)
        if selection is not None:
            result.update(selection)
        result["trace_sha256"] = sha(trace)
        append(batch / "evidence/executions.jsonl", result)


async def run(batch, group):
    environment = read(batch / "evidence/environment.json")
    if environment.get("measurement") != MEASUREMENT:
        raise ValueError("Legacy feedback-loop batches are out of scope; do not resume")
    if environment["group"] != group:
        raise ValueError("Use the runner for this batch's test type")
    if environment["source_hashes"] != source_state():
        raise ValueError("Sources changed since preparation; create a new batch")
    for name, digest in environment["frozen_hashes"].items():
        if sha(batch / "evidence" / name) != digest:
            raise ValueError("Frozen cases, contexts or review standard changed")
    if (batch / "evidence/run_started.json").exists():
        raise ValueError("This batch was already started; preserve it and prepare a new batch")
    os.environ["OTEL_SDK_DISABLED"] = "true"
    os.environ["LANGCHAIN_TRACING_V2"] = "false"
    sys.path.insert(0, str(ROOT / "backend"))
    from app.core.config import get_settings
    from app.core.llm import resolve_llm_base_url

    settings = get_settings().model_copy(update={
        "loop_trace_enabled": False, "agent_trace_mode": "off",
    })
    if not settings.llm_api_key:
        raise ValueError("Model API key is not configured")
    contexts = read(batch / "evidence/contexts.json")
    cases = read(batch / "evidence/cases.json")["cases"]
    environment["model"] = {"provider": settings.llm_provider,
                            "name": settings.llm_model, "temperature": 0,
                            "endpoint_sha256": hashlib.sha256(
                                resolve_llm_base_url(settings).encode(),
                            ).hexdigest()}
    environment["packages"] = {
        name: version(name) for name in ("strands-agents", "openai", "httpx", "pydantic")
    }
    write(batch / "evidence/environment.json", environment)
    write(batch / "evidence/run_started.json", {"at": now()})
    for case in cases:
        for attempt in range(1, environment["repeat"] + 1):
            if environment["source_hashes"] != source_state():
                raise ValueError("Sources changed during batch; remaining cases not executed")
            await execute_attempt(batch, case, attempt, contexts, settings, environment["budget"])
    write(batch / "evidence/run_finished.json", {"at": now()})
    print(f"Raw evidence saved: {batch}; semantic review still required")


def cli(group):
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--prepare", action="store_true", help="Freeze local inputs only")
    action.add_argument("--run", metavar="BATCH")
    parser.add_argument("--live", action="store_true", help="Explicitly enable model requests")
    parser.add_argument("--repeat", type=int, help="Preparation only; default 3")
    parser.add_argument("--case", action="append", dest="cases",
                        help="Preparation only; repeat to select a subset before freezing")
    args = parser.parse_args()
    if args.repeat is not None and not 1 <= args.repeat <= 10:
        parser.error("repeat must be 1..10")
    if args.prepare:
        prepare(group, args.repeat if args.repeat is not None else 3, args.cases)
    elif args.repeat is not None or args.cases:
        parser.error("--run uses frozen cases/repeat; --case and --repeat are preparation only")
    elif not args.live:
        parser.error("--run requires --live; no requests were made")
    else:
        asyncio.run(run(batch_path(args.run), group))
