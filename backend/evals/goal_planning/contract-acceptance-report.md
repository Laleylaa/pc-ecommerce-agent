# TI-01 / TI-03 验收记录

- 日期：2026-10-01；分支 `feat/orchestrator-catalog-integration`，基点 `744cc0f` 加工作区契约修复。
- 结论：**TI-01 满足本项关闭条件，从 issues 删除；TI-03 保留，转为待修复。** 已关闭 ID 不复用。
- 本次仅新增验证入口、产物及更新文档，未修改业务实现或 Prompt。
- 隔离回归重新执行：196 passed、1 xfailed（GI-02 纯空白输入），无配置数据库 fixture。
- 模型 `deepseek-flash`，temperature=0；19 个固定输入，19 次 LLM1、14 次 LLM2 调用，均一次生成即通过结构校验，无规划修复。自动检查 18/19 通过；人工复核另发现 `products_two` 回答违反分页口径，不能把自动通过率当作整体验收通过率。
- 其中 9 例运行正式完整图到 ChatResponse，含真实回答模型及 5 次正式工具绑定执行（其中一次注入依赖故障）。其余 10 例止于规划。数据仓库、检索资料、审计与会话持久化为人工 fixture，不连接配置数据库。

## TI-01 关闭依据

| 范围 | 固定用例及实际结果 |
| --- | --- |
| 当前用户显式订单号 | `explicit_order`：订单号7001进入 order_lookup.order_id，正式工具返回人工订单与物流，ChatResponse 含对应订单卡片；`short_order`：短编号123也通过规划校验 |
| 历史订单 | `historical_order`：LLM1 引用可信订单对象，LLM2 使用 context binding，未直填历史 order_id |
| 目录与对象事实 | `layout_choices` 选 catalog_facets；`object_layout` 选 catalog_search，SKU201的 query/scope 一致 |
| 正式 schema 与身份边界 | 重跑[契约对齐测试](../../tests/test_planning_contract_alignment.py)及[执行策略测试](../../tests/test_planning_execution_policy.py)：参数类型/来源、身份覆盖拒绝、绑定、正式 repository 归属谓词均在隔离环境通过 |

以上覆盖本项的公开说明、订单输入路径、校验与自然表达回归。未在编排复制目录规格字典。目录工具内部布局解析仍属于 CF-01；本轮目录两例只验收规划选择，不宣称目录查询整体修复。身份隔离证据使用 SQLite 内存表，未宣称真实 PostgreSQL/API 权限验收。

## TI-03 已通过与剩余失败

通过的范围：商品/订单显式 limit=2、未指定 limit 时省略；政策显式检索数量与回答分点数分开；明确知识库检索 limit=1 保持到正式工具执行；历史复用、无历史时澄清、历史复用混合当前订单查询；明确商品跳页或排除上批继续查询时不规划工具。完整回答中的历史复述、澄清、明确分页拒绝、订单物流和知识来源均已人工查看。

失败路径：`tool_failure` 注入 OSError，正式绑定返回 dependency_unavailable；工具仅调用一次，最终回答“业务信息查询暂时失败，请稍后重试。”，没有虚构订单卡片或将失败说成无订单。格式、参数、修复次数及其他异常边界由本轮隔离回归覆盖。联网放行前另有连接失败探针，未产生模型输出，不计入19例。

### 1. 查询成功后仍承诺商品翻页（确认缺陷）

- 输入：`挑两款白色的蓝牙键盘给我看看`（`products_two`）。
- LLM1/2 正确，limit=2，返回两张商品卡片。
- 实际最终回答包含：`符合条件的一共有 4 款，本次先展示这 2 款，如果你还想看更多，我可以继续往下翻。`
- 定位：[answer_context.py](../../app/agent/answer_context.py) 的 `_response_contract` 对 catalog_search v3 仍要求 `按next_offset说明分页`，并向回答模型提供 next_offset。无工具目标的 graph._summarize_goals 已有禁止分页说明，但有工具结果的回答链仍收到上述旧指令。
- 下一步：对齐商品结果回答契约，区分内部结果窗口统计与用户可用能力；保留返回数量/候选池解释，不承诺跳页或自动排除旧商品。不能误删订单自己的续查能力。

### 2. “找一条知识资料”被直接回答（自然表达边界待修复）

- 输入：`找一条关于红轴和青轴差别的知识资料`（`knowledge_one`）。预期尊重找资料要求，检索并限制一条证据。
- 实际：LLM1 输出 direct_response，goal.query 还省略了“一条”；LLM2/knowledge_search 未执行，最终直接给出较长的常识解释，没有资料来源。
- 补充输入：`请检索知识库，返回一条关于红轴和青轴差别的资料，并注明资料来源，不要仅凭常识回答`（`explicit_knowledge_retrieval`）通过：knowledge_search.limit=1，回答引用人工文档8002。
- 结论：明确检索后的数量链路可用，普通找资料表达的语义路由不稳定；不是工具将 limit 覆盖。一般外设知识可直答的已确认规则继续成立，不应为修此例强制全部知识问题查工具。
- 下一步：在 LLM1 区分普通知识问答与用户要求查找/检索资料；使用独立近义表达复测并保留知识直答反例，不在 Runtime 写关键词强制路由。

上述两项仍在 TI-03 跟踪，本轮未实施修复。正式 HTTP/SSE、持久化、真实数据库与检索索引、浏览器页面展示未验收，相关 RV 问题保留；9 个 ChatResponse 不等同于9次浏览器端到端验证。

## 原始证据与复现

- [首批17例原始计划、回答及源码 SHA-256](contract-acceptance-results.json)
- [补充2例原始计划、回答及源码 SHA-256](contract-acceptance-supplement.json)
- [验收脚本与固定用例](../../scripts/eval_contract_acceptance.py)
- [隔离回归的范围与安全复现命令](contract-alignment-report.md)

首批结果保留原始自动评分，人工发现的分页违规没有覆盖原始 JSON。脚本仅检查结构、参数和部分响应形状，最终回答必须人工审核。补充用例独立保存，没有覆盖或重采样掩盖首批失败；补充前仅增加两个用例和格式化脚本，业务源码未变。各结果保存实际运行时源码指纹。

从 backend 使用现有环境重跑（会调用配置的模型服务；无需初始化或 seed）：

```sh
.venv/bin/python -m scripts.eval_contract_acceptance --live --output /private/tmp/contract-acceptance-rerun.json
```

当前脚本包含全部19例，可重复传 `--case` 选用例；不要覆盖历史证据文件。模型小样本通过不保证未来采样与未覆盖表达稳定。
