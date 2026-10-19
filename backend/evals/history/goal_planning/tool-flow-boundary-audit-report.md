# 工具调用与结果边界独立审查（E23）

日期：2026-10-03。项目 `pc-ecommerce-agent`，分支 `feat/orchestrator-catalog-integration`，
HEAD `f90c5f6` 加工作区。开始时 trace 已提交、暂存区为空；既有业务、测试、前端及文档修改保留。
本轮只修改 `app/tools/catalog.py`、`app/tools/contracts.py`、既有 `test_agent_tracing.py`
及评测/开发说明；未暂存、提交、推送或改变忽略规则。

## 核对的实际链路

| 阶段 | 输入与输出 | 正式位置 |
| --- | --- | --- |
| 前置处理/上下文 | 当前消息、服务端用户身份 → 前置规则、只读可信对象 | `agent/graph.py`、`agent/workflow.py` |
| LLM1 | 当前输入与必要上下文 → 无 scope 的 GoalPlan → 内部 RequestRoutePlan | `goal_planning.route_goals` |
| LLM2 | 准入目标、选中对象、五工具契约和派生 schema → 完整任务/依赖/阻塞理由 | `execution_definition`、`plan_tools`、`validate_execution` |
| 执行前绑定 | 已提交参数、可信来源、正式上游对象 → 原 query 与机械绑定的未知 ID/scope | `graph._prepare_tool_call`、`bind_arguments` |
| 正式调用 | public schema → 服务端 runtime 注入 → internal schema → handler → output schema | `contracts.build_tool_catalog`、`RegistryToolExecutor`、`execute_bound_tool` |
| 必要交接 | ToolExecutionResult → normalized_tool_results → task_artifacts/task_status → 下游对象 | `execution.py`、`outcomes.py`、`artifacts.py`、`bound_inputs` |

`tools/registry.py` 是点号名称的兼容入口，当前编排使用正式 ToolCatalog/Executor。
没有恢复比较工具，也没有新建 registry。五工具名称、公开/内部 schema、默认数量、身份注入、
搜索硬 scope 和订单对象来源由实际实现及本轮隔离契约测试核对；没有因此宣称全部自然语言效果正确。

## 四个独立缺陷与最小修复

| 问题 | 触发条件与最早偏离 | 修复及结果 |
| --- | --- | --- |
| 内部 JSON 修复未执行 | 目录模型首次返回 `not JSON`；`LLMCatalogQueryPlanner._invoke_with_retry` 的异常反馈读取未赋值的 `plan_data`。正式图只留下 1 次内部模型调用，工具失败，即使准备好的第二份输出完全有效 | 每次尝试初始化空解析结果，再沿既有反馈进入第二次调用。原 query/limit 保持；成功修复保留完整筛选，持续失败仍失败，不增加重试次数。位置 `catalog.py:838` |
| 目录持续解析失败变成另一查询 | 两次返回 `{"facet":"invalid"}`；`_safe_plan_facets` 捕获校验失败后调用规则解析。原请求“白色蓝牙键盘有哪些布局可选”实际查询品牌，filters 变成 `{}`，返回 Keychron，随后被归一化为 usable | 移除失败后的替代查询，沿正式包装器返回 `ok=false/execution_error`；repository 零调用，artifact 不可用、任务 failed。正常配置的规则 planner 仍保留。位置 `catalog.py:1510` |
| 不支持标志被清除 | 内部模型返回合法 `supported=false` 和具体原因；`_validate_catalog_facet_plan` 重置为 true/null。之后仍执行品牌查询，任务 succeeded | 保留模型的拒绝标志及原因，程序仍可因非法字段追加拒绝。正式输出保留 unsupported_query，normalized 为 unsupported、任务 unavailable，repository 零调用。位置 `catalog.py:3688` |
| 错误输出类型越过包装器 | handler 故障注入返回 None/dict/str；output 校验直接调用 `.model_dump`，泄漏 AttributeError，未返回 ToolExecutionResult，也缺少 tool_output_rejected | 在 output 校验边界检查 BaseModel 类型并捕获类型错误，返回 execution_error。错 schema 的 BaseModel 继续拒绝。正式图交接为 error/不可用 artifact/failed。位置 `contracts.py:429` |

最后一项是错误返回的防御契约缺陷：本轮没有观察到现有五个正常 handler 自行返回 None/dict/str。
`supported=false` 是人工注入的模型输出，验证其确定性消费，不证明真实模型对本请求的分类正确。
前三项修改归工具内部解析与领域校验，第四项归正式工具输出包装器；编排不补关键词、参数或事实。
目录能力说明同步失败/不支持口径；没有修改正常 LLM3、业务排序或知识检索效果。

