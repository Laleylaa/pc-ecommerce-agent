# Trace 与现有测试整理（2026-10-06）

适用 `feat/orchestrator-catalog-integration`，基点 `931ba13299e3491aa4e192bd8a0a2a44f2229cbd`。
开始时索引为空，只有 `frontend/src/App.tsx` 未暂存；本轮未改该文件、索引、业务规则、业务 Prompt 或模型配置。
本地有效模型为 deepseek / deepseek-flash；源码默认模型名不能替代本地有效配置。
本轮没有新增测试用例、真实模型/搜索调用、数据库访问或聊天请求。尚未开始新测试或新一轮业务评测；仅运行既有隔离回归。

## 当前进展与证据层级

- 正式入口为 `api/routers/chat.py` → `AgentRuntime.run/run_stream` → `workflow.py`。
  LLM1/LLM2、工具内部计划、结果交接、LLM3、一次有界恢复批次和持久化已在代码中接通。
  E41 回到原规划节点，局部替换/保护任务及依赖失效由 `recovery.py` 管理；E44 独立只读工具最多三个并发，审计写入串行。
- E45 能力说明、E46 name/keywords/conditions 与候选范围、E47 推荐顺序、E48 显式 limit 均已接入当前源码。
  thinking 仍 disabled；兼容试验没有成为正式配置。模型理解正确性不是 schema/终态校验的保证。
- 已读指定七目录的 report，核对四个聊天批次的 manifest/audit 结构与版本，以及恢复 run1431 和数量 run1443 的原生节点证据。
  manifest 的旧 HEAD 加 dirty 文件哈希表示历史工作区，不把它当本轮代码验收。

| 历史目录 | 证据性质与限制 |
| --- | --- |
| chat-e2e-20261005-e44-review | 10 次正式 HTTP/SSE、保存回读；run1431 自然回 LLM2、部分准入及新旧引用。没有覆盖真实回 LLM1、跨目标依赖失效等全部恢复路径 |
| chat-e2e-20261006-llm1-capabilities | 4 次正式聊天；能力分类改善，其他数量/回答问题仍存在；required 推理兼容试验另计 |
| llm3-thinking-trial-20261006 | 标题匹配只读数据库检查＋3 次固定材料模型调用，不是聊天 E2E |
| llm3-scope-ab-20261006 | 2 次固定材料模型对照，不能代表新 LLM1 链路或推理总体收益 |
| chat-e2e-20261006-title-scope | 7 次聊天、另 1 次模型重放、1 次真实工具只读验证；三类不能相加成为 9 次 E2E |
| recommendation-contract-20261006 | 2 次模型重放，推荐顺序符合预期，不是保存/HTTP 验收 |
| chat-e2e-20261006-limit | run1443 正式 SSE 单例：limit=1、返回一个型号、订单完成且保存；不能证明所有表达不再漏数量 |

以上均为历史证据，本轮没有重跑。用户接受 run1441 的“这款”指向前述几款，本例不列待修缺陷，也不推广为普遍消歧规则。

## 测试清单与去留

清单按当前职责分类；“保留”不等于本轮逐用例通过。没有运行全后端或以文件年代判定过时。

