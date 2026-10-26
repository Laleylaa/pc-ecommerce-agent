"""Category-specific specification context for hint routing.

Pure prompt material: no database calls or candidate filtering. Examples were
checked against the current product catalog on 2026-09-29, and are not enums.
"""

from app.tools.catalog.fields import (
    CATEGORY_FIELDS,
    FIELD_DEFINITIONS,
    normalize_category_hint,
)
from app.tools.prompts.templates import load_prompt

# Small examples of recorded representation, not the set of permitted queries.
# Field membership and executable rules are always read from FIELD_DEFINITIONS.
_CATALOG_EXAMPLES = {
    "摄像头": {
        "connection_type": ("USB-A", "USB-C"),
        "field_of_view": ("103°", "78°", "90°"),
        "frame_rate": ("30fps", "60fps", "90fps"),
        "resolution": ("1080p", "1080p HDR", "1440p", "4K"),
    },
    "显示器": {
        "panel_type": ("Fast IPS", "IPS", "OLED", "VA"),
        "refresh_rate": ("144Hz", "165Hz", "240Hz", "75Hz"),
        "resolution": ("1920x1080", "2560x1440", "3440x1440", "3840x2160"),
        "response_time_ms": ("1", "3", "5"),
        "size_inch": ("24", "27", "32", "34"),
    },
    "耳机": {
        "connection_type": ("2.4G 无线", "三模", "有线", "蓝牙"),
        "enclosure_type": ("封闭式", "开放式"),
        "frequency_response": ("10Hz-40kHz", "20Hz-20kHz"),
        "type": ("入耳式", "头戴式"),
    },
    "键盘": {
        "backlit": ("RGB", "无背光", "白光"),
        "connection_type": ("2.4G 无线", "三模", "有线", "蓝牙"),
        "style": ("104 键", "60%", "75%", "87 键"),
        "switches": ("段落茶轴", "磁轴", "线性红轴", "静音红轴"),
    },
    "音箱": {
        "channels": ("2.0", "2.1", "3.1", "5.1"),
        "connection_type": ("2.4G 无线", "三模", "有线", "蓝牙"),
        "power_w": ("20", "30", "40", "50"),
        "type": ("2.0", "2.1", "Soundbar", "监听音箱"),
    },
    "鼠标": {
        "connection_type": ("2.4G 无线", "三模", "有线", "蓝牙"),
        "hand_orientation": ("右手",),
        "max_dpi": ("16000", "20000", "24000", "28000"),
        "tracking_method": ("光学",),
        "weight_g": ("58", "59", "60", "61"),
    },
}

_COMMON_RULES = load_prompt("catalog/specs_common.md")


# Contrasting examples, shared by search/facets and inherited by semantic review.
CONDITION_EXAMPLES = load_prompt("catalog/condition_examples.md")

_CATEGORY_NOTES = {
    "鼠标": load_prompt("catalog/categories/mouse.md"),
    "键盘": load_prompt("catalog/categories/keyboard.md"),
    "耳机": load_prompt("catalog/categories/headset.md"),
    "显示器": load_prompt("catalog/categories/monitor.md"),
    "音箱": load_prompt("catalog/categories/speaker.md"),
    "摄像头": load_prompt("catalog/categories/webcam.md"),
}


def _field_description(key: str, category: str | None = None, *, examples: bool = True) -> str:
    definition = FIELD_DEFINITIONS[key]
    meaning = definition.category_meanings.get(category, definition.meaning)
    parts = [f"{key}（{definition.kind}）：{meaning}"]
    if definition.units:
        units = ", ".join(
            f"{unit or '无单位'}={factor}" for unit, factor in definition.units.items()
        )
        parts.append(f"单位及基础单位倍率：{units}")
    if definition.kind == "boolean":
        parts.append("取值：是/否")
    elif examples and (values := _CATALOG_EXAMPLES.get(category, {}).get(key)):
        parts.append("记录示例：" + "、".join(values))
    if definition.aliases:
        parts.append("别名：" + ", ".join(f"{a}→{v}" for a, v in definition.aliases.items()))
    if definition.value_groups:
        parts.append(
            "能力匹配（要求→可满足的记录，非同义词）："
            + ", ".join(f"{v}→{'/'.join(g)}" for v, g in definition.value_groups.items())
        )
    return "；".join(parts)


