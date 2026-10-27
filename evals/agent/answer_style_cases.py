"""Synthetic, fixed answer handoffs and independent review criteria; no database."""

from datetime import UTC, datetime

from app.agent.loop.evidence import EvidenceRecord, ToolInvocation
from app.agent.loop.independent_answer import AnswerInput
from app.agent.loop.query_loop import project_record
from app.tools.schemas import ToolError, ToolExecutionResult


def handoff(message, *, outputs=(), disposition="tool_planning", actions=()):
    goals, records = [], []
    for index, (name, output) in enumerate(outputs, 1):
        goal_id = f"g{index}"
        now = datetime(2026, 10, 20, tzinfo=UTC)
        record = EvidenceRecord(
            evidence_id=f"ev_fixture_{index}",
            invocation=ToolInvocation(
                invocation_id=f"attempt_{index}", operation_id=f"op_{index}", goal_id=goal_id,
                tool_name=name, arguments={"query": message}, attempt=1,
                started_at=now, elapsed_seconds=0, status="succeeded",
            ),
            result=ToolExecutionResult(tool_name=name, ok=True, output=output),
            recorded_at=now,
        )
        goals.append({
            "id": goal_id, "source_text": message, "query": message,
            "disposition": disposition,
            "execution": {
                "attempt_count": 1, "ok_count": 1, "error_count": 0, "active_result_count": 1,
            },
            "evidence_ids": [record.evidence_id], "prerequisites_met": True,
        })
        records.append(project_record(record))
    if not goals:
        goals = [{
            "id": "g1", "source_text": message, "query": message,
            "disposition": disposition, "evidence_ids": [],
            "execution": {
                "attempt_count": 0, "ok_count": 0, "error_count": 0, "active_result_count": 0,
            },
        }]
    return AnswerInput(
        message=message, goals=goals, evidence=records,
        suggested_actions=list(actions), stop_reason="completed",
    )


VARIANTS = [
    {
        "sku_id": 9100 + i, "title": f"测试影刃鼠标 {color}{edition}",
        "price": 129 + i * 5, "stock": 20 + i * 5,
        "specs": {"color": color, "connection_type": "有线" if i % 2 == 0 else "蓝牙"},
    }
    for i, (color, edition) in enumerate([
        ("黑色", "标准版"), ("白色", "标准版"), ("银色", "增强版"), ("粉色", "增强版"),
        ("黑色", "无线版"), ("白色", "无线版"), ("银色", "旗舰版"), ("粉色", "旗舰版"),
        ("黑色", "套装版"), ("白色", "套装版"), ("银色", "电竞版"), ("粉色", "电竞版"),
    ])
]

STOCK = {
    "status": "success", "query_state": "completed", "match_kind": "exact",
    "is_exhaustive": True, "returned_count": 1, "total_match_count": 1,
    "products": [{
        "spu_id": 91, "sku_id": 9100, "title": "测试影刃鼠标", "entity_scope": "spu",
        "price": 129, "stock": 20, "series_min_price": 129, "series_max_price": 184,
        "series_total_stock": 570, "series_sku_count": 12, "series_variants": VARIANTS,
        "field_issues": [{
            "sku_id": 9104, "field": "connection_type", "issue_type": "conflict",
            "message": "黑色无线版标题写无线，规格为有线，是否无线无法确认。",
        }],
    }],
}
ORDER = {
    "result_type": "single_order",
    "order": {
        "id": 8201, "order_no": "TEST-8201", "status_label": "已发货",
        "created_at": "2026-10-18T09:00:00", "paid_at": "2026-10-18T09:05:00",
        "total_amount": "199.00", "paid_amount": "199.00", "shipping_fee": "0.00",
        "items": [{"title": "测试键盘 白色标准版", "quantity": 1, "price": "199.00"}],
        "logistics": {
            "company": "测试物流", "tracking_no": "TEST-TRACK",
            "traces": [
                {"time": "2026-10-18T12:00:00", "status": "已揽收"},
                {"time": "2026-10-19T10:00:00", "status": "运输中"},
            ],
        },
    },
}


