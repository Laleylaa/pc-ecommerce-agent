import base64
import binascii
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import Settings
from app.models import AppUser, UserAuthCredential, UserSession
from app.schemas.auth import AuthUser, TokenResponse

PASSWORD_ALGORITHM = "pbkdf2_sha256"
PASSWORD_ITERATIONS = 210_000
SESSION_STATUS_ACTIVE = "active"
SESSION_STATUS_REVOKED = "revoked"
USER_STATUS_ACTIVE = "active"


class AuthError(Exception):
    """Base class for authentication failures."""


class InvalidCredentialsError(AuthError):
    pass


class AuthTokenError(AuthError):
    pass


class AuthForbiddenError(AuthError):
    pass


@dataclass(frozen=True)
class AccessTokenPayload:
    user_id: int
    session_id: int
    expires_at: datetime


def utc_now() -> datetime:
    return datetime.now(UTC)


def normalize_login_identifier(value: str) -> str:
    return value.strip().lower()


class PasswordHasher:
    @staticmethod
    def hash_password(password: str) -> str:
        salt = secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt,
            PASSWORD_ITERATIONS,
        )
        return "$".join(
            [
                PASSWORD_ALGORITHM,
                str(PASSWORD_ITERATIONS),
                _b64encode(salt),
                _b64encode(digest),
            ]
        )

    @staticmethod
    def verify_password(password: str, stored_hash: str) -> bool:
        try:
            algorithm, iterations_raw, salt_raw, digest_raw = stored_hash.split("$", 3)
            iterations = int(iterations_raw)
            salt = _b64decode(salt_raw)
            expected = _b64decode(digest_raw)
        except (binascii.Error, ValueError, TypeError):
            return False

        if algorithm != PASSWORD_ALGORITHM:
            return False

        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
        return hmac.compare_digest(actual, expected)


