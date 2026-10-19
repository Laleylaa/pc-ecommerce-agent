"""Tracing must preserve the formal graph behavior and survive partial failures.

Only scripted models, existing in-memory tools, fake audit persistence and tmp_path.
Run with scripts.orchestration_audit_safety; no external database dependency.
"""

import asyncio
import json
from decimal import Decimal

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda
from langsmith import Client

from app.core.config import Settings
from app.core.tracing import agent_trace, trace_config, trace_data, trace_event
from app.tools.contracts import BoundTool, DefaultToolContractProvider, execute_bound_tool
from app.tools.schemas import ToolError
from scripts.eval_goal_tool_flow import ScriptedSmokeModel, evaluate
from scripts.goal_tool_fixtures import SearchRecords
from tests.support.runtime import (
    ScriptedModel,
    goal,
    keyboard_plan,
    setup_runtime,
    task,
    through_tools,
)
from tests.support.tool_plan_fixtures import facet_plan


def settings_at(path, mode="local"):
    return Settings(_env_file=None, llm_api_key="", agent_trace_mode=mode, agent_trace_dir=path)


def records(recording):
    content = (recording.directory / "trace.jsonl").read_text()
    return [json.loads(line) for line in content.splitlines()]


def events(recording, name):
    return [r["data"] for r in records(recording) if r.get("name") == name and r["kind"] == "event"]


