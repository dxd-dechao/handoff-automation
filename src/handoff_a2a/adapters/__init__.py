"""Server-side provider adapters (Claude, Codex, Cursor). The A2A client never branches on provider."""

from __future__ import annotations

from handoff_a2a.adapters.base import AdapterOutcome, ExecutorAdapter, RunPreparation
from handoff_a2a.adapters.claude import ClaudeAdapter, ClaudeAdapterConfig
from handoff_a2a.adapters.codex import CodexAdapter, CodexAdapterConfig
from handoff_a2a.adapters.cursor import CursorAdapter, CursorAdapterConfig

AdapterConfig = ClaudeAdapterConfig | CodexAdapterConfig | CursorAdapterConfig
PROVIDER_NAMES = ("claude", "codex", "cursor")


def build_adapter(config: AdapterConfig) -> ExecutorAdapter:
    if isinstance(config, CodexAdapterConfig):
        return CodexAdapter(config)
    if isinstance(config, CursorAdapterConfig):
        return CursorAdapter(config)
    return ClaudeAdapter(config)


__all__ = [
    "AdapterConfig",
    "AdapterOutcome",
    "ClaudeAdapter",
    "ClaudeAdapterConfig",
    "CodexAdapter",
    "CodexAdapterConfig",
    "CursorAdapter",
    "CursorAdapterConfig",
    "ExecutorAdapter",
    "PROVIDER_NAMES",
    "RunPreparation",
    "build_adapter",
]
