from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.database import get_session
from app.models import AppUser
from app.services.auth import AuthForbiddenError, AuthService, AuthTokenError

bearer_scheme = HTTPBearer(auto_error=False)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> AppUser:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise _unauthorized("Missing bearer token")

    service = AuthService(session, settings)
    try:
        return await service.get_current_user(credentials.credentials)
    except AuthForbiddenError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"message": "当前账号不可用，请联系管理员。"},
        ) from exc
    except AuthTokenError as exc:
        raise _unauthorized(str(exc)) from exc


def _unauthorized(message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"message": message},
        headers={"WWW-Authenticate": "Bearer"},
    )
