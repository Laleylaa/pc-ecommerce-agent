"""Shared answer text and goal-coverage checks, without model or I/O dependencies."""

from typing import Annotated

from pydantic import StringConstraints

AnswerText = Annotated[
    str,
    StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=6000),
]


def validate_goal_coverage(covered: list[str], goals: list[dict]) -> None:
    """Validate the declared coverage; semantic adequacy remains LLM3's responsibility."""
    expected = {str(g.get("id")) for g in goals}
    if len(covered) != len(set(covered)) or set(covered) != expected:
        raise ValueError("covered_goal_ids must contain every current goal exactly once")
