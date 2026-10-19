# 独立任务与工具示例复测（2026-10-01）

本轮只改编排Prompt与示例，未修改工具实现/schema/description。LLM1和LLM2规则按职责分段；独立示例模块包含六工具各一例和商品搜索＋目录枚举组合例。示例只教学工具选择与公开参数，不提供内部规格映射。

隔离选跑三份既有测试文件，新增七个Prompt示例的正式计划/工具参数校验，共61条：**60 passed，1 xfailed**（既有纯空白输入），fixture闭包排除写库用例 **0**。不运行完整后端或make check。首次运行测试的新增文件路径使用错误，未写入文件；随后纠正路径再增加用例，上述成绩来自最终61条。

真实模型deepseek-flash复测原有三个输入，共6次LLM1/LLM2调用，没有重试/非法JSON；5次实际工具执行，使用人工仓库，止于工具返回，不连接数据库、不运行LLM3。

| 例子 | 规划与执行 | 工具结果 |
| --- | --- | --- |
| office_layout | 一个完整tool_planning goal；搜索和目录两个user_facing任务，depends_on均空；两工具均执行 | 搜索因办公用途unsupported；目录内部生成layout，返回不支持 |
| office_paraphrase | 一个完整tool_planning goal；独立搜索＋目录，均user_facing，两工具均执行 | 搜索unsupported；目录使用style，返回符合白色/蓝牙/300预算条件的布局选项 |
| facets_only | 单个catalog_facets任务，实际执行 | style解析成功，返回布局选项 |

本轮修正了这两个既有输入的工具遗漏/错误依赖现象，不能由小样本保证所有表达稳定。目录内部字段问题再次复现，因此上一轮单例成功不代表已修复。搜索拒绝办公用途和名称解析问题不在本轮修复范围。仅七条示例经隔离契约校验，真实模型只复测以上三例，不能说六工具的自然语言效果都已验证。

审查结论：输出由GoalPlan/ExecutionPlan标准化；LLM2可见schema从正式工具模型派生，实际参数再按public_input_model校验；共用generate_plan回填原始失败输出及错误、最多重试一次。完整模型输入payload尚为dict组装，已记录PI-01；语义依赖合理性、条件保真、目标内诉求完整性不能由Pydantic独自保证。

证据：[逐例原始输出](tool-flow-independent-tasks-2026-10-01.json)、[实现指纹](independent-tasks-manifest.json)、[共享问题清单](../../../docs/development/issues.md)。Ruff与文档/差异检查通过；未提交。
