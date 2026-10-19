# 工具侧根因修复与验证（E29）

> E30修复后复核已发现未覆盖边界：VF-01、CF-06撤回过早关闭，CF-08保持开放，新增CF-09。当前结论以[复核报告](tool-repair-reaudit-report.md)和共享issues为准；下文保留E29当时实现与验证记录。

日期：2026-10-04。分支：`feat/orchestrator-catalog-integration`，基点`b390d61`加工作区。
用户已授权按讨论方案实施。保留开始时已有工作区修改；未暂存、提交或推送。
范围止于工具结构化输出，RP-02、回答和完成收尾不在本轮验收内。

## 结果及边界

本轮针对完整输出承接、字段能力、对象范围、订单过滤和输出失败收口修改正式实现，并完成隔离回归。结构验证通过不等于真实模型自然语言效果或配置数据库验收。E28中的`{}`是故障注入，不能据此声称该正常query曾真实生成空对象。

- **CS-03 / CF-02**：LLM必要决策在默认值注入前验证，根和嵌套必填与Prompt schema同源；默认值继续供公开参数及确定性程序构造使用。JSON重复键、非标准数值、Markdown包裹不作为有效JSON承接。缺字段、多余字段、错误类型、非法字段/原文证据进入明确校验反馈，耗尽停止查询。
- **VF-01**：组合`tools/structured_planning.py`，复用纯`core/llm_feedback.py`及抽出的`core/llm_calls.py`；不继承agent、不读取完整聊天状态。三个正式内部规划器统一接入，历史商品helper及其他模型节点未宣称迁移。模型SDK重试为0；传输原样重试最多一次，输出修复最多一次，总provider调用最多三次，搜索35秒、目录/订单6秒内部总预算。FORMAT_ERROR / SCHEMA_ERROR / 显式RULE_ERROR可纠错；真实不支持、澄清、条件冲突及无匹配不纠错，普通程序异常/取消也不纠错。401/403等非暂态拒绝直接失败。内部耗尽使用`planning_*`终止错误，不要求外层重写参数。
- **CF-01 / CF-06**：抽出共享`catalog_fields.py`，字段语义、品类能力、单位、别名及匹配统一；layout→style，wireless仍是布尔规格。搜索和目录按软category_hint生成规格说明，错误/缺少hint不能充当条件。条件与事实请求分别维护，能力外要求显式保留。模型品牌/价格/规格筛选统一提交带原文证据的conditions，旧brand/filters/min/max仅由程序处理调用方明确参数，避免绕过来源校验。正式目录使用相同商品记录/字段匹配，44Hz不会匹配144Hz，4英寸不会匹配24英寸；JSON和属性表的冲突保留来源值，不能首值覆盖。旧CatalogRepository辅助方法保留，本项关闭限正式工具路径。
- **CF-08**：catalog_facets接收可信scope，读取对象规格维度与已记录取值，并给SKU/SPU归属、缺失/冲突、总数量和完整性。已知对象直接有来源的scope；未知名称由正式搜索定位再task_output绑定范围；未定位时返回明确身份/澄清诊断，不能以全店目录代替。单项商品事实/版本查询原职责保留，编排不复制规格字典或猜编号。
- **OL-02**：统一OrderFilters支持创建/付款/发货时间半开区间、状态，以及同一订单商品行的名称/已记录规格值文本筛选；过滤先于计数、排序和分页。只复用受控品类/品牌/颜色别名，不匹配JSON键，也不猜未记录属性。快速路径仅完整受控语法，复杂表达交给模型；缺模型时明确失败。尚未支持商品价格/数量等谓词，必须保留unresolved_requirements并停止查询，不能标完整支持；真实冲突区间返回invalid_query。订单认证用户仍由运行时注入；OL-01下游认证匹配集合仍安全阻断。
- **TC-02 / CF-07**：输出schema和JSON序列化在同一错误边界；序列化异常返回execution_error/stop，不把合法输入变成invalid_input。无模型目录只接受完整封闭表达，开放查询明确失败，不从部分关键词生成宽查询。
- **超时重试协作**：在既有execute_tool_wave重试循环内维护一次逻辑调用的已校验计划生命周期；不改变图拓扑或LLM1/LLM2重试政策。执行超时重试复用深拷贝计划并重新读取事实，防止重复模型规划改变条件或重置内部调用预算。未通过校验的输出及不同请求不缓存。

