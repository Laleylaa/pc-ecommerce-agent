# 输入到工具结果交接复评（E25／E27）

**2026-10-04阶段范围更正（用户明确）：当前工作到工具结构化输出为止，输出后的处理尚未开展。** 下方E27额外检查了结果解释/回答及收尾，证据仍成立，但不应把这些后续缺陷当作当前工具层开发的前置阻塞项。RP-02及回答阶段问题保留后续issue；当前继续工具内部计划/反馈及工具输出序列化边界。本次仅调整验收范围说明，未修改代码或生成新测试成绩。

## 当前复核：编排能否结束验收、转入工具修改（E27）

2026-10-03，`feat/orchestrator-catalog-integration`、HEAD `f90c5f6`加E26及已有工作区。只核查与记录，生产实现、现有测试和锁文件指纹未变化，未暂存/提交/推送。

**结论：工具层可以开始修改，编排层仍有已确认缺陷，不能宣布无错误或完成整体验收。** CF-02/CS-03的工具输出契约修复不依赖回答层先全部修好；结果分类RP-02及TC-02编排侧必须纳入接口联调验收。不能用提高模型输出成功率掩盖失败处理缺陷。

- 新跑16文件及3条旧恢复/复用定向检查：1088 passed、33写库fixture依赖排除，无失败/skip/xfail，9.24秒。范围为E26报告列出的16文件，另选test_timeout_retry_cannot_rewrite_canonical_query、test_timeout_retry_allows_only_normalized_equivalent_query、test_tool_result_reuse_is_scoped_to_the_routed_task。完整精确命令保存在本轮run.json，没有复用或删除历史basetemp。
- 1088中包含9条断言已知错误现状的测试，它们通过表示问题仍存在，不是修复验收。只有原OR-03超时用例在E26改为正确行为断言；本轮未改旧断言规避问题。其余回归检查受测行为不回退。
- 重新跑9条独立探针，10份原生trace完整，无未完成run/写入错误；超时探针改为验证E26期望，其他探针继续检查当前坏路径和正常对照。
- 本地产物统一放`backend/evals/results/agent_traces/orchestration-readiness-20261003/`：probe.py、manifest.json、run.json、pytest-output.txt、results.json、probes下正式边界输出/指纹/trace以及pytest原生trace。socket/AsyncEngine连接被阻断；脚本模型、人工repository和假审计。无真实模型、配置数据库或页面验证；正常回答仅脚本/故障对照，不宣称真实LLM3效果通过。

| 当前编排侧问题 | 本轮证据与原因 | 验收判断 |
| --- | --- | --- |
| OR-03工具超时恢复 | timeout_retry_fixed：两次同参数调用、同wave=1、Task succeeded；连续超时与两层依赖专项回归通过 | E26修复仍有效 |
| RP-02失败误归资料不足 | search_parse_failure：planner_error/invalid_plan且未查询，交接仍为insufficient_evidence；trace_id=01a1026a-4c33-7481-a978-938c797eb6ab | 编排结果归一化/交接仍待修复 |
| TC-02编排侧异常误归输入 | serializer_graph及直接包装器：人工不可序列化输出异常在execute_tool_wave被ValueError捕获，仍为invalid_input/replan_arguments；trace_id=01a1026a-4c50-75e1-b0c8-c0f5abdb102d | 工具包装器与编排共同修复；不是实际用户输入错误 |
| OR-01完整终态漏答 | 当前正式图测试仍接受只回答/引用第二个成功目标；fallback也只显示最后一个，guard accepted | 已知回答覆盖缺陷仍存在 |
| OR-02回答调用/时间预算 | 当前脚本连续非法回答4次ainvoke只记2次；非规划客户端未配置HTTP deadline | 已修LLM1/2预算不覆盖剩余回答生命周期 |
| OR-04异常收尾 | 当前持久化故障对照仍显示同步/SSE处理不一致，失败审计二次故障会遮蔽原异常 | 隔离复现成立，真实数据库状态未验 |
| OR-05回答完整性 | 当前仍接受空白summarize_goals及合法终态与invalid_tool_calls并存 | 回答输出校验仍需补齐；未执行破损业务调用 |

工具层CF-01/CF-02/CS-03及VF-01坏路径也再次复现，责任和关闭条件统一见issues，不在这里宣布它们已修复。下一步可先工具完整输出契约CF-02/CS-03，再字段语义/三类反馈接入；同步处理RP-02/TC-02的失败边界，随后独立验收回答覆盖及生命周期。当前检查未证明所有自然语言理解或业务端到端均正确，不升级稳定模块基线。

