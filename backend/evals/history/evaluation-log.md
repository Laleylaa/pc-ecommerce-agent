# 历史评测索引（截至 2026-10-19 清理前）

以下为历史记录，命令和成绩仅适用于原版本；当前入口见[评测说明](../README.md)。

# Agent trace 与评测产物

2026-10-18：SSE接口已退役，聊天评测统一使用 `/api/chat`。通用客服评测读取响应的执行详情；订单本地评测读取同一隔离事务内已保存的工具审计。下方SSE记录为历史证据，不代表当前入口。

2026-10-07 E53：RV-02口径确认并接入；有效前置事实可用于未完成目标的部分回答及兜底，不计作目标完成，不增加guard重试。257项相关既有隔离检查通过，无真实模型/HTTP/SSE验收，前端与整体话术精简尚未实施。见[当前改动与证据](goal_planning/partial-answer-20261007.md)，真实措辞继续RV-03。

2026-10-06 E52：TP-02按当前正式契约及既有回归关闭；OR-06四个旧测试文件84项全通过，保留订单filters评测适配待办。本次相关隔离检查542通过，无新增场景或真实评测，未改业务代码。删除/迁移依据见[报告](goal_planning/test-contract-migration-20261006.md)；下方E51及更早数量为历史记录。

2026-10-06 E51：[共享issues核验](goal_planning/issues-review-20261006.md)完成；本次25个定向测试文件（其中名称文件仅选26项）905通过/29旧链路失败/254未选。未新增或删除测试，无新真实评测。当前问题以共享issues为准，下面各轮数字仅属历史证据。

2026-10-06 E50：最终guard改为只校验，拒绝后无回跳/重试，确定性收尾及人工策略在finalize_response执行。七个相关既有隔离测试文件210项通过，无新增场景或真实评测。下面E49的30个失败是当轮结果，其中guard预算例已由本轮职责调整解决；其余旧测试不据此关闭。

2026-10-06 E49：当前 trace 覆盖、旧测试去留及后续分层方案见[整理报告](goal_planning/trace-test-review.md)。九文件既有隔离回归 197 passed / 30 failed；未新增用例或运行真实评测。失败与责任按 OR-02/OR-06 跟踪，下方成绩均属历史轮次。

本地 `run_start` 的模型 span 新增 `invocation_params/options`，参数来源见 `tool_handler_start.input_sources`；恢复准入/回跳、保存/清理与 HTTP/SSE 响应准备增加紧凑事件。保持 schema_version=1 的增量兼容，旧字段/原生载荷不删；旧记录缺少新字段时标未知。`sse_event_prepared` 不证明客户端收到，`turn_persisted` 不证明业务语义正确。完整说明见整理报告。

2026-10-04 E34：政策检索有界异步化、外设知识改接 Tavily；新增32项隔离用例，指定18文件1080通过/33写库fixture排除。正式图使用脚本LLM1/LLM2与mock HTTP，止于工具输出；未调用真实搜索或模型。旧数量断言及本地检索算法回归保留，未删除文件/知识数据。配置、场景、初次失败及修正依据见[报告](goal_planning/knowledge-search-migration-report.md)。

2026-10-04 E33：输入到工具输出44项新场景，指定范围1038通过/33写库fixture排除；目录人工数据适配器迁到正式CatalogSearchRepository，旧模拟查询删除且历史场景保留。场景、修复、清理映射与未验证项见[报告](goal_planning/input-tool-contract-chain-report.md)。旧OrderRows不接受filters，条件订单评测不能以该适配器结果作为验收；本轮使用正式订单repository＋内存SQLite验证。

2026-10-03 E23独立工具边界审查：目录解析/能力与错误输出类型最小修复；最终隔离869通过、33写库fixture排除。
范围、失败修正、完整命令和关联ID见[报告](goal_planning/tool-flow-boundary-audit-report.md)。
修复前后manifest/results/trace保留在 `results/agent_traces/audit-before-confirmed-20261003/`
与 `results/agent_traces/audit-verified-20261003/`；全部人工数据，无真实模型、配置数据库或正常LLM3验收。

