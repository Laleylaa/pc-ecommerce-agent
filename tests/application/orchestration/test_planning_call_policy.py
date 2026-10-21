






import pytest
from app.core.config import Settings
from app.core.llm import build_chat_model


@pytest.mark.asyncio
async def test_real_clients_receive_timeout_and_disable_sdk_retries():
    model = build_chat_model(Settings(llm_api_key="test-placeholder"), timeout=30, max_retries=0)
    try:
        assert model.request_timeout == 30
        assert model.root_async_client.max_retries == 0
        assert model.root_async_client._client.timeout.read == 30
        assert model.root_client._client.timeout.connect == 30
    finally:
        await model.root_async_client.close()
        model.root_client.close()