## 本轮验证

本轮最终：**916 passed，33 deselected，8.90秒，无失败/skip/xfail**。33项是安全插件排除的配置数据库fixture相关用例。没有跑完整后端测试、make check、配置数据库写入或初始化。

```sh
cd backend
.venv/bin/python -m pytest -p scripts.orchestration_audit_safety \
  tests/test_tool_planning_repairs.py tests/test_catalog_search_tool.py \
  tests/test_llm_feedback.py tests/test_tools.py tests/test_tool_retry_policy.py \
  tests/test_planning_execution_policy.py tests/test_planning_call_policy.py \
  tests/test_goal_planning_validation.py tests/test_planning_handoff.py \
  tests/test_planning_contract_alignment.py tests/test_goal_task_integration.py \
  -q --tb=short --basetemp=/private/tmp/pc-tools-fix-20261004 \
  -o cache_dir=/private/tmp/pc-tools-fix-cache
```

脚本模型、人工商品记录、fake repository/审计，正式图停在工具返回边界；socket及AsyncEngine连接被安全插件阻断。订单谓词使用内存SQLite执行同一SQLAlchemy语句，唯一方言适配是`jsonb_each_text`→`json_each`；这不是实际PostgreSQL执行验证。计数/列表/最近/明细过滤传参独立核对，SQL覆盖用户归属、半开时间端点、状态、多个词必须属于同一商品行、中文别名和JSON值而非键。

新增根因回归32项包括空计划修复/耗尽、嵌套缺字段、非法值、三类反馈、暂态传输原样重试与输出修复总预算、403不重试、动态品类说明、数值匹配、对象范围和适用版本、属性来源冲突、无模型开放表达、订单未执行条件、晚到取消抑制响应、序列化直接及正式图错误出口、内部耗尽不外层重启、执行超时复用计划与重查事实。有效模型wire fixtures显式写决策，不依赖生产默认值补全无效输出。

针对本轮生产文件、新脚本及相关测试的Ruff检查通过，`git diff --check`通过。新进程独立导入搜索/订单schema、纯规格规则、搜索Prompt通过，未加载agent、database或工具注册器；tools下无反向agent导入。

## 未验证与下一步

1. **真实模型条件完整性**：新增`backend/scripts/eval_tool_planning_repairs.py`，8条独立合成表达覆盖多条件、否定、价格端点、75%布局、错误品类hint、对象规格、去年计数及付款区间+状态+商品条件。沙箱运行8条全部传输失败，raw_outputs均为空，未产生模型计划；`tool-planning-repair-results.json`仅是此次失败证据。不能报告“真实模型8例通过/语义失败”。
2. **自动审批拒绝**：外部运行该脚本被拒绝，理由是原请求未明确授权把内部Prompt、Schema和合成请求发送到外部配置模型服务。已向用户请求此项授权；未绕过限制。没有发送真实订单/用户/数据库内容。授权后再运行模型评测，记录遗漏条件率和失败出口；CS-04保持开放。
3. **数据库时间口径**：服务端提供Asia/Shanghai可信当前时间，aware边界转对应naive墙上时间以匹配现有naive订单列。实际数据库历史记录写入时区尚未查证；验证这一约定前，不能声称跨时区业务端到端通过。实际PostgreSQL JSONB谓词、运行性能与读取结果未验收。
4. **后续结果消费**：facets新增维度/取值归属及完整性字段、订单新增过滤/错误结果的回答/记忆/前端解释未验收；RP-02及回答/收尾继续后续阶段。工具提供完成和本轮参数交接通过不等于完整聊天业务验收。

问题当前状态记录在`docs/development/issues.md`的E29段；本地development入口同步，未升级modules稳定基线。
