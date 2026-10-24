# 三个商品工具的基础测试

版本：`catalog-review-v2` / 用例 `catalog-basic-1`。
状态：2026-10-24，删除标题语义冲突规则后，原失败的4条facets用例重测全部通过，程序、Trace和独立LLM复检均通过。
最新汇总见[本轮报告](results/summary-20261024-basic.md)。历史首轮为16通过、4失败、1无法判定；lookup无线另批重测通过。
本轮不是21例全量回归；当前各例最近记录均通过，但来自不同代码/数据批次，不代表当前版本完整验收。
先读 [通用流程](../../README.md) 和 [复检标准](../../REVIEW.md)。

## 本轮范围

每个工具 7 条，共 21 条。每个批次只运行一个工具，可进一步只选一种测试类型。

| 工具 | 冒烟 1 条 | 输入契约 3 条 | 基础业务 3 条 |
| --- | --- | --- | --- |
| [catalog_lookup](lookup_cases.json) | 名称定位型号、汇总价格 | 空 scope、limit=0、注入 user_id | 明确版本价格库存、型号无线支持、按价格列两个版本 |
| [catalog_search](search_cases.json) | 预算内键盘候选 | 同上，分别验证正式绑定 | 可信型号范围内选最便宜、白色+预算、无线+预算 |
| [catalog_facets](facets_cases.json) | 无品类提示的全店品类目录 | 同上，分别验证正式绑定 | 全店品牌、范围内颜色选项、预算内全店品牌 |

查具体商品功能属于 lookup；按条件找候选属于 search；查看范围内选项属于 facets。
全部正常业务都使用自然语言，无强制 category_hint；可信 scope 来自独立数据。
输入契约要求 `invalid_input`，没有业务输出，handler、模型与SQL均不得启动。
输出契约通过正常业务的正式绑定校验和 Trace 检查覆盖；尚未加入非法输出故障注入。

[coverage.json](coverage.json) 记录缺口。本轮不覆盖聊天、编排选工具、回答或前端。
旧 28 条价格/身份/筛选/数据边界模板及专用脚本已移除；其中仍有效的边界需求保留为缺口，
不表示这些需求退役或已通过。旧 `test_planning_context.py` 移除，通用规划预算/修复单元回归待补。
`results/` 中历史证据保留；旧批次需要对应 Git 版本脚本，不用新脚本改判旧结果。

## 文件职责与提前标注

- `*_cases.json`：输入、测试目的、硬断言、允许等价形式、语义预期、数据前提。
- `catalog_basis.py`：独立只读 SQL 和预期计算；不导入生产匹配、排序、规格归一化规则。
- `run_catalog_cases.py`：分别准备/执行；冻结并检查数据、代码、脚本、模型配置。
- `catalog_evidence.py`：调用正式 `build_tool_catalog` / `execute_bound_tool`，记录尝试、预算、SQL次数和 Trace。
- `catalog_trace.py` / `check_catalog_cases.py`：读取证据、程序检查、生成复检材料和报告。
- `test_catalog_contracts.py`：复用上述 9 条输入契约的离线执行入口；handler为禁止调用的替身。

JSON 中的 `$oracle.*` 在 **prepare 阶段** 用独立原始数据解析，先于任何工具调用冻结。
预期不来自被测工具输出；未获得数据库快照前，不声称已完成具体编号/价格的实例化标注。

首批型号固定为“Keychron 星轴 K01 机械键盘”；独立查询必须确认其唯一性。
颜色/无线用例要求原始规格完整一致；不猜别名、不把缺失当否。
搜索用例至少有足够候选，价格截断位置不能并列；目录要求不超过 50 个值。
前提不满足记未执行，不自动建数、不改现有商品。
搜索 SKU 明细可能只是预览：匹配总数查完整集合，展示编号只要求属于正确集合。
LLM 复检还须核对每张卡片的型号归属与规格证据。

## 执行入口

以下命令均从项目根执行；使用已有环境，不自动安装或启动服务。

离线输入契约，无真实模型/数据库/Trace平台：

```bash
backend/.venv/bin/python tests/run.py tests/tools/catalog/test_catalog_contracts.py -q
```

也可加 `-k lookup`、`-k search` 或 `-k facets` 分别选择工具。
这只是同 9 条契约的隔离验证，不是另一套业务覆盖，也不代替真实工具验收。

真实单工具实验需要后续运行授权。将 `lookup` 换为 `search` 或 `facets`：

```bash
backend/.venv/bin/python tests/tools/catalog/run_catalog_cases.py lookup --live --prepare
backend/.venv/bin/python tests/tools/catalog/run_catalog_cases.py lookup --live --run <批次目录名>
```

