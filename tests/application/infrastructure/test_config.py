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


def test_local_connection_encodes_credentials_and_tracks_ports():
    from sqlalchemy.engine import make_url

    settings = Settings(
        _env_file=None, database_url="", postgres_user="demo@user",
        postgres_password="literal$(false):a/b%#", postgres_port=15432,
        frontend_port=15173, backend_cors_origins=[], podman_project_name="demo-project",
    )
    url = make_url(settings.database_url)
    assert url.username == "demo@user"
    assert url.password == "literal$(false):a/b%#"
    assert url.port == 15432
    assert settings.postgres_volume == "demo-project-postgres-data"
    assert settings.backend_cors_origins == [
        "http://localhost:15173", "http://127.0.0.1:15173",
    ]


def test_explicit_database_override_and_podman_values_stay_separate():
    from scripts.local_config import podman_values

    url = "postgresql+asyncpg://demo:example@db.invalid:15432/external"
    settings = Settings(_env_file=None, database_url=url, postgres_port=5432,
                        postgres_password="synthetic-password")
    assert settings.database_url == url
    values = podman_values(settings)
    assert values["POSTGRES_PORT"] == "5432"
    assert values["POSTGRES_PASSWORD"] == "synthetic-password"
    assert "LLM_API_KEY" not in values
    assert "AUTH_TOKEN_SECRET" not in values


def test_podman_and_application_share_dotenv_rules(monkeypatch, tmp_path):
    from scripts.local_config import podman_values

    env_file = tmp_path / ".env"
    env_file.write_text('POSTGRES_PASSWORD="literal$(false) # value"\nPOSTGRES_PORT=15432\n')
    monkeypatch.setenv("POSTGRES_PORT", "25432")
    settings = Settings(_env_file=env_file, database_url="")
    assert settings.postgres_password.get_secret_value() == "literal$(false) # value"
    assert settings.postgres_port == 25432
    assert podman_values(settings)["POSTGRES_PORT"] == "25432"
