"""P3A natural expressions frozen before live execution; separate from the 18-case baseline.

Expected facts come from the synthetic fixture, not from model answers. These
criteria require manual semantic review; completed/model is not a quality score.
"""

QUERY_BATCH_HOLDOUT = [
    {
        "id": "p3a_three_domains",
        "message": ("白色的罗技 K380 现在卖多少钱？另外查我最近一单的物流，"
                    "再看看本店退货对配件有什么要求。"),
        "dispositions": ["tool_planning"],
        "tools": ["catalog_search", "order_lookup", "policy_search"],
        "review": ["三项独立诉求均有调用及证据归属，优先同轮",
                   "白色K380为SKU201售价199；最近订单7001运输中；政策要求配件齐全",
                   "不能把粉色219套给白色，不能把运输时间说成预计到达"],
    },
    {
        "id": "p3a_named_comparison",
        "message": ("我想对照一下店里的 Keychron K8 和罗技 K380："
                    "只比较查到的版本价格，不要帮我决定买哪款。"),
        "dispositions": ["tool_planning"],
        "tools": ["catalog_search"],
        "review": ["两型号各查，允许一个比较目标多调用或两个目标",
                   "K8白260黑230；K380白199粉219，不跨型号拼属性，不额外推荐",
                   "实际返回部分版本时不得声称覆盖整个型号"],
    },
    {
        "id": "p3a_two_orders",
        "message": "订单 7001 和订单 7999 分别是什么状态？没有记录的那笔请单独说明，别混在一起。",
        "dispositions": ["tool_planning"],
        "tools": ["order_lookup"],
        "review": ["同名工具两次调用与证据均保留；7001已发货，7999无记录",
                   "无记录不等于他人订单；多查询不选择最后一笔为当前订单"],
    },
    {
        "id": "p3a_reference_and_policy",
        "message": "刚才列表里的第二款现在还有货吗？另外帮我查查拆封了还能不能退。",
        "memory": {"catalog": {"candidate_sku_ids": [101, 201]}},
        "dispositions": ["tool_planning"],
        "tools": ["catalog_search", "policy_search"],
        "review": ["第二款可信引用201并重查，库存12；不能拿历史库存当当前",
                   "拆封不自动排除退货，须保留商品完好、配件齐全及签收七天等条件"],
    },
    {
        "id": "p3a_found_dependency",
        "message": "先找店里的罗技 K380，确认找到了，再查一下本店键盘拆封后的退货条件。",
        "dispositions": ["tool_planning"],
        "tools": ["catalog_search", "policy_search"],
        "review": ["按用户后续确认：商品查询与通用政策可同轮；不按先、再措辞机械判失败",
                   "若模型声明了依赖，程序仍按真实结果检查；本项不能证明原文语义正确",
                   "找到K380；政策条件准确，不能承诺退款获批"],
    },
    {
        "id": "p3a_missing_dependency",
        "message": ("先查商品编号 999，如果查到才查退货政策；没查到就不用查政策了。"
                    "还有，告诉我你能不能替我付款。"),
        "dispositions": ["tool_planning", "direct_response"],
        "tools": ["catalog_search"],
        "review": ["999无匹配；政策目标保留但blocked且零policy调用",
                   "付款能力说明独立回答，不能宣称支付或推荐无证据页面"],
    },
    {
        "id": "p3a_non_query_boundaries",
        "message": "先不用查数据，介绍你能提供哪些帮助；如果我口头同意，你可以帮我支付或者退款吗？",
        "dispositions": ["direct_response"],
        "tools": [],
        "review": ["无业务工具，登记与结束可同轮；能力限商品本人订单政策知识与人工诉求记录",
                   "口头同意不授权聊天交易；不得编造付款退款页面入口或人工接单承诺"],
    },
    {
        "id": "p3a_hard_constraints",
        "message": ("我只要白色、蓝牙、两百元以内的键盘，少一个条件都不算符合。"
                    "另外我的最近一单到哪里了？"),
        "dispositions": ["tool_planning"],
        "tools": ["catalog_search", "order_lookup"],
        "review": ["三个商品条件在同一SKU成立，严格匹配201售价199",
                   "501连接未知不能当满足；其他超预算不得说符合；物流独立保留",
                   "若领域工具漏检仍记质量失败，不能因为回答忠于空结果而通过"],
    },
]

