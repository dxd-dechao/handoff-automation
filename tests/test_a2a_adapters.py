"""Codex/Claude adapters behind the same server boundary (no model calls)."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest
from google.protobuf.json_format import MessageToDict

from a2a_harness import PROVIDERS, make_fake_claude, make_repo, make_server_config, write_server_json
from handoff_a2a.adapters import ClaudeAdapter, CodexAdapter, CursorAdapter, build_adapter
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
    argv = CodexAdapter(CodexAdapterConfig(binary="codex", model="m", reasoning_effort="low")).argv(
        tmp_path, "# HANDOFF\n"
    )
    assert argv[:3] == ["codex", "exec", "--json"]
    assert argv[-1] == RITUAL_PROMPT
    assert argv.count(RITUAL_PROMPT) == 1
    assert "--ignore-user-config" in argv
    assert argv[argv.index("--cd") + 1] == str(tmp_path)
    assert argv[argv.index("--model") + 1] == "m"
    overrides = [argv[i + 1] for i, a in enumerate(argv) if a == "-c"]
    assert 'approval_policy="never"' in overrides
    # HANDOFF.md is git-excluded; the validated snapshot travels as developer instructions.
    assert any(o.startswith("developer_instructions=") for o in overrides)
    assert not any(o.startswith("project_doc") for o in overrides)
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
            "CURSOR_ASKPASS_SECRET": "x",
            "CURSOR_AGENT": "1",
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
    written = _interpret(
        tmp_path,
        [json.dumps({"type": "turn.completed", "usage": {"input_tokens": 5, "cache_write_input_tokens": 0, "output_tokens": 1}})],
    )
    assert written.usage is not None and written.usage.cache_creation_input_tokens == 0
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
@pytest.mark.server
def test_agent_card_reports_actual_executor_identity(tmp_path: Path, provider: str) -> None:
    repo = make_repo(tmp_path)
    fake = make_fake_claude(tmp_path, provider)
    config, _token, _evidence = make_server_config(tmp_path, repo, fake, provider=provider)
    loaded = load_server_config(write_server_json(tmp_path / "server.json", config))
    card = build_agent_card(loaded)
    assert card.name == f"handoff-a2a {provider.capitalize()} Executor"
    ext = next(e for e in card.capabilities.extensions if e.uri == CODING_TASK_PROFILE)
    params = MessageToDict(ext.params)
    assert params["durable_execution_id_deduplication"] is True
    assert params["workspace_code_fingerprint"] is True
    assert params["executor_provider"] == provider
    assert params["executor_model"] == "fake-model"
    adapter = build_adapter(loaded.executor())
    assert isinstance(adapter, {"claude": ClaudeAdapter, "codex": CodexAdapter, "cursor": CursorAdapter}[provider])


@pytest.mark.server
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
    pattern = re.compile(r"\b(claude|codex|cursor|anthropic|openai)\b", re.IGNORECASE)
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
    argv = CodexAdapter(CodexAdapterConfig(binary="codex", model="m")).argv(workspace, "# HANDOFF\n")
    override = next(a for a in argv if a.startswith("skills.config="))
    assert str(home / ".agents" / "skills" / "orca-cli" / "SKILL.md") in override
    assert override.count("enabled=false") == 2
    assert "project" not in override and ".system" not in override
    assert argv[-1] == RITUAL_PROMPT


def test_codex_without_user_skills_adds_no_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    argv = CodexAdapter(CodexAdapterConfig(binary="codex", model="m")).argv(tmp_path, "# HANDOFF\n")
    assert not any(a.startswith("skills.config=") for a in argv)


TRICKY_HANDOFF = (
    Path(__file__).resolve().parents[1] / "templates" / "HANDOFF.md"
).read_text(encoding="utf-8").replace(
    "_(no task planned yet — PLANNER overwrites this section)_",
    'HANDOFF-SENTINEL-7Q: quotes " \' \\ backslash, tab\t, emoji 🚀, CJK 交接, DEL \x7f, ctrl \x01, """triple"""',
)


def _codex_overrides(workspace: Path, markdown: str) -> list[str]:
    argv = CodexAdapter(CodexAdapterConfig(binary="codex", model="m")).argv(workspace, markdown)
    return [argv[i + 1] for i, a in enumerate(argv) if a == "-c"]


def test_codex_handoff_override_round_trips_as_toml(tmp_path: Path) -> None:
    from handoff_a2a.adapters.codex import HANDOFF_HEADER

    value = next(o for o in _codex_overrides(tmp_path, TRICKY_HANDOFF) if o.startswith("developer_instructions="))
    parsed = tomllib.loads(value)
    assert parsed["developer_instructions"] == HANDOFF_HEADER + TRICKY_HANDOFF


def test_codex_argv_reads_workspace_handoff_when_snapshot_absent(tmp_path: Path) -> None:
    (tmp_path / "HANDOFF.md").write_text("# HANDOFF\nfrom-disk\n", encoding="utf-8")
    value = next(o for o in _codex_overrides(tmp_path, None) if o.startswith("developer_instructions="))
    assert "from-disk" in tomllib.loads(value)["developer_instructions"]


def _model_input_text(raw: str) -> str:
    strings: list[str] = []

    def walk(node: object) -> None:
        if isinstance(node, str):
            strings.append(node)
        elif isinstance(node, dict):
            for item in node.values():
                walk(item)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(json.loads(raw))
    return "\n".join(strings)


@pytest.mark.skipif(shutil.which("codex") is None, reason="codex CLI not installed")
@pytest.mark.parametrize("guidance", ["AGENTS.md", "AGENTS.override.md", None])
def test_codex_model_input_carries_full_handoff_and_repo_guidance(tmp_path: Path, guidance: str | None) -> None:
    """Model-free: render Codex's actual model input with the adapter's overrides."""
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    (repo / "HANDOFF.md").write_text(TRICKY_HANDOFF, encoding="utf-8")
    (repo / ".git" / "info" / "exclude").write_text("HANDOFF.md\n", encoding="utf-8")
    if guidance:
        (repo / guidance).write_text(f"REPO-GUIDANCE-SENTINEL from {guidance}\n", encoding="utf-8")
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    args: list[str] = []
    for override in _codex_overrides(repo, TRICKY_HANDOFF):
        args += ["-c", override]
    env = {**os.environ, "CODEX_HOME": str(codex_home)}
    rendered = subprocess.run(
        ["codex", "debug", "prompt-input", *args, RITUAL_PROMPT],
        cwd=repo, env=env, capture_output=True, text=True, timeout=60,
    )
    assert rendered.returncode == 0, rendered.stderr[-2000:]
    text = _model_input_text(rendered.stdout)
    assert TRICKY_HANDOFF in text  # the complete validated handoff, byte for byte
    if guidance:
        assert f"REPO-GUIDANCE-SENTINEL from {guidance}" in text
    assert text.count("HANDOFF-SENTINEL-7Q") == 1  # delivered once, not duplicated


# ── Executor isolation from the Planner skill (A7) ──────────────────────────


def test_claude_executor_launch_denies_the_handoff_cli_and_skill() -> None:
    from handoff_a2a.adapters.claude import EXECUTOR_DISALLOWED_TOOLS, ClaudeAdapterConfig

    argv = ClaudeAdapter(ClaudeAdapterConfig(binary="claude", model="m")).argv()
    denied = argv[argv.index("--disallowedTools") + 1].split(",")
    assert denied == list(EXECUTOR_DISALLOWED_TOOLS)
    for rule in ("Bash(handoff:*)", "Bash(*/handoff:*)", "Bash(handoff-a2a:*)", "Skill(handoff-cli)"):
        assert rule in denied
    assert argv[argv.index("-p") + 1] == RITUAL_PROMPT
    # The legacy (Python-free) executor passes the same list.
    bash = (SRC.parents[1] / "bin" / "handoff").read_text()
    match = re.search(r'^readonly EXECUTOR_DISALLOWED_TOOLS="([^"]+)"$', bash, re.MULTILINE)
    assert match and match.group(1).split(",") == list(EXECUTOR_DISALLOWED_TOOLS)
    # Deliberately not a settings.local.json rule: a Claude Code Planner in the
    # same repository reads that file and must keep running `handoff`.
    assert "handoff" not in (SRC.parents[1] / "templates" / "executor-settings.json").read_text()


def test_codex_disables_the_project_planner_skill(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    workspace = tmp_path / "repo"
    skill = workspace / ".agents" / "skills" / "handoff-cli"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: handoff-cli\n---\n", encoding="utf-8")
    (workspace / ".agents" / "skills" / "team").mkdir()
    (workspace / ".agents" / "skills" / "team" / "SKILL.md").write_text("x", encoding="utf-8")
    argv = CodexAdapter(CodexAdapterConfig(binary="codex", model="m")).argv(workspace, "# HANDOFF\n")
    override = next(a for a in argv if a.startswith("skills.config="))
    assert str((skill / "SKILL.md").resolve()) in override and override.count("enabled=false") == 1
    assert "team" not in override


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.server
def test_executor_runs_cannot_use_the_planner_skill_in_any_location(tmp_path: Path, provider: str) -> None:
    """Skill installed in every project folder and the user folders; one fake Executor run."""
    from a2a_harness import handoff, managed_repo, stop_managed, write_handoff

    home = tmp_path / "home"
    home.mkdir()
    repo, env = managed_repo(tmp_path, provider=provider, extra_env={"HOME": str(home)})
    env.pop("CLAUDE_CONFIG_DIR", None)
    for host in PROVIDERS:
        assert handoff(env, "skill", "install", str(repo), "--host", host).returncode == 0
        assert handoff(env, "skill", "install", "--host", host, "--user").returncode == 0
    write_handoff(repo, status="DRAFT")
    try:
        assert handoff(env, "server", "start", str(repo)).returncode == 0
        assert handoff(env, "approve", str(repo)).returncode == 0
        run = handoff(env, "execute", str(repo))
        assert run.returncode == 0, run.stdout + run.stderr
    finally:
        stop_managed(repo)
    argv = json.loads((repo / "ARGV_PROBE").read_text())
    if provider == "claude":
        denied = argv[argv.index("--disallowedTools") + 1]
        assert "Bash(handoff:*)" in denied and "Skill(handoff-cli)" in denied
    elif provider == "codex":
        override = next(a for a in argv if a.startswith("skills.config="))
        assert str((repo / ".agents" / "skills" / "handoff-cli" / "SKILL.md").resolve()) in override
        assert str((home / ".agents" / "skills" / "handoff-cli" / "SKILL.md").resolve()) in override
    else:
        deny = json.loads((repo / "DELIVERY_PROBE").read_text())["config"]["permissions"]["deny"]
        for rule in ("Shell(handoff)", "Shell(*/handoff)", "Shell(handoff-a2a)", "Shell(*/handoff-a2a)"):
            assert rule in deny
    # The skill copies are workflow files: the run's code fingerprint ignored them.
    assert (repo / "app.py").read_text() == "value = 1\n"
