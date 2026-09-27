from datetime import datetime

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    login_identifier: str = Field(min_length=2, max_length=128)
    password: str = Field(min_length=8, max_length=128)


class RefreshTokenRequest(BaseModel):
    refresh_token: str = Field(min_length=20)


class LogoutRequest(BaseModel):
    refresh_token: str = Field(min_length=20)


class AuthUser(BaseModel):
    id: int
    login_identifier: str
    display_name: str
    status: str
    last_login_at: datetime | None = None


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    user: AuthUser
