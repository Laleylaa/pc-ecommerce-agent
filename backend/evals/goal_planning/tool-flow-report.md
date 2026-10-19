# LLM1 → LLM2 → 工具返回专项验证（2026-10-01）

## 范围与结论

基于 `feat/orchestrator-catalog-integration`、HEAD `c210539` 上的当前未提交开发实现。生产代码未因本轮测试修改。模型 `deepseek-flash`。沿正式 LangGraph 执行入口运行到工具执行、结果归一化与回答上下文生成；不运行 LLM3、不持久化。

复用此前 20 个主要/边界/无效请求，补充预算无严格匹配、缺失规格事实、对象不存在、历史第二候选四例。正式 **24 例**：

- **9 例**正确走无工具路径，只验证分流，不证明最终回复正确。
- **7 例**实际取得符合本轮预期的工具信息。
- **5 例**规划失败，未执行工具。
- **3 例**执行了工具，但没有取得完整目标所需的信息。

共实际执行 **14 次工具调用**：catalog_search 9、order_lookup 3、catalog_facets 1、policy_search 1。catalog_compare 被前置结果阻塞，knowledge_search 未调用；不能宣称六种工具都经过真实模型链路验证。LLM1/LLM2 共记录 49 次尝试，工具内部规划模型 10 次。另有 2 例预检，单独保存，不计入这 24 例。

这是**真实模型＋正式工具逻辑＋人工数据适配器**验证，不是纯隔离单测，也不是真实商品库评测，更不是聊天端到端验收。没有读取真实订单、聊天或数据库；人工价格、库存和政策不是实际店铺事实。检索只验证人工文档的词法检索分支，没有验证真实向量索引。

## 场景结果

| 场景 ID | 实际结果 |
| --- | --- |
| office_layout | LLM1 保留完整办公目标，tool_planning；LLM2 两次 JSON 非法，未调用工具 |
| office_paraphrase | 工位用途保留，tool_planning；LLM2 两次 JSON 非法，未调用工具 |
| office_without_facets | 调用 catalog_search；办公用途被判 unsupported，整条查询未执行 |
| named_product | 搜索取得 Keychron K8 的两个版本、各自规格、价格与库存 |
| comparison | 两次名称搜索中 K380 定位失败；比较依赖阻塞，未调用 catalog_compare |
| facets_only | 正确选择 catalog_facets，但工具内部生成不支持的 layout 字段，返回空结果 |
| recent_orders | 两个 order_lookup 均取得人工订单及物流；按用户口径允许拆分 |
| policy | policy_search 返回人工政策来源与正文片段 |
| knowledge | direct_response，允许跳过知识工具；回答准确性未测 |
| direct | 无工具分流正确 |
| ambiguous_reference | 缺少对象时澄清，无工具 |
| handoff | 人工诉求分流，无业务操作 |
| out_of_scope | 范围外分流，无工具 |
| security | 安全分流，无工具 |
| mixed | 商品搜索与订单查询均取得可用信息 |
| budget_update | 当前 300 元预算覆盖历史 500 元；返回三个严格匹配型号及对应版本 |
| two_reads | 此表达下两个型号均查到，各自版本信息保留 |
| contradictory_conditions | 矛盾条件走澄清，无工具 |
| gibberish | 无意义输入走澄清，无工具 |
| session_rewrite | 会话改写类直接目标，无工具 |
| alternative_budget | 100 元以内无严格匹配，取得四个替代型号及逐版本条件差异 |
| missing_fact | LLM2 直接填 scope 被拒绝，修复又产生非法 JSON；未验证到缺失事实返回 |
| missing_object | 同上；未验证到对象不存在的完整真实模型链路 |
| second_candidate | LLM1 重试修正身份引用；LLM2 scope 校验失败后修复出现非法 JSON，未执行工具 |

## 已取得的可用信息（均为人工数据）

1. **型号与版本**：Keychron K8 为 SPU 11，SKU 102 为黑色 USB、230 元，SKU 101 为白色蓝牙、260 元；返回最低/最高价与两个版本，不混成同一配置。
2. **更新预算**：白色蓝牙、300 元以内返回 SKU 101/201/301 对应的三个型号；缺连接规格的 SKU 501 没被当作已确认蓝牙匹配。缺失事实计数及 `matching_complete=false` 得到保留。
3. **替代结果**：白色蓝牙、100 元以内的严格结果为 0；返回 SKU 201/101/301/401 对应四个替代型号。`match_kind=alternative` 和逐版本 `condition_checks` 保留超预算差异；回答契约禁止当作严格匹配，也不代表用户已接受放宽预算。
4. **订单与政策**：人工订单 7001 含“测试包裹运输中”物流信息，数量与分页信息保留；政策检索得到来源 8001 的人工政策正文片段。

## 已确认问题与定位

### 1. 结构化修复反馈已接通，但模型仍可能修复失败

办公双工具请求两次均多出 JSON 闭合符号。其 LLM1 目标是完整 tool_planning，没有误变为 clarification。原始输出中能看到工具意图，但因校验失败不算实际工具选择成功或工具执行。

