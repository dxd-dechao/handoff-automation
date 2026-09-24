"""Codex/Claude adapters behind the same server boundary (no model calls)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from google.protobuf.json_format import MessageToDict

from a2a_harness import PROVIDERS, make_fake_claude, make_repo, make_server_config, write_server_json
from handoff_a2a.adapters import ClaudeAdapter, CodexAdapter, build_adapter
from handoff_a2a.adapters.base import RITUAL_PROMPT
from handoff_a2a.adapters.codex import (
    CodexAdapterConfig,
    child_environment as codex_environment,
    interpret_codex_files,
    user_skill_files,
)
from handoff_a2a.contracts import CODING_TASK_PROFILE
from handoff_a2a.server import build_agent_card, load_server_config

SRC = Path(__file__).resolve().parents[1] / "src" / "handoff_a2a"


def _interpret(tmp_path: Path, lines: list[str], exit_code: int = 0):
    out = tmp_path / "stdout.json"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return interpret_codex_files(out, tmp_path / "missing-stderr", exit_code=exit_code, duration_s=1.0, argv=("codex",))


def test_codex_argv_uses_ritual_prompt_and_restricted_sandbox(tmp_path: Path) -> None:
    argv = CodexAdapter(CodexAdapterConfig(binary="codex", model="m", reasoning_effort="low")).argv(tmp_path)
    assert argv[:3] == ["codex", "exec", "--json"]
    assert argv[-1] == RITUAL_PROMPT
    assert argv.count(RITUAL_PROMPT) == 1
    assert "--ignore-user-config" in argv
    assert argv[argv.index("--cd") + 1] == str(tmp_path)
    assert argv[argv.index("--model") + 1] == "m"
    overrides = [argv[i + 1] for i, a in enumerate(argv) if a == "-c"]
    assert 'approval_policy="never"' in overrides
    # HANDOFF.md is git-excluded; Codex loads it natively instead of via extra prompt text.
    assert 'project_doc_fallback_filenames=["HANDOFF.md"]' in overrides
    assert any(o.startswith("project_doc_max_bytes=") for o in overrides)
    assert 'default_permissions="handoff"' in overrides
    assert 'model_reasoning_effort="low"' in overrides
    profile = next(o for o in overrides if o.startswith("permissions.handoff="))
    assert 'extends=":workspace"' in profile and '".git"="write"' in profile
    assert "network" not in profile  # network stays off: no push/PR publication
    joined = " ".join(argv)
    assert "danger" not in joined and "bypass" not in joined and "--sandbox" not in joined


def test_codex_child_environment_strips_provider_credentials() -> None:
    env = codex_environment(
        {
            "PATH": "/bin",
            "HOME": "/h",
            "CODEX_HOME": "/h/.codex",
            "OPENAI_API_KEY": "x",
            "OPENAI_BASE_URL": "x",
            "CODEX_API_KEY": "x",
            "AZURE_OPENAI_API_KEY": "x",
            "ANTHROPIC_API_KEY": "x",
            "CLAUDECODE": "1",
        }
    )
    assert env == {"PATH": "/bin", "HOME": "/h", "CODEX_HOME": "/h/.codex"}


def test_codex_jsonl_success_usage_and_unknown_cost(tmp_path: Path) -> None:
    outcome = _interpret(
        tmp_path,
        [
            json.dumps({"type": "thread.started", "thread_id": "t1"}),
            json.dumps({"type": "error", "message": "Reconnecting... 1/5"}),
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "done"}}),
            json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 3}}),
        ],
    )
    assert outcome.invalid_output is False
    assert outcome.provider_error is False  # transient error before a completed turn
    assert outcome.summary == "done"
    assert outcome.usage is not None
    assert outcome.usage.input_tokens == 10
    assert outcome.usage.cache_read_input_tokens == 0  # valid zero is kept
    assert outcome.usage.cache_creation_input_tokens is None
    assert outcome.cost_usd is None and outcome.cost_provenance is None
    assert outcome.parsed and outcome.parsed["thread_id"] == "t1"


@pytest.mark.parametrize(
    ("lines", "invalid", "provider_error"),
    [
        ([json.dumps({"type": "turn.failed", "error": {"message": "quota"}})], False, True),
        ([json.dumps({"type": "error", "message": "unauthorized"})], False, True),
        (["not json"], True, False),
        ([json.dumps({"unrelated": 1})], True, False),
        ([json.dumps({"type": "thread.started", "thread_id": "t"})], True, False),
    ],
)
def test_codex_failures_are_reported(tmp_path: Path, lines: list[str], invalid: bool, provider_error: bool) -> None:
    outcome = _interpret(tmp_path, lines)
    assert outcome.invalid_output is invalid
    assert outcome.provider_error is provider_error
    if provider_error:
        assert outcome.provider_error_detail in {"quota", "unauthorized"}


@pytest.mark.parametrize("provider", PROVIDERS)
def test_agent_card_reports_actual_executor_identity(tmp_path: Path, provider: str) -> None:
    repo = make_repo(tmp_path)
    fake = make_fake_claude(tmp_path, provider)
    config, _token, _evidence = make_server_config(tmp_path, repo, fake, provider=provider)
    loaded = load_server_config(write_server_json(tmp_path / "server.json", config))
    card = build_agent_card(loaded)
    assert card.name == f"handoff-a2a {'Claude' if provider == 'claude' else 'Codex'} Executor"
    ext = next(e for e in card.capabilities.extensions if e.uri == CODING_TASK_PROFILE)
    params = MessageToDict(ext.params)
    assert params["durable_execution_id_deduplication"] is True
    assert params["workspace_code_fingerprint"] is True
    assert params["executor_provider"] == provider
    assert params["executor_model"] == "fake-model"
    adapter = build_adapter(loaded.executor())
    assert isinstance(adapter, ClaudeAdapter if provider == "claude" else CodexAdapter)


def test_server_config_requires_exactly_one_adapter(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    config, _token, _evidence = make_server_config(tmp_path, repo, make_fake_claude(tmp_path))
    path = write_server_json(tmp_path / "server.json", config)
    raw = json.loads(path.read_text())
    assert load_server_config(path).claude is not None  # existing Claude configs still load
    raw["codex"] = {"binary": "codex", "model": "m"}
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="exactly one"):
        load_server_config(path)
    del raw["claude"]
    del raw["codex"]
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="exactly one"):
        load_server_config(path)


def test_neutral_client_and_orchestration_have_no_provider_branches() -> None:
    pattern = re.compile(r"\b(claude|codex|anthropic|openai)\b", re.IGNORECASE)
    for name in ("client.py", "integration.py", "workflow.py", "config.py", "contracts.py", "reporting.py"):
        hits = [line for line in (SRC / name).read_text().splitlines() if pattern.search(line)]
        assert hits == [], f"{name}: {hits}"


def test_codex_disables_user_scope_skills_but_not_workspace_skills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    for folder in (
        home / ".agents" / "skills" / "orca-cli",
        home / ".codex" / "skills" / "personal",
        home / ".codex" / "skills" / ".system" / "bundled",
    ):
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text("---\nname: x\ndescription: handoff\n---\n", encoding="utf-8")
    (home / ".agents" / "skills" / "not-a-skill").mkdir()
    workspace = tmp_path / "repo"
    (workspace / ".agents" / "skills" / "project").mkdir(parents=True)
    (workspace / ".agents" / "skills" / "project" / "SKILL.md").write_text("x", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    files = user_skill_files()
    assert [f.parent.name for f in files] == ["orca-cli", "personal"]
    argv = CodexAdapter(CodexAdapterConfig(binary="codex", model="m")).argv(workspace)
    override = next(a for a in argv if a.startswith("skills.config="))
    assert str(home / ".agents" / "skills" / "orca-cli" / "SKILL.md") in override
    assert override.count("enabled=false") == 2
    assert "project" not in override and ".system" not in override
    assert argv[-1] == RITUAL_PROMPT


def test_codex_without_user_skills_adds_no_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    argv = CodexAdapter(CodexAdapterConfig(binary="codex", model="m")).argv(tmp_path)
    assert not any(a.startswith("skills.config=") for a in argv)
