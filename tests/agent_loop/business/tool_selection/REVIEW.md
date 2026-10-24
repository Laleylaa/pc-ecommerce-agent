# 主流程首次工具选择测试

准则版本：`tool-selection-review-v3`，2026-10-24。
当前39条预标注、4个分类入口；2026-10-24真实模型各3次，程序＋独立LLM语义复检117通过，0失败/无法判定/未执行。
本地[本轮汇总](results/summary-20261024-first-selection.md)链接各分类报告；不沿用旧成绩。

**唯一目标：根据用户输入和当前工具职责，主流程能否选对工具。**
不执行工具、不返回任何工具结果、不观察反馈后模型如何处理。
v1/v2曾错误扩大为领域替身反馈后的循环测试，其报告不是本目标的验收证据。
用户于2026-10-24明确要求删除错误测试结果：旧v1/v2批次及汇总已删除，不作为v3成绩。

通用流程引用 [tests/README.md](../../../README.md) 和 [tests/REVIEW.md](../../../REVIEW.md)。
工具职责来自[正式注册契约](../../../../backend/app/tools/contracts.py)，不在测试中复制description。

## 1. 分类与预标注

| 类型 | 用例 | 批量入口 | 数量 | 只检查的业务选择 |
| --- | --- | --- | ---: | --- |
| 单一目的 | [single_intent_cases.json](single_intent_cases.json) | [run_single_intent.py](run_single_intent.py) | 12 | 候选搜索、指定对象事实/版本、目录枚举 |
| 易混淆职责 | [intent_boundaries_cases.json](intent_boundaries_cases.json) | [run_intent_boundaries.py](run_intent_boundaries.py) | 10 | 无线筛选/事实/选项、品牌/型号、条件存在性 |
| 混合目的 | [mixed_goals_cases.json](mixed_goals_cases.json) | [run_mixed_goals.py](run_mixed_goals.py) | 8 | 三商品工具组合，及订单/政策/知识搭配 |
| 既有上下文 | [context_scope_cases.json](context_scope_cases.json) | [run_context_scope.py](run_context_scope.py) | 9 | 已知候选指代、集合/逐型号、未知品类、缺对象澄清 |

旧反馈与依赖7例整组退出；旧正常命中3例因反馈差异不属于选择目标而退出。
[coverage.json](coverage.json)为当前清单；[contexts.json](contexts.json)只含调用前的历史输入，
没有任何本轮业务输出、默认空结果或命中替身。未知品类请求仍按当前正式工具职责标注。

每例事前记录用户输入、业务目的、允许工具集合（可有等效组合）、routing/coverage语义标准。
例如cs-02允许search或lookup比价；混合品牌枚举＋候选推荐需要facets和search两种工具。
工具名按集合比较，不要求顺序、同类调用次数、goal拆法或特定参数表达。

## 2. 运行边界

```text
冻结用户输入＋可选既有上下文
  → 正式QuerySession构建的Prompt、全部注册工具/schema和初始可见性
  → 正式QueryModel的一次模型响应
  → 保存原始工具调用提议，立即结束测试
```

复用正式query_context和QuerySession.build来获得模型输入；调用QueryModel.stream，
不调用Agent.invoke或QuerySession.run执行循环，不执行register_goals/query_complete等控制工具。
所有已注册业务工具都可选，不能为了提高成绩只提供三个商品工具。
只用static_tool_catalog获取正式定义，不构造真实领域handler。

模型提出错误工具、非法参数或错误控制格式也原样记录，不先执行/拒绝再纠正。
HTTP保护禁止成功响应后的第二次请求，禁止带assistant/tool反馈的请求；
未获得模型响应时仅保留正式模型适配器的有界传输重试，不算新的选择轮次。
领域工具执行预算为0，数据库入口阻断，无Phoenix、聊天服务、回答模型或持久化调用。

## 3. 判定范围

- **程序检查**：是否严格停在首次响应；实际工具名称集合是否为某个事前允许组合。
  混合目的少选/多选工具会失败；同一类型出现几次不影响集合判定。