def product(title, price, *, sku_id=9201, connection="USB"):
    return {
        "status": "success", "query_state": "completed", "match_kind": "exact",
        "products": [{
            "title": title, "sku_id": sku_id, "price": price, "stock": 4,
            "specs": {"connection_type": connection},
        }],
    }


def partial_policy_handoff():
    """Reproduce AL-012's same-goal empty then useful evidence, without retrieval randomness."""
    facts = handoff("本店退货对配件有什么要求？", outputs=[
        ("policy_search", {"result_type": "empty", "documents": []}),
        ("policy_search", {"result_type": "documents", "documents": [{
            "source_type": "knowledge_document", "source_id": 8001,
            "title": "测试店铺键盘七天无理由退货政策",
            "snippet": (
                "仅用于评测的虚构政策：键盘拆封后，商品完好、配件齐全且不影响二次销售的，"
                "可在签收七天内申请无理由退货；拆封本身不自动排除退货。具体审核由人工处理。"
            ),
        }]}),
    ])
    facts.evidence[1]["invocation"]["goal_id"] = "g1"
    facts.goals = [{
        **facts.goals[0],
        "execution": {
            "attempt_count": 2, "ok_count": 2, "error_count": 0, "active_result_count": 2,
        },
        "query": "本店退货政策对配件（如线材、说明书、包装配件）的要求",
        "evidence_ids": ["ev_fixture_1", "ev_fixture_2"],
    }]
    return facts


def failed_known_pair_handoff():
    """Known positions/identities remain available even when both lookups fail."""
    facts = handoff("刚才那三款，只把第一和第三款的当前价格对照给我。")
    now = datetime(2026, 10, 20, tzinfo=UTC)
    for index in (1, 2):
        facts.evidence.append(project_record(EvidenceRecord(
            evidence_id=f"ev_failed_pair_{index}",
            invocation=ToolInvocation(
                invocation_id=f"attempt_{index}", operation_id=f"op_{index}", goal_id="g1",
                tool_name="catalog_search",
                arguments={"query": "查询两款当前价格", "scope": {"sku_ids": [201, 101]}},
                attempt=1, started_at=now, elapsed_seconds=0, status="failed",
            ),
            result=ToolExecutionResult(
                tool_name="catalog_search", ok=False,
                error=ToolError(
                    code="planning_failed", message="查询计划未通过校验", next_action="stop",
                ),
            ),
            recorded_at=now,
        )))
    facts.goals[0].update(
        execution={"attempt_count": 2, "ok_count": 0, "error_count": 2, "active_result_count": 2},
        evidence_ids=[r["evidence_id"] for r in facts.evidence],
        trusted_references=[
            {"reference": {"kind": "catalog_candidate", "position": 1},
             "identity": {"sku_id": 201}},
            {"reference": {"kind": "catalog_candidate", "position": 3},
             "identity": {"sku_id": 101}},
        ],
    )
    return facts


