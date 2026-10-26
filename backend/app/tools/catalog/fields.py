"""Pure catalog field semantics and typed value matching, shared by tools."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from app.schemas.catalog_search import SearchCondition


def hard_condition_verdict(
    checks: Iterable[tuple[str, str]],
) -> Literal["match", "mismatch", "unknown"]:
    """Only requirements decide eligibility; a known failure excludes unknown rows too."""
    verdicts = [verdict for strength, verdict in checks if strength == "requirement"]
    if "mismatch" in verdicts:
        return "mismatch"
    return "unknown" if "unknown" in verdicts else "match"


def preference_rank(checks: Iterable[tuple[str, str]]) -> tuple[int, int]:
    """More confirmed preferences first, then known differences before missing facts."""
    verdicts = [verdict for strength, verdict in checks if strength == "preference"]
    return -verdicts.count("match"), verdicts.count("unknown")


CATEGORY_FIELDS = {
    "鼠标": "connection_type wireless max_dpi weight_g hand_orientation tracking_method color",
    "键盘": "connection_type wireless switches style backlit tenkeyless color",
    "耳机": "connection_type wireless type enclosure_type microphone frequency_response color",
    "显示器": "size_inch resolution refresh_rate panel_type response_time_ms color",
    "音箱": "connection_type wireless channels power_w type color",
    "摄像头": "connection_type resolution frame_rate field_of_view microphone color",
}
CATEGORY_ALIASES = {
    "mouse": "鼠标",
    "keyboard": "键盘",
    "headset": "耳机",
    "headphone": "耳机",
    "monitor": "显示器",
    "speaker": "音箱",
    "webcam": "摄像头",
}


def normalize_category_hint(value: str | None) -> str | None:
    """A hint selects prompt context only; unknown hints use the full context."""
    value = value.strip() if value else ""
    category = CATEGORY_ALIASES.get(value.casefold(), value)
    return category if category in CATEGORY_FIELDS else None


def product_term_aliases(value: str) -> tuple[str, ...]:
    """Controlled name/spec aliases, without interpreting or splitting a query."""
    normalized = value.strip().casefold()
    aliases = {value}
    if category := CATEGORY_ALIASES.get(
        normalized, normalized if normalized in CATEGORY_FIELDS else None
    ):
        aliases.add(category)
        aliases.update(alias for alias, target in CATEGORY_ALIASES.items() if target == category)
    for key in ("brand", "color"):
        values = FIELD_DEFINITIONS[key].aliases
        canonical = values.get(normalized, normalized)
        if normalized in values or canonical in values.values():
            aliases.add(canonical)
            aliases.update(alias for alias, target in values.items() if target == canonical)
    return tuple(sorted(aliases))


@dataclass(frozen=True)
class FieldDefinition:
    """One field's value semantics, capability and prompt guidance."""

    kind: Literal["text", "boolean", "number"] = "text"
    filterable: bool = True
    recorded: bool = True
    categories: tuple[str, ...] = ()
    units: Mapping[str, int] = field(default_factory=dict)
    aliases: Mapping[str, str] = field(default_factory=dict)
    meaning: str = ""
    examples: tuple[str, ...] = ()
    value_groups: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    category_meanings: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "units", MappingProxyType(dict(self.units)))
        object.__setattr__(self, "aliases", MappingProxyType(dict(self.aliases)))
        object.__setattr__(self, "value_groups", MappingProxyType(dict(self.value_groups)))
        object.__setattr__(
            self, "category_meanings", MappingProxyType(dict(self.category_meanings))
        )
        if self.category_meanings:
            object.__setattr__(
                self,
                "meaning",
                "；".join(
                    f"{category}:{meaning}" for category, meaning in self.category_meanings.items()
                ),
            )

    @property
    def operators(self) -> frozenset[str]:
        if not self.filterable:
            return frozenset()
        operators = {"eq", "ne", "in", "not_in"}
        if self.kind == "number":
            operators.update({"gt", "gte", "lt", "lte"})
        return frozenset(operators)


