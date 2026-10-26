"""Offline checks of the E2E apparatus, never business acceptance results."""

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from tests.e2e import check, run
from tests.e2e.suite import check_value, load_cases, read, write


def prepared(tmp_path, monkeypatch, case_id="B01"):
    cases = load_cases(selected=[case_id])
    oracle = run.template(cases, "normal")
    oracle["target"].update(
        user_id=41,
        model="synthetic-model",
        server_revision="synthetic-server",
        dataset_revision="synthetic-data",
        synthetic_account_only=True,
        initial_preferences_verified=True,
        valid_until=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
    )
    oracle["cases"][case_id].update(
        ready=True,
        verified_at=datetime.now(UTC).isoformat(),
        verified_by="offline-test",
        facts="Only a synthetic harness check, not business evidence.",
        evidence_files=["facts.txt"],
        bindings={"eligible_500_ids": [1], "eligible_300_ids": [1]},
    )
    (tmp_path / "facts.txt").write_text("Synthetic independent fixture", encoding="utf-8")
    write(tmp_path / "oracle.json", oracle)
    monkeypatch.setattr(run, "source_paths", list)
    batch = tmp_path / "batch"
    run.prepare(cases, tmp_path / "oracle.json", batch, 1, 300)
    return batch, read(batch / "evidence/frozen.json")


def response(answer="你好，我能帮助查询商品。", conversation_id=7):
    return {
        "conversation_id": conversation_id,
        "answer": answer,
        "products": [],
        "orders": [],
        "order": None,
        "evidence": [],
        "suggested_actions": [],
        "execution_details": {"tool_calls": []},
    }


def execute_mock(batch, frozen, bodies):
    messages, requests = [], []

    def handler(request):
        requests.append(request)
        if request.method == "POST":
            payload = json.loads(request.content)
            body = bodies[len([m for m in messages if m["role"] == "assistant"])]
            messages.extend(
                [
                    {"role": "user", "content": payload["message"]},
                    {"role": "assistant", "content": body["answer"], "metadata": body},
                ]
            )
            return httpx.Response(200, json=body)
        return httpx.Response(200, json={"id": 7, "messages": messages})

    with httpx.Client(
        transport=httpx.MockTransport(handler), base_url="http://test/api/"
    ) as client:
        run.execute(batch, frozen, client)
    return requests


def reviewed(batch, status="pass"):
    rows = check.jsonl(batch / "checks/semantic.jsonl")
    for row in rows:
        row.update(
            status=status,
            reviewer={"kind": "llm", "name": "synthetic-reviewer"},
            reviewed_at=datetime.now(UTC).isoformat(),
            reason="Synthetic review only.",
            evidence=["evidence/executions.jsonl#B01/1/1"],
        )
    check.write_jsonl(batch / "checks/semantic.jsonl", rows)


def test_catalog_has_unique_cases_and_per_turn_annotations():
    cases = load_cases()
    assert len(cases) == 50
    assert sum(len(c["turns"]) for c in cases) == 61
    assert len({c["id"] for c in cases}) == 50
    assert {c["suite"] for c in cases} == {"business", "boundary"}


def test_isolated_target_rejects_shared_reuse_and_identifier_injection():
    from tests.e2e.isolated import isolated_url

    source = "postgresql+asyncpg://synthetic:synthetic@localhost/pc_e2e_existing"
    for name in ("pc_agent", "pc_e2e_existing", 'pc_e2e_bad";DROP DATABASE x', "pc_e2e_x"):
        with pytest.raises(ValueError):
            isolated_url(source, name)
    assert isolated_url(source, "pc_e2e_new_fixture").endswith("/pc_e2e_new_fixture")


def test_isolated_data_really_contains_cross_sku_trap_and_large_legal_titles():
    from tests.e2e.isolated import fixtures

    rows = fixtures()
    trap = [r for r in rows if r["spu"] == 30]
    assert any(r["specs"]["color"] == "白色" for r in trap)
    assert any(r["specs"]["connection_type"] == "蓝牙" for r in trap)
    assert not any(
        r["specs"]["color"] == "白色" and r["specs"]["connection_type"] == "蓝牙" for r in trap
    )
    assert len([r for r in rows if r["spu"] == 50]) == 12
    assert all(len(r["name"] + " " + r["version"]) <= 255 for r in rows)


