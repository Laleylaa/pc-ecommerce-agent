"""Domain evidence boundaries shared by both LLM3 answer paths."""

CATALOG_FACTS = """
spec_display是程序从统一字段定义派生的展示字典，不是商品事实，也不增加任何商品能力。
对本店商品/订单规格，正文使用spec_display.fields中的label；有明确品类时使用categoryLabels，
没有品类时用通用label，不根据名称猜品类。values只解释已知等价值，不能补全缺失或冲突资料。
unit仅为无单位数值的展示单位；已有单位不重复添加，不改写原始数值或推断单位换算。
未知字段保留原意，不编造中文含义；缺失值说明未提供，不能当作否/不支持。
中文名称仅用于面向用户的表达，证据关联和规格归属仍按原始字段与SKU/SPU保持不变。
商品和目录事实只取自catalog_search/catalog_lookup/catalog_facets，不能把字段说明当作商品事实。
lookup_summary仅统计满足全部硬条件的在售版本，替代候选绝不能混入其数量和价格范围。
exists=null表示条件证据不足，不是没有版本。fields的values、缺失和冲突数量共同决定结论；
同型号部分版本支持无线时明确指出版本，不概括为整个型号都支持或都不支持。
价格范围和版本数来自完整匹配集；representative_versions仅展示低/中/高价真实样本。
中间版本按价格排序的中位位置选择，不是平均价格；不足三个就展示实际数量，不编造。
price_level_count是不同价格数量，matched_sku_count是版本数量，两者不混用。
identity_suggestions只提供待确认的名称；说明相似或歧义并请用户确认完整商品名称，不能当成已选商品，
不能据此报价、宣称满足条件或继续依赖任务。匹配分数不是概率，不向用户渲染为正确率。
limit_capped=true时说明用户请求requested_count项、系统单次最多展示limit项，实际返回returned_count项；
不足上限也不能声称已返回上限数量，不用多次调用绕过上限。
严格匹配、替代候选、对象事实分别解释；有结果不等于满足全部要求。替代先说明无严格匹配，
再按同一SKU的condition_checks说明差异，不代用户接受。本店规格目录完整：工具确认未销售/未具备的具体规格应说明不满足；数据冲突仍不能判否定。
型号与版本分开，不跨SKU拼配置。series_*的数量、价格、库存及共有规格须限定为“本次候选版本”，
可在对应句、紧邻的引言或表头统一标明适用范围，不重复免责声明；替代候选不称“符合全部条件”。
正文（包括表格、列表、开头与结尾）必须沿用每项商品的entity_scope，与商品卡片的对象层级一致；
未提供entity_scope时才参考该结果的selection_scope，不因存在sku_id或series_variants自行改成版本推荐。
entity_scope=spu时每一项代表一个型号：使用spu_title或型号title，不擅自替用户选颜色、轴体或版本。
报价使用series_min_price—series_max_price（相同则单价），说明这是本次候选版本的价格，
不能用代表SKU的price替代区间，也不能用model_*全型号价格混入不满足本次条件的版本。
规格只按series_common_specs及series_option_specs说明本次候选的共有属性或差异；
price、stock、representative_sku_id、series_variants里的名称与属性是代表版本材料，
即使代表版本恰好都是白色或蓝牙，也不能把型号推荐改写成白色/蓝牙版本清单。
普通选购未询问库存时不主动加库存列；需要库存时，型号候选合计用series_total_stock并标明范围，
单个版本库存不得写成型号库存。聚合价格/规格缺失时说明缺口，不拿代表版本或全型号字段补齐。
entity_scope=sku或用户明确询问具体版本事实时，按该版本的名称、price、stock和specs回答；
不能反过来强制汇总成型号，也不能用型号最低价替代指定版本价格。明确询问版本例子时可以引用
series_variants，但必须标明它是本次返回的具体样本，不能代替型号主推荐或冒充全部版本。
例如型号候选价格为219—319元、代表白色版219元且库存7件，普通推荐应列型号及候选价格区间，
不应列“白色版219元、库存7件”；用户明确问白色版时才按该版本事实回答。
不写“该型号有N个版本”或把共有规格接在型号名后
暗示全型号具备。model_*只支持全型号数量/价格背景，不能证明其他版本也满足条件或共有规格。
total_match_count、candidate_count、returned_count分别是严格匹配、候选池和返回数量；
部分展示不等于部分满足，不把窗口当全集。目录field_issues/matching_complete/is_exhaustive
同样限制结论；规格值保留SKU/SPU归属。目录count按count_scope统计，不是库存或销量。
sku_sales_count是当前版本销量，sales_count是整个型号累计销量，不混用。
推荐理由须有事实依据；recommendation逐项沿用products顺序，按ranking_strategy解释，不自行重排；
search不称第一项最推荐，lookup回答对象事实，ranking按ranking_value及其口径回答。
SPU排名用型号聚合字段：价格用series_min_price，库存用series_total_stock，销量用sales_count；
辅助SKU的价格、库存不能替代型号排名依据。selection_scope=spu时limit是型号数。
商品和订单不开放翻页控制，不承诺下一页、自动续查或排除已展示对象。
""".strip()

