"""Application configuration via Pydantic Settings."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL


class Settings(BaseSettings):
    """Application settings loaded from environment variables.

    Production startup rejects empty required secrets via ``validate_secrets``.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Application ---
    app_env: str = "development"
    app_debug: bool = True
    # Change this in production via environment variable or .env
    app_secret_key: str = Field(default="")

    # --- Server ---
    api_host: str = "127.0.0.1"
    api_port: int = 8000

    # --- PostgreSQL ---
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str = "agent_knowledge"
    postgres_user: str = "app"
    postgres_password: str = Field(default="")

    @property
    def database_url(self) -> URL:
        return URL.create(
            drivername="postgresql+asyncpg",
            username=self.postgres_user,
            password=self.postgres_password,
            host=self.postgres_host,
            port=self.postgres_port,
            database=self.postgres_db,
        )

    # --- Redis ---
    redis_host: str = "localhost"
    redis_port: int = 6379

    # --- Worker ---
    worker_processes: int = Field(default=1, ge=1)
    worker_threads: int = Field(default=4, ge=1)
    worker_shutdown_timeout_ms: int = Field(default=30_000, ge=1_000)
    diagnostic_task_timeout_ms: int = Field(default=10_000, ge=1_000)
    diagnostic_task_max_retries: int = Field(default=3, ge=0)
    diagnostic_task_min_backoff_ms: int = Field(default=1_000, ge=100)

    # --- Model Gateway ---
    model_provider: Literal["fake", "openai-compatible", "disabled"] = "fake"
    model_endpoint: str | None = None
    model_api_key: SecretStr | None = None
    fast_chat_model: str | None = None
    embedding_model: str | None = None
    model_allow_external: bool = False
    model_timeout_seconds: float = Field(default=15.0, gt=0, le=120)
    model_max_retries: int = Field(default=2, ge=0, le=5)
    model_retry_backoff_seconds: float = Field(default=0.1, ge=0, le=10)

    @property
    def redis_url(self) -> str:
        return f"redis://{self.redis_host}:{self.redis_port}"

    # --- Logging ---
    log_level: str = "INFO"
    log_format: str = "json"

    # --- Embedding ---
    embedding_dimensions: int = Field(default=768, ge=64, le=4096)

    # --- OpenTelemetry ---
    otlp_endpoint: str | None = None
    otel_export_timeout_seconds: float = Field(default=2.0, gt=0, le=30)

    def validate_secrets(self) -> None:
        """Raise ValueError if required secrets are not set (production only)."""
        if self.app_env != "production":
            return
        missing: list[str] = []
        if not self.app_secret_key:
            missing.append("APP_SECRET_KEY")
        if not self.postgres_password:
            missing.append("POSTGRES_PASSWORD")
        if missing:
            raise ValueError(
                f"Required configuration values are missing: {', '.join(missing)}. "
                "Set them via environment variables or .env file."
            )


settings = Settings()