| 分类 | 位置与原验证目标 | 本轮处置 |
| --- | --- | --- |
| 当前有效、已运行 | test_agent_tracing：开关等价、原生父子 span、原始输出/修复、写盘/云故障、SSE 关闭；test_goal_recovery：准入/范围/保护/失效/预算；test_tool_concurrency：会话隔离/取消/顺序；test_answer_finalization：覆盖/预算/事务替身/HTTP | 全部保留，四文件合计 127 通过；是假模型/假 I/O，不是业务 E2E |
| 有效但失败 | test_orchestrator_runtime_audit 的 test_answer_format_attempts_are_counted_and_do_not_restart_at_guard | 原断言保留：连续空回答预期两次并不回 guard 重启，实际三次。格式修复局部计数在重新进入调用方法时重置；跟踪 OR-02，不改成三次求绿 |
| 旧入口、有效目标待适配 | test_request_router 的五个回答/SSE 例 | 旧 subqueries 路由输出、旧纯文本回答器/单阶段工具规划；当前 GoalPlan→ExecutionPlan→GoalReview/control。直答、历史证据、版本比较及 delta/done 仍有价值，保留五个失败；不恢复旧入口 |
| 旧职责断言 | test_orchestrator_task_dag 的 test_context_product_accepts_one_spu_with_multiple_sku_variants | 多版本同 SPU 与歧义阻断仍有效；搜索不再注入 catalog_context，需迁移为正式可信对象绑定断言，不能只删失败行。保留一个失败 |
| 旧状态与部分终态 | test_orchestrator_wave_loop_cases | 六个失败：五个终态路径缺 route_plan，一个旧 grounded_response 与当前 partial_response 不符；保留结果保护/禁止越权续查等有效断言，不机械改期望 |
| 旧结果/重试/入口 | test_orchestrator_tool_observation_eval 的 17 个参数场景 | 保留。搜索 catalog_context、缺 route_plan、knowledge 空结果旧结构、timeout 只调用一次均需按当前契约迁移。usage_mapping 历史分支是否仍需独立覆盖也要先对照当前领域状态；本轮不批量删除 |
| 仍有当前承接者、未扩大执行 | test_goal_planning_validation、test_planning_handoff、test_planning_contract_alignment、test_goal_task_integration、test_input_tool_contract_chain、test_tool_retry_policy | 规划校验、可信绑定、正式工具交接与重试；优先复用，不能被 trace 替代 |
| 领域、上下文及权限 | catalog_search/identity/outcomes/query_policies、tools/tool_planning_repairs、policy_retrieval_runtime、web_search、answer_context/handoff、conversation_context/working_memory、auth/session/memory/API 各文件 | 保留；本轮未运行这些完整文件。配置数据库 fixture 及其间接依赖不能纳入无副作用回归 |
| 历史模拟/真实评测脚本 | eval_goal_tool_flow、goal_tool_fixtures、订单 live runner、section5 离线消融等 | 不创建替代框架、不运行新评测。OrderRows 缺 filters 的适配缺口仍在，OR-06；脚本结果不能冒充正式 SQL 或聊天链路 |

**唯一删除**：`test_orchestrator_runtime_audit.py::test_audit_non_planning_model_still_has_no_http_deadline`。
原来验证裸 `build_chat_model()` 的 retry=2/timeout=None，并将其用作回答模型缺陷现状。
E36/E43 正式 `AgentRuntime` 已显式传 30 秒与 SDK retry=0，通用构造器默认值不再表示正式回答路径。
有效的正式客户端配置覆盖由
`test_answer_finalization.py::test_runtime_answer_http_client_has_deadline_and_no_hidden_retries` 承接；
真实挂起、传输与格式修复已有同文件回归。本轮这些既有用例已运行。
删除后不再锁定通用构造器的无超时默认值；没有已确认的产品契约要求该默认永久保持。
无其他删除、无 skip/xfail、无降低断言。AST 对照确认存留函数完全相同。

## Trace 覆盖与修改

