"""Minimal interface every server-side Executor adapter provides.

The server owns spawning, process groups, cancellation/deadlines, workspace
locks, durable task identity, and evidence capture. An adapter only supplies
the provider's native command, an isolated child environment, and the
conversion of the provider's output files into a neutral outcome.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol

from handoff_a2a.contracts import Usage

RITUAL_PROMPT = "execute the handoff"


@dataclass(frozen=True)
class AdapterOutcome:
    exit_code: int
    stdout: str
    stderr: str
    duration_s: float
    parsed: Mapping[str, Any] | None
    invalid_output: bool
    provider_error: bool
    summary: str
    usage: Usage | None
    cost_usd: float | None
    usage_provenance: str | None
    cost_provenance: str | None
    argv: tuple[str, ...]
    provider_error_detail: str | None = None


class ExecutorAdapter(Protocol):
    provider: str
    display_name: str
    model: str

    def argv(self, workspace: Path, handoff_markdown: str | None = None) -> list[str]:
        """Native command. `handoff_markdown` is the validated submitted snapshot."""
        ...

    def child_environment(self, base: Mapping[str, str] | None = None) -> dict[str, str]: ...

    def interpret(
        self,
        stdout_path: Path,
        stderr_path: Path,
        *,
        exit_code: int,
        duration_s: float,
        argv: tuple[str, ...],
    ) -> AdapterOutcome: ...


def strip_environment(
    prefixes: tuple[str, ...],
    names: tuple[str, ...] = (),
    base: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Copy the environment without inherited provider credentials/selectors."""
    env = dict(os.environ if base is None else base)
    for key in list(env):
        if key.startswith(prefixes) or key in names:
            del env[key]
    return env


def read_outputs(stdout_path: Path, stderr_path: Path) -> tuple[str, str]:
    stdout = stdout_path.read_text(encoding="utf-8", errors="replace") if stdout_path.is_file() else ""
    stderr = stderr_path.read_text(encoding="utf-8", errors="replace") if stderr_path.is_file() else ""
    return stdout, stderr


def as_count(value: Any) -> int | None:
    """Integral token count, or None for anything else (bools, floats with fractions, strings)."""
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if float(value) != int(value):
        return None
    return int(value)
