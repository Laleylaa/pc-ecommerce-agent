# 离线测试

从项目根运行，使用已有 `backend/.venv`；不安装依赖、不启动服务、不读取项目真实 `.env`。

| 入口 | 范围 | 模型 / 数据库 |
| --- | --- | --- |
| `make agent-loop-test` | `agent_loop/business`、`smoke`、`contracts` | 模拟模型、模拟数据 |
| `make tools-test` | 工具规则、查询构造、权限、故障 | 替身；少量 SQLite `:memory:` 合成表 |
| `make application-test` | API、认证、记忆、基础设施及入口选择 | 模拟 HTTP transport、repository、服务 |
| `make backend-test` | 上述三个集合及 Ruff | 全部离线，无收费调用 |
| `make legacy-test` | 旧 Graph / DAG / 规划 / 恢复专属检查 | 显式、离线、默认不收集 |
| `make check` | 默认隔离测试、Ruff、前端构建 | 不包含 legacy、integration、模型评测 |
| `make integration-test ARGS=--isolated` | 独立 PostgreSQL 验证 | **真实数据库，会写入；本轮未执行** |

单文件：`backend/.venv/bin/python tests/run.py tests/tools/catalog/test_catalog_search_tool.py -q`。
默认配置只有根 `pytest.ini`，`backend/tests` 不再是入口。`tests/integration` 不参与默认收集。
pytest 不自动加载环境第三方插件，只显式加载已审查的 `pytest_asyncio.plugin`。

`tests/run.py` 在收集前装载 `support/offline.py`，阻断 socket/DNS、SQLAlchemy 实际连接、asyncpg；
不读取仓库 `.env`，清除继承的密钥环境配置，使用空 key / 无效数据库目标。
Python 子进程继承 `sitecustomize` 保护；TypeScript/SSR 子进程使用 `node_offline.cjs`。
基础设施 shell 用例仅执行临时目录内的 Podman/curl/sleep 替身。网络标记直接报错，不提供放开保护的测试开关。
保护不能替代入口和 fixture 审查：新增 subprocess、插件或真实服务用例必须先审查。

共享替身在 `support`，loop 专属替身在 `agent_loop/support`；评测和测试共用的合成领域数据在
`evals/support`。不互相导入测试函数。混合文件已按函数拆分，旧规划专属检查保留在 legacy，
业务断言没有删减。逐文件迁移清单随历史 ZIP 封存，摘要见 [历史档案与恢复](../README.md#history-archive)。

真实 PostgreSQL 入口要求 `AGENT_LOOP_TEST_DATABASE_URL` 指向入口认可的隔离目标；其准备、写入、
事务和清理都是真实数据库操作，回滚也不算只读。本轮禁止执行。模型评测及成本比较见
[evals](../evals/README.md)，不属于 pytest 默认检查。

隔离通过仅证明模拟条件下的行为；不代表真实模型效果、SQL/数据库持久化或页面验收通过。
旧测试说明随历史 ZIP 封存（包内 `archive/test-layout/`），
不能作为本轮成绩。P3 10/11、P3A 11/12 未放行，P4 Trace 未实施。
