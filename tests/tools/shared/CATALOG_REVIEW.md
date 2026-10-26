# 三个商品工具的基础测试

> 最新复测：选定3条真实预算边界全部通过，相关离线140通过；最终Trace与交付一致，OB-001未复现。[报告](../catalog/results/summary-20261025-output-budget-retest.md)。非全量65例或聊天验收；旧失败批次保留。

> 修复前预算验收：离线394通过、6失败；选定3条真实预算边界均因输出校验追踪不一致整体失败，业务返回符合预算预期。[报告](../catalog/results/summary-20261025-output-budget.md)。当前用例65条，未全量真实复测；以下旧成绩仅适用各自版本。

> 最新：2026-10-25 新增职责验收，catalog-acceptance-4 共59例（原48＋11）。本轮58通过、1失败；[验收报告](../catalog/results/summary-20261025-acceptance.md)。下方48例为此前基线说明，不代表新增场景已通过。


当前用例版本：`catalog-review-boundary-3`，48条输入与业务目标保持不变。
本轮按用户确认拆分程序检查与工具外语义复检：程序只检查输入/输出契约、计数记账、
Trace来源及完整性，不判用户语义、商品候选集合或条件字段表达。
原业务硬断言完整移入`review_reference`，独立原始数据与预期仍在prepare阶段冻结；
复检者据此核对事实与需求，允许等价表达，不因缺少预设字段名判错。
旧批次结果不由新检查器改判；2026-10-25首次批次44通过、4失败，见
[修复前报告](../catalog/results/summary-20261025.md)。本轮修复后的结果另建批次记录。
用户已要求清除更早results，旧文档历史链接不再可访问，不复用历史通过作为本轮证据。
先读 [通用流程](../../README.md) 和 [复检标准](../../REVIEW.md)。

## 本轮范围

每个批次只运行一个工具，可进一步只选一种测试类型。

| 工具 | 冒烟 | 输入契约 | 核心业务 | 边界 |
| --- | --- | --- | --- | --- |
| [catalog_search](../catalog_search/cases.json) | 1 | 3 | 12：品牌/排除、双端价格、销量、六品类规格、可信范围 | 2：无精确匹配、候选不足2款 |
| [catalog_lookup](../catalog_lookup/cases.json) | 1 | 3 | 11：价格库存、版本统计、多品类事实、存在组合/唯一简称 | 2：组合不存在、简称歧义 |
| [catalog_facets](../catalog_facets/cases.json) | 1 | 3 | 9：品牌/品类、规格维度、条件目录、可信范围、窗口 | 0 |

原21条用例保持不变。本次只修正扩充时的4条错误成功预期，案例ID与用户问题保留。
零精确匹配、存在性为false和名称歧义都是可执行预期，不再要求不存在的正例数据。
替代允许有或无；若有，必须标为alternative，属于独立原始数据中的合法同SKU替代，
逐项解释差异、保留真实价格，不能混入精确数量/价格/存在性。简称候选不得带已确认报价。
独立SQL在prepare阶段冻结；`catalog_core_basis.py`不导入生产matcher/排序器/字段表。
仍拒绝缺失/冲突数据或不再满足本场景的数据快照，不把未知当作无匹配。

查具体商品功能属于 lookup；按条件找候选属于 search；查看范围内选项属于 facets。
全部正常业务都使用自然语言，无强制 category_hint；可信 scope 来自独立数据。
输入契约要求 `invalid_arguments` 与 `next_action=revise_arguments`，没有业务输出，handler、模型与SQL均不得启动。
输出契约通过正常业务的正式绑定校验和 Trace 检查覆盖；尚未加入非法输出故障注入。

[coverage.json](../catalog/coverage.json) 记录缺口。本轮不覆盖聊天、编排选工具、回答或前端。
旧 28 条价格/身份/筛选/数据边界模板及专用脚本已移除；其中仍有效的边界需求保留为缺口，
不表示这些需求退役或已通过。旧 `test_planning_context.py` 移除，通用规划预算/修复单元回归待补。
`results/` 中历史证据保留；旧批次需要对应 Git 版本脚本，不用新脚本改判旧结果。

## 文件职责与提前标注

- 各工具的 `cases.json`：输入、测试目的、契约硬断言、review_reference业务参考、允许等价形式、语义预期、数据前提。
- `catalog_basis.py` / `catalog_core_basis.py`：独立只读 SQL 和预期计算；不导入生产匹配、排序、规格归一化规则。
- `test_catalog_boundary_cases.py`：校验零匹配、组合不存在、歧义可准备；保留独立数据准备和Trace路径检查；原替代条件语义比较器及其断言已删除，替代事实与身份选择交由工具外复检。
- `test_catalog_core_basis.py`：用手写合成行校验预期装置的同SKU、端点、筛选后排序、全版本统计及目录窗口；通过不代表生产业务通过。
- `run_catalog_cases.py`：分别准备/执行；冻结并检查数据、代码、脚本、模型配置。
- `catalog_evidence.py`：调用正式 `build_tool_catalog` / `execute_bound_tool`，记录尝试、预算、SQL次数和 Trace。
- `catalog_trace.py` / `check_catalog_cases.py`：读取证据、程序检查、生成复检材料和报告。
- 各工具的 `test_contracts.py`：复用上述 9 条输入契约的离线执行入口；handler为禁止调用的替身。