def facet_model(query, responses):
    """Script only model outputs; use the real internal planner and native callbacks."""
    planner = ScriptedModel({
        "route_request": [AIMessage(content="", tool_calls=[{
            "id": "route", "name": "route_request", "args": {
                "rewritten_query": query, "goals": [goal(query)],
            },
        }])],
        "submit_execution_plan": [AIMessage(content="", tool_calls=[{
            "id": "plan", "name": "submit_execution_plan", "args": {
                "tasks": [{**task(query, tool="catalog_facets"), "goal_id": "goal_1",
                           "arguments": {"query": query, "limit": 1}}],
            },
        }])],
    })
    internal = FakeListChatModel(responses=responses)
    planner.ainvoke = internal.ainvoke
    return planner


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome", ["repaired", "invalid_json", "invalid_plan", "unsupported", "normal", "empty"],
)
async def test_facet_planning_preserves_failure_and_capability_boundaries(
    monkeypatch, tmp_path, outcome,
):
    query = "白色蓝牙键盘有哪些布局可选"
    conditions = [
        {"key": "color", "values": ["白色"], "evidence": "白色"},
        {"key": "connection_type", "values": ["蓝牙"], "evidence": "蓝牙"},
    ]
    valid = json.dumps(facet_plan({
        "facet": "spec_value", "category": "keyboard", "spec_key": "style",
        "conditions": conditions,
    }))
    unsupported = json.dumps(facet_plan({
        "supported": False, "unsupported_reason": "synthetic unsupported result shape",
        "unresolved_requirements": [{"evidence": "导出商品清单",
                                     "reason": "request_not_supported"}],
    }))
    if outcome == "unsupported":
        query = "白色蓝牙键盘给我导出商品清单"
    if outcome == "empty":
        query = "不存在的测试品牌有哪些键盘布局"
    empty = json.dumps(facet_plan({
        "facet": "spec_value", "category": "keyboard", "spec_key": "style",
        "conditions": [{"key": "brand", "values": ["不存在的测试品牌"],
                        "evidence": "不存在的测试品牌"}],
    }))
    responses = {"repaired": ["not JSON", valid], "invalid_json": ["not JSON"] * 2,
                 "invalid_plan": ['{"facet":"invalid"}'] * 2,
                 "unsupported": [unsupported], "normal": [valid], "empty": [empty]}[outcome]
    if outcome not in {"invalid_json", "invalid_plan"}:
        responses.append(json.dumps({"verdict": "approve", "checks": [{
            "evidence": query, "plan_paths": ["/facet"], "status": "matched",
            "explanation": "Scripted approval for tracing and execution tests only",
        }]}))
    queries = []
    load = SearchRecords.load

    async def counted_query(self, category, scope):
        queries.append((category, scope))
        return await load(self, category, scope)

    monkeypatch.setattr(SearchRecords, "load", counted_query)
    case = {"id": outcome, "message": query, "dispositions": ["tool_planning"],
            "tools": ["catalog_facets"]}
    with agent_trace(settings_at(tmp_path), case_id=outcome) as recording:
        result = await evaluate(case, settings_at(tmp_path), model=facet_model(query, responses))
    execution = result["tool_results"][0]["execution"]
    normalized = result["normalized"][0]
    internal_runs = [r for r in records(recording)
                     if r["kind"] == "run_start" and r["run_type"] == "llm"]
    attempts = 3 if outcome == "repaired" else 2
    assert len(internal_runs) == attempts
    assert events(recording, "tool_handler_start")[0]["internal_input"]["limit"] == 1
    ledger = next(r["outputs"] for r in records(recording)
                  if r["kind"] == "run_end" and r["name"] == "update_subquery_ledger")
    artifact = ledger["task_artifacts"]["task_1"]
    status = ledger["task_status"]["task_1"]["status"]
    if outcome in {"invalid_json", "invalid_plan"}:
        assert not execution["ok"] and execution["error"]["code"] == "planning_output_invalid"
        assert normalized["outcome"] == "error"
        assert not artifact["usable"] and status == "failed"
        assert not queries
    elif outcome == "unsupported":
        assert execution["ok"] and execution["output"]["query_plan"]["supported"] is False
        assert execution["output"]["items"] == []
        assert normalized["outcome"] == "unsupported"
        assert not artifact["usable"] and status == "unavailable"
        assert not queries
    elif outcome == "empty":
        assert execution["ok"] and execution["output"]["items"] == []
        assert normalized["outcome"] == "empty"
        assert not artifact["usable"] and status == "unavailable"
        assert len(queries) == 1
    else:
        output = execution["output"]
        assert execution["ok"] and output["query_plan"]["planner"] == "llm"
        assert output["query_plan"]["query"] == query and output["query_plan"]["limit"] == 1
        assert output["query_plan"]["conditions"] == facet_plan(
            {"conditions": conditions}
        )["conditions"]
        assert [i["value"] for i in output["items"]] == ["75%"]
        assert normalized["outcome"] == "usable" and artifact["usable"] and status == "succeeded"
        assert len(queries) == 1
        if outcome == "repaired":
            payload = json.loads(internal_runs[1]["inputs"]["prompts"][0].rsplit("\nHuman: ", 1)[1])
            assert events(recording, "tool_handler_start")[0]["internal_input"]["query"] == query
            assert payload["previous_output"] == "not JSON"
            assert payload["validation_feedback"]["errors"][0]["type"] == "FORMAT_ERROR"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_output", [None, {}, "invalid output", ToolError(code="x", message="x")],
)
async def test_handler_wrong_output_type_returns_contract_error(monkeypatch, tmp_path, bad_output):
    contract = DefaultToolContractProvider().get_contract("catalog_search")

    async def handler(request):
        return bad_output

    async def operation(_):
        return await execute_bound_tool(
            BoundTool(contract, handler), {"query": "查询SKU 101价格"}, {"user_id": 1},
        )

    with agent_trace(settings_at(tmp_path)) as recording:
        result = await RunnableLambda(operation).ainvoke({}, trace_config())
    assert not result.ok and result.error.code == "execution_error"
    assert len(events(recording, "tool_handler_output")) == 1
    assert len(events(recording, "tool_output_rejected")) == 1

    async def invalid_search(self, request):
        return bad_output

    monkeypatch.setattr("scripts.eval_goal_tool_flow.CatalogSearchService.search", invalid_search)
    case = {"id": "invalid_output_handoff", "message": "查询SKU 101价格",
            "dispositions": ["tool_planning"], "tools": ["catalog_search"]}
    with agent_trace(settings_at(tmp_path), case_id=case["id"]) as recording:
        result = await evaluate(case, settings_at(tmp_path), model=ScriptedSmokeModel())
    execution = result["tool_results"][0]["execution"]
    assert not execution["ok"] and execution["error"]["code"] == "execution_error"
    assert result["normalized"][0]["outcome"] == "error"
    ledger = next(r["outputs"] for r in records(recording)
                  if r["kind"] == "run_end" and r["name"] == "update_subquery_ledger")
    assert not ledger["task_artifacts"]["task_1"]["usable"]
    assert ledger["task_status"]["task_1"]["status"] == "failed"
    assert events(recording, "tool_output_rejected")[0]["tool_call_id"] == "planned_task_1"
    assert recording.stop_reason == "tool_handoff"
    assert not {"summarize_goals", "persist_turn"} & set(result["visited"])