## E25历史评估范围

2026-10-03，`feat/orchestrator-catalog-integration`、HEAD `f90c5f6`加已有工作区及E24。只评估和登记，未修改生产实现或现有测试。E24 manifest 中实现、测试和锁文件 hash 本轮保持一致；未暂存、提交或推送。

## 范围与方法

- 核对 API 身份接入、输入规则、上下文准备、LLM1、LLM2、可信参数与依赖绑定、正式工具、输出包装、归一化、Artifact 和 Task 交接。
- 重新运行输入/上下文/工具/规划回归：1016 passed、33写库fixture依赖排除，无失败/skip/xfail。独立运行现有超时恢复问题的特征测试1 passed，该测试断言缺陷仍存在，不是修复验收。
- 新增9条独立探针：7条坏路径或接入缺口、2条正常对照。8条图探针在update_subquery_ledger停止；订单反馈探针只到内部计划，输出序列化另做直接包装器对照。输入与LLM1/LLM2输出明确保留相同query，故坏结果定位于内部工具及消费路径。
- 使用正式图、正式工具实现、脚本模型和人工repository；没有调用真实模型、配置数据库、页面或正常LLM3。Socket connect/connect_ex和AsyncEngine连接被阻断，不运行seed/import/reset。真实模型出现这些坏输出的概率及Prompt因果未测。
- 10份选定原生trace全部recording_complete、无unfinished runs/write errors。初次探针报告器误读LocalTrace.trace_id，改为root_run_id后重跑；保留初次记录，不把该脚本错误算作生产缺陷。

完整本地证据在`backend/evals/results/agent_traces/tool-flow-reaudit-20261003/`：probe.py、manifest.json、results.json、audit-summary.json及原生trace；回归原生trace另在tool-flow-regression-20261003。JSON不复制到docs。仅获取报告不能替代同版本源码和上述本地产物。

## 当前发现

| ID | 状态 | 独立触发与实际结果 | 根因与责任 |
| --- | --- | --- | --- |
| CF-02 | 已知缺陷，本轮正式图复现 | query“白色蓝牙键盘有哪些布局可选”，内部输出`{}`；一次模型调用后执行facet=brand/category=null/filters={}查询，结果usable、task succeeded | FacetQueryPlan业务默认值承担LLM完整输出校验，缺失决策被补为具体查询。目录工具负责，三类反馈器没有接收到校验错误 |
| CS-03 | 新确认缺陷 | query“白色蓝牙键盘200元以内”，内部输出`{}`；执行空条件查询，返回7个SKU，包括黑色USB SKU102、粉色SKU202和599元SKU401；status=success/matches_found、task succeeded | SearchPlan所有字段可默认，模型没有表达任何查询条件也被接受为ready。校验只检查已提交条件及规则，不拒绝空模型对象。工具catalog_search负责；独立于CF-02的契约和调用方 |
| CF-01 | 已知缺陷，本轮坏字段路径复现 | 内部输出spec_value/keyboard/layout；只调用一次、repository零调用，最终unsupported_capability。style并带白色/蓝牙条件的对照返回布局选项 | 工具模型字段语义与正式style定义仍未对齐；非法字段变业务unsupported，不进入输出修复。此处模型输出为人工注入，不证明当前真实模型会再次生成layout；不能把全部unsupported强制重试 |
| TC-02 | 新确认故障边界缺陷 | 正式CatalogFacetOutput的diagnostic.details含人工不可JSON序列化对象；直接包装器抛PydanticSerializationError。正式图将其作为ValueError捕获，结果invalid_input/replan_arguments，而公开输入已合法 | 包装器只保护BaseModel/字段校验，最终JSON序列化在保护之外；执行层又把所有ValueError归参数错误。工具包装器主责、执行层配合；未观察到正常handler实际返回此对象 |
| OR-03 | 已知缺陷仍存在 | executor首次返回timeout/retry_once，准备第二次可成功；交接处一次调用、task failed。现有完整图特征测试再次确认最终只执行一次 | 冻结execution_plan下恢复决定被清空，失败任务不再ready；模型输出纠错与工具运行重试是两套机制。Runtime主责，涉及波次/依赖预算不能仅删一行 |
| RP-02 | 新确认状态消费缺陷 | 搜索内部输出not JSON；工具status=planner_error/reason=invalid_plan、query_state=not_executed、retryable=true。归一化变insufficient，task failed，但交接semantic_outcome=insufficient_evidence | v3状态映射默认归insufficient，后续将其解释为资料不足；原始invalid_plan仍保留，并非全部原因字符串丢失。编排结果归一化/交接主责；未运行LLM3，不据此断言最终话术 |
| VF-01 | 接入缺口 | 订单内部首次`{}`缺selection/operation/requested_fields，第二次正确输出成功；反馈仍为str，无三类type/path结构 | 订单规划器保留独立重试器，独立搜索规划器也未接入。E24已明确首轮范围；是待接入事项，不是三类模块回退。各工具主责，共享模块配合；搜索单次调用政策不应无授权改变 |