def test_exposure_never_passes_configuration_or_untriggered_budget(tmp_path):
    from tests.e2e.exposure import evaluate, verify

    assert verify(tmp_path, "E14", 1, 1)["status"] == "unknown"
    assert not evaluate(
        "E13",
        [
            {
                "attributes": {
                    "metadata.before_cost": "15000",
                    "metadata.token_limit": "12000",
                    "metadata.truncated": "False",
                    "validation_outcome": "accepted",
                }
            }
        ],
    )[0]
    assert evaluate(
        "E13",
        [
            {
                "attributes": {
                    "metadata.before_cost": "20000",
                    "metadata.token_limit": "12000",
                    "metadata.truncated": "True",
                    "validation_outcome": "accepted",
                }
            }
        ],
    )[0]


def test_policy_conflict_requires_both_actual_retrieved_sources():
    from tests.e2e.exposure import evaluate

    def spans(ids):
        return [
            {
                "attributes": {
                    "tool.name": "policy_search",
                    "output.value": json.dumps(
                        {
                            "ok": True,
                            "output": {"documents": [{"source_id": i} for i in ids]},
                        }
                    ),
                }
            }
        ]

    assert not evaluate("E16", spans([717]))[0]
    assert evaluate("E16", spans([717, 725]))[0]


def test_web_exposure_uses_public_error_and_real_internal_classification():
    from tests.e2e.exposure import evaluate

    span = {
        "attributes": {
            "tool.name": "knowledge_search",
            "output.value": json.dumps(
                {
                    "ok": False,
                    "error": {"code": "dependency_unavailable"},
                }
            ),
        },
        "events": [],
    }
    assert not evaluate("E15", [span])[0]
    span["events"] = [
        {
            "attributes": {
                "details": json.dumps(
                    {
                        "internal_code": "web_search_unavailable",
                    }
                )
            }
        }
    ]
    assert evaluate("E15", [span])[0]


def test_incomplete_baseline_cannot_prepare_or_contact_server(tmp_path):
    cases = load_cases(selected=["B03"])
    write(tmp_path / "oracle.json", run.template(cases, "normal"))
    with pytest.raises((ValueError, TypeError)):
        run.prepare(cases, tmp_path / "oracle.json", tmp_path / "batch", 1, 300)
    assert not (tmp_path / "batch").exists()


def test_http_success_needs_semantic_review_and_preserves_review_history(tmp_path, monkeypatch):
    batch, frozen = prepared(tmp_path, monkeypatch)
    execute_mock(batch, frozen, [response()])
    assert check.render(batch)["counts"] == {"unknown": 1}
    reviewed(batch)
    assert check.render(batch)["counts"] == {"pass": 1}
    assert list((batch / "checks/history").glob("*/semantic.jsonl"))
    reviewed(batch, "fail")
    assert check.render(batch)["counts"] == {"fail": 1}


def test_not_run_and_interrupted_request_are_distinct(tmp_path, monkeypatch):
    batch, _ = prepared(tmp_path, monkeypatch)
    assert check.render(batch)["counts"] == {"not_run": 1}
    run.append(batch, {"event": "started", "case_id": "B01", "attempt": 1, "turn": 1})
    assert check.render(batch)["counts"] == {"unknown": 1}


def test_frozen_input_tampering_is_rejected(tmp_path, monkeypatch):
    batch, frozen = prepared(tmp_path, monkeypatch)
    frozen["cases"][0]["turns"][0]["message"] = "changed"
    write(batch / "evidence/frozen.json", frozen)
    with pytest.raises(ValueError, match="frozen evidence changed"):
        check.render(batch)


@pytest.mark.parametrize(
    "body", [{}, {"products": []}, {"products": [{}]}, {"products": [{"price": "501"}]}]
)
def test_missing_or_empty_numeric_evidence_cannot_pass(body):
    result = check_value(
        body, {"id": "price", "path": "products.*.price", "op": "max", "expected": 500}
    )
    assert result["status"] == "fail"


