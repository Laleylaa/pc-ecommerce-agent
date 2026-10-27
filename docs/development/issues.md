# 共享问题与验收状态

2026-10-26最新：E2E-PRODUCT-IDENTITY、E2E-UNSUPPORTED-GUIDANCE 已按用户确认口径修复并定向通过；复测发现的助手建议擅自成为用户条件也已修复。最新4例通过，初轮3通过1失败，E21补充复测通过；跨r5/r6，未全量重跑。

此前建立四项E2E缺陷的可接手记录。
以下“定向验收通过”仅覆盖Strands正式聊天API，不代表所有历史任务或legacy已验收。

## E2E-COUNT：用户数量被工具复核改写

- 发现：E2E B12/E08/E17；主责：编排与商品工具；配合：契约与评测。
- 根因：编排传入limit=3，工具把调用参数当成用户明确数量，复核将正确的1改成3。
- 当前实现：公开requested_count保存用户明确数量，limit为单次上限；未给requested_count时沿用limit。
  内部requested_limit为程序注入的审计值，不由规划/复核模型决定；执行器直接使用请求数量。
- 消费方要求：用户要N项时调用方传requested_count=N、limit=min(N,10)；全部用limit=10并保留原文；
  新字段适用于catalog_search/catalog_lookup，既有仅传limit的调用仍按limit选取。
- 验收：三个原失败在至少3个合格候选的独立库中均只返回1款；离线覆盖内部模型试图改数量、
  候选不足、展示上限及输出预算。实际结果不足不得补造；不以窗口改变全集统计。
- 状态：当前Strands路径定向验收通过；legacy真实链路未复测。

## E2E-FACETS：多维目录请求无法完成

- 发现：E2E B06；主责：编排；配合：facets规划/复核与执行。
- 根因：单facet计划被要求同时完成品牌和轴体两个维度，内部反复修正耗尽。
- 当前实现：编排按维度拆调用；工具计划split_required=true时返回multiple_targets/split_tasks，
  不查询一部分冒充全部。复核说明单计划表达边界；组合价格库存仍可由lookup一次查询。
- 验收：原问题真实拆成品牌/轴体两次成功调用；离线证明split计划不会读取商品仓储。
- 状态：定向验收通过。

## E2E-ORDER-CARDS：订单明细未生成卡片

- 发现：E2E B07；主责：loop响应投影；配合：会话持久化与工作记忆。
- 根因：analysis_orders未被响应组装消费，仅消费order/candidates。
- 当前实现：按真实明细生成OrderSummary，保留工具顺序并按订单ID去重；同步历史和上下文。
  商品查询预算投影同时保留category，避免后续会话丢失品类。
- 验收：最近三单卡片、正文、历史一致；第二笔指代可重新查询；离线校验候选上下文。
- 状态：定向验收通过。

## E2E-ORDER-NAMES：紧凑名称漏查已购订单

- 发现：E2E E16；主责：订单查询与仓储匹配；配合：schema、编排/回答。
- 根因：测研茶语K1键盘被整串包含匹配，无法命中测研 茶语 K1 机械键盘。
- 当前实现：程序规范大小写/全半角/布局、拆中英文片段，同一订单项满足所有片段；
  英文数字型号保持边界，K1不精确匹配K10/K1Pro。LLM保留品牌/型号/品类，不改数字。
- 无精确结果时：保留本人、时间和状态，最多扫描200笔订单，名称相似只返回待确认建议，
  differences.field=product_terms记录原要求和实际名称；不写入精确结果/焦点订单。
  未扫描全集时明确非穷尽，建议数只计扫描范围；不对明确订单ID、计数或不支持要求放宽。
- 消费方要求：相似建议不能当作已选订单；沿用pending_suggestions与确认流程。
- 验收：E16同一自然表达成功找到9001；E19不同型号只给本人K1建议并请确认；
  独立PostgreSQL只读检查紧凑/拆词名称命中、K10/K2不精确命中K1。
- 状态：定向验收通过。单轮重复次数为1，不代表长期稳定性或全部名称变体覆盖。

## 前轮修复验证范围

相关离线671项通过，真实E2E12例14轮全部通过（含6个原失败），另有4项真实只读SQL检查通过。
没有修改共享商城业务数据；独立库及原始批次保留，无自动提交/推送。
可执行回归入口：tests/e2e/isolated.py --remediation；用例/标注见tests/e2e/business与boundary。
本地运行产物在artifacts/evals/e2e/fix-20261026，忽略产物不作为共享接入的唯一依据。

