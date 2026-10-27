"""Shared specification display metadata; never used to decide query eligibility.

Labels/units are maintained in fields.py. This module serves answer material and
the generated frontend dictionary without importing models, services or the DB.
"""

import json
import re
from collections.abc import Mapping
from typing import Any

from app.tools.catalog.fields import BOOLEAN_ALIASES, CATEGORY_ALIASES, FIELD_DEFINITIONS


def display_registry() -> dict[str, dict[str, Any]]:
    return {
        key: {
            "label": definition.display_label,
            "unit": definition.display_unit,
            "categoryLabels": dict(definition.category_labels),
            # Exact equivalence aliases only; never export query value_groups.
            "values": dict(BOOLEAN_ALIASES if definition.kind == "boolean" else definition.aliases),
        }
        for key, definition in FIELD_DEFINITIONS.items()
        if definition.display_label
    }


def spec_label(key: str, category: str | None = None) -> str:
    definition = FIELD_DEFINITIONS.get(key.strip().lower())
    if not definition or not definition.display_label:
        return key
    category = CATEGORY_ALIASES.get((category or "").lower(), category)
    return definition.category_labels.get(category, definition.display_label)


def format_spec_value(key: str, value: Any) -> str:
    """Format deterministic fallback text with the same exported display metadata."""
    if value is None or value == "":
        return "未标注"
    if isinstance(value, list):
        return " / ".join(format_spec_value(key, item) for item in value) or "未标注"
    if isinstance(value, (dict, bool)):
        raw = json.dumps(value, ensure_ascii=False)
    else:
        raw = str(value)
    definition = display_registry().get(key.strip().lower())
    if not definition or isinstance(value, dict):
        return raw
    text = raw.strip()
    if not text:
        return "未标注"
    if definition["unit"] and re.fullmatch(r"[+-]?\d+(?:\.\d+)?", text):
        return f"{text} {definition['unit']}"
    return definition["values"].get(text.lower(), raw)


def answer_spec_display(material: Any) -> dict[str, Any]:
    """Collect only structured specification references, without changing evidence.

    Unknown keys are visible in answer/trace material for explicit dictionary upkeep.
    Strings (including policy snippets and user text) are never searched for fields.
    """
    keys: set[str] = set()

    def collect(value: Any) -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                if key in {"specs", "sku_specs", "series_common_specs", "series_option_specs",
                           "common_specs", "option_specs"}:
                    if isinstance(child, Mapping):
                        keys.update(str(name) for name in child)
                    # Specification values may be structured; they aren't field dictionaries.
                    continue
                if key in {"field", "spec_key"} and isinstance(child, str):
                    keys.add(child)
                collect(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                collect(child)

    collect(material)
    registry = display_registry()
    known = {key: registry[key.strip().lower()] for key in sorted(keys)
             if key.strip().lower() in registry}
    unknown = sorted(key for key in keys if key.strip().lower() not in registry)
    return {"fields": known, "unmapped_fields": unknown}
