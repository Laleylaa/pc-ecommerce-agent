from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models import AppUser, UserSession
from app.services.auth import AuthService, AuthTokenError, PasswordHasher


def test_password_hasher_verifies_only_matching_password() -> None:
    password_hash = PasswordHasher.hash_password("demo-password")

    assert PasswordHasher.verify_password("demo-password", password_hash)
    assert not PasswordHasher.verify_password("wrong-password", password_hash)


def test_access_token_signature_and_expiration_are_verified() -> None:
    settings = Settings(auth_token_secret="unit-test-secret", auth_access_token_minutes=1)
    service = AuthService(cast(AsyncSession, None), settings)
    token = service._create_access_token(1, 10, datetime.now(UTC))

    payload = service.verify_access_token(token)

    assert payload.user_id == 1
    assert payload.session_id == 10

    tampered = f"{token}x"
    with pytest.raises(AuthTokenError):
        service.verify_access_token(tampered)

    expired = service._create_access_token(1, 10, datetime.now(UTC) - timedelta(minutes=2))
    with pytest.raises(AuthTokenError):
        service.verify_access_token(expired)


def test_auth_datetime_columns_bind_timezone_aware_values() -> None:
    assert AppUser.__table__.c.last_login_at.type.timezone is True
    assert AppUser.__table__.c.updated_at.type.timezone is True
    assert UserSession.__table__.c.expires_at.type.timezone is True
    assert UserSession.__table__.c.revoked_at.type.timezone is True
    assert UserSession.__table__.c.last_used_at.type.timezone is True
