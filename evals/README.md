# 模型效果评测

评测驱动、输入、评分逻辑在根 `evals`；运行结果在 `artifacts/evals`，本地 Trace 导出在
`artifacts/traces`，都按项目根定位并默认忽略。`backend/evals` 和 `backend/scripts` 中不再保留第二套评测入口。
历史报告与退役脚本已封存到本地历史 ZIP，包内保留原始结果、版本指纹和完整迁移索引；
保管与恢复方式见 [历史档案与恢复](../README.md#history-archive)。

所有命令从项目根执行：`PYTHONPATH=backend:.:tests backend/.venv/bin/python -m <模块> <参数>`。
这里列出的是显式、有风险的入口，**2026-10-20 目录整理没有执行任何真实模型、数据库或联网评测**。
离线开发请运行 [tests](../tests/README.md)，不要将这些命令作为普通调试准备。

| 模块 / Make 入口 | 评测对象 | 费用和数据影响 |
| --- | --- | --- |
| `make model-eval ARGS='--live'` / `evals.agent.compare_runtimes` | 两套当前 runtime 的质量和成本比较 | 真实 LLM，消耗额度；合成领域数据 |
| `evals.agent.eval_agent_loop_baseline` | 旧编排基线 | 真实 LLM；合成数据 |
| `evals.agent.eval_goal_planning` | 规划及评分 | 真实 LLM；合成用例；需 `--output` |
| `evals.agent.eval_goal_tool_flow` | 编排到正式工具交接 | `--live` 消耗模型额度；`--langsmith` 额外联网上传；`--scripted` 为替身模式 |
| `evals.agent.review_answer_style` | 独立回答质量 | 真实 LLM；合成证据；需人工语义复核 |
| `evals.agent.eval_answer_synthesizer` | 旧回答专项 | 真实 LLM；模拟工具结果 |
| `evals.agent.probe_agent_loop` | 历史 P0 SDK 探针 | `--live` 真实 LLM；不是正式 runtime |
| `evals.tools.eval_catalog_shopping` | 商品/订单规划、复核 | 真实 LLM；合成商品/订单 |
| `evals.tools.eval_catalog_search` | 商品实际查询 | **真实 LLM + 读取商品数据库** |
| `evals.tools.eval_order_tricky_cases` | 订单自然表达 | 真实 LLM；合成订单 |
| `evals.agent.eval_order_tool_live_e2e` | 订单聊天链路 | **真实 LLM + 数据库写入/回滚** |
| `evals.agent.run_customer_service_evals` | 外部 CSV 聊天数据 | **真实聊天 API，模型费用及会话/记忆写入**；需自备 `--eval-root` |

`agent/goal_planning/cases.json` 和 `agent/*cases.py` 保存现行用例；
`tools/catalog_search/` 保持原本本地忽略边界，缺少其输入时不能声称商品评测可复现。
共享合成领域数据在 `support/goal_tool_fixtures.py`；程序规则和评分器的隔离测试仍属于 tests。
评测允许引用测试的纯替身模块，但不能依赖测试函数或启动 pytest。

`--scripted` 模式本身不能替代进程级隔离保护；已有 pytest 中对这些驱动的模拟检查通过统一保护执行。
本次驱动只做静态路径检查及已审查的模拟消费检查，不运行 CLI live 模式，也未测模型连接。
`agent/legacy` 使用当前共用工具，不是冻结工具算法的历史基线；完整历史比较须恢复 Git 版本或本地 ZIP。
Phoenix 的服务、存储和 OTEL 接入保留给 P4（AL-016）；当前目录仅整理现有输出，没有部署平台。