- **独立语义复检**：routing判断所选工具是否承担正确业务目的；coverage判断混合目的是否漏选。
  参数、登记或文本只用来识别“这个工具被用来干什么”，不单独验收其正确性。
- **明确排除**：query改写精度、否定/数值端点传递、category_hint、scope、limit、参数schema、
  ID来源、登记协议、调用次数、依赖时序、查询结果、恢复行为、query_complete和最终回答。
  这些不能混进工具选择失败率，发现时最多记范围外观察。
- 只登记或只输出文本、没有提出必要业务工具：记“无法判定（未观察到工具选择）”，
  不把它判为选错工具，也不执行控制反馈强行续问。
- 缺对象时允许不选业务工具，但语义复检须确认表达了需澄清，不能将随意不调用一律判通过。
- 参数无法说明混合目的的工具归属时，语义证据不足可记无法判定，不把参数格式错误当路由错误。

程序与routing/coverage均通过且证据完整才给技术通过；语义复检不是人工验收。

## 4. 冻结、批量运行、结果与复检

每类一个批量脚本，共用执行器；默认每条重复3次，准备时可设置1～10次。
准备时可重复指定--case ID选择子集；选入/排除清单及重复次数都在调用前冻结。

```bash
# 仅冻结本地用例、上下文、准则、源码快照和预算
PYTHONDONTWRITEBYTECODE=1 backend/.venv/bin/python tests/agent_loop/business/tool_selection/run_single_intent.py --prepare --repeat 3
# 显式调用真实选择模型；每条每次只采集首次响应，领域执行预算为0
PYTHONDONTWRITEBYTECODE=1 backend/.venv/bin/python tests/agent_loop/business/tool_selection/run_single_intent.py --live --run <batch>
# 本地程序检查和语义复检包；不调用模型或数据库
PYTHONDONTWRITEBYTECODE=1 backend/.venv/bin/python tests/agent_loop/business/tool_selection/check_selection_cases.py --render <batch>
```

其他分类换对应runner。运行中不改源码/预期；失败批次不覆盖或原地续跑。
网络预算最多2次请求用于传输恢复，最多1个成功模型响应，120秒；不跑业务后续轮次。
不接入默认pytest真实模型执行，不安装依赖或启动服务。

```text
results/<时间-分类-批次>/
  report.md
  evidence/
    cases.json / contexts.json / review_prompt.md
    environment.json             目标、范围、HEAD、源码/输入指纹、模型与预算
    source/                      冻结源码、用例及准则（不含.env/密钥）
    run_started.json / run_finished.json
    executions.jsonl             开始/结束、异常、用量；无业务执行结果
    traces/<用例>-<尝试>.jsonl     首次请求/响应和实际传输错误
  checks/
    automatic.jsonl / review_packets.json / semantic.jsonl
    history/<时间>/              复检版本归档
```

独立复检者读取冻结的[review_prompt.md](review_prompt.md)，逐次读首次响应，
填写[语义记录](semantic_record.example.json)的routing/coverage，再--render合并报告。
已有判读修改前--archive；render也归档旧分析，始终不改原始调用证据。
报告区分通过/失败/无法判定/未执行，不挑选最好一次，不由程序代填语义结论。
当前检查器明确拒绝给v1/v2反馈循环批次重新评分；本地旧批次已按用户要求删除。

## 5. 离线装置验证与限制

[离线边界检查](../../contracts/test_tool_selection_boundary.py)验证正式首轮请求一致性、
零工具执行/零反馈、工具集合比较、不检查参数/次数、冻结到报告的流程以及旧批次保护。
生产scope/反馈回归仍在独立contracts文件，不能计为工具选择评测通过数。
本轮10项装置检查通过，另7项既有scope/反馈契约回归通过；相关Ruff和差异检查通过。
这些离线检查不调用真实模型；本轮39条首次选择用例的真实模型证据另见上方汇总。
本测试不证明领域内部能力、主流程后续行为、完整上下文准备、回答或端到端效果。