| 链路 | 已覆盖证据 | 漏点、冗余与本轮动作 |
| --- | --- | --- |
| 原始请求/上下文 | input_rules/load_context 原生输入输出、run/parent/trace ID、历史与可信对象 | 新增 turn_context_loaded，仅列 conversation/agent_run/user_message ID，区分数据库 run 与原生 run |
| LLM1/LLM2 与工具模型 | 原生模型输入输出；planning_attempt/response/accepted/validation_error；反馈和重试事件 | 本地模型 span 新增原生 invocation_params/options，保留实际 tools/schema/tool_choice/模型参数；不是从 Settings 猜参数。SDK 超时等未出现在回调的参数仍需核对构造器/manifest |
| 参数绑定 | tool_binding_start/ready/rejected，task/goal/call/wave，原始与绑定后参数 | tool_handler_start 新增 input_sources：public_argument/default/runtime；绑定来源仍看前序 binding 事件。显式默认值与省略默认可区分，不声称检测自然语言遗漏 |
| 工具执行 | contract_input、handler_start/output/error、tool_result；内部计划与领域 status/reason_code | 新增 handler_cancelled；输出校验拒绝补 error_type。超时/取消与错误分开，模型输出不记为程序完成 |
| 结果交接 | normalize/extract/ledger 原生节点输出，执行成功与 usable、无匹配/缺资料/澄清/不支持分开 | 原始输出、正式结果、完整状态有重复；保留原生证据，不用 ok 或空列表代替业务状态 |
| LLM3/终态 | 实际模型 span、answer_context、decision、terminal_guard_status/feedback | control 路径没有与 planning 相同的逐次输出校验事件，仍需结合原始输出和 guard；本轮不复制整段 Prompt/结果。模型覆盖声明≠业务事实验证 |
| 恢复 | recovery_guard/dispatch 与原规划节点；batch previous_plan/results/artifacts、replacements、新结果 source_tool_call_id | 新增 recovery_admission/dispatch 紧凑索引：准入理由、阶段、保护/失效任务、替代映射及可用 Artifact ID。完整校验依据保留原生节点；不把替代映射理解为语义纠正成功 |
| 并发/关闭 | 原生时间与 call 属性隔离，gather 后按计划合并；根 run 错误与未闭合列表 | handler_cancelled 增强实际取消证据；排队但未开始的子任务没有单独 span，仍依赖计划与节点错误，未新增调度日志体系 |
| 保存/异常事务 | persist_turn 原生结果、context complete_turn 的 commit、失败清理代码 | 新增 turn_persisted、turn_cleanup_started/completed/failed，仅类型/关联 ID，不复制订单或回答。cleanup_completed 表示清理方法返回，不保证数据库 failure record 已创建（无 run 或已终态时可无操作） |
| HTTP/SSE | API 500/504、SSE error/done 的既有实现 | 新增 chat_response_prepared、sse_event_prepared(done/error)。表示序列化响应已准备，不是网络送达/浏览器展示确认；真实回读仍需 HTTP/DB/浏览器验收 |
| trace 故障 | 写盘/上传已有隔离与计数 | 修复 trace_event 的 RuntimeError 降级分支，降级序列化/写入异常也由外层吞掉并计 write_errors，不进入业务重试；取消仍传播 |

格式采用 **schema_version=1 的增量字段/新事件**，原有 kind、输入输出和事件名不删除。
消费者按 kind/name 读取、忽略未知字段；旧记录缺 invocation_params/input_sources/新事件时只能标为未知，不能补造默认。
`test_agent_tracing` 和 `eval_goal_tool_flow` 的既有读取方式继续可用；没有改写/删除历史 JSONL。
没有新增日志平台、执行链路或模型调用。

原生状态重复是已确认的体积问题：历史 run1431 为 2,328,931 字节/111 行，run1443 为 1,020,669 字节/66 行。
节点前后状态、分支输入及根输出重复携带结果；planning 的消息/响应也与原生模型 span 重复。
本轮未做状态差量/引用压缩：现有消费者直接读取节点 outputs，且用户禁止新增压缩还原用例。
后续应先明确兼容与还原断言，再压缩重复状态；原始模型输出、有效 schema、校验反馈不得截断。

隐私：当前 key 脱敏保护 Settings/密钥，不是自由文本/订单隐私脱敏器；原生上下文仍可能含订单及地址。
新增事件只补 ID/决策摘要，不增加完整上下文副本；原始本地 trace 仍按敏感产物管理，云端未验证接收。
recording_complete 只表示记录闭合且无本地写错；recording status=completed 也可能是正常消费了 SSE error，
必须同时看 cleanup/error、persisted 和业务结果。认证前失败没有 Agent trace，网络送达也不由它证明。

## 本轮实际检查

运行前核对 conftest、人工工具/上下文替身及安全插件；插件排除 db_session_factory 依赖并阻断网络和 AsyncEngine.connect。
所有命令从 backend 执行，解释器为 `.venv/bin/python`，pytest 加 `-p scripts.orchestration_audit_safety -q --tb=short`。

1. 修改前五文件：test_agent_tracing、test_request_router、test_orchestrator_task_dag、test_orchestrator_wave_loop_cases、test_orchestrator_tool_observation_eval：**87 passed / 29 failed**。
2. 中间五文件（trace/recovery/concurrency/finalization/runtime_audit）：139 passed / 1 failed；当时旧默认测试暂改为当前客户端检查，随后因现有承接者而删除，最终不保留重复用例。
3. 从 HEAD 导出原 runtime_audit 文件到临时目录运行既有计数例：同样 3≠2。使用本轮源码，不冒充修改前业务基线；源码差异仅观察点，不改变预算/guard。
4. 最终上述九文件合并：**197 passed / 30 failed**，无 skip/xfail/deselected。原 29 个失败集合未变，另一个为保留的回答修复预算失败；删除一个旧默认断言。不是完整后端成绩。

