"""Report executable plan entries without inferring requirements from audit prose."""

from dataclasses import dataclass, field

from app.schemas.catalog_search import SearchCondition, SearchPlan
from app.tools.catalog_search.outcomes import QueryIssue, ResultReason


@dataclass
class PlannedRequirement:
    source: str
    paths: list[tuple[str | int, ...]] = field(default_factory=list)
    conditions: list[SearchCondition] = field(default_factory=list)
    declared_reasons: list[ResultReason] = field(default_factory=list)

    def owns(self, issue: QueryIssue) -> bool:
        path = tuple(issue.details.get("path", ()))
        return (
            any(path[: len(p)] == p for p in self.paths)
            or (
                issue.reason == ResultReason.CONFLICTING_CONDITIONS
                and any(c.key == issue.details.get("field") for c in self.conditions)
            )
        )


def planned_requirements(plan: SearchPlan) -> list[PlannedRequirement]:
    # Paths identify plan entries; audit notes cannot establish semantic identity.
    entries: list[PlannedRequirement] = []

    def add(source: str, path: tuple[str | int, ...]) -> PlannedRequirement:
        row = PlannedRequirement(source, paths=[path])
        entries.append(row)
        return row

    for index, condition in enumerate(plan.conditions):
        add(condition.evidence, ("conditions", index)).conditions.append(condition)
    for index, requirement in enumerate(plan.unresolved_requirements):
        row = add(requirement.evidence, ("unresolved_requirements", index))
        reason = ResultReason(requirement.reason)
        if reason not in row.declared_reasons:
            row.declared_reasons.append(reason)
    unsupported = (
        ResultReason.FILTER_DATA_UNAVAILABLE
        if plan.unsupported_reason
        in {
            ResultReason.DATA_INSUFFICIENT,
            ResultReason.FILTER_DATA_UNAVAILABLE,
        }
        else ResultReason.REQUEST_NOT_SUPPORTED
    )
    for name, reason in (
        ("clarification_requirements", ResultReason.REQUIREMENT_NEEDS_CLARIFICATION),
        ("unsupported_requirements", unsupported),
    ):
        for index, source in enumerate(getattr(plan, name)):
            row = add(source, (name, index))
            if reason not in row.declared_reasons:
                row.declared_reasons.append(reason)
    return entries


def declaration_issues(plan: SearchPlan) -> list[QueryIssue]:
    """Keep unresolved declarations blocking; prose is not a classification validator."""
    issues = []
    for requirement in planned_requirements(plan):
        for reason in requirement.declared_reasons:
            message = (
                "请将以下要求具体化为参数："
                if reason == ResultReason.REQUIREMENT_NEEDS_CLARIFICATION
                else "以下要求无法执行："
            )
            issues.append(QueryIssue(
                reason,
                message + requirement.source + "。未执行查询。",
                {"requirements": [requirement.source], "path": list(requirement.paths[0])},
            ))
    return issues


def requirement_assessment(plan: SearchPlan, issues: list[QueryIssue]) -> dict:
    """Describe plan executability, not semantic coverage of the user's request."""
    items = []
    for index, requirement in enumerate(planned_requirements(plan)):
        related = [issue for issue in issues if requirement.owns(issue)]
        item = {
            "id": f"requirement_{index + 1}",
            "source": requirement.source,
            "source_paths": [list(path) for path in requirement.paths],
            "state": "blocked" if related else "executable",
            "reasons": list(dict.fromkeys(issue.reason.value for issue in related)),
        }
        fields = list(dict.fromkeys(c.key for c in requirement.conditions))
        strengths = list(dict.fromkeys(c.strength for c in requirement.conditions))
        if fields:
            item.update(fields=fields, strengths=strengths)
            if len(fields) == 1:
                item["field"] = fields[0]
            if len(strengths) == 1:
                item["strength"] = strengths[0]
        items.append(item)
    executable = sum(item["state"] == "executable" for item in items)
    coverage = (
        "not_assessed"
        if any(i.reason == ResultReason.INVALID_PLAN for i in issues)
        else "not_applicable"
        if not items
        else "all"
        if executable == len(items)
        else "partial"
        if executable
        else "none"
    )
    return {"scope": "plan_entries", "coverage": coverage, "items": items}
