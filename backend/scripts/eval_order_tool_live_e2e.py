"""Run a live Order Tool classification evaluation through the authenticated chat API.

This is intentionally a manual evaluation script rather than a pytest test: it uses the
configured LLM for Router, planner, and answer synthesis. Database writes made by the chat
runtime are wrapped in an outer transaction and rolled back when the evaluation finishes.
"""

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Literal

from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.api.dependencies import get_session
from app.core.config import get_settings
from app.main import app
from scripts.seed_user_isolation import DEMO_PASSWORD

EVAL_LOGIN_IDENTIFIER = "test_user_001@example.com"
EXPLICIT_ORDER_ID = 991_000_000_103


@dataclass(frozen=True)
class EvalCase:
    name: str
    query: str
    expected_result_type: str | None
    expected_diagnostic: str | None = None
    expected_query_mode: str | None = None
    expected_boundary: Literal["in_scope_auto", "human_handoff_required"] = "in_scope_auto"
    required_fields: frozenset[str] = field(default_factory=frozenset)
    result_collection: Literal["order", "candidates", "analysis_orders", "none"] = "none"


SUPPORTED_CASES = (
    EvalCase(
        name="count",
        query="我到底有多少订单呀？",
        expected_result_type="order_count",
        expected_query_mode="count",
    ),
    EvalCase(
        name="all_summary",
        query="把我所有订单简单列一下就行",
        expected_result_type="order_candidates",
        expected_query_mode="all",
        result_collection="candidates",
        required_fields=frozenset(
            {
                "id",
                "status",
                "status_label",
                "total_amount",
                "pay_amount",
                "created_at",
                "item_count",
                "first_item_name",
                "express_company",
                "logistic_no",
                "latest_logistics_event",
            }
        ),
    ),
    EvalCase(
        name="recent_three",
        query="先翻我最近三笔订单出来，简单点就好",
        expected_result_type="order_candidates",
        expected_query_mode="recent",
        result_collection="candidates",
    ),
    EvalCase(
        name="latest_detail",
        query="最新那一单的完整信息给我瞅瞅",
        expected_result_type="single_order",
        expected_query_mode="latest",
        result_collection="order",
    ),
    EvalCase(
        name="explicit_safe_detail_fields",
        query=(
            f"帮我细看订单 {EXPLICIT_ORDER_ID}：总额、实付、运费、下单支付发货时间、"
            "买了啥和完整物流轨迹都说一下"
        ),
        expected_result_type="single_order",
        expected_query_mode="explicit",
        result_collection="order",
        required_fields=frozenset(
            {
                "id",
                "status",
                "status_label",
                "total_amount",
                "pay_amount",
                "freight_amount",
                "created_at",
                "pay_at",
                "delivery_at",
                "items",
                "logistics",
            }
        ),
    ),
    EvalCase(
        name="all_item_details",
        query="所有订单里买的东西、版本、单价和数量都摊开给我看看",
        expected_result_type="order_analysis",
        expected_query_mode="analysis",
        result_collection="analysis_orders",
    ),
    EvalCase(
        name="purchase_history_match",
        query="我是不是买过 Isolation Mock 的鼠标，压在哪张订单里了？",
        expected_result_type="order_analysis",
        expected_query_mode="analysis",
        result_collection="analysis_orders",
    ),
    EvalCase(
        name="latest_shipping_event",
        query="我最新那单物流现在到哪一步了？",
        expected_result_type="single_order",
        expected_query_mode="latest",
        result_collection="order",
    ),
    EvalCase(
        name="llm_colloquial_supported",
        query="我头前收的那只测试鼠标到底压在哪张单子里，帮我翻出来",
        expected_result_type="order_analysis",
        expected_query_mode="analysis",
        result_collection="analysis_orders",
    ),
)