准备时可加 `--kind smoke`、`--kind contract` 或 `--kind business`，省略则准备该工具七条。
单例重测使用 `--prepare --case lookup-wireless`；不会复用旧批次。
lookup现行版本 `catalog-lookup-basic-3`：按用户确认的程序校验边界，删除标题及connection_type与wireless之间的语义推断。
前检只比较同一结构化字段的独立来源；v2属于历史重测版本，不再作为现行规则。
prepare 只读数据库，不调用模型；run 调用正式绑定，正常业务会调用真实模型。
live 入口需要已有模型配置，包括契约批次；契约的实际工具调用仍应为零 HTTP / 零 SQL。
默认 pytest / make tools-test 不运行 live 脚本，不允许绕过 `tests/run.py` 的离线保护。
数据库连接设置只读与查询超时，准备/前检使用可重复读事务；工具可能使用独立只读会话，
因此另做执行后快照核对。前后相同不能排除中间短暂写入，验收宜使用稳定的隔离数据集。

源码、脚本、模型或冻结材料变化时拒绝继续。数据变化时保留原预期并标记无法判定。
每例每批调用一次；工具内部有界修复/重试全部留在 Trace 中，不另挑最好的一次。
同批不允许重跑；异常后也另建批次，保留前次记录并说明原因。

## 程序检查、独立 LLM 复检、报告

```bash
backend/.venv/bin/python tests/tools/catalog/check_catalog_cases.py --fetch <批次目录名>
backend/.venv/bin/python tests/tools/catalog/check_catalog_cases.py --render <批次目录名>
```

fetch 只读取本地 Phoenix 并保存原始 Trace；不覆盖现存文件。render 只处理本地证据。
两者均不调用模型/商品数据库，也不会自动将内部复核 approve 转成通过。
现存 Trace 不完整、尚有分页、截断、缺节点或来源关联不足时记无法判定；不能算通过。

程序检查：冻结预期与结果、输入拒绝的零调用、范围计划→业务计划→复核的校验顺序、
模型来源关联、正式输出校验、预算记账和根/工具/批次关联。
正常业务至少有三个模型阶段；允许有界修复，不能硬性限定恰好三次。
程序检查不代替语义审查，也不证明所有安全/业务边界均覆盖。

输出布局：

```text
results/<批次>/
  evidence/case_templates.json  原始设计
  evidence/cases.json           已实例化冻结输入/预期
  evidence/baseline.json        独立SQL和原始行
  evidence/preflight.json       执行前复核
  evidence/postflight.json      执行后复核
  evidence/environment.json    配置、源码/脚本指纹、阶段状态
  evidence/executions.jsonl     每次实际调用、结果/错误、用量
  evidence/traces/<用例>.json   原始Trace
  checks/automatic.jsonl        程序逐项结论
  checks/review_packets.json    独立复检材料，不含自动语义判决
  checks/semantic.jsonl         独立LLM或人工填写
  checks/history/              前次复检归档
  report.md                    逐例摘要及四类状态
```

独立复检者先读冻结预期/原始数据，再读实际结果和全部阶段 Trace。
每例必须判断 `intent`、`scope`、`workflow`、`facts` 四项，并结合该例 semantic_expectation。
契约用例的 workflow 判断应是校验前拒绝，不要求三个模型阶段；facts 判断是无业务输出。
在独立 LLM 会话/任务中复检，不能拿工具内部的自审冒充结果复检。
脚本负责准备材料和合并记录，不自行调用评审模型。人工验收与独立 LLM 复检分开记录。

`checks/semantic.jsonl` 每例一行，格式如下（占位值必须替换）：

```json
{"batch_id":"<本批次>","case_id":"<用例>","attempt":1,"trace_id":"<本次Trace>","reviewer":"<模型及复检者标识>","reviewer_type":"independent_llm","reviewed_at":"<带时区时间>","review_standard":["review-v1","catalog-review-v2"],"summary":"<业务结论>","checks":[{"id":"intent","status":"无法判定","evidence":["<路径、用例、Span或字段>"],"reason":"<证据与理由>"}],"notes":[]}
```

示例只演示一项；实际须填齐四项。人工判读用 `reviewer_type=human`。
修改旧语义结论前先归档，并说明改判原因；render会归档已有分析，不替代编辑前归档。
报告区分通过、失败、无法判定、未执行。缺独立复检不能通过；已有明确业务失败保留失败。
首轮失败和证据缺口保留在历史批次；facets四例最新重测通过且未复现截断，完整范围和剩余限制见最新汇总。
