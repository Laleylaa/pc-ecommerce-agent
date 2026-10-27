# 维护入口

日常使用根 Makefile；Python 命令从 `backend/` 用 `.venv/bin/python -m scripts.<模块>` 运行。
所有写入命令只在明确知道目标数据库与影响后执行。

| 模块 | 职责和影响 |
| --- | --- |
| `check_local_config` | 检查配置结构，不调用数据库/模型，不输出秘密 |
| `local_config` | 供 Podman shell 内部消费的 NUL 配置协议；含数据库密码，不直接展示/记录输出 |
| `run_local` | 根据根 Settings 启动前后端，统一端口与代理 |
| `demo.load` | 空库导入统一入口；`--summary` 只读；写入整体事务，非空拒绝 |
| `demo.catalog` / `catalog_store` | 商品纯生成规则 / 数据库写入，供导入能力复用 |
| `demo.accounts` / `orders` / `policies` | 演示账号、订单物流及统一政策来源导入 |
| `prepare_knowledge_model` | 显式下载或预热 embedding 模型 |
| `build_knowledge_vector_index` | 根据知识正文重建索引 |
| `sync_knowledge` | 显式启用 Chroma 后同步数据库知识；非默认初始化环节 |
| `export_spec_display` | 从规格定义生成前端中文展示字典，`--check` 只核对一致性 |

旧开发维护入口 `import_compact_catalog`、`import_pc_part_dataset`、`seed_demo`、
`seed_user_isolation` 保留在 develop，可更新已有记录，不属于新 Demo 的安全空库入口。
商品写入统一复用 `demo.catalog_store`；`seed_demo` 的知识部分改为读取正式知识 JSON，
遇到同标题但不同正文/元数据时拒绝覆盖，原有数据库不会被自动迁移成新政策。
主发布线整理时将移除旧入口；不要再为新安装文档增加它们的调用。

测试和评测分别在根 tests、evals，命令集中在 `Makefile.development`。
普通运行不依赖测试目录，不以真实初始化作为调试准备步骤。
