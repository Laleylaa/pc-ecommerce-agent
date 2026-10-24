"""Semantic review instructions; execution policy remains in structured_planning."""

import json

from app.schemas.tool_plan_review import ToolPlanReview
from app.tools.prompts.templates import render_prompt


def build_review_prompt(domain_prompt: str) -> str:
    return render_prompt(
        "shared/plan_review.md",
        domain_context=domain_prompt,
        output_schema=json.dumps(ToolPlanReview.model_json_schema(), ensure_ascii=False),
    )
