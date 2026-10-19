# 评测入口

日常开发先运行[隔离测试](../tests/README.md)；只有需要判断真实模型效果时才运行下面的评测。完整脚本分类见[脚本说明](../scripts/README.md)。以下命令均从 `backend/` 执行，不表示本轮已运行。

## 当前入口

| 目的 | 入口 | 依赖与边界 |
| --- | --- | --- |
| 编排到工具交接冒烟 | `python -m scripts.eval_goal_tool_flow --scripted` | 脚本模型、合成数据；不连接配置数据库，不验最终回答 |
| 商品/订单自然语言规划 | `python -m scripts.eval_catalog_shopping --acceptance` | 真实模型＋合成数据；订单只验计划 |
| 已知错误的审核修复 | `python -m scripts.eval_catalog_shopping --review-probes` | 真实模型，不能当自然错误率 |
| 商品实际查询 | `python -m scripts.eval_catalog_search --suite all` | 真实模型和只读商品库；用例在本地 `catalog_search/inputs/` |
| 订单专项 | `python -m scripts.eval_order_tricky_cases` | 真实模型＋合成订单 |
| 回答专项 | `python -m scripts.eval_answer_synthesizer` | 真实模型＋模拟工具结果，不是完整聊天 |
| 订单聊天链路 | `python -m scripts.eval_order_tool_live_e2e` | 认证 API、真实模型和数据库；会写入再回滚，执行前核对隔离目标 |

命令中的 `python` 使用 `.venv/bin/python`。真实模型评测消耗额度，不作为默认测试执行。
`run_customer_service_evals.py` 保留为可选 CSV 导入评测入口：本地没有默认数据集，必须提供 `--eval-root`；聊天会写会话/记忆。

## 正常购物工具评测

当前入口是 `eval_catalog_shopping`，支持 `--acceptance`、`--compare`、`--review-probes` 和 `--retry-timeouts`。
2026-10-19的[36项结果](results/catalog-shopping-20261019-120716.json)与[4项修复结果](results/catalog-shopping-20261019-120605.json)仅是历史小样本证据，不代表本轮或生产准确率。
完整对照、失败和限制见[历史记录](history/evaluation-log.md#正常购物工具评测)；归档时的验收缺口见[TP-05](history/issues-before-agent-loop.md#tp-05)。

## Trace 与结果

普通聊天通过 `AGENT_TRACE_MODE=off|local|langsmith` 控制，默认 `off`；默认目录为 `evals/results/agent_traces/`。
`eval_goal_tool_flow --live --case named_product` 使用真实模型与合成数据；需要上传时显式加 `--langsmith`，不能与 `--scripted` 组合。
每次运行打印产物位置：`manifest.json` 记录版本与配置指纹，`results.json`/`report.md` 汇总用例，`trace.jsonl`/`summary.json` 记录调用与终止状态。
`recording_complete` 只表示记录完整，不表示业务成功；失败和超时不能当作无匹配。

## 文件边界

- `goal_planning/cases.json`：当前编排脚本仍读取的用例，保留。
- `catalog_search/inputs/`：当前商品评测用例，本地忽略，不能当缓存删除。
- `results/` 与 `catalog_search/results/`：本地产物，保留被文档引用的证据。
- `history/`：旧报告和退役脚本快照，不是当前执行入口，不参与默认 pytest。

本次清理说明、备份位置与保留的回归见[历史区说明](history/README.md)。不改变 docs 和商品本地评测目录的 Git 忽略范围。
