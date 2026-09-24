"""Executor provider discovery: binaries, versions, model lists, validation.

Each check uses the installed CLI's own documented interface, with the same
credential-stripped environment the Executor child gets, so discovery reflects
the Executor's stored login rather than whatever the calling shell inherited:

- cursor: `cursor-agent models` lists the models this account can use
  (authentication required). Grok and every other ID come only from that
  output; nothing here assumes a catalog.
- codex: `codex debug models` renders the installed CLI's model catalog. It
  is a catalog, not proof of account entitlement; a rejected run is reported
  as a provider error.
- claude: the Claude CLI has no model enumeration. An explicit
  provider-native ID is accepted and labeled unverified.

All commands are argv lists; model IDs (including bracketed parameters) and
paths are data, never shell text.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from handoff_a2a.adapters.claude import child_environment as claude_environment
from handoff_a2a.adapters.codex import child_environment as codex_environment
from handoff_a2a.adapters.cursor import child_environment as cursor_environment

PROVIDERS = ("claude", "codex", "cursor")
BINARY_ENV = {
    "claude": "HANDOFF_CLAUDE_BIN",
    "codex": "HANDOFF_CODEX_BIN",
    "cursor": "HANDOFF_CURSOR_BIN",
}
DEFAULT_BINARIES = {"claude": ("claude",), "codex": ("codex",), "cursor": ("cursor-agent", "agent")}
REASONING_EFFORT_PROVIDERS = frozenset({"codex"})
LOGIN_COMMANDS = {"claude": "claude /login", "codex": "codex login", "cursor": "cursor-agent login"}
DISCOVERY_TIMEOUT_S = 60.0

_MODEL_LINE = re.compile(r"^(?P<id>[A-Za-z0-9][A-Za-z0-9._:/@+-]*)\s+-\s+(?P<name>.+?)\s*$")
_INVISIBLE = re.compile("[\u200b-\u200f\u2060\ufeff]")
_ENVIRONMENTS = {"claude": claude_environment, "codex": codex_environment, "cursor": cursor_environment}


class ProviderError(Exception):
    """Actionable discovery/validation failure; nothing was configured."""


@dataclass(frozen=True)
class ModelInfo:
    id: str
    name: str = ""


@dataclass(frozen=True)
class ModelList:
    provider: str
    binary: str
    version: str
    available: bool
    source: str
    models: tuple[ModelInfo, ...] = ()
    note: str = ""


@dataclass(frozen=True)
class Validation:
    provider: str
    model: str
    binary: str
    version: str
    status: str  # "listed" (reported by the provider CLI) or "unverified"
    source: str
    reasoning_effort: str | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)

    def describe(self) -> str:
        if self.status == "listed":
            return f"listed by {self.source}"
        return f"unverified ({self.source})"


def require_provider(provider: str) -> str:
    if provider not in PROVIDERS:
        raise ProviderError(f"unknown provider {provider!r} (choose {', '.join(PROVIDERS)})")
    return provider


def discovery_env(provider: str, base: Mapping[str, str] | None = None) -> dict[str, str]:
    return _ENVIRONMENTS[require_provider(provider)](base)


def resolve_binary(provider: str, explicit: str | None = None, env: Mapping[str, str] | None = None) -> str:
    """Absolute path of the provider CLI: explicit, HANDOFF_<P>_BIN, then PATH."""
    require_provider(provider)
    env = os.environ if env is None else env
    candidates = [explicit] if explicit else []
    override = env.get(BINARY_ENV[provider])
    if override and not explicit:
        candidates = [override]
    if not candidates:
        candidates = list(DEFAULT_BINARIES[provider])
    for candidate in candidates:
        if "/" in candidate:
            path = Path(candidate).expanduser()
            if path.is_file() and os.access(path, os.X_OK):
                # Keep the stable launcher path (not its versioned symlink
                # target) so a CLI self-update does not strand the config.
                return str(path.absolute())
            continue
        found = shutil.which(candidate, path=env.get("PATH"))
        if found:
            return str(Path(found).absolute())
    names = " or ".join(candidates)
    raise ProviderError(
        f"{provider} CLI not found ({names}); install it or set {BINARY_ENV[provider]} to its path"
    )


def _run(provider: str, argv: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            env=discovery_env(provider),
            stdin=subprocess.DEVNULL,
            timeout=DISCOVERY_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        raise ProviderError(f"{' '.join(argv[:2])} timed out after {DISCOVERY_TIMEOUT_S:.0f}s") from exc
    except OSError as exc:
        raise ProviderError(f"could not run {argv[0]}: {exc}") from exc


def cli_version(provider: str, binary: str) -> str:
    result = _run(provider, [binary, "--version"])
    text = (result.stdout or result.stderr).strip()
    lines = [line for line in text.splitlines() if line.strip() and not line.startswith("WARNING:")]
    if result.returncode != 0 or not lines:
        raise ProviderError(f"{binary} --version failed: {text or result.returncode}")
    return lines[0].strip()


def cursor_auth(binary: str) -> str:
    result = _run("cursor", [binary, "status"])
    text = _INVISIBLE.sub("", (result.stdout + result.stderr)).strip()
    if result.returncode != 0 or "logged in" not in text.lower() or "not logged in" in text.lower():
        raise ProviderError(
            f"Cursor CLI is not logged in for the Executor ({text or 'no status'}); run `{LOGIN_COMMANDS['cursor']}`"
        )
    return text.splitlines()[0]


def parse_cursor_models(text: str) -> tuple[ModelInfo, ...]:
    models: list[ModelInfo] = []
    for raw in text.splitlines():
        line = _INVISIBLE.sub("", raw).strip()
        match = _MODEL_LINE.match(line)
        if match:
            name = match.group("name")
            for suffix in (" (current, default)", " (default)", " (current)"):
                name = name.replace(suffix, "")
            models.append(ModelInfo(match.group("id"), name.strip()))
    return tuple(models)


def list_models(provider: str, binary: str | None = None) -> ModelList:
    binary = resolve_binary(provider, binary)
    version = cli_version(provider, binary)
    if provider == "cursor":
        result = _run(provider, [binary, "models"])
        text = (result.stdout + "\n" + result.stderr).strip()
        if result.returncode != 0 or "authentication required" in text.lower():
            raise ProviderError(
                f"cursor-agent models failed ({text.splitlines()[0] if text else result.returncode}); "
                f"run `{LOGIN_COMMANDS['cursor']}` for the Executor account"
            )
        models = parse_cursor_models(result.stdout)
        if not models:
            raise ProviderError("cursor-agent models returned no model IDs; check the Cursor CLI version")
        return ModelList(provider, binary, version, True, "cursor-agent models (this account)", models)
    if provider == "codex":
        result = _run(provider, [binary, "debug", "models"])
        try:
            data = json.loads(result.stdout)
            entries = data["models"] if isinstance(data, dict) else []
            models = tuple(
                ModelInfo(str(item["slug"]), str(item.get("display_name") or ""))
                for item in entries
                if isinstance(item, dict) and item.get("slug")
            )
        except (json.JSONDecodeError, KeyError, TypeError):
            models = ()
        if result.returncode != 0 or not models:
            return ModelList(
                provider, binary, version, False, "codex debug models",
                note="catalog unavailable; pass an explicit provider-native model ID",
            )
        return ModelList(
            provider, binary, version, True,
            "codex debug models (installed CLI catalog; not proof of account entitlement)",
            models,
        )
    return ModelList(
        provider, binary, version, False, "claude CLI",
        note="the Claude CLI has no model enumeration; pass an explicit provider-native model ID",
    )


def check_model_id(model: str) -> str:
    if not model or not model.strip():
        raise ProviderError("a model ID is required (see `handoff models --provider <name>`)")
    if model != model.strip() or any(ord(ch) < 32 for ch in model):
        raise ProviderError("model ID must not contain whitespace padding or control characters")
    return model


def validate_model(
    provider: str,
    model: str,
    *,
    binary: str | None = None,
    reasoning_effort: str | None = None,
) -> Validation:
    """Verify a candidate before any configuration changes. Never substitutes a model."""
    require_provider(provider)
    check_model_id(model)
    if reasoning_effort is not None:
        if provider not in REASONING_EFFORT_PROVIDERS:
            raise ProviderError(
                f"--reasoning-effort is not supported for {provider}"
                + ("; Cursor model IDs carry their own effort (e.g. a -high variant)" if provider == "cursor" else "")
            )
        if not reasoning_effort.strip() or any(ord(ch) < 32 for ch in reasoning_effort):
            raise ProviderError("--reasoning-effort must be a non-empty word")
    notes: list[str] = []
    if provider == "cursor":
        resolved = resolve_binary(provider, binary)
        cursor_auth(resolved)
    listing = list_models(provider, binary)
    base = model.split("[", 1)[0]
    if listing.available:
        ids = {item.id for item in listing.models}
        if base not in ids and provider == "codex":
            # The Codex catalog is not exhaustive (it omits models earlier live
            # runs used successfully), so absence is not detectable rejection.
            notes.append(f"not in {listing.source}; a rejection will surface as a provider error")
            return Validation(
                provider, model, listing.binary, listing.version, "unverified",
                "not in the installed Codex catalog", reasoning_effort, tuple(notes),
            )
        if base not in ids:
            raise ProviderError(
                f"model {model!r} is not in {listing.source}; "
                f"run `handoff models --provider {provider}` for available IDs (no fallback model is chosen)"
            )
        if base != model:
            notes.append("bracketed parameters are passed to the CLI unchanged")
        return Validation(
            provider, model, listing.binary, listing.version, "listed", listing.source, reasoning_effort, tuple(notes)
        )
    notes.append(listing.note)
    return Validation(
        provider, model, listing.binary, listing.version, "unverified", listing.note, reasoning_effort, tuple(notes)
    )


def main(argv: list[str] | None = None) -> int:
    """`handoff models [repo] --provider <name>`: what the installed CLI reports."""
    import argparse
    import sys

    parser = argparse.ArgumentParser(prog="handoff models")
    parser.add_argument("repo", nargs="?", default=".")
    parser.add_argument("--provider", choices=PROVIDERS, required=True)
    args = parser.parse_args(argv)
    try:
        listing = list_models(args.provider)
    except ProviderError as exc:
        print(f"handoff: {exc}", file=sys.stderr)
        return 1
    print(f"provider: {listing.provider}")
    print(f"binary:   {listing.binary}")
    print(f"version:  {listing.version}")
    if not listing.available:
        print(f"models:   enumeration unavailable — {listing.note}")
        return 0
    print(f"source:   {listing.source}")
    for item in listing.models:
        print(f"  {item.id}" + (f"  ({item.name})" if item.name and item.name != item.id else ""))
    return 0
