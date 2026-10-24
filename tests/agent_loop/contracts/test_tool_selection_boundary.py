"""The routing harness must stop before executing or receiving any tool feedback."""

import importlib
import json
from pathlib import Path

import httpx
import pytest
from agent_loop.support.strands_provider import SETTINGS, Provider, call
from app.agent.loop.query_loop import QuerySession
from app.agent.loop.strands_tools import StrandsToolBindings
from app.agent.loop.tool_adapter import QueryToolAdapter
from app.core.loop_budget import LoopBudget
from app.core.loop_models import loop_models
from app.schemas.context import PreparedTurn
from app.tools.contracts import static_tool_catalog

pytestmark = pytest.mark.contract
HERE = Path(__file__).parents[1] / "business/tool_selection"


@pytest.fixture
def scripts(monkeypatch):
    monkeypatch.syspath_prepend(str(HERE))
    return (importlib.import_module("run_selection_cases"),
            importlib.import_module("check_selection_cases"))


def prepared():
    return PreparedTurn(user_id=41, conversation_id=1, user_message_id=1, run_id=1,
                        message="K01键盘多少钱？")


def no_execution(*args, **kwargs):
    pytest.fail("selection must not validate/execute any control or business tool")


@pytest.mark.asyncio
async def test_initial_request_matches_production_and_never_dispatches(scripts, monkeypatch):
    runner, _ = scripts
    registration = call("register_goals", {"registration": {"goals": [{
        "id": "g", "source_text": "K01键盘多少钱？", "query": "K01键盘多少钱？",
        "disposition": "direct_response",
    }]}})
    provider = Provider([[registration, call("query_complete", {
        "completion": {"reason": "synthetic control completion"},
    })]])
    budget = LoopBudget()
    async with loop_models(SETTINGS, budget, transport=httpx.MockTransport(provider)) as models:
        await QuerySession(prepared(), static_tool_catalog(), budget).run(models[0])
    assert len(provider.requests) == 1

    monkeypatch.setattr(QuerySession, "_register", no_execution)
    monkeypatch.setattr(QueryToolAdapter, "prepare", no_execution)
    monkeypatch.setattr(QueryToolAdapter, "execute", no_execution)
    monkeypatch.setattr(StrandsToolBindings, "decode", no_execution)
    # Even malformed arguments must remain observable as a choice, not execute/retry.
    captured = Provider([[registration, call("catalog_lookup", "invalid JSON")]])
    budget = LoopBudget(tool_limit=0)
    async with loop_models(SETTINGS, budget, transport=httpx.MockTransport(captured)) as models:
        result = await runner.select_once(prepared(), models[0], budget)
    assert len(captured.requests) == 1
    assert captured.requests[0] == provider.requests[0]
    assert result["domain_executions"] == result["control_executions"] == 0


def events(names, arguments=None):
    return [{"event": "request", "request_id": 1,
             "body": {"messages": [{"role": "user", "content": "synthetic"}]}},
            {"event": "response", "request_id": 1, "status_code": 200, "body": {
                "choices": [{"message": {"role": "assistant", "tool_calls": [
                    call(name, arguments or {"arguments": {}}) for name in names
                ]}}],
            }}]


def execution():
    return {"state": "response_received", "domain_executions": 0, "control_executions": 0}


@pytest.mark.parametrize("names,expected", [
    (["catalog_facets", "catalog_lookup"], "通过"),
    (["catalog_lookup"], "失败"),
    (["catalog_facets", "catalog_search"], "失败"),
    (["register_goals"], "无法判定"),
])
def test_mixed_selection_checks_tool_set_only(scripts, names, expected):
    _, checker = scripts
    case = {"expected_tool_sets": [["catalog_facets", "catalog_lookup"]]}
    checks, _ = checker.automatic(case, execution(), events(names))
    assert checks[-1]["status"] == expected


def test_equivalent_routes_and_parameter_errors_do_not_change_routing_score(scripts):
    _, checker = scripts
    case = {"expected_tool_sets": [["catalog_search"], ["catalog_lookup"]]}
    for names in (["catalog_search"], ["catalog_lookup", "catalog_lookup"]):
        checks, _ = checker.automatic(case, execution(), events(names, {
            "arguments": {"category_hint": None, "scope": {"sku_ids": [], "spu_ids": []},
                          "limit": -10, "query": "raw text"},
        }))
        assert checks[-1]["status"] == checker.PASS
    checks, _ = checker.automatic(case, execution(), events(["catalog_search", "catalog_lookup"]))
    assert checks[-1]["status"] == checker.FAIL


