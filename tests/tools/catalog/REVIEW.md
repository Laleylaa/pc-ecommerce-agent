# catalog_search 结果复检手册

版本：`catalog-review-v1` · 2026-10-22。

先读 [通用复检手册](../../REVIEW.md)。本页补充商品工具业务判断和已有脚本使用方式，
不替代每批运行前标注，也不声称所有下列场景已经有用例或通过。
现有3条基础价格查询和4组共25条用例，均已改为名称输入版本；使用入口见第7节。
2026-10-23已完成名称用例的工作区Prompt前后对照，结果在忽略的results中，各批次独立保存。
用例模板中的design_status/revision_note保留设计时记录，不是当前执行状态；执行事实以批次证据为准。

**当前不是已验收基线。** 操作分类、名称与条件的等价表示、附带price字段及db-01的品类能力前提
存在待修订问题，见[AL-020～AL-023](../../../docs/development/issues.md#al-020商品工具操作分类不稳定精确条件询问曾返回替代结果)。
这些issue属于另行维护的工作区文档；若当前checkout尚无对应条目，以上限制仍适用。
本次整理不修改用例断言或改判历史结果。模板适配当前工具v4工作区，相关生产改动尚未随测试提交，
不能假设仅checkout测试提交就能复现相同工具行为。缺少结果时不得用历史通过数代替证据。

目录职责及程序、被测模型、独立复检LLM和人工的分工见 [tests/README.md](../../README.md)。
本页“人工记录”表示检查器之外独立填写的语义记录；由LLM填写时明确标识LLM身份，
不等于用户已人工验收。已有报告的字段标题不改变实际判读者身份。

## 1. 范围与源码定位

仅复检 `catalog_search` 单工具。编排、聊天API、回答、前端和其他工具不纳入本页验收。
执行链应是正式绑定、输入校验、规划/复核或允许的确定性路径、查询/组装及输出校验。
直接调用repository/engine的输出不能冒充正式工具验收。

阅读实现时使用执行批次的版本；以下是当前定位入口，不是旧批次实现的保证：

- [正式绑定与输入输出校验](../../../backend/app/tools/contracts.py)
- [请求、计划及结果契约](../../../backend/app/schemas/catalog_search.py)
- [规划Prompt](../../../backend/app/tools/prompts/catalog_search.py)
- [服务入口](../../../backend/app/tools/catalog_search/service.py)、[规划](../../../backend/app/tools/catalog_search/planner.py)、[执行引擎](../../../backend/app/tools/catalog_search/engine.py)
- [结果组装](../../../backend/app/tools/catalog_search/presentation.py)、[数据读取](../../../backend/app/repositories/catalog_search.py)
- [程序校验](../../../backend/app/tools/catalog_search/validation.py)、[状态规则](../../../backend/app/tools/catalog_search/outcomes.py)、[数量边界](../../../backend/app/tools/catalog_search/limits.py)
- [Trace](../../../backend/app/core/loop_tracing.py)、[预算上下文](../../../backend/app/core/loop_budget.py)

本页编写时结果契约为v4，候选/版本展示上限20、型号版本预览上限1、身份建议上限5。
这些是版本相关参数；复检其他版本时核对其冻结配置和标注，不能直接套用。

## 2. 专项业务判读表

按用例标注选择适用行。没有覆盖的行写“未覆盖”，不能计作通过。

| 维度 | 判断标准与必须识别的问题 | 主要证据 |
| --- | --- | --- |
| 操作类型 | 找候选为search；明确对象的事实询问为lookup；明确列版本/选版本为variants。不能为查型号价格先要求列完SKU或选择版本 | 原请求、operation、requested_fields、结果类型 |
| 型号与版本 | 先确定身份再筛条件；一个型号只有一个SKU、或筛后剩一个SKU，不足以把SPU改成SKU。versions按SKU展示，不等于用户指定了一个SKU | 原始身份依据、target、对象定位事件、selection_scope、facts.target或卡片entity_scope |
| 价格汇总 | 仅price的lookup返回facts；价格、数量覆盖已确认适用的在售SKU集合，不受展示limit限制。明确SKU只报该SKU。不能将型号背景价代替条件内价格 | 独立数据集合/价格、applied_conditions、聚合事件、完整性字段 |
| 名称与歧义 | 保留品牌、型号、版本；允许哪些换序/别名以标注为准。相似或多SPU待确认身份不能合并报价，不猜改型号数字或SKU编号 | search_text、target、object_results、identity_suggestions、实际事实对象 |
| 名称/编号/scope | 共同出现时满足交集；冲突须保留冲突，不只用其中一项查询成功就算通过。scope编号须有可信来源，不由模型创造 | 实际输入、独立编号依据、计划、范围和定位诊断 |
| 同SKU条件 | 多条硬条件必须在同一SKU上成立。单个SKU缺一项就不能拼其他版本补足；AND/OR、否定、单位和数值端点按预标注判断 | conditions、逐SKU condition_checks、独立规格来源 |
| 硬条件与偏好 | 硬条件决定准入；明确软偏好只排序，不剔除满足硬条件的非偏好商品。不能为了有结果将硬条件降为偏好 | strength、完整候选依据、排序事件和结果顺序 |
| 事实与筛选 | “这个型号支持X吗”不能变成仅筛支持X的版本；未知不能当不支持。用户所问字段须实际回应或明确缺失，不能仅在计划保留却在结果遗漏 | requested_fields、conditions、事实、field_issues与诊断 |
| 混合事实 | “价格和规格”要保留两者；不能因price-only分支简洁而丢规格。按该版本实际查询路径验证全部要求 | 请求字段、完整返回、输出校验前值 |
| 数量/排序/预览 | 先筛选和排序再截断；候选数、SKU数、展示数分开。有界预览不能当全部版本；统计完整性与明细完整性分开。超量请求按规则截断并说明 | requested_count、limit、returned_count、series计数/价格、variant_details_complete、窗口事件 |
| 上下架与库存 | 下架与零库存分开；无现货要求不能隐式加库存条件。参数没查到不是商品不支持 | 独立status/stock、条件、对象状态、事实诊断 |
| 受控替代 | 仅普通搜索在确认硬条件无匹配且符合规则时可替代，保留品类/身份/scope并说明差异。lookup/variants无匹配不得放宽条件或扩大报价范围 | fallback_attempted、match_kind、条件差异、unknown计数 |
| 不确定与失败 | 无匹配、资料缺失/冲突、需澄清、不支持、执行失败分别判断。unknown不能记作mismatch；部分可确认集合不能声称统计完整 | ok/error、query_state、status、reason_code、matching_complete、facts.complete、诊断 |
| 必要程序边界 | 结构、字段/操作符/单位、编号来源、确定性冲突与预算有实际校验依据。复核approve不证明语义，requirement_assessment仅描述计划条目执行能力 | 计划与复核输出校验、拒绝/修正事件、工具最终输出校验 |

范围集合有独立数据依据但Trace只记录计数时，应准确说明证据来源，不能声称Trace列出了完整SKU集合。
缺少必要独立事实时保留缺口，不从被测输出“验证”其自身价格或数量。
未查询规格的价格facts不能证明所有规格完整；型号卡片的代表版本事实不能当作全型号事实。

## 3. Trace读取顺序与关联

1. 按批次、用例、尝试号及Trace ID定位根记录和工具节点，核对实际输入与冻结输入。
2. 阅读所有实际规划输出、复核输入/输出和修正，不只读最后一次approve。
3. 查看计划校验、复核输出校验、工具输出校验；核对来源模型Span和父子关系。
4. 查看上下文范围、执行计划、对象定位、条件筛选、统计/返回窗口及结果事件。
5. 比较“工具输出校验”的校验前输入、最终正式返回和本地调用记录，检查截断与采集失败标记。
6. 核对模型HTTP请求计数、预算上下文、重试和异常。缺少tool_scope导致HTTP前拒绝是入口/实验问题，
   不能归为商品无匹配；预算不应为了获得结果而放宽。

现有封闭编号语法可以合法跳过模型。若用例要求真实自然语言规划，必须看到实际规划/复核请求；
若用例专测确定性路径，模型调用为零不自动构成失败。不得统一要求所有用例恰好两个模型节点。
根记录、工具成功、Phoenix接收成功和业务通过分别判定。

## 4. 已有批次文件与命令

批次位于 `tests/tools/catalog/results/<批次目录名>/`，产物已被Git忽略。

| 文件 | 读取内容 |
| --- | --- |
| `evidence/cases.json` | `common_hard`与每例`hard`、`common_semantic`、每例目标/允许形式/condition_policy/population、trace_requirements |
| `evidence/baseline.json` | prepared与before_execution的SQL、原始数据、核对时间和只读事务信息；没有快照一致证据时不能假设一致 |
| `evidence/environment.json` | 模型、代码/脚本指纹、执行准备、导出、Trace读取记录；部分verification字段由收尾检查另行填写，不假设总是存在 |
| `evidence/executions.jsonl` | 每条实际输入、工具结果/错误、尝试、Trace ID、模型用量与预算记录 |
| `evidence/traces/<用例ID>.json` | Phoenix原始`data`节点；`context.span_id`、`parent_id`、`attributes`和`events`用于定位证据 |
| `checks/automatic.jsonl` | hard_checks与trace_checks逐项预期、实际、结论 |
| `checks/semantic.jsonl` | 人工填写的语义逐项判读、摘要和观察 |
| `report.md` | 给用户看的单份报告 |

旧布局或文件缺失时先说明，不能在普通复检中静默迁移目录。

### 仅使用已有本地证据

从仓库根运行以下命令，将`<批次目录名>`替换成实际名称，不带上级路径：

```bash
PYTHONDONTWRITEBYTECODE=1 backend/.venv/bin/python tests/tools/catalog/check_price_lookup.py --render <批次目录名>
```

该入口重新计算程序检查、合并已有人工记录，写`checks/automatic.jsonl`和`report.md`；
不调用模型、数据库或Phoenix。它**不会替执行者做语义复检**。
首次可先生成待判读结果，再填写语义记录后重生成；已有分析则按通用手册先归档。

### 缺少本地Trace，且任务允许补取原平台记录

```bash
PYTHONDONTWRITEBYTECODE=1 backend/.venv/bin/python tests/tools/catalog/check_price_lookup.py --fetch <批次目录名>
```

只访问本地Phoenix原Trace，不调用工具/模型；保存原始Trace和取回记录后也会重新生成检查/报告。
它跳过已经存在的Trace文件，不能用于宣称已补齐一个现存但不完整的文件。
需要补取不同版本时应保留旧文件和来源记录，不擅自删除旧证据。

`run_price_lookup.py --live --prepare/--run` 会访问真实数据库，后者还调用真实模型；
它们是实验入口，**不属于复检命令**。不通过`tests/run.py`执行真实实验，不关闭其离线保护。

## 5. 人工记录如何填写

`checks/semantic.jsonl` 每个用例一行JSON对象；每个对象包含如下字段：

```json
{
  "case_id": "<本批用例ID>",
  "trace_id": "<本次实际Trace ID>",
  "reviewer": "<判读者>",
  "reviewed_at": "<带时区的实际复检时间>",
  "review_standard": ["review-v1", "catalog-review-v1"],
  "summary": "一句业务判断，不重复全部过程",
  "notes": [],
  "checks": [
    {
      "id": "<运行前语义标注ID>",
      "status": "无法判定",
      "evidence": ["evidence/traces/<用例ID>.json#span=<实际Span ID>"],
      "reason": "指出具体已核对内容或缺少的必要证据"
    }
  ]
}
```

这是格式示意，不可原样当作完成判读。现有价格批次必须逐项填写
`intent`、`identity`、`constraints`、`population`、`review`五个ID，
并结合每例的`identity_acceptance`等标注。其他批次读取其自身ID，不强套这五项。
所有结论独立填写；禁止程序自动把自动通过或模型approve映射为人工通过。
引用路径以批次根目录为基准，JSONL引用同时写用例/尝试，Trace引用写Span ID及必要字段/事件。

`summary`用于简洁结果表；`notes`用于报告中的重要观察。通过用例的详细解释留在`checks`，
失败或无法判定必须说明触发输入、预期与实际差异、来源证据及影响。
记录中的Trace ID必须属于本次执行，不能沿用上一批的语义判读。

已观察到的一个边界：价格用例中，模型可能把调用参数的显式limit复制到requested_limit，
但未限制价格统计。是否违反数量来源要求应按运行前标注判断，不能因报价正确就宣称来源规则通过，
也不能临时添加新的硬断言改判原批次。保留观察和未覆盖范围即可。

## 6. 当前检查器的限制与交付核对

本节限制针对 `check_price_lookup.py`，它是基础价格查询的小批入口：

- 报告中的价格字段和部分限制说明按这3条用例编写，不能直接套用于search/variants。
- 它会按case_id读取一条执行/人工记录；当前运行器禁止同批重跑。若已有材料出现同用例多次尝试，
  先逐次审查，不允许让字典覆盖掩盖尝试；注明当前生成器不适用。
- 它不会自动保存复检历史，也不会检查所有人工证据引用的真实性；这些由执行者完成。
- 它不会自动将数据变动、执行记录丢失、实验入口异常等所有情形分类正确；缺输出时的字段失败
  不能直接解释为工具业务缺陷。先查原始执行/错误，再依通用状态规则判定。
- 程序生成的报告若与证据或通用判定规则冲突，保留程序原结论，在人工记录和报告明确写出差异、
  最终状态及理由；不要为了修正报告而悄悄改脚本、预期或自动记录。人工调整后再次render会覆盖报告，
  因此最后必须核对成品，不以命令返回零作为交付完成。

交付前核对：每例输入和预期未改、实际Trace对应、全部必要语义项有证据、四类状态不漏计、
报告和分析一致、链接有效、旧结论与改判理由保留。提供`report.md`链接即可，
不要在回复里再次粘贴所有底层检查。只复检旧结果时明确说“完成复检”，不声称本轮重新运行了测试。

## 7. 分组用例与显式执行入口

原3条用例及其脚本保留。新增用例逐条包含自然语言输入、硬比较、条件强度/操作符、
允许等价形式、5项具体语义标准及证据要求。优先场景对应关系、未覆盖项与隔离数据需求见
[coverage.json](coverage.json)。覆盖索引及模板描述场景，不是验收证据；执行状态与未解决问题见本页开头。

| 类型 | 用例文件 | 独立运行入口 | 数量 |
| --- | --- | --- | --- |
| 价格、版本与对象层级 | [price_boundaries_cases.json](price_boundaries_cases.json) | [run_price_boundaries.py](run_price_boundaries.py) | 7 |
| 名称、编号与可信范围 | [identity_scope_cases.json](identity_scope_cases.json) | [run_identity_scope.py](run_identity_scope.py) | 6 |
| 条件、偏好、排序与预览 | [selection_ranking_cases.json](selection_ranking_cases.json) | [run_selection_ranking.py](run_selection_ranking.py) | 6 |
| 数据质量、库存与失败边界 | [data_boundaries_cases.json](data_boundaries_cases.json) | [run_data_boundaries.py](run_data_boundaries.py) | 6 |

同类在一个独立批次运行；四个入口复用 [run_catalog_cases.py](run_catalog_cases.py)，
正式调用继续使用原试跑脚本的 `run_cases`，保留绑定、参数/输出校验、真实规划/复核、
追踪及预算上下文。没有复制工具业务实现；SQL只用于独立预期，不代替被测工具调用。
这些入口均不命名为 `test_*.py`，不接入pytest、Makefile或默认离线测试；不更改隔离保护。

### 运行前独立依据与数据缺口

固定编号/价格来自既有试跑资料和数据导入源码的设计假设，不声称它们仍是当前事实。
每条 `oracle_sql` 预先定义只读原始行和 `eligible` 数据前提；`--prepare` 将来执行时核对。
固定预期不随查询值变化；`$oracle.*` 只用于预先声明的独立SQL聚合或原始规格，
在正式调用之前解析并冻结。`condition_expectation` 不通过被测matcher计算。

`pb-04/pb-05/db-01/db-02/db-03/db-04` 需要边界对象，暂不猜编号。
未来独立核对到现成数据后，通过绑定JSON传入每例所需的 `spu_id` 或 `sku_id`，
同时填写 `source`（核对来源/时间，无凭据），例如结构为：

```text
{"pb-04": {"spu_id": <独立确认的整数编号>, "source": "<只读核对来源与时间>"}}
```

这是格式示意，不是可直接运行的JSON。每组绑定只包含该组用例；未给绑定、数据前提不符、
SQL核对失败都保留为未执行。不得自动seed、改库存、改上下架状态或导入数据。
如果没有现成对象，按 `data_requirement` 提出的最小隔离数据方案另行确认；脚本不提供建数入口。

以下是未来获得运行授权后的命令示意，**本轮没有执行**。将文件名换成相应类型：

```bash
PYTHONDONTWRITEBYTECODE=1 backend/.venv/bin/python tests/tools/catalog/run_price_boundaries.py --live --prepare
PYTHONDONTWRITEBYTECODE=1 backend/.venv/bin/python tests/tools/catalog/run_price_boundaries.py --live --run <批次目录名>
```

准备有边界对象的批次时可加 `--bindings <绑定JSON路径>`。
准备与执行都使用只读、可重复读事务；准备不调用模型。
执行前重新读取同一独立SQL，任何相关原始数据变化都阻止该例调用，记录数据变化/无法判定，
不更新原预期。源码、脚本、用例快照或模型配置变化则拒绝继续该批次。
同批不重跑，异常后也不得复用；需要重试时保留旧批次，另建新批次并解释重试原因。

新增批次沿用 `report.md / evidence / checks`，并增加：

- `evidence/case_templates.json`：未解析的设计模板。
- `evidence/cases.json`：运行前冻结的输入、两类预期和数据就绪状态。
- `evidence/baseline.json`：准备阶段的原始行、SQL、绑定依据及核对时间。
- `evidence/preflight.json`：调用前二次只读核对和未执行原因，不覆盖原始baseline。

### 新批次的程序检查、语义复检与报告

新4组使用 [check_catalog_cases.py](check_catalog_cases.py)，不使用原3条的检查器：

```bash
PYTHONDONTWRITEBYTECODE=1 backend/.venv/bin/python tests/tools/catalog/check_catalog_cases.py --render <批次目录名>
PYTHONDONTWRITEBYTECODE=1 backend/.venv/bin/python tests/tools/catalog/check_catalog_cases.py --fetch <批次目录名>
```

`--render` 只读本地证据并写分析/报告；`--fetch` 另外读取本地Phoenix原Trace，须在任务授权内。
不会触发模型或数据库。若已有Trace文件，fetch不覆盖；存在下一页标记、缺失或截断时
保留证据不足，不能将采集到的第一页当完整记录。

程序明细包含逐项硬比较、Trace检查和从Trace抽取的规划结果、校验结论、事件及Span关联；
完整Prompt及原始节点仍在 `evidence/traces`，不铺进报告。
读取每例 `semantic_checks` 并结合 `common_semantic` 与 `semantic_expectation` 独立判读。
人工记录沿用第5节格式，**新批次必须另有 `batch_id` 和 `attempt`**，与execution一致；
重复判读注明改判原因。不能沿用不同Trace或其他批次的语义结论。

本轮没有实际调用，因此没有填任何语义“通过”或生成新实验report。
未来复检者填写 `checks/semantic.jsonl` 后再render合并；缺必要判读只能无法判定。
新检查器在覆盖前自动归档现有report/automatic/semantic及哈希；人工编辑semantic之前，
仍须先按通用手册归档旧版，避免把编辑后的文件误当编辑前记录。

`model` 路径要求真实规划/复核及正常查询事件；`model_stopped` 要求规划/复核与提前结束，
不要求本不应发生的查询事件；`input_rejected` 要求模型请求为零并保留正式参数拒绝。
三个路径均核对根/工具/批次/用例/Trace关联和预算记录，不把模型approve直接算通过。
工具/实验异常、数据核对失败、追踪缺失分别记录；已有明确业务失败优先保留。

新脚本尚未经过真实运行验证；静态检查无法证明SQL在当前数据库可执行、Phoenix读回完整，
或所有报告分支均正常。未来首跑发现脚本问题时也保留批次，不修改原预期后隐瞒重跑。
