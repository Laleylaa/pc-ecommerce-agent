# Agent loop 重构测试

后续 Agent loop 修改只运行本目录新增测试，以相同口径验收。统一入口为 `make agent-loop-test`；不运行或联合收集 `backend/tests/`，不执行旧测试脚本、`make backend-test` 或包含旧测试的 `make check`。覆盖不足时在本目录补充独立预期和场景，旧测试成绩不能代替新链路验收。

2026-10-20 P3A 核心实现先复用了 270 项既有用例；用户随后授权 P3A-07—11，
此前新增70项；本轮多对象与统一结构校验再增55项，该轮395项；最新结束协议调整后 **398 项**（30 smoke／70 business／298 contracts）。SDK 调用采用 `{goal_id, arguments}` 显式归属；
覆盖跨目标合批、原子拒绝、部分失败重试、预算／取消、结果聚合及跨轮引用。
多订单前端消费验证执行实际 App 处理函数，不等于浏览器交互验收。
运行记录与质量限制见 [P3A iteration](../docs/development/iterations/agent-loop-p3a.md)。
另在运行前冻结 `support/query_batch_cases.py` 的 8 条新表达及独立语义预期，
两次修正后分别再冻结 2 条新表达（共 12 条）。已参与修复的表达只算回归，
通过既有 compare_runtimes.py 显式 `--case p3a_three_domains` 等逐项选择，
不改变默认 18 例成本集合。`review_answer_style.py --live --case partial_policy_after_empty`
复用固定交接驱动，单独检查同目标“先空后有政策材料”的回答，不是完整 runtime 或成本对照。

## 分类

| 目录 | 范围 | 当前内容 |
| --- | --- | --- |
| `agent_loop/business/` | 业务目标、事实、硬条件、多轮和权限等业务验收 | 五工具一致性、多目标／条件阻塞、多轮／记忆、部分失败、推荐／比较／订单条件／冲突来源及多查询聚合共 70 项 pytest；另有显式模型对照与隔离 PostgreSQL 入口 |
| `agent_loop/smoke/` | 最小完整链路是否能运行 | SDK、五工具、完整两阶段 runtime、跨目标合批及正式 API 共 30 项 pytest；`probe_agent_loop.py` 为显式真实模型探针 |
| `agent_loop/contracts/` | schema、证据引用、错误状态、预算、取消及资源释放 | SDK、准入／身份、目标／证据、共享预算、取消、持久化、审计、前端历史及查询控制反馈共 298 项 pytest |
| `agent_loop/support/` | 测试共享替身和合成数据 | 脚本化供应商响应及独立合成领域数据；不放测试函数 |

按主要验证目标归类，避免同一测试在多个目录复制。`smoke`、`contract`、`business` 标记分别用于筛选；新增业务 pytest 用例应放入 business 并标记 business。冒烟通过不代表业务效果通过，目录名称也不代表已经具备对应覆盖。

`pytest.ini` 配置收集与导入路径，`conftest.py` 复用离线保护插件，阻断 socket 和配置数据库访问。复用现有服务、驱动或 fixture 不意味着运行其旧测试集合。live 入口不会被 pytest 自动执行。

## 运行

使用已按根 README 安装的 `backend/.venv`，从仓库根目录执行：

```sh
# 本目录全部隔离测试与 Ruff
make agent-loop-test

# 分别运行本目录冒烟、契约或业务测试
backend/.venv/bin/python -m pytest -c tests/pytest.ini tests/agent_loop/smoke -q
backend/.venv/bin/python -m pytest -c tests/pytest.ini tests/agent_loop/contracts -q
backend/.venv/bin/python -m pytest -c tests/pytest.ini tests/agent_loop/business -q

# 显式真实模型验证，会消耗模型额度；合成工具/数据，不写业务数据库
backend/.venv/bin/python tests/agent_loop/smoke/probe_agent_loop.py --live
backend/.venv/bin/python tests/agent_loop/business/eval_agent_loop_baseline.py --live

# P3 新旧真实模型对照：18 例各 3 次，同一合成领域数据；不连接数据库
PYTHONPATH=backend:tests backend/.venv/bin/python tests/agent_loop/business/compare_runtimes.py --live --repeat 3

# P3 真实 PostgreSQL 验收：仅专属临时 localhost:15439/agent_loop_p3
# 显式写合成会话／记忆／人工请求；不要配置原业务库，--seed 仅可用于空测试库
AGENT_LOOP_TEST_DATABASE_URL='<专属临时库连接串>' PYTHONPATH=backend:tests \
  backend/.venv/bin/python tests/agent_loop/business/verify_postgres.py --isolated --seed
```

真实模型结果仍按源码定位，写入 `backend/evals/results/agent_loop/`，不随当前工作目录改变；该目录继续忽略，历史产物不改写。业务基线复用旧图作为对照，其结果不能证明新链路通过业务验收。

本目录维护测试代码与运行说明；开发过程、失败修复和每轮实际验证结果统一写在 [P0 iteration](../docs/development/iterations/agent-loop-p0.md)、[P1 iteration](../docs/development/iterations/agent-loop-p1.md)、[P2 iteration](../docs/development/iterations/agent-loop-p2.md)、[P3 iteration](../docs/development/iterations/agent-loop-p3.md)及后续对应阶段记录。

