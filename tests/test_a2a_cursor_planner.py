"""Cursor as Planner (launcher + skill) and Cursor Executor delivery units (no model calls)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from a2a_harness import git, handoff, make_repo, managed_env, managed_repo
from handoff_a2a.adapters.base import RITUAL_PROMPT
from handoff_a2a.adapters.cursor import (
    DENY_RULES,
    RULE_EXCLUDE_PATTERN,
    CursorAdapter,
    CursorAdapterConfig,
    child_environment,
    cleanup_delivery,
    interpret_cursor_files,
    prepare_delivery,
    rule_path,
)
from handoff_a2a.workspace import is_workflow_path

REPO_SKILL = Path(__file__).resolve().parents[1] / "skills" / "handoff-cli"


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ── Planner ────────────────────────────────────────────────────────────────


def test_planner_launch_is_interactive_and_leaves_executor_selection_alone(tmp_path: Path) -> None:
    repo, env = managed_repo(tmp_path, provider="codex", planner="cursor")
    server = repo / ".handoff-logs" / "server.json"
    before = _digest(server)
    printed = handoff(env, "planner", str(repo), "--provider", "cursor", "--model", "grok-4.7-high", "--print-command")
    assert printed.returncode == 0, printed.stderr
    command = printed.stdout.split("command:", 1)[1]
    assert "--model grok-4.7-high" in command and "--workspace" in command
    tokens = command.split()
    for flag in ("--print", "-p", "--force", "--mode", "--plan", "--yolo"):
        assert flag not in tokens
    assert "execute the handoff" not in command
    probe = tmp_path / "interactive.json"
    launched = handoff({**env, "FAKE_INTERACTIVE_PROBE": str(probe)}, "planner", str(repo), "--provider", "cursor", "--model", "grok-4.7-high")
    assert launched.returncode == 0, launched.stderr
    seen = json.loads(probe.read_text())
    assert seen["argv"] == ["--model", "grok-4.7-high", "--workspace", str(repo)]
    assert Path(seen["cwd"]).resolve() == repo
    assert "/handoff-cli" in launched.stdout and "exiting is not approval" in launched.stdout
    assert _digest(server) == before  # Executor stays codex
    assert json.loads(server.read_text())["codex"]["model"] == "fake-model"
    assert not (repo / ".handoff-logs" / "workflow.json").exists()  # nothing approved


def test_planner_requires_skill_and_an_available_model(tmp_path: Path) -> None:
    repo, env = managed_repo(tmp_path)
    missing = handoff(env, "planner", str(repo), "--provider", "cursor", "--model", "fake-model", "--print-command")
    assert missing.returncode == 1 and "--planner cursor" in missing.stderr
    assert handoff(env, "init", str(repo), "--planner", "cursor").returncode == 0  # additive re-init
    unknown = handoff(env, "planner", str(repo), "--provider", "cursor", "--model", "grok-9", "--print-command")
    assert unknown.returncode == 1 and "not in cursor-agent models" in unknown.stderr
    ok = handoff(env, "planner", str(repo), "--provider", "cursor", "--model", "fake-model", "--print-command")
    assert ok.returncode == 0, ok.stderr


def test_installed_skill_matches_repository_skill_and_states_both_roles(tmp_path: Path) -> None:
    repo, _env = managed_repo(tmp_path, planner="cursor")
    installed = repo / ".cursor" / "skills" / "handoff-cli" / "SKILL.md"
    assert installed.read_bytes() == (REPO_SKILL / "SKILL.md").read_bytes()
    text = installed.read_text()
    assert text.startswith("---\nname: handoff-cli\n")
    for phrase in ("DRAFT", "handoff approve", "handoff model", "must **not** run", "cursor"):
        assert phrase in text, phrase
    assert is_workflow_path(".cursor/skills/handoff-cli/SKILL.md")
    assert not is_workflow_path(".cursor/skills/other/SKILL.md")
    assert not is_workflow_path(".cursor/rules/team.mdc")
    assert is_workflow_path(".cursor/rules/handoff-executor-123.mdc")


def test_legacy_repo_can_add_the_planner_skill_without_python(tmp_path: Path) -> None:
    import os

    repo = make_repo(tmp_path)
    env = os.environ.copy()
    env["HANDOFF_A2A_BIN"] = str(tmp_path / "no-a2a")
    result = handoff(env, "init", str(repo), "--transport", "legacy", "--planner", "cursor")
    assert result.returncode == 0, result.stderr
    assert (repo / ".cursor" / "skills" / "handoff-cli" / "SKILL.md").is_file()
    assert git(repo, "status", "--porcelain", "--", ".cursor").stdout.strip() == ""


# ── Cursor Executor units ────────────────────────────────────────────────────


def test_cursor_argv_pairs_force_with_sandbox_and_keeps_the_ritual_prompt(tmp_path: Path) -> None:
    model = "claude-opus-4-8[context=1m,effort=high]"
    argv = CursorAdapter(CursorAdapterConfig(binary="/x/cursor-agent", model=model)).argv(tmp_path, "# H\n")
    assert argv[0] == "/x/cursor-agent"
    assert argv[-1] == RITUAL_PROMPT and argv.count(RITUAL_PROMPT) == 1
    assert argv[argv.index("--model") + 1] == model  # bracketed parameters are one argv item
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    assert argv[argv.index("--workspace") + 1] == str(tmp_path)
    assert "--force" in argv and argv[argv.index("--sandbox") + 1] == "enabled"
    for forbidden in ("--approve-mcps", "--yolo", "--resume", "--continue", "--api-key"):
        assert forbidden not in argv


def test_cursor_environment_strips_inherited_credentials(tmp_path: Path) -> None:
    env = child_environment(
        {
            "PATH": "/bin",
            "HOME": "/h",
            "CURSOR_API_KEY": "planner-key",
            "CURSOR_AGENT": "1",
            "CURSOR_CONFIG_DIR": "/planner",
            "ANTHROPIC_API_KEY": "x",
            "CLAUDECODE": "1",
            "OPENAI_API_KEY": "x",
            "CODEX_API_KEY": "x",
        }
    )
    assert env == {"PATH": "/bin", "HOME": "/h"}
    key = tmp_path / "cursor-key"
    key.write_text("server-side-key\n")
    adapter = CursorAdapter(CursorAdapterConfig(binary="c", model="m", api_key_file=str(key)))
    child = adapter.child_environment({"PATH": "/bin", "CURSOR_API_KEY": "inherited"})
    assert child["CURSOR_API_KEY"] == "server-side-key"
    assert "server-side-key" not in " ".join(adapter.argv(tmp_path))


def _interpret(tmp_path: Path, lines: list[str], exit_code: int = 0):
    out = tmp_path / "stdout.json"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return interpret_cursor_files(out, tmp_path / "none", exit_code=exit_code, duration_s=1.0, argv=("cursor-agent",))


INIT = json.dumps({"type": "system", "subtype": "init", "model": "Grok 4.7 High", "apiKeySource": "login", "session_id": "s"})


def test_cursor_stream_success_reports_identity_and_zero_usage(tmp_path: Path) -> None:
    result = json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "done", "session_id": "s", "usage": {"inputTokens": 0, "outputTokens": 7}})
    outcome = _interpret(tmp_path, [INIT, result])
    assert outcome.invalid_output is False and outcome.provider_error is False
    assert outcome.summary == "done"
    assert outcome.parsed["reported_model"] == "Grok 4.7 High"
    assert outcome.parsed["api_key_source"] == "login"
    assert outcome.usage is not None and outcome.usage.input_tokens == 0 and outcome.usage.output_tokens == 7
    assert outcome.cost_usd is None and outcome.cost_provenance is None
    bare = _interpret(tmp_path, [json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "x"})])
    assert bare.usage is None and bare.usage_provenance is None  # absent stays null


@pytest.mark.parametrize(
    ("lines", "invalid", "error"),
    [
        ([INIT, json.dumps({"type": "result", "subtype": "error", "is_error": True, "result": "model unavailable"})], False, True),
        ([INIT, json.dumps({"type": "result", "subtype": "success", "is_error": False})][:1], True, False),
        ([INIT, "not json"], True, False),
        ([json.dumps({"unrelated": 1})], True, False),
        ([INIT, json.dumps({"type": "result", "subtype": "success"})], True, False),
        ([INIT, json.dumps({"type": "error", "message": "auth expired"})], True, True),
    ],
)
def test_cursor_stream_failures_are_not_success(tmp_path: Path, lines: list[str], invalid: bool, error: bool) -> None:
    outcome = _interpret(tmp_path, lines)
    assert outcome.invalid_output is invalid
    assert outcome.provider_error is error
    if error:
        assert outcome.provider_error_detail


def test_delivery_rule_is_collision_safe_excluded_and_removed(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    rules = repo / ".cursor" / "rules"
    rules.mkdir(parents=True)
    (rules / "team.mdc").write_text("---\nalwaysApply: true\n---\nteam rule\n", encoding="utf-8")
    (repo / "AGENTS.md").write_text("repo guidance\n", encoding="utf-8")
    git(repo, "add", ".cursor/rules/team.mdc", "AGENTS.md")
    git(repo, "commit", "-qm", "team guidance")
    handoff_text = (repo / "HANDOFF.md").read_text()
    run_dir = tmp_path / "evidence" / "exec-1"
    prep = prepare_delivery(repo, "exec-1", handoff_text, run_dir)
    path = rule_path(repo, "exec-1")
    body = path.read_text()
    assert body.startswith("---\n") and "alwaysApply: true" in body
    assert body.endswith(handoff_text) and "Handoff execution: exec-1" in body
    assert "do not run the `handoff`" in body
    config = json.loads((Path(prep.env["CURSOR_CONFIG_DIR"]) / "cli-config.json").read_text())
    assert set(DENY_RULES) <= set(config["permissions"]["deny"])
    for rule in ("Read(.handoff-logs/credentials/**)", "Write(.handoff-logs/**)", "Write(.handoff-config.json)"):
        assert rule in config["permissions"]["deny"]
    exclude = git(repo, "rev-parse", "--path-format=absolute", "--git-path", "info/exclude").stdout.strip()
    assert RULE_EXCLUDE_PATTERN in Path(exclude).read_text().splitlines()
    assert git(repo, "status", "--porcelain").stdout.count("handoff-executor") == 0
    with pytest.raises(RuntimeError, match="refusing to overwrite"):
        prepare_delivery(repo, "exec-1", handoff_text, run_dir)
    assert cleanup_delivery(repo, "exec-1", list(prep.artifacts["created_dirs"]))
    assert not path.exists()
    assert (rules / "team.mdc").read_text().endswith("team rule\n")
    assert (repo / "AGENTS.md").read_text() == "repo guidance\n"
    # A same-named file that is not ours is never deleted.
    path.write_text("user file\n", encoding="utf-8")
    assert cleanup_delivery(repo, "exec-1") is False and path.read_text() == "user file\n"


def test_delivery_refuses_symlinked_rules_and_removes_created_dirs(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    prep = prepare_delivery(repo, "e2", "# H\n", tmp_path / "run")
    assert cleanup_delivery(repo, "e2", list(prep.artifacts["created_dirs"]))
    assert not (repo / ".cursor").exists()  # created only for the run
    (tmp_path / "elsewhere").mkdir()
    (repo / ".cursor").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(RuntimeError, match="symlink"):
        prepare_delivery(repo, "e3", "# H\n", tmp_path / "run3")
    assert not list((tmp_path / "elsewhere").iterdir())