# Category membership/order is declared once in CATEGORY_FIELDS above. Other
# field semantics are declared here; all consumers use the derived registry.
_FIELD_DEFINITIONS = {
    "connection_type": FieldDefinition(
        aliases={
            "bluetooth": "蓝牙",
            "bt": "蓝牙",
            "wireless": "无线",
            "wired": "有线",
            "2.4g": "2.4g无线",
            "2.4ghz": "2.4g无线",
            "tri-mode": "三模",
            "type-c": "usb-c",
            "type-a": "usb-a",
        },
        meaning="具体连接方式：蓝牙、2.4G、三模、USB-A、USB-C、有线等",
        value_groups={
            "无线": ("蓝牙", "2.4g无线", "三模"),
            "蓝牙": ("三模",),
            "2.4g无线": ("三模",),
            "有线": ("usb", "usb-a", "usb-c", "三模"),
            "usb": ("usb-a", "usb-c"),
        },
    ),
    "wireless": FieldDefinition(
        kind="boolean", meaning="仅表示是否无线；不能替代蓝牙或2.4G等具体连接要求"
    ),
    "max_dpi": FieldDefinition(kind="number", units={"": 1, "dpi": 1}, meaning="最高DPI"),
    "weight_g": FieldDefinition(
        kind="number", units={"": 1, "g": 1, "克": 1, "kg": 1000, "千克": 1000}, meaning="重量"
    ),
    "hand_orientation": FieldDefinition(
        aliases={"right": "右手", "left": "左手"}, meaning="适用手型", examples=("右手", "左手")
    ),
    "tracking_method": FieldDefinition(
        aliases={"optical": "光学"}, meaning="追踪方式", examples=("光学",)
    ),
    "color": FieldDefinition(
        aliases={"black": "黑色", "white": "白色", "silver": "银色", "pink": "粉色"}, meaning="颜色"
    ),
    "switches": FieldDefinition(
        aliases={"red": "红轴", "brown": "茶轴", "blue": "青轴", "magnetic": "磁轴"},
        meaning="实际轴体类型；系列名称即使含轴字也只用于name，不能据此推断轴体条件",
        examples=("线性红轴", "段落茶轴", "静音红轴", "磁轴"),
    ),
    "style": FieldDefinition(
        meaning="键盘配列/布局（layout），如75%、87键；标准版/无线版等版本名不属于此字段"
    ),
    "backlit": FieldDefinition(
        meaning="背光模式；有无背光也可用是/否筛选",
        aliases={"有背光": "是", "带背光": "是"},
        examples=("RGB", "白光", "无背光"),
        value_groups={"是": ("RGB", "白光"), "否": ("无背光",)},
    ),
    "tenkeyless": FieldDefinition(kind="boolean", meaning="是否无独立数字键区；不代替具体配列"),
    "type": FieldDefinition(
        category_meanings={
            "耳机": "佩戴类型(头戴式/入耳式)",
            "音箱": "产品类型(2.0/2.1/Soundbar/监听音箱)",
        }
    ),
    "enclosure_type": FieldDefinition(
        meaning="耳机声学结构；佩戴类型另用type", examples=("封闭式", "开放式")
    ),
    "microphone": FieldDefinition(kind="boolean", meaning="是否有麦克风"),
    "frequency_response": FieldDefinition(meaning="频率响应，按完整记录文本匹配，不做区间推断"),
    "size_inch": FieldDefinition(
        kind="number", units={"": 1, "inch": 1, "英寸": 1, "寸": 1, '"': 1}, meaning="屏幕尺寸"
    ),
    "resolution": FieldDefinition(
        aliases={
            "2k": "2560x1440",
            "1440p": "2560x1440",
            "4k": "3840x2160",
            "1080p": "1920x1080",
            "fhd": "1920x1080",
        },
        meaning="分辨率，支持像素尺寸及别名",
    ),
    "refresh_rate": FieldDefinition(
        kind="number", units={"": 1, "hz": 1, "赫兹": 1}, meaning="刷新率"
    ),
    "panel_type": FieldDefinition(meaning="面板类型", examples=("IPS", "VA", "TN", "OLED")),
    "response_time_ms": FieldDefinition(
        kind="number", units={"": 1, "ms": 1, "毫秒": 1}, meaning="记录的响应时间"
    ),
    "channels": FieldDefinition(
        meaning="声道配置，文本匹配", examples=("2.0", "2.1", "3.1", "5.1")
    ),
    "power_w": FieldDefinition(kind="number", units={"": 1, "w": 1, "瓦": 1}, meaning="功率"),
    "frame_rate": FieldDefinition(kind="number", units={"": 1, "fps": 1, "帧": 1}, meaning="帧率"),
    "field_of_view": FieldDefinition(
        kind="number", units={"": 1, "°": 1, "度": 1}, meaning="视场角"
    ),
    "brand": FieldDefinition(
        meaning="品牌",
        categories=tuple(CATEGORY_FIELDS),
        aliases={
            "罗技": "logitech",
            "雷蛇": "razer",
            "赛睿": "steelseries",
            "索尼": "sony",
            "戴尔": "dell",
            "华硕": "asus",
            "漫步者": "edifier",
            "圆刚": "avermedia",
        },
    ),
    "price": FieldDefinition(
        kind="number",
        categories=tuple(CATEGORY_FIELDS),
        units={"": 1, "元": 1, "cny": 1},
        meaning="价格",
    ),
    "stock": FieldDefinition(
        kind="number", categories=tuple(CATEGORY_FIELDS), units={"": 1, "件": 1}, meaning="库存数量"
    ),
    "title": FieldDefinition(filterable=False),
    "category": FieldDefinition(filterable=False),
    "specifications": FieldDefinition(filterable=False),
    "variants": FieldDefinition(filterable=False),
    "availability": FieldDefinition(filterable=False),
    "sales_count": FieldDefinition(filterable=False),
    "sku_sales_count": FieldDefinition(filterable=False),
    "description": FieldDefinition(filterable=False),
    "video_input": FieldDefinition(
        filterable=False,
        recorded=False,
        meaning="视频输入能力，不能用connection_type替代；缺少记录时不能确认支持",
    ),
}
FIELD_DEFINITIONS = MappingProxyType(
    {
        key: replace(
            definition,
            categories=tuple(
                category for category, fields in CATEGORY_FIELDS.items() if key in fields.split()
            )
            or definition.categories,
        )
        for key, definition in _FIELD_DEFINITIONS.items()
    }
)
NUMERIC_UNITS = {
    key: definition.units
    for key, definition in FIELD_DEFINITIONS.items()
    if definition.kind == "number"
}
ALIASES = {
    key: definition.aliases for key, definition in FIELD_DEFINITIONS.items() if definition.aliases
}
FILTER_FIELDS = frozenset(
    key for key, definition in FIELD_DEFINITIONS.items() if definition.filterable
)
FACT_FIELDS = frozenset(key for key, definition in FIELD_DEFINITIONS.items() if definition.recorded)
BOOLEAN_ALIASES = {
    **dict.fromkeys(("yes", "true", "1", "是", "有", "支持"), "是"),
    **dict.fromkeys(("no", "false", "0", "否", "无", "不支持"), "否"),
}