`missing_fact`、`missing_object` 的第一版是合法 JSON，但 LLM2 直接填写 `scope`，触发现有 Runtime 绑定约束；第二版又出现 JSON 错误。用户显式给了 SKU，不能把这个失败描述为模型编造身份。`second_candidate` 的 LLM1 局部修复有效，但 LLM2 最终仍失败。

位置：`app/agent/goal_planning.py`、`app/agent/prompts/planning.py`。需要分别处理结构化可靠性和公开 scope 与规划绑定约定的可理解性；本轮没有修改。

### 2. 办公用途使搜索整体拒绝，没有返回其余明确条件的商品

输入“办公室用，要白色、蓝牙、300元以内”被完整传入 catalog_search。实际内部计划保留白色/蓝牙/预算条件，同时输出 `unsupported_requirements=["办公室用"]`，理由“办公用途要求无法结构化执行”。结果为 `status=unsupported`、`reason=request_not_supported`、`query_state=not_executed`、空商品列表，匹配数量未知。

这是工具真实返回策略，不是 LLM1 澄清分类错误；也不能解释成“没有商品”。目前尚不能实现“返回明确条件候选，同时解释办公适用性待确认”。边界需要在搜索契约/工具侧明确，不能由编排重复解析规格或偷偷删除用途。

位置：`app/tools/catalog_search/planner.py`、`app/tools/catalog_search/service.py`。

### 3. 布局查询工具内部字段解析失败

LLM2 选择 catalog_facets 正确。内部模型生成 `spec_key=layout`，实际工具不支持该键，返回 `unsupported spec_key: layout`，没有布局选项。这不是人工库缺少布局：隔离验证用正式支持的 `style` 得到 80%、75%、全尺寸。

位置：`app/tools/catalog.py` 的查询规划与校验。编排不应维护第二份规格字典来补这个问题。

进一步检查回答投影：实际 `answer_context` 仅剩 `unsupported_capability`、`artifact.facts=null` 和通用说明，未保留 `unsupported spec_key: layout`。回答阶段 `tool_loop._orchestrator_messages` 只送聚合上下文，system prompt 不注入原始结果。因此具体失败原因在回答输入中丢失，可能把解析错误说成业务能力不支持。这是已确认的信息损失；最终 LLM3 是否误答/触发人工仍未验证。

### 4. 比较前置搜索的名称解析不稳

输入中的“罗技 K380 键盘”被搜索内部计划原样当成商品名称，包含类别后缀。人工库有“罗技 K380”，此次却未找到；同批 `two_reads` 的另一表达能查到。Keychron K8 前置成功，比较被 `dependency_artifact_unusable:task_2` 阻塞，没有猜测另一商品的 ID。

位置：搜索内部名称解析/匹配；编排依赖阻塞机制有效。只证明当前人工数据与该表达触发问题，不能推断真实商品库中所有名称搜索失败。最终回答上下文仅保留用户可见比较任务的阻塞信息，未提供成功前置商品的完整事实。

## 验证、安全与限制

- 三份定向测试：`test_goal_task_integration.py`、`test_goal_planning_validation.py`、`test_goal_tool_flow_fixtures.py`，**40 passed / 1 xfailed**。执行前检查 fixture 闭包，41 条选定用例中依赖写库 fixture 的排除数为 **0**；没有运行其他后端测试。
- xfail 是已知纯空白输入未拒绝；未因本次请求修改规则/模板机制。
- 新增人工数据适配器与 4 条隔离测试；fixture 的布局验证显式指定正式内部字段，验证数据与聚合，不把它冒充自然语言解析通过。
- 真实链路用 DenySession 禁止数据库操作；上下文与审计替换为无持久化实现。仓库数据加载替换，其余正式工具规划、校验、匹配、聚合、执行绑定和结果消费保留。
- 未执行 LLM3、终态回复审查、HTTP/SSE、真实持久化、真实向量检索或真实商品库评测。报告中的 memory 是纯函数投影，不是保存成功的记忆。
- Ruff 和 git diff --check 通过。未提交、推送或切分支；没有升级模块稳定基线。

## 产物与复现

- [正式 24 例原始输入、模型输出、工具结果与回答上下文](tool-flow-2026-10-01.json)
- [独立 2 例预检](tool-flow-smoke.json)
- [运行时实现指纹和人工数据快照](tool-flow-manifest.json)
- [评测入口](../../scripts/eval_goal_tool_flow.py)、[人工数据边界](../../scripts/goal_tool_fixtures.py)

从 backend 运行 `.venv/bin/python -m scripts.eval_goal_tool_flow --live --output evals/goal_planning/<new-run>.json`；它会调用真实模型但不连接数据库。请使用新文件保存复测，避免覆盖本轮证据。程序退出成功只表示评测完成；逐例失败仍需检查 planning_failures、information_issues 和人工审阅。

自动检查中的“未保留用途/工具数不同”可能是上游规划失败的派生提示，不是独立缺陷；本报告已按原始尝试去重分类。以上 24 例是小样本，不是稳定性或全覆盖结论。
