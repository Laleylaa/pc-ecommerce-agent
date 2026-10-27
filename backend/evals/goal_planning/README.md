# LLM1 / LLM2 规划评测

[固定输入与预期](cases.json)由[评测脚本](../../scripts/eval_goal_planning.py)消费。
只验证目标识别与工具规划，不执行业务工具、不调用最终回答模型、不连接数据库。

从 `backend/` 显式执行：

```sh
.venv/bin/python -m scripts.eval_goal_planning --live --output evals/results/goal-planning/new-run.json
```

`--live` 会消耗模型额度；可重复传入 `--case` 只选指定场景。
每轮使用新的输出文件名，保留原始失败和复测结果，避免覆盖证据。
小样本结果不能代表总体准确率，规划通过也不代表工具业务或完整聊天验收通过。

[工具链路与 trace 使用说明](../README.md)包含脚本模型和真实模型入口。
开发过程报告、问题台账与批次输出只留本地；本页不收录历史成绩或报告链接。