P1 业务用例运行真实领域服务，替换数据读取、模型规划或 HTTP；另检查正式订单 repository 生成的用户过滤 SQL，未连接真实数据库。固定规划下的自然语言输入验证传参和领域规则，不代表真实 LLM 表达效果通过。P1 SDK smoke 在单次工具执行后由测试 hook 停止，不是生产查询结束协议或聊天端到端。

P2 使用正式 Strands SDK、HTTP 响应替身、正式领域／会话服务及事务内存仓储，验证完整两阶段及失败收口。多轮自然表达配固定语义计划，真实模型理解效果仍须 P3 验证；本轮没有连接真实数据库或运行页面。运行结果与失败修复只记录在 P2 iteration。

P3 的 contracts 通过 Node 使用已安装的前端 TypeScript、React 和 ReactDOM；先按根 README 安装前端依赖，缺依赖会失败，不静默跳过。类型检查和 SSR 不能代替浏览器点击验收。`support/postgres_app.py` 仅用于显式隔离集成，严禁作为生产入口；其模型响应脚本化，正式 repository／认证／事务连接专属 PostgreSQL。`compare_runtimes.py` 会逐次保存原始结果；异常和未知 usage 阻止成本放行，成功返回仍需人工复核语义。

P3 编排续作新增 5 项契约，覆盖程序状态／证据反馈、一次结束修复、分阶段工具定义及隐藏工具仍需准入；人工动作交接沿用现有业务用例；前端契约还执行实际事件函数，验证无卡片／旧卡片时确认及提交的订单号一致，并保留显式确认及防重复提交。成本对照默认仍是 18 例各 3 次，`FOLLOWUP_HOLDOUT` 的 4 条新表达需显式 `--case` 选择，单独复核知识取证、人工确认、缺字段与能力说明混合政策查询，不混入冻结成本集合。

首次登记合批新增 30 项 pytest（2 冒烟／4 业务／24 契约）：真实 SDK 合批、整批拒绝与准备态回滚、前提／身份／历史指代、结束引用与一次修复、预算及取消。原始单独登记路径继续覆盖；后续控制混批仍拒绝。`BATCH_HOLDOUT` 的 4 条新表达仅显式 `--case` 选择，不加入默认 18 例成本集合。具体过程及成绩见 P3 iteration 第 8 节。

服务事件循环回归新增 24 项（8 冒烟／16 契约）：使用默认 asyncio、实际 uvloop 以及两个独立时钟起点，验证正式路由、共享绝对 deadline、HTTP 限时、工具过期／取消、失败清理及持久化。uvloop 未安装的环境明确跳过对应参数，本机本轮全部执行；其余三类不依赖 uvloop。运行过程与成绩见 P3 iteration 第 10 节。

显式复现服务循环的真实模型验证（合成数据，不连接业务库）：

```sh
PYTHONPATH=backend:tests backend/.venv/bin/python tests/agent_loop/business/compare_runtimes.py \
  --live --runtime strands --event-loop uvloop --repeat 1 \
  --case server_greeting --case recent_orders --case missing_object
```

`server_greeting` 只显式选择，默认成本集合仍为 18 例；产物记录实际事件循环类型。该入口的真实模型验证和 ASGI 隔离冒烟均不能替代浏览器＋真实数据库联合验收。

回答展示新增 3 项 smoke/test_answer_presentation.py：通过实际 runtime 验证共享客服规则进入模型
请求、回答阶段不绑定工具、段落／列表／表格在响应和持久化中完整保留，且 JSON 控制字段不泄漏。
脚本化模型不能证明自然语言效果；真实回答复核使用根 business 入口：

```sh
PYTHONPATH=backend:tests backend/.venv/bin/python tests/agent_loop/business/review_answer_style.py \
  --live --repeat 3
```

该入口将 support/answer_style_cases.py 的 14 组合成证据交给正式独立回答阶段，不执行查询、
不连接数据库；每例附独立的用户可见验收要求。采集正文、原始模型输出、实际请求次数及用量，
可据此核对协议纠错；
程序返回 model 仅表示 JSON／覆盖／引用合法，语义和可读性仍须人工复核。可用 --case 筛选；
--baseline-prompt 指定保存的旧 Prompt 文本，对其中 4 个 paired 场景进行相同证据对照。
这里的 baseline 是修改前的 Strands 回答 Prompt，不是运行旧 LangGraph；不属于整链路成本比较。
原始产物仍在忽略的 backend/evals/results/agent_loop，过程、失败和实际成绩见 P3 iteration 第 12 节。

本轮多对象验证：`contracts/test_query_schema.py` 检查严格嵌套结构、位置字段及单独／同批校验一致；`business/test_multiple_references.py` 覆盖一个或多个任务关联两款商品、部分失败、后续追问、无来源编号和两笔历史订单。模型理解不由程序校验代替。真实模型新增表达 `p3a_selected_pair`、`p3a_historical_orders` 仅显式选择，不改变固定18例成本集合。

最新结束协议：`query_complete`仅提交`completion.reason`，程序自动整理任务和结果；执行计数不代表语义完成。`business/test_query_completion.py`新增5项，原4项逐目标结束检查替换为2项生命周期检查，净增3项。历史P0探针仍只测试SDK机制，不是正式结束schema。
