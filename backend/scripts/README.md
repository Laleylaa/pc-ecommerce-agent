# 运行与维护入口

在项目根目录使用 Makefile；这些命令不会依赖测试或开发分支。

| 命令 | 用途 |
| --- | --- |
| `make doctor` / `make config-check` | 工具与配置结构检查，不写入业务数据 |
| `make setup-local` | 首次安装、启动 PostgreSQL、迁移、空库 Demo 导入与模型准备 |
| `make demo-info` | 只读查看演示数据清单 |
| `make db-status` / `make db-migrate` | 查看迁移状态 / 应用迁移 |
| `make db-init` | 迁移后向空库导入 Demo；非空拒绝 |
| `make model-prepare` | 显式下载或预热政策检索模型 |
| `make knowledge-index` | 政策正文变化后重建本地索引 |
| `make spec-display` | 规格定义变化后重新生成前端中文展示字典 |
| `make frontend-build` | 检查规格字典并构建前端 |

应用与 Podman 统一读取根 `.env`。`local_config` 供 shell 内部读取，包含数据库凭据，
不要直接输出或记录其结果。已初始化环境复用流程见根 README；切分支不会隔离数据库。
