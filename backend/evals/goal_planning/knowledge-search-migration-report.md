# E34：政策异步检索与外设 Web Search

2026-10-04，`feat/orchestrator-catalog-integration`，基点 `8319fb1` 加既有及本轮未提交修改。保留用户工作区和暂存区，无 commit/push。范围止于工具输出及 URL 类型兼容，不实施 TC-03 业务输出一致性检查、LLM3 重试或 RP-02。

## 先确认的业务场景与独立预期

| 用户输入/边界 | 预期 | 验证层次 |
| --- | --- | --- |
| “键盘拆封后还能退吗？” | 通过 policy_search 查本店资料，不向公开网络发请求 | 正式图 + 脚本 LLM1/LLM2 + 本地人工政策 |
| “磁轴和机械轴有什么区别？” | knowledge_search 保留 query，搜索公开资料，返回来源 URL 和摘要 | 正式图 + 脚本 LLM1/LLM2 + mock HTTP |
| 搜索正常但零条结果 | ok + empty，区别于服务故障 | 工具及正式图 |
| 未填 TAVILY_API_KEY | 不发 HTTP，明确 web_search_not_configured | 工具及正式图 |
| HTTP 401/403、429/432、400/500、重定向 | 明确认证/额度/服务错误，不伪装成空结果，不回传供应商错误正文 | 正式工具边界 |
| HTTP 超时、总超时、连接异常 | 有界失败，不自动重发消耗额度 | 正式工具边界及超时正式图 |
| 供应商 JSON 缺字段/非法 URL | web_search_invalid_response，不当作证据 | mock HTTP → 正式解析/输出 |
| 默认及指定返回数量、重复 URL | 默认 3，遵守 1/2/4 等指定 limit，稳定 ID、保留 URL、去重 | 正式工具 + 历史数量回归 |
| 政策 embedding 同步阻塞 | 事件循环可继续运行，包装器及时返回 timeout | 人工慢 embedding + 正式包装器 |
| 政策超时/外部取消后立即再次调用 | 仍运行的计算占住名额，新调用返回 retrieval_busy；完成后恢复 | 真实线程 + 事件控制，不加载模型 |
| 同步检索异常 | 标准依赖错误，释放名额供下一次调用 | 正式工具边界 |
| 显式 BM25 / 空文档 / 缺失向量索引 | BM25 不加载模型/向量文件；空资料不偷偷读取默认资料；缺索引标明实际 bm25 | 隔离本地检索 |
| 本机模型缺失 | 请求内不下载；只有显式准备或索引构建允许下载 | mock 模型加载器 |

这些预期不以“调用了工具”或单独 ok 为通过标准。正式图用例检查 LLM1/LLM2 调用、规划参数原文、工具请求次数、结果类型、证据来源、失败代码；在 execute_tool_wave 后停止，不调用 LLM3。脚本模型不证明真实模型一定选对工具。

## 根因与实现

1. 原 policy_search 和 knowledge_search 都是本地 BM25 + 向量混合检索（RRF），并非只有 BM25，也不是正式走 Chroma。检索函数虽然声明 async，却同步读取文件、加载模型和计算 embedding，使事件循环上的 wait_for 无法及时超时。修复前新回归设 20ms 超时、同步计算 150ms，仍得到 ok=true，1 项失败已复现。
2. 政策保留本地数据/算法，阻塞阶段移入进程共享的单线程执行器。名额在实际线程完成回调中释放，不在 awaiter 取消时释放；8 秒正式超时，最多一个实际计算，没有待处理队列膨胀。运行中的 Python 线程不能强制杀死；持续卡死需重启后端，进程退出也可能等线程结束。这是明确限制，不宣称硬终止模型计算。
3. policy_search 仍默认 hybrid；索引缺失、版本/hash 不符等现有无向量路径转 BM25，并如实返回 search_strategy。BM25 避免多余向量加载；runtime 模型加载只允许本地缓存，显式 make model-prepare / knowledge-index 才可下载。没有改写或删除 backend/data。
4. knowledge_search 正式绑定改为 Tavily 原生异步 HTTP，复用 httpx，无新 SDK/依赖。公开参数仅 query/limit，不接受旧 document_type/retrieval_mode 或 API key。basic 固定一次调用，不用自动深度升级、生成答案或 raw_content；8 秒总截止，外层 9 秒保险。两层超时均停止自动重试，避免不确定是否扣费时再次消耗额度。
5. URL/摘要使用独立 WebSearchOutput，source_type=web_page，documents 容器与既有编排兼容；chat evidence 类型、artifact 来源与前端链接做必要接入。未改回答生成逻辑；仅调整共享工具选择说明及来源描述。
6. 根目录 .env.example / Settings / README 配置入口已同步，本机 .env 只补空 TAVILY_API_KEY，不输出/覆盖既有值。key 属服务端 SecretStr 并加入 trace 脱敏字段，缺 key 返回明确错误。

