# 输入到工具结构化输出复核（E33）

2026-10-04，分支 feat/orchestrator-catalog-integration，基点 8319fb1。
开始时暂存区为空，frontend/src/App.tsx 有用户未暂存修改，保留不动。
范围止于正式 execute_tool_wave；不验收结果解释、LLM3、回答或完成收尾（RP-02）。

## 先验场景与判断依据

预期来自用户确认口径及产品边界，人工商品/订单是独立固定数据；脚本模型只提供可控响应，不代表自然语言理解通过。
所有场景使用正式 LLM1/LLM2 校验、工具参数绑定、内部规划器、handler 和输出包装器；数据读取替换为人工 repository。

| 场景 | 用户表达示例 | 必须检查的预期 |
| --- | --- | --- |
| 多条件搜索 | 想买白色蓝牙键盘，200元以内 | 同一SKU满足颜色、连接与预算；价格/跨SKU反例排除，参数原样交付 |
| 软偏好 | 键盘200元以内，白色优先，其他颜色也可以 | 预算硬准入，白色排序优先；保留合格黑色，limit在排序后 |
| SKU/SPU规格 | SKU1有什么规格／SPU1有什么规格 | 正确粒度及实际读取范围；值关联实际SKU，不跨版本拼接 |
| 上下文 | 这款有什么规格 | 从已选可信历史对象绑定scope；不由category_hint限定范围 |
| 合法不存在编号 | SKU999有什么规格 | 正常not_found，无模型幻觉程序缺陷推断 |
| 对象资料缺失 | SKU1有什么规格（对象存在但specs为空） | data_insufficient、非穷尽，不能当确认无匹配 |
| 非法内部身份 | 请告诉我SPU1的键盘配列有哪些；内部提交name+SKU9999 | 规则反馈一次修复，耗尽停止，禁止错误读取 |
| 多范围目录 | 只看SKU1和SKU2的配列有哪些 | 保留多个可信范围的聚合，不照搬搜索单对象限制 |
| 业务澄清 | 办公键盘有哪些布局 | 需具体化要求原文留存；停止读取，不按模型错误纠正 |
| 能力不支持 | 按预计明天送达筛订单 | 明确不支持，不用无条件订单替代 |
| 订单条件 | 2025年已完成的订单有几笔 | 服务端user_id，时间半开区间和状态传给repository；总数对应条件 |
| 非法订单选择 | 去年有哪些订单；内部explicit无ID或remaining无offset | RULE_ERROR、字段路径、一次修复/耗尽，无错误读取 |
| 格式/结构错误 | 内部非JSON、缺决策字段 | FORMAT_ERROR/SCHEMA_ERROR有界纠错；持续失败不执行宽查询 |
| 超时/异常 | 模型超时、程序异常、取消 | 传输重试限额；程序异常不当格式纠错；取消传播 |
| 编排非法输出 | LLM1/LLM2缺字段或非法任务 | 既有正式交接用例检查阶段反馈与零工具调用，不只检查ok |

## 修复前实测与清理依据

先运行现有七个独立探针：默认编号、软偏好、排除对象冲突三个正常；两类订单规则仅一次调用且planning_failed；目录name+target实际读取SKU9999；存在对象空规格返回not_found且两个完整性标志true。四项异常与VF-01/CF-08对应，不能把旧报告已修复结论当本轮验证。

清理候选：goal_tool_fixtures.py仍patch旧CatalogRepository，而正式facets已使用CatalogSearchRepository。旧selected/CatalogRows模拟筛选、list_facets及CatalogFixtureService的两种对象读取覆盖不再被正式路径调用；须先验证现有适配测试并迁到真实聚合数据边界后才移除。保留自然语言、数值、来源和权限历史断言及E30探针脚本/原始产物。不因旧测试失败删除整个文件。

## 本轮结果

最终指定15个文件：**1038 passed / 33 deselected**，11.15秒，无失败/skip/xfail。33项均为安全插件排除的配置数据库fixture依赖；未运行完整后端。新增 `tests/test_input_tool_contract_chain.py` 共44项，主动在 execute_tool_wave 关闭图，LLM3调用与假持久化计数为零。

