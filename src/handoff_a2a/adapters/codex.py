"""Codex CLI adapter: `codex exec --json` launch, JSONL parse, auth stripping.

Verified against codex-cli 0.156.0 (`codex exec --help`, `codex sandbox`):

- `--ignore-user-config` keeps the operator's `~/.codex/config.toml` (notify
  hooks, plugins, default model/effort/tier) out of the Executor run; the
  stored login in CODEX_HOME is still used.
- The built-in `:workspace` permission profile allows edits in the workspace,
  but keeps `<root>/.git` read-only (so `git commit` fails) and turns network
  off. The `handoff` profile below extends it with `.git` write only, so the
  Executor can commit locally while `git push` / `gh pr create` still cannot
  reach a remote. This is Codex's sandbox, not Claude's permission file; the
  two are not equivalent (e.g. Codex may also write to temp directories).
- `approval_policy = "never"`: a headless run can never answer a prompt.
- User-scope skills (`~/.agents/skills`, `$CODEX_HOME/skills`) are disabled
  per run via `skills.config` (by SKILL.md path; verified with
  `codex debug prompt-input`). `--ignore-user-config` does not cover them, and
  in the first live check an operator skill whose description matched
  "handoff" hijacked the ritual prompt. Target-repo skills (`.agents/skills`
  in the workspace) and Codex's bundled system skills stay available.
- HANDOFF.md is git-excluded by `handoff init`, so Codex's ignore-aware file
  search (`rg --files`) does not see it; in the live check Codex then guessed a
  task from the code. The validated handoff snapshot is therefore delivered as
  `developer_instructions`, which Codex adds alongside the repository's own
  AGENTS.md / AGENTS.override.md guidance (a project-doc fallback would be
  dropped whenever AGENTS.md exists). Verified with `codex debug
  prompt-input`. The ritual prompt stays fixed; the only added text is a
  one-line provenance header naming the file.
"""

from __future__ import annotations

import json
import os
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

# Inherited API keys would silently bill a different account than the
# Executor's stored login; Claude selectors and a Cursor Planner session's
# CURSOR_* variables are irrelevant to this child.
_STRIP_PREFIXES = ("OPENAI_", "AZURE_OPENAI_", "ANTHROPIC_", "CLAUDE", "CURSOR_")
_STRIP_NAMES = ("CODEX_API_KEY", "CODEX_ACCESS_TOKEN")

PERMISSION_PROFILE = "handoff"
PERMISSION_PROFILE_TOML = (
    f'permissions.{PERMISSION_PROFILE}={{extends=":workspace", '
    'filesystem={":workspace_roots"={"."="write", ".git"="write"}}}'
)
HANDOFF_HEADER = "The following is the verbatim content of HANDOFF.md at the workspace root.\n\n"
USAGE_PROVENANCE = "codex.turn.completed.usage (input_tokens includes cache_read_input_tokens)"


def user_skill_files(env: Mapping[str, str] | None = None) -> list[Path]:
    """SKILL.md files in the operator's user-scope skill folders (not the workspace's)."""
    env = os.environ if env is None else env
    home = Path(env.get("HOME") or Path.home())
    codex_home = Path(env.get("CODEX_HOME") or home / ".codex")
    found: list[Path] = []
    for root in (home / ".agents" / "skills", codex_home / "skills"):
        if not root.is_dir():
            continue
        for folder in sorted(root.iterdir()):
            # Dot-folders (e.g. `.system`) hold Codex's bundled skills.
            if folder.name.startswith("."):
                continue
            skill = folder / "SKILL.md"
            if skill.is_file():
                found.append(skill.resolve())
    return found


def toml_string(text: str) -> str:
    """TOML basic string for a `-c key=value` override (UTF-8 kept; DEL escaped)."""
    return json.dumps(text, ensure_ascii=False).replace("\x7f", "\\u007f")


def handoff_override(workspace: Path, handoff_markdown: str | None) -> str:
    if handoff_markdown is None:
        handoff_markdown = (workspace / "HANDOFF.md").read_text(encoding="utf-8")
    return f"developer_instructions={toml_string(HANDOFF_HEADER + handoff_markdown)}"


def disable_skills_override(skill_files: list[Path]) -> str | None:
    if not skill_files:
        return None
    # JSON string escaping is valid TOML basic-string escaping for paths.
    entries = ", ".join(f"{{path={json.dumps(str(path))}, enabled=false}}" for path in skill_files)
    return f"skills.config=[{entries}]"