UNSUPPORTED_CASES = (
    EvalCase(
        name="receiver_address",
        query="把我每一单最后寄到哪儿整理成地址表",
        expected_result_type="unsupported",
        expected_diagnostic="unsupported_query",
        expected_boundary="human_handoff_required",
    ),
    EvalCase(
        name="receiver_name",
        query="我这些订单分别是谁收的，名字列一下",
        expected_result_type="unsupported",
        expected_diagnostic="unsupported_query",
        expected_boundary="human_handoff_required",
    ),
    EvalCase(
        name="receiver_phone",
        query="每张订单留的联系电话都翻出来给我",
        expected_result_type="unsupported",
        expected_diagnostic="unsupported_query",
        expected_boundary="human_handoff_required",
    ),
    EvalCase(
        name="payment_method",
        query=f"订单 {EXPLICIT_ORDER_ID} 当时到底拿什么方式付的钱？",
        expected_result_type="unsupported",
        expected_diagnostic="unsupported_query",
        expected_boundary="human_handoff_required",
    ),
    EvalCase(
        name="download_export",
        query="帮我把全部订单导成 Excel 下载下来",
        expected_result_type=None,
        expected_boundary="human_handoff_required",
    ),
    EvalCase(
        name="coupon_breakdown",
        query="从订单记录里扒一下，我每一单到底叠了哪些券、各省了多少？",
        expected_result_type="unsupported",
        expected_diagnostic="unsupported_query",
        expected_boundary="human_handoff_required",
    ),
    EvalCase(
        name="invoice_title",
        query="把每张订单当时开的发票抬头都给我找出来",
        expected_result_type="unsupported",
        expected_diagnostic="unsupported_query",
        expected_boundary="human_handoff_required",
    ),
    EvalCase(
        name="warehouse",
        query="我这些单子各是从哪个仓库拣出来的？",
        expected_result_type="unsupported",
        expected_diagnostic="unsupported_query",
        expected_boundary="human_handoff_required",
    ),
    EvalCase(
        name="delivery_proof",
        query="每笔订单签收时快递员拍的照片还能调出来吗？",
        expected_result_type="unsupported",
        expected_diagnostic="unsupported_query",
        expected_boundary="human_handoff_required",
    ),
    EvalCase(
        name="buyer_message",
        query="我下单时好像给商家嘱咐过一句话，能把原话翻出来吗？",
        expected_result_type="unsupported",
        expected_diagnostic="unsupported_query",
        expected_boundary="human_handoff_required",
    ),
)

MIXED_CASE = EvalCase(
    name="mixed_status_and_payment_method",
    query=f"瞅下订单 {EXPLICIT_ORDER_ID} 现在啥状态，当时又拿什么方式付的？",
    expected_result_type="single_order",
    expected_diagnostic="partial_unsupported_query",
    expected_query_mode="explicit",
    expected_boundary="in_scope_auto",
    result_collection="order",
)