@pytest.mark.asyncio
async def test_formal_trace_matches_untraced_handoff_and_has_complete_parent_tree(tmp_path):
    case = {"id": "sku", "message": "查询SKU 101价格",
            "dispositions": ["tool_planning"], "tools": ["catalog_search"]}
    settings = settings_at(tmp_path)
    baseline = await evaluate(case, settings, model=ScriptedSmokeModel())
    with agent_trace(settings, case_id=case["id"]) as recording:
        traced = await evaluate(case, settings, model=ScriptedSmokeModel())
    assert traced == baseline
    rows = records(recording)
    starts = {r["run_id"]: r for r in rows if r["kind"] == "run_start"}
    ends = {r["run_id"]: r for r in rows if r["kind"] == "run_end"}
    assert starts.keys() == ends.keys()
    assert len({r["trace_id"] for r in starts.values()}) == 1
    assert all(not r["parent_run_id"] or r["parent_run_id"] in starts for r in starts.values())
    assert {"input_rules", "load_context", "request_router", "plan_tools", "orchestrate",
            "execute_tool_wave", "normalize_tool_results", "extract_task_artifacts",
            "update_subquery_ledger"} <= {r["name"] for r in starts.values()}
    assert not {"summarize_goals", "persist_turn"} & {r["name"] for r in starts.values()}
    assert len(events(recording, "planning_attempt")) == 2
    assert events(recording, "tool_handler_start")[0]["tool_call_id"] == "planned_task_1"
    summary = json.loads((recording.directory / "summary.json").read_text())
    assert summary["recording_complete"] and summary["stop_reason"] == "tool_handoff"
    assert summary["business_success"] == "not_assessed"


@pytest.mark.asyncio
async def test_clarification_is_a_recorded_no_tool_route(tmp_path):
    case = {"id": "clarify", "message": "这个呢", "dispositions": ["clarification"], "tools": []}
    model = ScriptedModel({"route_request": [AIMessage(content="", tool_calls=[{
        "id": "route", "name": "route_request", "args": {
            "rewritten_query": case["message"],
            "goals": [goal(case["message"], disposition="clarification")],
        },
    }])]})
    with agent_trace(settings_at(tmp_path)) as recording:
        result = await evaluate(case, settings_at(tmp_path), model=model)
    assert result["tool_results"] == []
    assert recording.stop_reason == "no_tool_route"
    assert not events(recording, "tool_handler_start")
    assert "plan_tools" not in result["visited"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["invalid_input", "execution_error"])
async def test_contract_rejection_and_handler_failure_are_distinct(tmp_path, failure):
    contract = DefaultToolContractProvider().get_contract("catalog_search")

    async def handler(request):
        raise ValueError("synthetic handler failure")

    async def operation(_):
        return await execute_bound_tool(
            BoundTool(contract, handler),
            {"query": "" if failure == "invalid_input" else "查询SKU 101价格"}, {"user_id": 1},
        )

    with agent_trace(settings_at(tmp_path)) as recording:
        result = await RunnableLambda(operation).ainvoke({}, trace_config())
    assert not result.ok and result.error.code == failure
    assert len(events(recording, "tool_handler_start")) == (failure != "invalid_input")
    expected = "tool_contract_rejected" if failure == "invalid_input" else "tool_handler_error"
    assert len(events(recording, expected)) == 1


@pytest.mark.asyncio
async def test_invalid_model_output_and_actual_repair_payload_survive(monkeypatch, tmp_path):
    text = "查询键盘"
    invalid = AIMessage(content="", invalid_tool_calls=[
        {"id": "broken", "name": "submit_execution_plan", "args": '{"tasks":',
         "error": "unexpected end"},
    ])
    runtime, model, _, _ = setup_runtime(
        monkeypatch, text, [goal(text)], [{"goal_id": "goal_1", "tasks": [task(text)]}],
        [{"category": "键盘", "category_evidence": "键盘"}], bad_tasks=invalid,
    )
    with agent_trace(settings_at(tmp_path)) as recording:
        await through_tools(runtime, text)
    responses = events(recording, "planning_response")
    assert responses[1]["response"]["invalid_tool_calls"][0]["args"] == '{"tasks":'
    assert events(recording, "planning_validation_error")[0]["will_repair"] is True
    attempts = events(recording, "planning_attempt")
    assert [a["attempt"] for a in attempts] == [1, 1, 2]
    assert "invalid_json" in attempts[-1]["messages"][-1]["content"]
    assert len(model.calls["submit_execution_plan"]) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("price,expected_calls", [(300, 2), (100, 1)])