def test_feedback_or_second_response_is_not_a_selection_sample(scripts):
    _, checker = scripts
    case = {"expected_tool_sets": [["catalog_lookup"]]}
    trace = events(["catalog_lookup"])
    trace[0]["body"]["messages"].append({"role": "tool", "content": "no_match"})
    checks, _ = checker.automatic(case, execution(), trace)
    assert all(c["status"] == checker.UNKNOWN for c in checks)
    trace = events(["catalog_lookup"])
    trace.append(trace[-1])
    checks, _ = checker.automatic(case, execution(), trace)
    assert all(c["status"] == checker.UNKNOWN for c in checks)


def test_active_cases_have_no_feedback_or_parameter_assertions(scripts):
    runner, _ = scripts
    coverage = runner.read(HERE / "coverage.json")
    assert set(coverage["groups"]) == {
        "single_intent", "intent_boundaries", "mixed_goals", "context_scope",
    }
    count = 0
    contexts = runner.read(HERE / "contexts.json")["contexts"]
    for group in coverage["groups"].values():
        suite = runner.read(HERE / group["case_file"])
        assert suite["measurement"] == runner.MEASUREMENT
        for case in suite["cases"]:
            count += 1
            assert not {"hard", "fixture_profile", "feedback_scenario"} & set(case)
            assert set(case["semantic_checks"]) == {"routing", "coverage"}
            assert case["input"]["context"] in contexts
    assert count == coverage["total_cases"] == 39
    assert not (HERE / "fixtures.json").exists()


@pytest.mark.asyncio
async def test_freeze_capture_review_report_roundtrip_is_offline(scripts, monkeypatch, tmp_path):
    runner, checker = scripts
    monkeypatch.setattr(runner, "RESULTS", tmp_path)
    runner.prepare("single_intent", 1, ["si-04"])
    batch = next(tmp_path.iterdir())
    env = runner.read(batch / "evidence/environment.json")
    case = runner.read(batch / "evidence/cases.json")["cases"][0]
    provider = Provider([[call("catalog_lookup", {"arguments": {"query": "K01价格"}})]])
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda **_: httpx.MockTransport(provider))
    await runner.execute_attempt(batch, case, 1, runner.read(HERE / "contexts.json"),
                                 SETTINGS, env["budget"])
    rows = checker.jsonl(batch / "evidence/executions.jsonl")
    assert rows[-1]["state"] == "response_received"
    assert rows[-1]["domain_executions"] == rows[-1]["control_executions"] == 0
    assert len(provider.requests) == 1
    assert all(m["role"] in {"system", "user"} for m in provider.requests[0]["messages"])
    checker.render(batch)
    assert checker.jsonl(batch / "checks/automatic.jsonl")[0]["status"] == checker.UNKNOWN
    semantic = {
        "batch_id": batch.name, "case_id": "si-04", "attempt": 1,
        "trace_sha256": rows[-1]["trace_sha256"], "reviewer_kind": "llm",
        "reviewer": "synthetic unit fixture", "model": "synthetic", "reviewed_at": runner.now(),
        "review_standard": ["review-v1", "tool-selection-review-v3"],
        "summary": "synthetic review; not a real model evaluation",
        "checks": [{"id": k, "status": checker.PASS, "reason": "synthetic",
                    "evidence": ["evidence/traces/si-04-1.jsonl#request_id=1"]}
                   for k in ("routing", "coverage")],
    }
    (batch / "checks/semantic.jsonl").write_text(json.dumps(semantic) + "\n")
    checker.render(batch)
    assert checker.jsonl(batch / "checks/automatic.jsonl")[0]["status"] == checker.PASS
    assert "首次选择" in (batch / "report.md").read_text()


def test_legacy_results_cannot_be_rescored(scripts, tmp_path):
    runner, checker = scripts
    runner.write(tmp_path / "evidence/environment.json", {})
    runner.write(tmp_path / "evidence/cases.json", {"version": "tool-selection-v2"})
    with pytest.raises(ValueError, match="Legacy"):
        checker.render(tmp_path)
    assert not (tmp_path / "report.md").exists()
