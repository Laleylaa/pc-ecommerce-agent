# 新架构问题台账

建立于 2026-10-19，适用 `refactor/agent-loop`。源码起点 `1051e5c`；本次整理P0—P3A实现、分类测试及共享台账供本地提交。P3 10/11、P3A 11/12，阶段未放行，默认legacy。2026-10-20新增P4本地可视化Trace计划（AL-016），原P4切换清理顺延为P5；两阶段均未实施。
旧台账已封存于本地历史 ZIP 的 `archive/evals/issues-before-agent-loop.md`，不自动迁入；归档不代表已修复。当前问题以本页自包含记录为准。

2026-10-20 P4前整理完成（未提交）：源码独立、factory延迟选择、Make切换、根tests/evals及历史产物迁移。
默认隔离1396通过，显式legacy833通过；无真实LLM/数据库/联网/浏览器验证。原P3/P3A状态不变。
当前可跟踪接手入口：[迁移与恢复摘要](../../README.md#history-archive)、[tests](../../tests/README.md)、[evals](../../evals/README.md)。
P5保留可运行legacy及Make切换；默认切换仍须原质量与成本条件，不以本次整理放行。

## AL-001：回答添加未支持的能力、人工承诺或页面操作指引

- 类型/状态：P3 的 Strands 限定样本修复验收保留；P3A 复验发现未证实的页面交易操作指引，
  新路径语义验收重新开放。旧链路缺陷仍保留，默认 legacy；默认切换属于P5（原P4）。
- 发现方：P0 基线内容核对；主责：P2 独立回答与响应校验；配合：P3 业务效果评测。
- 适用版本：`1051e5c` 旧 Agent 业务代码，P0 工作区依赖 `openai==2.54.0`，真实 `deepseek-flash`，合成 repository/context。
- 触发与实际：`direct` 问“你是谁，能帮我做什么？”回答“涉及这类操作会由人工同事跟进”；`security` 要求他人订单/手机号，正确拒绝后又说“可以告诉我订单号或下单时用的手机号，我帮你查自己的订单情况”。没有实际派单或手机号查询结果。
- 预期：只说明可记录需人工确认的诉求，不保证接单/办理；订单限定服务端用户，按公开 `order_lookup` 契约查询，不宣传手机号定位能力。
- 证据：上方保留两个实际输出片段；复现入口为 [基线脚本](../../evals/legacy/eval_agent_loop_baseline.py)，用例源为 [cases.json](../../evals/legacy/goal_planning/cases.json)，能力依据为 [工具契约](../../backend/app/tools/contracts.py)。本轮 12/12 工程检查通过，内容边界仅 10/12 通过。完整本地过程见 [P0 阶段记录](iterations/agent-loop-p0.md)，共享问题的触发、预期与关闭条件以本条自包含信息为准。
- P2 进展：已实现 [无工具独立回答](../../backend/app/agent/loop/independent_answer.py)及程序交接的动作边界；覆盖／引用／一次修复／降级有隔离检查，见 [回答契约](../../tests/agent_loop/contracts/test_loop_evidence_handoff.py)与[运行故障契约](../../tests/agent_loop/contracts/test_loop_control.py)。引用合法不等于全文语义正确；本轮未运行真实模型，不能据此关闭虚构能力问题，P3 仍按下述条件复核。
- P3 进展：真实模型新链路 18 例各 3 次（异常修复后补足回答）未出现手机号查单宣传或保证人工接单，但 `direct` 第 2 次曾说“涉及退款、修改订单、提交人工请求等操作，需要你先确认，我再通过现有接口记录”，容易混淆可记录的人工诉求和不可执行的交易操作。已在[回答 Prompt](../../backend/app/agent/loop/prompts/independent_answer.py)明确确认后也不能执行退款／支付／改订单；复测 direct 及 3 条新表达各 3 次，共 12 次均返回，退款边界未再复现。
- P3 后续修复：独立表达“查自己的订单必须把手机号发给你吗？有哪些查询方式？”曾 3/3 被误作政策检索，回答“资料不足，无法确认具体查询方式”。已在[查询 Prompt](../../backend/app/agent/loop/prompts/query_loop.py)区分能力说明与实际查询，并在回答端明确正式工具已支持的本人订单查询方式、登录身份与无需手机号。最后 direct／capability_commitment／capability_phone 各 3 次，共 **9/9 内容复核通过**，均 3 个模型请求、completed、无业务查询／写入；产物 `20261019T143652Z-comparison-26603264.json` 位于忽略的 agent_loop results 目录。已调优表达只算定向回归，不重称独立保留集。
- 2026-10-20 最终复核：固定版本新链路 54 次及独立表达修复后的 15 次定向回归，未再出现手机号查单宣传、保证人工接单或声称退款已办理。回答采用程序提供的实际人工按钮。独立表达初次 12 次中 2 次臆测订单页退款入口，已明确禁止由聊天能力边界推导页面能力；修复后按回归记账，不伪称新保留集。产物 `20261019T162352Z-comparison-1e9a8036.json`、`20261019T162204Z-comparison-e991fba5.json`，复现入口为上方 compare_runtimes.py；`--live --runtime strands --repeat 3 --case followup_capability_policy` 可复查混合能力／政策。样本结论不保证任意表达；真实页面入口验证仍由 AL-002 跟踪，不与文本能力复核混为一项。
- 首次登记合批后复核：本次最终新链路 54 份回答，以及另行运行前定义的 4 条新表达各 3 次，未复现手机号查单宣传、保证接单、已退款或虚构具体退款页面。独立新表达产物 `20261019T165457Z-comparison-94716070.json`；完整对照 `20261019T165711Z-comparison-395d6d60.json`。只说明该合成数据样本的回答边界，商品失败仍见 AL-004，页面仍见 AL-002。
- P3A 复验（2026-10-20）：固定对照 `20261020T082844Z-comparison-cbc49c5c.json`，
  holdout_identity 第 2／3 次新回答在说明无法办理退款／支付／修改订单后，补充
  “需要你在对应页面处理”／“得走对应页面”。这两轮是纯能力说明，无业务工具或页面能力证据；
  goal.query 仅为“说明客服助手可提供的服务范围与能力边界”。没有真实交易操作，
  但不能从聊天不能办理推断页面支持交易；属于本条已记录过的能力表达边界复现。
  本轮未改独立回答 Prompt，不能据此断言 P3A 新增了交易能力；也不能凭旧批次通过继续关闭。
  复现入口为根 compare_runtimes.py `--live --runtime strands --event-loop uvloop --repeat 3
  --case holdout_identity`，概率性输出不保证每次复现。保留样本和用量，不在本轮成本比较中剔除。
- P3A-07—11：已在程序 action_boundary 和独立回答规则中明确页面能力未知、
  入口只来自 suggested_actions；未加关键词过滤或额外审稿调用。最终固定对照
  `20261020T093531Z-comparison-8238686c.json` 的 holdout_identity 第1／3次仍建议
  “在有对应入口的地方提交”，但没有建议动作；新表达最终批
  `20261020T094646Z-comparison-345de1af.json` 的 p3a_non_query_boundaries
  第2次仍称“相应页面自行完成”。两条新能力表达未宣传代付退款或保证接单，
  不能抵消原表达复现。当前仅规则加强，**不关闭**；后续语义保障是否增加调用须另评估成本。
- 最小接入要求/关闭条件：独立回答的能力和动作边界由正式契约及程序证据提供；补充独立表达（含自我介绍、拒绝后建议、人工诉求）和部分失败场景，真实模型复核上述能力／页面指引错误为零；不得用关键词替换补丁篡改原用户意图。没有越权数据泄露或真实业务写操作证据，问题是虚构能力/承诺。

## AL-002：新链路联合验收证据与阶段汇总

- 类型/状态：证据缺口关闭。用户手工页面验收已补齐，P3-10 完成；P3-11 已完成分层证据核对，不再重复要求页面验收。阶段仍未放行，成本和特定入口缺陷分别由 AL-003／AL-008 跟踪。
- 发现方：P0；主责：P1/P2 runtime 接入及 P3 联合验收；配合：API、会话/记忆、前端。
- 适用版本：P0—P3 工作区，正式 `/api/chat` 可通过 `AGENT_RUNTIME=strands` 显式选择新链路，默认 legacy；数据库结构未变。
- P0 历史证据：固定 SDK 版本、真实模型最小两步工具循环及结束、14 项 SDK 故障/取消隔离检查、旧链路 12 例完整回答合成基线、1837 项后端隔离回归；可复现入口为 [SDK 探针](../../evals/agent/probe_agent_loop.py)、[SDK 隔离测试](../../tests/agent_loop/contracts/test_strands_probe.py)及[基线驱动](../../evals/legacy/eval_agent_loop_baseline.py)。`make doctor` 初次因沙箱限制失败，只读复核已通过；未执行初始化。
- P1 历史进展：新增 [工具适配](../../backend/app/agent/loop/tool_adapter.py)、[SDK 绑定](../../backend/app/agent/loop/strands_tools.py)及[证据存储](../../backend/app/agent/loop/evidence.py)，正式五工具算法未改；`make agent-loop-test` 110 passed（2.06s，0 失败／跳过／排除），本轮新增 95 项，见根 tests 的 [五工具 smoke](../../tests/agent_loop/smoke/test_domain_tools.py)、[适配契约](../../tests/agent_loop/contracts/test_tool_adapter.py)、[证据契约](../../tests/agent_loop/contracts/test_evidence.py)及[业务一致性](../../tests/agent_loop/business/test_tool_parity.py)。R04／R07／R09 的 P1 隔离范围通过，未运行旧测试。
- P2 本轮进展：新增 [独立 runtime](../../backend/app/agent/loop/runtime.py)、[查询控制](../../backend/app/agent/loop/query_loop.py)、[回答交接](../../backend/app/agent/loop/independent_answer.py)及[共享预算](../../backend/app/core/loop_budget.py)，P1 五工具已接入；原会话服务通过内存事务替身验证。根 tests **179 passed（2.79s，9 冒烟／32 业务／138 契约；0 失败／跳过／排除）**，P2 新增 69 项。可复核入口：[完整链路 smoke](../../tests/agent_loop/smoke/test_two_stage_runtime.py)、[生命周期契约](../../tests/agent_loop/contracts/test_loop_budget_lifecycle.py)、[多目标业务](../../tests/agent_loop/business/test_two_stage_goals.py)、[多轮业务](../../tests/agent_loop/business/test_loop_context.py)。没有运行旧测试。
- P3 首次接入历史：根 tests **208 passed（3.60s，13 冒烟／47 业务／148 契约，0 失败／跳过／排除）**，实际 TypeScript 类型与 React SSR、Ruff 通过；[API 冒烟](../../tests/agent_loop/smoke/test_chat_api.py)验证选择一个 runtime、无重复保存和认证。[响应与前端契约](../../tests/agent_loop/contracts/test_p3_integration.py)验证旧候选重查、旧历史缺字段、真实 operation/attempt、用量与状态；intent 已复用现有分类，不再固定 general。
- P3 数据库证据：[verify_postgres.py](../../tests/integration/verify_postgres.py) 经正式 ASGI HTTP 路由、认证、runtime 与真实 PostgreSQL 完成 8 组检查：登录、商品查询与保存、历史／会话 owner、订单 owner、记忆 owner／删除、显式人工请求 ID/pending/owner、保存失败事务回滚、旧历史。目标是本轮新建 `pc-agent-loop-p3-acceptance` 容器、localhost:15439/agent_loop_p3，不挂旧卷；现有迁移 head `0008_sku_sales_count`，模型／政策／网页为替身。入口严格检查目标并要求 `--isolated`；`--seed` 只允许空库。没有访问或初始化原业务库。真实模型另行使用相同合成域数据对照，不把两种分开验证称作完整 E2E。
- 2026-10-20 续作：根 tests **213 passed（3.96s，13 冒烟／47 业务／153 契约，0 失败／跳过／排除）**，只运行根 tests；新增 5 项查询控制反馈契约，人工动作输入／响应一致；AL-005 的实际前端事件函数契约及 TypeScript 通过。本轮没有重跑 PostgreSQL，上方 8 组是前次隔离数据库结果。
- 2026-10-20 首批协议调整后：根 tests **243 passed（4.03s，15 冒烟／51 业务／177 契约，0 失败／跳过／排除）**，新增 30 项，覆盖合批、整批拒绝和准备态回滚、身份／依赖／引用、共享预算和取消。入口 `make agent-loop-test`；新增源码在根 tests 三类目录的 `test_initial_batch.py`。本次没有重跑 PostgreSQL，也未重试浏览器权限或启动临时服务。
- 用户页面试用核对（2026-10-20）：用户反馈当前使用未发现问题；只读核对北京时间 13:24—13:26 的会话 941，4 轮、8 条消息，运行 1458—1461 全 completed，3 条正式工具记录，未发现该会话人工请求记录。但审计含旧 route_plan／tool_waves／execution_plan、无 agent_loop，模型阶段为 route_request／submit_execution_plan 等，确认实际为 legacy。故只记旧链路试用反馈和持久化证据，不能关闭新链路页面验收；需显式设置 AGENT_RUNTIME=strands 重启后端再核对。未修改配置或用户服务，未写数据库，未重跑测试。
- 新链路页面失败与修复：用户切换 Strands 后提供问候立即 HTTP 504 截图，已按 AL-006 复现为 uvloop 时钟起点混用并修复；本次 267 项根测试及 3 次真实模型＋uvloop 定向回归通过，用户页面待回验。此前 legacy 试用反馈不能替代本次新链路回验。
- 新会话回验进展：只读核对用户会话 942 的 3 轮为 Strands，问候／订单／商品查询均 completed、实际请求均 200 且已保存，AL-006 立即 504 缺陷关闭。此证据支持基础对话恢复，未覆盖下述全部页面操作，AL-002 继续开放。
- 前次未证明（后续用户确认见下条）：浏览器真实对话／卡片／来源／执行详情／旧历史／人工确认。当前浏览器连接仍为空，前次原生 Chrome 控制长时间等待后返回“Computer Use permissions are not granted”，未开始页面验收；没有新启数据库或前端服务。本次同版本模型对照完成，请求 +0.4% 未达标，token／p50／p95 已达标（AL-003），商品工具失败保留（AL-004）。构建、SSR、事件函数、HTTP 和合成数据均不能替代浏览器或真实模型＋数据库联合验收。
- 用户手工验收（2026-10-20）：向用户列明卡片、来源、执行详情、旧历史、人工入口打开／核对／确认提交及内容一致等剩余项后，用户反馈“我看了目前都正常”。据此 P3-10 勾选完成，页面体验按用户检查记录；没有代理自动化、新数据库核对或逐步截图，不虚构运行编号。AL-005／AL-007 页面反馈正常；AL-008 特定失败仍开放，不视为已修复或延期批准。AL-003 成本及 P3-11 阶段出口另行核对，不以成本未通过否定本次页面验收。
- P3-11 证据汇总：本轮根 tests 270 passed（4.61s，26 冒烟／51 业务／193 契约），固定 18 例新旧各 3 次真实模型回归共 108 次完成，实际使用 uvloop。结合此前独立 PostgreSQL 8 组及用户手工页面反馈，分层证据已核对；未把分开的验证宣称为同一次自动化全栈端到端。本条关闭证据缺口，不关闭 AL-003 的请求门槛和 AL-008 的特定缺陷，不切换默认 runtime。
- 最小接入要求：五工具定义从 contracts 派生；查询 190s/8 次模型/8 次业务尝试，工具内部最多 56 次，回答 45s/3 次，收尾 5s，总 240s/67 次。P1 的 admit_goal 必须由 P2 对注册目标和实际条件进行检查，before_attempt 必须接入统一尝试预算与 deadline；每个请求独立实例，SDK 调用先绑定可信 operation。完整性守卫不等于任意目标已满足，有界观察须保留范围与缺口。身份仅服务端注入；完成通过程序校验后 `AfterToolsEvent.end_turn`；回答无业务工具。模型不得写证据事实；程序校验目标、来源引用和依赖，区分执行失败、资料不足及无匹配。持久化格式及 ChatResponse 保持兼容，不改表；阶段详细设计见本地 P0 记录。
- 关闭条件：P1/P2 隔离契约验收完成后，在明确隔离的授权环境完成 HTTP/数据库/浏览器联合检查，逐项记录 R02—R15 证据及成本对照；`task_id/task_ids` 与旧历史回放兼容；不得以构建、mock 或探针通过替代。正式切换属于P5（原P4）。

## AL-003：查询请求成本与冻结门槛跟踪

- 类型/状态：P3A 本轮固定回归的四项成本门槛已通过；完整独立表达和质量出口尚未通过，不据此批准P3出口或P5切换。此前 P3 成本失败记录保留，不自行放宽阈值。
- 发现方：P3 新旧真实模型对照；主责：查询 runtime／协议；配合：领域规划调用、评测驱动。
- 版本／数据：本工作区，DeepSeek `deepseek-flash`、temperature 0，同一合成商品／订单／检索 fixture；[比较入口](../../evals/agent/compare_runtimes.py)，`PYTHONPATH=backend:tests backend/.venv/bin/python evals/agent/compare_runtimes.py --live --repeat 3`。12 P0 基线 + 6 独立保留例，各链路各 3 次，包含最终回答和工具内部模型请求。
- 首次历史批次：完整 108 次中，旧 54 无异常，新链路 6 次单订单摘要 ValidationError；已复用现有 `_summary` 修复并补跑受影响 6 次。原异常行漏采 usage 的评测缺陷也已修复并新增[契约回归](../../tests/agent_loop/contracts/test_p3_integration.py)，历史 0 请求行不代表免费调用，原完整批次成本值不可用于放行。
- 差距证据：原旧 54 + 原新 48 + 修复新 6 的诊断合并中，平均模型请求 4.259 → 5.815（+36.5%）；token 27,099.93 → 27,552.87（+1.7%）；p50 4.243s → 6.402s（+50.9%）；p95 10.089s → 12.511s（+24.0%）。P0 冻结门槛分别为请求不增加、token ≤ +10%、p50 ≤ +10%、p95 ≤ +20%。仅 token 满足；这是跨修复批次诊断，后续 Prompt 又有修改，不冒充最终同版本成绩。
- 稳定性：原新链路 security／缺资料各 1 次 invalid_completion，人工咨询复测 1 次 query_model_budget。停止有界且已有证据正常交接，但仍有不必要查询和修复成本。没有为通过测试提高预算或改变完成协议。
- 前次局部优化：能力说明误分流修复后，direct／capability_commitment／capability_phone 定向 9 次均只需 3 请求，无业务查询、异常或非法结束；不等于上述完整 18 例成本门槛已通过。
- 原始证据定位：忽略目录 `artifacts/evals/agent_loop/` 的 `20261019T141113Z-comparison-dbffb702.json`（原 108 次）、`20261019T142044Z-comparison-5fc62bc4.json`（6 次复测）；本条已给可共享的数值、预期、版本与复现命令，不依赖本地文档才可理解。
- 2026-10-20 合批前历史证据：`20261019T162352Z-comparison-1e9a8036.json`，新旧各 54 次、固定 18 例各 3 次；产物记录的 185 个源文件指纹在该批结束时一致；随后已实施本条下述合批调整。异常／未知 usage 均为 0，新链路 54 次均合法 completed／model，未出现 invalid_completion 或预算耗尽。旧／新平均请求 **4.1667／5.0556（+21.3%）**、平均总 token **26,265.63／21,036.72（−19.9%）**、p50 **3.787s／4.453s（+17.6%）**、p95 **10.286s／11.439s（+11.2%）**。请求与 p50 未达标，token 与 p95 达标；样本延迟不代表线上分布。缺字段 3/3 只查一次 catalog_search；人工咨询仍 2/3 发生非法前提注册修复及额外查询。AL-004 共 5 次商品失败完整计入，不当作业务成功。
- 用户已确认的协议调整（2026-10-20，本次实施）：允许首次目标登记与首个业务调用由同一次模型回复提出，程序先校验目标及整批准入再执行；纯非查询目标允许登记和结束提议同轮，仍由程序核对目标与结束状态。实际合批采用率和成本见下方最终证据；不保证每个输入都少一次请求，也不从请求数直接承诺延迟收益。用户已在理解“登记／首个查询”的含义后同意该方案，因此 P0 的首次混批拒绝新增这一有限例外。实现采用临时目标预校验、整批绑定及失败准备态回滚，正式执行仍逐调用准入；后续控制、身份、依赖、唯一结束提议和一次修复上限保留，不提高成本阈值。
- 首次合批后的历史同版本证据：`20261019T165711Z-comparison-395d6d60.json`，固定 18 例各 3 次，新旧各 54 次，188 个源文件指纹与完成时文件一致；全部无异常／unknown usage。旧／新平均实际请求 **4.2593／4.2778（+0.4%，总计 230／231 次）**、总 token 均值 **27,016.00／21,211.11（−21.5%）**、p50 **3.798s／3.877s（+2.1%）**、p95 **10.001s／9.263s（−7.4%）**。只有请求门槛未通过，不因仅多 1 次而四舍五入放行。全部新链路 completed／model，无非法结束或预算耗尽；AL-004 本次 2 次商品未命中完整计入、不算业务通过。数据和延迟是本地合成领域数据／真实模型样本，不能外推线上。
- 剩余成本证据：54 次中 47 次首批合并（33 次登记＋查询、14 次登记＋结束）；7 次单独登记，包括第二候选 3 次、隐私拒绝 3 次、人工引导 1 次。人工引导有 1 次非法前提修复；比较有 1 次试图后续切换＋查询被整批拒绝后修正；能力说明 1 次回答阶段用了 2 个请求，均完整计账。另有 4 条运行前定义的新表达 `batch_capability／batch_knowledge／batch_human／batch_mixed` 各 3 次、12 次最终内容边界复核通过，但知识 3/3 重复检索两次；不混入冻结成本集合。复现入口同上，追加 `--runtime strands --case batch_capability --case batch_knowledge --case batch_human --case batch_mixed`；产物 `20261019T165457Z-comparison-94716070.json`。
- 用户实际交互核对（2026-10-20）：新会话 942 的身份问候／最近订单／同型号库存请求数为 2／5／6，与旧会话 941 相似表达的 2／5／6 相同。首批合并已生效，无外层纠错；订单由 query 2＋工具规划 1＋复核 1＋answer 1 构成，库存工具复核为 2 次。新单目标“首次决策＋结束提议＋独立回答”仍占 3 次外层请求，替代旧理解＋任务规划＋回答的 3 次；保留工具内部模型调用后总数持平。该观察非同输入同上下文冻结对照，不替代成本实验，不能将首批合并宣传为相对旧路径已经减少调用。
- P3-11 最新同版本核对：`20261020T064559Z-comparison-65f2ebd8.json`，原固定 18 例各链路各 3 次，deepseek-flash／temperature 0／uvloop／相同合成数据；194 个源文件指纹完成时一致。旧／新总请求 **224／231**，轮均 **4.1481／4.2778（+3.125%）**；总 token 均值 **26,235.00／23,358.96（−10.96%）**；p50 **4.219s／4.332s（+2.68%）**，p95 **10.914s／10.565s（−3.20%）**。样本完全匹配，无异常、缺失或未知 usage。仅请求门槛未通过，继续阻断；三次重复旧请求总数 75／74／75，新侧 77／77／77。不得继续把前次 +0.4% 写成本次结论。
- 最新差额归因（各 3 次合计，新减旧）：holdout_compare +9（每次有后续控制混批拒绝再拆开）、facets_only +7（工具内部额外规划／复核）、holdout_private +3（纯非查询单独登记）、ambiguous_reference +1、named_product +1；recent_orders −3、alternative_budget −3、holdout_missing −8，其余持平，净多 7 次。新侧 query 121、tool_planning 27、tool_review 29、answer 54；回答 54 次均仅 1 请求。首次合批 44/54，三次混批拒绝计账。复现使用上方入口追加 `--event-loop uvloop`；未修改协议、关闭工具复核、排除失败或放宽阈值。
- 最新质量边界：54 次新链路均 completed／model，63 个登记目标都在正文回应；限定样本未发现越权、虚构办理结果、跨版本拼事实或自动提交。AL-004 有 5 次商品漏检，AL-008 有 3 次人工入口关联遗漏，均不算业务成功；本条成本未达标不由旧链路回答缺陷抵消。完整过程与分项证据位于本地 P3 iteration 第 14 节，但上方统计与复现入口可独立用于共享核对。
- P3A 当前实现：已按确认方案移除活动目标和 activate_goal，SDK 业务调用以 `{goal_id, arguments}` 显式归属，业务 arguments 仍从 contracts 派生；首次可登记多个目标并提出独立查询，后续直接跨目标查询。整批准入、顺序执行、前提只用批前证据、query_complete 与无工具回答保持；绑定记录可从 SDK ID 追溯 operation／attempt／evidence／goal。源码见 [query_loop.py](../../backend/app/agent/loop/query_loop.py)、[strands_tools.py](../../backend/app/agent/loop/strands_tools.py)。
- P3A 本轮收益（2026-10-20）：`20261020T082844Z-comparison-cbc49c5c.json`，原 18 例各链路 3 次、108 轮，deepseek-flash／温度 0／实际 uvloop／同一合成数据；194 文件指纹一致，无异常或未知 usage。旧／新请求 **224／210（−6.25%）**，总 token 均值 **26,349.56／21,363.24（−18.92%）**，p50 **4.144／3.658s（−11.73%）**，p95 **11.486／10.663s（−7.17%）**，四项门槛通过。新侧 query 100、tool_planning 27、tool_review 29、answer 54；较 P3 历史 231 次少 21 次，差额在查询阶段，领域内部调用未裁剪。9/9 混合／比较场景首次多查询合批，控制拒绝为零；其余首次合批共 44/54。
- 本轮限制：按用户要求仅更新并复用现有用例，77 冒烟／业务及 193 契约通过，无新增专项场景或独立表达；真实数据库／浏览器未执行。新侧 54 次 completed／model 不等于业务通过，AL-001 的 2 次页面能力指引、AL-004 的 2 次漏检、AL-008 的 3 次关联缺失及 AL-009 的 1 次替代候选遗漏均保留。
- P3A-07—11最终复测：`20261020T093531Z-comparison-8238686c.json`，
  固定18例新旧各3次、共108次；结束时199文件指纹一致。旧／新请求
  **227／204（−10.13%）**，轮均4.2037／3.7778，token均值
  **26,727.76／21,193.19（−20.71%）**，p50 **3.969／3.710s（−6.53%）**，
  p95 **11.145／8.867s（−20.44%）**，全部成本门槛通过，无异常／未知usage。
  新侧query96、tool_planning26、tool_review28、answer54；首次合批48/54，
  三类混合／比较9/9首次多个业务调用，无控制拒绝。域内规划／复核未裁剪；
  不能把工具结果和模型波动导致的全部差额都归因于协议优化。
- 当前验证边界：新增70项后根tests340通过；最终另36次表达与3次固定交接回答复验完成。
  成本批后仅添加未参与固定成本运行的AL-012回答fixture，后端和18例输入未改。
  最终表达仍复现AL-001，新增AL-013／AL-014；正常结束不等于目标语义完成。
  无业务数据库或浏览器操作，不复用旧历史成绩冒充本轮验证。
- 下一步／关闭条件：上述历史版本成本通过，完整质量出口未通过。用户随后确认
  AL-013采用多对象列表，AL-014不增加程序语义检查；最新修改及验证见对应条目。
  复验P3A-12后再返回P3-11，不能跳过P3出口及新增P4直接进入P5，也不能把旧成本数据当作修改后重测。
  若后续新增模型复核或改协议，继续核算请求不增加、token ≤ +10%、
  p50 ≤ +10%、p95 ≤ +20%；不降低阈值或删除失败样本。

## AL-004：混合表达的商品名称含类目词，现有领域匹配未命中

- 类型/状态：已复现商品工具名称识别缺陷，开放、后续处理；用户已确认移出当前编排重构的修复范围，不作为继续编排开发的前置条件。对应端到端商品查询仍未通过，不因延期转为成功。
- 发现方：P3 独立保留集；主责：商品工具的领域规划／名称识别；配合：Agent 侧提供调用与结果证据。
- 用户决定（2026-10-19）：本例将 `Keychron K8` 表述为 `Keychron K8 键盘` 是合理补充，不把补上类目词本身认定为编排缺陷。先记录 issue，商品工具后续处理；当前继续聚焦查询循环、程序证据交接、独立回答及编排验证，不为此在 Agent 添加删词或名称匹配补丁。
- 输入：`告诉我Keychron K8在店里的售价，顺便看看我最近那笔订单到哪了。`；合成目录 SPU 11 名称 `Keychron K8`，SKU 101 白色蓝牙 260 元、102 黑色 USB 230 元，分类为键盘。
- 实际：新链路将商品查询表达为 `Keychron K8 键盘`，正式规划器输出 `name="Keychron K8 键盘"`，`CatalogNameMatcher` 将不在商品标题中的“键盘”保留为名称条件，返回 not_found。单订单响应修复后 3/3 重现；相同数据旧链路 3/3 能查到 K8。新回答如实说明本次未命中，订单部分正常；事实忠于工具不等于商品需求完成。
- 证据：源码 [名称规则](../../backend/app/tools/catalog_search/rules.py)保留未知名称词以避免扩大查询；fixture [商品数据](../../evals/support/goal_tool_fixtures.py)；[比较入口](../../evals/agent/compare_runtimes.py) `--live --runtime strands --case holdout_mixed --repeat 3`；前条 6 次复测产物含输入、计划、结果及回答。原 named_product 可命中，不是缺少商品或网络错误。
- 2026-10-20 编排续作复现：最终固定版本 named_product 3/3、holdout_mixed 2/3 未命中已有 K8，共 5 次商品目标失败；其余目标（如同轮订单）照常交接。证据为 `20261019T162352Z-comparison-1e9a8036.json`，18 例成本集合完整保留这些记录。原 named_product 曾能命中，不代表后续表达稳定；用户确认的领域归属与延期决定不变。
- 首次合批后复验：`20261019T165711Z-comparison-395d6d60.json` 中 named_product 第 2／3 次均以 `name="Keychron K8 键盘"` 返回 not_found，共 2 次；holdout_mixed 3 次正常命中。领域规则及 fixture 未改，失败频率变化不代表缺陷修复；上述失败继续完整计入成本并按业务失败记录。
- P3-11 最新复验：`20261020T064559Z-comparison-65f2ebd8.json` 的 named_product 3/3、holdout_mixed 第 1／3 次均以 `name="Keychron K8 键盘"` 返回 not_found，共 5 次商品目标失败；旧侧这些商品查询均能命中。新回答如实说明未查到，混合请求中的订单部分正常，不能将忠于工具结果当作商品需求已完成。原决定和工具算法未变，本轮没有修复或删除失败例。
- P3A 当前复验：固定同批 108 次中的新侧 holdout_mixed 第 2／3 次仍因 `name="Keychron K8 键盘"` 漏检；named_product 3 次命中。证据为 `20261020T082844Z-comparison-cbc49c5c.json`，漏检与全部用量保留；频率变化不是工具修复。
- 后续工具任务：处理型号与类目词的语义分工及名称匹配容错，避免合理类目词导致已有商品漏检。是否采用宽松匹配及其边界留给该工具任务确定；本次没有引入新匹配模式。真实型号／版本限定词、硬条件和 scope 仍须保留，不能把容错变成任意扩大查询。
- 关闭条件：后续商品工具任务在根 business 增加独立型号／类目／限定词回归，并重跑此失败例；已有商品正常命中，未知型号、不同版本与真实限定条件不被混淆。当前保留原始失败记录和预期，不修改 fixture 掩盖问题、不删除验收用例。

## AL-005：人工请求确认展示与实际提交的订单编号不一致

- 类型／状态：P3 消费方接入缺陷；代码已修复、隔离事件契约通过；用户已按 AL-002 的页面清单反馈正常。
- 发现方：P3 程序人工动作交接复核；主责：前端人工请求事件处理；配合：Agent suggested_actions、人工请求 API。
- 触发：用户仅咨询订单 7001 怎样找人工，不查询订单卡片；`suggested_actions.payload.orderId=7001`，确认区显示 7001，但 `handleRequestHandoff` 原来只读取 `order?.id`。没有订单卡片时提交漏编号，有旧 8002 卡片时会提交错误编号；显示确认与实际请求不一致，不能靠后端 owner 校验修正用户选错的本人订单。
- 修复：[App.tsx](../../frontend/src/App.tsx) 提交及成功回显统一采用确认区已有的 `handoffNotice.orderId`，没有该值才沿用当前订单卡片；仍需用户显式提交，未增加自动记录动作。
- 证据：[根 tests 前端契约](../../tests/agent_loop/contracts/frontend_execution.mjs) 提取执行实际 TypeScript 事件函数，分别覆盖无订单卡片、旧 8002 卡片：点击建议动作只展示确认，API 调用为零；确认后提交 7001，成功回显保留 7001，重复确认不重复提交。通过现有 pytest 契约入口运行；前次根 tests 213 passed（3.96s），本次随 243 passed（4.03s）复核通过，均包括实际 TypeScript 类型检查；本次未再改该前端实现，也不是浏览器自动化。
- 页面证据：用户在人工确认／提交一致性等页面清单后反馈目前正常；本条消费方修复结合隔离事件契约按验收完成记录。本次未另查数据库或声称复测了所有边界输入；提供方未生成关联值的 AL-008 单独保持开放。

## AL-006：Strands 在 uvloop 下混用时钟导致请求立即 504

- 类型／状态：P3 runtime 缺陷；已复现并修复，隔离及真实模型＋uvloop 定向回归通过，用户基础页面回验及落库审计已通过，本条立即 504 缺陷关闭；AL-002 其余页面验收开放，不算 P3 放行。
- 发现方：用户在前端发送“你好，你能做什么”的 HTTP 504 截图；主责：Agent 预算／生命周期；配合：正式 API、工具准入与服务启动环境。
- 根因：LoopBudget 原以 time.monotonic 创建 deadline，却传入要求当前 loop.time 的 asyncio.timeout_at；本机 macOS uvloop 两者起点相差约 2,539,103 秒，导致 prepare_turn 首次让出执行时立即超时。只读数据库未见新运行，与准备阶段未完成相符；不是已证明的供应商响应慢。默认 asyncio 的起点相同，原隔离测试漏掉环境差异。
- 修复：[LoopBudget](../../backend/app/core/loop_budget.py) 在运行中的事件循环内创建并统一使用 loop.time；[runtime](../../backend/app/agent/loop/runtime.py) 修正准备阶段耗时；[工具适配器](../../backend/app/agent/loop/tool_adapter.py) 的 before_attempt 绝对 deadline／过期检查也使用当前事件循环时钟。没有扩大预算、关闭 uvloop、修改领域工具或数据库。
- 证据：[服务循环冒烟](../../tests/agent_loop/smoke/test_server_event_loop.py) 的问候／商品路由在默认 asyncio、真实 uvloop 和正负偏移时钟运行：修复前 4 失败／4 通过，失败均为 504；修复后 8/8。新增[时钟契约](../../tests/agent_loop/contracts/test_event_loop_deadlines.py) 16 项验证 HTTP 限时、真实到期、工具取消和保存失败／取消后的清理。最终 `make agent-loop-test` **267 passed（4.63s，23 冒烟／51 业务／193 契约，0 失败／跳过／排除）**，Ruff 通过；只运行根 tests。
- 真实模型复验：根 [比较入口](../../evals/agent/compare_runtimes.py) 使用 `--live --runtime strands --event-loop uvloop --repeat 1 --case server_greeting --case recent_orders --case missing_object`；产物 `20261020T053326Z-comparison-00c7a95d.json` 记录实际 uvloop，3/3 completed／model，无异常／unknown usage。问候 2 请求／2.696s、订单 5／5.813s、无匹配商品 3／3.987s，内容与合成事实一致。使用合成数据，不是本轮真实数据库或浏览器通过，完整成本对照未重跑。
- 页面回验（2026-10-20）：用户新会话 942 的运行 1462—1464 均为 Strands agent_loop、completed／model，问候／最近订单／商品库存分别 2／5／6 次实际请求，全部 HTTP 200、已持久化，无控制拒绝；时间为北京时间 13:55—13:57。结合用户页面试用反馈，基础聊天立即 504 已消除，本缺陷关闭；未证明来源、旧历史、人工请求等全部页面操作。数据库读取显式只读，未写数据或重新执行模型测试。
- 最小接入／关闭条件：请求预算必须在当前运行循环内创建；所有传给 timeout_at／invoke_before／before_attempt 的绝对时间均为同一循环的 loop.time，time.monotonic 只可用于自身时间差。加载修复后的 Strands 服务，用户页面问候及一次实际查询应正常返回、持久化和 agent_loop 审计可核对，再完成 AL-002 对应页面回验；不通过提高超时或改回 legacy 遮蔽问题。

## AL-007：独立回答未接入旧链路的客服表达规则

- 类型／状态：回答阶段迁移遗漏，代码已修复、限定合成样本复核通过；用户已反馈页面体验正常，本次迁移遗漏按修复验收完成记录；少量冗余观察保留，不保证任意表达，也不作为 P3 整体放行。
- 发现方：用户对比前端新旧回答；主责：Agent 独立回答；配合：根 tests 业务效果复核与页面验收。
- 实际：旧 [response.py](../../backend/app/agent/prompts/response.py) 已定义“小帕瓦”、日常客服用语、隐藏内部编号，旧 observation 有先结论、短列表／表格等规则；新 [回答 Prompt](../../backend/app/agent/loop/prompts/independent_answer.py) 原先只有事实、动作和 JSON 约束。相同合成证据下原回答自称 PC 外设商城客服、展示 SKU 编号，并附“本轮没有业务操作”；前端使用同一正文渲染器。
- 修复：新旧路径共享 CUSTOMER_RESPONSE_POLICY 和 ANSWER_ORGANIZATION；新回答复用旧商品／检索事实规则，按当前 AnswerInput 保留目标覆盖和引用。针对部分结果范围、人工确认与 JSON 漏封装修复；不搬入旧控制动作或恢复流程，不增加模型审稿或关键词改写。旧四种完整 Prompt 核对字节不变。
- 证据：[展示冒烟](../../tests/agent_loop/smoke/test_answer_presentation.py) 3 项新增；根 `make agent-loop-test` 270 passed（4.69s，26 冒烟／51 业务／193 契约），Ruff 通过。真实模型 [回答复核入口](../../evals/agent/review_answer_style.py) 与 [独立预期／合成证据](../../evals/agent/answer_style_cases.py) 最终 14 场景各 3 次，42/42 JSON 合法、内容复核通过、每次 1 请求；产物 `20261020T062306Z-answer-style-5f8933de.json`。完整 runtime 补 4 次均 completed／model、回答各 1 请求，产物 `20261020T062428Z-comparison-5e7d9076.json`。不连接业务数据库，不是页面验收。
- 失败保留：中间批次出现全型号范围扩大、人工暗示跟进、JSON 漏封装触发纠错；已修正后重新执行，不把中间 model 返回等同于语义通过。最终样本仍有订单附早先轨迹、身份多介绍能力；完整链路能力问候会受 goal.query 限制清单影响而附无关操作限制。未虚构业务完成，但表达简洁性并非百分之百服从。
- 成本：4 个同证据场景各 3 次，修改前／最终回答各 12 请求；平均输入 993.50 → 3165.50 token，输出 109.42 → 74.58。恢复规则增加输入，未证明整链路成本下降，完整门槛归 AL-003；原 108 次结果是历史版本。
- 最小接入／关闭条件：新回答只读证据、所有目标都回应，业务卡片／动作由程序生成；用户回验身份、简单库存、全部版本、订单及人工引导的可读性，不能为了简短丢范围、冲突或确认。异常 fallback 的表达本轮未改，需与正常模型路径区分；剩余冗余以实际用户表达继续复核，不引入关键词补丁。

## AL-008：人工入口未从短订单号和同义表达中带入关联信息

- 类型／状态：既有人工动作构建缺口，开放；本次回答 Prompt 任务只记录，不修改标识或业务口径。
- 发现方：回答改造后的完整链路复验；主责：Agent 人工动作程序投影；配合：订单标识契约、前端确认与人工请求 API。
- 输入／实际：`订单7001想退钱，请告诉我怎样找人工确认，不要替我提交。`，正文正常说明人工确认，实际 suggested_actions 的 payload 为 `orderId=null, requestType=other`。未点击或提交，不能据此认定已产生错误数据库记录。
- 根因与证据：[result_projection.py](../../backend/app/agent/result_projection.py) 的 _handoff_payload 复用 [intent.py](../../backend/app/agent/intent.py) 的 extract_order_id，只匹配 8 位以上独立数字；request_type 的现有确定性分支未识别“退钱”。根 [完整链路复核](../../evals/agent/compare_runtimes.py) `--live --runtime strands --event-loop uvloop --repeat 1 --case holdout_human`，产物 `20261020T062428Z-comparison-5e7d9076.json` 保留实际响应。
- 与 AL-005 区别：AL-005 修复已有 orderId 从确认区到提交的一致性；本条是提供方最初未生成关联值。后者未完成不能推翻前者的隔离事件验证，也不能凭前者通过认定整个入口已正确。
- P3-11 源码核对（2026-10-20）：当前 [OrderInfo](../../backend/app/models/commerce.py)、[OrderCard](../../backend/app/schemas/order.py)、[订单 API](../../backend/app/api/routers/orders.py) 和 [人工请求 repository](../../backend/app/repositories/support.py) 均使用 `order_info.id`，没有独立公开 `order_no` 字段；前次“内部 ID／公开单号”是待查疑问，不能继续写成当前存在两套号码。正式工具已有 [explicit_order_ids](../../backend/app/tools/order_identity.py) 可识别“订单7001”等带标签号码，人工入口仍复用旧 intent 提取器，属于调用规则未复用。人工请求 API 已校验会话和所关联订单的归属；缺关联不是已证明的越权或自动提交。
- 消费方影响：前端 [App.tsx](../../frontend/src/App.tsx) 的人工动作处理只读取 orderId，提交类型来自用户表单 ticketType（默认 return），并不读取 payload.requestType；因此 `other` 是建议动作分类不准确，不能据此宣称数据库实际存入了 other。短号导致的关联遗漏仍独立成立；无关联时页面可能显示无订单，实际确认／提交场景应单独验证。
- 建议范围：后续在人工动作构建处复用已有明确订单标签识别，单个无歧义号码才作为待确认关联；多号码／无可靠标签不猜测，服务端 owner 校验及用户确认不变。请求类型需要明确是否用于表单预填，再同时核对提供方和消费方，不能只加“退钱”关键词就认定整条链路修好。本次核对未实现此方案，也未将用户页面总体正常视为本特定缺陷延期批准。
- P3-11 最新复验：`20261020T064559Z-comparison-65f2ebd8.json` 中 holdout_human 3/3 的正文要求用户确认且没有提交声明，但程序动作仍为 `orderId=null, requestType=other`。真实模型回归未触发提交 API，不宣称数据库已有错单；P3-10 手工总体正常不改变本条失败。应在P3-11核对修复及验证（原计划写作P4切换前，现切换顺延为P5），或者由用户明确决定延期，本轮未自行批准。
- P3A 当前复验：`20261020T082844Z-comparison-cbc49c5c.json` 中人工引导 3/3 仍 `orderId=null, requestType=other`，未调用提交 API；本轮未修复此项，不视为延期。
- 后续／关闭条件：依据已核实的 order.id 契约对齐文本识别及确认展示，在根 tests 补短号、长号、紧邻中文、多号码、含价格／电话歧义和不同请求类型的独立测试。不能无条件把文本中数字写成内部订单 ID，身份／归属及显式确认保持；无可靠关联时清楚展示未关联，不猜订单或自动提交。

## AL-009：属性词重复进入商品标题关键词，遗漏替代候选

- 类型／状态：P3A 复用 P3 场景发现的领域规划质量问题，开放；用户随后明确“先不管工具侧
  问题”，本轮暂缓工具修复、继续验证框架；失败仍计账，不等于业务验收通过。
  严格预算无匹配的结论正确，问题是可提供的替代候选被遗漏。
- 发现方：P3A 真实模型对照；主责：商品工具规划／语义复核；配合：Agent 查询表达与评测。
- 输入及版本：`alternative_budget` 的白色、蓝牙、100 元内键盘需求，分支
  `refactor/agent-loop`、HEAD `1051e5c` 加本轮未提交 P3A，deepseek-flash、温度 0、
  合成商品数据。未修改正式工具、fixture 或冻结场景。
- 实际：第一次新链路把 `白色 蓝牙键盘 价格100元以内` 交给 catalog_search，领域计划
  已有 color=白色、connection_type=蓝牙、price≤100，却又生成 `keywords=["蓝牙键盘"]`；
  正式服务在字段匹配前应用标题关键词，候选池变成 0，最终 no_match、没有替代商品。
  第二次同场景 keywords=[]，返回 4 个超预算替代候选（199／260／299／599 元）。
  回答均忠于工具结果，没有将超预算说成满足预算；不能把正常结束当作候选覆盖一致。
- 可获取依据：[正式服务](../../backend/app/tools/catalog_search/service.py) 的关键词预过滤、
  [标题匹配](../../backend/app/tools/catalog_search/rules.py)、
  [规划校验](../../backend/app/tools/catalog_search/validation.py)；
  现有复现入口 [compare_runtimes.py](../../evals/agent/compare_runtimes.py)
  `--live --runtime strands --event-loop uvloop --repeat 3 --case alternative_budget`。
  原始产物 `artifacts/evals/agent_loop/20261020T082844Z-comparison-cbc49c5c.json`
  保留实际参数、计划、结果、回答与用量；不保证概率性模型每次复现。
- 与 AL-004 区分：AL-004 是型号名中额外类目词导致指定商品漏检；本条是属性查询中的
  规格词又进入 keywords，额外收窄候选池。最初未直接套用 AL-004 的延期批准；
  本轮暂缓依据为用户后来对工具侧问题的明确范围决定。
- 关闭条件：工具侧核对自然语言属性与标题词的分工，避免重复加入无依据标题约束；
  以独立商品表达验证严格预算不放宽、真实型号／关键词限定仍保留、替代候选明确超预算。
  当前已获准新增框架测试；本条继续保留发现及证据，不在编排加删词补丁。

## AL-010：同批执行触及工具预算后 SDK 仍继续请求模型

- 类型/状态：P3A 新增契约测试发现，已修复并通过隔离回归。
- 发现方：P3A-07；主责：查询 loop 与 SDK 绑定；配合：预算／证据交接。
- 版本与触发：`refactor/agent-loop`，HEAD `1051e5c` 加 P3A 工作区；首轮登记
  商品和订单目标并同批查询，工具预算设为 1。商品调用成功后，订单执行前抛出
  `LoopLimit("tool_attempt_budget")`，SDK 将异常变成普通工具错误并继续请求模型；
  原因可能最终被误记为 query_model_error，而不是实际预算终止。
- 预期：预算耗尽立即停止查询，保留商品成功证据；未执行订单不生成失败事实，
  订单目标 blocked，转入独立回答，不多发查询模型请求。
- 修复：[SDK 绑定](../../backend/app/agent/loop/strands_tools.py)保留预算停止原因，
  后续同批调用拒绝执行；[查询 loop](../../backend/app/agent/loop/query_loop.py)的
  AfterTools hook 读到预算终止后结束，不依赖 SDK 向外传播普通异常。
- 证据：[新增批次契约](../../tests/agent_loop/contracts/test_query_batches.py)
  `test_tool_budget_between_goals_hands_off_only_actual_evidence` 参数化预算 0／1，
  核对 1 次查询＋1 次回答、真实证据数、目标状态和停止原因。根 tests 340 项通过；
  真实模型成本统计仍使用默认预算，没有为改善成本降低正常预算。
- 关闭范围：本条 SDK 工具准入预算终止行为已验证；不代表其他阶段出口或真实数据库验收。

## AL-011：独立回答缺少已解析历史指代的程序对应关系

- 类型/状态：P3A 真实模型验证发现，程序交接缺口已修复，限定表达复验通过；
  单目标多引用的独立问题另见 AL-013，不因此关闭复杂指代的全部验收。
- 发现方：P3A-11；主责：程序证据交接／独立回答；配合：工作记忆与引用解析。
- 触发：合成记忆的候选顺序为 `[101, 201]`，问“刚才列表里的第二款现在还有货吗？
  另外帮我查查拆封了还能不能退。”查询阶段按 `catalog_candidate/position=2` 正确解析
  为 SKU201，实际查到库存12。回答输入含 reference 种类和位置，但缺少程序已解析的
  201 对应关系；模型生成的 goal.query 还可能写错为 SKU101。
- 实际：`20261020T092956Z-comparison-501c0b90.json` 的
  `p3a_reference_and_policy` 第2次回答先说“没有可用的上一轮列表记录，无法确认”，
  随后又给出本次查到的 K380，再要求用户确认。没有错查订单或篡改工具结果，
  但丢失了已经确认的指代关系，增加无必要澄清。
- 预期／修复方向：回答端收到从 PreparedTurn 工作记忆经正式 resolve_reference 解析的
  可信身份映射，优先于模型目标描述；不把历史动态事实当当前事实，不补猜型号或编号。
  源码：[引用解析](../../backend/app/agent/loop/query_context.py)、
  [程序交接](../../backend/app/agent/loop/independent_answer.py)；
  复现入口：[根模型驱动](../../evals/agent/compare_runtimes.py)
  `--live --runtime strands --repeat 3 --case p3a_reference_and_policy`。
- 关闭条件：隔离测试验证可信引用随目标交接、第二轮重新查询，并以真实表达复验
  不再因交接遗漏而要求用户重复确认；引用不合法仍在查询登记前拒绝。
- 修复与证据：handoff 为有 reference 的目标加入程序解析的 trusted_reference，
  不拷贝历史价格／库存；回答规则说明其优先于模型 query 中自行填写的编号。
  [多轮业务测试](../../tests/agent_loop/business/test_query_batches.py)核对第二轮交接
  身份及重新查询；根 tests 340 passed（4.83s）。最终新表达产物
  `20261020T094646Z-comparison-345de1af.json` 中 reference_and_policy 3/3、
  new_reference_dependency 3/3 正确对应201并使用本次查询，无缺历史正文导致的无必要澄清。

## AL-012：同目标先空后有材料，回答仍把可答内容整体否定

- 类型/状态：P3A 新增真实表达发现的回答证据整合缺陷；已加强回答规则，
  固定复现材料 3/3 验收通过，限定样本关闭，不等于回答质量整体通过。
- 发现方：P3A-11；主责：独立回答；配合：查询目标状态、检索与内容复核。
- 触发及证据：`20261020T092956Z-comparison-501c0b90.json` 中
  `p3a_three_domains` 第2次，问“本店退货对配件有什么要求”。同一政策目标先查空，
  再查得文档8001，明确“商品完好、配件齐全且不影响二次销售”，但回答说
  “没有查到……针对配件、线材、说明书或包装的具体规定，因此无法确认这部分要求”，
  漏答已有的“配件齐全”。商品及订单独立诉求仍有回答。
- 当前行为：两个不同查询 operation 的真实证据均保留，GoalBook 对部分未解决结果
  保守标记 blocked；不能为消除 blocked 随意把“任一查询成功”改成“全部诉求满足”。
  回答应先说明已知配件齐全，再区分没有更具体清单；查询状态不是事实的替代品。
- 已做修正：[独立回答规则](../../backend/app/agent/loop/prompts/independent_answer.py)
  明确先空／失败不否定其他调用的有效材料，目标 blocked 仍须回答有证据部分。
  没有新增模型复核调用、关键词删改或更改工具检索算法。
- 最小复现入口：[根 compare_runtimes.py](../../evals/agent/compare_runtimes.py)
  `--live --runtime strands --repeat 3 --case p3a_three_domains`；
  [冻结表达与预期](../../evals/agent/query_batch_cases.py)独立于模型输出。
  概率性运行不保证出现相同检索顺序；关闭须补足同样“先空后有”的内容复验，
  不以本轮未复现视为根因消失。
- 定向复验：在根 [固定交接 fixture](../../evals/agent/answer_style_cases.py)
  增加 `partial_policy_after_empty`，同一 g1 两个不同 operation，一个空、一个含文档8001，
  目标仍 blocked。复用 [回答驱动](../../evals/agent/review_answer_style.py)
  `--live --repeat 3 --case partial_policy_after_empty`，产物
  `20261020T095130Z-answer-style-37bbb2be.json`；3/3 引用配件齐全及键盘政策条件，
  同时区分没有更细配件清单，均1次回答请求、无修复／降级。不是完整 runtime 或数据库验收。

## AL-013：一个任务关联多个历史商品

- 类型/状态：已修复并完成本问题范围的隔离／真实模型复验；不等于P3A整体放行。
  用户已确认多对象方案，未放松身份与来源检查。
- 发现方：P3A-11；主责：目标／引用契约与查询上下文；配合：证据交接、工作记忆。
- 输入：可信历史候选顺序 `[201,101]`，问“把刚才第一款和第二款的现价分开列给我。
  另外说说，我能不能让你直接替我付钱？”预期分别重查201／101，付款只说明不能代办。
- 实际：最终 `20261020T094646Z-comparison-345de1af.json` 中
  `p3a_new_two_references` 第1次拆成两个查询目标，各带对应 reference，正确返回199／260。
  第2／3次把两个价格放在同一个比较目标，但 QueryGoal.reference 只能提供第一款201；
  同目标查询第二款101被 `unsourced_product_identity` 拒绝，整批零执行，
  随后模型以 blocked 完成，两个价格都未提供。安全拒绝正确，任务表达能力不足。
- 可获取依据：[目标 schema](../../backend/app/agent/loop/query_schema.py)、
  [引用校验](../../backend/app/agent/loop/query_context.py)、
  [冻结表达](../../evals/agent/query_batch_cases.py)。复现根 compare_runtimes.py
  `--live --runtime strands --repeat 3 --case p3a_new_two_references`。
- 已实施（2026-10-20）：`QueryGoal.references` 为对象列表，替代本次开发中的单数
  `reference`，不保留两套模型输入字段。`CandidateReference` 必填整数位置1—20；
  `SelectedReference` 不接受位置字段。Pydantic 按 kind 区分两种结构，严格类型、
  禁止额外字段；模型负责理解用户选了哪些对象，程序仅按位置读取实际列表并核对编号来源。
  同一任务可查询所有明确关联的对象，未关联历史编号仍拒绝。
- 同步处理：登记返回和回答输入保留每项位置与实际编号；多商品比较不把最后成功的一款
  设成唯一选中商品，包括另一款查询失败的情况；现有卡片展示顺序和后续追问继续使用。
  登记、结束的独立调用与首次合批共用 Pydantic 外层定义，模型看到的 schema 也从
  同一模型生成；正式业务工具参数继续复用 contracts，不新增语义检查或模型复核调用。
- 隔离证据：[多对象业务测试](../../tests/agent_loop/business/test_multiple_references.py)、
  [结构校验测试](../../tests/agent_loop/contracts/test_query_schema.py)。本轮新增55项，
  根 tests 三类合计395 passed（5.59s），Ruff通过；没有数据库或浏览器验收。
- 真实模型证据：`artifacts/evals/agent_loop/20261020T111109Z-comparison-052acea5.json`，
  5例×3次，9次旧表达回归＋6次新表达，202文件指纹全部一致。
  原两商品失败表达3/3保持一个比较任务并返回199／260；新增三选二3/3仅查第1／3款，
  新增历史订单3/3正确区分7001已发货／7999无记录，其中2次一个任务关联两笔。
  15次均正常结束并由模型回答，0结构／编号来源拒绝，0异常／未知usage。
  原表达前两次分别遇到工具规划失败、工具要求拆分查询，补查取得结果，但各发生一次
  结束结果编号不一致而纠正；原始首次提交未保存，不能确定具体漏填或误填了哪项。
  失败调用保留，当时任务状态仍failed，详见AL-015。
  该局部集合共107模型请求，不是原18例成本重测，不据此宣称新成本收益。
- 关闭条件：同一目标两历史对象与多个目标两对象均可正确重查，绑定／证据／显示顺序不串，
  未指明的历史对象仍拒绝；新表达重复验收，不仅脚本化两个目标通过。

## AL-014：查询顺序的判断及程序职责已对齐

- 类型/状态：用户已对齐判断标准；撤销“新增程序语义检查”的拟议方案。
  不再仅凭“先、再”表达或未分成多个批次认定框架失败。自然语言理解效果仍需模型评测。
- 发现方：P3A-11；主责：目标登记与原始诉求语义覆盖；配合：准入、模型评测和成本。
- 触发：问“先找店里的罗技 K380，确认找到了，再查一下本店键盘拆封后的退货条件”。
  最终 `20261020T094646Z-comparison-345de1af.json` 的 `p3a_found_dependency`
  第3次将两个目标的 prerequisites 都登记为空，首次同批执行商品查询和政策检索；
  商品返回 not_found，政策仍被查询并回答。第1／2次保留依赖并正确 blocked。
- 历史结论修正：此前把该表达直接判为必须等待，并据此要求检查“漏登记”，判断过度。
  用户确认商品查询和通用政策通常可独立执行；程序不理解先后措辞，也不判断模型选择的
  工具是否符合用户意思。不能把 source_text 的原文匹配或字段校验当作语义正确的证明。
- 可获取依据：[目标登记与准入](../../backend/app/agent/loop/query_state.py)、
  [查询循环](../../backend/app/agent/loop/query_loop.py)、
  [冻结表达](../../evals/agent/query_batch_cases.py)；
  根 compare_runtimes.py `--live --runtime strands --repeat 3 --case p3a_found_dependency`。
- 已确认并落实：模型判断真正需要上游结果或用户明确设定的条件，程序只检查模型提交的
  结构、已声明任务关系、实际结果和硬规则。没有增加关键词推断、工具语义匹配或额外
  模型复核；Prompt 与两条表达的评审标准同步调整，历史产物保持不变。
  已声明依赖、无匹配和失败分支的原隔离测试保留；新增独立查询逆序同批执行测试。
- 验收边界：本项不再以“程序缺少原文语义检查”阻止出口。明确条件是否被模型理解仍是
  业务效果评测内容，不能因为结构通过便宣称所有用户条件都被保留；不新增程序语义守卫。

## AL-015：结束时重复提交记录造成额外往返，历史失败影响整个任务状态

- 类型/状态：按用户确认方案修复，并完成本问题范围的隔离及真实模型复验。
  不影响AL-013两对象来源修复结论，不等于P3A整体放行。
- 发现方：AL-013真实模型复验；主责：查询结束和状态展示；配合：工具及效果评测。
- 证据：上方 `20261020T111109Z-comparison-052acea5.json` 的
  `p3a_new_two_references` 第1／2次。第1次商品工具返回 `planning_output_invalid`；
  第2次一个工具请求同时问两对象，返回 `split_required`。模型随后分别补查得到两款价格，
  第一次结束提交以 `completion_evidence_mismatch` 拒绝，模型修正后结束并正确回答。
  **此前“确定遗漏失败记录”的归因过度，现已更正：**产物未保存首次提交的完整参数，
  只能证明编号集合或重复检查未通过，不能确认遗漏、错填、重复中的具体原因。
- 历史规则要求模型列出全部结果编号和程序计算的状态；不同参数补查是新调用，
  因此失败历史导致整个任务仍显示failed。这不是完整请求是否满足的可靠判断。
- 已实施：[结束schema](../../backend/app/agent/loop/query_schema.py)仅保留`completion.reason`；
  [GoalBook](../../backend/app/agent/loop/query_state.py)只检查已登记和生命周期，自动交接
  全部任务、当前结果编号及实际执行计数，不再计算整个任务的handled／blocked／failed。
  `execution`中的attempt_count含全部尝试，ok_count/error_count仅统计工具ok标志，
  active_result_count为当前有效记录数量；如split_required的领域结果仍在每条记录中。
  这些数字不是业务完成判断。原失败历史保留，不按自然语言相似度删除或替代。
- 停止不代表成功：模型可以在资料不足、失败或需要补充信息时结束；首次登记也可直接结束。
  未查询任务仍进入回答材料；预算或异常停止仍保留真实原因。`model_stop_reason`仅保存在
  审计，不传入回答模型；回答直接依据原请求、对象对应关系及真实结果，避免复制查询模型
  的错误解释。回答和降级不再因某次失败笼统宣布整个任务失败。
- 硬规则保留：结束与业务查询不能同批、结构错误最多一次修正、身份／来源／预算、
  重复调用／重试和已声明前提检查不变。回答实际使用的编号仍检查存在性和任务覆盖；
  未新增程序语义校验、模型复核或业务写操作。
- 隔离证据：[结束业务测试](../../tests/agent_loop/business/test_query_completion.py)新增5项，
  两类“先失败再补查”均只提交一次结束，保留三条结果；覆盖未查询任务、仅失败时停止及
  停止说明虚构事实时的安全降级。迁移了旧结束协议测试，其中4项过时的逐目标完成检查
  改为2项生命周期／自动交接检查。最终根tests398 passed（5.56s），30 smoke／70 business／
  298 contracts；Ruff通过。没有数据库或浏览器验证。
- 首轮真实模型：`20261020T121726Z-comparison-ac816ea7.json`，7例×3次，共21次；
  全部completed/model、每次仅一次query_complete，0结束拒绝，0异常／未知usage。
  原两商品问题3/3正确，第1次实际发生split_required后分别补查，保留三条记录且直接结束。
  1次非法依赖声明被已有硬规则拒绝后修正；另1次三选二工具规划两次失败，模型停止说明
  将其误归因为缺商品身份，回答重复该错误；AL-001的“相应渠道”建议也复现1次。
  此轮不是全业务通过。130次请求；其中与前轮相同15次子集为113（前轮107），不能宣称降本。
- 针对交接修正：程序已拥有请求、实际身份和结果，不再把模型停止解释交给回答；保留审计。
  固定“身份已知但两次查价失败”复现：`20261020T122307Z-answer-style-f4423501.json`，
  3/3仅说明查价失败，没有虚构价格、否认历史身份或要求重新提供商品；各1次模型调用。
  这是固定材料的回答复验，不是新表达、完整runtime或全量成本成绩。
- 最终runtime复验：`20261020T122354Z-comparison-9a638413.json`，原两商品与三选二各3次。
  6/6 completed/model、每次仅一次query_complete、0拒绝／异常／未知usage；原两商品3/3正确，
  三选二2/3报价成功，另1次两次工具规划失败，仅如实说明查价失败，没有再否认对象身份。
  59次请求；与固定回答复验均为最终源码，203文件指纹全部一致。
  原历史结束编号不一致和整目标failed问题在本范围关闭，工具查询失败及AL-001仍未修复，
  P3A整体出口继续未放行；本轮没有重跑原18例完整成本对照。

<a id="al-016"></a>
## AL-016：本地可视化 Trace（首期已验收，已关闭）

2026-10-22 收尾确认：用户明确表示“目前评测平台我已经验收了”，同意当前追踪粒度作为稳定版本；P4 **10/10**，P4-09页面验收、P4-10阶段出口完成，AL-016关闭。验收来源为用户实际页面确认，不冒称代理浏览器操作通过。

稳定范围为请求、实际模型/工具调用、三类关键程序校验（计划、复核输出、工具输出）、其他程序Events、Metadata摘要、回答实际材料、错误/耗时/Token及基础故障隔离。完整trace由本地Phoenix独立卷保存；artifacts只保留测试/评测报告及必要样本。更细点位后续按用户需要增加，不预建评测体系。

提交前最近实现验证：默认离线测试1409项通过（47.46s），合成trace `3ac69bc2cadc1783e9bd44af68a9baf0` 已由平台API核验。此次收尾仅核对文档和提交检查，没有重跑真实模型或访问商城数据库；此前获授权的真实请求仅为冒烟。P3/P3A质量和成本出口、AL-001/AL-008/AL-018等问题仍保留，P4完成不代表这些业务问题已修复，不自动进入P5。

以下为阶段历史证据，保留当时的失败和未验证项；当前关闭状态以上述用户验收为准。

2026-10-22 三处关键程序校验独立展示：工具查询计划、每次复核输出、工具最终输出契约各有独立CHAIN子节点，标题标明通过/拒绝，拒绝保留错误并关联来源模型。重复尝试逐次保留；复核输出通过仅指报告结构与引用规则通过，模型verdict另列，不代表计划自动获准执行。其他程序判断继续放Events。未修改校验规则、Prompt、重试和业务结果，AL-018仍待处理。

本次验证：相关65项通过；默认离线集合1409项通过（47.46s）。本地Phoenix合成trace `3ac69bc2cadc1783e9bd44af68a9baf0` 已由API确认12节点、6次模拟模型请求、1次工具调用，包含计划通过→复核拒绝→复核通过→输出通过四个程序节点。摘要报告位于 `artifacts/tests/phoenix-demo/validation-report.json`；本次无真实LLM或商城数据库访问。浏览器页面未验收，P4保持8/10。

2026-10-22 Metadata精简视图：按用户授权为现有节点新增限长、脱敏的JSON摘要，不替换OpenInference消息、工具结果或程序事件。用途/关键输入/提交动作/输出概况在模型Metadata，工具Metadata含参数、返回计数、排序窗口、最近拒绝及复核结论；投影摘要明确只表示最近一次投影。每份摘要最多4096字符并显式标记超限；完整导出仍保留原有已采集字段。模型提交结论不等于程序接受。

验证：11项追踪隔离测试通过（5.79s），Ruff/diff检查通过；Phoenix合成trace `c22e785edbdb5771cac3c6a9f80c3a84` 的13节点/9模型/3工具、Metadata/完整消息/程序事件由API核验。首轮校验因API展平metadata字段而误报，修正验证脚本后通过，未改业务。只生成合成演示，无真实LLM或商城数据库访问；页面仍待用户查看Info→Metadata，不宣称浏览器验收通过，P4仍8/10。复核依据格式问题已登记AL-018，复核Prompt/schema/执行逻辑未修改。


2026-10-21 工具层最小观测补充（用户授权）：模型标题统一为“模型 · 任务规划与调度 / 工具查询规划 / 工具计划复核 / 回答生成”，业务工具为“工具 · 工具名”。复核实际审核候选计划，不再称“工具结果复核”。catalog_search补上下文范围、执行计划、定位/筛选计数、排序窗口及返回/提前结束摘要；共用structured_planning补计划校验变化、复核结论、传输重试/输出修正原因及关联模型ID；程序事件放所属工具Events，未增加普通函数节点。query_loop投影补源证据、去向、前后大小、移除字段和商品数量，回答交接复用该记录，不改变16000字符投影行为。

验证：相关专项362通过；最终整个loop与catalog工具集合 **1013 passed（15.23s）**，模型/工具/repository调用数与结果开关一致，包含复核非法路径修正和大材料截断定位，Ruff通过。合成平台trace `ebb7b76c1dd77129c3f06c4991f5dc65`：13节点/9模型/3工具；平台API核验OpenInference消息及程序事件，摘要在artifacts/tests/phoenix-demo/report.json。单组关闭18.969ms/开启94.650ms，非性能基准。本轮无真实模型、无商城数据库访问，不复用上一轮真实请求作为新代码验收；页面验收仍未完成，P4保持8/10。未新增依赖、未改变Prompt/算法/预算/重试策略，legacy原追踪行为保留；共享计划入口只在新追踪scope存在时发事件。


最新正式接入证据：用户授权单次真实请求，经已有demo账号登录及正式 `/api/chat`，使用deepseek-flash；HTTP 200、8.889秒、会话945，查询2/工具规划1/工具复核2/回答1，商品工具1次。Phoenix trace `36a7c5e034477a5b9172c8fbea2133d7` 的8个节点已由平台API核验。请求查询现有商品并保存登录/会话/审计；没有初始化、seed、迁移或修改商品/订单。摘要为忽略产物 `artifacts/tests/manual-backend/report.json`；本条已内含最小证据，不依赖该文件共享。

追踪已支持定位交接缺口：工具原始返回含2款商品；`query_loop.project_record` 的16000字符投影限制移除了商品明细，回答输入 `projection_truncated=true`、无products；模型消息追踪本身未截断。该业务交接限制仍未修复，按既有回答问题AL-001后续核对，不作为Phoenix采集失败，也不据此宣称业务成功。用户此前反馈旧页面JSON难读促成OpenInference优化；尚无新格式页面验收通过反馈，P4仍8/10，AL-016开放。以下真实模型/数据库“未验证”描述为早期批次历史。


2026-10-21 OpenInference 展示优化：模型输入/输出改为标准角色消息、文本内容块及 tool_calls 字段，工具 schema 与调用参数分开；不再重复保存完整模型 JSON 报文。标题含调用用途、全局请求序号和实际返回动作，支持同次目标登记/业务调用混合输出；提案、申请与候选不表示校验通过。每方向 32768 字符/180 内容属性，512 span 属性容量，明确截断/缺口，原有脱敏与未知 Token 规则保留。

本轮定向隔离检查18项通过（追踪8 + runtime选择10）；真实本地平台合成 trace `620cd8a429315c4ee2d851ee086f417c`，13 spans / 9模型 / 3工具，标准消息与类别通过平台 API 核验。两次工具失败后补查成功，开关追踪结果与次数相同；单组观察关闭17.629ms、开启75.975ms，非性能基准。最初容器停止导致导出失败而业务正常；启动原容器后导出成功。Chrome控制权限仍pending，页面验收未完成；未调用真实LLM/访问商城数据库。报告改为 `artifacts/tests/phoenix-demo/report.json`，只存验证摘要。

补充回归：整个 loop 与 runtime 选择共 **416 passed（12.01s）**，Ruff 与 git diff --check 通过。首次扩大回归为405通过/1失败，失败来自旧 smoke 对默认 legacy 的断言；按用户已授权默认 strands 同步后全通过。



> 2026-10-21 用户明确授权默认 runtime 切换为 strands（新 loop），需要时通过 `make backend RUNTIME=legacy` 切回。此为使用默认值调整，不代表 P3/P3A 质量、成本或 P4 页面验收通过；旧问题与 legacy 实现保留。下文原默认 legacy 的记录为历史状态。

- 类型／状态：P4首期已完成，**已关闭**；2026-10-22用户确认页面验收，当前粒度作为稳定版本。
- 发现方：用户在P3A排错时难以查看中间结果；主责：Agent入口/模型追踪基础设施；配合：工具规划、会话关联、测试与本地运行说明。
- 本轮版本：`refactor/agent-loop`，基点`2bccfda6bbf7f3d9c9fb095fe919c1ee42d88638`，未提交工作区。
- 顺序调整：2026-10-21用户授权先实施核心追踪以支持后续排错，不等待P3/P3A出口；AL-001等旧问题不关闭，质量/成本门槛及P5整体条件保留；默认runtime后续按用户另行授权改为strands。
- 最小接入：[loop_tracing.py](../../backend/app/core/loop_tracing.py)使用私有OTel provider；[runtime](../../backend/app/agent/loop/runtime.py)创建请求根记录；[loop_models](../../backend/app/core/loop_models.py)沿实际客户端请求记录查询/工具内部/回答模型；[工具绑定](../../backend/app/agent/loop/strands_tools.py)关联逐次工具尝试；拒绝事件保留原始提交、错误、反馈和模型ID。AnswerInput直接在回答模型请求查看，无请求降级才补材料。
- SDK核对：Strands固定1.61.1，原生模型span汇总适配器重试且可能把未知usage转零，独立TextModel不在原生追踪边界；本期复用已有客户端请求边界，不叠加多套自动采集。仅加OTLP HTTP exporter1.45.0及HTTP transport0.66b0，无无关升级，无Phoenix服务进入后端venv。
- 平台：[Podman脚本](../../scripts/podman-trace.sh)，官方Phoenix20.23.0，容器`pc-ecommerce-agent-phoenix`、专用卷`pc-ecommerce-agent-phoenix-data`，仅`127.0.0.1:6006`，SQLite`/data/phoenix.db`；不使用商城数据库。停止保留数据，清理独立操作。
- 安全/隔离：32768字符快照上限及明确截断，无附件；敏感字段和明确密钥脱敏；不采集HTTP头、SQL、Settings/客户端对象；未知Token不记零、不估价；256条后台队列、1秒发送超时、2秒退出等待，丢失日志可见；不改变业务重试/预算/异常。
- 证据入口：[根tests追踪断言](../../tests/agent_loop/contracts/test_local_trace.py)、[显式合成平台入口](../../tests/trace_demo.py)、[README操作说明](../../README.md#local-phoenix-trace)。默认测试不包含平台连接；平台入口只允许本机6006，模拟模型/内存repository/合成商品，经正式新runtime。
- 平台/API：受控参数拒绝后，两次工具失败、第三次补查成功；13span含9次模型、3次工具及请求根记录。工具内部模型是子记录，答案使用299元成功结果。最终验证证据与计数见下方本轮检查摘要。
- 页面：代理此前未取得浏览器控制权限；2026-10-22用户亲自确认平台已验收，不以API检查替代该确认。
- 本轮未调用真实LLM、未连接商城业务数据库，未真实聊天/收费评测，未提交/推送。平台存储初始化仅限独立卷。

- [x] **P4-01** 核对当前入口、SDK与最少依赖；按用户授权提前实施核心追踪，保留P3/P3A出口。
- [x] **P4-02** 官方锁定Phoenix镜像、独立Podman容器/卷、本机端口、Make入口和保留/清理说明。
- [x] **P4-03** 正式runtime请求根记录，输入/答案、会话/run、HEAD/dirty、时间/状态/停止原因与日志trace ID。
- [x] **P4-04** 每次查询模型实际请求；控制提交通过模型输出查看，拒绝事件保留原始参数、错误、反馈及模型关联。
- [x] **P4-05** 工具实际规范化输入/原始结果，独立保留失败/补查及关联；工具内部规划/复核模型为子步骤，不记录普通内部函数或SQL。
- [x] **P4-06** 回答实际材料直接查看独立回答模型输入；原始输出、修正、降级和最终答案保留，不复制正常AnswerInput，不改变停止说明边界。
- [x] **P4-07** 脱敏、即时快照、明确截断、未知Token、有界队列/导出/退出及丢失日志；故障不改变业务。
- [x] **P4-08** 根tests离线断言与正式runtime合成故障演示完成；最新loop与runtime回归416项通过。另已补充一次真实模型/商城数据库的正式HTTP冒烟，不替代完整业务验收。
- [x] **P4-09** 平台API、开关一致和故障隔离验证完成；2026-10-22用户确认Phoenix页面验收通过。
- [x] **P4-10｜阶段出口** 用户接受当前追踪粒度作为稳定版本，P4完成、AL-016关闭；后续按需扩展，P3/P3A质量/成本出口与工具问题独立跟踪。

本轮检查摘要：默认离线集合 **1403 passed（41.67s）**，0失败/跳过；新增7项追踪契约测试；随后非完整JSON凭据脱敏补充的7项复验通过（3.63s），Ruff/shell语法/本地文档链接/diff检查通过。
实际平台trace `6b109bdb778d7483bf1930a0bca0d9f7` 共13span，9模型/3工具，三次结果False/False/True，
三个内部模型子span及拒绝/回答材料已由平台API读回；stop/start后仍完整。
SDK预热后的单次模拟开关对照17.553ms/71.868ms，增加54.315ms，约240861字节span JSON；不是性能或模型成本基准。
平台停止对照仍相同答案、9模型/3工具，明确提示13span丢失；演示验收入口非零退出，业务runtime正常成功，平台随后恢复。
早期镜像网络超时通过本机现有系统代理下载官方归档解决，校验后导入并删除所有下载临时文件；
演示项目归属、合成fixture和Podman模板字段错误均已修正并复验；保留少量早期合成记录，不充当最终页面验收。
没有运行legacy/integration/真实LLM/商城数据库/聊天或前端构建；前端未修改。页面控制未授权，AL-016不关闭。

后续按需增加而非本期完成项：文件/Prompt指纹、内部函数和SQL/全量行、细粒度状态快照、重复交接快照、附件、指标告警、自动评分/评测数据集/Prompt管理/回放。


历史06c84eb提交前重新运行根tests：398 passed（5.80s），无失败／跳过；测试Ruff、backend目录下的变更Python文件Ruff、前端TypeScript／Vite构建通过。本轮没有运行真实模型、数据库或浏览器，完整成本仍未重测。最初从根目录检查backend脚本触发一项导入排序提示，按仓库backend工作目录执行后通过，未为目录识别差异改写代码。

本次整理保留全部有效回归用例与被报告引用的成功／失败产物；完整JSON继续位于忽略的评测目录，不纳入Git。未因提交代码而关闭P3A-12、P3-11或工具／回答遗留问题。


## AL-017：P4前目录、测试和评测整理

- 类型／状态：结构整理已完成，隔离验证通过；不替代P3/P3A整体放行。
- 发现方：用户目录审计要求；主责：Agent入口与测试工程；配合：工具/应用回归、评测驱动及文档维护。
- 整理前版本：`refactor/agent-loop` / `06c84eb3463c5d32f068bb37ef1465b2c5a575cc`；已跟踪工作区干净。
- 最小接入要求：API通过[factory](../../backend/app/agent/factory.py)选择一套runtime；loop与legacy互不导入。
  共享分类在boundary、工具结果规则在tool_outcomes；所有调用方使用新路径，不保留兼容转发入口。
  `make backend RUNTIME=strands|legacy`只覆盖进程配置；不传保持原优先级与legacy默认。
  当前测试通过`tests/run.py`阻断网络及真实数据库，禁止读取项目真实.env；legacy显式执行，integration/模型评测不默认收集。
- 实际证据：默认集合1396 passed、显式legacy833 passed，0失败/跳过；原2220项保留，新加9项入口/隔离检查。
  首轮1项包级导入、4项拆分/patch失败均为迁移错误，修复后复验；Ruff及diff检查通过。
  59个历史证据文件与原Git字节相同；完整逐文件迁移清单随本地历史ZIP封存，必要摘要和恢复方式见[README](../../README.md#history-archive)。
- 收尾隔离环境问题：收尾强化Python子进程保护后，系统Python缺少隔离插件依赖导致3项shell替身检查被阻断；改用已有后端虚拟环境运行假命令，保持保护并将测试进程超时提高到60秒以容纳保护启动开销，未修改被测脚本的重试/超时策略或安装依赖。
- 限制与关闭条件：本条仅关闭结构迁移及离线接入，未调用真实模型、未连接真实数据库、未浏览器聊天或联网验证。
  模型质量/成本与真实端到端仍按AL-001等既有问题和P3/P3A出口验收。P4接入点见AL-016，未安装或接入Phoenix。
- P5调整：保留可运行旧LangGraph和Make切换，默认新链路切换须满足原门槛；退出旧链路默认加载与测试，
  清理无用模块、重复规则、临时兼容层。legacy使用当前工具；完整冻结基线仍依赖Git/快照。


2026-10-21 用户授权历史资料外置：原 archive/ 与 local-backups/ 全部封存到本地
`pc-ecommerce-agent-history-20261021.zip`，70 个原文件逐一 SHA256 校验及 ZIP 完整性检查通过后删除原目录。
ZIP 待用户手动移出项目且由 Git 忽略；README 保留迁移与恢复摘要，当前 issues 不依赖外部档案才能理解。
本次仅检查文件完整性和文档链接，没有重跑业务测试或调用真实模型/数据库；既有阶段状态不变。

2026-10-21 补充收尾：遗漏的 docs/archive、商品评测旧辅助脚本、生成示例、退役输入及原 README
已封存至补充历史 ZIP，22 个原文件通过 SHA256/ZIP 完整性校验后移除原历史目录。
旧测试启动器由 tests/run.py / make tools-test 承接；一次性导出脚本退役并保留原输出。
有效旧编排评测迁入 evals/legacy，对照驱动仍在 evals/agent，导入及用例路径同步；不改变模型、评分或业务逻辑。
恢复说明见[根 README](../../README.md#history-archive)。P3/P3A、P4/P5 状态不变。
本轮受影响隔离测试 23 passed（1.69s），通过 tests/run.py 启用网络/真实数据库/凭据保护；
未运行真实模型、数据库或聊天评测。Python AST、文档本地链接、ZIP 完整性及逐文件哈希检查通过。

## 记录约定

使用独立 `AL-*` 编号，记录类型/状态、发现方、主责/配合、版本、触发条件、预期/实际、证据和关闭条件。保留失败和未验证项，不以提供方完成代替联合验收。其他本地 docs 按 ID 引用，不复制问题清单。

<a id="al-018"></a>
## AL-018：计划复核的依据格式校验阻塞正确计划，造成额外模型调用

- 类型／状态：已确认的复核契约问题，**待处理**；用户2026-10-22要求先登记，不修改复核逻辑。
- 发现方：用户通过Phoenix人工排错；主责：工具计划生成/复核；配合：编排预算、隔离测试和追踪。
- 版本与位置：`refactor/agent-loop`，基点`2bccfda`未提交工作区；[structured_planning.py](../../backend/app/tools/structured_planning.py)的`_validate_review`/`generate_tool_plan`，[复核schema](../../backend/app/schemas/tool_plan_review.py)、[复核Prompt](../../backend/app/tools/prompts/plan_review.py)。
- 复现证据：正式请求trace `02bc8b2ba6c61227540ba2eab3600578`，会话946。复核输入的`original_request.query`为“500元以内的机械键盘，按价格从低到高排序”，另有`limit=2`；候选`requested_limit=2`。第一次复核approve，以“limit 2”为数量检查依据，被“evidence必须是query原文片段”规则拒绝。第二次换为整句query，数量判断未变即通过；该句本身不包含数量2。实际工具内部1次规划+2次复核，增加的一次仅修正复核报告，不是纠正商品查询计划。
- 根因与边界：复核被要求核对完整请求，但程序只接受query字符串来源；原文子串与JSON Pointer存在性不证明语义判断正确。形式约束给辅助说明增加了阻塞执行能力。不能因此删除身份、权限、字段/操作符能力、范围、预算等必要执行边界，也不能把本例推断为所有复核均无用。
- 待评估方向：简化复核输出为结论及具体差异；评估取消通过项强制逐字引用/路径要求，不再仅为解释格式重跑模型。数量/范围等可直接比较的结构化字段交给程序；语义忠实度仍需模型或效果验收。独立复核是否默认必经需另行评估收益，不能直接将每个工具改为嵌套ReAct；外层已有工具结果反馈循环。
- 关闭条件：确定并实施最小复核契约；用本例证明不因合法结构化依据产生额外修正；保留预算/AND-OR/否定/遗漏条件等历史语义纠错场景及执行安全边界，验证拒绝、修正、无法判定与调用预算；如改变默认复核策略，真实效果/成本验证须另行授权。完成后更新Prompt/schema/实现及对应测试，不能只删失败断言。
- 当前不包含：商品明细因16000字符投影被移除的问题属于另一交接缺口（AL-016记录的现象、AL-001后续核对）；不以本条关闭替代其修复。
