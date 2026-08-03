"""Tests for configuration validation."""

import pytest
from api.main import create_app
from infrastructure.config import Settings, settings
from pytest import MonkeyPatch


def test_config_secrets_validation_fails_in_production() -> None:
    """In production, missing secrets must raise ValueError."""
    s = Settings(app_env="production", app_secret_key="", postgres_password="")

    with pytest.raises(ValueError, match="APP_SECRET_KEY"):
        s.validate_secrets()


def test_config_secrets_validation_passes_in_production_with_values() -> None:
    s = Settings(app_env="production", app_secret_key="real-key", postgres_password="real-pw")
    s.validate_secrets()  # should not raise


def test_config_secrets_skipped_in_development() -> None:
    """In development, empty secrets should not block startup."""
    s = Settings(app_env="development", app_secret_key="", postgres_password="")
    s.validate_secrets()  # should not raise


def test_model_api_key_uses_secret_type() -> None:
    value = "synthetic-secret-api-key"
    s = Settings(model_api_key=value)

    assert s.model_api_key is not None
    assert s.model_api_key.get_secret_value() == value
    assert value not in repr(s)


def test_capability_specific_api_keys_use_secret_type() -> None:
    s = Settings(
        fast_chat_api_key="synthetic-chat-secret",
        embedding_api_key="synthetic-embedding-secret",
    )

    assert s.fast_chat_api_key is not None
    assert s.embedding_api_key is not None
    assert s.fast_chat_api_key.get_secret_value() == "synthetic-chat-secret"
    assert s.embedding_api_key.get_secret_value() == "synthetic-embedding-secret"
    assert "synthetic-chat-secret" not in repr(s)
    assert "synthetic-embedding-secret" not in repr(s)


def test_embedding_dimensions_are_not_runtime_configurable() -> None:
    assert "embedding_dimensions" not in Settings.model_fields


def test_embedding_batch_size_is_bounded_and_configurable() -> None:
    assert Settings(_env_file=None).embedding_batch_size == 32
    assert Settings(embedding_batch_size=4).embedding_batch_size == 4
    with pytest.raises(ValueError, match="greater than or equal to 1"):
        Settings(embedding_batch_size=0)


def test_reranker_batch_size_is_bounded_and_configurable() -> None:
    assert Settings(_env_file=None).reranker_batch_size == 32
    assert Settings(reranker_batch_size=4).reranker_batch_size == 4
    with pytest.raises(ValueError, match="greater than or equal to 1"):
        Settings(reranker_batch_size=0)


def test_fake_embedding_provider_is_independent_from_real_chat_provider() -> None:
    configured = Settings(
        model_provider="openai-compatible",
        fast_chat_endpoint="https://models.example.test/v1",
        fast_chat_model="chat-model",
        embedding_provider="fake",
    )

    assert configured.active_embedding_identity().model_revision == "fake-sha256-v1"


def test_local_tei_embedding_provider_is_independent_from_real_chat_provider() -> None:
    configured = Settings(
        model_provider="openai-compatible",
        fast_chat_endpoint="https://models.example.test/v1",
        fast_chat_model="chat-model",
        embedding_provider="text-embeddings-inference",
        embedding_endpoint="http://tei:80",
        embedding_model="Qwen/Qwen3-Embedding-0.6B",
        embedding_model_revision="fixed-revision",
    )

    assert configured.active_embedding_identity().model_revision == "fixed-revision"


def test_qwen3_query_instruction_resolves_to_reviewed_prefix() -> None:
    s = Settings(embedding_query_instruction_version="qwen3-web-search-v1")

    assert s.query_embedding_prefix() == (
        "Instruct: Given a web search query, retrieve relevant passages that answer the query\n"
        "Query: "
    )


def test_qwen3_knowledge_qa_query_instruction_is_accepted() -> None:
    s = Settings(embedding_query_instruction_version="qwen3-knowledge-qa-v1")

    assert s.query_embedding_prefix() == (
        "Instruct: Given a question, retrieve the most relevant passage from the knowledge base "
        "that answers it\nQuery: "
    )


def test_unknown_query_instruction_version_is_rejected() -> None:
    s = Settings(embedding_query_instruction_version="unknown-v1")

    with pytest.raises(ValueError, match="EMBEDDING_QUERY_INSTRUCTION_VERSION"):
        s.active_embedding_identity()


async def test_application_lifespan_rejects_missing_production_secrets(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "app_secret_key", "")
    monkeypatch.setattr(settings, "postgres_password", "")
    app = create_app()

    with pytest.raises(ValueError, match="APP_SECRET_KEY"):
        async with app.router.lifespan_context(app):
            pass
