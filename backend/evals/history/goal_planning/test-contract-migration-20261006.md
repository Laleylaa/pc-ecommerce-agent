# TP-02与旧编排测试迁移（2026-10-06，E52）

适用`feat/orchestrator-catalog-integration`，HEAD `931ba13299e3491aa4e192bd8a0a2a44f2229cbd`加当前工作区。仅处理TP-02核对和OR-06的四个旧测试文件；无业务实现/Prompt/模型配置变更，无新测试场景，无真实模型、搜索、配置数据库或浏览器调用。台账唯一入口仍为[issues](../issues-before-agent-loop.md)。

## 结果

- TP-02关闭：剩余“合法商品上游缺父型号/候选版本关系”在正式契约中不可达，不新造非法输出用例。
- OR-06部分完成：四文件由E51的59通过/29失败，变为本次84通过。26个原失败场景迁移；删除4个过时/重复场景（其中3个原失败、1个原通过）。不是仅通过删失败测试得到全绿。
- OR-06仍保留OrderRows不支持filters的评测适配问题，本轮未改该适配器，也未验条件订单live入口。
- 没有新发现可确认的程序bug。迁移过程中失败来自旧Goal字段、遗漏正式artifact节点、旧终态期望及夹具不完整；仅修测试，不改实现求绿。

## TP-02核对依据

1. [goal_planning.validate_execution](../../../app/agent/goal_planning.py)限定product类型task_output只能来自catalog_search，不能把任意工具对象当商品依赖。
2. [正式工具包装器](../../../app/tools/contracts.py)使用CatalogSearchResult验证输出后才交付。SearchProduct继承[ProductCard](../../../app/schemas/catalog.py)的必需spu_id；[SearchProduct](../../../app/schemas/catalog_search.py)要求candidate_sku_ids非空、正值、唯一，SKU身份一致，SPU候选与variants完全对应。缺字段、空候选不是可用正式结果。父型号ID来自正式repository，不能以人为构造缺关系字典代表真实查询结果。
3. [artifact提取](../../../app/agent/artifacts.py)对v3保留整个输出，只补query及移除分页控制；不会裁掉产品身份关系。bound_inputs直接读取对应关系。
4. 无匹配/对象不存在或非exact依赖由既有任务状态/准入处理，不能伪装为成功但缺身份的对象。已有context缺关系、位置越界、SKU/SPU混合、依赖绑定及正式schema错误测试保留。
5. 本次重跑planning_handoff、goal_task_integration、goal_planning_validation及20项搜索schema/包装器回归。关闭限定当前正式生产者与契约；不是宣称任意自定义executor返回都安全，也不要求为不可达字典状态建立新测试。

## 调整的现有测试

| 文件 | 当前数量 | 迁移内容及保留的独立断言 |
| --- | ---: | --- |
| [test_request_router.py](../../../tests/test_request_router.py) | 45 | 5项回答/SSE场景改为GoalPlan及source_text、统一summarize_goals/finish_answer；保留原回答内容、历史材料、无业务调用、SSE delta/done、持久化结果及调用次数。纯文本场景按现行契约一次格式修复后接受结构化回答，不允许最终guard重试。本店材料改由policy_search，避免用已改为Web的knowledge_search假装本店知识库 |
| [test_orchestrator_task_dag.py](../../../tests/test_orchestrator_task_dag.py) | 7 | 单SPU多版本场景经LLM1/LLM2正式入口提交可信scope；工具运行时仅接user_id，不再断言旧catalog_context。保留一个型号多个版本的范围及多型号需澄清场景；歧义决定由脚本LLM1提交，程序保留且不猜对象，不宣称测到了真实模型消歧能力 |
| [test_orchestrator_wave_loop_cases.py](../../../tests/test_orchestrator_wave_loop_cases.py) | 17 | 回答状态使用有效task关联和schema校验的v3事实；保留预算停止、被替代来源不可引用、回答失败保留商品、额外品牌拒绝及后续失败保留先前结果。未涉及的兼容策略单测保持原范围，不据此声称旧循环仍为正式入口 |
| [test_orchestrator_tool_observation_eval.py](../../../tests/test_orchestrator_tool_observation_eval.py) | 15 | 复用planning_harness交付计划，按正式顺序执行→归一化→artifact→ledger；保留参数/认证身份、业务状态、有效来源和可用结果断言。超时验证同波两次相同调用、旧尝试superseded且额度耗尽；普通invalid_input不当成超时重试 |

