"""Phase-specific Tool-call construction and recovery rules."""

TOOL_INPUT_PROTOCOL = """
- 原生 Tool schema 是工具名、字段名、类型、枚举和必填项的唯一依据；不得翻译字段名、创造别名
  或填写 schema 未声明的字段。
- `subquery` 必须复制当前 ready Task 的 `task_n` ID（兼容旧 `sq_n`）。canonical query 由 Runtime
  根据该 ID 读取，
  并派生 Tool 的 `query`；Planner 不输出 query，也不得重新拆分、合并或补充其他 task 的条件。
- 只填写公开 schema 要求的必要信息，不要生成或覆盖 Tool 内部查询计划。
- 调用 order_lookup 时保留 canonical query 的数量与窗口语义；除非已有明确订单号或受信任的
  task_output 绑定，否则不要填写 order_id，也不要用 limit=1 把列表查询改成单笔查询。
- 每个 Tool Call 只服务一个 ready task；相互独立且必要的 ready task 应放在同一 wave。不得提前
  调用仍在等待 `depends_on` 的 task，也不得自行猜测 `task_output` 参数。
""".strip()

TOOL_RECOVERY_PROTOCOL = """
- 工具最终失败使用统一code、message和next_action；内部细码及复核意见不属于调用修正契约。
- next_action=revise_arguments时只修正被明确指出的非query参数，最多一次；canonical_query不变。
  若未指出字段或错误指向query，不得猜测改写。历史invalid_input/replan_arguments按同一边界解释。
- next_action=retry才许可一次相同工具、相同参数重试，程序预算优先；历史timeout/retry_once
  按既有策略处理。stop不得通过改写query绕过，保留其他usable结果并如实说明查询失败。
- request_authentication引导登录；narrow_request可说明缩小展示或拆分请求的需要，不能删除硬条件。
  access_denied不得绕过；dependency_unavailable不表示商城用户未登录；planning_failed不表示无匹配。
- fingerprint相同或reused_from_tool_call_id不构成新证据，不重复成功查询。
- empty、not_found、unsupported及资料不足仍按领域事实处理，不自动删除条件或重规划。
""".strip()

# Compatibility aggregate for documentation and older imports. Runtime prompts load the two
# sections independently so recovery rules are absent until a real Tool failure occurs.
TOOL_CALL_PROTOCOL = f"{TOOL_INPUT_PROTOCOL}\n\n{TOOL_RECOVERY_PROTOCOL}"

__all__ = ["TOOL_CALL_PROTOCOL", "TOOL_INPUT_PROTOCOL", "TOOL_RECOVERY_PROTOCOL"]
