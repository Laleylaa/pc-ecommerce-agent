# 编排运行可靠性审查（2026-10-01）

审查对象：`feat/orchestrator-catalog-integration`、HEAD `744cc0f` 加审查开始时已有的未提交修改。没有修改生产实现、Prompt、锁文件或数据库，没有提交、推送、回退。新增隔离复现测试及本报告，现行开发入口仍为原编排 iteration。

证据：[原始测试结果和最终源码指纹](orchestration-audit-results.json)、[复现测试](../../tests/test_orchestrator_runtime_audit.py)、[隔离运行保护](../../scripts/orchestration_audit_safety.py)。测试是**现状刻画**：断言已观察到的问题；通过不表示问题已修复，修复时要将断言改为期望行为。源码指纹对应最终审查工作区，未读取或收录凭据。

## 实际执行链路

```text
POST /api/chat 或 /api/chat/stream：服务端注入 user_id
  load_context：准备本轮、历史、工作记忆、长期记忆
  request_router：LLM1 GoalPlan → 来源校验 → 边界保护 → 内部 route_plan
    无工具目标/规划失败 → summarize_goals → persist_turn → END
    有工具目标 → plan_tools：LLM2 ExecutionPlan → 参数/覆盖/依赖校验
      全部阻塞/规划失败 → summarize_goals → persist_turn → END
      可执行 → orchestrate：计算 ready tasks
        → execute_tool_wave（同 wave 内顺序 await）
        → normalize_tool_results → extract_task_artifacts → update_subquery_ledger
        → orchestrate（还有 ready tasks 则继续，否则调用回答模型）
        → terminal_guard
          → 有限回到 orchestrate，或 finalize_response → persist_turn → END
```

| 模块 | 当前职责与证据入口 |
| --- | --- |
| `api/routers/chat.py` | HTTP/SSE、认证身份接入；页面当前使用非流式 `/api/chat` |
| `goal_planning.py` | LLM1/2 schema、function-call 解析、一次修复、来源/任务覆盖/DAG 校验、延迟绑定 |
| `routing.py`、`route_runtime.py` | 内部 Goal/Task 表示、确定性安全/业务边界、部分旧兼容辅助函数 |
| `workflow.py` | 正式节点与条件边；六个旧 render 节点已无进入边 |
| `graph.py` | 运行入口、节点实现、就绪调度、回答调用、终态与持久化组织 |
| `execution.py`、`tools/contracts.py` | 按任务准备参数、服务端身份注入、正式工具调用、超时/结果封装、审计 |
| `artifacts.py` | 从结果生成任务产物，依赖与严格匹配检查，任务状态推导 |
| `outcomes.py` | 业务结果归一化、账本、终止动作校验 |
| `answer_context.py`、`responses.py`、`projections.py` | 回答证据与完成度、保底文本、API 展示投影 |
| `services/context.py` | 会话完成、记忆与审计保存；不负责选择业务工具 |

## 已确认问题

### OR-01：结果可用被当作回答完整，保底还会遗漏成功目标

- 触发：两次独立搜索成功，回答模型不可用；或模型只回答并引用第二个任务。
- 代码：`responses._fallback_answer` 遇 v3 搜索直接返回 `_latest_successful_tool_output`；`tool_loop._state_terminal_decision` 却填入所有可回答来源并生成 `finish_answer`。`outcomes.validate_terminal_decision` 只检查 `used_ids <= answerable_ids` 及所有任务已有结果，没有检查回答来源覆盖所有需回答任务。
- 复现：查询 SKU 1、SKU 2，均成功；保底只出现“测试键盘2”，不出现“测试键盘1”，`finish_answer`、guard accepted、引用两个来源。另给模型一个只引用 task_2 的回答，同样 accepted。
- 影响：独立结果没丢，但最终文字漏答且宣布全完成。与 RV-02 的“internal 前置结果是否展示”不同，本项两个任务都是 user_facing。
- 主责：编排回答/终态；配合：结果投影。
- 最小修复：保底按 user_facing task/goal 汇总；来源校验覆盖应回答的任务（允许明确等价来源映射），补齐非工具目标通知；不要试图仅靠检查自然语言关键词证明回答完整。验收双商品、商品＋订单、成功＋失败、工具＋澄清、回答模型异常；文字、来源、完成动作一致。

