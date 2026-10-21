# 维护脚本

从 `backend/` 用已有 `.venv/bin/python -m scripts.<名称>`；日常启动使用根 Makefile。

| 脚本 | 影响 |
| --- | --- |
| `check_local_config` | 读取本地配置；不是本次离线验证入口 |
| `import_compact_catalog`、`seed_demo`、`seed_user_isolation` | 写入目标数据库 |
| `import_pc_part_dataset` | 旧数据格式导入，仍由显式 `legacy-data-import` 使用 |
| `prepare_knowledge_model` | 下载 / 准备 embedding 模型 |
| `build_knowledge_vector_index`、`sync_knowledge` | 更新运行索引 / 同步 Chroma |
| `audit_catalog_spec_values` | 读取实际商品数据库 |

本轮没有运行上述维护操作。评测已移到 [根 evals](../../evals/README.md)，
离线保护和测试替身移到 [根 tests](../../tests/README.md)，没有兼容转发入口。
初始化、seed、索引重建及真实评测不能作为普通调试准备步骤。
