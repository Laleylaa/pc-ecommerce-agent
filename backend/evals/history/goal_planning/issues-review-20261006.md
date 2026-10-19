# 共享issues核验记录（2026-10-06，E51）

适用 `feat/orchestrator-catalog-integration`，HEAD `931ba13299e3491aa4e192bd8a0a2a44f2229cbd` 加当前工作区。本报告记录一次核验的证据和删除理由，不另建待办；当前事项仅在[共享issues](../issues-before-agent-loop.md)。

仅整理文档，未修改业务、Prompt、guard、trace实现或测试。保留此前全部工作区差异及前端独立会话修复；索引开始为空。本次无add/commit/push，无新测试用例、真实模型/搜索调用、配置数据库或浏览器测试，无完整后端测试/make check。

## 实际验证

先读fixture及安全插件：`db_session_factory`会接触配置数据库，不能作只读准备；本次所选用例无该fixture。所有命令启用`-p scripts.orchestration_audit_safety`，阻断socket连接和AsyncEngine.connect；tool_planning_repairs的SQL使用显式内存SQLite及人工记录。测试使用脚本模型、mock HTTP和假持久化，不证明真实模型质量或真实事务结局。

合计905 passed、29 failed、254 deselected，无skip/xfail。254仅来自名称专项的`-k`筛选，不是数据fixture排除。各批无重复文件；原始输出/JUnit在本地忽略目录[issues-review-20261006](../../results/issues-review-20261006)，未强制暂存。

| 文件 | 通过 | 失败 |
| --- | ---: | ---: |
| [test_tool_planning_repairs.py](../../../tests/test_tool_planning_repairs.py) | 41 | 0 |
| [test_catalog_query_policies.py](../../../tests/test_catalog_query_policies.py) | 29 | 0 |
| [test_catalog_identity_intersection.py](../../../tests/test_catalog_identity_intersection.py) | 33 | 0 |
| [test_policy_retrieval_runtime.py](../../../tests/test_policy_retrieval_runtime.py) | 8 | 0 |
| [test_tool_retry_policy.py](../../../tests/test_tool_retry_policy.py) | 22 | 0 |
| [test_planning_handoff.py](../../../tests/test_planning_handoff.py) | 105 | 0 |
| [test_planning_contract_alignment.py](../../../tests/test_planning_contract_alignment.py) | 68 | 0 |
| [test_goal_task_integration.py](../../../tests/test_goal_task_integration.py) | 43 | 0 |
| [test_goal_planning_validation.py](../../../tests/test_goal_planning_validation.py) | 67 | 0 |
| [test_answer_handoff.py](../../../tests/test_answer_handoff.py) | 38 | 0 |
| [test_answer_finalization.py](../../../tests/test_answer_finalization.py) | 55 | 0 |
| [test_answer_context.py](../../../tests/test_answer_context.py) | 21 | 0 |
| [test_orchestrator_outcomes.py](../../../tests/test_orchestrator_outcomes.py) | 35 | 0 |
| [test_orchestrator_runtime_audit.py](../../../tests/test_orchestrator_runtime_audit.py) | 12 | 0 |
| [test_chat_input_rules.py](../../../tests/test_chat_input_rules.py) | 89 | 0 |
| [test_web_search.py](../../../tests/test_web_search.py) | 24 | 0 |
| [test_llm_feedback.py](../../../tests/test_llm_feedback.py) | 37 | 0 |
| [test_input_tool_contract_chain.py](../../../tests/test_input_tool_contract_chain.py) | 44 | 0 |
| [test_agent_tracing.py](../../../tests/test_agent_tracing.py) | 28 | 0 |
| [test_goal_recovery.py](../../../tests/test_goal_recovery.py) | 21 | 0 |
| [test_catalog_search_tool.py](../../../tests/test_catalog_search_tool.py) | 26 | 0 |
| [test_request_router.py](../../../tests/test_request_router.py) | 40 | 5 |
| [test_orchestrator_task_dag.py](../../../tests/test_orchestrator_task_dag.py) | 6 | 1 |
| [test_orchestrator_wave_loop_cases.py](../../../tests/test_orchestrator_wave_loop_cases.py) | 13 | 6 |
| [test_orchestrator_tool_observation_eval.py](../../../tests/test_orchestrator_tool_observation_eval.py) | 0 | 17 |