### OR-02：模型与整轮缺少截止时间，回答调用计数低于实际调用

- 触发：模型连接/响应不结束；或工具失败后回答模型连续返回非法格式。
- 代码：`core/llm.build_chat_model` 未设置 timeout；`graph.run/run_stream`、`workflow` 未设置整轮/节点截止时间；回答模型默认 `max_retries=2`。`_invoke_orchestrator_decision` 内部两次调用仅由外层 `_orchestrate` 计一次。
- 实测客户端：`root_async_client.timeout is None`，HTTP 客户端 `Timeout(timeout=None)`，不能把 SDK 常见默认 600 秒套到这个实例。
- 复现：一个失败工具之后，回答格式修复两次，再走一次 guard replan，共 4 次 `ainvoke`，`orchestrator_call_count=2`。有限退出，未出现无限循环。实际网络重试没有触发；若每次调用叠加可重试传输错误，HTTP 尝试数还可增大。
- 影响：演示可能一直等待；计数不能表示 LLM 调用总数。前端当前 `sendChat` 只接受取消 signal，没有自动截止时间，SSE 的 45 秒空闲超时不覆盖此入口。
- 主责：编排生命周期和模型配置；配合：API。
- 最小修复：明确单次模型超时、整轮截止时间及唯一传输重试策略；逐次记录模型尝试，区分阶段调用和 HTTP 尝试；超时走可验证收尾。验收用永不完成的 fake model 和连续异常，无需真等数分钟。

### OR-03：新 ExecutionPlan 路径丢弃工具重试建议

- 触发：第一波工具返回 `timeout/recommended_action=retry_once`，仍有第二波预算。
- 代码：`graph._orchestrate` 先取得 `_deterministic_recovery_decision`，随后 `if state.get("execution_plan"): capability_decision = None` 清空；失败任务也不再是 ready。工具外层超时在 `tools/contracts.py` 返回数据，不抛给图。
- 复现：正式图仅调用一次工具、wave=1 后结束；结束状态单独传给恢复函数仍可得到 `deterministic_retry_once`。
- 影响：工具契约建议和执行行为不一致；瞬态错误无法按既有策略恢复。不是“重试太多”。
- 主责：编排调度；配合：工具契约。
- 最小修复：在冻结计划执行器接入同参数的一次重试；将逻辑依赖层数与单任务尝试数分开核算，明确第一层重试后能否继续第二层。不能只删除清空语句，否则两波依赖＋重试的预算仍冲突。验收超时→成功、连续超时、不可重试错误、独立任务及下游依赖。

### OR-04：非流式与 SSE 异常收尾不一致，收尾自身还能遮蔽原异常

- 触发：持久化或节点异常；错误审计再次失败。
- 代码：`graph.run` 直接 `ainvoke`，无异常/取消收尾；`run_stream` 捕获异常后直接 await `_mark_run_failed`，这一步失败则没有最终 error 事件；`_mark_run_failed` 复用 session，没有显式处理已失败事务。API 同步入口也不补偿。
- 复现：模拟 complete_turn 抛错，非流式直接抛出且未调用失败记录，SSE 会调用；再模拟失败记录抛错，SSE 对外变为第二个异常，没有 error 事件。
- 影响：同一业务错误有不同运行结局；排障可能丢原始原因。不能由此断言真实库一定留下 running 记录：prepare_turn 仅 flush，事务是否回滚、记录是否存在需真实持久化验收。
- 主责：Runtime 生命周期；配合：上下文服务、repository/API。
- 最小修复：共享运行异常与取消收尾，保留最后可用状态；定义回滚/失败记录事务边界，收尾失败不能覆盖原异常，SSE 尽力发受控错误。不要直接给整个有审计写入的节点加自动重跑。验收同步/SSE 同一故障、取消、审计二次故障与隔离数据库事务。

### OR-05：两条回答路径的校验强度不一致