def normalize(key: str, value: str) -> str:
    value = re.sub(r"\s+", "", value.casefold()).replace("×", "x")
    if (definition := FIELD_DEFINITIONS.get(key)) and (
        definition.kind == "boolean" or definition.value_groups
    ):
        value = BOOLEAN_ALIASES.get(value, value)
    return ALIASES.get(key, {}).get(value, value)


def number(key: str, value: str) -> Decimal | None:
    match = re.fullmatch(r"([+-]?\d+(?:\.\d+)?)\s*([^\d\s]*)", value.strip().casefold())
    if not match or match[2] not in NUMERIC_UNITS.get(key, {}):
        return None
    try:
        return Decimal(match[1]) * Decimal(NUMERIC_UNITS[key][match[2]])
    except InvalidOperation:
        return None


def equal(key: str, actual: str, wanted: str) -> bool:
    actual, wanted = normalize(key, actual), normalize(key, wanted)
    if key in NUMERIC_UNITS:
        left, right = number(key, actual), number(key, wanted)
        return left is not None and right is not None and left == right
    if key == "switches":
        families = {"红轴": {"红轴", "线性红轴", "静音红轴"}, "茶轴": {"茶轴", "段落茶轴"}}
        return actual in families.get(wanted, {wanted})
    if definition := FIELD_DEFINITIONS.get(key):
        if group := definition.value_groups.get(wanted):
            return actual == wanted or actual in {normalize(key, value) for value in group}
    return actual == wanted


