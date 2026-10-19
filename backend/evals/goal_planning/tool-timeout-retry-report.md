# 工具超时重试修复（E26）

2026-10-03，`feat/orchestrator-catalog-integration`、HEAD `f90c5f6`加已有工作区及本轮修改。用户授权修复工具超时重试、复用统一规则位置，并解释规划失败；未暂存、提交或推送。

## 原因与修复

OR-03此前由旧观察循环产生恢复决定，但冻结execution_plan路径丢弃该决定；失败Task不再ready。单纯恢复旧分支还会让重试占用第二个wave，挤掉依赖任务的执行预算。

现由`agent/execution.py`在当前wave内完成工具尝试。`agent/limits.py`新增冻结的ToolRetryPolicy及TOOL_RETRY_POLICY，默认max_timeout_retries=1，即含首次最多两次；retry_once契约只允许0或1次追加尝试。次数和允许条件不写在执行循环里，不与PlanningCallPolicy的LLM传输重试或输出修复共用计数。

- 只有正式错误code=timeout、retryable=true、recommended_action=retry_once三者同时满足且额度未耗尽才重试；stop、不可重试、参数/权限/依赖/程序错误不自动重跑。
- 冻结同一任务、工具、参数和可信运行时上下文；不重跑LLM1/LLM2、不重新绑定依赖或记忆，向executor传独立副本，避免首次执行修改参数影响重试。
- 每次真实尝试有独立tool_call_id、trace、流事件及审计调用；同一wave保留两份结果，投影使用最终结果，Artifact/ledger沿用正式提取规则。首轮失败会被后续成功结果取代，不能继续当作当前失败。
- MAX_TOOL_WAVES仍为2，表示依赖调度波次。每任务尝试额度独立，同参数结果复用不刷新额度；旧恢复、重复调用限制及动态失败Prompt也读取同一超时策略。参数修正规则保持原有范围。
- 使用各工具契约已有timeout_seconds；取消向上传播，不因取消触发重试。没有增加整轮工具时间截止机制或调整回答模型预算，OR-02/OR-04保持各自状态。

## 实际验证

新增`tests/test_tool_retry_policy.py`22例，覆盖参数/身份保持（含故障executor修改输入）、超时后成功、停止/不可重试/非超时错误、统一策略禁用、首层/下游/两层都超时、耗尽后依赖阻断、独立任务、真实execute_bound_tool挂起取消后重试、外部取消及耗尽结果复用。原超时缺陷特征测试改为两次调用后停止的修复验收断言，其余审查断言保留。

首次定向21通过/2失败：新测试夹具的查询证据不一致，以及错误使用dataclasses.replace复制Pydantic工具契约；修正夹具后23通过。本轮最终16文件回归1085 passed、33写库fixture依赖排除，无失败/skip/xfail，9.86秒。另定向复核旧恢复路径的query改写拒绝、规范等价query允许及Task范围结果复用3 passed，0.01秒。Ruff及git diff --check通过。其他旧契约迁移问题未整体清理或关闭。

backend执行入口如下；完整精确命令在本地产物run.json（不要重用已存在的basetemp，否则pytest会删除其原始产物）：

```bash
.venv/bin/python -m pytest -p scripts.orchestration_audit_safety \
  tests/test_chat_input_rules.py tests/test_conversation_context.py \
  tests/test_llm_feedback.py tests/test_tools.py tests/test_planning_handoff.py \
  tests/test_goal_planning_validation.py tests/test_planning_contract_alignment.py \
  tests/test_planning_execution_policy.py tests/test_catalog_search_tool.py \
  tests/test_catalog_search_outcomes.py tests/test_agent_tracing.py \
  tests/test_goal_task_integration.py tests/test_planning_call_policy.py \
  tests/test_tool_retry_policy.py tests/test_orchestrator_runtime_audit.py \
  tests/test_orchestrator_outcomes.py -q --tb=short
```

本地产物统一放`backend/evals/results/agent_traces/tool-timeout-retry-20261003/`：manifest.json实现/测试/锁文件hash、run.json、pytest-output.txt、results.json及pytest下32份完整原生trace。超时成功正式图trace_id=`01a10250-b2bd-7361-8795-4b5eb592a105`，一波两份tool_result、一个tool_timeout_retry事件。全部recording_complete，无未完成run和写入错误；trace完整不代表真实业务端到端验收。

验证使用脚本模型、正式图/工具、人工商品及假审计；socket与AsyncEngine连接被阻断，无配置数据库、真实模型、页面或真实LLM3验证。没有运行完整后端测试，不升级稳定模块基线。OR-03按本轮确定性关闭条件关闭，其他issue不自动关闭。

## 规划失败解释及范围

E25的RP-02是搜索工具内部规划失败，LLM1/LLM2的query在探针中完整保留。探针主动注入`not JSON`，CatalogSearchPlanner调用SearchPlan.model_validate_json失败，CatalogSearchService捕获后返回planner_error/invalid_plan、query_state=not_executed。没有查询数据库，也没有业务事实证明“无匹配”或“资料不足”。

搜索内部规划器独立于已接入的generate_plan/目录helper，按现有政策只进行一次模型调用；三类格式反馈模块尚未接入此路径（VF-01）。原始规划失败被检测到了；缺陷是编排结果归一化将未显式映射的planner_error归insufficient，回答交接据此标insufficient_evidence。

这能确认解析失败触发与消费方根因，不能证明当前真实模型为何返回非法JSON，也不能据此断言Prompt混乱或模型理解错误。RP-02/VF-01本轮仅解释，未改实现，保持开放；不能用工具超时重试代替模型输出修复。
