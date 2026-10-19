"""Backend-only input rules and templates, evaluated before any model call.

Match the current message as a whole. No model, history, database or browser dependency.
"""

import re
from dataclasses import dataclass
from typing import Literal

MAX_MESSAGE_CHARACTERS = 1000


@dataclass(frozen=True)
class InputRule:
    id: str
    category: str
    method: Literal["over_limit", "blank", "exact", "fullmatch"]
    message: str
    phrases: tuple[str, ...] = ()
    pattern: str = ""
    disposition: str = "direct_response"
    trailing_punctuation: str = ""
    recorded_message: str | None = None


RULES: tuple[InputRule, ...] = (
    InputRule(
        id="message_too_long",
        category="invalid_input",
        method="over_limit",
        recorded_message="[输入超过长度限制，本轮未处理]",
        message=(
            f"这条消息太长了，单条最多支持 {MAX_MESSAGE_CHARACTERS:,} 个字符。"
            "请精简后重新发送，保留商品名称、预算和必要条件。本条消息尚未处理。"
        ),
    ),
    InputRule(
        id="blank_message",
        category="invalid_input",
        method="blank",
        message="请输入你想咨询的问题。",
    ),
    InputRule(
        id="greeting",
        category="social",
        method="exact",
        phrases=("你好", "您好", "hello", "hi"),
        trailing_punctuation="。！!?？",
        message="你好，我是商城 AI 客服，可以帮你查询商品、比较外设和查看本人订单。",
    ),
    InputRule(
        id="thanks",
        category="social",
        method="exact",
        phrases=("谢谢", "谢谢你", "谢谢您", "thank you", "thanks"),
        trailing_punctuation="。！!",
        message="不客气，有外设、本人订单或商城政策问题都可以继续问我。",
    ),
    InputRule(
        id="goodbye",
        category="social",
        method="exact",
        phrases=("再见", "拜拜", "bye", "goodbye"),
        trailing_punctuation="。！!",
        message="再见，有需要时欢迎继续咨询。",
    ),
    InputRule(
        id="identity",
        category="assistant_help",
        method="exact",
        phrases=("你是谁", "你是真人吗", "你是人工客服吗", "你是人工客服还是智能客服"),
        trailing_punctuation="。？?",
        message="我是 PC 外设商城的 AI 客服，不是真人客服。需要人工处理时，可以提交人工请求。",
    ),
    InputRule(
        id="capabilities",
        category="assistant_help",
        method="exact",
        phrases=("你能做什么", "你能帮我什么", "怎么使用你", "怎么用你"),
        trailing_punctuation="。？?",
        message=(
            "我可以帮你查询、推荐和比较 PC 外设，查询本人订单物流，咨询商城政策。"
            "例如：推荐300元以内的无线鼠标。我不能直接办理支付、退款或订单修改。"
        ),
    ),
    InputRule(
        id="human_entry",
        category="assistant_help",
        method="exact",
        phrases=("转人工", "转人工客服", "我要人工客服", "我要转人工客服", "怎么联系人工客服"),
        trailing_punctuation="。！!？?",
        disposition="human_handoff",
        message=(
            "你可以通过人工请求入口提交诉求。本条回复尚未提交请求；"
            "提交成功也不代表人工已接单或业务已办理。"
        ),
    ),
    InputRule(
        id="private_data_request",
        category="security",
        method="fullmatch",
        pattern=(
            r"(?:(?:请|麻烦)(?:帮我)?|帮我)?(?:显示|获取|导出|查询|告诉我)"
            r"(?:其他客户|其他用户|所有客户|所有用户)的"
            r"(?:手机号|电话|收货地址|订单|订单信息|聊天记录)[。！!]?"
        ),
        disposition="security_refusal",
        message="我不能提供其他客户的私人信息，可以帮助你查询当前登录账号本人的订单。",
    ),
    InputRule(
        id="secret_request",
        category="security",
        method="fullmatch",
        pattern=(
            r"(?:(?:请|麻烦)(?:帮我)?|帮我)?(?:显示|获取|导出|查询|告诉我)"
            r"(?:后台数据库密码|管理员密码|服务端API密钥|系统访问令牌)[。！!]?"
        ),
        disposition="security_refusal",
        message="我不能提供系统密码、访问密钥等秘密信息。",
    ),
    # Credential heuristics only; bounds are not account password requirements.
    InputRule(
        id="password_value",
        category="credential_warning",
        method="fullmatch",
        pattern=(
            r"(?:我的)?(?:登录密码|支付密码|密码)[ \t]*(?:(?:是|为)[ \t]*)?"
            r"[:：][ \t]*[!-~]{6,128}"
        ),
        disposition="security_refusal",
        recorded_message="[已隐藏疑似密码]",
        message=(
            "这条消息疑似包含密码，请移除后重新描述问题。本轮未调用模型，服务端记录中的原文已隐藏。"
        ),
    ),
    InputRule(
        id="token_value",
        category="credential_warning",
        method="fullmatch",
        pattern=(
            r"(?:我的[ \t]*)?(?:API_KEY|API[ \t]*KEY|访问令牌|access[ \t]+token)"
            r"[ \t]*(?:(?:是|为)[ \t]*)?[:：=][ \t]*[A-Za-z0-9_./+~=-]{12,512}"
        ),
        disposition="security_refusal",
        recorded_message="[已隐藏疑似访问密钥或令牌]",
        message=(
            "这条消息疑似包含访问密钥或令牌，请移除后重新描述问题。"
            "本轮未调用模型，服务端记录中的原文已隐藏。"
        ),
    ),
    InputRule(
        id="verification_code_value",
        category="credential_warning",
        method="fullmatch",
        pattern=(
            r"(?:我的)?(?:验证码|短信验证码)[ \t]*(?:(?:是|为)[ \t]*)?"
            r"[:：][ \t]*[0-9]{4,8}"
        ),
        disposition="security_refusal",
        recorded_message="[已隐藏疑似验证码]",
        message=(
            "这条消息疑似包含验证码，请移除后重新描述问题。"
            "本轮未调用模型，服务端记录中的原文已隐藏。"
        ),
    ),
)


def normalized_text(message: str) -> str:
    """Trim boundaries only; preserve internal whitespace, punctuation and Unicode."""
    return message.strip()


def matches(rule: InputRule, message: str) -> bool:
    if rule.method == "over_limit":
        # Count Unicode code points, including whitespace, before normalization.
        return len(message) > MAX_MESSAGE_CHARACTERS
    text = normalized_text(message)
    if rule.method == "blank":
        return not text
    if rule.method == "exact":
        if text and rule.trailing_punctuation and text[-1] in rule.trailing_punctuation:
            text = text[:-1]
        return text.lower() in rule.phrases
    return re.fullmatch(rule.pattern, text, flags=re.IGNORECASE | re.ASCII) is not None


def match_input_rule(message: str) -> InputRule | None:
    return next((rule for rule in RULES if matches(rule, message)), None)


def rule_by_id(rule_id: str) -> InputRule:
    return next(rule for rule in RULES if rule.id == rule_id)
