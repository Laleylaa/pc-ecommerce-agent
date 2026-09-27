from collections.abc import AsyncIterator, Callable

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.agent.graph import AgentRuntime
from app.api.routers import chat as chat_router
from app.core.config import Settings, get_settings
from app.core.database import get_session
from app.main import app
from app.schemas.chat import EvidenceItem
from app.services.dataset_mapper import normalize_part_record
from app.tools.contracts import RegistryToolExecutor, ToolContract
from app.tools.schemas import ToolExecutionResult
from scripts.seed_demo import (
    DEMO_LOGIN_IDENTIFIER,
    DEMO_PASSWORD,
    _ensure_user_auth_credential,
    _get_or_create_user,
    _seed_knowledge,
    _seed_order,
    _upsert_product,
)

TEST_PRODUCT_RECORDS = [
    (
        "mouse",
        {
            "name": "Logitech Codex G502 Hero",
            "price": 44.77,
            "tracking_method": "Optical",
            "connection_type": "Wired",
            "max_dpi": 25600,
            "hand_orientation": "Right",
            "color": "Black",
        },
    ),
    (
        "mouse",
        {
            "name": "Razer Codex Viper V3 Pro",
            "price": 139.99,
            "tracking_method": "Optical",
            "connection_type": "Wired, Wireless",
            "max_dpi": 35000,
            "hand_orientation": "Right",
            "color": "White",
        },
    ),
    (
        "headphones",
        {
            "name": "SteelSeries Codex Arctis Nova Wireless",
            "price": 49.88,
            "type": "Circumaural",
            "frequency_response": [20, 22],
            "microphone": True,
            "wireless": True,
            "enclosure_type": "Closed",
            "color": "Black",
        },
    ),
    (
        "headphones",
        {
            "name": "Beyerdynamic Codex DT 770 PRO",
            "price": 169.99,
            "type": "Circumaural",
            "frequency_response": [5, 35],
            "microphone": False,
            "wireless": False,
            "enclosure_type": "Closed",
            "color": "Gray",
        },
    ),
    (
        "keyboard",
        {
            "name": "RK Royal Kludge Codex RK61",
            "price": 49.99,
            "style": "Mini",
            "switches": "RK Red",
            "backlit": "RGB",
            "tenkeyless": True,
            "connection_type": "Wired, Wireless, Bluetooth Wireless",
            "color": "White",
        },
    ),
]


class FakeKnowledgeService:
    async def retrieve(self, query: str) -> list[EvidenceItem]:
        if "退货" not in query:
            return []
        return [
            EvidenceItem(
                source_type="knowledge_document",
                source_id=9001,
                title="测试退货政策",
                document_type="policy",
                snippet="签收次日起七天内，未影响二次销售可申请七天无理由退货。",
                score=0.91,
                metadata={"scenario": "return"},
            )
        ]


class RuntimeWithFakeKnowledge(AgentRuntime):
    def __init__(self, session: AsyncSession, settings: Settings):
        super().__init__(
            session,
            settings,
            tool_executor=TestToolExecutor(session, settings),
        )


class TestToolExecutor:
    def __init__(self, session: AsyncSession, settings: Settings):
        self.registry_executor = RegistryToolExecutor(session, settings)
        self.knowledge = FakeKnowledgeService()

    async def execute(
        self,
        contract: ToolContract,
        arguments: dict,
        runtime_context: dict,
    ) -> ToolExecutionResult:
        if contract.name == "policy_search":
            evidence = await self.knowledge.retrieve(str(arguments["query"]))
            return ToolExecutionResult(
                tool_name=contract.name,
                ok=True,
                output={
                    "result_type": "documents" if evidence else "empty",
                    "documents": [item.model_dump(mode="json") for item in evidence],
                    "search_strategy": "test",
                },
            )
        return await self.registry_executor.execute(contract, arguments, runtime_context)


@pytest.fixture
def demo_credentials() -> tuple[str, str]:
    return DEMO_LOGIN_IDENTIFIER, DEMO_PASSWORD


@pytest_asyncio.fixture
async def api_client(
    db_session_factory: Callable[[], AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[AsyncClient]:
    async def override_get_session() -> AsyncIterator[AsyncSession]:
        async with db_session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    app.dependency_overrides[get_settings] = lambda: Settings(llm_api_key="")
    monkeypatch.setattr(chat_router, "AgentRuntime", RuntimeWithFakeKnowledge)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client

    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def auth_headers(
    api_client: AsyncClient,
    demo_credentials: tuple[str, str],
) -> dict[str, str]:
    login_identifier, password = demo_credentials
    response = await api_client.post(
        "/api/auth/login",
        json={"login_identifier": login_identifier, "password": password},
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest_asyncio.fixture
async def db_session_factory() -> AsyncIterator[Callable[[], AsyncSession]]:
    test_engine = create_async_engine(
        get_settings().database_url,
        pool_pre_ping=True,
        poolclass=NullPool,
    )
    try:
        connection = await test_engine.connect()
        transaction = await connection.begin()
    except (OSError, DBAPIError, OperationalError) as exc:
        await test_engine.dispose()
        pytest.skip(f"PostgreSQL integration database is unavailable: {exc}")

    session_factory = async_sessionmaker(
        bind=connection,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    try:
        async with session_factory() as session:
            user = await _get_or_create_user(session)
            await _ensure_user_auth_credential(session, user)
            first_sku = None
            for part_type, record in TEST_PRODUCT_RECORDS:
                product = normalize_part_record(part_type, record)
                assert product is not None
                sku = await _upsert_product(session, product)
                first_sku = first_sku or sku
            if first_sku:
                await _seed_order(session, user.id, first_sku)
            await _seed_knowledge(session)
            await session.commit()

        yield session_factory
    finally:
        await transaction.rollback()
        await connection.close()
        await test_engine.dispose()
