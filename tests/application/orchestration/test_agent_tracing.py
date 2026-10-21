import asyncio
import json
from decimal import Decimal

import pytest
from app.core.config import Settings
from app.core.tracing import agent_trace, trace_config, trace_data, trace_event
from app.tools.contracts import BoundTool, DefaultToolContractProvider, execute_bound_tool
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.runnables import RunnableLambda
from langsmith import Client

from tests.support.orchestration_agent_tracing import (
    events,
    records,
    settings_at,
)


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
