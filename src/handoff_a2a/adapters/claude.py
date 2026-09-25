"""Claude Code adapter: native argv launch, JSON parse, auth stripping."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from handoff_a2a.adapters.base import (
    RITUAL_PROMPT,
    AdapterOutcome,
    as_count,
    read_outputs,
    strip_environment,
)
from handoff_a2a.contracts import Usage

__all__ = [
    "RITUAL_PROMPT",
    "AdapterOutcome",
    "ClaudeAdapter",
    "ClaudeAdapterConfig",
    "child_environment",
    "interpret_claude_files",
    "parse_claude_json",
]

# CURSOR_* covers a Cursor Planner session that started or restarted the server.
_STRIP_PREFIXES = ("ANTHROPIC_", "CLAUDE", "CURSOR_")

# The Executor must never run the Planner skill or the handoff CLI (recursive
# dispatch). Denied per launch rather than in .claude/settings.local.json,
# which a Claude Code Planner in the same repository also reads. Deny beats
# allow. bin/handoff's legacy executor passes the same list.
EXECUTOR_DISALLOWED_TOOLS = (
    "Bash(handoff:*)",
    "Bash(handoff-a2a:*)",
    "Bash(*/handoff:*)",
    "Bash(*/handoff-a2a:*)",
    "Skill(handoff-cli)",
)


@dataclass(frozen=True)
class ClaudeAdapterConfig:
    binary: str
    model: str


def child_environment(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """Drop inherited provider auth so the child uses its own stored login."""
    return strip_environment(_STRIP_PREFIXES, base=base)


def is_claude_result(data: Any) -> bool:
    """A Claude --output-format json result must identify itself and report outcome."""
    if not isinstance(data, dict):
        return False
    if data.get("type") != "result":
        return False
    if "is_error" not in data or not isinstance(data["is_error"], bool):
        return False
    return True


def parse_claude_json(raw: str) -> tuple[Mapping[str, Any] | None, bool]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None, True
    if not is_claude_result(data):
        return None, True
    return data, False


def _usage_from_claude(data: Mapping[str, Any]) -> tuple[Usage | None, str | None]:
    usage = data.get("usage")
    if not isinstance(usage, dict):
        return None, None

    def as_int(name: str) -> int | None:
        return as_count(usage.get(name))

    parsed = Usage(
        input_tokens=as_int("input_tokens"),
        output_tokens=as_int("output_tokens"),
        cache_creation_input_tokens=as_int("cache_creation_input_tokens"),
        cache_read_input_tokens=as_int("cache_read_input_tokens"),
    )
    return parsed, "claude.usage"


def _cost_from_claude(data: Mapping[str, Any]) -> tuple[float | None, str | None]:
    if "total_cost_usd" not in data:
        return None, None
    value = data.get("total_cost_usd")
    if value is None:
        return None, "claude.total_cost_usd"
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None, "claude.total_cost_usd"
    return float(value), "claude.total_cost_usd"


class ClaudeAdapter:
    provider = "claude"
    display_name = "Claude"

    def __init__(self, config: ClaudeAdapterConfig):
        self.config = config
        self.model = config.model

    def child_environment(self, base: Mapping[str, str] | None = None) -> dict[str, str]:
        return child_environment(base)

    def interpret(
        self,
        stdout_path: Path,
        stderr_path: Path,
        *,
        exit_code: int,
        duration_s: float,
        argv: tuple[str, ...],
    ) -> AdapterOutcome:
        return interpret_claude_files(
            stdout_path, stderr_path, exit_code=exit_code, duration_s=duration_s, argv=argv
        )

    def argv(self, workspace: Path | None = None, handoff_markdown: str | None = None) -> list[str]:
        # Claude runs in the server-chosen cwd and reads HANDOFF.md itself.
        return [
            self.config.binary,
            "-p",
            RITUAL_PROMPT,
            "--permission-mode",
            "acceptEdits",
            "--model",
            self.config.model,
            "--disallowedTools",
            ",".join(EXECUTOR_DISALLOWED_TOOLS),
            "--output-format",
            "json",
        ]

    async def run(
        self,
        workspace: Path,
        stdout_path: Path,
        stderr_path: Path,
        env: Mapping[str, str] | None = None,
    ) -> AdapterOutcome:
        argv = self.argv()
        started = time.monotonic()
        with stdout_path.open("wb") as out, stderr_path.open("wb") as err:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(workspace),
                env=child_environment(env),
                stdout=out,
                stderr=err,
            )
            exit_code = await process.wait()
        duration_s = time.monotonic() - started
        return interpret_claude_files(
            stdout_path,
            stderr_path,
            exit_code=int(exit_code),
            duration_s=duration_s,
            argv=tuple(argv),
        )


def interpret_claude_files(
    stdout_path: Path,
    stderr_path: Path,
    *,
    exit_code: int,
    duration_s: float,
    argv: tuple[str, ...],
) -> AdapterOutcome:
    stdout, stderr = read_outputs(stdout_path, stderr_path)
    parsed, invalid = parse_claude_json(stdout) if stdout.strip() else (None, True)
    provider_error = False
    summary = ""
    usage = None
    cost_usd = None
    usage_provenance = None
    cost_provenance = None
    if parsed is not None:
        provider_error = parsed.get("is_error") is True
        summary = str(parsed.get("result") or "")
        usage, usage_provenance = _usage_from_claude(parsed)
        cost_usd, cost_provenance = _cost_from_claude(parsed)
    return AdapterOutcome(
        exit_code=int(exit_code),
        stdout=stdout,
        stderr=stderr,
        duration_s=duration_s,
        parsed=parsed,
        invalid_output=invalid,
        provider_error=provider_error,
        summary=summary,
        usage=usage,
        cost_usd=cost_usd,
        usage_provenance=usage_provenance,
        cost_provenance=cost_provenance,
        argv=argv,
        provider_error_detail="provider reported is_error=true" if provider_error else None,
    )