def same_recorded_value(key: str, left: str, right: str) -> bool:
    """Aliases denote the same fact; satisfying a broader requirement does not."""
    if key in NUMERIC_UNITS:
        return equal(key, left, right)
    return normalize(key, left) == normalize(key, right)


def matches(actual: str | None, condition: SearchCondition) -> bool | None:
    """Unknown stays unknown even for exclusions (missing != evidence of absence)."""
    if actual is None or not actual.strip():
        return None
    op = condition.operator
    if op in {"eq", "ne", "in", "not_in"}:
        result = any(equal(condition.key, actual, wanted) for wanted in condition.values)
        return not result if op in {"ne", "not_in"} else result
    left = number(condition.key, actual)
    right = number(condition.key, condition.values[0])
    if left is None or right is None:
        return None
    return {"gt": left > right, "gte": left >= right, "lt": left < right, "lte": left <= right}[op]


def explain_conditions(conditions: Iterable[SearchCondition]) -> dict:
    """Explain the executable predicates, without interpreting the user's request."""
    explained = []
    for index, condition in enumerate(conditions):
        definition = FIELD_DEFINITIONS.get(condition.key)
        if definition is None:
            continue
        candidates = set(condition.values) | set(definition.examples)
        candidates.update(definition.value_groups)
        candidates.update(value for group in definition.value_groups.values() for value in group)
        candidates = sorted({normalize(condition.key, value) for value in candidates})
        explained.append({
            "plan_path": f"/conditions/{index}", "field": condition.key,
            "operator": condition.operator, "strength": condition.strength,
            "meaning": definition.meaning, "units": dict(definition.units),
            "record_examples": [{"value": value, "matches": matches(value, condition)}
                                for value in candidates],
        })
    return {
        "requirements": "all_on_same_sku", "preferences": "ranking_only",
        "missing_record_value": "complete_catalog_absent_capability; conflicts_remain_unknown",
        "examples_only": True, "conditions": explained,
    }


def canonical_field(key: str) -> str:
    normalized = key.strip().lower().replace(" ", "_").replace("-", "_")
    aliases = {
        "connection": "connection_type",
        "connectivity": "connection_type",
        "wireless": "wireless",
        "wired": "connection_type",
        "连接": "connection_type",
        "连接方式": "connection_type",
        "无线": "connection_type",
        "有线": "connection_type",
        "dpi": "max_dpi",
        "maxdpi": "max_dpi",
        "max_dpi": "max_dpi",
        "重量": "weight_g",
        "克重": "weight_g",
        "左右手": "hand_orientation",
        "手型": "hand_orientation",
        "传感器": "tracking_method",
        "追踪方式": "tracking_method",
        "分辨率": "resolution",
        "刷新率": "refresh_rate",
        "赫兹": "refresh_rate",
        "hz": "refresh_rate",
        "尺寸": "size_inch",
        "大小": "size_inch",
        "面板": "panel_type",
        "响应时间": "response_time_ms",
        "轴": "switches",
        "轴体": "switches",
        "背光": "backlit",
        "灯光": "backlit",
        "layout": "style",
        "配列": "style",
        "布局": "style",
        "小键盘": "tenkeyless",
        "数字键盘": "tenkeyless",
        "外壳": "enclosure_type",
        "结构": "enclosure_type",
        "麦克风": "microphone",
        "麦": "microphone",
        "声道": "channels",
        "频响": "frequency_response",
        "频率响应": "frequency_response",
        "功率": "power_w",
        "瓦数": "power_w",
        "帧率": "frame_rate",
        "视场角": "field_of_view",
        "广角": "field_of_view",
        "颜色": "color",
        "类型": "type",
    }
    return aliases.get(normalized, normalized)
