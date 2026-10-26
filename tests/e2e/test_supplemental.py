"""Offline checks for new-case selection and actual boundary exposure."""

import json

import pytest

from tests.e2e.check import corrected_path
from tests.e2e.exposure import evaluate
from tests.e2e.isolated import (
    SUPPLEMENTAL_CASES,
    policy_documents,
    supplemental_fixtures,
)
from tests.e2e.suite import digest, load_cases, write


def test_supplemental_selection_contains_only_new_cases():
    ids = [cid for values in SUPPLEMENTAL_CASES.values() for cid in values]
    cases = load_cases(selected=ids)
    assert len(ids) == len(set(ids)) == 14
    assert sum(len(c["turns"]) for c in cases) == 20
    assert all(c["id"] in SUPPLEMENTAL_CASES[c["profile"]] for c in cases)
    assert set(ids) == {f"B{i}" for i in range(17, 21)} | {f"E{i}" for i in range(20, 30)}


@pytest.mark.parametrize("case_id", ["E23", "E24", "E25", "E26", "E28", "E29", "B19", "B20"])
def test_boundary_setup_without_actual_tool_evidence_cannot_pass(case_id):
    assert evaluate(case_id, [])[0] is False


def test_supplemental_facts_expose_missing_stock_and_policy():
    assert policy_documents("empty_policy") == []
    rows = supplemental_fixtures()
    assert [(r["id"], r["stock"]) for r in rows] == [(601, 7), (602, 8), (603, 0)]


@pytest.mark.parametrize("host,expected", [
    ("support.microsoft.com", True),
    ("api.support.office.com", True),
    ("support.microsoft.com.example.invalid", False),
    ("learn.microsoft.com", False),
])
def test_official_source_hosts_include_support_alias_but_not_community_or_spoof(host, expected):
    spans = [{"attributes": {
        "tool.name": "knowledge_search",
        "output.value": json.dumps({"ok": True, "output": {
            "documents": [{"url": f"https://{host}/article/pair-bluetooth"}],
        }}),
    }}]
    assert evaluate("B20", spans)[0] is expected


@pytest.mark.parametrize("invalid", [None, "fingerprint", "expected"])
def test_annotation_path_repair_preserves_frozen_expectation(tmp_path, invalid):
    frozen = tmp_path / "evidence/frozen.json"
    write(frozen, {"original": "unchanged"})
    schema = tmp_path / "evidence/source/order.py"
    schema.parent.mkdir()
    schema.write_text("class OrderSummary: id: int\n")
    original = {"id": "orders", "path": "orders.*.order_id", "op": "set_eq", "expected": [9003]}
    document = {
        "revision": "annotation1", "frozen_sha256": digest(frozen),
        "corrections": [{
            "case_id": "B18", "turn": 1, "original": dict(original),
            "corrected_path": "orders.*.id", "reason": "schema field typo",
            "schema_evidence": "evidence/source/order.py#OrderSummary.id",
        }],
    }
    if invalid == "fingerprint":
        document["frozen_sha256"] = "different"
    if invalid == "expected":
        document["corrections"][0]["original"]["expected"] = [9002]
    write(tmp_path / "checks/path_corrections.json", document)
    if invalid:
        with pytest.raises(ValueError):
            corrected_path(tmp_path, "B18", 1, original)
    else:
        repaired = corrected_path(tmp_path, "B18", 1, original)
        assert repaired["path"] == "orders.*.id"
        assert repaired["expected"] == [9003]
        assert original["path"] == "orders.*.order_id"
        assert digest(frozen) == document["frozen_sha256"]


@pytest.mark.parametrize('extra,expected', [(None, True), ('stock', False)])
def test_changed_request_cannot_inherit_assistant_suggested_stock(extra, expected):
    conditions = [{'key': 'price'}, {'key': 'connection_type'}]
    if extra:
        conditions.append({'key': extra})
    spans = [{'attributes': {'tool.name': 'catalog_search', 'output.value': json.dumps({
        'ok': True, 'output': {'match_kind': 'exact', 'returned_count': 1,
                              'query_plan': {'conditions': conditions}},
    })}}]
    assert evaluate('E21', spans, 2)[0] is expected
