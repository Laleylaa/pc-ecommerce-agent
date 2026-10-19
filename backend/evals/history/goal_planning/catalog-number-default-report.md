# 商品编号默认 SKU（E31）

2026-10-04，feat/orchestrator-catalog-integration，基点b390d61加保留的工作区。
用户明确业务口径：工具收到“编号1有什么规格”应按SKU1查询，不要求LLM1/LLM2先转换编号，也不因缺少SKU字样要求澄清。

## 本轮实施

- `rules/product_identity.py`维护受控编号语法：明确SKU/SPU保持原类型；“编号N”“商品编号N”按SKU解释。价格、型号数字、展示序号不是商品编号；订单/物流/快递/运单编号不应用商品默认。
- 搜索和目录封闭语法复用同一编号前缀/类型映射；完整受控表达可零LLM查询。开放表达仍由工具内部模型解析，Prompt明确同一规则；Search Prompt v14，公开contract_version=3保持。
- 搜索/目录共享来源校验接受该业务默认，不再要求原文必须出现字面SKU。错误类型/不同编号仍用已有INVALID_PLAN→RULE_ERROR反馈；没有原文或可信范围来源的商品标识不能因此合法。
- query原文保留，数据库查询决定商品存在性。用户给“编号999”，合法转换SKU999，即使无记录也返回not_found而非规划错误。
- 编排图/公开输入未改；共享纯编号规则同步让来源检查理解同义编号，但LLM1/LLM2不必转换query。正式图隔离回归验证编号1原样传入facets后查SKU1。

## 验证

最终定向范围：**933 passed，33 deselected，8.79秒**，无失败/skip/xfail。命令与E30相同11文件，basetemp改为`/private/tmp/pc-catalog-number-full`，cache_dir为`/private/tmp/pc-catalog-number-cache`。

新增17个用例：5个共享编号来源语法，4个SKU/SPU封闭规格查询，1个合法但不存在编号，2个内部模型错误类型→反馈纠错，1个搜索编号默认，1个正式图原样query交接，以及3个新编号写法不能绕过原有来源约束的回归。受控查询明确区分SKU1仅一个版本、SPU1两个版本；复杂表达用脚本模型确认默认SKU及明确SPU不会互换。

相关源码/测试/探针脚本Ruff和git diff检查通过。安全插件排除33个配置数据库fixture、阻断socket/AsyncEngine；人工模型/内存记录，无真实模型或配置数据库验收。保留已有工作区，没有暂存/提交/推送。

## E30口径更正及未完成部分

E30将“编号1→SKU1”列为应澄清的错误，不符合用户本轮明确的业务要求，撤回该判断。旧JSON保留为历史行为证据，不再将该结果计为缺陷。探针脚本改为`facet_bare_number_defaults_to_sku`，本轮产物`catalog-number-default-results.json`包含1个合法默认场景及仍复现的6个其他异常场景；脚本正常退出不表示这些其他问题通过业务验收。

CF-08仍开放：name/target组合提前返回后忽略校验、已有对象空规格状态未完成；VF-01、CF-06、CF-09仍按E30跟踪。工具负责收到query的内部计划/执行边界，不审核query与最初用户输入的语义一致性。CF-01/CS-04真实模型、OL-02数据库时间口径和RP-02后续消费保持各自验证范围。