## E2E-PRODUCT-IDENTITY：并列商品编号与后置类型说明被误拦截

- 状态：当前 Strands 定向验收通过；legacy 未修改或复测。
- 发现：B17；主责：编排与商品计划校验；配合：契约和评测。
- 根因：编号提取正则只识别紧邻前置标签的数字，漏掉并列999999及后置类型的101，并以来源不足拒绝整个查询。
- 当前实现：公开商品编号由模型理解、正式工具查询，移除当前loop、scope与plan的编号语义门禁。保留字段类型、结构、受控引用、权限与私有订单来源约束；仍禁止模型把型号猜作SKU。
- 验收：B17两轮实际查询101、999999及SPU10，返回真实价格库存、版本与明确未找到。相关离线结构/权限边界通过。
- 接手入口：`tests/e2e/isolated.py --semantics --case B17`；用例及合成事实在business/cases.json与isolated.py。

## E2E-UNSUPPORTED-GUIDANCE：缺售规格被当作资料不足

- 状态：按用户确认的完整规格口径修复，E21/E30定向验收通过。
- 发现：E21；主责：商品规划/匹配与回答；配合：编排和评测。
- 历史表现：无线充电要求返回filter_data_unavailable而不执行，回答只要求补型号。此前“不能判定不支持”的验收口径已被本次用户明确决定取代。
- 当前口径：本店目录完整；具体未销售规格通过spec_not_carried保留为不满足条件，无精准匹配时可返回明确差异的相近款，其余条件保持。办公等无法规格化的用途须澄清，不等于没有该用途商品。
- 实现：工具只执行合法字段，缺售规格通过受控声明保留为匹配证据；布尔缺项表示否，实际资料冲突仍未知，缺失测量数值不补零。回答说明无精准匹配和替代差异，不替用户接受放宽。
- 验收：E21无线充电鼠标、E30无线显示器均实际查询并返回明确标注的相近款；E20用途澄清链路通过。
- 接手入口：`tests/e2e/isolated.py --semantics`；边界标注在boundary/cases.json。E05同步完整规格标注但本轮未执行。

## E2E-ASSISTANT-CONDITION：助手建议被继承为用户硬条件

- 状态：r6定向复测通过；主责：编排Prompt；配合：暴露条件评测。
- 发现：本次E21初轮第二轮，用户仅要求100–200元有线鼠标一款，实际计划额外加入助手上一轮建议的stock>0。
- 修复：只继承用户明确提出/接受的条件；助手建议不形成用户约束。新增Trace检查防止隐性加条件。
- 验收：仅复测E21两轮，后轮计划只有price与connection_type，没有库存等附加条件。

## 本次语义修复验证范围

相关离线643通过，相邻契约/模板39通过，后续运行器/暴露检查19通过（与前述部分重叠）。
真实模型与正式API初轮4例8轮3通过1失败；E21修复后2轮通过。最新4例无失败，跨r5/r6，非同一最终版本全套重跑。
独立合成库业务数据/长期记忆前后相同，服务停止；非盲Codex语义复检。未验收legacy、浏览器或人工请求提交。
本地产物：artifacts/evals/e2e/semantics-20261026；共享接手依据为上述源码、分类用例及可执行入口。

## 新增覆盖验证范围（修复前历史）

仅新增B17–B20/E20–E29，共14例20轮，12通过2失败，0无法判定/未执行。
使用独立合成库，前后业务数据/长期记忆相同；真实聊天模型与工具。
B20是真实Web；E25是受控HTTP200空结果依赖，不表示真实搜索召回验收。
17项新增脚本离线检查通过，准备阶段21项运行器检查通过；Ruff通过。
B18订单卡字段标注order_id→id、B20官方支持域名别名检查已修正，仅重判原证据，原标注/检查保留。
本轮未修复生产失败、未运行旧35例、浏览器或人工请求提交；一次运行、Codex非盲LLM语义复检。

## DOC-COUNT-LIMIT：商品展示数量的产品要求与工具窗口不一致

