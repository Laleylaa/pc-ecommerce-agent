# 共享问题台账

## 当前状态与证据边界

更新：2026-10-19；适用 `feat/orchestrator-catalog-integration` 当前未提交工作区。本页主线只保留 **13项未关闭事项**：2项待修复/接入、8项待验证、3项待决策/评估。

2026-10-19收尾基线：纳入执行详情展示、SSE退役与普通聊天评测适配，修正8项未跟随工具语义审核更新的反馈测试。最终完整隔离回归1825通过，改动文件Ruff、前端构建及diff检查通过；命令和首轮失败见[测试说明](../../backend/tests/README.md#本次提交的验证边界)。未执行真实模型、配置数据库或浏览器验收，现有待验证事项保持开放。

CS-06、CF-04、CF-05按已修复的具体缺陷关闭；QA-01、CS-04、CS-05也移出活跃清单。关闭范围、证据和原锚点保留在页尾折叠记录。工具真实环境与语义稳定性由TP-05承接，结果贯穿消费端由TI-02/RV-03承接，不因单工具通过就升级整个模块基线。完整迭代与评测记录见[评测说明](../../backend/evals/README.md#正常购物工具评测)，不在台账重复每轮流水。

本轮OL-01修改及相关隔离回归1050项通过（含16项新增业务场景），Ruff/diff检查通过。没有调用真实模型、数据库或聊天/浏览器。此前真实模型36/36、错误初稿修复4/4属于历史证据，不代表本轮候选功能的真实链路验收。

## 当前索引

| ID | 剩余工作 | 类型/状态 | 主责（配合） |
| --- | --- | --- | --- |
| [OR-08](#or-08) | 确定性兜底合并重复失败提示 | 缺陷／待修复 | 回答收尾（结果投影） |
| [OR-06](#or-06) | 订单评测适配器真正执行filters | 待接入 | 评测维护（订单工具） |
| [TP-05](#tp-05) | 工具语义复核扩大样本及真实链路验收 | 验证缺口／待验证 | 工具规划（编排/评测） |
| [OL-02](#ol-02) | 核对订单时间筛选与实际存储时区 | 验证缺口／待验证 | 订单工具/repository |
| [KR-02](#kr-02) | Web真实服务、政策冷启动和来源展示 | 验证缺口／待验证 | 知识工具（API/前端） |
| [OR-04](#or-04) | 验证异常收尾的真实事务结局 | 验证缺口／待验证 | Runtime（API/repository） |
| [RV-03](#rv-03) | 补齐已确认范围的联合验收 | 验证缺口／待验证 | 编排（工具/API/前端） |
| [RV-05](#rv-05) | 验证分页限制、数量与对象组合 | 验证缺口／待验证 | 编排（上下文/工具） |
| [TI-02](#ti-02) | 验证v3结果贯穿记忆/API/页面 | 验证缺口／待验证 | 结果消费（前端/工具） |
| [UI-01](#ui-01) | 执行详情的浏览器与真实聊天验收 | 验证缺口／待验证 | 前端（API/编排） |
| [RV-06](#rv-06) | 替代候选允许多大价格偏离 | 待决策 | 商品规则（回答侧） |
| [TC-03](#tc-03) | 其他工具输出的一致性约束范围 | 待决策，用户暂缓 | 回答侧（工具/schema） |
| [PI-01](#pi-01) | 是否需要整体模型输入契约 | 工程整理／待评估 | 编排（上下文） |

先完成明确缺陷和接入；验证事项按条目关闭条件执行，业务决策不作为程序缺陷擅自实现。

## 未完成事项

<a id="or-08"></a>
### OR-08：确定性兜底仍逐任务重复失败提示

- 发现方：E37真实追问，原归RV-03；E51拆分。主责编排回答收尾，配合投影。
- 改善：E50已经保留校验通过的unavailable回答，不再因业务未完成而强制替换模型说明；本次回答/guard回归通过。旧问题中“正常失败回答必被覆盖”已经解决。
- 剩余：[fallback_goal_response](../../backend/app/agent/responses.py)仍逐goal/task追加同类失败文案，标题直接取补全后的goal.query前100字；其中可能包含编排补全的SPU标识。当前触发限定为模型生成失败或终态拒绝等确定性兜底，不能沿用旧正常路径声称E50后已真实复现。
- 证据：原E37追问链路及E38中间失败，记录于本地`backend/evals/results/live-smoke-2026-10-05.json`和`identity-repair-2026-10-05.json`；本次源码确认同一生成函数仍无去重。原记录的具体会话以产物为准。
- 关闭条件：同因提示可合并但不丢目标/事实/不可用部分；区分用户主动提供的编号和内部补全标识，不一律删编号；保留guard只校验、零重试。先用既有失败收尾场景核对，再按授权决定是否补新用例或真实复测。

<a id="or-06"></a>
### OR-06：订单评测适配器尚未支持filters

- 发现方E7/E33，E52处理旧测试；主责评测维护，订单工具配合。
- 已完成：四个旧编排测试文件从59通过/29失败到84通过；26项原失败迁移、4项过时/重复场景删除（含1项原通过）。有效覆盖及删除承接见[本次报告](../../backend/evals/goal_planning/test-contract-migration-20261006.md)，不再将29项列为待迁移。
- 剩余：[OrderRows](../../backend/scripts/goal_tool_fixtures.py)的count_orders/list_recent_orders_page仍不接受filters；旧live适配器不能验收条件订单SQL。本次未改该适配器，已有正式repository内存SQL测试不代表其修好。
- 关闭条件：适配器真实执行正式筛选，复用或接入隔离SQL入口；验证条件计数/列表一致、同商品项和用户隔离，不能仅添加参数后忽略filters。无需重新迁移已通过的四文件，也不因适配器旧就删除仍有效的评测场景。

<a id="tp-05"></a>
### TP-05：工具语义反思闭环的稳定性与联合验收

- 类型/状态：验证缺口，待验证；发现方用户设计讨论及真实模型对照；主责工具规划，配合编排/评测。适用本页版本。
- 已实现：[内部规划](../../backend/app/tools/structured_planning.py)接入程序初验→独立语义审核→有界修正→程序终验，搜索Prompt v21共用few-shot与[正式匹配解释](../../backend/app/tools/catalog_fields.py)。两次纠错、一次传输重试、最多七次模型调用共用60秒，外层75秒；[超时诊断](../../backend/app/core/llm_calls.py)区分本地截止及底层明确超时，未知来源不猜测。确定性编号路径沿用原规则。
- 最小接入要求：`planning_review_inconclusive`/`planning_output_invalid`终止，不返回假空结果、不由编排重启修复；审核的原文证据及计划路径也需程序检查，认证身份不由模型改写。`tool_review`独立计数；仅批准并终验通过的计划可在单次调用内复用，动态事实重查。
- 已有证据：[反思回归](../../backend/tests/catalog/test_tool_plan_review.py)、[超时回归](../../backend/tests/catalog/test_tool_transport_diagnostics.py)包含于最近955项通过集合；[评测入口](../../backend/scripts/eval_catalog_shopping.py)的`--acceptance`36/36、`--review-probes`4/4。正常请求均一次生成＋一次审核，无重试/超时，均值2.488秒。首轮曾有审核误改和三次超时，完整记录见[评测说明](../../backend/evals/README.md#正常购物工具评测)，不因最终小样本通过删除失败证据。
- 剩余/关闭：扩大独立问法样本，检查漏判、误改及可接受的延迟/失败率；在明确目标的真实只读数据库和聊天链路验证字段、身份、同SKU条件、错误交接及展示。模型可能共同误解，程序终验不证明任意自然语言完全正确；CS-06/CF-04/CF-05局部缺陷已关闭，不重复挂为待修。

<a id="ol-02"></a>
### OL-02：订单时间筛选的实际存储时区未验

- 发现方E28，主责订单工具/repository，编排配合。旧“没有时间/状态筛选却完整支持”已被实现和隔离证据取代。
- 当前[orders工具](../../backend/app/tools/orders.py)及[repository](../../backend/app/repositories/orders.py)复用OrderFilters，覆盖创建/付款/发货时间、半开区间、状态、同订单行商品词；先过滤再排序/分页，所有入口检查user_id。本次tool_planning_repairs中内存SQLite及调用方验证通过。
- 剩余：aware边界转Asia/Shanghai本地naive，实际PostgreSQL历史写入时区尚未确认；不能以“最近订单成功”证明区间筛选正确。
- 关闭条件：只在获准环境核对存储约定与边界记录，验证区间起止、计数/列表一致和归属；OL-01匹配集合及OR-06旧适配器另计。不是等待任意日期表达永不误解。

<a id="kr-02"></a>
### KR-02：外部搜索与政策冷启动/来源展示验收

- 发现方E34，主责知识工具，编排/API/前端配合。[WebSearchToolService](../../backend/app/tools/web_search.py)为Tavily basic，政策为本地hybrid；无索引可降级bm25，错误分类与来源边界不同。
- 本次web_search、policy_retrieval_runtime通过，覆盖mock HTTP及受控线程超时/取消。历史E44 run1426两个policy_search实际hybrid并完成HTTP/SSE；run1431外部搜索未配置，按错误收尾。因此删除旧“完全没有真实政策检索”的说法。
- 剩余/关闭：真实Tavily成功及来源质量、供应商失败反馈；本地embedding冷/热运行超时；API到浏览器链接和来源标识。Web资料不能当本店政策/订单/库存事实。未配key是环境阻塞，不单列程序bug；本次未调用服务商。

<a id="or-04"></a>
### OR-04：异常收尾的真实事务结局未验

- 发现方E7，主责Runtime，API/上下文/repository配合。类型已由“同步与SSE收尾缺陷”改为验证缺口。
- [graph.py](../../backend/app/agent/graph.py)已共用最后完成状态捕获及有界清理，失败rollback后尝试失败审计，二次故障不遮蔽原异常。当前[收尾测试](../../backend/tests/orchestration/test_answer_finalization.py)和runtime audit通过；E44～E48历史正常HTTP/SSE保存亦有证据。
- 剩余/关闭：在明确隔离数据库验证工具后取消、提交失败、提交确认中断、二次清理异常的实际事务结局、失败run留存及普通HTTP取消/失败收尾。假事务通过、正常回答保存都不能替代；本次未接数据库。

<a id="rv-03"></a>
### RV-03：联合验收尚不完整

- 发现方早期联合评测，主责编排，工具/API/上下文/前端配合。当前不是“尚未做真实E2E”。
- 历史E38真实HTTP/SSE已验证两款鼠标追问及名称＋SPU身份交集；E44正式10请求验证并发商品任务、政策同波串行、自然恢复和正常保存，同时保留业务失败。E45最近订单能力说明改善，E46标题/规格/候选范围改进，E48 run1443一款显示器＋最近订单正常执行保存。每次只证明其场景，HTTP200/completed/trace完整不等于业务全通过。
- E47推荐排序与LLM3 scope/thinking实验含历史输入的新模型重放，不计作新聊天E2E；推理未接正式配置。用户接受某次“这款”指向刚才几款，不再把该例列缺陷，也不推广成任意歧义可扩范围。
- 历史报告目录均在`backend/evals/results/agent_traces/`：`chat-e2e-20261005-e44-review`、`chat-e2e-20261006-llm1-capabilities`、`chat-e2e-20261006-title-scope`、`recommendation-contract-20261006`、`chat-e2e-20261006-limit`；按report/audit/manifest读取，不改旧trace。
- E54已更新 LLM3 精简表达约束和候选范围标注方式，前端固定通用提示并修复 Markdown 内段落套框、支持表格滚动；102项既有隔离检查、Ruff及前端构建通过，无新增用例。实现见 `backend/app/agent/prompts/observation.py`、`answer_facts.py`、`response.py`、`answer_context.py` 及 `frontend/src/components/ChatPanel.tsx`、`MarkdownContent.tsx` 和样式。
- 剩余：E50 guard及E53部分事实保留改动后的真实回答/收尾尚未验证；E54真实回答的简洁性、具体限制不遗漏及浏览器窄屏/表格展示尚未验；E55已统一“小帕瓦”身份、自然话术和静态文案，138项既有隔离检查、Ruff及前端构建通过，真实自我介绍与日常回应效果未验；异常事务见OR-04，真实Web及冷启动见KR-02，页面与v3见TI-02，分页/数量边界见RV-05，确定性兜底残留见OR-08；LangSmith服务端接收/页面未验。按获准场景逐项验收，不设无限“全场景”放行门槛，不另列纯模型偶发语义缺陷。

<a id="rv-05"></a>
### RV-05：分页能力边界及数量/对象组合

- 主责编排，配合上下文/工具；发现方历史边界评测。当前商品/订单均不开放用户分页控制，旧游标读取和记忆已移除，历史快照兼容丢弃游标；内部窗口不代表用户可翻页。
- 既有隔离覆盖保留第二款/第二笔序号、删除续查游标、拒绝公开offset。本次goal_task_integration及answer_context相关断言通过。E48已统一显式N必须传limit=N，历史run1443确实传1且返回1型号、订单成功；删除“该原句还未复测”的旧状态。
- 剩余/关闭：真实表达的下一页/第二页及混合目标只处理相关澄清；序号、显式/缺省/超上限数量不混淆；对象缺失、歧义、错误hint及异常传播组合按独立预期检查。单例通过不证明漏limit永久消失，本次没有新模型评测。

<a id="ti-02"></a>
### TI-02：v3结果贯穿消费端的验收

- 来源旧迁移清单，主责结果消费，配合工具、上下文/API/前端；非新生产缺陷。
- 已接入：Artifact/answer_context保留status/reason/action/field_issues，候选series与型号model范围分开；本次answer_handoff和goal_task_integration覆盖确定性交接。历史E46/E48有真实候选范围和数量单例。前端类型已有candidate_sku_ids/series字段，不再表述为消费方完全未接入。
- 剩余/关闭：沿exact/alternative/object_facts/缺资料/失败结果核对工作记忆→下一轮→API→页面，确认不依赖旧candidates/identity_only/matched_sku_ids商品语义；历史兼容读取说明用途。先外层ok/error，再业务状态，资料缺失不等于能力不支持；total_match_count是严格集合，不用内部窗口统计承诺分页。浏览器和全类结果贯穿仍未验，复用现有隔离覆盖，勿重建一套规则。

<a id="ui-01"></a>
### UI-01：聊天记录执行详情的浏览器与真实聊天验收

- 10月14日展示细化：外层移除intent和资料数标签，固定显示本轮商品/订单数；执行详情摘要增加参考资料数，展开显示来源及标题/摘要，历史沿各轮metadata恢复。本次前端构建和diff检查通过；浏览器验收还需核对折叠摘要、多轮资料归属及HTTP(S)链接。

- 发现方：用户页面测试；主责前端，配合API/编排。适用`37c2337`加当前工作区。
- 已实现：每条聊天记录折叠展示子目标、工具调用及分阶段模型调用次数；新请求`execution_details`与assistant metadata共用投影，旧记录显示未记录。调用、复用及回答引用分别标注。模型次数含工具内部规划、失败与重试，不代表内部思考步数。
- 源码：[展示组件](../../frontend/src/components/TurnExecution.tsx)、[前端接入](../../frontend/src/App.tsx)、[响应契约](../../backend/app/schemas/chat.py)、[执行投影](../../backend/app/agent/execution_details.py)、[次数收集](../../backend/app/core/model_usage.py)。模拟预览位于`frontend/dev/execution-preview.html`，启动Vite后访问`/dev/execution-preview.html`；仅模拟数据，不作为真实验收。
- 本轮证据：158项既有隔离检查及[6项新增检查](../../backend/tests/orchestration/test_execution_details.py)通过，Ruff、前端构建和diff检查通过；没有真实模型/配置数据库调用。浏览器检查受电脑控制权限阻挡，未确认视觉/点击效果。
- 关闭条件：在获准环境核对多目标、工具重试、模型纠错、无工具回复及历史重载的数据归属；浏览器确认展开/收起、窄屏和更早记录入口。首版在回复完成后展示，整轮异常/取消及实时更新尚未接入，是否扩展另按用户测试需求确认。

<a id="rv-06"></a>
### RV-06：替代候选价格偏离幅度待决策

- 发现方早期人工评测，主责商品业务规则，回答侧配合。当前替代规则限制违反一个条件维度，没有额外金额/比例阈值；不是已证实现错误。
- 历史E44/E48“10元以内4K显示器”仍出现数百元替代品；正确说明超预算可满足当前契约，但推荐价值有限，不得称满足原预算或默认用户接受。
- 关闭条件：决定是否设置金额/比例及适用范围；若保持现状，记录接受理由及话术边界。规则变更后才安排对应独立边界验收，本次不擅自改预算策略。

<a id="tc-03"></a>
### TC-03：其他工具输出的一致性约束范围

- 发现方E28人工handler探针；主责后续回答侧，工具/schema配合。E34用户决定暂缓，后续评估；不是本轮必须实施的schema扩展。
- 部分schema未约束result_type与集合/计数的全部关系。历史人工handler可造empty但有items或documents但空列表，不宣称正常handler实际产生过这些结果；不能据构造数据推定线上故障。
- 当前仅保留决策：哪些确定性不变量应由工具schema负责、哪些内容矛盾交LLM3判断。若恢复讨论，先核对真实handler可达路径及证据，再确定约束和测试；最终guard职责已收紧，不能再借本项让guard恢复重试。

<a id="pi-01"></a>
### PI-01：模型整体输入载荷仍用动态字典

- 发现方早期规划审查，主责编排，上下文配合。[goal_planning.py](../../backend/app/agent/goal_planning.py)仍在调用处拼装payload/context_objects；输出和工具参数已有schema，不能说“输入输出都没校验”。
- 工程风险是生产/消费字段漂移，尚无证据证明现有业务失败由此造成；不是阻塞业务验收的确定性bug。
- 关闭条件：基于实际字段边界决定是否需要整体输入模型，或记录沿用动态载荷的理由及字段一致性保证；不复制完整聊天状态、不预建框架。

## 已解决记录

<details>
<summary>展开7项关闭记录（本轮关闭OL-01）</summary>

保留原ID和证据，不再进入活跃索引；复发时补新证据再重新打开。具体工具契约已在提供方迭代和源码维护；本页关闭不代表已提交、部署或完成真实业务链路验收。

<a id="ol-01"></a>
### OL-01：订单匹配结果交接与待确认候选（已关闭）

2026-10-19用户明确规则后实现。发现方E19，主责订单工具，编排/记忆配合。完全匹配的`match_orders`明细窗口可供明确对象选择后的下游绑定，不要求窗口穷尽；不再按operation统一阻断。语义反思、程序计划校验、本人归属及缺条件阻断保持。

严格查询零结果时，仅对含购买商品条件的查找，保留所有商品词，每次放宽时间范围或状态之一（先时间，最多两次候选查询），返回独立`order_suggestions`。逐候选`differences`区分不符/未知，`suggestion_filters`保留实际执行条件，候选数量与严格匹配数量分离。计数、明确订单号、无商品锚点及未解决条件不自动放宽。候选不能作为本轮依赖，编排询问确认；记忆只存待确认引用和差异，不自动设为当前订单。用户下一轮明确选择后重新查询动态事实；不支持猜型号或无限放宽。

证据：[工具实现](../../backend/app/tools/orders.py)、[绑定](../../backend/app/agent/goal_planning.py)、[候选业务回归](../../backend/tests/orders/test_order_suggestions.py)、[实际工具交接回归](../../backend/tests/orchestration/test_planning_handoff.py)。本轮订单/编排/记忆及规划修复、反思相关1050项隔离通过，含16项新增；Ruff/diff通过，无失败/跳过/排除。真实模型、数据库与聊天未重验，继续[TP-05](#tp-05)/[RV-03](#rv-03)；时间存储问题仍属[OL-02](#ol-02)，不因本项关闭宣称已解决。

<a id="cs-06"></a>
### CS-06：组合连接能力被误判冲突或放宽为OR（已关闭）

2026-10-19按局部缺陷关闭。主责商品字段/搜索，目录配合；[字段规则](../../backend/app/tools/catalog_fields.py)统一组合能力、冲突与匹配，AND/OR由结构化计划、few-shot和语义复核保留。证据：[购物回归](../../backend/tests/catalog/test_catalog_shopping_requests.py)、[反思回归](../../backend/tests/catalog/test_tool_plan_review.py)及[评测脚本](../../backend/scripts/eval_catalog_shopping.py)的同时/任选/否定/三模原句与改写；最近955项相关隔离检查、真实模型36/36及修复4/4通过。更大样本与真实链路继续[TP-05](#tp-05)。

<a id="cf-04"></a>
### CF-04：目录需求根因被泛化、同源要求跨类混记（已关闭）

2026-10-19按局部缺陷关闭。主责目录规划/校验，共享要求归类配合；[结构化要求](../../backend/app/tools/catalog_search/requirements.py)及[目录校验](../../backend/app/tools/catalog.py)分别处理字段能力不足、对象缺失、用途待澄清和真实不支持，同源跨类冲突反馈修复，不靠自由文本猜错误码。证据：[购物回归](../../backend/tests/catalog/test_catalog_shopping_requests.py)及[真实模型脚本](../../backend/scripts/eval_catalog_shopping.py)覆盖无线充电、办公混合要求、无对象指代与改写，最近相关验收通过。旧声明列表仅保留历史兼容；开放语义与消费链路继续[TP-05](#tp-05)/[TI-02](#ti-02)。

<a id="cf-05"></a>
### CF-05：对象未定位时目录仍声明结果完整（已关闭）

2026-10-19按局部缺陷关闭。主责目录输出，结果消费配合；[目录执行](../../backend/app/tools/catalog_facets.py)对未定位对象返回`matching_complete=false/is_exhaustive=false`，区分具名对象需搜索定位与无名指代需明确对象。证据：[购物回归](../../backend/tests/catalog/test_catalog_shopping_requests.py)、[阶段回归](../../backend/tests/catalog/test_catalog_query_stages.py)及真实模型原句/改写通过，未执行结果不宣称已穷尽；页面和下一轮贯穿继续[TI-02](#ti-02)。

<a id="cs-04"></a>
### CS-04：标题错放筛选字段被归为资料不足（已关闭）

主责商品计划校验；[validation.py](../../backend/app/tools/catalog_search/validation.py)将已知字段位置错误归为invalid_plan并有界修复，未知能力仍为filter_data_unavailable。关闭证据：[阶段回归](../../backend/tests/catalog/test_catalog_query_stages.py)保留历史title错误、修复成功/耗尽及无查询保证；[正式契约](../../backend/app/tools/contracts.py)保留phase与反馈。真实回答和页面继续[RV-03](#rv-03)/[TI-02](#ti-02)。

<a id="cs-05"></a>
### CS-05：商品拆分主原因与重复要求（已关闭）

主责商品工具；合法split_required不再按单对象校验，同源要求由[requirements.py](../../backend/app/tools/catalog_search/requirements.py)归一化。证据：[阶段回归](../../backend/tests/catalog/test_catalog_query_stages.py)及历史14/14真实搜索复验通过，完整原记录见[评测入口](../../backend/evals/README.md)。历史品类“无匹配”曾被撤回缺陷判定，未作为修复项；关闭限拆分原因与重复诊断。

<a id="qa-01"></a>
### QA-01：SSE跨任务关闭异常（按功能退役关闭）

2026-10-18按用户决定退役SSE关闭，未修复旧生成器异常。`/api/chat/stream`、run_stream及闲置前端实现已删除，评测改用普通`/api/chat`；当时1764项隔离通过、30项写库依赖排除，后续用户另行删除数据库测试集合。依据：[退役记录](../../backend/evals/test_maintenance/sse-retirement-20261018.json)、[测试入口](../../backend/tests/README.md)。普通聊天真实业务与异常事务继续[RV-03](#rv-03)/[OR-04](#or-04)。

</details>

## 已删除条目的历史链接

<details>
<summary>展开更早条目的兼容锚点</summary>

以下仅保留原锚点，供旧iteration/报告引用；不是待办，也不保留相互矛盾的历史正文。E51关闭范围、核验文件与未覆盖边界见[核验报告](../../backend/evals/goal_planning/issues-review-20261006.md)。
<a id="or-01"></a>
<a id="or-02"></a>
<a id="or-03"></a>
<a id="or-05"></a>
<a id="or-07"></a>
<a id="cs-01"></a>
<a id="cs-02"></a>
<a id="cs-03"></a>
<a id="cf-01"></a>
<a id="cf-02"></a>
<a id="cf-03"></a>
<a id="cf-06"></a>
<a id="cf-07"></a>
<a id="cf-08"></a>
<a id="cf-09"></a>
<a id="tc-01"></a>
<a id="tc-02"></a>
<a id="kr-01"></a>
<a id="vf-01"></a>
<a id="rp-01"></a>
<a id="rp-02"></a>
<a id="cc-01"></a>
<a id="gi-04"></a>
<a id="tp-04"></a>

<a id="tp-02"></a>

TP-02于E52关闭，依据见[正式契约与回归核对](../../backend/evals/goal_planning/test-contract-migration-20261006.md)；仅保留历史锚点。

<a id="rv-02"></a>

RV-02于E53完成已确认口径与程序接入，见[关闭证据](../../backend/evals/goal_planning/partial-answer-20261007.md)；真实措辞验收由RV-03承接。

</details>