class AuthService:
    def __init__(self, session: AsyncSession, settings: Settings):
        self.session = session
        self.settings = settings

    async def login(
        self,
        login_identifier: str,
        password: str,
        user_agent: str | None = None,
    ) -> TokenResponse:
        credential = await self._get_credential(login_identifier)
        if credential is None or not PasswordHasher.verify_password(
            password, credential.password_hash
        ):
            raise InvalidCredentialsError("Invalid login identifier or password")

        user = credential.user
        self._ensure_active_user(user)
        now = utc_now()
        refresh_token = secrets.token_urlsafe(48)
        session = UserSession(
            user_id=user.id,
            refresh_token_hash=hash_refresh_token(refresh_token),
            status=SESSION_STATUS_ACTIVE,
            user_agent=_trim_user_agent(user_agent),
            expires_at=now + timedelta(days=self.settings.auth_refresh_token_days),
            last_used_at=now,
        )
        self.session.add(session)
        user.last_login_at = now
        user.updated_at = now
        await self.session.flush()
        await self.session.commit()
        await self.session.refresh(user)
        access_token = self._create_access_token(user.id, session.id, now)
        return self._token_response(user, access_token, refresh_token)

    async def refresh(self, refresh_token: str, user_agent: str | None = None) -> TokenResponse:
        session = await self._get_session_by_refresh_token(refresh_token)
        if session is None:
            raise AuthTokenError("Invalid refresh token")

        now = utc_now()
        self._ensure_active_session(session, now)
        self._ensure_active_user(session.user)

        next_refresh_token = secrets.token_urlsafe(48)
        session.refresh_token_hash = hash_refresh_token(next_refresh_token)
        session.last_used_at = now
        session.user_agent = _trim_user_agent(user_agent) or session.user_agent
        await self.session.commit()
        access_token = self._create_access_token(session.user_id, session.id, now)
        return self._token_response(session.user, access_token, next_refresh_token)

    async def logout(self, refresh_token: str) -> None:
        session = await self._get_session_by_refresh_token(refresh_token)
        if session is None:
            return
        now = utc_now()
        session.status = SESSION_STATUS_REVOKED
        session.revoked_at = now
        session.last_used_at = now
        await self.session.commit()

    async def get_current_user(self, access_token: str) -> AppUser:
        payload = self.verify_access_token(access_token)
        stmt = (
            select(UserSession)
            .where(
                UserSession.id == payload.session_id,
                UserSession.user_id == payload.user_id,
            )
            .options(selectinload(UserSession.user))
        )
        session = (await self.session.execute(stmt)).scalar_one_or_none()
        if session is None:
            raise AuthTokenError("Session not found")
        self._ensure_active_session(session, utc_now())
        self._ensure_active_user(session.user)
        return session.user

    def verify_access_token(self, access_token: str) -> AccessTokenPayload:
        payload = self._decode_access_token(access_token)
        if payload.get("typ") != "access":
            raise AuthTokenError("Invalid token type")
        try:
            user_id = int(payload["uid"])
            session_id = int(payload["sid"])
            expires_at = datetime.fromtimestamp(int(payload["exp"]), UTC)
        except (KeyError, TypeError, ValueError) as exc:
            raise AuthTokenError("Invalid token payload") from exc
        if expires_at <= utc_now():
            raise AuthTokenError("Access token expired")
        return AccessTokenPayload(
            user_id=user_id,
            session_id=session_id,
            expires_at=expires_at,
        )

    async def _get_credential(self, login_identifier: str) -> UserAuthCredential | None:
        stmt = (
            select(UserAuthCredential)
            .where(
                UserAuthCredential.login_identifier
                == normalize_login_identifier(login_identifier)
            )
            .options(selectinload(UserAuthCredential.user))
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def _get_session_by_refresh_token(self, refresh_token: str) -> UserSession | None:
        stmt = (
            select(UserSession)
            .where(UserSession.refresh_token_hash == hash_refresh_token(refresh_token))
            .options(selectinload(UserSession.user))
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    def _create_access_token(self, user_id: int, session_id: int, now: datetime) -> str:
        expires_at = now + timedelta(minutes=self.settings.auth_access_token_minutes)
        payload = {
            "typ": "access",
            "uid": user_id,
            "sid": session_id,
            "iat": int(now.timestamp()),
            "exp": int(expires_at.timestamp()),
        }
        body = _b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        signature = self._sign(body)
        return f"{body}.{signature}"

    def _decode_access_token(self, access_token: str) -> dict[str, Any]:
        try:
            body, signature = access_token.split(".", 1)
        except ValueError as exc:
            raise AuthTokenError("Malformed access token") from exc
        if not hmac.compare_digest(self._sign(body), signature):
            raise AuthTokenError("Invalid access token signature")
        try:
            decoded = _b64decode(body).decode("utf-8")
            payload = json.loads(decoded)
        except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
            raise AuthTokenError("Invalid access token payload") from exc
        if not isinstance(payload, dict):
            raise AuthTokenError("Invalid access token payload")
        return payload

    def _sign(self, body: str) -> str:
        digest = hmac.new(
            self.settings.auth_token_secret.encode("utf-8"),
            body.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        return _b64encode(digest)

    def _token_response(
        self,
        user: AppUser,
        access_token: str,
        refresh_token: str,
    ) -> TokenResponse:
        return TokenResponse(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=self.settings.auth_access_token_minutes * 60,
            user=_to_auth_user(user),
        )

    def _ensure_active_user(self, user: AppUser) -> None:
        if user.status != USER_STATUS_ACTIVE:
            raise AuthForbiddenError("User is not active")

    def _ensure_active_session(self, session: UserSession, now: datetime) -> None:
        if session.status != SESSION_STATUS_ACTIVE or session.revoked_at is not None:
            raise AuthTokenError("Session is not active")
        if _as_aware(session.expires_at) <= now:
            raise AuthTokenError("Session expired")


def hash_refresh_token(refresh_token: str) -> str:
    return hashlib.sha256(refresh_token.encode("utf-8")).hexdigest()


def _to_auth_user(user: AppUser) -> AuthUser:
    return AuthUser(
        id=user.id,
        login_identifier=user.login_identifier,
        display_name=user.display_name,
        status=user.status,
        last_login_at=user.last_login_at,
    )


def _trim_user_agent(user_agent: str | None) -> str | None:
    if not user_agent:
        return None
    stripped = user_agent.strip()
    return stripped[:255] if stripped else None


def _as_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii"))