## Trace 与关联定位

统一根目录 `backend/evals/results/agent_traces/`，全部人工数据、本地记录，无云上传。

- 修复前：[索引](../../results/agent_traces/audit-before-confirmed-20261003/report.md)、
  [results](../../results/agent_traces/audit-before-confirmed-20261003/results.json)、
  [manifest](../../results/agent_traces/audit-before-confirmed-20261003/manifest.json)。
- 最终修复后：[索引](../../results/agent_traces/audit-verified-20261003/report.md)、
  [results](../../results/agent_traces/audit-verified-20261003/results.json)、
  [manifest](../../results/agent_traces/audit-verified-20261003/manifest.json)。

以下三例共用 `goal_id=goal_1`、`task_id=task_1`、`tool_call_id=planned_task_1`、`wave=1`。
`trace_id` 为根 run，events.run_id 对应 execute_tool_wave 的原生子 run；完整 parent_run_id 见 results。

| Case | 修复前 trace_id | 最终 trace_id | 关键证据 |
| --- | --- | --- | --- |
| repaired | `01a1015e-3630-7423-a1dd-b5aa755bb750` | `01a10164-862f-70b0-83a2-03a047196c2a` | 前：1 个内部模型 run → tool_handler_error；后：2 个 run，第二份 payload 含校验反馈及原 query/limit → 正确 style 值 |
| invalid_plan | `01a1015e-36aa-7382-acd4-df6238e8e2fc` | `01a10164-866c-7320-8f06-f4d8df8e1ccb` | 前：两个非法模型输出 → tool_handler_output（rule_based_fallback/brand/空 filters）；后：两个非法输出 → tool_handler_error/execution_error |
| unsupported | `01a1015e-36c9-7930-b134-326c05d1b1aa` | `01a10164-8689-77a2-9eab-60d519437cc1` | 原生模型输出 false，对比 tool_handler_output 的 supported；后保持 false，ledger 为 unavailable |

错误输出前例保留独立包装器 run 的 AttributeError；后例依次有
`tool_handler_output → tool_output_rejected → tool_result`，正式图再检查
`normalize_tool_results / extract_task_artifacts / update_subquery_ledger`，IDs 在 results 内逐例列出。
修复后 10 个输入/输出边界用例共留下 14 份记录（错误输出同时测独立包装器和正式图）。
六个目录例及四个错误输出图例都在 `tool_handoff` 主动停止；GeneratorExit 是预期关闭。
`recording_complete=true` 只证明记录完整，业务结论依靠独立断言。

修复前 manifest 已在生产修改之前记录 HEAD、dirty、生产源码/Prompt/脚本/锁文件哈希与依赖版本；
当时未额外采集 pytest 文件哈希。最终 manifest 追加本轮测试文件哈希、命令和成绩。
完整请求/模型原始输出/工具结果保留在本地 trace，不放 docs，不覆盖历史 trace。

## 实际验证与限制

1. 新增原始定向：6 failed / 3 passed / 18 deselected。
2. 补齐结构错误回退探针：7 failed / 3 passed / 18 deselected；7 项均为新缺陷复现。
3. 修复后定向：1 failed / 9 passed / 18 deselected；唯一失败是新测试误把原生 LLM 的
   `inputs.prompts` 当成 messages，不是业务失败。修正字段读取后 trace/正式图/策略 128 passed。
4. 补充错误输出的完整正式图交接，并使用现有 evaluate 的停止边界，最终 **869 passed / 33 deselected**；
   0 failed、skip、xfail。没有删除有效断言、放宽契约或增加测试框架。
5. Ruff、git diff --check 通过。修复前后 `eval_goal_tool_flow --scripted` 均通过有限单 SKU 冒烟。
6. `make doctor` 失败：Podman 不可用；Python 3.11.15 可用。未启动或变更容器/数据库/依赖。

最终命令（backend 目录；运行前检查 conftest、fixture、测试助手和脚本间接依赖）：

```sh
.venv/bin/python -m pytest -p scripts.orchestration_audit_safety \
  tests/test_tools.py tests/test_planning_handoff.py \
  tests/test_goal_planning_validation.py tests/test_planning_contract_alignment.py \
  tests/test_planning_execution_policy.py tests/test_catalog_search_tool.py \
  tests/test_catalog_search_outcomes.py tests/test_agent_tracing.py \
  tests/test_goal_task_integration.py tests/test_planning_call_policy.py \
  --basetemp=evals/results/agent_traces/audit-verified-20261003 -q --tb=short
```

33 项全部通过 fixture 闭包排除配置数据库写入，不属于通过项。安全插件阻断 socket/AsyncEngine，
工具 repository、上下文和审计采用人工/内存或假实现；云 SDK 测试禁用后台线程并替换写入方法。
安全插件不是完整沙箱。没有读取/打印凭据文件，也没有上传 trace。

