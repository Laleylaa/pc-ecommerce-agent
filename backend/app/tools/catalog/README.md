# 商品工具实现

正式工具名仍是 `catalog_search`、`catalog_lookup`、`catalog_facets`，统一由
`tools/contracts.py` 绑定。本包按业务职责组织；导入包本身不加载服务。

| 位置 | 职责 |
| --- | --- |
| `scope.py` / `identity.py` / `text_matching.py` | 共用范围准备、对象定位与名称匹配 |
| `fields.py` / `matching.py` | 字段能力、类型化条件与同一 SKU 匹配 |
| `planner.py` / `service.py` / `engine.py` / `presentation.py` | search 与 lookup 共用的规划、执行、结果组织，通过 operation 保留业务分界 |
| `validation.py` / `requirements.py` / `outcomes.py` | 计划校验、条件归属及结果原因；facets 按需复用 |
| `facets.py` / `facets_planner.py` / `facets_plan.py` / `facets_execution.py` | facets 服务、规划、计划契约与校验、目录聚合 |
| `context.py` | 已有运行时上下文解释；保留旧输入兼容语义 |
| `normalization.py` | 调用方旧结构化参数的归一化；词表与 fields 的能力提示词表语义不同，不合并 |
| `compatibility.py` | 保留旧搜索、规则规划和旧类调用入口；不是当前正式搜索实现 |

三个工具复用字段和数据判断，但 facets 自己维护计数、聚合和完整性规则。
Prompt 继续集中在 `tools/prompts/`；模型调用与预算仍由 `structured_planning.py` 管理。

当前范围阶段使用`scope_kind`描述查谁/在哪查，不判定工具职责；指定对象按需先确认身份，
详细规划与现有语义复核判断事实、选购和统计的职责（包括停止计划）。search可通过
`candidate_unit=sku`选购具体版本，复用SKU筛选、排序和卡片输出；lookup仍使用
`version_view`展示版本事实。普通LLM成功路径仍三次调用，预算不变。
普通推荐先同SKU筛选，再按SPU分组和匹配版本最低价排序；明确选版本才使用SKU候选单位。
三个正式工具均不注入catalog_context；主流程模型读取上下文，自行提交自足query及可选scope。
facets计划不重复定义scope，执行时以原请求scope与计划条件取交集，复核结合两者判断。
旧compatibility搜索输入保留其上下文类型，不属于正式三工具路径。
验证入口在根 tests；运行结果与复检记录只保留本地。

当前正式 LLM facets 不导入 compatibility；显式关闭 LLM、未注入 planner 的路径仍按
原行为延迟加载规则规划器。保留这个路径及旧测试调用方是 compatibility 的用途。
待规则规划路径与旧调用方分别完成迁移、验证后，再删除兼容实现，不能直接删整个文件。