def _parse_sse(body: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for block in body.strip().split("\n\n"):
        data = "\n".join(
            line.removeprefix("data: ")
            for line in block.splitlines()
            if line.startswith("data: ")
        )
        if data:
            events.append(json.loads(data))
    return events


def _order_tool_output(events: list[dict[str, Any]]) -> dict[str, Any]:
    completed = [
        event
        for event in events
        if event.get("type") == "tool_call"
        and event.get("tool_name") == "order_lookup"
        and event.get("status") == "completed"
    ]
    if not completed:
        raise AssertionError("Order Tool was not called")
    execution = completed[-1].get("output")
    if not isinstance(execution, dict) or not execution.get("ok"):
        raise AssertionError(f"Order Tool execution failed: {execution}")
    output = execution.get("output")
    if not isinstance(output, dict):
        raise AssertionError(f"Order Tool returned no structured output: {execution}")
    return output


def _order_tool_input(events: list[dict[str, Any]]) -> dict[str, Any]:
    started = [
        event
        for event in events
        if event.get("type") == "tool_call"
        and event.get("tool_name") == "order_lookup"
        and event.get("status") == "started"
    ]
    if not started or not isinstance(started[-1].get("input"), dict):
        raise AssertionError("Order Tool did not expose its bound input")
    return started[-1]["input"]


def _done_response(events: list[dict[str, Any]]) -> dict[str, Any]:
    done = [event for event in events if event.get("type") == "done"]
    if not done or not isinstance(done[-1].get("response"), dict):
        raise AssertionError("Chat stream did not finish with a response")
    return done[-1]["response"]


def _assert_no_private_fields(value: Any) -> None:
    serialized = json.dumps(value, ensure_ascii=False)
    for field_name in (
        "receiver_name",
        "receiver_phone",
        "receiver_address",
        "pay_type",
        "pay_type_label",
        "payment_method",
    ):
        if field_name in serialized:
            raise AssertionError(f"private field leaked into Order Tool output: {field_name}")


def _validate_case(
    case: EvalCase,
    output: dict[str, Any],
    response: dict[str, Any],
) -> None:
    assert output["result_type"] == case.expected_result_type, (
        f"expected result_type={case.expected_result_type}, "
        f"actual={output.get('result_type')}"
    )
    if case.expected_query_mode is not None:
        assert output["query_mode"] == case.expected_query_mode, (
            f"expected query_mode={case.expected_query_mode}, "
            f"actual={output.get('query_mode')}"
        )
    diagnostic_codes = {
        diagnostic["code"] for diagnostic in output.get("diagnostics", [])
    }
    if case.expected_diagnostic is not None:
        assert case.expected_diagnostic in diagnostic_codes, (
            f"expected diagnostic={case.expected_diagnostic}, "
            f"actual={sorted(diagnostic_codes)}"
        )
    else:
        assert "unsupported_query" not in diagnostic_codes, sorted(diagnostic_codes)
        assert "partial_unsupported_query" not in diagnostic_codes, sorted(
            diagnostic_codes
        )
    actual_boundary = response["boundary"]["classification"]
    assert actual_boundary == case.expected_boundary, (
        f"expected boundary={case.expected_boundary}, actual={actual_boundary}"
    )

    if case.result_collection == "order":
        value = output.get("order")
        assert isinstance(value, dict), "expected one order fact object"
    elif case.result_collection in {"candidates", "analysis_orders"}:
        collection = output.get(case.result_collection)
        assert isinstance(collection, list) and collection, (
            f"expected non-empty {case.result_collection}"
        )
        value = collection[0]
    else:
        value = output
    assert case.required_fields.issubset(value), (
        f"missing fields={sorted(case.required_fields - set(value))}"
    )
    _assert_no_private_fields(
        {
            "order": output.get("order"),
            "candidates": output.get("candidates"),
            "analysis_orders": output.get("analysis_orders"),
        }
    )


async def _stream_chat(
    client: AsyncClient,
    headers: dict[str, str],
    query: str,
    *,
    conversation_id: int | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    payload: dict[str, Any] = {"message": query}
    if conversation_id is not None:
        payload["conversation_id"] = conversation_id
    async with client.stream(
        "POST",
        "/api/chat/stream",
        json=payload,
        headers=headers,
    ) as response:
        response.raise_for_status()
        body = "".join([chunk async for chunk in response.aiter_text()])
    events = _parse_sse(body)
    return events, _done_response(events)


async def main() -> int:
    settings = get_settings()
    if not settings.llm_api_key:
        raise RuntimeError("A configured LLM API key is required for this live evaluation")

    engine = create_async_engine(
        settings.database_url,
        pool_pre_ping=True,
        poolclass=NullPool,
    )
    connection = await engine.connect()
    transaction = await connection.begin()
    session_factory = async_sessionmaker(
        bind=connection,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )

    async def override_get_session() -> Any:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    results: list[dict[str, Any]] = []
    failures: list[str] = []
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://order-e2e",
            timeout=120,
        ) as client:
            login = await client.post(
                "/api/auth/login",
                json={
                    "login_identifier": EVAL_LOGIN_IDENTIFIER,
                    "password": DEMO_PASSWORD,
                },
            )
            login.raise_for_status()
            headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

            for case in (*SUPPORTED_CASES, *UNSUPPORTED_CASES, MIXED_CASE):
                events: list[dict[str, Any]] = []
                output: dict[str, Any] | None = None
                response: dict[str, Any] | None = None
                try:
                    events, response = await _stream_chat(
                        client,
                        headers,
                        case.query,
                    )
                    if case.expected_result_type is None:
                        assert not any(
                            event.get("type") == "tool_call"
                            and event.get("tool_name") == "order_lookup"
                            for event in events
                        ), "Router-owned case unexpectedly called Order Tool"
                        actual_boundary = response["boundary"]["classification"]
                        assert actual_boundary == case.expected_boundary, (
                            f"expected boundary={case.expected_boundary}, "
                            f"actual={actual_boundary}"
                        )
                    else:
                        output = _order_tool_output(events)
                        _validate_case(case, output, response)
                except Exception as exc:
                    failures.append(f"{case.name}: {type(exc).__name__}: {exc}")
                    results.append(
                        {
                            "case": case.name,
                            "status": "FAIL",
                            "reason": f"{type(exc).__name__}: {exc}",
                            "result_type": (
                                output.get("result_type")
                                if isinstance(output, dict)
                                else None
                            ),
                            "query_mode": (
                                output.get("query_mode")
                                if isinstance(output, dict)
                                else None
                            ),
                            "diagnostics": (
                                [
                                    item["code"]
                                    for item in output.get("diagnostics", [])
                                ]
                                if isinstance(output, dict)
                                else []
                            ),
                            "boundary": (
                                response.get("boundary", {}).get("classification")
                                if isinstance(response, dict)
                                else None
                            ),
                            "tool_names": sorted(
                                {
                                    str(event.get("tool_name"))
                                    for event in events
                                    if event.get("type") == "tool_call"
                                    and event.get("tool_name")
                                }
                            ),
                        }
                    )
                else:
                    results.append(
                        {
                            "case": case.name,
                            "status": "PASS",
                            "result_type": (
                                output["result_type"]
                                if isinstance(output, dict)
                                else "router_owned"
                            ),
                            "query_mode": (
                                output.get("query_mode")
                                if isinstance(output, dict)
                                else None
                            ),
                            "diagnostics": [
                                item["code"]
                                for item in (
                                    output.get("diagnostics", [])
                                    if isinstance(output, dict)
                                    else []
                                )
                            ],
                            "boundary": response["boundary"]["classification"],
                        }
                    )

            try:
                first_events, first_response = await _stream_chat(
                    client,
                    headers,
                    "最近两笔先给我看看，别一下子全铺开",
                )
                first_output = _order_tool_output(first_events)
                assert first_output["result_type"] == "order_candidates"
                assert first_output["returned_count"] == 2
                assert first_output["next_offset"] == 2
                conversation_id = first_response["conversation_id"]
                remaining_events, remaining_response = await _stream_chat(
                    client,
                    headers,
                    "行，那剩下的也都给我吧",
                    conversation_id=conversation_id,
                )
                remaining_output = _order_tool_output(remaining_events)
                assert remaining_output["result_type"] == "order_candidates"
                assert remaining_output["query_mode"] == "remaining"
                assert remaining_output["returned_count"] == 3
                assert remaining_output["is_exhaustive"] is True
                assert remaining_response["boundary"]["classification"] == "in_scope_auto"
            except Exception as exc:
                failures.append(f"remaining_followup: {type(exc).__name__}: {exc}")
                results.append(
                    {
                        "case": "remaining_followup",
                        "status": "FAIL",
                        "reason": f"{type(exc).__name__}: {exc}",
                    }
                )
            else:
                results.append(
                    {
                        "case": "remaining_followup",
                        "status": "PASS",
                        "result_type": remaining_output["result_type"],
                        "query_mode": remaining_output["query_mode"],
                        "returned_count": remaining_output["returned_count"],
                        "boundary": remaining_response["boundary"]["classification"],
                    }
                )

            try:
                latest_events, latest_response = await _stream_chat(
                    client,
                    headers,
                    "最新的一笔订单是什么",
                )
                latest_output = _order_tool_output(latest_events)
                latest_order_id = latest_output["order"]["id"]
                assert latest_order_id == 991_000_000_101

                keyboard_events, keyboard_response = await _stream_chat(
                    client,
                    headers,
                    "我买过什么键盘吗",
                    conversation_id=latest_response["conversation_id"],
                )
                keyboard_output = _order_tool_output(keyboard_events)
                assert keyboard_output["result_type"] == "order_analysis"
                assert keyboard_output["focus_order_ids"] == [991_000_000_103]

                logistics_events, logistics_response = await _stream_chat(
                    client,
                    headers,
                    "这单的物流状态是什么",
                    conversation_id=keyboard_response["conversation_id"],
                )
                logistics_input = _order_tool_input(logistics_events)
                logistics_output = _order_tool_output(logistics_events)
                assert logistics_input["order_id"] == 991_000_000_103
                assert logistics_output["order"]["id"] == 991_000_000_103
                assert logistics_response["boundary"]["classification"] == "in_scope_auto"

                eta_events, eta_response = await _stream_chat(
                    client,
                    headers,
                    "这个多久能到？",
                    conversation_id=logistics_response["conversation_id"],
                )
                eta_input = _order_tool_input(eta_events)
                eta_output = _order_tool_output(eta_events)
                assert eta_input["order_id"] == 991_000_000_103
                assert eta_output["result_type"] == "unsupported"
                assert eta_output["diagnostics"][0]["code"] == "unsupported_query"
                assert eta_output["query_plan"]["unsupported_fields"] == [
                    "estimated_delivery_time"
                ]
                assert eta_response["boundary"]["classification"] == (
                    "human_handoff_required"
                )
            except Exception as exc:
                failures.append(f"analysis_reference_followup: {type(exc).__name__}: {exc}")
                results.append(
                    {
                        "case": "analysis_reference_followup",
                        "status": "FAIL",
                        "reason": f"{type(exc).__name__}: {exc}",
                    }
                )
            else:
                results.append(
                    {
                        "case": "analysis_reference_followup",
                        "status": "PASS",
                        "analysis_focus_order_ids": keyboard_output["focus_order_ids"],
                        "followup_bound_order_id": logistics_input["order_id"],
                        "result_order_id": logistics_output["order"]["id"],
                        "eta_result_type": eta_output["result_type"],
                        "eta_diagnostic": eta_output["diagnostics"][0]["code"],
                        "eta_boundary": eta_response["boundary"]["classification"],
                    }
                )
    finally:
        app.dependency_overrides.clear()
        await transaction.rollback()
        await connection.close()
        await engine.dispose()

    print(json.dumps(results, ensure_ascii=False, indent=2))
    if failures:
        print("\nFailures:")
        for failure in failures:
            print(f"- {failure}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