本轮所选集合没有旧测试失配失败。OR-06 旧观察/回答/SSE 集合未重跑，不借历史失败恢复旧契约，
也不更新其历史成绩。CF-01 的真实布局表达、OL-01 认证匹配集合决定、RV-03/RV-04 的真实链路与
保真评测保持未验收。未运行真实模型、配置数据库、全后端/make check、浏览器、人工写操作或正常 LLM3 验收。

下一最小行动：按 CF-01 对原失败布局/配列表达做工具专项真实模型复核，检查原始计划、
正式字段、查询约束和返回选项；这需单独授权真实模型调用。本轮四项确定性修复已隔离验收，
不以它们关闭其他 issue 或升级 modules 稳定基线。

## 根因续查：程序错误与模型效果分别解释

2026-10-03，用户要求进一步调查根因。本次仅调查、隔离探针和文档记录，未继续修改生产代码。
原来的四项程序修复成绩不代表模型理解准确率已经提高。

1. **JSON 反馈错误的根因是失败阶段没有区分。** `_invoke_with_retry` 把文本解析和解析后的结构校验放在同一 try 内，而反馈函数要求解析后的 dict。文本解析失败时根本没有这个对象。已有重试测试主要用可解析 JSON 的字段错误，无法覆盖这条控制流。变量初始化修复覆盖任意文本解析失败，与增加商品关键词/规则表无关。更完整的反馈应分别保留原始输出、解析失败或已解析字段的结构错误；不需要预建通用重试框架。
2. **错误回退的根因是把“还能产生结果”当成“还能满足原请求”。** 目录 LLM 与规则解析器没有同等语义覆盖；后者 `_infer_spec_key_from_text` 只有少量规格，缺少 style/布局，最终使用默认 brand。没有语义等价证明，规则回退仍按正常查询返回。删除回退修复了错误执行，但没有改善内部模型解析准确率。
3. **supported 覆盖的根因是能力权责混在单一标志中。** 程序应验证字段、类型和正式能力，但通过这些检查不能证明请求形态/语义也受支持。旧校验先清除模型拒绝，再只检查已表达字段，相当于把“这些字段合法”当成“请求完整且受支持”。保留 false 阻止错误放行，仍不能解决模型错误拒绝；真实不支持、错误字段映射和解析缺失需要区分。
4. **错误输出类型的根因是把类型注解当成运行时保证。** 注册阶段检查签名，不能保证每条 handler 返回路径都遵守类型。旧 output 校验先执行 BaseModel 专属方法，只处理 Pydantic ValidationError，结果类型错误在校验之前泄漏。先验证对象类型再验证内容，是包装器层面的通用修复；不是为某个工具写特例。

**关于“为什么简单问题也会理解错”：原 E23 使用人工非法输出，不能证明真实模型出错。**
另查历史 `tool-flow-independent-tasks-2026-10-01.json`：office_layout 的真实模型正确保留 facet=spec_value、键盘、白色、蓝牙及300预算，却输出 spec_key=layout，并明确认为布局不受支持。该历史错误更接近业务词与内部字段未对齐，而非完全没有理解请求；同批另两例使用style成功。历史源码哈希与当前版本不同，不把它当本次真实模型成绩。

当前源码发现的结构原因/风险：

- 目录 Prompt 只列允许的字段名，没有说明 style 的配列含义；搜索的纯规则/规格说明已有这一语义。Prompt与程序字段说明分散维护。没有证据证明 Prompt 存在相互矛盾的规则，但存在领域解释缺口。
- 模型输入 `explicit_overrides` 把内部默认 facet=brand 也列成明确覆盖项。当前正式公开调用没有提交facet；此描述混淆默认值与调用方确定的要求。能证明载荷事实，尚不能证明其对真实输出的因果影响。
- 内部调用采用普通 ainvoke + 文本JSON解析，调用时没有绑定 FacetQueryPlan 输出schema。该模型几乎所有字段都有默认值；空对象也能通过结构校验。不能通过格式合法证明用户语义完整。
- `_validate_catalog_facet_plan_data` 忽略 `_validate_catalog_facet_plan` 返回的 unsupported 状态；重试器只接异常。因此未知 spec_key 可以直接进入不支持出口，准备好的第二份修正输出不会被调用。不能把真实能力不支持一律改成解析重试，需先对齐错误分类。

**本次三条当前代码探针均通过独立现状断言**（不是修复验收）：