官方资料：[Tavily 价格](https://www.tavily.com/pricing)、[额度](https://docs.tavily.com/documentation/api-credits)、[Search API](https://docs.tavily.com/documentation/api-reference/endpoint/search)。当前免费 1,000 credits/月，无需绑卡；basic 为 1 credit/请求。免费是限额，并非无限使用，注册后的实际政策以服务商为准。

## 清理与迁移依据

本轮没有删除任何测试文件、历史验证产物或知识 JSON/向量索引。

- test_tools 中“两个公开工具共用本地混合检索 schema”的旧断言已限定到 policy_search；knowledge_search 的替代断言在 test_web_search，验证公开 schema、正式绑定及来源 URL。
- test_planning_execution_policy 的两个旧数量用例保留，分别改用正式本地排序与 mock Tavily 响应。初次扩大回归 2 failed / 1078 passed / 33 deselected：一项仍绑定旧本地 knowledge handler；另一项使用 object() 假索引，被新增实际策略判断访问。修正的是无效适配器，不放宽业务断言。
- eval_goal_tool_flow 的 knowledge_search 改为真实 WebSearchToolService + MockTransport，复用正式 HTTP 构造、解析与结果模型。人工网页明确是 fixture，不算真实联网验证。
- 本地 search_knowledge helper 仍被本地检索评测及历史 BM25/vector 回归调用，因此保留并明确非公开工具路径；没有因正式绑定更换就删除仍有效算法验证。

## 本轮验证

- 指定 18 个文件：**1080 passed，33 deselected**（全部为写库 fixture 及其间接依赖），无失败/skip/xfail。安全插件禁止网络和配置数据库连接。新增 32 个参数化用例见 test_policy_retrieval_runtime.py / test_web_search.py。
- Ruff、git diff --check 通过；前端 TypeScript/Vite 构建通过。构建不等于浏览器交互验收。
- 未运行：真实 Tavily、真实 LLM/embedding、配置 PostgreSQL、完整后端测试、初始化/seed、完整聊天 API/浏览器端到端。KR-02 继续跟踪真实接入；KR-01 仅关闭已复现的同步阻塞和无界后台任务风险，模型实际耗时未测。

```bash
cd backend
.venv/bin/python -m pytest -p scripts.orchestration_audit_safety \
  tests/test_policy_retrieval_runtime.py tests/test_web_search.py tests/test_tool_eval.py \
  tests/test_input_tool_contract_chain.py tests/test_agent_tracing.py \
  tests/test_goal_tool_flow_fixtures.py tests/test_catalog_query_policies.py \
  tests/test_tool_planning_repairs.py tests/test_catalog_search_tool.py \
  tests/test_llm_feedback.py tests/test_tools.py tests/test_tool_retry_policy.py \
  tests/test_planning_execution_policy.py tests/test_planning_call_policy.py \
  tests/test_goal_planning_validation.py tests/test_planning_handoff.py \
  tests/test_planning_contract_alignment.py tests/test_goal_task_integration.py \
  -q --tb=short -o cache_dir=/private/tmp/pc-chain-cache
```
