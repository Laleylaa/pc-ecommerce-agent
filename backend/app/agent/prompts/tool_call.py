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
- 只有 `code=invalid_input` 且 `recommended_action=replan_arguments` 时，才根据安全错误摘要定位
  被指出的非 `query` 参数，并最多修正一次；`canonical_query` 始终不变。若错误指向 query 本身或
  未指出具体字段，不得猜测改写，应停止或向用户澄清。
- `code=execution_error` 表示工具内部执行失败，不提供可安全推断的参数修复信息。若
  `recommended_action=stop` 或 `retryable=false`，不得修改 key/value 猜测原因，不得重复等价调用；
  保留其他 usable 结果，并按情况使用 `finish_partial` 或 `finish_unavailable`。
- `code=timeout` 且 action 为 `retry_once` 时只允许一次等价重试；`dependency_unavailable` 应说明
  暂时不可用；`unauthorized`、`forbidden` 和 `unknown_tool` 不得通过改写参数绕过。
- fingerprint 相同或出现 `reused_from_tool_call_id` 表示没有获得新证据。除非错误协议明确允许
  retry_once，否则立即停止重复调用；不要用改写 `query` 掩盖语义等价的重试。
- `empty`、`not_found` 和 `unsupported` 不允许通过擅自删除用户显式条件自动重查；可以直接说明
  结果，或询问用户是否愿意放宽一个条件。
- `insufficient` 或 `invalid_catalog_plan` 由 Tool 内部 planner/fallback 负责；Orchestrator 不得
  改写 `canonical_query` 再试。
""".strip()

# Compatibility aggregate for documentation and older imports. Runtime prompts load the two
# sections independently so recovery rules are absent until a real Tool failure occurs.
TOOL_CALL_PROTOCOL = f"{TOOL_INPUT_PROTOCOL}\n\n{TOOL_RECOVERY_PROTOCOL}"

__all__ = ["TOOL_CALL_PROTOCOL", "TOOL_INPUT_PROTOCOL", "TOOL_RECOVERY_PROTOCOL"]