原始终端结果在本地忽略目录 `backend/evals/results/trace-test-review-20261006/`；失败 ID 均在 final.txt。
四个核心文件收集数另核对为127，与最终运行均通过一致。Ruff、git diff --check及新增跟踪文档本地链接检查通过。没有配置数据库、真实模型、真实搜索、浏览器或云接收验证。
新增 trace 字段及降级分支的专属故障断言本轮未新增，现有用例通过只证明其覆盖范围内的兼容性。

## 后续测试方案（未实施）

独立预期先固定于产品/契约，不从被测输出生成；程序检查、trace 完整与语义正确分别打分。

| 优先级/层级 | 场景与独立预期 | 观察证据与通过条件 | 复用/新增及数据影响 |
| --- | --- | --- | --- |
| P0 隔离契约 | 连续非法 LLM3 输出；一次格式修复耗尽应按既有契约收口，不借 guard 再获格式额度；传输重试与格式修复分别计数 | 既有计数断言、guard 次数、最终 fallback；不重跑工具、不丢已成功部分 | 先复用 runtime_audit/finalization；OR-02 修复获授权后运行，无真实 I/O |
| P0 隔离 trace | 降级写入失败、模型参数/默认来源、恢复拒绝/替代、取消与 SSE error | 与 trace off 的业务结果/调用数一致；错误计数可见，原异常/取消不被覆盖；旧 JSONL 可读 | 复用 tracing/concurrency/recovery 的场景；以后才补字段与故障断言，只写临时文件 |
| P0 隔离契约迁移 | 旧五个回答/SSE、同 SPU 多版本、结果保护与无关续查、knowledge 空/超时耗尽 | 正式 GoalPlan/ExecutionPlan/GoalReview，原自然语言问题和独立语义断言保留；scope 来源、handler 次数、结果引用明确 | 先迁移现有失败例，不能只替换期望值；不新增业务场景、不接配置数据库 |
| P1 真实工具/数据库 | 同 SKU 条件、候选子集/全型号、limit 缺省/1/上限、本人/他人订单与无匹配/缺资料 | 预先选定事实的只读 SQL 与返回 ID/统计/排序独立对照；过滤一致且无跨用户/跨 SKU 拼接 | 复用搜索及订单专项；先修 OrderRows filters 适配，真实 DB 用 READ ONLY；无业务写 |
| P1 真实模型 | 独立新表达：最近订单混合请求、标题/轴体、显式 N 与缺省 N、推荐顺序、候选范围 | 原句→各层计划→工具事实→逐句回答核对；数量、范围、排序保真，失败/缺资料不编造；结构与语义分别判定 | 复用历史评审维度，新增盲测表达，不只重放调优句；消耗额度；固定材料重放不写聊天 DB |
| P1 HTTP/SSE | 正常、部分成功、summary/control 恢复回 LLM1/LLM2、保护成功项/传递失效、超时/取消/保存失败 | HTTP/SSE 事件、原生 trace、独立 DB 回读一致；成功恰一 done 且回答已存；错误不发 done、事务状态符合契约 | 复用 finalization/recovery 脚本模式，缺失跨层故障需以后新增；专用隔离数据库，登录/消息/run/记忆有写入 |
| P2 浏览器 | 同步聊天、切换/新建会话、混合结果展示、失败提示、人工确认取消 | UI 对话归属与 API/DB 对照，成功部分可见、无旧会话串入、不暗自提交人工请求 | 现无浏览器套件，需新增；包含 App.tsx 用户修复的独立验收。使用隔离账号/库，聊天会写入；不实际提交人工请求 |

缺口与责任只在共享 issues 跟踪：OR-02 回答修复额度、OR-06 旧测试/订单适配器、OR-04 真事务、RV-03/TI-02 联合验收。
本报告不关闭这些事项，不把模型偶发语义错误新增为共享程序缺陷，不升级 modules 稳定基线。
