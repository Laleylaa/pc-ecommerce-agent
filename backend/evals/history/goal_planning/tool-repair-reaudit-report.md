# 工具修复后复核（E30）

> E31用户明确商品“编号N”默认SKU，撤回本文对此项的澄清要求；相应规则/Prompt/校验已实现。当前口径见[编号默认报告](catalog-number-default-report.md)，其余复现保持开放，以下保留E30当时判断与原始证据。

2026-10-04，`feat/orchestrator-catalog-integration`，基点`b390d61`加保留的工作区。
本轮复核正式契约/规划器/执行入口，重新运行隔离回归，并新增独立边界探针。
**没有修改生产代码或原有测试。** 新增复核脚本、JSON证据和报告，更新共享issue。

## 结论

E29修复有效，但不能宣布工具侧全部完成。空对象/缺字段默认化、输出序列化和已有传输/格式/结构反馈边界保持通过；订单部分规则错误仍未反馈，目录身份校验、冲突统计、缺失规格和偏好语义有遗漏。VF-01、CF-06的上一轮关闭范围过大，本轮撤回为部分完成，CF-08保持开放。

| Issue | 本轮结论 | 当前关闭范围/剩余 |
| --- | --- | --- |
| CS-03 / CF-02 | 结构缺陷可保持关闭 | 空/缺决策和嵌套字段不能补成成功查询，持续无效不查库；不包括非空输出的自然语言完整性 |
| VF-01 | 部分完成，重新打开 | 三个正式入口已使用统一反馈，但订单explicit/remaining两处规则仍抛普通ValueError，未进入纠错 |
| CF-01 | 实现及隔离通过，待真实表达 | style/layout、wireless和动态品类说明对齐；尚无本版本真实模型语义成绩 |
| CF-06 | 部分修复，重新打开 | 数值/别名匹配、来源冲突保存已修；不匹配SKU的冲突仍污染聚合完整性 |
| CF-08 | 接入完成，边界未完成 | scope/绑定/版本来源已接入；内部身份校验结果被忽略，以及空对象规格缺失状态需修 |
| CF-09（新增） | 契约缺口，待修复/明确执行策略 | facets接受preference但执行与requirement相同；应明确支持软偏好或诚实拒绝，不能静默硬化 |
| OL-02 | 能力及隔离通过，仍待验 | 同谓词时间/状态/商品文本过滤存在；实际PostgreSQL、历史记录时区、真实自然语言未验收 |
| TC-02 | 保持关闭 | JSON投影异常返回execution_error/stop；直接包装器及正式图回归均通过 |
| CS-04 | 保持开放 | 完整schema无法证明模型提取了全部自然语言要求，真实模型外发仍待授权 |

## 新确认的边界

全部由合成query、人工模型输出及内存商品复现，不证明真实模型生成这些错误的概率。探针经过正式`execute_bound_tool`，保留完整ToolExecutionResult；不执行回答/结果解释，不连接配置数据库或真实模型。

