# 两阶段规划契约对齐验证

- 日期：2026-10-01。
- 代码：`feat/orchestrator-catalog-integration`，基点 `744cc0f` 加本轮未提交修改。
- 结果：197 个隔离用例，196 passed、1 xfailed；xfail 为既有纯空白 ChatRequest 输入问题。
- fixture 收集检查：无直接或间接依赖 `db_session_factory` 的选中用例；没有连接配置数据库。
- 限制：没有调用真实 LLM、真实数据库、网络检索或执行聊天端到端；不代表自然表达稳定性验收。

## 覆盖与证据

- [规划契约回归](../../tests/test_planning_contract_alignment.py)：新 GoalPlan 到内部结构转换；订单原文/历史引用来源、伪造身份拒绝；SQLite 内存表执行正式订单 repository 的用户归属谓词。
- [执行策略回归](../../tests/test_planning_execution_policy.py)：历史复用由 LLM1 决定；无历史依据时反馈修复；两阶段实际输入不包含旧控制字段；六工具显式/默认数量经过规划、Runtime、正式绑定保持一致；严格类型/范围检查；内部错误和依赖故障不重试。
- [工具链回归](../../tests/test_goal_task_integration.py)：脚本模型＋真实商品工具＋内存商品；默认/指定候选数返回；已移除的商品分页参数不能执行。内部 offset 实现仍可直接测试，不对 LLM 暴露。
- [既有规划验证](../../tests/test_goal_planning_validation.py)、[评测 fixture 验证](../../tests/test_goal_tool_flow_fixtures.py)：非法 JSON、覆盖/依赖/范围校验、Prompt 示例及测试数据隔离。
- [工具测试](../../tests/test_tools.py)选中公开 schema、身份注入、订单快路径、数量窗口及知识返回上限用例；[路由测试](../../tests/test_request_router.py)选中职责与权限 guard 用例。

六工具的绑定测试使用回执 handler 检查真实入参；另有真实商品、订单和知识工具执行测试，数据/模型 I/O 由内存对象替换。不能据此声称六工具真实业务链全部验收。

## Prompt 与模型可见输入审查

1. LLM1 绑定 `GoalPlan`，使用 `GOAL_ROUTER_PROMPT`；只负责目标理解与分类。历史查询仅传自然语言，不传原始 query_plan、分页游标或数量默认值；可信对象单独投影。没有将旧 Router 的工具 DAG 指令送入新入口。
2. LLM2 绑定由正式 public_input_model 派生的 ExecutionPlan schema，使用 `TASK_PLANNER_PROMPT`。只接收 tool_planning 目标的必要字段和相关引用，不传旧 route 的 reason_code、clarification_question 或历史 tasks。
3. 商品公开参数没有 offset，任务没有 page_from，可信对象没有 catalog_page。LLM2 不以另一个字段接收商品页码；内部 CatalogSearchRequest 的 offset 仍保留。
4. 显式数量、省略数量、订单原文编号及“回答三点不等于检索三条”都有 schema-valid 示例；没有独立维护另一张默认数量表。
5. 工具说明移除“所有五笔订单”的假设，增加目录选项与具体商品事实查询的区别。订单内部 Prompt 不再让模型决定 limit/offset。旧 Prompt 文件作为兼容实现保留，未注入这两个正式入口。

这些检查证明输入结构和脚本场景的一致性；模型能否在新表达中正确选 disposition、识别数量或说明分页限制，仍需真实模型评测。

## 安全复现

在 backend 目录使用现有虚拟环境；不需要安装、初始化或写入配置数据库：

```python
# 保存为临时脚本后使用 .venv/bin/python 运行，或通过 heredoc 执行。
import pytest

class NoDatabaseFixtures:
    def pytest_collection_modifyitems(self, items):
        unsafe = [i.nodeid for i in items if "db_session_factory" in i.fixturenames]
        if unsafe:
            raise pytest.UsageError(f"Configured database fixtures prohibited: {unsafe}")

files = [
    "tests/test_planning_execution_policy.py",
    "tests/test_planning_contract_alignment.py",
    "tests/test_goal_planning_validation.py",
    "tests/test_goal_task_integration.py",
    "tests/test_goal_tool_flow_fixtures.py",
]
tool_tests = [
    "test_catalog_public_schemas_are_query_first",
    "test_document_search_public_schema_fixes_hybrid_retrieval",
    "test_registry_tool_executor_injects_runtime_and_rewrites_tool_name",
    "test_registry_tool_executor_rejects_llm_supplied_runtime_field",
    "test_order_fast_path_accepts_plain_english_explicit_order_id",
    "test_order_fast_path_classifies_colloquial_queries",
    "test_order_lookup_returns_exact_total_for_bounded_recent_window",
    "test_document_search_limit_bounds_returned_chunks",
]
router_tests = [
    "test_runtime_does_not_reclassify_llm_order_goals",
    "test_runtime_does_not_append_order_goals_to_llm_plan",
    "test_privilege_bypass_order_table_request_requires_security_refusal",
    "test_security_policy_question_about_access_controls_remains_allowed",
    "test_mixed_privilege_bypass_preserves_catalog_goal_and_security_refusal",
    "test_order_write_policy_and_export_requests_are_not_forced_into_order_tool",
    "test_order_and_warranty_tasks_are_not_collapsed_into_one_order_lookup",
]
raise SystemExit(pytest.main([
    "-q", *files,
    *["tests/test_tools.py::" + n for n in tool_tests],
    *["tests/test_request_router.py::" + n for n in router_tests],
], plugins=[NoDatabaseFixtures()]))
```
