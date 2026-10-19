"""Domain evidence boundaries shared by both LLM3 answer paths."""

CATALOG_FACTS = """
商品和目录事实只取自catalog_search/catalog_facets，不能把字段说明当作商品事实。
严格匹配、替代候选、对象事实分别解释；有结果不等于满足全部要求。替代先说明无严格匹配，
再按同一SKU的condition_checks说明差异，不代用户接受。缺失/冲突规格不代表商品不支持。
型号与版本分开，不跨SKU拼配置。series_*的数量、价格、库存及共有规格须限定为“本次候选版本”，
可在对应句、紧邻的引言或表头统一标明适用范围，不重复免责声明；替代候选不称“符合全部条件”。
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