async def test_deferred_binding_or_blocking_is_visible_without_invented_calls(
    monkeypatch, tmp_path, price, expected_calls,
):
    text = f"白色蓝牙键盘{price}元以内"
    downstream = "查询第一款当前价格"
    runtime, _, executor, _ = setup_runtime(
        monkeypatch, text, [goal(text)], [{"goal_id": "goal_1", "tasks": [
            task(text, answer_role="internal"),
            task(downstream, "task_2", depends_on=["task_1"], bindings=[{
                "source": "task_output", "ref": "task_1", "kind": "product", "positions": [1],
                "selection": "candidate_versions",
            }]),
        ]}], [keyboard_plan(price), {"operation": "lookup", "purpose": "lookup"}],
    )
    with agent_trace(settings_at(tmp_path)) as recording:
        state, _ = await through_tools(runtime, text, waves=expected_calls)
    assert len(executor.calls) == len(events(recording, "tool_handler_start")) == expected_calls
    if price == 300:
        before = events(recording, "tool_binding_start")[1]
        after = events(recording, "tool_binding_ready")[1]
        assert "scope" not in before["planned_call"]["arguments"]
        assert after["arguments"]["scope"]["sku_ids"] == [1]
        assert after["arguments"]["query"] == downstream
    else:
        assert state["task_status"]["task_2"]["status"] == "blocked"
        node_end = next(r for r in records(recording)
                        if r["kind"] == "run_end" and r["name"] == "update_subquery_ledger")
        assert node_end["outputs"]["task_status"]["task_2"]["status"] == "blocked"


@pytest.mark.asyncio
async def test_rejected_source_has_no_handler_start(monkeypatch, tmp_path):
    text = "查询键盘"
    invalid = task(text)
    invalid["arguments"]["scope"] = {"sku_ids": [999]}
    runtime, model, executor, _ = setup_runtime(
        monkeypatch, text, [goal(text)], [{"goal_id": "goal_1", "tasks": [invalid]}],
    )
    model.scripts["submit_execution_plan"] *= 2
    runtime.settings = settings_at(tmp_path)
    from app.schemas.chat import ChatRequest

    await runtime.run(ChatRequest(message=text), 1)
    directory = next(tmp_path.iterdir())
    rows = [json.loads(line) for line in (directory / "trace.jsonl").read_text().splitlines()]
    assert not executor.calls
    assert len([r for r in rows if r.get("name") == "planning_validation_error"]) == 2
    assert not [r for r in rows if r.get("name") == "tool_handler_start"]


@pytest.mark.asyncio
async def test_timeout_keeps_partial_input_and_propagates(tmp_path):
    async def hang(value):
        await trace_event("waiting", input=value)
        await asyncio.Event().wait()

    with pytest.raises(TimeoutError), agent_trace(settings_at(tmp_path)) as recording:
        await asyncio.wait_for(
            RunnableLambda(hang).ainvoke({"query": "fixture"}, trace_config()), .02,
        )
    assert events(recording, "waiting")[0]["input"] == {"query": "fixture"}
    summary = json.loads((recording.directory / "summary.json").read_text())
    assert summary["status"] == "TimeoutError"
    assert any(r.get("error") for r in records(recording) if r["kind"] == "run_end")


@pytest.mark.asyncio
async def test_local_and_off_override_global_cloud_flag(monkeypatch, tmp_path):
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setattr(
        "app.core.tracing.Client", lambda **kwargs: pytest.fail("cloud client created"),
    )
    with agent_trace(settings_at(tmp_path, "off")):
        assert await RunnableLambda(lambda x: x).ainvoke(3, trace_config()) == 3
    assert not list(tmp_path.iterdir())
    with agent_trace(settings_at(tmp_path)) as recording:
        assert await RunnableLambda(lambda x: x).ainvoke(3, trace_config()) == 3
    assert (recording.directory / "summary.json").exists()


