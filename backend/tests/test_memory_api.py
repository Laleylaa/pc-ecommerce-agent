from datetime import datetime
from types import SimpleNamespace
from typing import cast

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_current_user
from app.core.database import get_session
from app.main import app
from app.repositories.conversations import ConversationRepository


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path"),
    [("GET", "/api/memories"), ("DELETE", "/api/memories/1")],
)
async def test_memory_endpoints_require_authentication(method: str, path: str) -> None:
    app.dependency_overrides.clear()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.request(method, path)

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_list_memories_returns_only_current_users_active_explicit_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        _memory(11, 7, "brand_preference", "偏好罗技品牌"),
        _memory(12, 8, "usage_preference", "偏好办公场景"),
        _memory(13, 7, "disabled_preference", "不应返回", disabled=True),
    ]
    calls: list[tuple[int, int | None]] = []

    async def fake_list_memory(
        _repository: ConversationRepository, user_id: int, limit: int | None = 10
    ) -> list[SimpleNamespace]:
        calls.append((user_id, limit))
        return [
            item
            for item in rows
            if item.user_id == user_id
            and item.origin == "explicit_user"
            and item.disabled_at is None
        ]

    monkeypatch.setattr(ConversationRepository, "list_memory", fake_list_memory)
    session = _FakeSession()

    async with _authenticated_client(7, session) as client:
        response = await client.get("/api/memories")

    assert response.status_code == 200
    assert calls == [(7, None)]
    assert response.json() == [
        {
            "id": 11,
            "key": "brand_preference",
            "fact_type": "preference",
            "display_value": "偏好罗技品牌",
            "structured_value": {"brand": "罗技", "negated": False},
            "origin": "explicit_user",
            "created_at": "2026-07-01T08:30:00",
            "updated_at": "2026-07-02T09:45:00",
            "last_used_at": None,
        }
    ]


@pytest.mark.asyncio
async def test_delete_memory_soft_disables_only_current_users_active_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = {
        11: _memory(11, 7, "brand_preference", "偏好罗技品牌"),
        12: _memory(12, 8, "usage_preference", "偏好办公场景"),
    }
    calls: list[tuple[int, int]] = []

    async def fake_disable_memory(
        _repository: ConversationRepository, user_id: int, memory_id: int
    ) -> bool:
        calls.append((user_id, memory_id))
        item = rows.get(memory_id)
        if item is None or item.user_id != user_id or item.disabled_at is not None:
            return False
        item.disabled_at = datetime(2026, 7, 3, 10, 0)
        return True

    monkeypatch.setattr(ConversationRepository, "disable_memory", fake_disable_memory)
    session = _FakeSession()

    async with _authenticated_client(7, session) as client:
        deleted = await client.delete("/api/memories/11")
        repeated = await client.delete("/api/memories/11")
        other_user = await client.delete("/api/memories/12")
        absent = await client.delete("/api/memories/999")

    assert deleted.status_code == 204
    assert deleted.content == b""
    assert rows[11].disabled_at == datetime(2026, 7, 3, 10, 0)
    assert repeated.status_code == 404
    assert other_user.status_code == 404
    assert absent.status_code == 404
    assert calls == [(7, 11), (7, 11), (7, 12), (7, 999)]
    assert session.commit_count == 1


class _AuthenticatedClient:
    def __init__(self, user_id: int, session: "_FakeSession") -> None:
        self.user_id = user_id
        self.session = session
        self.client: AsyncClient | None = None

    async def __aenter__(self) -> AsyncClient:
        async def override_get_session():
            yield cast(AsyncSession, self.session)

        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=self.user_id)
        app.dependency_overrides[get_session] = override_get_session
        self.client = AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        )
        return await self.client.__aenter__()

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        assert self.client is not None
        await self.client.__aexit__(exc_type, exc, traceback)
        app.dependency_overrides.clear()


def _authenticated_client(user_id: int, session: "_FakeSession") -> _AuthenticatedClient:
    return _AuthenticatedClient(user_id, session)


class _FakeSession:
    def __init__(self) -> None:
        self.commit_count = 0

    async def commit(self) -> None:
        self.commit_count += 1


def _memory(
    memory_id: int,
    user_id: int,
    key: str,
    value: str,
    *,
    disabled: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=memory_id,
        user_id=user_id,
        key=key,
        fact_type="preference",
        value=value,
        value_json={"brand": "罗技", "negated": False},
        origin="explicit_user",
        created_at=datetime(2026, 7, 1, 8, 30),
        updated_at=datetime(2026, 7, 2, 9, 45),
        last_used_at=None,
        disabled_at=datetime(2026, 7, 2, 12, 0) if disabled else None,
    )
