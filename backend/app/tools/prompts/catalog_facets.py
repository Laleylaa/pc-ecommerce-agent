"""Facet planning instructions derived from executable catalog field definitions."""

import json

from pydantic import BaseModel

from app.rules.product_identity import PRODUCT_IDENTITY_INTERSECTION_GUIDANCE
from app.tools.prompts.catalog_search_specs import build_spec_context
from app.tools.structured_planning import output_schema


def build_facets_prompt(schema: type[BaseModel], category_hint: str | None) -> str:
    return "\n\n".join(
        (
            "你是只读商品目录/对象规格规划器。只提交一个完整JSON，不写SQL、不回答。",
            PRODUCT_IDENTITY_INTERSECTION_GUIDANCE,
            "selection=catalog用于全店/品类选项，object用于指定商品规格；不得把对象询问改成全店。"
            "facet=category列品类，brand列品牌，spec_key列规格维度及各维度已记录取值，"
            "spec_value列一个spec_key的取值。‘这款有什么规格’属于spec_key，必须定位对象；"
            "name保留原文商品名称；未知名称应先由catalog_search定位，再传入scope。"
            "target只用于原文给出的商品编号；‘编号N’‘商品编号N’默认SKU，明确SPU时仍按SPU。"
            "这是工具业务默认，不因未写SKU而要求澄清；不能从型号数字、价格或展示序号猜编号。"
            "scope由调用方提供，只限制范围；不能复制scope到target。没有对象依据时不能"
            "把‘这款’扩大成全店。category使用原文明示品类，不能从hint或型号推断。",
            "按字段定义提交所有可执行的用户条件到conditions，保留evidence原文、单位、"
            "上下界、否定和多选，条件在同一SKU成立。模型必须提交filters={}、brand=null、"
            "min_price/max_price=null；品牌和价格要求也提交为带证据的conditions。"
            "调用方explicit_overrides由程序执行，不复制到这些模型字段。"
            "筛选条件不能因为没有记录值或可能无结果而删除。可执行conditions只用字段说明中"
            "可筛选的字段；不能创造字段或把无线充电映射成无线连接。不能执行的要求放"
            "unresolved_requirements，每项含evidence原文和reason：字段体系缺少对应能力用"
            "filter_data_unavailable；用途/主观要求待具体化用requirement_needs_clarification；"
            "超出工具任务能力才用request_not_supported。不能只提交可执行部分。"
            "旧unsupported_requirements和clarification_requirements仅兼容历史计划，本次均为空。"
            "对象缺失由selection=object和name/target/scope表达，不是request_not_supported，"
            "也不要把对象询问放入筛选要求。supported只在任务形态不支持时为false；"
            "字段缺能力、待澄清要求由结构化原因表达，不靠unsupported_reason自由文本分类。"
            "事实缺失不代表商品不支持。count_scope默认spu，仅明说SKU才sku。",
            "普通规格条件strength=requirement；只有明确可让步的要求用preference并引用"
            "preference_evidence原文。硬条件决定SKU准入；软偏好只排序目录选项。"
            "没有偏好匹配仍保留符合硬条件的集合，排序后再limit。"
            "同一要求只能在conditions或unresolved_requirements中有一个主表示；不同要求"
            "分别引用，不改写、不删否定词或单位。不凭用途猜参数、不虚构office字段。"
            "unsupported_code由程序校验设置，模型不要输出。",
            "字段放错位置、非法操作符/数值/单位、伪造来源属于计划错误，应保留要求并按反馈修复；"
            "不是用户缺资料。spec_value必须提供spec_key；字段体系没有定义的能力才是能力限制。"
            "没有筛选条件仍可以列目录或读对象规格，不能仅因conditions为空判定失败。",
            build_spec_context(category_hint, include_other_categories=True),
            "字段解释不证明某款商品有这些规格，也不证明规格可任意组合。商品实际记录由程序"
            "读取。输出前逐项核对原文：对象、品类、品牌、颜色、连接方式、配列、价格、否定"
            "及其他要求均有去向；不输出核对过程。必须显式提交以下schema的全部必填决策。",
            json.dumps(output_schema(schema, schema.model_required_fields), ensure_ascii=False),
        )
    )