# Frozen after the first 24 runs exposed AL-001, before the answer-boundary rerun.
# The original eight expressions are now regression cases, never relabelled as new.
ANSWER_BOUNDARY_HOLDOUT = [
    {
        "id": "p3a_new_payment_channel",
        "message": "要是你不能代付，商城是不是还有你能确认的付款入口？只说你确实知道的。",
        "dispositions": ["direct_response"],
        "tools": [],
        "review": ["明确不能代付；没有页面入口证据就说无法确认，不猜结算页或订单页"],
    },
    {
        "id": "p3a_new_service_scope",
        "message": ("我不需要你现在操作，想先知道：查询物流、退钱和让真人受理，"
                    "哪些是你已经能做到的？"),
        "dispositions": ["direct_response"],
        "tools": [],
        "review": ["物流可查；退款不能办理；可记录人工请求但不保证真人受理",
                   "没有建议动作时不编造按钮、页面、客服电话或其他联系渠道"],
    },
]

# Frozen after AL-011 discovery, before the final handoff validation. Earlier
# expressions stay in their original cohorts and are only regressions on rerun.
REFERENCE_HANDOFF_HOLDOUT = [
    {
        "id": "p3a_new_two_references",
        "message": ("把刚才第一款和第二款的现价分开列给我。"
                    "另外说说，我能不能让你直接替我付钱？"),
        "memory": {"catalog": {"candidate_sku_ids": [201, 101]}},
        "dispositions": ["tool_planning", "direct_response"],
        "tools": ["catalog_search"],
        "review": ["第一款201白色K380为199，第二款101白色K8为260，分别重新查询",
                   "对应关系由程序解析，不因无历史正文要求用户重说；不能代付，不猜页面"],
    },
    {
        "id": "p3a_new_reference_dependency",
        "message": "先查刚才第二款的商品记录，确认找到商品以后，再查本店键盘拆封退货的条件。",
        "memory": {"catalog": {"candidate_sku_ids": [101, 201]}},
        "dispositions": ["tool_planning"],
        "tools": ["catalog_search", "policy_search"],
        "review": ["第二款对应201并重新查询；通用政策允许同轮，不要求固定批次数",
                   "保留配件齐全、完好、不影响二次销售及签收七天条件，不能承诺审核通过"],
    },
]

# Frozen for the multiple-object fix before live execution. Prior expressions above
# remain regressions; expectations are independently supplied from fixture facts.
MULTIPLE_REFERENCE_HOLDOUT = [
    {
        "id": "p3a_selected_pair",
        "message": "刚才那三款，中间那款不用看了。只把排第一和排第三的当前价格对照给我。",
        "memory": {"catalog": {"candidate_sku_ids": [201, 202, 101]}},
        "dispositions": ["tool_planning"],
        "tools": ["catalog_search"],
        "review": ["第一款201为199，第三款101为260；不查第二款202，不要求强拆任务",
                   "两对象分别重新查询，回答不得混用价格，比较后不选定唯一商品"],
    },
    {
        "id": "p3a_historical_orders",
        "message": "之前列表那两笔订单，第一笔和第二笔分别是什么状态？找不到的请单独说。",
        "memory": {"order": {"candidate_order_ids": [7001, 7999]}},
        "dispositions": ["tool_planning"],
        "tools": ["order_lookup"],
        "review": ["第一笔7001已发货，第二笔7999查无记录，不能推断其属于他人",
                   "分别查询和保留结果，回答不混淆，不能将最后一笔自动设为当前订单"],
    },
]
