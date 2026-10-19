"""Category-specific specification context for hint routing.

Pure prompt material: no database calls or candidate filtering. Examples were
checked against the current product catalog on 2026-09-29, and are not enums.
"""

from app.tools.catalog_fields import (
    CATEGORY_FIELDS,
    FIELD_DEFINITIONS,
    normalize_category_hint,
)

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

_COMMON_RULES = """你在解析单目标商品查询，以下是字段说明，不是商品事实或可售配置。
category_hint只帮助选取说明，不能作为新增筛选条件、替代原文或证明商品品类。
没有可靠hint时使用统一说明；只按原文明确品类填写category，不凭型号猜测。
若hint与请求不符，不得删除要求或改用相近字段；正式Prompt同时提供其他品类的备用说明。
用于发现商品的规格硬要求保留conditions，标题词按name/keywords分工；事实询问保留requested_fields。
未记录参数：事实询问仍定位商品并标记缺失；硬筛选标记filter_data_unavailable，不能改成询问。
所有条件须在同一个SKU上成立；记录示例不是取值白名单，也不表示能任意组合。
同一字段同时满足多个能力时分成多个eq条件（AND）；任一满足才用in（OR）。
不能为绕过冲突校验把同时满足改为in；否定条件保留ne/not_in。
示例之外的合法值仍保留，如500Hz；不能替换成当前记录中的最近值。
品牌/型号/系列名用于定位，不推断轴体、配列或无线能力；名称查找不依赖预先知道品类。
boolean用是/否（兼容true/false等别名）；数值保留单位、端点与否定，文本保留具体型号规格。
普通选购不加库存条件；问有没有货是事实询问，明确要求现货才筛库存。
返回完整已记录资料；支持查询某字段不等于每个商品都有该事实。"""


# Contrasting examples, shared by search/facets and inherited by semantic review.
CONDITION_EXAMPLES = """以下是成对条件片段，不是完整输出，也不代表店内商品：
1. “蓝牙和2.4G都要有”：
conditions=[{"key":"connection_type","operator":"eq","values":["蓝牙"],"strength":"requirement","evidence":"蓝牙和2.4G都要有"},
{"key":"connection_type","operator":"eq","values":["2.4G"],"strength":"requirement","evidence":"蓝牙和2.4G都要有"}]。
两项AND；用in表达此原文是错误，审核应revise。上述两个eq正确，审核应approve。
2. “蓝牙或者2.4G都行”：
conditions=[{"key":"connection_type","operator":"in","values":["蓝牙","2.4G"],"strength":"requirement","evidence":"蓝牙或者2.4G都行"}]。
这是OR；不能因为出现“都”字就判AND。上述in正确，审核应approve。
3. “蓝牙必须有，最好也支持2.4G”：
conditions=[{"key":"connection_type","operator":"eq","values":["蓝牙"],"strength":"requirement","evidence":"蓝牙必须有"},
{"key":"connection_type","operator":"eq","values":["2.4G"],"strength":"preference","evidence":"最好也支持2.4G","preference_evidence":"最好也支持2.4G"}]。
前者决定准入，后者只排序；把两项都当硬条件或都当任选均应revise。
4. “三模鼠标，蓝牙也要支持”：eq三模 + eq蓝牙可正确保留，审核应approve。
这些是能力谓词，三模记录可同时满足两项；不能按字符串必须同时等于两种值而误判冲突。
反向不成立：“蓝牙和2.4G都要有”不能直接改写成“三模”，不得增加原文没要求的配置。
5. “支持无线充电”：保留unresolved_requirements=
[{"evidence":"支持无线充电","reason":"filter_data_unavailable"}]。
字段体系缺少此能力；正确停止计划应approve，替换成wireless=是应revise。
“支持无线连接”才按连接能力表达，不能把连接和充电混为一谈。
审核以上片段时仍要核对实际原文的其他要求；生成时补齐schema要求，不能复制无关条件。"""

_CATEGORY_NOTES = {
    "鼠标": "DPI是最高灵敏度记录，weight_g是重量；不能据此保证游戏性能。无线不等于蓝牙。",
    "键盘": "switches是红轴、茶轴等实际轴体，不能用机械代替；机械键盘是标题类型词，"
    "无具体型号时放keywords，有具体型号时保留在name。style是配列；"
    "系列名含轴字、版本名含无线不证明对应规格，明确轴体/连接要求仍用规格条件。"
    "backlit为背光模式；tenkeyless为有无独立数字键区，不反推具体配列。",
    "耳机": "type表示佩戴类型，enclosure_type表示声学结构，两者不可互换。"
    "microphone只表示有无麦克风；不保证降噪能力。frequency_response按记录文本匹配。",
    "显示器": "refresh_rate为屏幕刷新率，不使用摄像头frame_rate。Fast IPS与IPS不自动等同。"
    "当前未记录视频输入接口；不能用摄像头connection_type或USB-C名称证明显示器支持视频输入。",
    "音箱": "type表示音箱产品类型，channels表示声道配置，power_w表示功率，分别判断。"
    "不能把Soundbar自动转换为特定声道，也不能把耳机的佩戴类型填入type。",
    "摄像头": "frame_rate为摄像头帧率，不使用显示器refresh_rate。connection_type为连接接口。"
    "USB-C不保证额外协议能力；microphone为有无麦克风；1080p HDR不自动等同1080p。",
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
) -> str:
    """Return common guidance plus one category, or all categories as fallback.

    Blank/unrecognized hints use the full guide; raw hint text is never inserted
    into the prompt. This function does not modify a query, plan, or scope.
    """
    category = normalize_category_hint(category_hint)
    categories = (category,) if category in CATEGORY_FIELDS else tuple(CATEGORY_FIELDS)
    if include_other_categories and category:
        categories = (category, *(c for c in CATEGORY_FIELDS if c != category))
    common = {
        key
        for key, definition in FIELD_DEFINITIONS.items()
        if definition.filterable and set(definition.categories) == set(CATEGORY_FIELDS)
    }
    lines = (["## 统一处理规则", _COMMON_RULES, "## 条件对照示例", CONDITION_EXAMPLES]
             if include_common_rules else [])
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
        f"{key}：{d.meaning}；询问保留缺失，硬筛选标记filter_data_unavailable。"
        for key, d in FIELD_DEFINITIONS.items()
        if not d.recorded
    )
    return "\n".join(lines)
