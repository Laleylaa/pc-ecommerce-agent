# 正式工具契约与结果交接审查

日期：2026-10-03。分支 `feat/orchestrator-catalog-integration`，HEAD `6d3f84211734bbc61ed0daf3a4d9171c4283a46d` 加工作区修改。开始时暂存区为空，有14个已修改文件及既有未跟踪产物；未暂存、提交、推送、回退或清理。本文成绩仅对应本轮。

## 实际调用链

| 阶段 | 真正输入/输出 | 消费方与入口 |
| --- | --- | --- |
| HTTP与前置规则 | 认证用户ID、ChatRequest；输入规则结果或完整输入 | api/routers/chat.py → graph._initial_state / workflow.input_rules |
| 上下文与LLM1 | 当前文本、必要history、只读context_objects → GoalPlan（无scope） | goal_planning.route_goals；校验来源后适配RequestRoutePlan |
| LLM2 | 准入目标、所选context对象、正式工具说明及派生schema → ExecutionPlan | execution_definition / plan_tools / validate_execution；一次输出修复，不执行业务 |
| 执行前 | 已校验arguments、身份来源、依赖结果 → 完整公开参数 | graph._prepare_tool_call → bind_arguments；仅未知结果机械绑定，不补写query |
| 正式工具 | public model → 服务端runtime字段 → internal model → handler → output model | contracts.RegistryToolExecutor → execute_bound_tool；六个真实绑定由build_tool_catalog建立 |
| 结果交接 | ToolExecutionResult → 业务outcome、task artifact、任务状态 | execution.execute_tool_wave → normalize_tool_results → extract_task_artifacts → update_subquery_ledger；后续bound_inputs读取正式对象 |

`registry.py`是沿用点号名称和内部输入的兼容入口，app中没有当前编排调用者；不新建或替换registry。正式六工具均只读，人工请求创建不在工具列表中。API仍注入认证身份，订单repository带user_id谓词；人工请求独立API校验会话及订单归属，本轮未调用写入。

## 独立确认与修复

1. **比较纯空白query可进入无对象限制查询。** `CatalogComparePublicInput`及内部输入原来只有min_length；默认规则planner会让空白query进入`_compare_candidates_from_plan`并查询商品。内存repository探针实测被调用。两处改用已有NonBlankQuery，LLM2和正式绑定均拒绝；无关键词补丁。
2. **比较结果位置缺少已知上限检查。** 上游catalog_compare limit=2而下游positions=[3]（默认limit=5而选第6项亦同），原先LLM2交付成功。把比较纳入既有result_limits校验，使用public schema默认值，支持任务数组倒序、一次修复及持续失败；不更改参数数量。
3. **订单明细分析结果不能被正式绑定消费。** 工具返回order_analysis/analysis_orders，artifact保留，bound_inputs却只读order/candidates，合法第2笔被报invalid_input。现在按analysis_orders原序读取，机械绑定order_id，query原样。实际不足、多对象未选择一个、空结果仍不调用下游handler；非穷尽窗口可引用已返回位置，但不能冒充全集。
4. **单笔工具接受多个或冲突的明确订单号。** `列出最近的订单101和订单102`原先通过LLM2和正式绑定，真实OrderToolService转而调用最近订单列表（user_id=7, limit=5, offset=0），丢失明确对象范围。公开/内部输入共用的validate_order_lookup_selection复用explicit_order_ids，拒绝多个明确ID及query/order_id冲突。LLM2可修复为两个独立完整调用；不由程序猜一个或补query。

订单分析修复还补了必要的安全边界：operation=match_orders返回原始有界窗口，focus_order_ids由品类/品牌启发式生成，不能证明完整筛选成立。临时实现若直接绑定所有analysis_orders，会把无匹配证据的订单交给下游（4条负例复现）。最终对这类依赖拒绝绑定，返回明确invalid_input及原因，handler零调用；普通detail明细窗口仍可绑定。工具描述由“exhaustive snapshot”改为有界窗口并要求检查is_exhaustive。未重写订单匹配业务；能力限制跟踪OL-01。

比较工具保留：SKU事实对比、SPU有效版本聚合和两两规格差异仍有真实实现和消费方，不能仅凭搜索也返回事实就移除。去留仍按CC-01单独决定。

## 本轮验证

全部采用脚本模型、假repository、人工数据、假审计。启用`scripts.orchestration_audit_safety`阻断socket及AsyncEngine连接，检查并排除db_session_factory及其间接依赖。安全插件不是完整沙箱。

- 第一轮新增用例：17 failed / 2 passed / 301 deselected，其中1个空结果断言是新测试漏跑正式状态刷新节点；补齐生产节点顺序后，该对照通过，订单合法绑定仍3项失败。
- 完整定向缺陷复现：19 failed / 3 passed / 301 deselected；最小修复后22 passed / 301 deselected。
- 首次扩大范围：1 failed / 774 passed / 40 deselected。唯一失败为旧test_catalog_public_schemas_are_query_first仍断言比较无sku_ids/spu_ids；只更新为现行公开字段集合，保留其他契约断言。
- 将原本不需要数据库的注册构造测试改为隔离检查真实build_tool_registry/ToolCatalog绑定；另增加非穷尽订单窗口对照，随后777 passed / 39 deselected。
- 筛选分析依赖负例：4 failed / 10 passed / 113 deselected，确认直接接入原始analysis_orders会误调用下游；增加match_orders阻挡后进入最终回归。
- **最终：784 passed / 39 deselected，0 failed / skip / xfail。** 39项全部依赖写配置数据库fixture，未执行，非通过项。无通过数量复用。
- Ruff与git diff --check通过；schemas独立导入通过，未加载contracts、orders服务或数据库模块。make doctor发现Podman不可用，Python 3.11.15可用；未启动或修改容器/数据库，未变更依赖和锁文件。

最终命令（backend目录）：

```sh
PYTHONPATH=. .venv/bin/python -m pytest -p scripts.orchestration_audit_safety \
 tests/test_tools.py tests/test_planning_handoff.py \
 tests/test_goal_planning_validation.py tests/test_planning_contract_alignment.py \
 tests/test_planning_execution_policy.py tests/test_catalog_search_tool.py \
 tests/test_catalog_search_outcomes.py -q --tb=short
```

覆盖六工具schema、真实注册签名、公开/内部转换、默认/显式limit、身份注入、内存SQLite正式订单归属谓词、搜索scope及v3结果状态、订单/比较依赖和失败交接。订单新集成例走生产LLM1/LLM2节点后，执行正式工具波、归一化、artifact和ledger节点，止于必要结果交接；没有正常LLM3。

未运行：完整后端/make check、真实模型、配置数据库、HTTP/SSE聊天端到端、浏览器、人工请求写入、正常回答质量和开放自然语言效果评测。未改API/前端/业务数据，未升级modules稳定基线。

## 剩余与最小下一步

本轮已确认确定性缺陷均修复并隔离验证。订单筛选分析若要驱动下游，须先决定是否提供可证明匹配的对象集合及完整性状态（OL-01）；未支持前保持拒绝，不用focus_order_ids代替。比较是否移除仍是CC-01产品/工具设计决定。真实链路验收和旧回答/SSE测试分别按RV-03、OR-06跟踪，不以本轮工具专项成绩关闭。