1. **订单规则错误不修复（VF-01）**：query“去年有哪些订单”，人工输出selection=explicit且无可信订单号，或remaining且offset=0。两次独立探针均只调用模型一次，返回planning_failed/invalid_order_plan；第二份合法修复候选未使用，未查询订单。`validate_order_query_plan`两处抛普通ValueError；共享helper只修复ValidationError/明确OutputValidationError。因此“复用模块”不等于所有模型规则已接入。
2. **目录多身份提前返回绕过来源（CF-08，高优先）**：query“请告诉我SPU 1的键盘配列有哪些”；人工模型完整提交name=K01及target=SKU9999。搜索校验先返回MULTIPLE_TARGETS，尚未核对原文名称/编号；目录回调只处理INVALID_PLAN，丢弃该返回值。实际load(scope.sku_ids=[9999])，内存SKU9999归属SPU2的75%配列被返回为facets，无诊断且完整性为true。应先完成身份/来源校验，再显式处理所有非成功返回值；不能只拦其中一种reason。
3. **目录不明确编号被猜成SKU（CF-08）**：query“编号1有什么规格”，人工模型target=SKU1。校验返回QUERY_NEEDS_CLARIFICATION仍被忽略；实际查询SKU1并返回规格维度，未要求确认SKU/SPU。
4. **已有对象空规格被当完整空集合（CF-08）**：可信scope=[SKU1]，query“这款有什么规格”，SKU存在但specs={}。零LLM路径返回empty/not_found且is_exhaustive=true、matching_complete=true，无缺资料诊断。应区分找到对象却没有规格证据与确认没有匹配对象/选项；不能推断商品没有规格，也不要求按字段registry猜补资料。
5. **偏好执行被硬化（CF-09）**：query“键盘有哪些布局，最好白色，但其他颜色也可以”；人工条件color=白色、strength=preference、真实原文preference_evidence均通过校验。内存有白色75%和黑色全尺寸；执行对所有条件作相同硬过滤，仅返回75%且标完整，没有说明偏好被当必要条件。schema已区分强弱，执行却忽略strength；业务执行策略须明确后校验/Prompt/程序一致。可选择支持软偏好，或明确不支持这类目录查询；不预建推荐框架。
6. **已明确不匹配的SKU污染完整性（CF-06）**：query“白色键盘有哪些布局”，白色SKU1规格确定；黑色SKU2的style有冲突。SKU2已被颜色排除，程序仍在检查条件前把其issues加入聚合；返回SKU1的75%，却带SKU2冲突和data_insufficient，完整性false。只属于已确认不匹配记录的无关冲突不能影响白色集合；潜在未知匹配和已匹配记录的冲突仍需保留。

2种订单选择 + 5种目录边界，共**7个探针**。完整输出/读取范围见`tool-repair-reaudit-results.json`。探针是缺陷复现，不能把脚本正常退出说成7条业务正确通过。

## 本轮验证

重新运行E29同一11文件定向范围：**916 passed，33 deselected，8.76秒**，无失败/skip/xfail。33项为安全插件排除的配置数据库fixture；socket及AsyncEngine连接阻断。这个数量仅反映原有范围，新增7条探针揭示其覆盖不足，不纳入915/916回归通过数量。

```sh
cd backend
.venv/bin/python -m pytest -p scripts.orchestration_audit_safety \
  tests/test_tool_planning_repairs.py tests/test_catalog_search_tool.py \
  tests/test_llm_feedback.py tests/test_tools.py tests/test_tool_retry_policy.py \
  tests/test_planning_execution_policy.py tests/test_planning_call_policy.py \
  tests/test_goal_planning_validation.py tests/test_planning_handoff.py \
  tests/test_planning_contract_alignment.py tests/test_goal_task_integration.py \
  -q --tb=short --basetemp=/private/tmp/pc-tools-reaudit-20261004 \
  -o cache_dir=/private/tmp/pc-tools-reaudit-cache
.venv/bin/python -m scripts.audit_tool_repair_boundaries \
  --output evals/goal_planning/tool-repair-reaudit-results.json
```

新增脚本Ruff检查和git diff空白检查通过，E30资料链接核对；没有跑完整后端、真实模型、配置PostgreSQL、正常LLM3或浏览器。本轮没有再次请求外部模型执行，E29自动审批因未明确授权发送内部Prompt/Schema/合成query而拒绝的验证缺口仍在。

## 下一步及关闭条件

先修CF-08身份/非成功校验结果丢弃，再补VF-01显式规则反馈；补目录聚合的缺资料/排除记录范围和偏好契约。将以上独立失败表达固化为正确预期回归，确认修复前失败、修复后通过，再更新相应issue关闭范围。

CS-04/CF-01自然语言和OL-02数据库时间口径保持独立验收，结构通过不能代替；OL-01认证下游匹配集合维持阻断。RP-02及回答/完成收尾继续后续阶段。
