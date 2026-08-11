"""Versioned contracts and application services for the product-level Assistant Agent."""

from .autonomous_loop import (
    AssistantConversationLoopFinalizer,
    AssistantSkillContext,
    AutonomousAssistantLoopService,
)
from .commands import (
    AssistantCommandCatalog,
    AssistantCommandKind,
    AssistantCommandParser,
    AssistantCommandService,
    CommandCatalogError,
    CommandDescriptor,
    CommandExecutionResult,
    CommandParseError,
    ParsedAssistantCommand,
    SkillCommandInvoker,
)
from .context import (
    ConversationCompactionService,
    ConversationContextDataPort,
    ConversationContextMessage,
    ConversationContextService,
    ConversationContextSnapshot,
)
from .finalization import ConversationFinalizer, FinalizationInput, grounded_material
from .metrics import (
    AssistantAction,
    AssistantMetrics,
    AssistantRoutingObservation,
    aggregate_assistant_metrics,
)
from .reasoning import ReasoningProfileResolver
from .resources import (
    NaturalLanguageResourceResolver,
    ResolvedResource,
    ResourceResolutionError,
    ResourceResolutionErrorCode,
    ResourceResolutionPort,
)
from .runs import (
    AssistantTurnApplicationPort,
    AssistantTurnSubmission,
    ConversationReader,
    ConversationRunApplicationError,
    ConversationRunService,
)
from .skill_invocation import AssistantSkillInvocationService, SkillProjectionPort
from .workspace import (
    ConversationWorkspace,
    ConversationWorkspaceError,
    ConversationWorkspaceService,
)

__all__ = [
    "AssistantTurnApplicationPort",
    "AssistantConversationLoopFinalizer",
    "AssistantCommandCatalog",
    "AssistantCommandKind",
    "AssistantCommandParser",
    "AssistantCommandService",
    "AssistantSkillContext",
    "AutonomousAssistantLoopService",
    "ConversationCompactionService",
    "ConversationContextDataPort",
    "ConversationContextMessage",
    "ConversationContextService",
    "ConversationContextSnapshot",
    "AssistantAction",
    "AssistantMetrics",
    "AssistantRoutingObservation",
    "aggregate_assistant_metrics",
    "CommandCatalogError",
    "CommandDescriptor",
    "CommandExecutionResult",
    "CommandParseError",
    "ParsedAssistantCommand",
    "SkillCommandInvoker",
    "AssistantTurnSubmission",
    "ConversationReader",
    "ConversationRunApplicationError",
    "ConversationRunService",
    "ConversationFinalizer",
    "FinalizationInput",
    "grounded_material",
    "NaturalLanguageResourceResolver",
    "ResourceResolutionError",
    "ResourceResolutionErrorCode",
    "ResourceResolutionPort",
    "ResolvedResource",
    "ReasoningProfileResolver",
    "AssistantSkillInvocationService",
    "SkillProjectionPort",
    "ConversationWorkspace",
    "ConversationWorkspaceError",
    "ConversationWorkspaceService",
]
