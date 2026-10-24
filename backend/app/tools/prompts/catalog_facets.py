"""Facet planning instructions derived from executable catalog field definitions."""

import json

from pydantic import BaseModel

from app.rules.product_identity import PRODUCT_IDENTITY_INTERSECTION_GUIDANCE
from app.tools.prompts.catalog_search_specs import build_spec_context
from app.tools.structured_planning import output_schema


def build_facets_review_context(category_hint: str | None) -> str:
    return "\n\n".join(
        (
            "业务范围：只读商品目录与候选集合选项统计，不写SQL、不回答。",
            PRODUCT_IDENTITY_INTERSECTION_GUIDANCE,
            "本工具只统计目录或可信候选集合的选项，不返回具体商品的报价、详情或版本列表。"
            "selection=catalog；facet=category列品类，brand列品牌，spec_key列规格维度，"
            "spec_value列一个规格的可选值及计数。scope可限定已确认候选集合。"
            "询问指定商品的规格、颜色、能力、版本数量和价格范围应使用catalog_lookup；"
            "这类请求保留selection=object，程序返回明确的工具边界提示，不扩大为全店查询。"
            "name只用于保留误投本工具的指定对象，target仅原文明示编号，不能猜编号。"
            "category只取用户明说的品类，category_hint仅选择说明，不是筛选条件。",
            "按字段定义提交所有可执行的用户条件到conditions，保留单位、"
            "上下界、否定和多选，条件在同一SKU成立。模型省略filters、brand、"
            "min_price/max_price；品牌和价格要求也提交为conditions。"
            "调用方explicit_overrides由程序执行，不复制到这些模型字段。"
            "筛选条件不能因为没有记录值或可能无结果而删除。可执行conditions只用字段说明中"
            "可筛选的字段；不能创造字段或把无线充电映射成无线连接。不能执行的要求放"
            "unresolved_requirements，每项以evidence简述具体要求并给出reason：字段体系缺少对应能力用"
            "filter_data_unavailable；用途/主观要求待具体化用requirement_needs_clarification；"
            "超出工具任务能力才用request_not_supported。不能只提交可执行部分。"
            "旧unsupported_requirements和clarification_requirements仅兼容历史计划，本次省略。"
            "指定对象请求用selection=object保留，交给程序提示使用catalog_lookup，"
            "也不要把对象询问放入筛选要求。supported只在任务形态不支持时为false；"
            "字段缺能力、待澄清要求由结构化原因表达，不靠unsupported_reason自由文本分类。"
            "事实缺失不代表商品不支持。count_scope默认spu，仅明说SKU才sku。",
            "普通规格条件strength=requirement；只有明确可让步的要求用preference。"
            "条件evidence和preference_evidence是可选审计说明，不要求摘录，不影响执行。"
            "硬条件决定SKU准入；软偏好只排序目录选项。"
            "没有偏好匹配仍保留符合硬条件的集合，排序后再limit。"
            "同一要求只能在conditions或unresolved_requirements中有一个主表示；不同要求"
            "分别保留完整含义，不删否定或单位。不凭用途猜参数、不虚构office字段。"
            "unsupported_code由程序校验设置，模型不要输出。",
            "字段放错位置、非法操作符/数值/单位、无可信来源的编号属于计划错误，应保留要求并按反馈修复；"
            "不是用户缺资料。spec_value必须提供spec_key；字段体系没有定义的能力才是能力限制。"
            "没有筛选条件仍可以列目录或读对象规格，不能仅因conditions为空判定失败。",
            build_spec_context(category_hint, include_other_categories=False),
            "字段解释不证明某款商品有这些规格，也不证明规格可任意组合。商品实际记录由程序"
            "读取。输出前逐项核对原文：对象、品类、品牌、颜色、连接方式、配列、价格、否定"
            "及其他要求均有去向；不输出核对过程。程序只验证结构与执行规则，不通过摘录证明语义正确。"

        )
    )


def build_facets_prompt(schema: type[BaseModel], category_hint: str | None) -> str:
    return "\n\n".join((
        "你是商品目录查询规划器，只输出一份符合schema的JSON。",
        build_facets_review_context(category_hint),
        json.dumps(output_schema(schema, schema.model_required_fields), ensure_ascii=False),
    ))