JSON 中的 `$oracle.*` 在 **prepare 阶段** 用独立原始数据解析，先于任何工具调用冻结。
预期不来自被测工具输出；未获得数据库快照前，不声称已完成具体编号/价格的实例化标注。

原基础型号固定为“Keychron 星轴 K01 机械键盘”；独立查询必须确认其唯一性。
颜色/无线用例要求原始规格完整一致；不猜别名、不把缺失当否。
正常搜索正例要求足够候选，边界例分别要求0或不足请求数量；价格截断位置不能并列。目录要求不超过50个值。
前提不满足记未执行，不自动建数、不改现有商品。
搜索 SKU 明细可能只是预览：匹配总数查完整集合，展示编号只要求属于正确集合。
LLM 复检还须核对每张卡片的型号归属与规格证据。

## 执行入口

以下命令均从项目根执行；使用已有环境，不自动安装或启动服务。

离线输入契约，无真实模型/数据库/Trace平台：

```bash
backend/.venv/bin/python tests/run.py tests/tools/catalog_search/test_contracts.py tests/tools/catalog_lookup/test_contracts.py tests/tools/catalog_facets/test_contracts.py -q
```

也可加 `-k lookup`、`-k search` 或 `-k facets` 分别选择工具。
这只是同 9 条契约的隔离验证，不是另一套业务覆盖，也不代替真实工具验收。

真实单工具实验需要后续运行授权。将 `lookup` 换为 `search` 或 `facets`：

```bash
backend/.venv/bin/python tests/tools/shared/run_catalog_cases.py lookup --live --prepare
backend/.venv/bin/python tests/tools/shared/run_catalog_cases.py lookup --live --run <批次目录名>
```

准备时可加 `--kind smoke`、`--kind contract`、`--kind business` 或 `--kind boundary`，省略则准备该工具全部用例。
单例重测使用 `--prepare --case lookup-wireless`；不会复用旧批次。
原lookup版本 `catalog-lookup-basic-3`：按用户确认的程序校验边界，删除标题及connection_type与wireless之间的语义推断。
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
backend/.venv/bin/python tests/tools/shared/check_catalog_cases.py --fetch <批次目录名>
backend/.venv/bin/python tests/tools/shared/check_catalog_cases.py --render <批次目录名>
```

fetch 只读取本地 Phoenix 并保存原始 Trace；不覆盖现存文件。render 只处理本地证据。
两者均不调用模型/商品数据库，也不会自动将内部复核 approve 转成通过。
现存 Trace 不完整、尚有分页、截断、缺节点或来源关联不足时记无法判定；不能算通过。

程序检查：输入/输出契约、输入拒绝的零调用、范围计划→业务计划→复核的校验顺序、
模型来源关联、正式输出校验、预算记账和根/工具/批次关联。
正常规划路径至少有三个模型阶段；允许有界修复，不能硬性限定恰好三次。
预标注`trace_route=identity_pending`的名称歧义例允许范围模型+程序身份定位后提前返回，
不强制详细规划与复核；仍检查范围校验、完整模型响应、正式输出校验、事件和来源关联。
仅此用例预先声明该路径，不能根据运行后的失败结果临时豁免正常三阶段。
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
每例必须判断 `intent`、`scope`、`workflow`、`facts` 四项，并结合该例 semantic_expectation、review_reference和独立原始行。业务参考不规定模型必须采用某个语义字段或同一种等价表达。
契约用例的 workflow 判断应是校验前拒绝，不要求三个模型阶段；facts 判断是无业务输出。
复检独立于工具内部规划/自审，不能拿内部approve冒充结果复检。
本轮按用户指示由当前Codex会话逐例复检，明确记录其也参与了用例编写；不声称独立测试作者盲审或人工验收。
新会话复检可以按通用手册再开展，但不自动触发额外收费API。
脚本负责准备材料和合并记录，不自行调用评审模型。人工验收与独立 LLM 复检分开记录。

`checks/semantic.jsonl` 每例一行，格式如下（占位值必须替换）：

```json
{"batch_id":"<本批次>","case_id":"<用例>","attempt":1,"trace_id":"<本次Trace>","reviewer":"<模型及复检者标识>","reviewer_type":"independent_llm","reviewed_at":"<带时区时间>","review_standard":["review-v1","catalog-review-v3"],"summary":"<业务结论>","checks":[{"id":"intent","status":"无法判定","evidence":["<路径、用例、Span或字段>"],"reason":"<证据与理由>"}],"notes":[]}
```

示例只演示一项；实际须填齐四项。人工判读用 `reviewer_type=human`。
修改旧语义结论前先归档，并说明改判原因；render会归档已有分析，不替代编辑前归档。
报告区分通过、失败、无法判定、未执行。缺独立复检不能通过；已有明确业务失败保留失败。
每批失败和证据缺口均保留；不同批次的局部通过不能拼成当前版本的全量通过。
