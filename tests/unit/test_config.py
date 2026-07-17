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
