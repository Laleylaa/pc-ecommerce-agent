"""P3 explicit integration entry: writes only to a new disposable PostgreSQL database."""

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import httpx
from app.models import (
    AgentRun,
    AppUser,
    Brand,
    Category,
    Conversation,
    HandoffRequest,
    Message,
    OrderInfo,
    Sku,
    Spu,
    UserAuthCredential,
)
from app.repositories.conversations import ConversationRepository
from app.services.auth import PasswordHasher
from sqlalchemy import func, select, text

from tests.integration.postgres_app import create_app


async def seed(app):
    async with app.state.sessions() as session:
        if await session.scalar(select(func.count(AppUser.id))):
            raise ValueError("Refusing to seed a non-empty database")
        for identity in (41, 42):
            session.add(
                AppUser(
                    id=identity,
                    login_identifier=f"p3-{identity}@example.test",
                    display_name=f"P3合成用户{identity}",
                    status="active",
                )
            )
        session.add_all([Category(id=1, name="键盘"), Brand(id=1, name="P3测试品牌")])
        await session.flush()
        for identity in (41, 42):
            session.add(
                UserAuthCredential(
                    user_id=identity,
                    login_identifier=f"p3-{identity}@example.test",
                    password_hash=PasswordHasher.hash_password("p3-test-password"),
                )
            )
        session.add(Spu(id=91, category_id=1, brand_id=1, title="P3测试键盘", status=1))
        await session.flush()
        session.add(
            Sku(
                id=9101,
                spu_id=91,
                title="P3测试键盘白色版",
                price=299,
                stock=4,
                specs_json={"color": "白色", "connection_type": "USB"},
                status=1,
            )
        )
        session.add(
            OrderInfo(
                id=8201,
                user_id=41,
                total_amount=299,
                pay_amount=299,
                status=3,
                receiver_name="合成收件人",
                receiver_phone="TEST_ONLY",
                receiver_address="合成地址，不是真实用户数据",
            )
        )
        await session.commit()


