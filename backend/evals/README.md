# Agent trace 与评测使用说明

本目录保留固定用例与复跑说明；评测脚本位于 `backend/scripts/`。
开发 issues、调试过程、实际成绩和审查报告只保留在本地 `docs/`、`artifacts/` 或忽略的输出目录，
不提交到远端。聊天评测使用 `/api/chat`。

## 商品工具模型评测

从 `backend/` 显式执行（消耗模型额度）：

```sh
.venv/bin/python -m scripts.eval_catalog_shopping --acceptance
.venv/bin/python -m scripts.eval_catalog_shopping --review-probes
```

使用合成商品和正式商品工具；订单部分仅校验计划。不连接配置数据库，不验证完整聊天或浏览器。
报告输出到忽略的 `evals/results/`。预置错误修复探针不代表自然错误发生率。
规划专项的输入和入口见 [规划评测](goal_planning/README.md)。

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
聊天自动记录整个图；技术评测入口主动止于工具结果交接，不评测正常回答质量。

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

测试入口及安全边界见 [后端测试](../tests/README.md)。运行前检查 fixture 与脚本副作用，
不把真实模型评测当作离线检查。历史成绩不代表当前版本已完成验收。
