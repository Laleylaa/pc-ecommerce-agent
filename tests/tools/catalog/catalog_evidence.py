"""Evidence I/O and official tool invocation. No import-time services or credentials."""

import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]


def now():
    return datetime.now(UTC).isoformat()


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def read_json(path):
    return json.loads(path.read_text())


def read_jsonl(path):
    return (
        [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        if (path.exists())
        else []
    )


def write_jsonl(path, records):
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in records)
    )


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def code_state():
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()

    files = list((ROOT / "backend/app").rglob("*.py"))
    files += list((ROOT / "backend/app/tools/prompts/templates").rglob("*.md"))
    files += [ROOT / "tests/run.py", ROOT / "pytest.ini", ROOT / "backend/pyproject.toml"]
    return {
        "head": git("rev-parse", "HEAD"),
        "branch": git("branch", "--show-current"),
        "worktree": git("status", "--short"),
        "fingerprints": {str(p.relative_to(ROOT)): sha(p) for p in sorted(files)},
    }


def batch_path(name):
    path = (HERE / "results" / name).resolve()
    if path.parent != (HERE / "results").resolve() or not path.is_dir():
        raise ValueError("Batch must be an existing direct child of catalog/results")
    return path


async def run_cases(session, settings, folder, suite):
    from app.core import loop_tracing as trace
    from app.core.loop_budget import LoopBudget
    from app.core.loop_models import LoopUsage, TextModel
    from app.core.strands_model import openai_client
    from app.tools.catalog.facets_planner import CatalogFacetPlanner
    from app.tools.catalog.planner import CatalogSearchPlanner
    from app.tools.contracts import build_tool_catalog, execute_bound_tool
    from sqlalchemy import event
    from sqlalchemy.ext.asyncio import async_sessionmaker

    tool_name = suite["tool_name"]
    sql_calls = []

    def record_sql(conn, cursor, statement, parameters, context, executemany):
        sql_calls.append(statement.split(None, 1)[0])

    event.listen(session.bind.sync_engine, "before_cursor_execute", record_sql)

    # Reuse existing request-local trace context without invoking chat runtime.
    tokens = [
        (
            trace._secrets,
            trace._secrets.set(
                tuple(
                    v
                    for v in (
                        settings.llm_api_key,
                        settings.auth_token_secret,
                        settings.tavily_api_key.get_secret_value(),
                        settings.langsmith_api_key.get_secret_value(),
                    )
                    if v
                )
            ),
        )
    ]
    try:
        for case in suite["cases"]:
            sql_calls.clear()
            budget = LoopBudget()
            usage = LoopUsage(budget)
            last = trace._last_model.set({})
            scope = trace._scope.set(None)
            record = {
                "case_id": case["id"],
                "batch_id": folder.name,
                "attempt": 1,
                "tool_name": tool_name,
                "started_at": now(),
                "arguments": case["input"],
                "trace_id": None,
            }
            try:
                with trace.step("商品工具测试 · " + case["id"], root=True) as span:
                    if span:
                        record["trace_id"] = format(span.context.trace_id, "032x")
                    trace.fields(
                        batch_id=folder.name,
                        case_id=case["id"],
                        attempt=1,
                        runtime="catalog_tool_basic",
                        model=settings.llm_model,
                    )
                    trace.payload("input", case["input"])
                    async with openai_client(
                        settings, usage, client_class=trace.ModelHTTPClient
                    ) as client:
                        model = TextModel(client, settings, budget)
                        catalog = build_tool_catalog(
                            session,
                            settings=settings,
                            catalog_planner=CatalogFacetPlanner(model),
                            order_planner=object(),
                            catalog_search_planner=CatalogSearchPlanner(
                                model, operation="search"
                            ),
                            catalog_lookup_planner=CatalogSearchPlanner(
                                model, operation="lookup"
                            ),
                            catalog_session_factory=async_sessionmaker(session.bind),
                        )
                        bound = catalog.get_by_llm_name(tool_name)
                        if bound is None:
                            raise ValueError("Missing official tool: " + tool_name)
                        budget.before_tool(None)
                        with (
                            budget.tool_scope(None),
                            trace.step("工具 · " + tool_name, "TOOL"),
                        ):
                            trace.fields(
                                batch_id=folder.name,
                                case_id=case["id"],
                                attempt=1,
                                tool_name=tool_name,
                            )
                            trace.payload("input", case["input"])
                            result = await execute_bound_tool(
                                bound, case["input"], {"user_id": 0}
                            )
                            record["result"] = result.model_dump(mode="json")
                            trace.payload("output", record["result"])
                    trace.payload("output", record.get("result"))
            except Exception as exc:  # noqa: BLE001 - persist every failed live attempt
                record["harness_error"] = {
                    "type": type(exc).__name__,
                    "message": trace.clean(str(exc)),
                }
            finally:
                record["tool_sql_count"] = len(sql_calls)
                record.update(
                    finished_at=now(),
                    usage=usage.summary(),
                    budget_calls=dict(budget.calls),
                    tool_attempts=budget.tool_attempts,
                )
                with (folder / "evidence/executions.jsonl").open("a") as handle:
                    handle.write(
                        json.dumps(trace.clean(record), ensure_ascii=False) + "\n"
                    )
                trace._scope.reset(scope)
                trace._last_model.reset(last)
            print(
                json.dumps(
                    {
                        "case": case["id"],
                        "trace_id": record["trace_id"],
                        "http_attempts": usage.attempts,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
        return {
            "flush_completed": trace.provider().force_flush(timeout_millis=2000),
            "notice": "Flush is not receiver verification; check_catalog_cases.py fetches Phoenix",
        }
    finally:
        event.remove(session.bind.sync_engine, "before_cursor_execute", record_sql)
        for variable, token in reversed(tokens):
            variable.reset(token)
        trace.shutdown()
