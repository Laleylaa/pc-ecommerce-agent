# 编排到商品工具的三项口径（E32）

2026-10-04，`feat/orchestrator-catalog-integration`，基点b390d61加保留的工作区。
本轮针对scope职责、硬/软条件、待具体化要求的业务反馈；不重写编排，不进入正常LLM3、最终回答或泛化工具输出消费。

## 行为与复用

- LLM1补全历史对象、不生成scope；LLM2从完整query和可信结构化对象决定任务范围。明确编号可以省略scope，参照对象不自动成为范围。搜索名称定位和目录名称定位规则分别说明；未知名称对象规格先搜索，再绑定目录。目录内部模型输入增加原样调用方scope，不复制scope成为内部target。运行时仍只机械落实LLM2声明的绑定。
- 共用SearchCondition及strength，不新增工具或两套条件类型。catalog_fields中的纯hard_condition_verdict/preference_rank供执行和schema复用：硬条件准入，软条件按确认满足项数优先、同分时已知差异先于未知事实；组内沿用用户sort/default，最后limit。数值及非数值矛盾检查只求硬条件交集，仍校验软条件的字段能力、类型和来源。
- 搜索exact/total_match_count现在表示所有硬条件在同SKU确认成立；软条件的match/mismatch/unknown完整保留在condition_checks。未知软条件不加入unknown_sku_count。现有明确标注的硬条件替代机制保留，不能冒充正常精确匹配。型号偏好评分与代表版本取单个真实SKU，不跨版本拼偏好。
- 目录使用同一硬条件准入；软偏好只排序选项/维度内取值，保留非偏好SKU贡献的值及计数。确定排除的记录不进入冲突集合；潜在未知和实际匹配的冲突仍保留，修复CF-06。
- 内部计划显式提交clarification_requirements，逐字引用query中需具体化的用途/主观要求，缺字段走原SCHEMA_ERROR，虚构来源走原RULE_ERROR。没有增加关键词场景转换或第四种模型反馈类型。
- 业务结果复用纯ResultPolicy：requirement_needs_clarification对应clarification_required/clarify_query、不可重试；具体能力缺失仍request_not_supported，资料不足仍data_insufficient。程序不猜静音等属性，不执行部分筛选；其他明确条件保留在query_plan。工具调用正常返回不代表筛选完成，搜索query_state=not_executed，目录停止结果不声称完整。
- 编排仅为目录上述三种停止反馈保留原因和完整材料到Artifact，供后续LLM3使用；正向目录产物的全面字段接入、RP-02、正常回答生成和收尾未处理。

## 本轮验证

最终定向范围：**962 passed / 33 deselected**。12个文件，含新增`tests/test_catalog_query_policies.py`的29个独立行为用例；另保留旧偏好用例并按用户确认的新语义更新预期。首轮4个旧业务断言不符合新语义，新增夹具的provider接口、scope归一化和封闭表达绕过模型等预期问题已修正，最终无失败/skip/xfail。

命令（backend目录）：

```sh
.venv/bin/python -m pytest -p scripts.orchestration_audit_safety \
  tests/test_catalog_query_policies.py tests/test_tool_planning_repairs.py \
  tests/test_catalog_search_tool.py tests/test_llm_feedback.py tests/test_tools.py \
  tests/test_tool_retry_policy.py tests/test_planning_execution_policy.py \
  tests/test_planning_call_policy.py tests/test_goal_planning_validation.py \
  tests/test_planning_handoff.py tests/test_planning_contract_alignment.py \
  tests/test_goal_task_integration.py -q --tb=short \
  --basetemp=/private/tmp/pc-three-policies-delivery \
  -o cache_dir=/private/tmp/pc-three-policies-cache
```

覆盖白色优先后保留其他颜色、预算等硬约束、组内价格排序、排序后limit、无偏好匹配、偏好未知、软硬同字段差异、单SKU偏好评分与代表版本、目录选项全量保留、冲突集合范围、完整输出/来源反馈、三种业务停止结果以及正式图scope和反馈材料交接。

相关源码/测试Ruff及diff空白检查通过。安全插件排除33个配置数据库fixture，阻断socket/AsyncEngine；模型为脚本、记录为内存数据。未调用真实模型或配置数据库，未运行正常LLM3、回答、HTTP/SSE及页面验收，不声称自然语言零错误。

## 独立旧边界复核与未完成项

运行`python -m scripts.audit_tool_repair_boundaries --output /private/tmp/pc-three-policies-boundaries.json`，正式包装器、脚本模型、内存记录，阻断外部I/O。
新证据为`catalog-query-policies-boundaries.json`；case名称沿用历史缺陷名称，不能按名称判断当前失败。

7个场景中：默认编号合法；目录偏好返回75%和全尺寸，完整性正确；排除黑色SKU冲突后白色75%集合完整。其余4个异常仍复现：订单explicit缺可信编号、remaining缺offset未进入规则纠错（VF-01两类）；目录name/target校验早退漏处理，以及已有对象空规格被标成完整空结果（CF-08两类）。

CS-01按本阶段停止查询并记录待具体化需求的政策关闭；CF-09按工具内软硬语义关闭；CF-06限正式聚合范围关闭。TP-02只补scope当前说明和已验场景，不整体关闭其他绑定组合。KR-01、TC-03、OL-01及OL-02数据库时间口径保持原状态。后续LLM3实际能否正确汇总并提问，仍未验收。