def catalog_granularity_cases():
    """Distinct model/subset/sample prices expose incorrect cross-level field selection."""
    products = [{
        "spu_id": 71, "sku_id": 7101, "representative_sku_id": 7101,
        "entity_scope": "spu", "title": "测研 云杉 K7 机械键盘",
        "spu_title": "测研 云杉 K7 机械键盘", "price": "219", "stock": 7,
        "series_min_price": "219", "series_max_price": "319", "series_total_stock": 23,
        "series_sku_count": 2, "series_common_specs": {"wireless": "是"},
        "series_option_specs": {"connection_type": ["蓝牙", "2.4G"]},
        "model_min_price": "169", "model_max_price": "599", "model_sku_count": 4,
        "series_variants": [{"sku_id": 7101, "title": "测研 云杉 K7 白色标准版",
                             "price": "219", "stock": 7,
                             "specs": {"color": "白色", "connection_type": "蓝牙"}}],
        "variant_details_complete": False, "match_kind": "exact",
    }, {
        "spu_id": 72, "sku_id": 7201, "representative_sku_id": 7201,
        "entity_scope": "spu", "title": "测研 湖光 K8 机械键盘",
        "spu_title": "测研 湖光 K8 机械键盘", "price": "289", "stock": 11,
        "series_min_price": "289", "series_max_price": "459", "series_total_stock": 31,
        "series_sku_count": 2, "series_common_specs": {"wireless": "是"},
        "model_min_price": "199", "model_max_price": "699", "model_sku_count": 5,
        "series_variants": [{"sku_id": 7201, "title": "测研 湖光 K8 白色标准版",
                             "price": "289", "stock": 11,
                             "specs": {"color": "白色", "connection_type": "蓝牙"}}],
        "variant_details_complete": False, "match_kind": "exact",
    }]
    output = {
        "status": "success", "result_type": "products", "result_purpose": "recommendation",
        "selection_scope": "spu", "ranking_strategy": "relevance",
        "match_kind": "exact", "returned_count": 2, "total_match_count": 2,
        "is_exhaustive": True, "products": products,
    }
    sku = {
        **products[0], "entity_scope": "sku", "title": "测研 云杉 K7 黑色增强版",
        "sku_id": 7102, "representative_sku_id": None, "price": "319", "stock": 16,
        "specs": {"color": "黑色", "connection_type": "2.4G"}, "series_variants": [],
    }
    return [
        {"id": "spu-recommendation", "paired": False,
         "facts": handoff("推荐两款500元以内的无线机械键盘", outputs=[("catalog_search", output)]),
         "review": ["主推荐为云杉K7和湖光K8型号，不默认选白色标准版或蓝牙版本",
                    "报价分别为本次候选版本219—319和289—459元，与型号卡片一致",
                    "不使用全型号169—599/199—699或单版本价替代候选区间",
                    "未问库存不报7/11/23/31件，不把部分版本属性说成全部"]},
        {"id": "sku-recommendation", "paired": False,
         "facts": handoff("云杉K7里推荐黑色2.4G版本，报它的价格和库存", outputs=[
             ("catalog_search", {**output, "selection_scope": "sku", "returned_count": 1,
                                 "total_match_count": 1, "products": [sku]}),
         ]),
         "review": ["黑色增强版319元、库存16件、2.4G准确",
                    "不改成型号区间219—319、型号起价169或候选库存23"]},
        {"id": "spu-candidate-stock", "paired": False,
         "facts": handoff("这两个型号符合条件的候选版本分别有多少库存？不用展开具体版本", outputs=[
             ("catalog_lookup", {**output, "result_purpose": "lookup"}),
         ]),
         "review": ["云杉K7候选合计23件，湖光K8候选合计31件，限定本次候选范围",
                    "不使用代表版本7/11件，不展开版本或称型号全部库存"]},
    ]


