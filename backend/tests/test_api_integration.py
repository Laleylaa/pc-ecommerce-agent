import pytest
from httpx import AsyncClient

from scripts.seed_user_isolation import (
    DEMO_PASSWORD as ISOLATION_PASSWORD,
)
from scripts.seed_user_isolation import (
    seed_user_isolation,
)


@pytest.mark.parametrize(
    ("message", "expected_count"),
    [
        ("推荐一个键盘", 1),
        ("推荐一个版本的键盘", 1),
        ("推荐两个键盘", 2),
    ],
)
@pytest.mark.asyncio
async def test_chat_recommendation_returns_distinct_spu_series(
    api_client: AsyncClient,
    auth_headers: dict[str, str],
    message: str,
    expected_count: int,
) -> None:
    response = await api_client.post(
        "/api/chat",
        json={"message": message},
        headers=auth_headers,
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["boundary"]["classification"] == "in_scope_auto"
    assert len(payload["products"]) == expected_count
    assert len({product["spu_id"] for product in payload["products"]}) == expected_count
    assert all(product["entity_scope"] == "spu" for product in payload["products"])
    assert all(product["spu_title"] for product in payload["products"])
    assert all(product["series_sku_count"] >= 1 for product in payload["products"])
    assert "系列库存" in payload["answer"]


@pytest.mark.asyncio
async def test_chat_followup_switches_from_recommended_spu_to_its_sku_variants(
    api_client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    recommendation = await api_client.post(
        "/api/chat",
        json={"message": "推荐一个键盘"},
        headers=auth_headers,
    )
    assert recommendation.status_code == 200
    first = recommendation.json()
    assert len(first["products"]) == 1
    selected_spu_id = first["products"][0]["spu_id"]
    assert first["products"][0]["entity_scope"] == "spu"

    variants = await api_client.post(
        "/api/chat",
        json={
            "message": "查看这个键盘所有版本",
            "conversation_id": first["conversation_id"],
        },
        headers=auth_headers,
    )
    assert variants.status_code == 200
    second = variants.json()
    assert second["products"], second
    assert {product["spu_id"] for product in second["products"]} == {
        selected_spu_id
    }
    assert all(product["entity_scope"] == "sku" for product in second["products"])

    cheapest = await api_client.post(
        "/api/chat",
        json={
            "message": "这个键盘哪个版本最便宜",
            "conversation_id": first["conversation_id"],
        },
        headers=auth_headers,
    )
    assert cheapest.status_code == 200
    third = cheapest.json()
    assert len(third["products"]) == 1
    assert third["products"][0]["spu_id"] == selected_spu_id
    assert third["products"][0]["entity_scope"] == "sku"
    assert third["products"][0]["ranking_scope"] == "sku"


@pytest.mark.asyncio
async def test_chat_recommends_real_dataset_wireless_mouse(
    api_client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    response = await api_client.post(
        "/api/chat",
        json={"message": "推荐 1200 元以内 Codex 无线鼠标"},
        headers=auth_headers,
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["boundary"]["classification"] == "in_scope_auto"
    assert payload["intent"] == "catalog_search"
    assert payload["products"][0]["title"] == "Razer Codex Viper V3 Pro White"
    assert "Wireless" in payload["products"][0]["specs"]["connection_type"]
    assert "兼容" in payload["answer"] or "适合" in payload["answer"]


@pytest.mark.asyncio
async def test_chat_returns_rag_evidence_for_after_sales_policy(
    api_client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    response = await api_client.post(
        "/api/chat",
        json={"message": "退货政策怎么走"},
        headers=auth_headers,
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["boundary"]["classification"] == "in_scope_auto"
    assert payload["intent"] == "policy_search"
    assert payload["evidence"][0]["title"] == "测试退货政策"
    assert "测试退货政策" in payload["answer"]


@pytest.mark.asyncio
async def test_chat_stream_emits_progress_context_delta_and_done(
    api_client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    async with api_client.stream(
        "POST",
        "/api/chat/stream",
        json={"message": "退货政策怎么走"},
        headers=auth_headers,
    ) as response:
        assert response.status_code == 200
        body = "".join([chunk async for chunk in response.aiter_text()])

    events = _parse_sse(body)
    event_types = [event["type"] for event in events]

    assert event_types[0] == "run_started"
    assert "boundary" in event_types
    assert "tool_call" in event_types
    assert "context" in event_types
    assert "delta" in event_types
    assert event_types[-1] == "done"
    assert any(
        event["type"] == "context" and event["evidence"][0]["title"] == "测试退货政策"
        for event in events
    )
    deltas = [event["delta"] for event in events if event["type"] == "delta"]
    assert deltas == [events[-1]["response"]["answer"]]
    assert events[-1]["response"]["evidence"][0]["title"] == "测试退货政策"


@pytest.mark.asyncio
async def test_orders_latest_returns_seeded_order(
    api_client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    response = await api_client.get("/api/orders/latest", headers=auth_headers)

    assert response.status_code == 200
    payload = response.json()
    assert payload["status_label"] == "已发货"
    assert payload["items"]
    assert payload["logistics"]["logistic_no"] == "SF100200300400"


@pytest.mark.asyncio
async def test_order_analysis_reference_and_eta_unsupported_across_conversation(
    api_client: AsyncClient,
    db_session_factory,
) -> None:
    async with db_session_factory() as session:
        await seed_user_isolation(session)
        await session.commit()

    login = await api_client.post(
        "/api/auth/login",
        json={
            "login_identifier": "test_user_001@example.com",
            "password": ISOLATION_PASSWORD,
        },
    )
    assert login.status_code == 200
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    latest = await api_client.post(
        "/api/chat",
        json={"message": "最新的一笔订单是什么"},
        headers=headers,
    )
    assert latest.status_code == 200
    latest_payload = latest.json()
    assert latest_payload["order"]["id"] == 991000000101

    keyboard = await api_client.post(
        "/api/chat",
        json={
            "message": "我买过什么键盘吗",
            "conversation_id": latest_payload["conversation_id"],
        },
        headers=headers,
    )
    assert keyboard.status_code == 200

    logistics = await api_client.post(
        "/api/chat",
        json={
            "message": "这单的物流状态是什么",
            "conversation_id": keyboard.json()["conversation_id"],
        },
        headers=headers,
    )
    assert logistics.status_code == 200
    logistics_payload = logistics.json()
    assert logistics_payload["order"]["id"] == 991000000103

    async with api_client.stream(
        "POST",
        "/api/chat/stream",
        json={
            "message": "这个多久能到？",
            "conversation_id": logistics_payload["conversation_id"],
        },
        headers=headers,
    ) as response:
        assert response.status_code == 200
        body = "".join([chunk async for chunk in response.aiter_text()])

    events = _parse_sse(body)
    completed = [
        event
        for event in events
        if event.get("type") == "tool_call"
        and event.get("tool_name") == "order_lookup"
        and event.get("status") == "completed"
    ]
    assert completed
    output = completed[-1]["output"]["output"]
    assert output["result_type"] == "unsupported"
    assert output["diagnostics"][0]["code"] == "unsupported_query"
    assert output["query_plan"]["unsupported_fields"] == ["estimated_delivery_time"]
    done = [event for event in events if event.get("type") == "done"][-1]
    assert done["response"]["boundary"]["classification"] == "human_handoff_required"


@pytest.mark.asyncio
async def test_after_sales_endpoint_records_handoff_request(
    api_client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    chat_response = await api_client.post(
        "/api/chat",
        json={"message": "我要申请退货"},
        headers=auth_headers,
    )
    assert chat_response.status_code == 200
    conversation_id = chat_response.json()["conversation_id"]

    response = await api_client.post(
        "/api/after-sales",
        json={
            "session_id": conversation_id,
            "order_id": 202607020001,
            "request_type": "return",
            "reason": "商品不符合预期",
        },
        headers=auth_headers,
    )

    assert response.status_code == 202
    payload = response.json()
    assert payload["request_id"]
    assert payload["status"] == "pending"
    assert "不会自动办理业务操作" in payload["message"]

    query_response = await api_client.get(
        f"/api/after-sales/handoff-requests/{payload['request_id']}",
        headers=auth_headers,
    )
    assert query_response.status_code == 200
    query_payload = query_response.json()
    assert query_payload["id"] == payload["request_id"]
    assert query_payload["session_id"] == conversation_id
    assert query_payload["order_id"] == 202607020001
    assert query_payload["request_type"] == "return"
    assert query_payload["boundary_category"] == "human_handoff_required"
    assert query_payload["status"] == "pending"


def _parse_sse(body: str) -> list[dict]:
    import json

    events: list[dict] = []
    for block in body.strip().split("\n\n"):
        data_lines = [
            line.removeprefix("data: ").strip()
            for line in block.splitlines()
            if line.startswith("data:")
        ]
        if data_lines:
            events.append(json.loads("\n".join(data_lines)))
    return events
