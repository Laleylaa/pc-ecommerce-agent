# 工具 Prompt 维护入口

固定提示词统一放在 [templates](templates/)；Python builder 只选择片段、注入业务能力和输出 Schema。
适用于 catalog_lookup、catalog_search、catalog_facets、order_lookup、共用复核/修复，以及保留的旧商品规划入口。
knowledge/policy 等没有内部规划模型的工具不新增 Prompt；agent-loop 的编排/回答提示词不在此目录迁移。

## 编辑位置

| 目标 | Markdown |
| --- | --- |
| 商品共用范围阶段 | `templates/catalog/scope.md` |
| lookup / search 职责 | `templates/catalog/lookup.md`、`search.md` |
| 共用边界、需求、身份、条件、偏好和限制 | `templates/catalog/boundary.md`、`requirements.md`、`identity.md`、`conditions.md`、`preferences.md`、`limits.md` |
| 阶段交接 / 请求上下文 / 执行职责 / 校验边界 | `resolved_scope.md`、`context.md`、`execution.md`、`validation_boundary.md`（均在catalog目录） |
| 商品规划角色 / 输出约束 / 复核补充 | `templates/catalog/role.md`、`plan_output.md`、`review.md` |
| facets 职责及规划入口 | `templates/catalog/facets.md`、`facets_plan.md` |
| 规格说明与条件示例 | `templates/catalog/specs_common.md`、`condition_examples.md`、`categories/*.md` |
| 订单规划 | `templates/orders/plan.md` |
| 计划语义复核 / 修复 | `templates/shared/plan_review.md`、`repair_*.md` |
| 旧商品接口兼容规划 / 修复 | `templates/catalog/compatibility_plan.md`、`compatibility_repair.md` |

片段按职责复用，不让每个工具复制一整份共用规则。Python builder 的公开函数签名保持不变。
固定文字的语义迁移以保留现有业务边界为主，按角色/任务、输入边界、规则、示例、输出契约整理。
范围识别、详细规划、复核分别有任务职责；复核最终只输出 ToolPlanReview，不输出修订后的业务计划。
输入JSON通过原来的HumanMessage传递；模板变量由代码提供，不能用用户文本决定模板路径。

## 动态内容与渲染

- 字段能力、单位、操作符和能力分组仍从 `catalog/fields.py` 派生，不能在MD再建第二套规则表。
- 输出JSON Schema仍从Pydantic模型生成；必填决策仍由 `output_schema` 标记。
- 记录示例数据保留在 `catalog_search_specs.py`，不是合法值白名单。
- 跨agent/tools的商品身份共同契约仍引用 `rules/product_identity.py`，不复制或让rules反向依赖tools模板。
- `{{name}}` 为唯一占位符语法。缺变量、多传变量、缺文件会直接报错；不使用静默默认值。
- 不支持表达式、条件、循环或模板内include；组合逻辑在Python，模板只写说明文字。
- 单次替换，不递归展开变量内容。普通JSON花括号无需转义，插入内容中的 `{{...}}` 也保持原样。
- UTF-8包资源加载，支持源码运行和wheel安装。部分共用片段在模块导入时加载，修改后应重启进程；不承诺热更新。
- `backend/pyproject.toml` 明确打包MD资源；商品评测和工具选择评测的源码指纹已包含这些资源。

## 维护与验证

修改职责/规则时注明行为变化；只有排版或迁移也需要检查模板渲染及Schema契约。
入口：`backend/.venv/bin/python tests/run.py tests/tools/prompts -q`。
测试覆盖三个范围模板、search/lookup/facets输出Schema、动态品类、订单调用与修复、复核Schema、占位符和资源边界。
真实模型效果必须另行评测；离线渲染通过不能证明语义表现提升，旧批次成绩不能转算到新Prompt。

本次参考[OpenAI官方Prompt工程指南](https://developers.openai.com/api/docs/guides/prompt-engineering)的角色/指令/示例/上下文分段，
以及[Prompt生成指南](https://developers.openai.com/api/docs/guides/prompt-generation)保留领域约束和示例的建议。
采用的是组织方式，不更换当前模型，不引入特定供应商API特性，也不要求输出隐藏推理过程。

## 基础品类与商品特征

scope.md 要求从可选基础品类选择标准名称，区分基础品类与特征修饰词。
当前版本已删除具体商品名、品类映射及查询示例，只保留通用规则。
完整商品名称和 query 仍保留，规格条件交后续阶段解释；本阶段不把名称中的特征
擅自追加为筛选条件，归类可修正时不直接返回 unsupported。
品类列表当前仍来自 catalog/fields.py，不宣称已接入数据库动态品类。
