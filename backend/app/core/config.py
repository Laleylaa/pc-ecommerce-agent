import json
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import quote, urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_env: str = "local"
    podman_project_name: str = "pc-agent"
    podman_network: str = ""
    postgres_container: str = ""
    postgres_volume: str = ""
    postgres_image: str = "postgres:16-alpine"
    postgres_host: str = "localhost"
    postgres_port: int = Field(default=5432, ge=1, le=65535)
    postgres_db: str = "pc_agent"
    postgres_user: str = "pc_agent"
    postgres_password: SecretStr = SecretStr("pc_agent")
    database_url: str = ""
    redis_enabled: bool = False
    redis_container: str = ""
    redis_volume: str = ""
    redis_image: str = "redis:7-alpine"
    redis_port: int = Field(default=6379, ge=1, le=65535)
    redis_url: str = ""
    chroma_enabled: bool = False
    chroma_container: str = ""
    chroma_volume: str = ""
    chroma_image: str = (
        "docker.io/chromadb/chroma@sha256:"
        "bd21353aee6ccdf4a57bd91e6001626826700f3838e1f230d4aae75bfd4889a1"
    )
    chroma_host: str = "localhost"
    chroma_port: int = Field(default=8001, ge=1, le=65535)
    backend_host: str = "127.0.0.1"
    backend_port: int = Field(default=8000, ge=1, le=65535)
    frontend_port: int = Field(default=5173, ge=1, le=65535)
    knowledge_collection: str = "pc_agent_knowledge"
    knowledge_score_threshold: float = 0.16
    deepseek_search_api_key: SecretStr = SecretStr("")

    llm_provider: str = "deepseek"
    llm_api_key: str = ""
    llm_model: str = "deepseek-chat"
    llm_base_url: str = ""
    catalog_llm_planner_enabled: bool = True
    agent_runtime: Literal["legacy", "strands"] = "strands"
    agent_context_budget_tokens: int = 6000
    loop_trace_enabled: bool = False
    agent_trace_mode: Literal["off", "local", "langsmith"] = "off"
    agent_trace_dir: Path = Path(__file__).resolve().parents[3] / "artifacts/traces"
    langsmith_api_key: SecretStr = SecretStr("")
    langsmith_project: str = "pc-ecommerce-agent"
    langsmith_endpoint: str = "https://api.smith.langchain.com"

    auth_token_secret: str = "local-dev-auth-token-secret-change-me"
    auth_access_token_minutes: int = 30
    auth_refresh_token_days: int = 14

    backend_cors_origins: list[str] = Field(default_factory=list)

    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[3] / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        enable_decoding=False,
    )

    @model_validator(mode="after")
    def local_defaults(self) -> "Settings":
        if not self.database_url.strip():
            host = self.postgres_host
            if ":" in host and not host.startswith("["):
                host = f"[{host}]"
            self.database_url = (
                f"postgresql+asyncpg://{quote(self.postgres_user, safe='')}:"
                f"{quote(self.postgres_password.get_secret_value(), safe='')}@"
                f"{host}:{self.postgres_port}/{quote(self.postgres_db, safe='')}"
            )
        if not self.redis_url.strip():
            self.redis_url = f"redis://localhost:{self.redis_port}/0"
        if not self.backend_cors_origins:
            self.backend_cors_origins = [
                f"http://localhost:{self.frontend_port}", f"http://127.0.0.1:{self.frontend_port}",
            ]
        if not self.podman_network:
            self.podman_network = f"{self.podman_project_name}-net"
        for service in ("postgres", "redis", "chroma"):
            for suffix, default in (("container", ""), ("volume", "-data")):
                field = f"{service}_{suffix}"
                if not getattr(self, field):
                    setattr(self, field, f"{self.podman_project_name}-{service}{default}")
        return self

    @property
    def resolved_search_api_key(self) -> str:
        """Reuse only official DeepSeek credentials, never a different provider/proxy key."""
        explicit = self.deepseek_search_api_key.get_secret_value().strip()
        if explicit:
            return explicit
        base_url = self.llm_base_url.strip()
        if self.llm_provider.strip().lower() != "deepseek":
            return ""
        if base_url:
            parsed = urlsplit(base_url)
            if parsed.scheme != "https" or parsed.netloc != "api.deepseek.com":
                return ""
        return self.llm_api_key.strip()

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
