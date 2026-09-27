from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_current_user
from app.core.config import Settings, get_settings
from app.core.database import get_session
from app.models import AppUser
from app.schemas.auth import (
    AuthUser,
    LoginRequest,
    LogoutRequest,
    RefreshTokenRequest,
    TokenResponse,
)
from app.services.auth import (
    AuthForbiddenError,
    AuthService,
    AuthTokenError,
    InvalidCredentialsError,
)

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=TokenResponse)
async def login(
    request: LoginRequest,
    user_agent: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> TokenResponse:
    try:
        return await AuthService(session, settings).login(
            request.login_identifier,
            request.password,
            user_agent,
        )
    except InvalidCredentialsError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"message": "登录标识或密码不正确。"},
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    except AuthForbiddenError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"message": "当前账号不可用，请联系管理员。"},
        ) from exc


@router.post("/refresh", response_model=TokenResponse)
async def refresh_token(
    request: RefreshTokenRequest,
    user_agent: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> TokenResponse:
    try:
        return await AuthService(session, settings).refresh(request.refresh_token, user_agent)
    except AuthForbiddenError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"message": "当前账号不可用，请联系管理员。"},
        ) from exc
    except AuthTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"message": "登录已过期，请重新登录。"},
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: LogoutRequest,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Response:
    await AuthService(session, settings).logout(request.refresh_token)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/me", response_model=AuthUser)
async def me(current_user: AppUser = Depends(get_current_user)) -> AuthUser:
    return AuthUser(
        id=current_user.id,
        login_identifier=current_user.login_identifier,
        display_name=current_user.display_name,
        status=current_user.status,
        last_login_at=current_user.last_login_at,
    )
