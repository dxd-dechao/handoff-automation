"""Cursor CLI adapter: headless `cursor-agent --print` launch, stream-json parse.

Verified against cursor-agent 2026.09.18-9a7762b (`--help`, and the installed
bundle's rule loader and permission matcher; no model call):

- `--print --output-format stream-json` emits one JSON object per line: a
  `system`/`init` event naming the model actually used, assistant/tool events,
  and a terminal `result` event (`is_error`, `subtype`, `result`,
  `session_id`). A run without that terminal event is invalid output.
- Print mode only applies file edits and runs shell commands with `--force`
  ("force allow commands unless explicitly denied"). It is therefore paired
  with a per-run configuration directory (`CURSOR_CONFIG_DIR`, which keeps the
  stored login) whose deny rules block publishing, the handoff CLI, and nested
  agents, and whose sandbox runs shell commands with network access limited to
  an empty allowlist. This is Cursor's own enforcement, not Claude's
  permission file; the operator's global `~/.cursor/cli-config.json` is not
  read or modified.
- HANDOFF.md is git-excluded by `handoff init`. Cursor loads project rules
  from `.cursor/rules/**/*.mdc` alongside AGENTS.md/CLAUDE.md, so the validated
  snapshot is delivered as a temporary always-applied rule named for the
  execution, locally git-excluded, and removed once the worker has stopped.
  Repository AGENTS.md and rules are never touched. The prompt stays the fixed
  ritual phrase.
- Cursor also discovers project and user skills. The delivery rule states the
  Executor role explicitly, and the deny rules stop a skill from recursively
  dispatching `handoff execute`/`watch`.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from handoff_a2a.adapters.base import (
    RITUAL_PROMPT,
    AdapterOutcome,
    RunPreparation,
    as_count,
    read_outputs,
    strip_environment,
)
from handoff_a2a.contracts import Usage

# Inherited Cursor selectors/keys (including a Planner session's own
# CURSOR_* environment) and other providers' credentials never reach the child.
_STRIP_PREFIXES = ("CURSOR_", "ANTHROPIC_", "CLAUDE", "OPENAI_", "AZURE_OPENAI_")
_STRIP_NAMES = ("CODEX_API_KEY", "CODEX_ACCESS_TOKEN")

RULES_DIR = ".cursor/rules"
RULE_PREFIX = "handoff-executor-"
RULE_EXCLUDE_PATTERN = f"{RULES_DIR}/{RULE_PREFIX}*.mdc"
RULE_MARKER = "<!-- handoff-a2a executor delivery -->"
HANDOFF_HEADER = (
    "The following is the verbatim content of HANDOFF.md at the workspace root "
    "(record Execution Notes and Status in that file, not in this rule).\n\n"
)
USAGE_PROVENANCE = "cursor.result.usage"

# Deny beats allow and `--force`. Matching follows the installed CLI:
# `Shell(x)` matches the base command x; `Shell(cmd:args-glob)` matches args.
DENY_RULES = (
    "Shell(git:push*)",
    "Shell(git:merge*)",
    "Shell(git:remote*)",
    "Shell(gh)",
    "Shell(*/gh)",
    "Shell(handoff)",
    "Shell(*/handoff)",
    "Shell(handoff-a2a)",
    "Shell(*/handoff-a2a)",
    "Shell(cursor-agent)",
    "Shell(*/cursor-agent)",
    "Shell(agent)",
    "Shell(*/agent)",
    "Shell(claude)",
    "Shell(codex)",
    "Write(.git/config)",
    "Write(.git/hooks/**)",
)
ALLOW_RULES = ("Read(**)", "Write(**)", "Shell(git)", "Shell(ls)")


@dataclass(frozen=True)
class CursorAdapterConfig:
    binary: str
    model: str
    # Optional server-side credential reference (Cursor API key file). Read at
    # launch and passed only through the child environment, never argv.
    api_key_file: str | None = None


def child_environment(base: Mapping[str, str] | None = None) -> dict[str, str]:
    return strip_environment(_STRIP_PREFIXES, _STRIP_NAMES, base)


def rule_path(workspace: Path, execution_id: str) -> Path:
    return workspace / RULES_DIR / f"{RULE_PREFIX}{execution_id}.mdc"


def rule_text(execution_id: str, handoff_markdown: str) -> str:
    return (
        "---\n"
        "description: handoff Executor task delivery (generated per run; removed afterwards)\n"
        "alwaysApply: true\n"
        "---\n"
        f"{RULE_MARKER}\n"
        f"Handoff execution: {execution_id}\n\n"
        "You are the headless Executor started with the prompt \"execute the handoff\". "
        "Implement the Current Task below directly, following its Executor rules. "
        "Do not use a handoff skill, do not run the `handoff` or `handoff-a2a` CLI, "
        "and do not start another agent.\n\n"
        + HANDOFF_HEADER
        + handoff_markdown
    )


def cli_config(*, sandbox: bool = True) -> dict[str, Any]:
    return {
        "version": 1,
        "permissions": {"allow": list(ALLOW_RULES), "deny": list(DENY_RULES)},
        "approvalMode": "allowlist",
        "autoAcceptWebSearch": False,
        "sandbox": {
            "mode": "enabled" if sandbox else "disabled",
            "networkAccess": "user_config_only",
            "networkAllowlist": [],
        },
    }


def git_exclude_file(workspace: Path) -> Path:
    """Local exclude file via git plumbing (linked worktrees have a .git file)."""
    out = subprocess.run(
        ["git", "-C", str(workspace), "rev-parse", "--path-format=absolute", "--git-path", "info/exclude"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return Path(out)


def ensure_excluded(workspace: Path, pattern: str) -> None:
    exclude = git_exclude_file(workspace)
    exclude.parent.mkdir(parents=True, exist_ok=True)
    lines = exclude.read_text(encoding="utf-8").splitlines() if exclude.is_file() else []
    if pattern in lines:
        return
    with exclude.open("a", encoding="utf-8") as handle:
        if lines and not exclude.read_text(encoding="utf-8").endswith("\n"):
            handle.write("\n")
        handle.write(pattern + "\n")


def _is_tracked(workspace: Path, rel: str) -> bool:
    result = subprocess.run(
        ["git", "-C", str(workspace), "ls-files", "--error-unmatch", rel],
        check=False,
        capture_output=True,
    )
    return result.returncode == 0


def prepare_delivery(
    workspace: Path, execution_id: str, handoff_markdown: str, run_dir: Path
) -> RunPreparation:
    """Write the delivery rule and per-run CLI config. Refuses on any collision."""
    path = rule_path(workspace, execution_id)
    rel = path.relative_to(workspace).as_posix()
    if path.exists() or path.is_symlink():
        raise RuntimeError(f"refusing to overwrite existing {rel}")
    if _is_tracked(workspace, rel):
        raise RuntimeError(f"{rel} is tracked in git; refusing to write a delivery rule there")
    for parent in (workspace / ".cursor", workspace / RULES_DIR):
        if parent.is_symlink():
            raise RuntimeError(f"{parent.relative_to(workspace)} is a symlink; refusing delivery")
    ensure_excluded(workspace, RULE_EXCLUDE_PATTERN)
    created_dirs = [str(p) for p in (workspace / ".cursor", workspace / RULES_DIR) if not p.exists()]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(rule_text(execution_id, handoff_markdown), encoding="utf-8")
    config_dir = run_dir / "cursor-config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "cli-config.json").write_text(
        json.dumps(cli_config(), indent=2) + "\n", encoding="utf-8"
    )
    return RunPreparation(
        env={"CURSOR_CONFIG_DIR": str(config_dir)},
        artifacts={"delivery_rule": rel, "cursor_config_dir": str(config_dir), "created_dirs": created_dirs},
    )


def cleanup_delivery(workspace: Path, execution_id: str, created_dirs: list[str] | None = None) -> bool:
    """Remove this execution's delivery rule (only if it is ours). Idempotent."""
    path = rule_path(workspace, execution_id)
    if path.is_file() and not path.is_symlink():
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return False
        if RULE_MARKER not in text:
            return False
        path.unlink()
    for raw in reversed(created_dirs or []):
        directory = Path(raw)
        try:
            directory.rmdir()  # only if still empty
        except OSError:
            pass
    return not path.exists()


