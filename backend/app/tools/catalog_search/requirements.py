"""One source requirement owns its predicates, declarations and validation outcome."""

from dataclasses import dataclass, field

from app.schemas.catalog_search import SearchCondition, SearchPlan
from app.tools.catalog_search.outcomes import QueryIssue, ResultReason
from app.tools.catalog_search.rules import compact_text


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
            or compact_text(self.source)
            in {compact_text(s) for s in issue.details.get("requirements", [])}
            or (
                issue.reason == ResultReason.CONFLICTING_CONDITIONS
                and any(c.key == issue.details.get("field") for c in self.conditions)
            )
        )


def planned_requirements(plan: SearchPlan) -> list[PlannedRequirement]:
    # Exact source identity, allowing only the same normalization as source validation.
    # Do not guess semantic equivalence using substrings or strip negation/units.
    by_source: dict[str, PlannedRequirement] = {}

    def add(source: str, path: tuple[str | int, ...]) -> PlannedRequirement:
        row = by_source.setdefault(compact_text(source), PlannedRequirement(source))
        row.paths.append(path)
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
    return list(by_source.values())


def declaration_issues(plan: SearchPlan, existing: list[QueryIssue]) -> list[QueryIssue]:
    """Attach declarations to requirements; do not repeat a known capability failure."""
    unresolved: dict[ResultReason, list[str]] = {}
    errors: list[QueryIssue] = []
    for requirement in planned_requirements(plan):
        known = {issue.reason for issue in existing if requirement.owns(issue)}
        # Conflicting classifications are model output errors, not two user problems.
        reasons = set(requirement.declared_reasons)
        if len(reasons) > 1 or (
            ResultReason.REQUIREMENT_NEEDS_CLARIFICATION in reasons
            and ResultReason.FILTER_DATA_UNAVAILABLE in known
        ):
            errors.extend([
                QueryIssue(
                    ResultReason.INVALID_PLAN,
                    "同一原文要求被归入不同原因；保留原要求并给出唯一准确归类。",
                    {"path": list(requirement.paths[-1]),
                     "error_code": "conflicting_requirement_classification"},
                )
            ])
            continue
        for reason in requirement.declared_reasons:
            if reason in known or (
                reason == ResultReason.REQUEST_NOT_SUPPORTED
                and ResultReason.FILTER_DATA_UNAVAILABLE in known
            ):
                continue
            unresolved.setdefault(reason, []).append(requirement.source)
    return errors + [
        QueryIssue(
            reason,
            (
                "请将以下要求具体化为参数："
                if reason == ResultReason.REQUIREMENT_NEEDS_CLARIFICATION
                else "以下要求无法执行："
            )
            + "；".join(sources)
            + "。未执行查询。",
            {"requirements": sources},
        )
        for reason, sources in unresolved.items()
    ]


def requirement_assessment(plan: SearchPlan, issues: list[QueryIssue]) -> dict:
    """Assess source requirements once, preserving every predicate and source path."""
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
        # A single source span may express a range or several predicates. Keep
        # their links and fields instead of silently discarding all but the first.
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
    return {"scope": "extracted_requirements", "coverage": coverage, "items": items}
