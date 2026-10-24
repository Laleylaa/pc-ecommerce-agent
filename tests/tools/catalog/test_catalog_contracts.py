"""Offline public-boundary checks, sharing the frozen contract case designs.

No real handler, model, database, Phoenix, or live runner imports.
Run explicitly through tests/run.py; live execution remains a separate entry.
"""

import asyncio
import json
from pathlib import Path

import pytest
from app.tools.contracts import BoundTool, execute_bound_tool, static_tool_catalog

HERE = Path(__file__).resolve().parent
CASES = [
    (suite["tool_name"], case)
    for path in sorted(HERE.glob("*_cases.json"))
    for suite in [json.loads(path.read_text())]
    for case in suite["cases"]
    if case["kind"] == "contract"
]


@pytest.mark.contract
@pytest.mark.parametrize("tool_name,case", CASES, ids=[c["id"] for _, c in CASES])
def test_invalid_public_arguments_stop_before_handler(tool_name, case):
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
    assert result.error.code == "invalid_input"
