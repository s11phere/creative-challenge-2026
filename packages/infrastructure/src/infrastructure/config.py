"""Application configuration via Pydantic Settings."""

from __future__ import annotations

from typing import Literal

from domain.embedding import EmbeddingIdentity
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
    qa_task_timeout_ms: int = Field(default=120_000, ge=10_000)
    qa_task_max_retries: int = Field(default=12, ge=0, le=100)
    qa_task_retry_delay_ms: int = Field(default=3_000, ge=100, le=60_000)
    qa_task_lease_seconds: int = Field(default=30, ge=10, le=600)
    qa_task_heartbeat_interval_s: int = Field(default=10, ge=1, le=300)
    diagnostic_task_timeout_ms: int = Field(default=10_000, ge=1_000)
    diagnostic_task_max_retries: int = Field(default=3, ge=0)
    diagnostic_task_min_backoff_ms: int = Field(default=1_000, ge=100)

    # --- Ingestion ---
    max_upload_size_mb: int = Field(default=50, ge=1, le=500)
    blob_store_path: str = Field(
        default="./data/blobs", description="Root directory for raw file storage"
    )
    ingestion_task_timeout_ms: int = Field(default=600_000, ge=10_000)
    ingestion_task_max_retries: int = Field(default=3, ge=0)
    ingestion_task_min_backoff_ms: int = Field(default=5_000, ge=1_000)
    ingestion_task_heartbeat_interval_s: int = Field(default=30, ge=5)
    ingestion_task_lease_seconds: int = Field(default=120, ge=30)
    embedding_batch_size: int = Field(default=32, ge=1)

    # --- Model Gateway ---
    model_provider: Literal[
        "fake", "openai-compatible", "text-embeddings-inference", "disabled"
    ] = "fake"
    model_endpoint: str | None = None
    model_api_key: SecretStr | None = None
    fast_chat_endpoint: str | None = None
    fast_chat_api_key: SecretStr | None = None
    fast_chat_model: str | None = None
    embedding_endpoint: str | None = None
    embedding_api_key: SecretStr | None = None
    embedding_model: str | None = None
    embedding_model_revision: str | None = None
    reranker_endpoint: str | None = None
    reranker_api_key: SecretStr | None = None
    reranker_model: str | None = None
    embedding_protocol: Literal["openai-compatible", "tei"] = "openai-compatible"
    embedding_query_instruction_version: str = "none-v1"
    embedding_document_instruction_version: str = "none-v1"
    embedding_normalization: str = "none"
    embedding_precision: str = "float32"
    model_allow_external: bool = False
    model_timeout_seconds: float = Field(default=15.0, gt=0, le=120)
    model_max_retries: int = Field(default=2, ge=0, le=5)
    model_retry_backoff_seconds: float = Field(default=0.1, ge=0, le=10)

    # --- Retrieval ---
    retrieval_timeout_seconds: float = Field(default=15.0, gt=0, le=120)
    retrieval_debug_diagnostics: bool = False

    # --- Skill Registry ---
    skill_root_path: str = "./skills"
    knowledge_qa_skill_version: str = "0.1.0"
    knowledge_agent_skill_version: str = "0.1.0"

    def active_embedding_identity(self, *, allow_unconfigured: bool = False) -> EmbeddingIdentity:
        """Return the one identity shared by ingestion and online retrieval."""
        self.query_embedding_prefix()
        model_revision = self.embedding_model_revision
        if not model_revision:
            model_revision = (
                "fake-sha256-v1" if self.model_provider == "fake" else self.embedding_model
            )
        if not model_revision:
            if allow_unconfigured:
                model_revision = "embedding-unconfigured-v1"
            else:
                raise ValueError("EMBEDDING_MODEL_REVISION is required for non-fake embedding")
        return EmbeddingIdentity(
            model_revision=model_revision,
            query_instruction_version=self.embedding_query_instruction_version,
            document_instruction_version=self.embedding_document_instruction_version,
            normalization=self.embedding_normalization,
            precision=self.embedding_precision,
        )

    def query_embedding_prefix(self) -> str:
        """Resolve a reviewed query instruction from its versioned identity."""
        prefixes = {
            "none-v1": "",
            "qwen3-web-search-v1": (
                "Instruct: Given a web search query, retrieve relevant passages that answer "
                "the query\nQuery: "
            ),
        }
        try:
            return prefixes[self.embedding_query_instruction_version]
        except KeyError as exc:
            raise ValueError("Unsupported EMBEDDING_QUERY_INSTRUCTION_VERSION") from exc

    @property
    def redis_url(self) -> str:
        return f"redis://{self.redis_host}:{self.redis_port}"

    # --- Logging ---
    log_level: str = "INFO"
    log_format: str = "json"

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