def build_spec_context(
    category_hint: str | None = None,
    *,
    include_common_rules: bool = True,
    include_other_categories: bool = False,
    categories: tuple[str, ...] | None = None,
) -> str:
    """Return common guidance plus one category, or all categories as fallback.

    Explicit categories select resolved rules; an empty tuple selects common fields only.
    Without explicit categories, blank/unrecognized hints use the full guide.
    Raw hint text is never inserted
    into the prompt. This function does not modify a query, plan, or scope.
    """
    category = normalize_category_hint(category_hint)
    categories = (
        tuple(c for c in categories if c in CATEGORY_FIELDS)
        if categories is not None
        else (category,)
        if category in CATEGORY_FIELDS
        else tuple(CATEGORY_FIELDS)
    )
    if include_other_categories and category:
        categories = (category, *(c for c in CATEGORY_FIELDS if c != category))
    common = {
        key
        for key, definition in FIELD_DEFINITIONS.items()
        if definition.filterable and set(definition.categories) == set(CATEGORY_FIELDS)
    }
    lines = (
        ["## 统一处理规则", _COMMON_RULES, "## 条件对照示例", CONDITION_EXAMPLES]
        if include_common_rules
        else []
    )
    lines.append("## 公共字段")
    # Derive operators from the execution registry instead of a second rule table.
    for kind in ("text", "boolean", "number"):
        definition = next(d for d in FIELD_DEFINITIONS.values() if d.filterable and d.kind == kind)
        lines.append(f"{kind}支持运算：{'/'.join(sorted(definition.operators))}")
    lines.extend(_field_description(key) for key in FIELD_DEFINITIONS if key in common)
    lines.append(
        "其他已记录事实字段："
        + "/".join(key for key, d in FIELD_DEFINITIONS.items() if d.recorded and not d.filterable)
    )
    shared = {
        key
        for key, d in FIELD_DEFINITIONS.items()
        if len(categories) > 1
        and d.filterable
        and any(selected in d.categories for selected in categories)
        and len(d.categories) > 1
        and not d.category_meanings
        and key not in common
    }
    if shared:
        lines.append("## 跨品类共用规格")
        for key, definition in FIELD_DEFINITIONS.items():
            if key not in shared:
                continue
            lines.append(_field_description(key) + "；适用：" + "/".join(definition.categories))
            examples_by_category = {}
            for selected in definition.categories:
                if selected not in categories:
                    continue
                if include_other_categories and category and selected != category:
                    continue
                if values := _CATALOG_EXAMPLES.get(selected, {}).get(key):
                    examples_by_category.setdefault(values, []).append(selected)
            for values, owners in examples_by_category.items():
                lines.append("记录示例（" + "/".join(owners) + "）：" + "、".join(values))
    for selected in categories:
        suffix = (
            "备用规格" if include_other_categories and category and selected != category else "规格"
        )
        lines.append(f"## {selected}{suffix}")
        lines.append("支持字段：" + CATEGORY_FIELDS[selected])
        lines.extend(
            _field_description(key, selected, examples=not category or selected == category)
            for key in CATEGORY_FIELDS[selected].split()
            if key not in common | shared
        )
        lines.append("边界：" + _CATEGORY_NOTES[selected])
    lines.append("## 未记录能力")
    lines.extend(
        f"{key}：{d.meaning}；询问保留缺失，具体规格硬筛选标记spec_not_carried，用途模糊标记requirement_needs_clarification。"
        for key, d in FIELD_DEFINITIONS.items()
        if not d.recorded
    )
    return "\n".join(lines)
