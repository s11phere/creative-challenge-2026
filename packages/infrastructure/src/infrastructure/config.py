"""Application configuration via Pydantic Settings."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    def database_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    # --- Redis ---
    redis_host: str = "localhost"
    redis_port: int = 6379

    @property
    def redis_url(self) -> str:
        return f"redis://{self.redis_host}:{self.redis_port}"

    # --- Logging ---
    log_level: str = "INFO"
    log_format: str = "json"

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
