# 扁平任务与query＋scope复测（2026-10-01）

## 修改与验证范围

LLM1目标增加scope，允许将可信context_refs中的SKU/SPU补入query并核验一致性；LLM2改为顶层tasks，每项带goal_id，无法执行的目标由blocked_goals表达。LLM2逐任务决定scope，Runtime不继承整个goal的scope，只校验来源/一致性；未知的上游结果仍通过bindings延迟解析，分页沿用page_from。订单身份路径未扩展。

同一版本更新执行查找、严格依赖判断、回答上下文、评测器与相关测试，内部route_plan仍按目标组织任务，模型输出不再要求嵌套。没有修改商品工具内部算法，也没有修复办公用途、名称解析和目录字段算法。

隔离测试54条：**53 passed、1 xfailed**；fixture闭包检查，写库依赖排除 **0**。新增13例覆盖身份来源/冲突、空scope、历史scope缺失、任务scope越界、scope与bindings冲突、扁平目标覆盖、以及同一goal下查询商品携带scope而目录不继承。初次新增用例7条因测试夹具遗漏planning_pending触发旧路由兼容转换，修正夹具后得到上述结果，未修改生产兼容转换。

真实模型复用此前24例中的13例，不改输入/预期；模型deepseek-flash、人工商品/订单数据、正式工具服务与图，止于工具返回。没有真实数据库、LLM3或持久化。另对最终清理旧Prompt表述后的“第二款”单例复核，单独记录，不混入13例统计。

## 同一13例的前后对照

| 指标 | 之前 | 本轮 |
| --- | ---: | ---: |
| LLM1＋LLM2实际调用尝试 | 32 | 24 |
| 非法JSON调用输出 | 8 | 0 |
| 规划失败导致未执行工具的场景 | 5 | 0 |
| 实际业务工具调用 | 8 | 13 |

本轮13例中2例无需工具；其余11例的LLM1、LLM2均首次通过结构化校验。**结构合法不代表工具选择完整：office_layout漏了目录任务。** 两项改动一起实施、样本仅单次运行，不能分别量化贡献，也不能据此保证模型永不生成坏JSON。

## 逐例结果

| 场景 | 本轮结果 |
| --- | --- |
| office_layout | LLM1完整tool_planning；LLM2合法，但仅搜索，未规划目录；搜索整体unsupported |
| office_paraphrase | LLM1完整tool_planning；LLM2搜索＋目录，但目录依赖搜索且搜索为internal；搜索unsupported，目录被阻塞 |
| office_without_facets | 规划合法；办公用途仍导致搜索整体unsupported |
| comparison | 两搜索＋依赖比较的计划合法；K380名称定位仍失败，比较未执行 |
| facets_only | 本次工具内部成功解析布局，返回75%、80%、全尺寸；没修改目录工具，不能把一次成功当修复稳定 |
| direct | 正确跳过LLM2/工具；未验证最终回答 |
| ambiguous_reference | 无明确对象走澄清，未调用工具 |
| mixed | 商品与订单均返回可用信息 |
| budget_update | 当前300元替换500元，返回明确条件匹配及缺失事实计数 |
| alternative_budget | 无严格匹配，返回带差异的替代候选 |
| missing_fact | SKU 501实际执行；partial/object_facts，蓝牙事实unknown、字段missing，未冒充已支持 |
| missing_object | SKU 999实际执行；not_found/target_not_found，不是执行失败 |
| second_candidate | LLM1 query补SKU 201＋scope＋product_2来源；LLM2同scope；实际返回SKU 201价格199（人工测试价格） |

按目标信息判断：7例取得预期工具事实/明确缺失或不存在结果，2例正确无需工具，4例目标尚未完整满足。不能把13例格式通过称为业务13/13通过。

## 新暴露的规划问题

- 办公完整请求含“看看可选布局”，本次一个表达只生成catalog_search。query虽保留文字，工具分工未覆盖完整诉求。
- 工位表达生成目录任务，但其query只问目录布局，不消费上游对象，也不带条件，依然依赖搜索成功；同时商品搜索标internal。搜索unsupported后整个可回答路径被阻塞。依赖及展示角色需要根据任务实际所需证据设定，不能因同goal而串行。
- 以上是TP-03；现有数据只证明这次输出，不能声称所有同类请求都失败。本轮按用户范围仅优化scope与输出结构，未继续调工具选择规则来追分。

## 产物

- [本轮13例完整结果](tool-flow-flat-scope-2026-10-01.json)
- [主复测实现指纹](tool-flow-flat-scope-manifest.json)
- [最终Prompt单例复核](tool-flow-flat-scope-final-wording.json)
- [上轮24例原始结果](tool-flow-2026-10-01.json)
- [跨模块问题](../issues-before-agent-loop.md)

所有原始成绩保留，Ruff与差异检查另行执行；本轮没有完整后端测试或make check。唯一xfail仍为纯空白输入，未在本轮修复。
