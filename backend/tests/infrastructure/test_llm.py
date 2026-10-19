from typing import Any

import pytest

from app.core.config import Settings
from app.core.llm import build_chat_model


@pytest.mark.parametrize(
    ("provider", "expected_extra_body"),
    [
        ("deepseek", {"thinking": {"type": "disabled"}}),
        ("qwen", None),
    ],
)
def test_build_chat_model_only_disables_thinking_for_deepseek(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    expected_extra_body: dict[str, Any] | None,
) -> None:
    captured: dict[str, Any] = {}

    def fake_chat_openai(**kwargs: Any) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr("app.core.llm.ChatOpenAI", fake_chat_openai)

    model = build_chat_model(
        Settings(
            llm_provider=provider,
            llm_api_key="test-key",
            llm_model="test-model",
        )
    )

    assert model is not None
    assert captured.get("extra_body") == expected_extra_body
    assert captured["temperature"] == 0.2


def test_build_chat_model_accepts_phase_temperature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_chat_openai(**kwargs: Any) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr("app.core.llm.ChatOpenAI", fake_chat_openai)

    model = build_chat_model(
        Settings(llm_api_key="test-key"),
        temperature=0.0,
    )

    assert model is not None
    assert captured["temperature"] == 0.0
