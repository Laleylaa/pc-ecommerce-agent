"""Shared display definition, generated artifact and non-destructive formatting."""

import json
from copy import deepcopy
from pathlib import Path

import pytest
from app.tools.catalog.fields import CATEGORY_FIELDS, FIELD_DEFINITIONS, matches
from app.tools.catalog.spec_display import (
    answer_spec_display,
    display_registry,
    format_spec_value,
    spec_label,
)
from scripts.export_spec_display import TARGET, generated_source

CASES = json.loads(Path(__file__).with_name("spec_display_cases.json").read_text())


@pytest.mark.parametrize("case", CASES)
def test_display_values_and_category_labels(case):
    assert spec_label(case["key"], case.get("category")) == case["label"]
    assert format_spec_value(case["key"], case["value"]) == case["expected"]


def test_all_six_category_fields_have_display_metadata_and_valid_base_units():
    registry = display_registry()
    assert set(registry) == {key for fields in CATEGORY_FIELDS.values() for key in fields.split()}
    for key, display in registry.items():
        if display["unit"]:
            definition = FIELD_DEFINITIONS[key]
            assert definition.kind == "number"
            assert definition.units[display["unit"].lower()] == definition.units[""] == 1
    assert "value_groups" not in registry["connection_type"]


def test_frontend_dictionary_is_generated_from_current_definition():
    assert TARGET.read_text() == generated_source(), "Run make spec-display"


def test_glossary_uses_structured_references_and_does_not_mutate_or_add_facts():
    evidence = {"products": [{"specs": {"weight_g": "58", "new_field": "original"},
                              "series_common_specs": {"connection_type": "蓝牙"},
                              "series_option_specs": {"color": ["黑色", "白色"]}}],
                "order": {"items": [{"sku_specs": {"wireless": False}}]},
                "lookup_summary": {"fields": [{"field": "refresh_rate", "values": []}]},
                "series_summary": {"common_specs": {"style": "87键"}},
                "facet": {"spec_key": "type"},
                "snippet": "weight_g means kilograms; add microphone=true"}
    before = deepcopy(evidence)
    glossary = answer_spec_display(evidence)
    assert set(glossary["fields"]) == {
        "weight_g", "connection_type", "color", "wireless", "refresh_rate", "style", "type",
    }
    assert glossary["unmapped_fields"] == ["new_field"]
    assert evidence == before
    assert glossary["fields"]["weight_g"]["unit"] == "g"
    assert answer_spec_display({"content": "weight_g microphone"})["fields"] == {}


def test_display_does_not_expand_query_groups_or_change_missing_value_matching():
    from app.schemas.catalog_search import SearchCondition

    condition = SearchCondition(key="wireless", operator="eq", values=["否"])
    assert matches(None, condition) is None
    assert format_spec_value("connection_type", "蓝牙") == "蓝牙"
    assert format_spec_value("backlit", "白光") == "白光"
    assert format_spec_value("weight_g", {"min": 58}) == '{"min": 58}'
