"""Server-side provider adapters (Claude, Codex). The A2A client never branches on provider."""

from __future__ import annotations

from handoff_a2a.adapters.base import AdapterOutcome, ExecutorAdapter
from handoff_a2a.adapters.claude import ClaudeAdapter, ClaudeAdapterConfig
from handoff_a2a.adapters.codex import CodexAdapter, CodexAdapterConfig

AdapterConfig = ClaudeAdapterConfig | CodexAdapterConfig


def build_adapter(config: AdapterConfig) -> ExecutorAdapter:
    if isinstance(config, CodexAdapterConfig):
        return CodexAdapter(config)
    return ClaudeAdapter(config)


__all__ = [
    "AdapterConfig",
    "AdapterOutcome",
    "ClaudeAdapter",
    "ClaudeAdapterConfig",
    "CodexAdapter",
    "CodexAdapterConfig",
    "ExecutorAdapter",
    "build_adapter",
]
