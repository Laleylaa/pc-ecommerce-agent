# 独立比较工具删除与回归检查（CC-01 / E20）

2026-10-03；`feat/orchestrator-catalog-integration`、HEAD `6d3f842` 加未提交修改。用户本轮明确授权删除独立比较工具及关联部分；保留开始时全部其他修改，无暂存、提交、推送、数据库操作或真实模型调用。

暂存范围说明：本报告的回归数字来自删除完成后的完整工作区，包含当时其他尚未提交的修复；本次独立暂存只纳入比较工具删除，未把其他修复混入。该数字不能直接当作独立暂存快照的全套测试成绩。 拆分后额外对“HEAD＋仅比较删除”的临时副本执行100项隔离验证（正式注册、完整交接测试文件、双对象工具返回及历史记忆/查询），全部通过；该副本app/tests/scripts的Ruff也通过。

## 行为与职责

- 删除 `catalog_compare` / `catalog.compare`：正式契约、注册绑定、公开/内部输入与输出、内部比较计划/Prompt/handler、专属差异计算、比较级别参数、路由能力和结果类型、依赖提取、结果投影及比较记忆写入均移除。LLM2提交旧名称会进入现有校验修复，持续提交则规划失败；旧registry名称返回unknown_tool。
- 比较意图仍留在LLM1 goal；LLM2分别查询需要的商品事实，已返回事实足够时不追加“比较”工具任务。名称定位仍在搜索工具；搜索一次一个明确对象。没有扩展多对象查询、limit、8任务/2波次预算或分页能力，没有加入程序关键词改写。
- 搜索、目录、订单、政策、知识五个工具保留。工具返回的商品事实由既有products artifact接收；未知上游对象仍按正式结果机械绑定，不改query；订单认证与单编号冲突规则保持。
- 搜索共用的型号聚合 `_aggregate_product_series` 保留，其内部模型由 `CatalogSeriesComparisonItem` 更名为 `CatalogSeriesSummary`。比较专用成对差异计算删除，当前不再提供该专用结构化输出。
- 历史会话只在校验读取时丢弃退役的comparison字段，不写库、不迁移；其他候选/引用商品、订单上下文保留。仅存在于旧comparison字段的对象不再作为可信历史来源，不能从旧文本猜回编号。
- 活跃Prompt、示例、评测脚本和输入用例同步；历史评测输出/报告保持原证据，不作为现行可调用接口。比较工具专属测试/参数场景退役；混合文件中的搜索、目录、订单、政策等独立场景保留。

## 实际链路

输入及只读上下文 → LLM1目标/可信对象引用 → LLM2完整工具任务 → 既有来源/数量/身份/依赖校验 → contracts绑定执行五个正式工具 → 归一化结果/products或其他artifact → 必要下游参数绑定。

关键位置：`app/tools/contracts.py`、`app/tools/catalog.py`、`app/tools/schemas.py`、`app/agent/goal_planning.py`、`app/agent/prompts/planning.py`及`planning_examples.py`、`app/agent/artifacts.py`、`app/agent/projections.py`、`app/schemas/context.py`、`app/services/context.py`。

## 本轮验证（不复用历史通过数）

使用删除前工作区副本作对照，包含当时尚未提交的用户成果，未使用git回退。所有pytest启用 `-p scripts.orchestration_audit_safety`；事前检查fixture闭包及相关脚本，排除db_session_factory依赖，阻断网络和AsyncEngine.connect。

| 检查 | 删除前 | 删除后 |
| --- | --- | --- |
| 16个定向工具/规划/交接文件 | 973通过、5失败、39写库fixture排除 | 927通过、同5失败、33写库fixture排除 |
| 6个涉及旧比较场景的链路文件 | 83通过、59失败、5写库fixture排除 | 66通过、46失败、无排除 |
| 增强后的正式图工具交接测试 | — | 41通过，无失败/排除 |

16文件最后一次运行含41项交接测试，随后增强同一文件的双SPU多版本断言并再次完整通过；不重复累加为测试数量。六个旧链路文件保留112项，逐项对照状态一致、**新增失败0**。退役30项旧比较场景不算修复成功；删除前的59个失败中13项随比较场景退役。全部存留的51个失败（5+46）均在删除前存在，没有skip/xfail掩盖。

额外证据：五个存留工具的公开schema（含默认值/数量/类型）、输出schema、描述、注册名、运行时注入声明与超时逐项相等。真实handler构造及旧名称拒绝无需写库fixture。脚本模型走正式图、正式搜索服务与内存repository：两SKU、两完整SPU（每个两版本）、缺失规格均能到结果artifact；历史SKU/SPU对象分别查询并保留身份；原有筛选子集、替代候选、来源/硬scope、订单身份与依赖拒绝测试继续执行。

本次编写测试时出现并修正：混合已知/未知绑定测试误带search硬scope、内部脚本planner缺响应、旧观察用例总数断言未随3个比较场景退役更新。均未通过放宽生产校验解决。初次源码删除误带走相邻搜索常量，由静态检查发现后从本轮开始快照精确恢复。

失败细节见 [结构化记录](comparison-tool-removal-results.json)。5条test_request_router旧例是direct/session-grounded/普通回答SSE旧脚本未迁移；其余46条仍属既存链路/测试问题，按OR-06继续逐项核对，不宣称都是同一种原因或已修复。工具结果交接通过不等于正常LLM3、真实自然语言效果、真实数据库或浏览器验收。

复跑（backend；仅指定范围，不是完整后端测试）：

```sh
PYTHONPATH=. .venv/bin/python -m pytest -p scripts.orchestration_audit_safety tests/test_tools.py tests/test_planning_handoff.py tests/test_goal_planning_validation.py tests/test_planning_contract_alignment.py tests/test_planning_execution_policy.py tests/test_catalog_search_tool.py tests/test_catalog_search_outcomes.py tests/test_working_memory.py tests/test_conversation_context.py tests/test_answer_context.py tests/test_orchestrator_outcomes.py tests/test_router_capabilities.py tests/test_goal_task_integration.py tests/test_request_router.py tests/test_goal_tool_flow_fixtures.py tests/test_tool_eval.py -q --tb=short
PYTHONPATH=. .venv/bin/python -m pytest -p scripts.orchestration_audit_safety tests/test_goal_task_integration.py tests/test_orchestrator_task_dag.py tests/test_orchestrator_plan_execute_e2e.py tests/test_orchestrator_tool_observation_eval.py tests/test_orchestrator_wave_loop_cases.py tests/test_agent_tool_wiring.py tests/test_orchestrator.py -q --tb=short
```

最后清理单值参数分支后，交接测试文件再次105通过。修改过的评测模块均可导入，活跃输入的工具名均在正式注册中；离线消融脚本完成30个人工场景/60次运行（仅确认脚本运行，不计为业务工具或回答验收）。

静态检查：app/tests/scripts Ruff通过；全量仅收集测试：1498项中54项写库fixture排除、1444项成功收集，未执行完整后端；所有存留测试可导入。diff空白检查通过。源码/活跃输入已无旧工具接口，剩余旧名称仅用于拒绝/历史兼容测试；历史评测证据保留。

## 下一步

CC-01删除及确定性交接完成；不因此关闭其他工具缺陷、模型语义问题和回答验收。OR-06按现行两阶段入口处理旧用例；RV-03后续若验收真实比较回答，以两SKU、两完整型号、筛选候选和缺字段四场景检验事实范围与表达，不恢复旧工具或扩大任务预算。OL-01订单匹配对象决定保持待定且安全阻断。
