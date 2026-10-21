"""Validate configuration without opening databases or calling an LLM."""

from pathlib import Path

from app.core.config import Settings
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[2]
PLACEHOLDER_SECRETS = {
    "",
    "replace-with-a-local-random-secret",
    "local-dev-auth-token-secret-change-me",
}


def main() -> None:
    if not (ROOT / ".env").is_file():
        raise SystemExit("Missing root .env. Copy .env.example and configure it first.")
    try:
        settings = Settings()
    except ValidationError as exc:
        fields = sorted({".".join(map(str, error["loc"])) for error in exc.errors()})
        raise SystemExit("Invalid configuration fields: " + ", ".join(fields)) from None
    if settings.auth_token_secret in PLACEHOLDER_SECRETS:
        raise SystemExit("Set a private random AUTH_TOKEN_SECRET in .env first.")
    if not settings.llm_api_key:
        print("NOTE: LLM_API_KEY is empty; only limited fallback behavior is available.")
    print("Configuration structure: OK (credentials and connectivity not tested).")


if __name__ == "__main__":
    main()
