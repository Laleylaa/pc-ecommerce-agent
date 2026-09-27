from app.core.config import Settings


def test_default_env_file_is_independent_of_working_directory(monkeypatch, tmp_path) -> None:
    from pathlib import Path

    from app.core import config

    env_path = Path(config.__file__).resolve().parents[3] / ".env"
    (tmp_path / ".env").write_text("AUTH_TOKEN_SECRET=wrong-directory\n")
    child = tmp_path / "child"
    child.mkdir()
    monkeypatch.chdir(child)
    monkeypatch.delenv("AUTH_TOKEN_SECRET", raising=False)

    assert Settings.model_config["env_file"] == env_path
    assert Settings().auth_token_secret == Settings(_env_file=env_path).auth_token_secret


def test_backend_cors_origins_accepts_comma_separated_env(monkeypatch) -> None:
    monkeypatch.setenv("BACKEND_CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173")

    settings = Settings()

    assert settings.backend_cors_origins == [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]


def test_backend_cors_origins_accepts_json_env(monkeypatch) -> None:
    monkeypatch.setenv("BACKEND_CORS_ORIGINS", '["http://localhost:5173","http://127.0.0.1:5173"]')

    settings = Settings()

    assert settings.backend_cors_origins == [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]
