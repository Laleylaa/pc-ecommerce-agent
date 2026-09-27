import json
from pathlib import Path

import pytest

from scripts.run_section5_offline_ablation import _write_outputs, run_all

REQUIRED_RESULT_FIELDS = {
    "case_id",
    "variant",
    "goal_completion",
    "blocked_goal_result",
    "tool_calls",
    "task_states",
    "dependency_violations",
    "guard_result",
    "final_action",
    "latency_ms",
    "passed",
}


def _metrics(summary: dict, experiment: str, variant: str) -> dict:
    return next(
        group["metrics"]
        for group in summary["groups"]
        if group["experiment"] == experiment and group["variant"] == variant
    )


@pytest.mark.asyncio
async def test_minimal_protocol_retains_all_fixed_cases_and_variants() -> None:
    records, summary = await run_all()

    assert summary["fixed_case_count"] == 30
    assert summary["run_count"] == 60
    assert len(records) == 60
    assert len({(record["case_id"], record["variant"]) for record in records}) == 60
    assert all(REQUIRED_RESULT_FIELDS == set(record) for record in records)
    assert len({record["case_id"] for record in records}) == 30
    assert all(group["case_count"] == 10 for group in summary["groups"])


@pytest.mark.asyncio
async def test_full_and_ablation_results_expose_the_expected_contrasts() -> None:
    records, summary = await run_all()

    selective_full = _metrics(summary, "selective_routing", "full")
    selective_ablation = _metrics(
        summary,
        "selective_routing",
        "whole_request_blocking",
    )
    assert selective_full["valid_goal_completion_rate"] == 1.0
    assert selective_full["selective_fulfilment_rate"] == 1.0
    assert selective_full["over_refusal_count"] == 0
    assert selective_ablation["valid_goal_completion_rate"] == 0.0
    assert selective_ablation["selective_fulfilment_rate"] == 0.0
    assert selective_ablation["over_refusal_count"] == 10

    dependency_full = _metrics(summary, "dependency_execution", "full")
    dependency_ablation = _metrics(
        summary,
        "dependency_execution",
        "flat_task_execution",
    )
    assert dependency_full["dependency_violation_count"] == 0
    assert dependency_full["premature_tool_call_count"] == 0
    assert dependency_ablation["dependency_violation_count"] == 10
    assert dependency_ablation["premature_tool_call_count"] == 10

    guard_full = _metrics(summary, "terminal_guard", "full")
    guard_ablation = _metrics(summary, "terminal_guard", "no_terminal_guard")
    assert guard_full["invalid_response_delivery_rate"] == 0.0
    assert guard_ablation["invalid_response_delivery_rate"] == 1.0

    full_records = [record for record in records if record["variant"] == "full"]
    ablation_records = [record for record in records if record["variant"] != "full"]
    assert len(full_records) == 30
    assert len(ablation_records) == 30
    assert all(record["passed"] is True for record in full_records)
    assert all(record["passed"] is False for record in ablation_records)


@pytest.mark.asyncio
async def test_output_writer_preserves_failed_runs(tmp_path: Path) -> None:
    records, summary = await run_all()

    jsonl_path, summary_path = _write_outputs(records, summary, tmp_path)

    written_records = [
        json.loads(line)
        for line in jsonl_path.read_text(encoding="utf-8").splitlines()
    ]
    written_summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert len(written_records) == 60
    assert sum(record["passed"] is False for record in written_records) == 30
    assert written_summary["selection_policy"].startswith("All 30 predeclared cases")