29失败名称及断言保留在[legacy-tests.txt](../../results/issues-review-20261006/legacy-tests.txt)，数量与E49一致；旧入口、route_plan/catalog_context、工具输出及重试fixture未迁移。未修测试求绿，不将其等同29个生产故障。OrderRows无filters签名也仍存在。

命令均从backend执行，公共前缀为`.venv/bin/python -m pytest -p scripts.orchestration_audit_safety`，公共后缀为`-q --tb=short --junitxml=/tmp/pc-issues-<批次>.xml`，输出重定向至`/tmp/pc-issues-<批次>-tests.txt`，再复制到上述忽略目录：

```text
review: tests/test_tool_planning_repairs.py tests/test_catalog_query_policies.py tests/test_catalog_identity_intersection.py tests/test_policy_retrieval_runtime.py tests/test_tool_retry_policy.py tests/test_planning_handoff.py tests/test_planning_contract_alignment.py tests/test_goal_task_integration.py tests/test_goal_planning_validation.py tests/test_answer_handoff.py tests/test_answer_finalization.py tests/test_answer_context.py tests/test_orchestrator_outcomes.py tests/test_orchestrator_runtime_audit.py tests/test_chat_input_rules.py tests/test_web_search.py tests/test_llm_feedback.py
chain: tests/test_input_tool_contract_chain.py tests/test_agent_tracing.py tests/test_goal_recovery.py
names: tests/test_catalog_search_tool.py -k 'name or vocabulary or clarification or empty_plan'
legacy: tests/test_request_router.py tests/test_orchestrator_task_dag.py tests/test_orchestrator_wave_loop_cases.py tests/test_orchestrator_tool_observation_eval.py
```

## 从台账删除的26项

关闭的是表中确定性缺陷/任务范围，不是整个产品、模型效果或完整业务验收。条目正文和待办行已删除；原锚点仅作历史链接兼容。

| ID | 当前依据与删除范围 | 本次既有回归 |
| --- | --- | --- |
| OR-01 | 目标/来源覆盖校验及逐目标兜底已接入；guard只校验不改写决定。残留文案去重拆为OR-08 | answer_context、answer_finalization、orchestrator_outcomes |
| OR-02 | 正式模型截止时间/调用预算已接入；E50删除guard额外回跳，预算例通过。人工空输出不视作真实故障 | answer_finalization、orchestrator_runtime_audit |
| OR-03 | timeout同task/args有界重试接入执行波，不丢有效结果 | tool_retry_policy |
| OR-05 | 非空白回答契约、混合破损调用拒绝及summary终态校验 | answer_finalization、orchestrator_runtime_audit |
| OR-07 | 正式入口1000字符限制在规划前生效，1001以上不进入内部长输入失败链；未重写绕过入口的内部函数 | chat_input_rules |
| CS-01 | 明确待具体化要求并停止执行，交编排澄清；不继续默认无条件查商品 | catalog_query_policies、input_tool_contract_chain |
| CS-02 | 当前名称匹配支持名称/品类词、品牌别名、型号格式及近似型号负例；完整名称＋编号贯穿正式图。旧catalog_compare已撤，搜索独立对象比较由现行图承接 | catalog_search_tool名称专项、catalog_identity_intersection、goal_task_integration；历史12条真实库只读名称探针补证，不冒充本次复测 |
| CS-03 | 空计划缺必要字段先反馈，持续错误停止且不查询 | tool_planning_repairs |
| CF-01 | 布局字段style共享定义/说明，错误字段有限修复 | tool_planning_repairs、input_tool_contract_chain |
| CF-02 | 模型缺字段不再由默认值补成可执行查询 | tool_planning_repairs |
| CF-03 | 原始非JSON输出可进入FORMAT_ERROR反馈，不依赖先解析成功 | tool_planning_repairs、llm_feedback、agent_tracing |
| CF-04 | 模型解析失败不降级为丢条件的规则查询 | tool_planning_repairs、agent_tracing |
| CF-05 | disposition为不支持等停止结果时不被后续字段校验改写成成功 | input_tool_contract_chain、catalog_query_policies |
| CF-06 | 正式目录复用搜索字段匹配/事实来源，硬条件范围外的冲突不污染集合 | catalog_query_policies、tool_planning_repairs |
| CF-07 | 无模型仅接受完整封闭表达，开放附加条件不静默生成宽查询 | tool_planning_repairs |
| CF-08 | 非成功校验停止；name＋target取交集，编号来源/范围验证；已有对象无规格保留资料不足 | input_tool_contract_chain、catalog_identity_intersection |
| CF-09 | 硬条件筛选与软偏好排序分工，不把偏好当必需 | catalog_query_policies |
| TC-01 | BaseModel类型/契约校验先于输出使用，错误类型受控execution_error | agent_tracing |
| TC-02 | JSON序列化失败在输出边界处理，不误归invalid_input或重规划合法参数 | tool_planning_repairs |
| KR-01 | 同步检索移入有界单线程，取消/超时不释放仍在工作的名额，不无界排队。线程无法硬杀的限制不变 | policy_retrieval_runtime |
| VF-01 | 正式搜索/目录/订单计划已接格式/规则反馈及预算 | input_tool_contract_chain、tool_planning_repairs、llm_feedback |
| RP-01 | answer_context保留reason/action/diagnostics/field_issues，原“具体失败原因被裁掉”不再成立；真实措辞验收归RV-03 | answer_handoff |
| RP-02 | planner_error/invalid_plan投影为execution_failed，真实data_insufficient仍为insufficient_evidence | answer_handoff、orchestrator_outcomes、goal_task_integration |
| CC-01 | catalog_compare无注册/执行别名，比较目标用独立商品查询材料；无复活旧工具 | goal_task_integration、源码注册搜索 |
| GI-04 | Goal必需文本及missing_information非空白，非法输出有限修复 | planning_handoff、goal_planning_validation |
| TP-04 | blocked_reason非空白与长度约束，修复耗尽不交付计划 | planning_handoff、goal_planning_validation |