终态断言调整有当前契约依据：确定性兜底可保留事实但无法完成综合说明，因此标partial；查询已执行且无匹配是可回答结果；Web检索为空只证明无证据，保持insufficient/no_retrieval_evidence。并未把失败改为成功，或用trace存在替代行为断言。

## 删除前已说明的4项与承接

| 删除项 | 原来验证 | 取代口径及现有承接 |
| --- | --- | --- |
| observation参数例01-catalog-applied | usage_mapping=applied时商品结果可用 | v3不再使用该标志，和保留的03商品成功结果例重复；answer_handoff及正式goal_task_integration继续覆盖v3可用事实 |
| observation参数例02-catalog-expanded | usage_mapping=expanded时商品结果可用 | 同上；不恢复旧用途自动扩展分类。用途不明确场景保留为05-requirement-needs-clarification，验证停止并澄清 |
| wave::test_runtime_allows_invalid_input_fix_when_query_is_unchanged | limit=0执行失败后由旧循环修为3 | 非法limit在LLM2交付前阻断；planning_execution_policy中的test_limit_types_and_ranges_are_strict、test_llm2_repairs_argument_errors_once_preserving_quantity承接。tool_retry_policy验证非超时错误不自动重试 |
| wave::test_runtime_terminates_invalid_input_fix_that_rewrites_query | 同一旧循环修参不得重写query | 当前业务恢复必须经原LLM2局部替代，不能要求任意恢复query逐字不变；goal_recovery保护有效/无关任务，planning_handoff和tool_retry_policy分别保留绑定不改query及同次超时重试参数不变。旧非法limit状态也不可从正式计划交付到执行 |

无有效覆盖被先删除后推给“以后补测试”；新旧规则分工不同的部分明确退役。输出契约错误例08保留，改为消费包装器execution_error；真正输出拒绝由本次重跑的搜索schema/包装器测试承接，不能让不合法empty+products穿过包装器才由回答层补救。

## 实际检查

检查过既有fixture；所有pytest命令启用`scripts.orchestration_audit_safety`阻断网络和AsyncEngine.connect，没有配置数据库fixture进入此次选择。只使用脚本模型、人工记录、mock执行器和假持久化；未调用真实聊天接口。

从backend运行：

```sh
.venv/bin/python -m pytest -p scripts.orchestration_audit_safety tests/test_request_router.py tests/test_orchestrator_task_dag.py tests/test_orchestrator_wave_loop_cases.py tests/test_orchestrator_tool_observation_eval.py tests/test_planning_handoff.py tests/test_planning_execution_policy.py tests/test_goal_planning_validation.py tests/test_goal_task_integration.py tests/test_goal_recovery.py tests/test_tool_retry_policy.py tests/test_answer_handoff.py tests/test_answer_finalization.py tests/test_agent_tracing.py -q --tb=short --junitxml=/tmp/e52-final.xml
.venv/bin/python -m pytest -p scripts.orchestration_audit_safety tests/test_catalog_search_tool.py -k 'model_facts_must_describe_matching_variants or result_schema_rejects_inconsistent_states or invalid_output_and_timeout_keep_outer_error_contract' -q --tb=short --junitxml=/tmp/e52-schema.xml
.venv/bin/ruff check tests/test_request_router.py tests/test_orchestrator_task_dag.py tests/test_orchestrator_wave_loop_cases.py tests/test_orchestrator_tool_observation_eval.py
```

结果为522 passed＋20 passed/260 deselected（未选的搜索场景），合计542 passed，无失败/skip/xfail；Ruff及git diff --check通过。最后仅缩短一个测试函数名称满足100列规范，未改逻辑；JUnit中保留运行时旧名称。原始输出/JUnit在[本地忽略产物](../../results/test-contract-migration-20261006)，不强制加入Git。未跑完整后端测试或make check。

本次新增工作区差异仅四个测试文件、此报告、evals入口、issues和本地development入口。索引保持开始时空状态，已有业务/guard/trace及frontend修改保留；未add、commit、push。