- 触发：无工具路径返回纯空白 response；工具回答同时携带一个正常控制调用和 invalid_tool_calls。
- 代码：`GoalAnswer.response` 仅 min_length；`_summarize_goals` 的语义校验是空 lambda，直接连 persist_turn；`decisions.decision_from_ai_message` 不检查 invalid_tool_calls。相对地 LLM1/2 的 generate_plan 已正确拒绝混合非法输出。
- 复现：三个空格作为最终回答被持久化且未经过 guard；正常 finish_answer 加破损 catalog_search JSON 被解析为正常终止动作。破损业务调用没有执行，本项不是越权执行漏洞。
- 影响：空回答、协议非法输出被当有效终态。无工具路径对边界和规划失败的解释主要靠 Prompt；是否真实模型会错误声称办理成功仍是风险，未据模拟输出宣称发生过。
- 主责：编排回答契约。
- 最小修复：回答公共非空白校验，统一拒绝 invalid_tool_calls/混合调用；无工具终态按 disposition 校验并保留确定性必要通知，不强制它伪造工具引用。验收直答、澄清、拒绝、人工、规划失败及空白输出。

### OR-06：回归测试仍混用旧入口契约

- 本轮扩大到 10 个相关文件：304 passed、54 failed、1 xfailed，排除写库 fixture 闭包 0 条；原始每例结果见 JSON。
- 48 条在 `test_request_router.py`：仍用旧 `subqueries` 路由输出、旧 planned/blocked 状态、旧 fast-path/会话回答预期。例如第396行起的模型返回不符合 GoalPlan。
- 5 条在 `test_orchestrator_wave_loop_cases.py`：传入无 route_plan 的旧状态，`answer_context.py:110` 报 KeyError，未真正走到想检查的失败处理。
- 1 条在 `test_orchestrator_task_dag.py:1350`：仍要求搜索 runtime_context 包含已撤出的 catalog_context。
- 主责：编排契约迁移与测试维护。不能将 54 条都算业务缺陷，也不能全部删除以变绿。
- 最小修复：迁移到现行 LLM1→LLM2→工具→回答入口；保留原安全/混合/失败行为验收意图。明确旧辅助 API 是否仍支持，否则删除兼容代码须独立评估。迁移后的失败逐项归因，不恢复已被取代的关键词否决以迎合旧断言。

### 既有问题本轮状态

- GI-02：`schemas/chat.py:30` 纯空白仍通过；本轮既有 strict xfail 再次命中，归 API 输入校验。最小修复为非空白校验，验收不调用模型/工具并返回请求校验错误。
- RP-01：源码仍把 unsupported 解释为通用能力不足，未新增真实工具错误复现，不新增重复 issue。
- RV-01、GI-01、GI-03、TP-01～03、RV-04/05：本轮隔离验证不替代真实自然语言验收，未宣称关闭。
- 知识检索、工具内部解析、用途/替代业务决定继续排除。

## 重试、退出和 LangGraph 专项结论

