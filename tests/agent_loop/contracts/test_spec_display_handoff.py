"""Exercise actual answer invocation with a local model double and original evidence."""

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from app.agent.loop.independent_answer import AnswerInput, answer
from app.core.loop_budget import LoopBudget


@pytest.mark.asyncio
async def test_answer_receives_shared_display_rules_without_changing_raw_evidence():
    evidence = [{"evidence_id": "e1", "result": {"ok": True, "output": {
        "order": {"items": [{"sku_specs": {"weight_g": "58", "wireless": "否"}}]},
    }}}]
    original = deepcopy(evidence)
    facts = AnswerInput(message="我的鼠标多重", goals=[{"id": "g1", "evidence_ids": ["e1"]}],
                        evidence=evidence, stop_reason="completed",
                        spec_display={"fields": {"weight_g": {"unit": "kg"}}})
    requests = []

    async def invoke(messages):
        materials = json.loads(messages[1].content)
        requests.append(materials)
        assert materials["evidence"] == original
        assert materials["spec_display"]["fields"]["weight_g"]["unit"] == "g"
        assert materials["spec_display"]["fields"]["wireless"]["label"] == "支持无线"
        assert "不是商品事实" in messages[0].content
        return SimpleNamespace(content=json.dumps({
            "response": "重量为 58 g。", "covered_goal_ids": ["g1"], "evidence_ids": ["e1"],
        }))

    _, mode = await answer(facts, SimpleNamespace(ainvoke=invoke), LoopBudget())
    assert mode == "model" and len(requests) == 1
    assert facts.evidence == original