- 状态：开放，文档核对发现，未调整产品上限或生产实现。
- 发现：2026-10-26架构与需求整理；主责：产品口径与商品交付；配合：工具契约、编排和E2E。
- 当前需求：本地需求文档4.3规定单次最多展示20个候选，超过上限时说明截断。
- 当前实现：`backend/app/tools/output_limits.py`的`MAX_RESULT_ITEMS=10`，公开工具limit最大10；`requested_count`保留用户数量，不能自动重复调用绕过窗口。
- 影响：不能把最多20项的产品要求标记为已实现；不能让文档和调用方误把工具limit设为20。
- 关闭条件：确认产品上限是否调整为10，或实现并接入20项交付；同步公开契约、预算说明和需求，验收用户指定数量、候选不足、超上限及结果体积受限场景。

## DOC-LOOKUP-EXAMPLES：型号事实汇总的默认示例要求不一致

- 状态：开放，文档核对发现，未改产品要求或生产输出。
- 发现：2026-10-26架构与需求整理；主责：产品交付与catalog_lookup；配合：回答与E2E。
- 当前需求：指定型号的版本存在性、数量和价格范围汇总默认附最低、中间、最高价的最多三个真实版本。
- 当前实现：`backend/app/tools/contracts.py`的catalog_lookup说明及`backend/app/tools/catalog/presentation.py`按所问事实组织汇总，不默认附上述示例。
- 影响：窗口统计与真实版本汇总仍需保持准确，但不能据现有定向通过声明默认示例要求已经满足。
- 关闭条件：明确默认示例是否仍是产品要求；保留则补齐实现及卡片/正文验收，取消则按确认后的决定更新需求。无论选择哪种方式，均保留完整版本集统计与版本归属。

## UI-RESULTS-01：用户查询结果界面浏览器验收

- 2026-10-27提交前整体验证：默认离线初检1134通过/3失败，均为应用测试仍断言旧公开错误码；按正式errors.py对齐预期并保留身份隔离/追踪事件断言后，1137项全部通过。随后修复导入排序、测试超长行及新增文件尾空行，范围无业务行为变化；全范围Ruff、规格字典同步、前端构建、预览TypeScript和暂存diff检查通过。另显式运行E2E测试装置离线检查21项通过；本次不运行真实模型/数据库、legacy全链路或浏览器验收。
- 2026-10-27统一规格展示：`backend/app/tools/catalog/fields.py` 唯一维护六品类24字段展示标签/基础单位/品类标签，`spec_display.py` 提供回答说明与展示入口；`backend/scripts/export_spec_display.py` 自动生成前端字典，商品预览/详情/选项及订单统一用 `frontend/src/utils/spec-display.ts`。本轮 AnswerInput 按实际结构化规格附带同源字段说明及 unmapped_fields，原始证据与工具键不变；legacy 标签表改为派生，未验收旧全链路。`make spec-display` 生成，`make frontend-build` 和隔离测试校验同步。142项相关隔离检查、前端构建、预览类型检查、范围Ruff/diff通过；包含30条跨语言固定展示用例和实际回答入参检查。真实模型措辞/浏览器视觉仍待验收，未调用真实模型/数据库。
- 状态：第一版已实现，待页面与真实聊天联合验收。发现方：前端改版；主责：前端；配合：用户验收、聊天 API。
- 实现：`frontend/src/components/ChatPanel.tsx`、`MessageResults.tsx`、`ProductDetail.tsx`；消息消费与历史恢复见 `frontend/src/App.tsx`。商品/订单/资料绑定每条消息；型号不混入代表版本规格。偏好/人工诉求按需打开，人工提交显式确认。后端响应与追踪不变。
- 最小接入：沿用 ChatResponse 的 products/order/orders/order_query/evidence 和会话 metadata；旧 metadata 缺字段时不补猜结果。订单追问填入输入框，发送后才重新查询。
- 2026-10-27阅读体验调整：商品默认两款紧凑预览，“查看全部”进入ProductResultsDrawer，列表/详情共用一个抽屉并支持返回。长回答定位开头，短回答完整呈现；阅读历史时不抢滚动位置，显示新回复按钮；会话切换清理商品状态。正文不裁剪，后端契约不变。
- 2026-10-27配色：白/灰/石墨底色，小面积蓝色规格标签和选中态，绿色正向状态、琥珀差异/待支付提示；移除米黄铜棕与装饰渐变。纯样式及订单标签展示属性调整，构建/diff及8组主要文字对比度检查通过；未进行浏览器视觉验收，未运行业务模型/数据库。
- 2026-10-27订单分区：`OrderCards.tsx` 详细卡为白色概览、浅灰订单详情、浅蓝物流进度；商品费用与轨迹独立折叠，最新动态常显。按有效时间倒序；时间不全时保留来源顺序且不宣称最新。color/max_dpi/weight_g 转中文，纯数值 weight_g 按字段定义补 g，已有单位/未知字段保留。构建、预览 TypeScript、diff 及前端专项隔离检查通过（1 passed / 9 deselected，含实际组件 SSR、轨迹与单位边界）；本轮无浏览器视觉/点击及真实模型/数据库验收。模拟预览 `/dev/results-preview.html?scene=orders` 可直接查看。
- 后续紧凑化：用户反馈物流区常驻仍撑高回复；消息内改为紧凑摘要，完整订单/物流移入 DetailDrawer，保留灰/蓝分区和中文规格。多单去重后仅预览两笔，“查看本次全部”打开抽屉列表，摘要原始记录可在抽屉查看。沿用长回复定位开头、关闭抽屉 preventScroll 恢复焦点；本轮未改自动滚动逻辑。构建、预览 TypeScript、diff 与前端专项隔离检查通过（1 passed / 9 deselected，新增紧凑摘要及多单上限断言，原滚动检查仍通过）；浏览器视觉/点击尚未验收。
- 本次增量验证：前端构建、模拟预览TypeScript、前端专项隔离检查1项通过（9项未选；实际运行消息恢复/消费、SSR及滚动几何/决策断言），预览HTTP200。两款预览和全量抽屉内容已核对；真实浏览器点击、响应式截图、焦点与滚动体验仍待验收。Chrome/IAB不可用，原生Computer Use权限未授予；本次未调用模型/业务数据库。
- 本轮证据：前端构建、实际处理函数/历史恢复/卡片 SSR 契约检查通过；模拟预览 TypeScript 通过，`/dev/results-preview.html` 返回 HTTP 200。不代表浏览器或真实业务验收。
- 关闭条件：在桌面/移动端核对折叠侧栏、商品展开/版本、订单明细、政策来源、历史切换与刷新、输入法/停止等待、偏好忘记和人工确认；验证焦点与滚动，核对真实响应一致性。浏览器控制权限未授予，本轮未调用模型或业务数据库。