- VF-01：explicit无可信ID、remaining无有效offset均显式RULE_ERROR，path=selection；一次修复后可正常返回业务结果，持续错误planning_output_invalid/stop，不重启外层规划。
- CF-08：目录name+target互斥先做规则校验，禁止搜索校验早退绕过来源；其余非成功结果保留标准业务原因、停止读取。复用同一validate_plan，目录明确允许范围聚合，搜索默认单对象限制不变。可信单元素scope的冗余target按共享规则消除，不复制成额外身份；程序原因由trusted参数注入，模型不能覆盖。
- 目录条件冲突现在保留conflicting_conditions，而非统称request_not_supported；裸数字目标缺明确类型时保留query_needs_clarification（“编号N”仍按SKU）。业务问题不触发输出修复。
- 已有SKU无规格时增加field=specs/status=missing；SPU含空规格版本亦为非完整。无确认值且有资料缺口时仅报data_insufficient，不附带not_found。合法不存在对象继续not_found。
- 三种订单时间字段使用正式OrderRepository.count_orders及其SQL在内存SQLite表执行：包含起点、排除终点，排除非完成状态及其他用户。此为隔离SQL执行，不是实际PostgreSQL/存储时区验收。
- 模型主动抛取消时，正式LangGraph以NodeCancelledError保留CancelledError原因向上传播；不返回工具成功。外部取消沿用本轮重跑的tool_retry_policy既有覆盖；未改取消/最终收尾实现。

修复前新增26项首跑13通过/13失败，其中2项是新测试自身误把SPU数当SKU数、把型号背景读取当额外执行，已纠正；其余11项覆盖订单纠错、目录身份/裸数字及资料缺失。之后新增18项扩展上下文、范围聚合、业务停止、订单SQL、超时/异常/取消。实现中一次显式空scope的构造回归已修正；三项取消探针按实际LangGraph异常包装核对原因链，未改生产代码适配测试。

旧七探针前后完整人工输出见 [results](input-tool-contract-chain-results.json)。其中两个订单修复后的unsupported由预设第二份合法计划明确留下“核对时间条件”，不是把原非法选择当业务不支持；新增回归同时覆盖持续无效的stop出口。

### 实际清理

| 删除内容 | 核对用途、调用及替代覆盖 |
| --- | --- |
| goal_tool_fixtures.selected | 仅供旧CatalogRows，使用旧匹配规则重复模拟筛选；现在SearchRecords只提供人工事实，正式工具执行筛选 |
| CatalogRows.list_facets/search_products及类 | 正式工具已不用其repository；trace计数器原绑定list_facets，迁到SearchRecords.load；其他引用仅fixture patch |
| CatalogFixtureService及两种_products_by_*覆盖 | 正式目录不走旧私有商品读取；eval_goal_tool_flow和fixture测试改用CatalogToolService原类，patch正式CatalogSearchRepository.load |

未删除整个测试/评测脚本，未删除任何历史断言场景或JSON证据。现有4项fixture测试保留；订单物流表达补完整脚本模型，因其已不是封闭规则路径。现有28项trace测试保留；6个目录场景迁到完整facet_plan/带来源conditions及planning_output_invalid错误码，仍检查修复次数、参数、读取次数、能力停止、空结果和trace。适配器初测2失败/2通过，trace初测6失败/22通过；迁移后两文件32项全部通过。上述旧失败是测试接入失效，未登记为8个生产缺陷。全仓库Python/文档引用核对后没有遗留旧类调用，报告中的名称仅为删除记录。

### 复现命令

backend目录：

```sh
.venv/bin/python -m pytest -p scripts.orchestration_audit_safety \
  tests/test_input_tool_contract_chain.py tests/test_agent_tracing.py \
  tests/test_goal_tool_flow_fixtures.py tests/test_catalog_query_policies.py \
  tests/test_tool_planning_repairs.py tests/test_catalog_search_tool.py \
  tests/test_llm_feedback.py tests/test_tools.py tests/test_tool_retry_policy.py \
  tests/test_planning_execution_policy.py tests/test_planning_call_policy.py \
  tests/test_goal_planning_validation.py tests/test_planning_handoff.py \
  tests/test_planning_contract_alignment.py tests/test_goal_task_integration.py \
  -q --tb=short -o cache_dir=/private/tmp/pc-chain-cache
.venv/bin/python -m scripts.audit_tool_repair_boundaries --output /private/tmp/pc-chain-after.json
```

修改的Python文件Ruff、git diff --check通过。没有安装依赖、初始化、seed、迁移、暂存、提交或推送。用户前端修改保持原样，docs忽略范围不变。

### 限制与剩余问题

真实模型自然语言质量、配置数据库/PostgreSQL时间存储口径（OL-02）、API及浏览器端到端均未执行。脚本模型输入真实表达只能证明确定性程序边界，不能证明模型会自行生成正确计划。部分既有回归检查工具交接后的状态，但本轮不修改或据此验收LLM3/回答/完成收尾，RP-02保持后续。

VF-01和CF-08仅按上述确定性工具边界关闭；字段输出下游全面消费仍归TI-02/RP-02。KR-01同步知识计算超时、TC-03输出业务一致性、OL-01订单匹配集合决定、OR-06其他旧测试迁移均未处理。此次也确认旧live评测OrderRows尚不接受新filters参数，不能用该适配器验证条件订单；本轮条件订单采用正式repository+内存SQL，适配器迁移缺口记入OR-06，不静默忽略filters。
