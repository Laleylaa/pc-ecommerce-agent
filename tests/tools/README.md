# 工具测试

按公开工具名称找入口；跨工具的检查与评测装置放在 shared。当前商品修复及验证见下方最新进度，历史记录仅适用于各自日期的版本。
通用运行与复检原则见 [测试说明](../README.md) 和 [复检手册](../REVIEW.md)。

| 目录 | 对象与内容 |
| --- | --- |
| `catalog_search/` | 商品候选搜索：提前标注 `cases.json`、离线输入契约 |
| `catalog_lookup/` | 指定商品事实与版本：提前标注 `cases.json`、离线输入契约 |
| `catalog_facets/` | 目录选项与计数：提前标注 `cases.json`、离线输入契约 |
| `order_lookup/` | 订单身份、查询与建议、Prompt 调用检查 |
| `policy_search/` | 店铺政策检索、类型过滤、超时与后台工作槽生命周期 |
| `knowledge_search/` | 正式网页搜索绑定、HTTP 替身、结果与错误边界 |
| `shared/` | 跨工具的注册、执行、Prompt、商品规则和评测装置；不逐工具重复 |
| `shared/local_retrieval/` | 保留的本地知识库、混合检索与 Chroma 服务检查，不代表正式 knowledge_search 网页工具 |
| `catalog/` | 商品共用覆盖索引、评测说明跳转和历史 results；不再承载测试实现 |

测试目的只分冒烟、契约、基础核心业务、边界/异常。缺陷回归放入对应类别，不新增平级目录。
沿用现有测试名和 marker，本次不批量重新判定测试分类；目录名称不自动赋予 marker。
提前标注不要求全部 JSON 化：商品业务实验保留 JSON；确定性订单/政策/网页检查保留 pytest 内的输入与断言。

## 离线入口

均通过 `tests/run.py`，阻断真实模型、数据库与凭据读取。以下是使用方式，不是本轮执行成绩。

```bash
# 单个工具（示例）
backend/.venv/bin/python tests/run.py tests/tools/catalog_lookup -q
backend/.venv/bin/python tests/run.py tests/tools/knowledge_search -q

# 注册与调用结构，不执行领域业务
backend/.venv/bin/python tests/run.py tests/tools/shared/test_tool_contracts.py tests/tools/shared/test_strands_registry.py tests/tools/shared/test_tool_concurrency.py -q

# 只收集 tools 中的全部 pytest 项，不执行测试函数
backend/.venv/bin/python tests/run.py tests/tools --collect-only -q
```

不要将整个 shared 当作纯契约清单运行：其中保留的 goal-tool fixture 测试会执行合成领域业务，
部分本地检索测试验证服务内部行为。注册通过、离线业务通过、真实模型通过分别记录。
受商品契约变化影响的 loop/support 替身和旧模拟复核格式已按当前接口适配；本地检索和 legacy 全链路不属于本轮回归范围。

## 商品真实评测

三个工具共用一份装置，输入与提前标注分别归各工具；[专项手册](shared/CATALOG_REVIEW.md)。
只有显式 live 执行触及真实模型/只读数据库，目录整理不授权运行。

```bash
backend/.venv/bin/python tests/tools/shared/run_catalog_cases.py lookup --live --prepare
backend/.venv/bin/python tests/tools/shared/run_catalog_cases.py lookup --live --run <批次目录名>
backend/.venv/bin/python tests/tools/shared/check_catalog_cases.py --render <批次目录名>
```

search/facets 使用相同入口的对应参数。新旧结果仍保存在 `tests/tools/catalog/results/`，
保持原忽略规则与历史证据路径。迁移会改变源码/脚本指纹，新运行须重新 prepare；不得覆盖旧冻结批次。
旧批次需要对应版本装置，目录迁移不表示可以用当前脚本重新判读任何历史批次。

## 原路径迁移

- `orders/` → `order_lookup/`。
- `knowledge/test_web_search.py` → `knowledge_search/`；政策专属用例 → `policy_search/`。
- 本地知识库/Chroma及混合检索内部检查 → `shared/local_retrieval/`。
- `contracts/`、`execution/`、`smoke/`、`prompts/` 中跨工具检查 → `shared/`。
- `catalog/*_cases.json` → `catalog_<工具>/cases.json`；九条输入契约分到三个工具入口，断言仍复用。
- 商品共用规则检查、数据依据、执行/检查脚本 → `shared/`；历史结果不移动。

旧的文件级命令需按本表换路径；目录迁移与后续业务修复的验证范围分别记录。

## 最新修复与验证（2026-10-25）

商品相关离线检查 237 通过；现有真实用例 48/48 通过：search 18、lookup 17、facets 13。
程序检查契约与 Trace，业务语义参考保留在 review_reference，由工具外逐例复检；
本轮复检由参与实现的当前 Codex 会话完成，不是独立作者盲审。
详见[最新报告](catalog/results/summary-20261025-fixes.md)，不代表聊天端到端或新版首次工具选择已通过。
本次未新增测试用例，未重跑下方历史批次；下方记录仅说明各阶段当时的结果。

## 目录整理核验（2026-10-24，核心业务扩充前）

- 迁移前使用 HEAD 的独立临时源码快照收集，迁移后使用当前源码收集，均通过 `tests/run.py --collect-only`：164 项，测试名与参数化数量一致。首次收集与文件移动重叠的记录无效，未用作基线。
- 原 90 个测试函数名全部保留；商品输入契约改为三个工具分别收集，共用原断言。其余测试函数 AST 只改变评测装置路径引用。
- 三份商品提前标注共 21 条，迁移前后逐字节一致；覆盖索引只更新文件路径。
- 通过 `tests/run.py` 定向执行：shared 下注册契约、SDK 冒烟、会话清理，三个商品工具的输入契约，商品导入边界，以及 Prompt 资源指纹检查：48 passed。
- Ruff（使用 `backend/pyproject.toml` 配置）、Python 语法、文档链接、覆盖索引路径和两个评测脚本 `--help` 检查通过。
- 未运行完整工具业务、真实模型、真实数据库、Phoenix 或聊天端到端；未修改 tools 外文件；未提交或推送。

## 商品核心业务扩充（2026-10-24）

原21条保留，新增27条核心业务，现共48条；业务用例分别为search 14、lookup 13、facets 9。
输入、独立预期和数据前提在各工具 `cases.json`，覆盖矩阵见 [coverage.json](catalog/coverage.json)。
18项定向离线检查通过；真实业务32条已执行、4条前提不足，程序检查发现2条lookup失败。
按用户指示由当前Codex会话逐例复检，最终30通过、2失败、4未执行；完整结果与未覆盖项见 [本轮报告](catalog/results/summary-20261024-core-business.md)。
此结论不代表聊天、首次选工具或端到端验收。

## 原3—6用例修正（2026-10-24）

按真实只读数据库将四条成功前提错误的用例改为边界：无精确匹配、候选不足、组合不存在、名称歧义。
当前48条分为冒烟3、契约9、核心业务32、边界4；独立数据和预期已新建批次冻结，4/4可准备。
本次28项定向离线检查通过，未执行新业务调用；原失败和未执行成绩不改写。
见 [修正报告](catalog/results/summary-20261024-boundary-correction.md)。