统一产物目录为 `backend/evals/results/agent_traces/`，已在现有 `.gitignore` 中忽略。
用例、脚本和说明进 Git；包含请求/上下文/模型与工具原始输出的运行产物留在本机。
历史 `goal_planning/` 报告保留原位，不移动或覆盖已有证据。

## 正常购物工具评测

2026-10-19 本轮最终结果：

| 最终产物 | 结果 | 范围与观察 |
| --- | --- | --- |
| [最终36项验收](../results/catalog-shopping-20261019-120716.json) | 36/36 | 历史20/20＋新表达16/16；每项生成一次、审核一次，共72次API调用；无输出修复、重试或超时。平均2.488秒、最大3.084秒，历史20项均值2.472秒 |
| [最终错误初稿修复](../results/catalog-shopping-20261019-120605.json) | 4/4 | AND误OR、漏预算、无线充电误映射、漏订单状态；每项一次修正后通过，共12次真实API调用（另有4次替身初稿） |
| 相关隔离测试 | 955通过 | 商品、订单、编排交接/恢复/trace、公共模型调用策略；无失败/跳过，非全后端集合 |

最终报告所列实现SHA256已与工作区核对一致，Ruff与diff检查通过。中间版本[36项](../results/catalog-shopping-20261019-120521.json)和[修复4项](../results/catalog-shopping-20261019-120422.json)亦通过，但搜索跳过共用说明导致示例接线不全；补齐后重新跑上述最终版本，并保留中间记录，不作为最终版本证据。

本轮没有复现自然超时，不能归因“加长预算解决了API网络问题”；本地截止和连接/读取/SDK包装超时分类由隔离测试验证。平均耗时是当前小样本观察值，不能据此承诺线上延迟；两组模型调用可能与其他验收并发。少量新表达未逐字加入Prompt，不是生产准确率保证。上轮反思误改与超时记录保留在下方，TP-05继续跟踪更大样本及真实业务库/聊天验收。

本轮few-shot与超时优化的可复现命令：

```bash
cd backend
.venv/bin/python -m scripts.eval_catalog_shopping --acceptance
.venv/bin/python -m scripts.eval_catalog_shopping --review-probes
```

`--acceptance`为20项历史问法＋16项未逐字写入Prompt的新表达（12项连接逻辑/偏好/否定/预算，两项能力/对象缺失，两项订单筛选）；均使用当前真实模型，商品读内存合成记录，订单只验计划。`cohort`区分历史与新表达。偏好用例同时检查候选集合与preference，不能只看排序碰巧一致。它不是盲测大样本，不能据此推算总体准确率。

当前报告增加实现文件SHA256、各次模型阶段/耗时、实际API次数及终止错误details。`timeout_source=local_deadline`表示本地共享预算耗尽；connect/read/write/pool来自底层明确异常；provider_unspecified/unspecified不推断连接阶段。原始请求URL/凭据/响应体不进入传输诊断。SDK包装的APITimeoutError此前会归为planning_transport_failed，本轮修正为planning_call_timeout，外层仍终止不重启。客户端未另设HTTP超时，仍由总预算截止。

最终代码：内层60秒、外层75秒，最多两次纠错和一次传输重试不变。955项相关隔离回归通过、Ruff通过；本轮没有前端修改，未重跑前端构建，亦未验真实业务数据库/聊天/浏览器。原先35/45秒配置和分数以下保留为历史证据。

2026-10-19 上轮反思闭环验证（当时工作区，未提交）：

- `--compare`：同一组20项分别关闭/开启语义复核（16项商品工具＋4项订单规划），其他规则相同。检查返回集合，也检查指定连接条件的原文语义，防止AND被OR或某种商品配置替代却碰巧返回相同结果。订单部分只验计划，不验真实数据库执行。
- `--review-probes`：四个正常业务请求预置错误初稿，后续审核和修复调用真实模型，验证AND写成OR、漏预算、无线充电误映射、漏待发货状态。这是已知错误修复能力，不计自然发生错误率；初稿替身不产生API调用。`model_calls`计所有规划阶段尝试（含预置初稿），新增`model_api_calls`区分实际API尝试。
- `--retry-timeouts evals/results/<原报告>.json`：只复测原报告里的planning_call_timeout，生成新文件并记录来源，不覆盖或抹掉失败。
- 本轮相关887项隔离回归通过；随后正确组合条件专项扩展，`tests/catalog/test_tool_plan_review.py`共15项通过。旧执行测试中的自动approve替身只测执行/交接，不能作为模型审核有效的证据。真实修复证据用上述probes。Ruff、前端构建和差异检查通过；没有真实库/聊天/浏览器验收。

