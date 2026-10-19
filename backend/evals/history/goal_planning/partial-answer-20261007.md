# RV-02：保留前置事实的部分回答（2026-10-07，E53）

适用`feat/orchestrator-catalog-integration`，HEAD `931ba13299e3491aa4e192bd8a0a2a44f2229cbd`加当前未提交工作区。用户确认：比较两款只查到一款时，应保留已查到的有用信息，简短说明另一部分未完成，不给出无依据的比较结论。

## 根因与改动

原supporting_tasks已传给LLM3，但终态来源集合仅包含user_facing已解决任务，确定性fallback也只遍历这些任务。故“输入保留事实”不等于“回答可引用并在失败时保留”。

- answer_context按当前依赖关系标出前置事实可服务的未完成目标：下游failed/unavailable/blocked，前置succeeded、artifact可用、当前active ledger与artifact来源一致。失效/被替代来源、失败前置、无相关未完成后继不开放。保留事实范围、诊断、response_contract；不把前置成功加入resolved_task_ids。
- 输出校验仅允许finish_partial额外引用这些前置来源，同时要求有效ledger和usable artifact；finish_answer完整覆盖要求不变。LLM3可选择有用事实，不强制输出所有内部材料，也不新增业务执行。
- fallback按目标保留同样准入的事实并引用来源，最终任务仍未完成；guard只检查，拒绝交确定性收尾，无重试或恢复回跳。
- Prompt明确已确认产品口径，并保留候选范围/替代条件/缺资料的事实边界。完整目标本来已有独立成功结果的场景保持原行为。

实现：[answer_context](../../../app/agent/answer_context.py)、[输出校验](../../../app/agent/outcomes.py)、[guard调用](../../../app/agent/graph.py)、[fallback](../../../app/agent/responses.py)、[LLM3契约](../../../app/agent/prompts/observation.py)。未改工具、模型/推理配置、权限或前端。

## 本次验证

复用现有案例，未新增测试文件或独立用例：

- 扩展goal_task_integration原“前置返回替代候选、严格依赖被阻塞”场景：真实工具实现＋人工商品＋脚本规划，检查材料进入部分回答来源，结构化部分回答通过并原样保留；相同来源冒充完成被拒绝，兜底保留具体商品/价格，下游仍blocked、工具仍只调用一次；失效来源不能复活。
- 扩展answer_handoff原失败前置场景：失败的internal前置不能变成可引用事实，下游仍为依赖阻塞。
- 扩展goal_recovery原依赖替换场景：最终下游已成功时，internal材料不额外开放为部分回答来源；旧结果失效及新结果绑定原断言保留。
- 相关回答、guard、恢复和旧测试迁移回归全部通过。LLM3答案为脚本输出，不是新真实模型效果或聊天HTTP/SSE验收；正文是否自然简短、是否避免无依据比较仍须真实表达验证，归RV-03，不再把已修程序接入重复挂在RV-02。

从backend执行：

```sh
.venv/bin/python -m pytest -p scripts.orchestration_audit_safety tests/test_goal_task_integration.py tests/test_goal_recovery.py tests/test_answer_handoff.py tests/test_answer_context.py tests/test_answer_finalization.py tests/test_orchestrator_outcomes.py tests/test_orchestrator_runtime_audit.py tests/test_orchestrator_tool_observation_eval.py tests/test_orchestrator_wave_loop_cases.py -q --tb=short --junitxml=/tmp/e53-final.xml
.venv/bin/ruff check app/agent/answer_context.py app/agent/responses.py app/agent/outcomes.py app/agent/graph.py app/agent/prompts/observation.py tests/test_goal_task_integration.py tests/test_goal_recovery.py tests/test_answer_handoff.py
```

257 passed，无失败/排除/skip/xfail；Ruff、文档链接和git diff --check通过。fixture已检查，启用安全插件阻断外部连接及配置数据库；未新建测试会话、调用真实模型/搜索、操作数据库。原始pytest输出和JUnit在本地忽略目录[partial-answer-20261007](../../results/partial-answer-20261007)，未覆盖历史trace或强制暂存。

## 后续话术与展示考虑（仅分析，尚未实施）

当前前端已经安装并使用react-markdown＋remark-gfm，ChatPanel同时渲染Markdown正文及ProductCardRow。短结论、少量关键差异和商品卡片可以先复用现有组件，不需要为这一步先增加UI库。

用户希望通用“AI有不确定性”等自我说明改为固定前端提示，正文简短。后续应区分通用提示与本次证据边界：前者可统一展示，后者只保留会影响购买/比较结论的具体缺口，如“另一款未查到”或“候选超预算”。可考虑正文一句结论＋必要差异，参数由卡片/短表格承载，避免正文和卡片重复。尚未调整整体Prompt、默认回答长度、前端固定提示或页面样式；不以本轮实现声称话术已变得稳定简洁。

本轮保留既有工作区差异，索引保持原样，无add/commit/push。RV-02关闭的是已确认口径与程序接入，真实模型及浏览器验证仍独立跟踪。
