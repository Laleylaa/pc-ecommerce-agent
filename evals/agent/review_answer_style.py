
"""Live answer-only review on identical handoffs; not a full-runtime cost benchmark."""

import argparse
import asyncio
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from app.agent.loop import independent_answer
from app.core.config import get_settings
from app.core.loop_budget import LoopBudget
from app.core.loop_models import loop_models

from evals.agent.answer_style_cases import style_cases
from evals.legacy.eval_goal_tool_flow import implementation_manifest
from evals.support.paths import RESULTS


class RecordingAnswerModel:
    """Retain synthetic outputs so protocol repairs can be inspected, not guessed."""

    def __init__(self, model):
        self.model = model
        self.responses = []

    async def ainvoke(self, messages):
        response = await self.model.ainvoke(messages)
        self.responses.append(response.content)
        return response


async def main(args):
    settings = get_settings()
    if not settings.llm_api_key:
        raise SystemExit("No model configured")
    current = independent_answer.ANSWER_PROMPT
    baseline = Path(args.baseline_prompt).read_text() if args.baseline_prompt else None
    cases = [c for c in style_cases() if not args.case or c["id"] in args.case]
    if not cases:
        raise SystemExit("No cases selected")
    directory = RESULTS / "agent_loop"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (
        datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-answer-style-" + uuid4().hex[:8] + ".json"
    )
    prompts = {"current": current, **({"before": baseline} if baseline else {})}
    report = {
        "manifest": implementation_manifest(),
        "model": settings.llm_model,
        "event_loop": type(asyncio.get_running_loop()).__module__,
        "scope": "answer only; synthetic fixed handoffs; no DB or domain execution",
        "prompts": {
            name: {"text": value, "sha256": hashlib.sha256(value.encode()).hexdigest()}
            for name, value in prompts.items()
        },
        "cases": [{**c, "facts": c["facts"].model_dump(mode="json")} for c in cases],
        "results": [],
    }
    for repeat in range(args.repeat):
        for case in cases:
            names = ["before", "current"] if baseline and case["paired"] else ["current"]
            if repeat % 2:
                names.reverse()
            for name in names:
                row = {"id": case["id"], "prompt": name, "repeat": repeat}
                budget = LoopBudget()
                usage = None
                started = asyncio.get_running_loop().time()
                try:
                    # Both variants use the identical evidence, model, guard and JSON protocol.
                    async with loop_models(settings, budget) as (_, model, usage):
                        recorded = RecordingAnswerModel(model)
                        with patch.object(independent_answer, "ANSWER_PROMPT", prompts[name]):
                            draft, mode = await independent_answer.answer(
                                case["facts"], recorded, budget,
                            )
                        row["model_responses"] = recorded.responses
                    row.update(mode=mode, draft=draft.model_dump(mode="json"))
                except Exception as exc:
                    row["error_type"] = type(exc).__name__
                row["seconds"] = round(asyncio.get_running_loop().time() - started, 3)
                row["usage"] = usage.summary() if usage else None
                report["results"].append(row)
                path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
                print(case["id"], name, repeat, row.get("mode", row.get("error_type")), flush=True)
    print("Artifacts:", path, flush=True)
    return int(any(r.get("mode") != "model" for r in report["results"]))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", required=True, action="store_true")
    parser.add_argument("--baseline-prompt")
    parser.add_argument("--repeat", type=int, choices=(1, 3), default=3)
    parser.add_argument("--case", action="append")
    args = parser.parse_args()
    import uvloop

    raise SystemExit(uvloop.run(main(args)))