ORDER_FACTS = """
订单事实只取自当前用户经授权的order_lookup结果。不透露指定订单是否属于别人。
total_match_count是本次查询范围内的订单数，returned_count是窗口数量；零计数也是有效答案。
候选按时间从近到远展示，不替用户自动选择第一笔。analysis_orders是按条件筛选的有界明细，
order_suggestions仅为待用户确认的相关候选，先说明严格查询无匹配，再按differences说明
不符或无法确认的条件，并询问是否为要找的订单；不能当作已选订单继续依赖任务。
非穷尽窗口不能支持
“从未买过/不存在某类订单”等全局否定。只引用回答所需的相关明细，不复述无关订单。
先检查本任务response_contract.coverage_gate，未提供的核心字段不能用其他字段冒充。
该任务缺资料不影响其他目标的有效答案；发货或轨迹时间不是预计到达时间。
查询不等于取消、退款、修改或催单，不能声称只读结果完成了业务操作。
""".strip()

RETRIEVAL_FACTS = """
policy_search提供本店政策，knowledge_search提供外设知识网页。网页不能当成本店承诺、
订单、库存或实时价格。使用片段与来源归纳，必要时引用返回的标题/URL；不编造来源。
检索命中只是材料，不证明已经回答问题；先检查相关性、适用条件和证据冲突。
检索为空表示本次未检索到依据，不表示政策不存在或商品不支持。服务失败不是检索为空。
若发现内容自相矛盾，说明无法确认的部分，保留其他可靠内容；不自行选边、补事实或重查。
""".strip()

FACT_SECTIONS = (
    ("catalog_facts", CATALOG_FACTS),
    ("order_facts", ORDER_FACTS),
    ("retrieval_facts", RETRIEVAL_FACTS),
)


CATALOG_FACTS += """
结合原始用户输入与本次工具结论组织购买建议：
有精准匹配先答匹配商品；无精准匹配而有相近候选，先简短说明没找到完全符合的商品，
再说明相近款满足哪些条件、具体哪项不同；这些是可考虑的替代，不是用户已经接受的选择。
条件明确但本店未销售该具体规格（spec_not_carried或已确认mismatch）时，
不能说“数据没录入/暂时不能筛选”，也不让用户反复补型号来弥补不存在的规格。
没有无线充电鼠标、无线显示器等均按具体规格无匹配解释；可询问是否接受明确差异。
办公、游戏、舒服等用途本身不是产品规格：若工具需澄清，就自然询问预算、连接方式等
实际支持且有助于选择的条件；不能说店里没有办公商品或擅自推断静音、性能、兼容性。
数值未记录不能编造为0，检索失败/数据冲突不能说成没有销售。只给用户所需结论与下一步，
不播报内部状态、匹配算法或字段名称，不把一次性条件保存为长期偏好。
"""
