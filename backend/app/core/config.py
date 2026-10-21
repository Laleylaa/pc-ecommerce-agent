import json
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_env: str = "local"
    database_url: str = "postgresql+asyncpg://pc_agent:pc_agent@localhost:5432/pc_agent"
    redis_url: str = "redis://localhost:6379/0"
    chroma_host: str = "localhost"
    chroma_port: int = 8001
    knowledge_collection: str = "pc_agent_knowledge"
    knowledge_score_threshold: float = 0.16
    tavily_api_key: SecretStr = SecretStr("")

    llm_provider: str = "deepseek"
    llm_api_key: str = ""
    llm_model: str = "deepseek-chat"
    llm_base_url: str = ""
    catalog_llm_planner_enabled: bool = True
    agent_runtime: Literal["legacy", "strands"] = "legacy"
    agent_context_budget_tokens: int = 6000
    agent_trace_mode: Literal["off", "local", "langsmith"] = "off"
    agent_trace_dir: Path = Path(__file__).resolve().parents[3] / "artifacts/traces"
    langsmith_api_key: SecretStr = SecretStr("")
    langsmith_project: str = "pc-ecommerce-agent"
    langsmith_endpoint: str = "https://api.smith.langchain.com"

    auth_token_secret: str = "local-dev-auth-token-secret-change-me"
    auth_access_token_minutes: int = 30
    auth_refresh_token_days: int = 14

    backend_cors_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:5173", "http://127.0.0.1:5173"]
    )

    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[3] / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        enable_decoding=False,
    )

    @field_validator("backend_cors_origins", mode="before")
    @classmethod
    def parse_origins(cls, value: str | list[str]) -> list[str]:
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return []
            if stripped.startswith("["):
                decoded = json.loads(stripped)
                if isinstance(decoded, list):
                    return [str(item).strip() for item in decoded if str(item).strip()]
            return [item.strip() for item in stripped.split(",") if item.strip()]
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
