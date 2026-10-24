import pytest
from pydantic import ValidationError

from backendfastapi.core.config import Settings, get_settings


@pytest.fixture
def settings_loader(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("APP_ENV_FILE", raising=False)
    for name in Settings.model_fields:
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.upper(), raising=False)
    get_settings.cache_clear()
    yield get_settings
    get_settings.cache_clear()


def test_settings_loader_keeps_default_dotenv(settings_loader, tmp_path):
    (tmp_path / ".env").write_text("LOG_LEVEL=DEBUG\n")
    assert settings_loader().log_level == "DEBUG"


@pytest.mark.parametrize("filename", [".env.development", ".env.production"])
def test_settings_loader_selects_only_explicit_file(
    settings_loader, monkeypatch, tmp_path, filename
):
    (tmp_path / ".env").write_text("CHAT_ENABLED=true\n")
    (tmp_path / filename).write_text("LOG_LEVEL=WARNING\n")
    monkeypatch.setenv("APP_ENV_FILE", filename)
    settings = settings_loader()
    assert settings.log_level == "WARNING"
    assert settings.chat_enabled is False
    assert settings.environment == "development"


def test_settings_loader_environment_overrides_file(
    settings_loader, monkeypatch, tmp_path
):
    path = tmp_path / "selected.env"
    path.write_text("LOG_LEVEL=DEBUG\n")
    monkeypatch.setenv("APP_ENV_FILE", str(path))
    monkeypatch.setenv("LOG_LEVEL", "ERROR")
    assert settings_loader().log_level == "ERROR"


def test_settings_loader_rejects_missing_file(settings_loader, monkeypatch):
    monkeypatch.setenv("APP_ENV_FILE", "missing.env")
    with pytest.raises(ValueError, match="APP_ENV_FILE"):
        settings_loader()


def test_settings_loader_disables_dotenv(settings_loader, monkeypatch, tmp_path):
    (tmp_path / ".env").write_text("CHAT_ENABLED=true\n")
    monkeypatch.setenv("APP_ENV_FILE", "")
    assert settings_loader().chat_enabled is False


def test_settings_loader_reads_valid_production_file(
    settings_loader, monkeypatch, tmp_path
):
    path = tmp_path / ".env.production"
    path.write_text(
        "ENVIRONMENT=production\n"
        "AUTH_ENABLED=true\n"
        f"JWT_SECRET={'x' * 64}\n"
        f"API_KEY={'a' * 32}\n"
        "DATABASE_URL=postgresql+asyncpg://app:test-password@db/app\n"
        'ALLOWED_HOSTS=["api.example.com"]\n'
    )
    monkeypatch.setenv("APP_ENV_FILE", str(path))
    settings = settings_loader()
    assert settings.environment == "production"
    assert settings.auth_enabled is True
    assert settings.allowed_hosts == ["api.example.com"]


def test_settings_loader_still_validates_production(
    settings_loader, monkeypatch, tmp_path
):
    (tmp_path / ".env.production").write_text("ENVIRONMENT=production\n")
    monkeypatch.setenv("APP_ENV_FILE", ".env.production")
    with pytest.raises(ValidationError, match="API_KEY is required"):
        settings_loader()


def test_settings_loader_caches_until_cleared(settings_loader, monkeypatch):
    monkeypatch.setenv("APP_ENV_FILE", "")
    first = settings_loader()
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    assert settings_loader() is first
    assert settings_loader().log_level == "INFO"
    settings_loader.cache_clear()
    assert settings_loader().log_level == "DEBUG"


def test_development_defaults():
    assert Settings(_env_file=None).environment == "development"


def test_compose_empty_secrets_are_optional_in_development():
    settings = Settings(_env_file=None, api_key="", openai_api_key="")
    assert settings.api_key is None
    assert settings.openai_api_key is None


def test_production_rejects_missing_api_key():
    with pytest.raises(ValidationError, match="API_KEY is required"):
        Settings(_env_file=None, environment="production", api_key=None)


def test_production_rejects_default_database_password():
    with pytest.raises(ValidationError, match="non-default database password"):
        Settings(
            _env_file=None,
            environment="production",
            api_key="a" * 32,
            database_url="postgresql+asyncpg://app:local_dev_password@db/app",
        )


def test_chat_requires_provider_credentials():
    with pytest.raises(ValidationError, match="OPENAI_API_KEY is required"):
        Settings(_env_file=None, chat_enabled=True, openai_api_key=None)


def test_ollama_chat_does_not_require_provider_api_key():
    settings = Settings(
        _env_file=None,
        chat_enabled=True,
        llm_provider="ollama",
        llm_model="qwen2.5:7b",
    )
    assert settings.chat_api_key.get_secret_value() == "ollama"
    assert settings.chat_base_url == "http://localhost:11434/v1"


def test_xai_chat_uses_compatible_api_defaults():
    settings = Settings(
        _env_file=None,
        chat_enabled=True,
        llm_provider="xai",
        llm_api_key="test-xai-key",
        llm_model="grok-test",
    )
    assert settings.chat_api_key.get_secret_value() == "test-xai-key"
    assert settings.chat_base_url == "https://api.x.ai/v1"
    assert settings.chat_model == "grok-test"


def test_provider_base_url_can_be_overridden():
    settings = Settings(
        _env_file=None,
        llm_provider="ollama",
        llm_model="qwen2.5:7b",
        llm_base_url="http://ollama:11434/v1/",
    )
    assert settings.chat_base_url == "http://ollama:11434/v1"


def test_non_openai_provider_requires_explicit_model():
    with pytest.raises(ValidationError, match="LLM_MODEL is required"):
        Settings(_env_file=None, chat_enabled=True, llm_provider="ollama")


def test_validation_errors_do_not_print_secrets():
    with pytest.raises(ValidationError) as error:
        Settings(_env_file=None, api_key="private-weak-key")
    assert "private-weak-key" not in str(error.value)


def test_production_accepts_explicit_configuration():
    settings = Settings(
        _env_file=None,
        environment="production",
        auth_enabled=True,
        jwt_secret="x" * 64,
        api_key="a" * 32,
        database_url="postgresql+asyncpg://app:strong-password@db/app",
        allowed_hosts=["api.example.com"],
    )
    assert settings.environment == "production"


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"api_key": "short"}, "at least 32"),
        ({"database_url": "sqlite+aiosqlite:///test.db"}, "postgresql\\+asyncpg"),
        ({"allowed_hosts": ["*"]}, "explicit ALLOWED_HOSTS"),
        ({"cors_origins": ["*"]}, "explicit CORS_ORIGINS"),
    ],
)
def test_production_rejects_unsafe_configuration(overrides, message):
    values = {
        "environment": "production",
        "api_key": "a" * 32,
        "database_url": "postgresql+asyncpg://app:strong-password@db/app",
        **overrides,
    }
    with pytest.raises(ValidationError, match=message):
        Settings(_env_file=None, **values)
