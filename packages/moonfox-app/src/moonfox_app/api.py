"""Supported headless entry points; import does not discover or start extensions."""

from .bootstrap import HeadlessApp, create_application, create_headless_app
from .config import HostSettings, SessionSettings
from .extension_api import (
    ExtensionManifest,
    PermissionDecision,
    SessionBinding,
    SessionEvent,
    SessionView,
)
from .extensions import ExtensionRegistry, HostExtensionManager, SessionExtensionManager
from .runtime.compaction import (
    CompactionPolicy,
    CompactionRequest,
    CompactionResult,
    CompactionSettings,
    SummarizingCompactor,
    SummaryRequest,
    SummaryResult,
)
from .runtime.prompt import ContextBudgetExceeded, PromptPipelineOptions
from .runtime.session.entries import (
    CompactionDraft,
    CompactionEntry,
    ContextEditDraft,
    ContextEditEntry,
    CustomEntry,
    EntryDraft,
    HeaderEntry,
    MessageDraft,
    MessageEntry,
    ModelChangeDraft,
    ModelChangeEntry,
    SessionEntry,
    ThinkingLevelChangeEntry,
    ThinkingLevelDraft,
)
from .runtime.session.manager import SessionManager, SessionPreview
from .runtime.session.orchestration import AgentSession
from .runtime.session.projection import (
    IncompatibleContextError,
    ProjectionError,
    SessionProjection,
    build_projection,
)
from .runtime.storage import (
    InMemorySessionStore,
    JsonlSessionStore,
    SessionStore,
    StoreBusyError,
    StoreConflictError,
    StoreCorruptionError,
    StoreError,
    StoreSnapshot,
)
from .security import Principal, TokenAuth
from .services.hub import SessionHub
from .services.types import ModelRegistration

__all__ = [
    "HostSettings",
    "SessionSettings",
    "Principal",
    "TokenAuth",
    "SessionHub",
    "ModelRegistration",
    "create_application",
    "AgentSession",
    "CompactionDraft",
    "CompactionEntry",
    "CompactionPolicy",
    "CompactionRequest",
    "CompactionResult",
    "CompactionSettings",
    "ContextBudgetExceeded",
    "ContextEditDraft",
    "ContextEditEntry",
    "CustomEntry",
    "EntryDraft",
    "ExtensionManifest",
    "ExtensionRegistry",
    "HeaderEntry",
    "HeadlessApp",
    "HostExtensionManager",
    "InMemorySessionStore",
    "IncompatibleContextError",
    "JsonlSessionStore",
    "MessageDraft",
    "MessageEntry",
    "ModelChangeDraft",
    "ModelChangeEntry",
    "PermissionDecision",
    "ProjectionError",
    "PromptPipelineOptions",
    "SessionBinding",
    "SessionEntry",
    "SessionEvent",
    "SessionExtensionManager",
    "SessionManager",
    "SessionPreview",
    "SessionProjection",
    "SessionStore",
    "SessionView",
    "StoreBusyError",
    "StoreConflictError",
    "StoreCorruptionError",
    "StoreError",
    "StoreSnapshot",
    "SummarizingCompactor",
    "SummaryRequest",
    "SummaryResult",
    "ThinkingLevelChangeEntry",
    "ThinkingLevelDraft",
    "build_projection",
    "create_headless_app",
]