真实模型记录（温度0，仍有输出和网络波动；调优使用过的少量问法，不能代表总体准确率）：

| 记录 | 结果 | 解释 |
| --- | --- | --- |
| [初轮](../results/catalog-shopping-20261018-235755.json) | 输出集合40/40 | 只看集合的旧验收过宽；人工复查发现审核将多个能力eq误当冲突、改成三模，原始成绩保留但不作语义通过证据。随后强调领域匹配规则与能力蕴含不等价，并增强独立语义断言 |
| [最终完整对照](../results/catalog-shopping-20261019-104725.json) | baseline18/20；review18/20 | baseline一例AND误写OR、一例调用超时；review两例调用超时，完成的18例语义/结果通过；超时均未进入数据查询 |
| [超时定向复测](../results/catalog-shopping-20261019-104830.json) | 3/3 | baseline订单一例、review搜索两例均通过；仅这些超时复测成功，不把完整对照改写为首次全过 |
| [预置错误修复](../results/catalog-shopping-20261019-104754.json) | 4/4 | 四个错误全部识别并修复；一次审核缺少JSON Pointer由程序拒绝后修复。无线充电保留字段资料不足，未用无条件查询代替 |

完整对照调用次数20→40，累计耗时56.993→117.169秒，含失败等待。剔除超时后，各自完成样例均值约1.157→2.619秒（样例数19与18不同，仅观察值，不是严格配对性能基准）。单独复测后，这20个问法累计结果baseline19个通过、review20个通过；不能据此承诺零错误或给出泛化准确率。当前识别缺口、真实环境延迟与联合验收按[TP-05](issues-before-agent-loop.md#tp-05)跟踪。

`cd backend && .venv/bin/python -m scripts.eval_catalog_shopping`：调用当前配置真实模型，使用合成商品和正式搜索/目录工具，不连接数据库、不调用聊天、不保存用户消息。16个正常购物问法，预期SKU集合独立手写；会消耗模型额度。JSON保存到`evals/results/catalog-shopping-<时间>.json`，不覆盖上轮报告。

2026-10-18历史：初轮12/16，修正AND/OR主Prompt冲突、对象缺失分类及合成数据漏填后16/16。不是配置数据库或聊天E2E验收。离线业务回归：`tests/catalog/test_catalog_shopping_requests.py`共20项，固定模型输出检查正式执行，不能替代自然语言评测。

## 运行

在项目根目录执行离线冒烟（脚本模型、人工商品、假持久化，不读取 `.env`、不使用真实模型或配置数据库）：

```sh
cd backend
.venv/bin/python -m scripts.eval_goal_tool_flow --scripted
```

真实模型＋同一人工数据环境（显式花费模型额度；仍止于 LLM3 前）：

```sh
.venv/bin/python -m scripts.eval_goal_tool_flow --live --case named_product
```

需要同时上传 LangSmith 时，在本机 `.env` 配置 `LANGSMITH_API_KEY`、`LANGSMITH_PROJECT`，
再加 `--langsmith`。默认评测只写本地，即使环境中设置了 `LANGSMITH_TRACING=true`。
`--scripted` 与云上传不能组合。`--case` 可重复；省略时运行现有正式用例集。
旧 `--output` 参数保留为 results.json 的额外副本；主产物始终位于统一目录。

普通聊天通过 `AGENT_TRACE_MODE=off|local|langsmith` 控制，默认 `off`；普通聊天运行使用该记录器。
`local` 不创建云客户端，`langsmith` 追加云上传。所有模式都不修改模型参数、重试次数或业务结果。
`AGENT_TRACE_DIR` 可覆盖目录；建议保持默认。普通聊天一请求一个目录，评测一批一个目录。
聊天自动记录整个图；本轮技术评测入口主动止于工具结果交接，不评测正常回答质量。

## 查看

每批评测完成后终端打印 `Artifacts: <目录>`。将这个目录路径交给后续 agent 即可，无需截图：

```text
agent_traces/
  batch-<UTC时间>-<随机ID>/
    manifest.json                 # HEAD、dirty、源码/Prompt/锁/用例哈希、依赖版本
    results.json                  # 现有评测结果＋每条用例的 trace_directory
    report.md                     # 用例索引；异常不冒充业务成功
    cases/
      <UTC时间>-<随机ID>/
        trace.jsonl               # 每行一个运行开始/结束或边界事件，增量落盘
        summary.json              # 完整性、停止原因、阶段耗时、事件数、云上传状态
```

普通聊天目录包含 `trace.jsonl` 和 `summary.json`；只有评测批次记录实现清单。
LangChain/LangGraph 提供 `trace_id/run_id/parent_run_id`、模型输入输出和节点耗时；
本项目只补充程序能够确定的业务边界事件：

| 记录位置 | 主要证据 |
| --- | --- |
| input_rules / load_context | 原始输入、前置处理结果、历史与可信对象来源 |
| tool_model_attempt | 工具内部生成/审核的阶段、尝试序号、单次及总耗时、剩余预算；失败保留超时来源和异常类名链，不记录原始异常正文 |
| planning_attempt / response / validation_error / accepted | 实际 Prompt、schema、模型原始输出（含 invalid_tool_calls）、校验、修复与网络重试次数 |
| tool_binding_start / ready / rejected | goal/task/call/wave ID、依赖声明、绑定前后完整参数 |
| tool_contract_input / handler_start | 公开参数、服务端运行时身份、默认值展开后的内部参数；后者才表示真实 handler 开始 |
| tool_handler_output / error / output_rejected / tool_result | 原始工具输出、契约校验、正式结果；复用标注 reused_from_tool_call_id |
| normalize_tool_results / extract_task_artifacts / update_subquery_ledger | 结果解释、可依赖对象、后续任务状态与阻断原因 |

工具内部使用的 LangChain 模型自动留下子 run；未调用模型的明确编号路径不会伪造模型 span。
技术 trace 记录事实，不自动判断自然语言理解是否正确。
`results.json` 的既有关键词/数量检查只是复核提示，不能作为完整语义评测成绩。

## 错误和完整性

- `recording_complete` 只表示节点闭合且没有本地记录错误，不表示业务成功。
- 正常评测停在 `tool_handoff`、`planning_stopped` 或 `no_tool_route`。主动关闭图可能留下原生
  `GeneratorExit` root 错误，summary 的 `expected_boundary_close` 会标注该情况，保留原始错误。
- 超时、取消、校验失败保留已经写入的行；进程被强制终止时可能没有 summary，按不完整产物处理。
- 写盘或上传失败不触发业务重试。`write_errors`、`setup_errors`、`unfinished_run_ids` 必须一起看。
  云上传只标记请求过，未做服务端回读确认；本地记录不依赖云端导出或后台索引何时可见。
- 密钥字段和 Settings 对象会屏蔽；这不是任意自由文本/手机号/地址的完整脱敏器。
  默认使用人工用例，真实会话产物在分享或上传前需自行确认内容。

## 隔离回归

2026-10-18起按模块目录执行，完整清单和当前验证范围见[测试入口](../../tests/README.md)。下方历史记录中的旧路径/成绩保留历史含义，不作为当前运行结论。

```sh
.venv/bin/python -m pytest -p scripts.orchestration_audit_safety \
  tests/orchestration/test_agent_tracing.py tests/orchestration/test_goal_task_integration.py \
  tests/orchestration/test_planning_call_policy.py -q
```

测试使用内存工具和假持久化；云测试替换 SDK 写入接口并禁用后台线程，不请求云服务。
不运行 `db_session_factory`、全后端测试或 `make check`。

### 本轮验证记录（2026-10-03）

- 基点 `b34a69a` 加此前未提交工作区和本轮 trace 修改。上方三文件最终 **118 passed**，
  无失败/skip/xfail；其中 trace 文件 18 项，覆盖开关等价、修复、依赖绑定/阻断、同步/SSE、
  原生内部模型子 run、超时、并发隔离、写盘失败及云上传失败。
- `tests/test_tools.py -k 'tool_registry or tool_contracts or tool_catalog or registry_ or catalog_contract or all_public_input_models or catalog_public_schemas or document_search_public_schema'`
  加同一安全插件：**16 passed / 169 deselected**（未选或写库 fixture 排除），未运行全工具测试。
  五个正式工具定义与本轮开始的源码快照 AST 相同；本轮仅增加观察点。
- 最终 Ruff、`git diff --check`、`uv lock --check --offline` 通过。
  LangSmith 已在原锁中为 0.14.1；本轮补为直接依赖，无依赖版本升级。
  初次 uv 缓存写权限/空缓存解析失败，随后只同步根依赖元数据并通过离线锁一致性检查。
- 首轮云替身测试虽通过断言，SDK 后台线程在 fixture 退出后仍尝试 `/info`，DNS 失败；
  随后在构造时禁用后台线程并提供静态 info，最终无云请求。新 SSE 用例首次错误使用 `event`
  字段（1 failed / 16 passed），核对正式协议后改为 `type`；生产 SSE 协议未改变。
- 收尾探针复现了新 trace 接入的流式跨任务关闭问题：ContextVar token 跨 yield 导致 ValueError 和调用方上下文残留。已将作用域收窄到读取图事件期间，并显式关闭迭代器；新增正式 SSE 提前关闭回归通过，未修改原业务失败状态规则。
- 离线 CLI 冒烟使用脚本模型、人工商品和假持久化，额外阻断 socket、DNS 及 AsyncEngine.connect；
  2 次规划、1 次工具执行，到结果交接停止。真实模型、配置数据库、浏览器及云端实际接收均未验证。

本轮未清理旧测试、调整业务规则或关闭既有业务缺陷；它们继续按共享 issues 的原状态处理。

### 后续测试去重（2026-10-03，E22）

Trace 接管过程记录和事后检查；自动判断对错仍由测试承担。本次只删除下面的重复或静态清单检查，
不以失败、文件较旧或已有 trace 为删除理由。

| 删除项 | 保留的检查位置 |
| --- | --- |
| `test_orchestrator_wave_loop_cases.py::test_wave_loop_outcome_cases` 的 8 个参数用例：商品成功/空/不支持/计划无效、目录成功、订单不存在、知识文档成功、工具超时 | `test_orchestrator_outcomes.py::test_normalize_tool_result_distinguishes_execution_from_usable_information` 覆盖同一归一化分支及可用性；`test_usable_outcome_is_ready_to_answer_not_implicitly_request_complete` 保留知识文档到 ledger 的断言 |
| `test_orchestrator_tool_observation_eval.py::test_tool_observation_eval_covers_remaining_tools` 的 1 个清单用例 | 删除固定用例数/ID 数及手写工具名集合的重复检查；真实工具注册集合和元信息由 `test_tools.py::test_tool_registry_exposes_expected_business_tools`、`test_tool_contracts_expose_llm_safe_metadata` 检查。未删除 17 个观察场景本身 |

同时删除已无调用的归一化导入、未使用的第二商品常量；没有修改业务实现、保留测试的函数体，
也没有把旧测试改成 skip/xfail。没有宣称 trace 自动覆盖了旧断言。

实际验证使用 `-p scripts.orchestration_audit_safety`：

- 清理涉及的三个文件（上述两个文件＋`test_orchestrator_outcomes.py`）：删除前 **58 passed / 22 failed**，
  删除后 **49 passed / 22 failed**。22 个失败 node ID 完全相同；保留的函数 AST 相同。
  失败集中在旧 `catalog_context` 注入预期及缺少 `route_plan` 的手工状态，按 OR-06 保留待迁移。
- trace、正式图内存接入、规划调用策略三文件：本轮重新运行 **118 passed**。
- 上述两个真实工具注册替代检查：本轮 **2 passed**。
- 没有运行配置数据库、真实模型、全后端或浏览器；没有 skip/xfail，也没有写库 fixture 进入执行集合。
- 原始输出和 JUnit 存在 `backend/evals/results/test_cleanup/20261003/`；
  `comparison.json` 记录删除数量、同一失败集合、保留函数及生产源码一致性。

下一步先迁移 OR-06 中仍有效的 17 个旧观察场景及 wave 终态状态构造，再判断哪些重复执行夹具可以整段删除。