| 输入模型输出 | 当前实际结果 | 证据解释 |
| --- | --- | --- |
| `{}` | 一次模型调用，brand/null category/空filters/supported=true | 缺字段被默认值补成合法查询，新增CF-02待修复 |
| `spec_key=layout`，随后准备有效style输出 | 只调用一次；最终unsupported spec_key: layout | 确认目录校验返回状态与输出修复器异常接口未接通，CF-01继续跟踪 |
| style + 白色/蓝牙完整条件 | 一次模型调用，合法计划保留原条件 | 正常对照；不能推定真实模型能稳定产生它 |

新trace、manifest和results在 `results/agent_traces/root-cause-audit-20261003/`；全部人工输出、原生回调、无repository调用，socket/AsyncEngine阻断。trace_id依次为 `01a10175-2ac7-72a2-a00b-45aba5571296`、`01a10175-2ac9-7562-8a23-07f309a54987`、`01a10175-2acb-78e3-9005-015ea5e00d2e`。未重跑完整回归或真实模型，没有提升准确率的前后对照。

下一步先处理CF-02的完整输出结构与参数来源，再按CF-01对齐字段语义、解析错误反馈和能力拒绝；最后用同输入及独立表达做真实模型对照。不能仅增加“布局”关键词或改一次Prompt就宣布根因解决。


## 三类输出校验反馈（E24）

2026-10-03，`feat/orchestrator-catalog-integration`、HEAD `f90c5f6`加全部既有及本轮未提交修改；未暂存/提交/推送。本节为新一轮成绩，不替代上方E23历史记录。

- 用户最终确认仅FORMAT_ERROR/SCHEMA_ERROR/RULE_ERROR。新增`app/core/llm_feedback.py`纯模块，按三类映射纠正指令；原生code作诊断保留，不据异常字符串猜业务类型。普通运行时异常不生成模型纠错指令，无通用事实或自然语言语义校验。
- 目录恢复分开解析和计划校验，反馈带本轮raw_output/可选parsed_output/stage及统一errors；根对象不符合归schema，JSON语法错误归format。首次失败可修复，连续失败保留原因链；第一轮字段错第二轮语法错无旧字段残留。保持两次调用上限、原query/limit/可信参数、代码块/文本包裹兼容及正式execution_error出口。
- 共享generate_plan接入三类反馈并在trace和修复消息使用同份结果。Pydantic包裹的显式业务错误保留RULE_ERROR，LLM2公共工具字段补齐完整任务路径。Goal/InputBinding、订单选择、scope/条件关系及旧商品规划的既有业务判定只加标记，不更改规则；合法同query不同ID任务不被去重。
- 初次定向143 passed/7 failed：新增ProductQueryPlan夹具漏query，新增RoutedSubquery夹具漏reason_code/planning_pending；后者产生6个失败（含误入额外修复的对照）。修正夹具、不放宽断言后150 passed。随后增加公共规则分类/路径适配及5例，最终新文件37例；全部范围906 passed、33写库fixture闭包排除，无失败/skip/xfail。
- Ruff范围覆盖五个修改的实现文件、新模块、新测试及两份更新的旧测试；git diff --check通过。独立进程导入反馈模块确认未加载agent/tools/SQLAlchemy/LangChain/LangSmith。此处所有检查属于本轮，无真实模型、配置数据库、浏览器、全量后端测试或自然语言效果验收。

最终命令（backend目录）：

```bash
.venv/bin/python -m pytest -p scripts.orchestration_audit_safety \
  tests/test_llm_feedback.py tests/test_tools.py tests/test_planning_handoff.py \
  tests/test_goal_planning_validation.py tests/test_planning_contract_alignment.py \
  tests/test_planning_execution_policy.py tests/test_catalog_search_tool.py \
  tests/test_catalog_search_outcomes.py tests/test_agent_tracing.py \
  tests/test_goal_task_integration.py tests/test_planning_call_policy.py \
  --basetemp=evals/results/agent_traces/feedback-verified-20261003 -q --tb=short
```

安全插件阻断网络及AsyncEngine连接，排除db_session_factory间接依赖，不访问配置数据库。统一目录`backend/evals/results/agent_traces/feedback-verified-20261003/`保存本轮manifest/results和32份既有正式图/调用策略回归的原生trace；其中目录失败事件确认FORMAT_ERROR 3次、SCHEMA_ERROR 2次，规则分类另由隔离新用例直接验收。完整JSON不写入docs。manifest固定本轮四个修改的实现文件、新模块、三份测试及锁文件hash，源代码与测试须同版本获取。

CF-03按已接入目录恢复/共享规划路径关闭。独立catalog_search v3和其他非generate_plan调用未强制迁移；CF-01模型布局理解、CF-02缺字段默认化、RV-03真实链路仍开放。关闭不代表所有LLM节点已统一或模型一次修复必然成功。