def parse_cursor_stream(raw: str) -> tuple[list[dict[str, Any]], bool]:
    """Events plus invalid flag. Valid output is JSONL ending in a `result` event."""
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
    results = [event for event in events if event["type"] == "result"]
    if not results or not isinstance(results[-1].get("is_error"), bool):
        return events, True
    return events, False


def _usage(raw: Any) -> Usage | None:
    if not isinstance(raw, Mapping):
        return None

    def pick(*names: str) -> int | None:
        for name in names:
            if name in raw:
                return as_count(raw.get(name))
        return None

    return Usage(
        input_tokens=pick("input_tokens", "inputTokens"),
        output_tokens=pick("output_tokens", "outputTokens"),
        cache_creation_input_tokens=pick("cache_creation_input_tokens", "cacheWriteTokens", "cache_write_input_tokens"),
        cache_read_input_tokens=pick("cache_read_input_tokens", "cacheReadTokens", "cached_input_tokens"),
    )


def interpret_cursor_files(
    stdout_path: Path,
    stderr_path: Path,
    *,
    exit_code: int,
    duration_s: float,
    argv: tuple[str, ...],
) -> AdapterOutcome:
    stdout, stderr = read_outputs(stdout_path, stderr_path)
    events, invalid = parse_cursor_stream(stdout) if stdout.strip() else ([], True)
    init = next((e for e in events if e["type"] == "system" and e.get("subtype") == "init"), None)
    result = next((e for e in reversed(events) if e["type"] == "result"), None)
    errors = [e for e in events if e["type"] == "error"]
    provider_error = False
    detail = None
    summary = ""
    usage = None
    usage_provenance = None
    if result is not None:
        subtype = str(result.get("subtype") or "")
        provider_error = result.get("is_error") is True or (bool(subtype) and subtype != "success")
        summary = str(result.get("result") or "")
        if provider_error:
            detail = summary or f"provider reported {subtype or 'is_error=true'}"
        if "usage" in result:
            usage = _usage(result.get("usage"))
            usage_provenance = USAGE_PROVENANCE if usage is not None else None
    elif errors:
        provider_error = True
        detail = "; ".join(str(e.get("message") or e.get("error") or "error") for e in errors)
    parsed = None
    if events:
        parsed = {
            "session_id": (result or init or {}).get("session_id"),
            "reported_model": (init or {}).get("model"),
            "api_key_source": (init or {}).get("apiKeySource"),
            "permission_mode": (init or {}).get("permissionMode"),
            "event_count": len(events),
            "request_id": (result or {}).get("request_id"),
        }
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
        # Cursor reports no price; an unknown cost stays null, never invented.
        cost_usd=None,
        usage_provenance=usage_provenance,
        cost_provenance=None,
        argv=argv,
        provider_error_detail=detail,
    )


