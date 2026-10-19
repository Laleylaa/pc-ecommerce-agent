# 开发脚本

从 `backend/` 使用 `.venv/bin/python -m scripts.<名称>`；日常安装和启动优先使用根 Makefile。不要把初始化、seed 或真实评测当普通测试运行。

| 分类 | 脚本 | 用途 |
| --- | --- | --- |
| 环境检查 | `check_local_config` | 配置检查，不连接数据库 |
| 数据维护 | `import_compact_catalog`、`seed_demo`、`seed_user_isolation` | 导入/初始化，会写目标数据库 |
| 旧数据导入 | `import_pc_part_dataset` | Makefile 的 `legacy-data-import` 仍使用，非默认入口 |
| 知识维护 | `prepare_knowledge_model`、`build_knowledge_vector_index`、`sync_knowledge` | 下载/准备模型、重建索引、同步 Chroma |
| 数据审计 | `audit_catalog_spec_values` | 检查实际商品规格，需核对数据库目标 |
| 当前效果评测 | `eval_catalog_shopping`、`eval_catalog_search`、`eval_order_tricky_cases` | 规划/查询效果，见评测说明 |
| 回答与聊天 | `eval_answer_synthesizer`、`eval_order_tool_live_e2e` | 回答专项及订单聊天链路，后者会写库再回滚 |
| 可选 CSV 评测 | `run_customer_service_evals` | 需要外部数据集，通过 `--eval-root` 提供；不用于日常启动 |
| 编排调试 | `eval_goal_planning`、`eval_goal_tool_flow` | 当前规划与交接评测；新编排替代完成后再迁移 |
| 测试支撑 | `goal_tool_fixtures`、`orchestration_audit_safety` | 仍被测试直接导入，不能单独删除 |

当前命令和风险边界见[评测入口](../evals/README.md)，自动化回归见[测试入口](../tests/README.md)。
旧商品评测、阶段审计和论文消融已移至[历史区](../evals/history/README.md)，不再作为可运行的维护入口。