价格、编号及商品属性来自人工fixture，不是店铺实时事实。正常搜索对照明确保留白色、蓝牙、<=200三条件，仅返回SKU201；正常目录对照明确提交style及两筛选条件，结果可用。两个对照确认查询执行规则本身能正确处理有效计划，坏路径主要在计划承接和状态消费。

## 逐例追溯

| 探针 | 原生trace_id | 主要核对点 |
| --- | --- | --- |
| facet_empty_plan | 01a10212-0ce5-78c0-a8e0-c19dc898d784 | 原query到LLM1/LLM2保留；空内部输出到品牌query，再到succeeded |
| facet_layout_key | 01a10212-0d01-71c3-b371-339d9a063ea2 | layout到supported=false、零查询及unsupported_capability |
| facet_valid_control | 01a10212-0d18-74a2-8425-8b2b51460af7 | style/白色/蓝牙到正式查询与成功Artifact |
| search_empty_plan | 01a10212-0d30-7011-be39-16b27e777664 | 空条件正式加载记录；7SKU均作为成功候选 |
| search_valid_control | 01a10212-0d50-7982-83d4-ca24143c5309 | 三条件到SKU201，成功对照 |
| search_parse_failure | 01a10212-0d6a-7c20-aa53-41828c973f26 | 零查询；planner_error到insufficient_evidence |
| serializer_graph | 01a10212-0d85-7aa1-8023-7b0311c50d2f | 合法输入/结构化输出到invalid_input；直接包装器失败另存direct_trace_id |
| timeout_retry_gap | 01a10212-0d9b-75e2-8a0d-8ace69d140f6 | retry_once但仅一次工具调用；Task不再ready |
| order_feedback_gap | 01a10212-0da9-78b3-b41b-22974310ab7f | 两次内部调用、str反馈、第二次有效计划；未进入repository |

## 验证入口

backend运行范围内回归：

```bash
.venv/bin/python -m pytest -p scripts.orchestration_audit_safety \
  tests/test_chat_input_rules.py tests/test_conversation_context.py \
  tests/test_llm_feedback.py tests/test_tools.py tests/test_planning_handoff.py \
  tests/test_goal_planning_validation.py tests/test_planning_contract_alignment.py \
  tests/test_planning_execution_policy.py tests/test_catalog_search_tool.py \
  tests/test_catalog_search_outcomes.py tests/test_agent_tracing.py \
  tests/test_goal_task_integration.py tests/test_planning_call_policy.py \
  --basetemp=evals/results/agent_traces/tool-flow-regression-20261003 -q --tb=short
```

独立超时特征测试：`tests/test_orchestrator_runtime_audit.py::test_audit_new_plan_discards_timeout_recovery`，同安全插件。探针脚本在统一目录，backend运行`.venv/bin/python evals/results/agent_traces/tool-flow-reaudit-20261003/probe.py`；其断言用于复现现状，修复后须变更预期，不作为当前产品正确性验收。

回归通过说明既有校验、输入规则、身份参数、依赖边界、失败隔离等受测路径未回退；不能证明所有自然语言条件都被解析，也不能关闭未知真实模型/数据库或权限端到端风险。同步/SSE生命周期、正常回答、真实LLM1/LLM2语义、知识召回效果及完整工具重试策略未按产品端到端验收；分别保留原issue状态。

## 建议处理顺序

先CF-02/CS-03，明确内部模型输出必填的决策与条件字段，缺失输出进schema失败，合法无条件查询仍允许；不从query关键词自动补条件，不把公开请求默认值一并删除。接着TC-02/RP-02，可靠保留并消费执行/解析失败类别，避免错误责怪用户输入或资料。再按各工具调用政策处理VF-01，按预算处理OR-03；CF-01需要字段语义对齐及真实模型独立表达对照。无需增加第四类错误；三类反馈映射只有在节点实际产生校验错误并接入时才生效。
