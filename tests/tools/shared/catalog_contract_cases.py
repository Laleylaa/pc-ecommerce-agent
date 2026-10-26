"""Offline public-boundary checks, sharing the frozen contract case designs.

No real handler, model, database, Phoenix, or live runner imports.
Run explicitly through tests/run.py; live execution remains a separate entry.
"""

import asyncio
import json
from pathlib import Path

from app.tools.contracts import BoundTool, execute_bound_tool, static_tool_catalog


def contract_cases(tool_name):
    path = Path(__file__).resolve().parents[1] / tool_name / "cases.json"
    suite = json.loads(path.read_text())
    assert suite["tool_name"] == tool_name
    return [case for case in suite["cases"] if case["kind"] == "contract"]


def assert_invalid_public_arguments_stop_before_handler(tool_name, case):
    contract = static_tool_catalog().get_by_llm_name(tool_name).contract
    calls = []

    async def forbidden_handler(request):
        calls.append(request)
        raise AssertionError("Invalid arguments must not reach a business handler")

    result = asyncio.run(
        execute_bound_tool(
            BoundTool(contract, forbidden_handler),
            case["input"],
            {"user_id": 7},
        )
    )
    assert calls == []
    assert result.tool_name == tool_name
    assert result.ok is False
    assert result.output is None
    # Read the same authored assertions used by live batches, so labels cannot drift.
    payload = result.model_dump(mode="json")
    for field, comparison, expected in case["hard"]:
        assert comparison == "eq"
        actual = payload
        for key in field.split("."):
            actual = actual[key]
        assert actual == expected, (field, actual, expected)