@pytest.mark.asyncio
async def test_disk_failure_does_not_change_result(monkeypatch, tmp_path):
    with agent_trace(settings_at(tmp_path)) as recording:
        def fail_open(*args, **kwargs):
            raise OSError("simulated disk failure")

        monkeypatch.setattr(type(tmp_path), "open", fail_open)
        assert await RunnableLambda(lambda x: x + 1).ainvoke(2, trace_config()) == 3
    assert recording.write_errors > 0


@pytest.mark.asyncio
@pytest.mark.parametrize("flush_failure", [False, True])
async def test_cloud_sdk_receives_same_ids_and_domain_events(monkeypatch, tmp_path, flush_failure):
    created, updated = [], []
    # Disable the SDK's background worker/info lookup before construction, not just
    # its write methods: a worker could outlive the network guard's fixture teardown.
    monkeypatch.setattr("app.core.tracing.Client", lambda **kwargs: Client(
        **kwargs, auto_batch_tracing=False, info={},
    ))
    monkeypatch.setattr(Client, "create_run", lambda self, **data: created.append(data))
    monkeypatch.setattr(Client, "update_run", lambda self, run_id, **data: updated.append(
        {"id": run_id, **data}
    ))
    def flush(*args, **kwargs):
        if flush_failure:
            raise OSError("synthetic cloud failure")

    monkeypatch.setattr(Client, "flush", flush)
    settings = settings_at(tmp_path, "langsmith")
    settings.langsmith_api_key = Settings(
        _env_file=None, langsmith_api_key="synthetic-key",
    ).langsmith_api_key

    async def operation(value):
        await trace_event("contract_checked", arguments=value)
        return {"ok": True}

    with agent_trace(settings) as recording:
        assert await RunnableLambda(operation).ainvoke({"query": "synthetic"}, trace_config()) == {
            "ok": True,
        }
    assert created and updated
    assert str(created[0]["id"]) == recording.root_run_id
    assert any(e["name"] == "contract_checked" for u in updated for e in u.get("events", []))
    summary = json.loads((recording.directory / "summary.json").read_text())
    assert summary["recording_complete"]
    if flush_failure:
        assert summary["cloud_status"] == "flush_failed_local_available"


@pytest.mark.asyncio
async def test_native_internal_chat_model_is_a_child_run(tmp_path):
    model = FakeListChatModel(responses=['{"operation":"lookup"}'])

    async def tool_stage(_):
        return await model.ainvoke("synthetic internal planner input")

    with agent_trace(settings_at(tmp_path)) as recording:
        await RunnableLambda(tool_stage).ainvoke({}, trace_config())
    rows = records(recording)
    model_start = next(r for r in rows if r["kind"] == "run_start" and r["run_type"] == "llm")
    model_end = next(r for r in rows if r["kind"] == "run_end" and r["run_type"] == "llm")
    assert model_start["parent_run_id"] == recording.root_run_id
    assert "synthetic internal planner input" in json.dumps(model_start["inputs"])
    assert '{\\"operation\\":\\"lookup\\"}' in json.dumps(model_end["outputs"])


def test_secret_keys_and_json_messages_are_redacted_without_losing_money():
    value = {"content": '{"api_key":"test-secret","query":"fixture"}',
             "price": Decimal("12.34"), "authorization": "Bearer test-secret",
             "settings": Settings(_env_file=None, llm_api_key="test-secret")}
    clean = trace_data(value)
    assert "test-secret" not in json.dumps(clean)
    assert clean["price"] == "12.34"
    assert json.loads(clean["content"])["query"] == "fixture"


@pytest.mark.asyncio
async def test_concurrent_cases_have_independent_ids_and_files(tmp_path):
    async def one(value):
        async def operation(data):
            await asyncio.sleep(0)
            await trace_event("case", value=data)
            return data

        with agent_trace(settings_at(tmp_path), case_id=value) as recording:
            await RunnableLambda(operation).ainvoke(value, trace_config())
        return recording

    left, right = await asyncio.gather(one("left"), one("right"))
    assert left.directory != right.directory and left.root_run_id != right.root_run_id
    assert events(left, "case") == [{"value": "left"}]
    assert events(right, "case") == [{"value": "right"}]
