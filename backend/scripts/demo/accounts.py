"""Public synthetic demo accounts; unrelated to isolation-test fixtures."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AppUser, UserAuthCredential
from app.services.auth import PasswordHasher

DEMO_PASSWORD = "demo-password"
DEMO_LOGINS = ("demo@example.com", *(f"demo{i}@example.com" for i in range(2, 7)))


async def create_accounts(session: AsyncSession) -> list[AppUser]:
    users = []
    for ordinal, login in enumerate(DEMO_LOGINS, start=1):
        user = AppUser(login_identifier=login, display_name=f"演示用户 {ordinal}", status="active")
        session.add(user)
        await session.flush()
        session.add(UserAuthCredential(
            user_id=user.id, login_identifier=login,
            password_hash=PasswordHasher.hash_password(DEMO_PASSWORD),
        ))
        users.append(user)
    return users