def style_cases():
    # Fixed before model runs. Rubrics describe user-visible outcomes, not prompt wording.
    return [
        *catalog_granularity_cases(),
        {
            "id": "identity", "paired": True,
            "facts": handoff("你好，你是谁", disposition="direct_response"),
            "review": ["明确小帕瓦及智能助手身份", "简短自然，无无关退款/写操作声明"],
        },
        {
            "id": "capabilities", "paired": True,
            "facts": handoff("你好，你能做什么", disposition="direct_response"),
            "review": ["小帕瓦身份与已有服务范围", "日常用语及易读组织，无未支持能力"],
        },
        {
            "id": "stock", "paired": True,
            "facts": handoff("测试影刃鼠标有货吗", outputs=[("catalog_search", STOCK)]),
            "review": ["先说有货，限定本次12版本", "不把12条塞入长段", "保留黑色无线版冲突"],
        },
        {
            "id": "order", "paired": True,
            "facts": handoff("帮我看看最近的订单到哪了", outputs=[("order_lookup", ORDER)]),
            "review": ["先说已发货/运输中", "必要信息易读，无全字段倾倒或虚构到达日期"],
        },
        {
            "id": "thanks", "paired": False,
            "facts": handoff("好，明白啦，谢谢你", disposition="direct_response"),
            "review": ["自然简短收尾", "不再次介绍能力或无关业务限制"],
        },
        {
            "id": "all_versions", "paired": False,
            "facts": handoff(
                "把这款鼠标查到的全部版本、价格和库存逐项列给我",
                outputs=[("catalog_search", STOCK)],
            ),
            "review": ["完整列12项且数值准确", "用列表或表格组织", "保留冲突，不为简短丢项"],
        },
        {
            "id": "comparison", "paired": False,
            "facts": handoff("比较这两款键盘的价格和连接方式", outputs=[
                ("catalog_search", product("测试甲键盘", 199)),
                ("catalog_search", product("测试乙键盘", 299, sku_id=9202, connection="蓝牙")),
            ]),
            "review": ["199/USB与299/蓝牙分别正确", "对比清楚，不编优点或虚构胜负"],
        },
        {
            "id": "partial", "paired": False,
            "facts": handoff("比较甲和乙的价格，找不到的直接说", outputs=[
                ("catalog_search", product("测试甲键盘", 199)),
                ("catalog_search", {"status": "not_found", "products": []}),
            ]),
            "review": ["保留甲199及乙未查到", "不伪造比较结果", "简短说明缺口"],
        },
        {
            "id": "missing", "paired": False,
            "facts": handoff("这款键盘能连蓝牙吗？资料没写就说不知道", outputs=[
                ("catalog_search", {
                    "status": "success", "products": [
                        {"title": "测试缺资料键盘", "sku_id": 9501, "price": 199, "specs": {}},
                    ],
                }),
            ]),
            "review": ["无法确认而非不支持", "不播报内部字段或无关价格"],
        },
        {
            "id": "knowledge", "paired": False,
            "facts": handoff("红轴和青轴有什么区别？只按找到的资料讲", outputs=[
                ("knowledge_search", {
                    "documents": [{"title": "测试资料", "snippet": "机械轴与磁轴触发方式不同。"}],
                }),
            ]),
            "review": ["资料不覆盖红青轴则说明不足", "不借简洁语气输出无依据知识"],
        },
        {
            "id": "handoff", "paired": False,
            "facts": handoff(
                "我想找人工确认退货，你告诉我怎么操作就行", disposition="human_handoff",
                actions=[{"label": "转人工客服", "payload": {"handoff": True}}],
            ),
            "review": ["实际按钮与用户确认", "不声称已提交、退款或保证人工接单", "没有虚构页面"],
        },
        {
            "id": "phone", "paired": False,
            "facts": handoff("必须给你手机号才能查我的订单吗", disposition="direct_response"),
            "review": ["无需手机号，按登录身份查询", "说明订单号/最近订单等已有方式"],
        },
        {
            "id": "stock_brief_holdout", "paired": False,
            "facts": handoff(
                "别把每个颜色都报一遍，只说这款现在能不能买，多少钱起就好",
                outputs=[("catalog_search", STOCK)],
            ),
            "review": ["有货，已查版本129元起", "简短不展开12版本", "不暗示全型号均价或总版本数"],
        },
        {
            "id": "handoff_status_holdout", "paired": False,
            "facts": handoff(
                "点转人工是不是就有人接了？还是我还得操作什么", disposition="human_handoff",
                actions=[{"label": "转人工客服", "payload": {"handoff": True}}],
            ),
            "review": ["点击后仍须确认提交", "记录不等于接单，无保证跟进", "只说实际入口"],
        },
        {
            "id": "partial_policy_after_empty", "paired": False,
            "facts": partial_policy_handoff(),
            "review": ["必须回答已知的配件齐全要求，不能说完全未查到配件规定",
                       "可以说明没有更细的配件清单；不能猜包装、线材或说明书必需规则",
                       "保留键盘政策范围及人工审核边界；不把blocked当作所有证据无效"],
        },
        {
            "id": "failed_known_pair", "paired": False,
            "facts": failed_known_pair_handoff(),
            "review": ["承认查价失败，不能编造价格",
                       "对象对应关系已知，不能声称缺少历史或要求用户重新确认商品身份",
                       "不复述工具内部错误码，不承诺本阶段重新查询"],
        },
    ]