CS-02没有重新构造旧K380库场景；关闭依据是现行匹配契约、独立正反例与当前正式图承接，而非声称旧原句已在本轮真实模型复测。名称抽取的偶发模型偏差不再重复挂一个无限期程序缺陷。

## 保留、更新和新发现的判据

13个原开放事项已按当前源码收窄：OR-04/OL-02为真实事务/时区验证；OR-06是已确认旧测试及适配器缺口；TP-02仅剩关系缺失路径的覆盖核对；KR-02已有真实政策hybrid证据但无外部搜索成功；RV-02已有supporting_tasks投影；RV-03/RV-05/TI-02按E44～E48分层补证；OL-01/RV-06/TC-03/PI-01保留决策边界。没有因“还没验证一切”保留已经修复的程序缺陷。

新增CS-04基于真实run1431旧输出加当前确定性分支核对：`conditions.key=title`被validate_plan归DATA_INSUFFICIENT，内部planner只修INVALID_PLAN，故字段位置错误未得到内部修复反馈。E46已改善Prompt，但该分类未变。不是把所有未知字段视作错误，也不是把模型偶发误写单独记bug。

OR-08是从RV-03拆出的既知兜底重复问题：E50修复正常unavailable回答被覆盖，剩余仅确定性兜底函数的逐任务重复和补全query标题；本次未生成新线上失败证据。两项均只登记，未修改代码。

## 历史证据读取边界

E44～E48的report/audit/manifest及相关原生trace属于历史证据；本次不修改、不重放：

- `chat-e2e-20261005-e44-review`：正式HTTP/SSE，正常保存/并发/政策及业务失败并存；run1431发生真实恢复。
- `chat-e2e-20261006-llm1-capabilities`、`chat-e2e-20261006-title-scope`：有正式聊天；后者也包含单独模型重放与只读工具，分别计数。
- `llm3-thinking-trial-20261006`、`llm3-scope-ab-20261006`、`recommendation-contract-20261006`：含固定历史材料的新模型调用，不作新聊天E2E；推理试验未部署。
- `chat-e2e-20261006-limit`：run1443单例limit=1、返回1型号、本人订单正常、回答已保存；不证明所有数量表达稳定。

本次仍未开始新一轮业务测试；后续按共享issue的独立关闭条件选择最小场景，复用既有隔离回归。涉及真实事务、真实服务或浏览器需在明确授权环境进行。文档相对文件链接及`git diff --check`在整理后验证；不以历史通过数替代本次905/29结果。