锁文件和安装环境均为 LangGraph **1.2.12**，langchain-openai 1.6.6、openai 3.19.2。本地 `StateGraph.add_node` 支持 retry_policy 和 timeout，未凭最低依赖声明 `>=0.2.0` 推断能力。核对官方 [Graph API](https://docs.langchain.com/oss/python/langgraph/graph-api) 与 [容错文档](https://docs.langchain.com/oss/python/langgraph/fault-tolerance)；推荐只涉及本地已确认存在的能力。

| 回路 | 当前真实边界 | 判断 |
| --- | --- | --- |
| LLM1 | 首次＋一次格式/契约修复，SDK重试0 | 有界；运输/普通程序异常不走格式修复 |
| LLM2 | 同上，失败不重跑LLM1；最多8任务、DAG深度2 | 有界；校验失败不执行业务工具 |
| 工具波 | 最多2波，wave内串行，正式工具自带超时 | 不必为了使用图能力改并发；OR-03需要修复 |
| 回答格式 | 单次 orchestrate 内最多2次 | OR-02计数低估；回答SDK另有2次重试 |
| terminal_guard | 最多一次回到 orchestrate；已有可答结果直接保底 | 这里是重生成回答，不是重新调用LLM1/LLM2 |
| 图 | 本地默认 recursion_limit=25 | 当前两波加一次guard回路约19个节点步骤，未发现顶到递归上限；递归限制不限制节点耗时 |

图已真实承载阶段和循环，不能评价成“没有用好 LangGraph”。任务 DAG 描述业务依赖，StateGraph 描述执行阶段，两者并存合理。节点内的两次模型格式修复保留普通函数即可。工具异常已转成 ToolExecutionResult，原生 RetryPolicy 不会自动识别这些数据；对整个波节点重跑还可能重复已成功任务和审计。

低优先级整理：`orchestrate` 同时调度和回答，命名/分支可在修复时局部拆清；六个无进入边的旧 render 节点可核实调用方后清理；normalized_tool_results 被写入但后续产物/账本再次归一化，主要是重复计算/审计表示，不是已证实业务故障。当前节点串行、没有并行写状态的证据，不需要为并发预建 reducer/Send 框架；未来改并行再设计状态合并。

## 待验证风险与建议顺序

待验证：真实模型条件保真和 goal 覆盖；无工具最终回答的事实与边界遵守；真实数据库异常/取消事务；工具审计或投影异常是否中止同波剩余任务；真实 HTTP/SSE/页面长等待。现有模拟证明普通工具失败前后，独立任务都能保留结果；不能外推为所有基础设施故障隔离。

1. **先做有截止时间的受控收尾（OR-02＋OR-04）**：fake model 永不返回、传输错误、持久化及审计故障；在设定时限内结束、保留原错误，同步/SSE一致，工具不重复执行。明确事务策略后再做隔离数据库验证。
2. **补全回答完整性（OR-01＋OR-05，顺手GI-02）**：逐目标保底、来源覆盖、非空白和非法调用拒绝；验证双成功、部分失败及无工具终态。
3. **恢复有限重试（OR-03）**：先明确单任务尝试与逻辑波预算，再测两层依赖中的超时重试；不要整体重规划。
4. **随上述修复迁移相关回归，最后清理剩余旧测试（OR-06）**：保留行为断言，分开当前契约与兼容入口。
5. **最后做面试场景联合验收**：正常推荐、连续追问、两个独立目标、一项失败、两层依赖、模型超时、人工确认；真实数据/模型/页面另列成绩。知识库继续排除。

## 实际验证与复现

- 新增最终12例：全部通过，覆盖已观察缺陷和“独立任务普通异常仍隔离”的对照；不是修复通过。
- 扩展相关回归：304通过、54失败、1既有xfail（共359条，当时新增审查测试11例）。之后仅把独立失败对照扩为前后两种顺序，再跑审查文件12例通过，未重复计算总成绩。
- 新增测试和隔离插件 Ruff 检查通过。生产代码未改，无结构重构完成声明。
- 未运行完整后端/make check、真实模型、真实数据库、HTTP/SSE网络或浏览器交互；SSE只验证Python异步事件生成器和模拟持久化。
- 审查过 conftest，未选调用写库 fixture 的用例；额外禁止测试期间 socket.connect/connect_ex 与 AsyncEngine.connect。不把保护插件当作任意未知脚本的安全沙箱。

从 backend 执行新增复现：

```bash
PYTHONPATH=. .venv/bin/python -m pytest -p scripts.orchestration_audit_safety tests/test_orchestrator_runtime_audit.py -q
```

扩展回归使用同一命令，再追加：`test_goal_planning_validation.py`、`test_planning_execution_policy.py`、`test_planning_contract_alignment.py`、`test_goal_task_integration.py`、`test_orchestrator_wave_loop_cases.py`、`test_orchestrator_outcomes.py`、`test_answer_context.py`、`test_orchestrator_task_dag.py`、`test_request_router.py`（均位于 tests）。修复前预期仍有上述失败；最终独立对照新增一条后总用例数会比本次扩展运行多一条。

文档差异：modules/orchestration.md 已标“基线待核实”，其中直接走六个模板、错误/取消统一收尾等说明不适用于当前工作区；当前正式无工具链路走 summarize_goals。本轮更新 development 和评测入口，不升级 modules 稳定基线。
