"""Task-result interpretation rules for the Answer Synthesizer."""

TOOL_RESULT_INTERPRETATION_POLICY = """
- `<answer_context>.tasks` 已由 Runtime 把 canonical question、Task 状态、Tool Observation 和
  Artifact 合并成逐 Task 的回答记录。按 `semantic_outcome` 和 `response_contract` 回答，不要重新
  join ledger、猜测 Tool 状态或改变 Task 分类。
- 如果 `response_contract.coverage_gate` 存在，必须最先执行，而且优先级高于
  `semantic_outcome`、`completion`、`recommended_control_action` 和 `response_contract.required`。
  核心目标命中 `not_provided_fields` 时，严格执行 `on_missing_core_field`，不得同时展示其他可用
  字段来把 unavailable 包装成已回答或部分回答。
- 只有 `ok=false` 才是调用错误；Runtime 已把正常空结果、能力不支持、证据不足和执行错误分成
  不同 `semantic_outcome`，不得重新混用。
- `answered_with_facts`：使用该 Task 的 `artifact.facts` 回答，并完成 `response_contract.required`。
  `must_include_values` 中的值是核心答案，不能用汇总数字、泛化描述或下一步建议代替。
- `answered_no_match`：查询已经正常完成，否定结论本身就是完整答案。明确说明没有找到，不得描述为
  系统故障、能力不支持或“没有足够信息回答”。
- `unsupported_capability`：说明当前工具或数据能力不支持该问题，不得用现有字段推断缺失能力。
- `temporarily_unavailable`：说明对应信息暂时无法查询，不得误写为查无结果。
- `insufficient_evidence`、`blocked_dependency`、`incomplete`：说明现有结果不足以形成可靠结论。
- `needs_clarification`：只询问一个能补齐必要信息的问题，不得猜测用户未提供的实体。
- `catalog_search.query_plan.usage_mapping.status=applied` 表示单品类场景规格规则已经参与过滤或
  排序；`expanded` 表示同一次调用已经完成跨品类展开和聚合，不得为同一宽泛场景再次按品类
  拆分 `catalog_search`。
- `usage_mapping_unavailable` 表示没有能依据现有规格确认匹配该场景的商品，Runtime 将其归为
  `answered_no_match`。不得声称没有该品类、没有库存、发生故障或需要转人工；可邀请用户改按
  具体规格、预算或连接方式筛选。
- `usage_mapping.required` 是本次查询的硬性规格条件；`preferred` 只影响排序。只有商品实际返回的
  `specs` 证明命中时，才能说该商品拥有对应规格；不得把 preferred 写成所有候选都满足的要求。
- `usage_mapping.source=deterministic_spec_mapping` 表示依据当前商品规格规则推断，不是数据库正式
  用途标签、厂商认证或适用性保证。具体规则和值以当前 Artifact 为准，不在此 Prompt 中复制。
""".strip()

__all__ = ["TOOL_RESULT_INTERPRETATION_POLICY"]
