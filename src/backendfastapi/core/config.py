import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", hide_input_in_errors=True
    )

    database_url: str = (
        "postgresql+asyncpg://app:local_dev_password@localhost:5432/backendfastapi"
    )
    environment: Literal["development", "test", "production"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    api_key: SecretStr | None = None
    auth_enabled: bool = False
    registration_enabled: bool = False
    jwt_secret: SecretStr | None = None
    jwt_issuer: str = "backendfastapi"
    jwt_audience: str = "backendfastapi-api"
    access_token_minutes: int = Field(default=15, ge=1, le=60)
    refresh_token_days: int = Field(default=7, ge=1, le=30)
    requests_per_minute: int = Field(default=120, ge=1, le=10000)
    login_attempts_per_minute: int = Field(default=10, ge=1, le=100)
    monthly_budget_microusd: int = Field(default=10000000, ge=0)
    model_input_price_per_million_microusd: int = Field(default=400000, ge=1)
    model_output_price_per_million_microusd: int = Field(default=1600000, ge=1)
    agent_max_input_tokens: int = Field(default=8192, ge=256, le=32000)
    worker_lease_seconds: int = Field(default=90, ge=30, le=300)
    worker_poll_seconds: float = Field(default=1, ge=0.1, le=30)
    worker_max_attempts: int = Field(default=3, ge=1, le=5)
    worker_concurrency: int = Field(default=4, ge=1, le=16)
    provider_max_concurrency: int = Field(default=8, ge=1, le=64)
    otel_enabled: bool = False
    otel_endpoint: str = "http://otel-collector:4318/v1/traces"
    allowed_hosts: list[str] = ["localhost", "127.0.0.1", "testserver"]
    cors_origins: list[str] = []
    max_request_body_bytes: int = Field(default=32768, ge=1024, le=10485760)
    database_pool_size: int = Field(default=5, ge=1, le=100)
    database_max_overflow: int = Field(default=10, ge=0, le=100)
    database_timeout_seconds: int = Field(default=10, ge=1, le=60)
    chat_enabled: bool = False
    openai_api_key: SecretStr | None = None
    openai_model: str = "gpt-4.1-mini"
    llm_provider: Literal["openai", "xai", "ollama"] = "openai"
    llm_api_key: SecretStr | None = None
    llm_base_url: str | None = None
    llm_model: str | None = None
    openai_embedding_model: str = "text-embedding-3-small"
    chat_timeout_seconds: float = Field(default=30, gt=0, le=120)
    chat_max_history_messages: int = Field(default=20, ge=1, le=100)
    chat_max_output_tokens: int = Field(default=1024, ge=1, le=4096)

    @model_validator(mode="before")
    @classmethod
    def normalize_optional_secrets(cls, values):
        # Compose passes empty strings when optional secrets are not configured.
        for key in (
            "api_key",
            "openai_api_key",
            "llm_api_key",
            "jwt_secret",
            "llm_base_url",
            "llm_model",
        ):
            if values.get(key) == "":
                values[key] = None
        return values

    @property
    def chat_model(self) -> str:
        return self.llm_model or self.openai_model

    @property
    def chat_api_key(self) -> SecretStr:
        if self.llm_provider == "ollama":
            return self.llm_api_key or SecretStr("ollama")
        key = self.llm_api_key or self.openai_api_key
        if key is None:
            raise ValueError("LLM_API_KEY is required for the selected provider")
        return key

    @property
    def chat_base_url(self) -> str | None:
        if self.llm_base_url:
            return self.llm_base_url.rstrip("/")
        if self.llm_provider == "xai":
            return "https://api.x.ai/v1"
        if self.llm_provider == "ollama":
            return "http://localhost:11434/v1"
        return None

    @model_validator(mode="after")
    def validate_deployment(self) -> "Settings":
        database = make_url(self.database_url)
        if database.drivername != "postgresql+asyncpg":
            raise ValueError("DATABASE_URL must use postgresql+asyncpg")
        if self.chat_enabled and self.llm_provider != "ollama":
            if not self.llm_api_key and not self.openai_api_key:
                credential = (
                    "LLM_API_KEY is required when LLM_PROVIDER=xai"
                    if self.llm_provider == "xai"
                    else (
                        "OPENAI_API_KEY is required when CHAT_ENABLED=true "
                        "(or set LLM_API_KEY)"
                    )
                )
                raise ValueError(credential)
        if self.chat_enabled and self.llm_provider != "openai" and not self.llm_model:
            raise ValueError(
                f"LLM_MODEL is required when LLM_PROVIDER={self.llm_provider}"
            )
        if self.auth_enabled and (
            not self.jwt_secret or len(self.jwt_secret.get_secret_value()) < 48
        ):
            raise ValueError(
                "JWT_SECRET of at least 48 characters is required for auth"
            )
        if self.environment == "production" and self.registration_enabled:
            raise ValueError("Public registration must be disabled in production")
        if self.api_key and len(self.api_key.get_secret_value()) < 32:
            raise ValueError("API_KEY must contain at least 32 characters")
        if self.environment == "production":
            if not self.api_key:
                raise ValueError("API_KEY is required in production")
            if not database.password or database.password == "local_dev_password":
                raise ValueError("Production requires a non-default database password")
            if not self.allowed_hosts or "*" in self.allowed_hosts:
                raise ValueError("Production requires explicit ALLOWED_HOSTS")
            if "*" in self.cors_origins:
                raise ValueError("Production requires explicit CORS_ORIGINS")
            if not self.auth_enabled:
                raise ValueError("Production requires AUTH_ENABLED=true")
        return self


@lru_cache
def get_settings() -> Settings:
    env_file = os.getenv("APP_ENV_FILE")
    if env_file is None:
        return Settings()
    if env_file == "":
        return Settings(_env_file=None)
    path = Path(env_file)
    if not path.is_file():
        raise ValueError("APP_ENV_FILE must point to an existing settings file")
    return Settings(_env_file=path)
