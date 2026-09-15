"""Optional target-repository `.handoff-config.json` for the production CLI."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from handoff_a2a.workspace import CONFIG_NAME


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class A2ASettings:
    agent_card_url: str
    workspace_id: str
    credential_file: Path
    request_timeout_s: float = 30.0
    wait_timeout_s: float = 180.0
    poll_interval_s: float = 1.0


@dataclass(frozen=True)
class HandoffConfig:
    transport: str
    path: Path
    max_rounds: int = 3
    a2a: A2ASettings | None = None


def _positive_number(raw: Any, name: str, default: float) -> float:
    if raw is None:
        return default
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ConfigError(f"{name} must be a positive number")
    value = float(raw)
    if value <= 0:
        raise ConfigError(f"{name} must be a positive number")
    return value


def _positive_int(raw: Any, name: str, default: int) -> int:
    if raw is None:
        return default
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        raise ConfigError(f"{name} must be a positive integer")
    return raw


def config_path(repo: Path) -> Path:
    return repo / CONFIG_NAME


def load_config(repo: Path) -> HandoffConfig | None:
    """Return None when the file is absent (legacy). Invalid files raise ConfigError."""
    path = config_path(repo)
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"malformed {CONFIG_NAME}: {exc.msg}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{CONFIG_NAME} must be a JSON object")
    transport = raw.get("transport")
    if not isinstance(transport, str) or not transport:
        raise ConfigError("transport must be a non-empty string")
    if transport not in {"legacy", "a2a"}:
        raise ConfigError(f"unknown transport {transport!r}")
    max_rounds = _positive_int(raw.get("max_rounds"), "max_rounds", 3)
    a2a: A2ASettings | None = None
    if transport == "a2a":
        block = raw.get("a2a")
        if not isinstance(block, dict):
            raise ConfigError("a2a configuration object is required when transport is a2a")
        card = block.get("agent_card_url")
        workspace_id = block.get("workspace_id")
        cred = block.get("credential_file")
        if not isinstance(card, str) or not card.strip():
            raise ConfigError("a2a.agent_card_url is required")
        if not isinstance(workspace_id, str) or not workspace_id.strip():
            raise ConfigError("a2a.workspace_id is required")
        if not isinstance(cred, str) or not cred.strip():
            raise ConfigError("a2a.credential_file is required")
        credential_file = Path(cred).expanduser()
        if not credential_file.is_absolute():
            credential_file = (repo / credential_file).resolve()
        else:
            credential_file = credential_file.resolve()
        if not credential_file.is_file():
            raise ConfigError(f"credential-file not found: {credential_file}")
        a2a = A2ASettings(
            agent_card_url=card.strip(),
            workspace_id=workspace_id.strip(),
            credential_file=credential_file,
            request_timeout_s=_positive_number(
                block.get("request_timeout_s"), "a2a.request_timeout_s", 30.0
            ),
            wait_timeout_s=_positive_number(
                block.get("wait_timeout_s"), "a2a.wait_timeout_s", 180.0
            ),
            poll_interval_s=_positive_number(
                block.get("poll_interval_s"), "a2a.poll_interval_s", 1.0
            ),
        )
    return HandoffConfig(transport=transport, path=path, max_rounds=max_rounds, a2a=a2a)


def require_a2a_config(repo: Path) -> tuple[HandoffConfig, A2ASettings]:
    config = load_config(repo)
    if config is None or config.transport != "a2a" or config.a2a is None:
        raise ConfigError("A2A transport is not configured")
    return config, config.a2a