class CursorAdapter:
    provider = "cursor"
    display_name = "Cursor"

    def __init__(self, config: CursorAdapterConfig):
        self.config = config
        self.model = config.model

    def argv(self, workspace: Path, handoff_markdown: str | None = None) -> list[str]:
        return [
            self.config.binary,
            "--print",
            "--output-format",
            "stream-json",
            "--model",
            self.config.model,
            "--workspace",
            str(workspace),
            "--trust",
            "--force",
            "--sandbox",
            "enabled",
            RITUAL_PROMPT,
        ]

    def child_environment(self, base: Mapping[str, str] | None = None) -> dict[str, str]:
        env = child_environment(base)
        if self.config.api_key_file:
            key = Path(self.config.api_key_file).expanduser().read_text(encoding="utf-8").strip()
            if not key:
                raise RuntimeError("cursor api_key_file is empty")
            env["CURSOR_API_KEY"] = key
        return env

    def prepare_run(
        self, workspace: Path, execution_id: str, handoff_markdown: str, run_dir: Path
    ) -> RunPreparation:
        return prepare_delivery(workspace, execution_id, handoff_markdown, run_dir)

    def cleanup_run(self, workspace: Path, execution_id: str, preparation: RunPreparation | None) -> bool:
        created = list((preparation.artifacts if preparation else {}).get("created_dirs") or [])
        return cleanup_delivery(workspace, execution_id, created)

    def interpret(
        self,
        stdout_path: Path,
        stderr_path: Path,
        *,
        exit_code: int,
        duration_s: float,
        argv: tuple[str, ...],
    ) -> AdapterOutcome:
        return interpret_cursor_files(
            stdout_path, stderr_path, exit_code=exit_code, duration_s=duration_s, argv=argv
        )
