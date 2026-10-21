
import pytest
from app.services.memory import MemoryService


def test_one_turn_request_does_not_create_long_term_facts() -> None:
    service = MemoryService()

    facts = service.extract_long_term_facts(
        "我偏好无线鼠标，预算 500 元以内，手机号 13800138000，地址是测试路 1 号"
    )

    assert facts == []


def test_extract_long_term_facts_accepts_explicit_stable_preferences() -> None:
    service = MemoryService()

    facts = service.extract_long_term_facts("请记住，我通常偏好无线鼠标，长期预算 500 元以内")

    assert facts == [
        {
            "scope": "user",
            "fact_type": "preference",
            "key": "connection_preference",
            "value": "偏好无线设备",
            "value_json": {
                "preference": "wireless",
                "negated": False,
                "operation": "set",
            },
            "confidence": 0.8,
        },
        {
            "scope": "user",
            "fact_type": "preference",
            "key": "budget_preference",
            "value": "偏好 500 元以内预算",
            "value_json": {
                "amount": 500.0,
                "currency": "CNY",
                "maximum": True,
                "operation": "set",
            },
            "confidence": 0.7,
        },
    ]


def test_extract_long_term_facts_preserves_negation_as_structured_correction() -> None:
    service = MemoryService()

    facts = service.extract_long_term_facts("以后不要无线")

    assert facts == [
        {
            "scope": "user",
            "fact_type": "preference",
            "key": "connection_preference",
            "value": "不偏好无线设备",
            "value_json": {
                "preference": "wireless",
                "negated": True,
                "operation": "exclude",
            },
            "confidence": 0.8,
        }
    ]


def test_stable_marker_only_applies_to_the_clause_that_contains_it() -> None:
    service = MemoryService()

    assert service.extract_long_term_facts("这次预算 500 元，以后再说") == []
    assert service.extract_long_term_facts("以后不要无线但这次预算 500 元以内") == [
        {
            "scope": "user",
            "fact_type": "preference",
            "key": "connection_preference",
            "value": "不偏好无线设备",
            "value_json": {
                "preference": "wireless",
                "negated": True,
                "operation": "exclude",
            },
            "confidence": 0.8,
        }
    ]


@pytest.mark.parametrize(
    ("message", "key", "value", "value_json"),
    [
        (
            "以后不玩游戏",
            "usage_preference",
            "不偏好游戏场景",
            {"usage": "gaming", "negated": True, "operation": "exclude"},
        ),
        (
            "以后不要罗技",
            "brand_preference",
            "不偏好 罗技 品牌",
            {"brand": "罗技", "negated": True, "operation": "exclude"},
        ),
        (
            "以后不要logitech",
            "brand_preference",
            "不偏好 Logitech 品牌",
            {"brand": "Logitech", "negated": True, "operation": "exclude"},
        ),
    ],
)
def test_explicit_usage_and_brand_negation_are_structured_corrections(
    message: str,
    key: str,
    value: str,
    value_json: dict,
) -> None:
    facts = MemoryService().extract_long_term_facts(message)

    assert len(facts) == 1
    assert facts[0]["key"] == key
    assert facts[0]["value"] == value
    assert facts[0]["value_json"] == value_json
