from types import SimpleNamespace

from app.api.dependencies import get_current_user
from app.api.routers import chat
from app.core.config import Settings, get_settings
from app.core.database import get_session
from fastapi import FastAPI


def isolated_app():
    app = FastAPI()
    app.include_router(chat.router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=1)
    app.dependency_overrides[get_session] = lambda: None
    app.dependency_overrides[get_settings] = lambda: Settings(llm_api_key="")
    return app