## E2E-ANSWER-GRANULARITY：型号卡片与正文版本表格口径不一致

- 状态：2026-10-27本范围已修复，离线检查、三条固定材料真实回答及单次B21真实聊天通过；未验收浏览器或多次采样稳定性。发现方：用户页面截图；主责：回答层；配合：商品结果契约、前端、E2E。
- 触发：普通无线机械键盘推荐返回entity_scope=spu，卡片显示型号及候选价格区间，正文却自行列白色标准版、单版本价格/库存。工具结果同时保留型号聚合与代表SKU材料；旧规则强调不混淆事实，但没有明确普通推荐正文必须沿用卡片层级。降级回答另有固定使用product.price的问题。
- 修改：`backend/app/agent/prompts/answer_facts.py`明确每项entity_scope优先、缺失时参考selection_scope；型号推荐使用spu_title/型号title、series_min/max_price和候选规格，不默认选版本或加库存列；库存问法使用候选合计并限定范围。SKU/明确版本问法保留具体版本价格和库存。`agent/loop/independent_answer.py`降级型号报价改用候选区间；缺失聚合不以代表SKU或model_*补齐。
- 接入边界：保留原始型号及代表版本证据，不改工具输出契约、数据投影、前端Markdown或程序结构/业务校验。未按关键词解析或替换生成正文。旧历史回答不重写；重新查询才生成新回答。
- 离线：36项通过（范围见新增`tests/agent_loop/business/test_catalog_answer_granularity.py`及相邻回答/证据/完成协议测试），Ruff/diff通过。覆盖候选区间与全型号/样本价分离、聚合缺失、等价单价、显式SKU优先及现行评测fixture契约。此成绩不证明自然语言稳定性。
- 固定材料真实回答：`evals.agent.review_answer_style --live --repeat 1 --case spu-recommendation --case sku-recommendation --case spu-candidate-stock`，3/3工具外复检通过，各一次模型调用；型号区间219—319/289—459、黑色版本319元16件、候选合计23/31件分别准确。首次装置启动因未选旧用例ToolError契约过期失败，零模型调用；只修正fixture为planning_failed/next_action=stop后继续。原报告及复检：`artifacts/evals/agent_loop/20261027T040158Z-answer-style-0e9bc29c.json`及`.review.json`。
- B21：加严S2为正文/卡片型号粒度一致；新隔离库pc_e2e_keyboard_spu_20261027，源商品目录只读复制，正式聊天仅一次9.759秒，6次模型调用。正文五型号价格分别204—254、217—267、230—280、243—293、256—306元，与卡片及冻结原始目录聚合一致；无白色标准版默认选择或库存列。7项自动检查、6项语义复检通过（当前Codex会话复检，非独立作者盲审），目录/订单/偏好快照不变。
- Trace：c3d612060b8fb2d1ab767a6cce9cf529，工具span b3feeadcfd88f05d；报告`artifacts/evals/e2e/keyboard-spu-answer-20261027/batch/report.md`。未执行其余50例、浏览器、legacy或全量成本评测。前轮B21成绩按当时标准保留，不倒改为已验证本次更严格的展示要求。

