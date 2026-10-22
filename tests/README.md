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
不能作为本轮成绩。P3 10/11、P3A 11/12 未放行，P4 核心已接入；平台及页面范围见下方。


## P4 Trace 验证

`tests/agent_loop/contracts/test_local_trace.py` 使用内存导出器、MockTransport和内存repository；
覆盖请求/工具/内部模型父子关联、实际输入输出、参数拒绝、两次失败及补查、并发隔离、
脱敏/截断/即时快照、未知Token、回答修正、工具取消、失败导出、忽略环境中的云端地址及认证头。
所有默认检查仍通过 `tests/run.py`，真实网络和数据库保护不变；离线入口强制默认关闭新追踪开关。

`make trace-demo` 是单独显式入口，不参与默认收集；保留离线保护，**只在该进程允许127.0.0.1:6006**。
使用同一正式runtime和受控合成场景，对照开关追踪的答案/调用次数，经正式批处理导出器发送，
并用本地平台只读API确认入库。`--offline`仅使用内存采集器，不发送；可执行：

```bash
PYTHONPATH=backend:.:tests backend/.venv/bin/python -m tests.trace_demo --offline
```

报告在忽略的 `artifacts/tests/phoenix-demo/`。平台离线时演示业务仍通过，但导出验证入口明确以非零退出，
不能把导出失败当作完整记录。页面需要手动按根README检查；API核验不是浏览器验收。
本轮未运行真实LLM、商城数据库、integration、legacy测试或商城聊天；前端未改动，不重跑构建。

OpenInference 展示回归覆盖：实际请求与角色消息/文本块逐项对应、混合返回动作分类、工具调用关联、回答材料、脱敏与字符/属性限额；不依赖在线 Phoenix。显式平台演示另核验接收端的消息字段与标题。分类只表示模型提交内容，不替代校验结果或效果评测。

工具观测补充：追踪契约验证实际CatalogSearchPlanner的非法复核路径→反馈修正→批准→查询，比较追踪开关时答案、模型及repository调用数；验证筛选计数和16000字符材料投影事件。模拟repository不连接业务数据库。显式平台演示另核对程序事件入库，报告仍只保存摘要。

Metadata摘要验证：复用追踪场景核对关键动作、返回数量、拒绝及复核结论；保留原始消息逐字段断言，验证摘要独立限长、脱敏及快照。平台API将metadata展平为metadata.*，显式演示按接收端格式核验。

关键校验节点验证：计划校验、每次复核输出校验及工具输出契约校验作为工具下独立CHAIN节点，核对通过/拒绝、父子关系及来源模型ID；非法复核路径修正逐次保留，工具返回不合契约时追踪开关不改变错误结果。其他程序判断仍核对Events，不新增普通函数节点。
