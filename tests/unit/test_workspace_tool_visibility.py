from __future__ import annotations

from infrastructure.config import settings
from model_gateway import FakeModelGateway, OpenAICompatibleGateway
from worker.assistant_tasks import _workspace_model_visibility_allowed


def test_fake_provider_can_use_workspace_tools_without_consent(monkeypatch) -> None:
    monkeypatch.setattr(settings, "agent_workspace_model_visibility_consent", False)

    assert _workspace_model_visibility_allowed(FakeModelGateway()) is True


def test_non_fake_provider_requires_workspace_visibility_consent(monkeypatch) -> None:
    gateway = OpenAICompatibleGateway()
    monkeypatch.setattr(settings, "agent_workspace_model_visibility_consent", False)

    assert _workspace_model_visibility_allowed(gateway) is False


def test_non_fake_provider_can_use_workspace_tools_after_consent(monkeypatch) -> None:
    gateway = OpenAICompatibleGateway()
    monkeypatch.setattr(settings, "agent_workspace_model_visibility_consent", True)

    assert _workspace_model_visibility_allowed(gateway) is True