def test_transport_timeout_is_not_retried_or_reported_as_not_run(tmp_path, monkeypatch):
    batch, frozen = prepared(tmp_path, monkeypatch)
    count = 0

    def timeout(request):
        nonlocal count
        count += 1
        raise httpx.ReadTimeout("synthetic", request=request)

    with httpx.Client(transport=httpx.MockTransport(timeout), base_url="http://test/") as client:
        run.execute(batch, frozen, client)
    assert count == 1
    assert check.render(batch)["counts"] == {"unknown": 1}


def test_multiturn_uses_actual_conversation_and_hard_failure_beats_semantic_pass(
    tmp_path,
    monkeypatch,
):
    batch, frozen = prepared(tmp_path, monkeypatch, "B14")
    bodies = [response(), response()]
    for body in bodies:
        body["products"] = [{"sku_id": 1, "price": "499"}]
    requests = execute_mock(batch, frozen, bodies)
    payloads = [json.loads(r.content) for r in requests if r.method == "POST"]
    assert "conversation_id" not in payloads[0]
    assert payloads[1]["conversation_id"] == 7
    check.render(batch)
    reviewed(batch)
    assert check.render(batch)["counts"] == {"fail": 1}


def test_history_mismatch_fails_even_with_semantic_pass(tmp_path, monkeypatch):
    batch, frozen = prepared(tmp_path, monkeypatch)
    execute_mock(batch, frozen, [response()])
    events = check.jsonl(batch / "evidence/executions.jsonl")
    events[-1]["body"]["messages"][-1]["content"] = "wrong saved answer"
    check.write_jsonl(batch / "evidence/executions.jsonl", events)
    check.render(batch)
    reviewed(batch)
    assert check.render(batch)["counts"] == {"fail": 1}


def test_stale_or_missing_evidence_review_cannot_pass(tmp_path, monkeypatch):
    batch, frozen = prepared(tmp_path, monkeypatch)
    execute_mock(batch, frozen, [response()])
    check.render(batch)
    reviewed(batch)
    rows = check.jsonl(batch / "checks/semantic.jsonl")
    rows[0]["evidence"] = ["evidence/missing.json#row1"]
    check.write_jsonl(batch / "checks/semantic.jsonl", rows)
    assert check.render(batch)["counts"] == {"unknown": 1}


def test_finished_execution_hash_rejects_changed_raw_response(tmp_path, monkeypatch):
    batch, frozen = prepared(tmp_path, monkeypatch)
    execute_mock(batch, frozen, [response()])
    path = batch / "evidence/executions.jsonl"
    write(batch / "evidence/run_finished.json", {"executions_sha256": run.digest(path)})
    run.append(batch, {"event": "unexpected_mutation"})
    with pytest.raises(ValueError, match="execution evidence changed"):
        check.render(batch)


def test_failed_turn_does_not_send_dependent_followup(tmp_path, monkeypatch):
    batch, frozen = prepared(tmp_path, monkeypatch, "B14")
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(500, json={"detail": "synthetic failure"})

    with httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test/") as client:
        run.execute(batch, frozen, client)
    assert len(requests) == 1
    assert check.render(batch)["counts"] == {"fail": 1}
    checks = check.jsonl(batch / "checks/automatic.jsonl")
    assert any(row.get("turn") == 2 and row["status"] == "not_run" for row in checks)


def test_live_requires_matching_user_and_never_saves_token(tmp_path, monkeypatch):
    batch, _ = prepared(tmp_path, monkeypatch)
    monkeypatch.setenv("E2E_TOKEN", "synthetic-bearer-value")
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"id": 99})

    real_client = httpx.Client
    monkeypatch.setattr(
        run.httpx,
        "Client",
        lambda **kwargs: real_client(
            **kwargs,
            transport=httpx.MockTransport(handler),
        ),
    )
    with pytest.raises(ValueError, match="identity mismatch"):
        run.live(batch)
    assert len(requests) == 1 and requests[0].url.path == "/api/auth/me"
    assert all(
        "synthetic-bearer-value" not in p.read_text() for p in batch.rglob("*") if p.is_file()
    )
    with pytest.raises(FileExistsError):
        run.live(batch)