async def verify(app):
    checks = []
    async with app.state.sessions() as session:
        completed_before = await session.scalar(
            select(func.count(AgentRun.id)).where(AgentRun.status == "completed")
        )
        handoffs_before = await session.scalar(select(func.count(HandoffRequest.id)))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://isolated"
    ) as client:

        async def login(identity):
            result = await client.post(
                "/api/auth/login",
                json={
                    "login_identifier": f"p3-{identity}@example.test",
                    "password": "p3-test-password",
                },
            )
            assert result.status_code == 200, result.text
            return {"Authorization": "Bearer " + result.json()["access_token"]}

        owner, other = await login(41), await login(42)
        assert (await client.post("/api/chat", json={"message": "你好"})).status_code == 401
        checks.append("real_authentication")
        result = await client.post(
            "/api/chat", headers=owner, json={"message": "查 SKU 9101 的价格"}
        )
        assert result.status_code == 200, result.text
        body = result.json()
        assert body["products"][0]["sku_id"] == 9101
        conversation = body["conversation_id"]
        assert len(body["execution_details"]["tool_calls"]) == 1
        checks.append("real_catalog_query_and_chat_persistence")
        history = await client.get(f"/api/conversations/{conversation}", headers=owner)
        assert len(history.json()["messages"]) == 2
        assert history.json()["messages"][1]["metadata"]["products"][0]["sku_id"] == 9101
        assert (
            await client.get(f"/api/conversations/{conversation}", headers=other)
        ).status_code == 404
        checks.append("history_and_conversation_ownership")
        for headers, expected in ((owner, True), (other, False)):
            lookup = await client.post(
                "/api/chat", headers=headers, json={"message": "查订单 8201"}
            )
            assert lookup.status_code == 200, lookup.text
            assert bool(lookup.json()["order"]) == expected
        checks.append("real_order_user_filter")
        remembered = await client.post(
            "/api/chat",
            headers=owner,
            json={
                "message": "请记住，以后我偏好无线设备",
                "conversation_id": conversation,
            },
        )
        assert remembered.status_code == 200
        memories = (await client.get("/api/memories", headers=owner)).json()
        assert len(memories) == 1
        memory_id = memories[0]["id"]
        assert (await client.delete(f"/api/memories/{memory_id}", headers=other)).status_code == 404
        assert (await client.delete(f"/api/memories/{memory_id}", headers=owner)).status_code == 204
        assert (await client.get("/api/memories", headers=owner)).json() == []
        checks.append("stable_memory_and_authorized_forgetting")
        async with app.state.sessions() as session:
            assert await session.scalar(select(func.count(HandoffRequest.id))) == handoffs_before
            assert (
                await session.scalar(
                    select(func.count(AgentRun.id)).where(AgentRun.status == "completed")
                )
                == completed_before + 4
            )
        request = {
            "session_id": conversation,
            "order_id": 8201,
            "request_type": "return",
            "reason": "合成验收诉求，已确认提交",
        }
        assert (
            await client.post("/api/after-sales", headers=other, json=request)
        ).status_code == 404
        accepted = await client.post("/api/after-sales", headers=owner, json=request)
        assert accepted.status_code == 202
        request_id = accepted.json()["request_id"]
        assert request_id > 0 and accepted.json()["status"] == "pending"
        assert "不会自动办理" in accepted.json()["message"]
        assert (
            await client.get(f"/api/after-sales/handoff-requests/{request_id}", headers=owner)
        ).json()["id"] == request_id
        assert (
            await client.get(f"/api/after-sales/handoff-requests/{request_id}", headers=other)
        ).status_code == 404
        checks.append("confirmed_handoff_record_id_and_owner_only_read")
        async with app.state.sessions() as session:
            assistant_count = await session.scalar(
                select(func.count(Message.id)).where(Message.role == "assistant")
            )
        with patch.object(
            ConversationRepository,
            "update_working_memory",
            side_effect=RuntimeError("synthetic persistence failure"),
        ):
            failed = await client.post(
                "/api/chat",
                headers=owner,
                json={
                    "message": "请记住，以后我不喜欢无线设备",
                    "conversation_id": conversation,
                },
            )
        assert failed.status_code == 500
        assert (await client.get("/api/memories", headers=owner)).json() == []
        async with app.state.sessions() as session:
            assert (
                await session.scalar(
                    select(func.count(Message.id)).where(Message.role == "assistant")
                )
                == assistant_count
            )
            last_run = (
                await session.execute(select(AgentRun).order_by(AgentRun.id.desc()).limit(1))
            ).scalar_one()
            assert last_run.status == "failed"
            assert last_run.state_json["error"]["type"] == "RuntimeError"
        checks.append("real_transaction_rollback_on_persistence_failure")
        # Existing old history without execution_details remains readable.
        async with app.state.sessions() as session:
            legacy = Conversation(user_id=41, title="P3旧历史兼容", working_memory_json={})
            session.add(legacy)
            await session.flush()
            session.add_all(
                [
                    Message(conversation_id=legacy.id, role="user", content="你好"),
                    Message(
                        conversation_id=legacy.id,
                        role="assistant",
                        content="你好！",
                        metadata_json={},
                    ),
                ]
            )
            await session.commit()
            legacy_id = legacy.id
        assert (
            await client.get(f"/api/conversations/{legacy_id}", headers=owner)
        ).status_code == 200
        checks.append("legacy_history_read")
    async with app.state.sessions() as session:
        version = await session.scalar(text("select version_num from alembic_version"))
        assert version == "0008_sku_sales_count"
    return {
        "checks": checks,
        "passed": len(checks),
        "migration_head": version,
        "mode": "real PostgreSQL + ASGI HTTP + authentication + runtime; scripted model",
        "database": "disposable localhost:15439/agent_loop_p3",
        "conversation_id": conversation,
    }


async def main(args):
    app = create_app()
    try:
        if args.seed:
            await seed(app)
        result = await verify(app)
        directory = Path(__file__).resolve().parents[2] / "artifacts/evals/agent_loop"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / (datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-postgres.json")
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(result, ensure_ascii=False))
        print("Artifacts:", path)
    finally:
        await app.state.engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--isolated", required=True, action="store_true")
    parser.add_argument("--seed", action="store_true", help="Only on a fresh empty test database")
    asyncio.run(main(parser.parse_args()))