@dataclass(frozen=True)
class CodexAdapterConfig:
    binary: str
    model: str
    reasoning_effort: str | None = None


def child_environment(base: Mapping[str, str] | None = None) -> dict[str, str]:
    return strip_environment(_STRIP_PREFIXES, _STRIP_NAMES, base)


def parse_codex_jsonl(raw: str) -> tuple[list[dict[str, Any]], bool]:
    """Events plus invalid flag. Valid output is JSONL with a completed turn."""
    events: list[dict[str, Any]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return events, True
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            return events, True
        events.append(event)
    types = {event["type"] for event in events}
    if not ({"turn.completed", "turn.failed", "error"} & types):
        return events, True
    return events, False


def _error_message(event: Mapping[str, Any]) -> str:
    error = event.get("error")
    if isinstance(error, Mapping) and error.get("message"):
        return str(error["message"])
    if event.get("message"):
        return str(event["message"])
    return str(event.get("type"))


def interpret_codex_files(
    stdout_path: Path,
    stderr_path: Path,
    *,
    exit_code: int,
    duration_s: float,
    argv: tuple[str, ...],
) -> AdapterOutcome:
    stdout, stderr = read_outputs(stdout_path, stderr_path)
    events, invalid = parse_codex_jsonl(stdout) if stdout.strip() else ([], True)
    # Transient `error` events (e.g. stream reconnects) can precede a completed
    # turn; only a failed turn, or errors without any completed turn, count.
    completed = any(event["type"] == "turn.completed" for event in events)
    errors = [
        event
        for event in events
        if event["type"] == "turn.failed" or (event["type"] == "error" and not completed)
    ]
    summary = ""
    usage = None
    usage_provenance = None
    totals: dict[str, int | None] = {}
    thread_id = None
    for event in events:
        kind = event["type"]
        if kind == "thread.started":
            thread_id = event.get("thread_id")
        elif kind == "item.completed":
            item = event.get("item")
            if isinstance(item, Mapping) and item.get("type") == "agent_message":
                summary = str(item.get("text") or "")
        elif kind == "turn.completed" and isinstance(event.get("usage"), Mapping):
            for name, value in event["usage"].items():
                count = as_count(value)
                if count is None:
                    continue
                totals[name] = (totals.get(name) or 0) + count
    if totals:
        usage = Usage(
            input_tokens=totals.get("input_tokens"),
            output_tokens=totals.get("output_tokens"),
            # Reported by codex-cli 0.156.0 as cache_write_input_tokens; absent → unknown.
            cache_creation_input_tokens=totals.get("cache_write_input_tokens"),
            cache_read_input_tokens=totals.get("cached_input_tokens"),
        )
        usage_provenance = USAGE_PROVENANCE
    parsed = None
    if events:
        parsed = {"thread_id": thread_id, "event_count": len(events), "raw_usage": totals or None}
    return AdapterOutcome(
        exit_code=int(exit_code),
        stdout=stdout,
        stderr=stderr,
        duration_s=duration_s,
        parsed=parsed,
        invalid_output=invalid,
        provider_error=bool(errors),
        summary=summary,
        usage=usage,
        # Codex JSONL reports no price; an unknown cost stays null, never invented.
        cost_usd=None,
        usage_provenance=usage_provenance,
        cost_provenance=None,
        argv=argv,
        provider_error_detail="; ".join(_error_message(event) for event in errors) or None,
    )


class CodexAdapter:
    provider = "codex"
    display_name = "Codex"

    def __init__(self, config: CodexAdapterConfig):
        self.config = config
        self.model = config.model

    def argv(self, workspace: Path, handoff_markdown: str | None = None) -> list[str]:
        argv = [
            self.config.binary,
            "exec",
            "--json",
            "--ignore-user-config",
            "--model",
            self.config.model,
            "-c",
            PERMISSION_PROFILE_TOML,
            "-c",
            f'default_permissions="{PERMISSION_PROFILE}"',
            "-c",
            'approval_policy="never"',
            "-c",
            handoff_override(workspace, handoff_markdown),
            "--cd",
            str(workspace),
        ]
        if self.config.reasoning_effort:
            argv += ["-c", f'model_reasoning_effort="{self.config.reasoning_effort}"']
        disable = disable_skills_override(user_skill_files())
        if disable:
            argv += ["-c", disable]
        return argv + [RITUAL_PROMPT]

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
        return interpret_codex_files(
            stdout_path, stderr_path, exit_code=exit_code, duration_s=duration_s, argv=argv
        )