## E2E-SEARCH-TEXT-OVERRIDE：范围空检索文本覆盖详细规划的类型条件

- 状态：2026-10-27 本缺陷已修复，相关离线及单次真实B21通过；不代表多表达稳定性或浏览器验收。发现方：前端实际咨询及B21；主责：商品scope/planner；配合：structured_planning、E2E。
- 触发：`推荐几款 500 元以内的无线机械键盘`。上游范围规划返回category=键盘、search_text=null；详细规划两次正确输出search_text=机械键盘，但均被trusted绑定覆盖为null。语义复核要求补回机械类型，修正后有效计划仍相同，触发repeated_plan并返回planning_failed。
- 源码：`backend/app/tools/catalog/scope.py`的search_binding包含空search_text；`catalog/planner.py`将其传入trusted；`structured_planning.py`的data合并trusted优先覆盖模型值。结构校验均通过，复核为revise，不是uncertain/JSON格式失败。
- 可获取复现：`tests/e2e/business/cases.json`的B21，`tests/e2e/catalog_repro.py`复制当前商品目录至新隔离库、固定执行一次、开启并导出Phoenix；独立基准在实际调用前冻结。
- 本轮证据：B21单次失败、HTTP200、无商品卡片；Trace 4222bc9ba674fec8a27e3d069c354da6，工具span 4b936991d7d1f441，事件记录submitted=机械键盘/effective=null/source=trusted及planning_output_invalid/repeated_plan。真实模型7次，独立目录基准289个合格SKU/32个SPU，源库只读；前次仅准备失败未调用模型。40项离线装置检查通过，其他50例未执行。
- 关闭条件：明确身份/范围绑定与发现查询文本的职责，阻止空值覆盖有效类型条件，同时保持指定对象身份和范围不可被后续阶段擅改；补相关离线回归，再定向真实B21验证正文、卡片和Trace。本次结果不能补写原运行1508的具体失败原因。
- 修复：内部CatalogScopePlan改用identity_text，仅表示单个指定对象名称；普通catalog/category/candidates发现查询不注入search_text绑定，详细规划自行生成和修正标题搜索词。指定对象仍固定名称/编号并沿用已定位集合；facets的name继续用于对象误投边界。公开工具契约不变；structured_planning、嵌套结构/必填字段/硬规则校验、只读准入、复核和纠错预算均未放宽。
- 本轮验证：414项相关离线测试通过，Ruff及diff检查通过。新增tests/tools/shared/test_catalog_scope_binding.py覆盖原问法执行、同SKU条件、候选范围、名称/编号冲突、详细修正、真实重复拒绝及结构/业务规则；test_tool_adapter补非只读工具隐藏与双入口拒绝。
- 真实验收：新隔离库pc_e2e_keyboard_20261027_fix，源商品目录只读复制，单次正式聊天8.85秒；返回5款SKU386/398/410/422/434，价格204/217/230/243/256元，均在冻结的289个合格SKU基准内。正文、卡片、历史一致；偏好未写入，目录/订单不变。Trace a1ccdcbc2f0f1577f98310c2d7ce71e4（工具span7686f839f53ca614）记录identity_text=null、search_text=机械键盘、price<=500、wireless=是，首次复核approve，无纠错、无planning_failed。
- 证据：artifacts/evals/e2e/keyboard-fix-20261027/batch/report.md及evidence/trace；7项自动检查与6项工具外语义复检通过（当前Codex会话逐项复检，非独立作者盲审）。其他50例、浏览器、legacy和全量真实模型评测未执行；保留前次失败证据。
