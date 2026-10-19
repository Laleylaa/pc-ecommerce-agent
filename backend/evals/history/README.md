# 历史资料

2026-10-19从 `refactor/agent-loop` 的基点 `f1cd9ff` 整理。这里只保留历史证据与固定用例，**不作为当前脚本入口，不自动维护或执行**。

- `goal_planning/`：旧阶段报告和输出；活动用例仍在 `../goal_planning/cases.json`。
- `test_maintenance/`：过去的测试清理记录，原数字保留历史含义。
- `retired/`：保留原相对目录的脚本、用例和论文协议测试快照（Python源码以`.py.txt`冻结，避免参与pytest发现或Ruff扫描）；它们依赖当时的源码与契约，需要复现时使用原 Git 版本。
- [旧问题台账](issues-before-agent-loop.md)：完整保留旧问题和归档时状态，停止按旧台账跟进，不代表修复。
- `evaluation-log.md`：清理前的评测说明，保留旧实验的成功、失败与限制。

## 退役范围与承接

| 退役项 | 原因与保留方式 |
| --- | --- |
| `eval_catalog_llm_tools`＋`catalog_llm_eval_cases.json` | 旧输出字段和品类口径；当前使用 `eval_catalog_search` 与 `eval_catalog_shopping` |
| `audit_tool_repair_boundaries` | E30一次性现状探针，原脚本明确不作为正确性断言；固定输入随快照保留 |
| `eval_contract_acceptance` | TI-01/TI-03阶段验收，含旧分页目标口径；不并入当前验收成绩，原场景随快照保留 |
| `eval_tool_planning_repairs` | 早期8例规划探针；当前条件/纠错机制由工具测试与shopping评测维护，旧表达保留在快照 |
| `run_section5_offline_ablation`＋协议测试 | 退役论文的30例/60次实验与3个协议测试，不宣称保留全部消融实验覆盖 |

仍保留的业务回归：`tests/catalog/test_tool_planning_repairs.py`、`test_catalog_query_policies.py`、`test_catalog_query_stages.py`，以及 `tests/orchestration/test_planning_handoff.py`、`test_orchestrator_task_dag.py`、`test_boundary_classification.py`、`test_answer_finalization.py`。它们继续检查条件、身份、依赖、业务边界和回答收尾；不将脚本归档描述为新增覆盖。

## 本地可恢复备份

清理前的全部 `docs/`、`backend/evals/` 和退役脚本已备份到项目根的 `local-backups/file-cleanup-20261019/before-cleanup.tar.gz`；同目录 `manifest.json` 记录 SHA256、移动与清理清单。归档逐文件校验了1002个文件，未收录 `.env` 或数据库备份。
该目录被 Git 忽略，只在本机存在，不能把 Git 提交当成它的备份。
未被文档引用的112个本地产物条目（约50.6 MiB）移出工作目录，仍在压缩包中；当前用例和文档引用的证据保留。

恢复时先解压到独立目录，再选择性取回；不要覆盖当前源码。生成缓存未备份，可重建。

## 本轮验证

完整隔离回归1822通过（清理前1825，减少3项论文协议测试）；全后端Ruff、当前文档入口链接及diff检查通过。活动Python模块没有引用退役脚本。归档源码与JSON保留原内容，Markdown仅调整迁移链接。未运行真实模型、数据库或浏览器验收，未提交。
