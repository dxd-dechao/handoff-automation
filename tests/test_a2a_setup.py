"""`handoff init` / `handoff models`: generated configuration without hand-written JSON."""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from a2a_harness import (
    PROVIDERS,
    field,
    git,
    handoff,
    ignore_probes,
    make_repo,
    managed_env,
    managed_repo,
)
from handoff_a2a.config import load_config
from handoff_a2a.server import load_server_config

EXCLUDES = (
    "HANDOFF.md",
    ".handoff-logs/",
    ".handoff-config.json",
    ".cursor/rules/handoff-executor-*.mdc",
)


def _exclude_lines(repo: Path) -> list[str]:
    path = git(repo, "rev-parse", "--path-format=absolute", "--git-path", "info/exclude").stdout.strip()
    return Path(path).read_text(encoding="utf-8").splitlines()


def _generated(repo: Path) -> list[Path]:
    return [p for p in (repo / ".handoff-config.json", repo / ".handoff-logs" / "server.json", repo / ".handoff-logs" / "credentials") if p.exists()]


def test_noninteractive_bare_init_prints_the_command_and_changes_nothing(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    (repo / "HANDOFF.md").unlink()
    env = managed_env(tmp_path)
    before = sorted(p.name for p in repo.iterdir())
    result = handoff(env, "init", str(repo))
    assert result.returncode == 2
    assert "--transport a2a --executor <claude|codex|cursor> --model" in result.stderr
    assert "--transport legacy" in result.stderr
    assert "nothing was changed" in result.stderr
    assert sorted(p.name for p in repo.iterdir()) == before
    # --transport a2a without a model on a non-TTY is the same refusal.
    partial = handoff(env, "init", str(repo), "--transport", "a2a", "--executor", "cursor")
    assert partial.returncode == 2 and not _generated(repo)


@pytest.mark.parametrize("provider", PROVIDERS)
def test_scripted_init_generates_valid_files_for_each_provider(tmp_path: Path, provider: str) -> None:
    space = tmp_path / "work space"
    space.mkdir()
    repo, env = managed_repo(space, provider=provider, planner="cursor" if provider == "cursor" else None)
    config = load_config(repo)
    assert config is not None and config.transport == "a2a" and config.managed is not None
    server = load_server_config(repo / ".handoff-logs" / "server.json")
    assert server.executor_name() == provider
    assert server.executor().model == "fake-model"
    assert server.config_generation == 1
    assert server.workspace_path == repo
    assert config.a2a is not None and config.a2a.workspace_id == server.workspace_id
    token = repo / ".handoff-logs" / "credentials" / "service-token"
    assert stat.S_IMODE(token.stat().st_mode) == 0o600
    assert stat.S_IMODE(token.parent.stat().st_mode) == 0o700
    assert len(token.read_text().strip()) >= 32
    assert (repo / "HANDOFF.md").is_file()
    lines = _exclude_lines(repo)
    for pattern in EXCLUDES:
        assert pattern in lines
    assert git(repo, "status", "--porcelain").stdout.strip() == ""
    raw = json.loads((repo / ".handoff-logs" / "server.json").read_text())
    assert raw["selection"]["validation"] in {"listed", "unverified"}
    if provider == "cursor":
        assert (repo / ".cursor" / "skills" / "handoff-cli" / "SKILL.md").is_file()
        assert ".cursor/skills/handoff-cli/" in lines
        assert raw["selection"]["validation"] == "listed"
    if provider == "claude":
        assert raw["selection"]["validation"] == "unverified"  # no enumeration in the Claude CLI
        assert (repo / ".claude" / "settings.local.json").is_file()


def test_reinit_preserves_task_token_selection_and_history(tmp_path: Path) -> None:
    repo, env = managed_repo(tmp_path, planner="cursor")
    (repo / "HANDOFF.md").write_text("# my plan\n\n**Status:** DRAFT\n", encoding="utf-8")
    workflow = repo / ".handoff-logs" / "workflow.json"
    workflow.write_text('{"workflow_id": "keep-me"}\n', encoding="utf-8")
    snapshot = {p: p.read_bytes() for p in (repo / "HANDOFF.md", workflow, repo / ".handoff-config.json",
                                            repo / ".handoff-logs" / "server.json",
                                            repo / ".handoff-logs" / "credentials" / "service-token")}
    again = handoff(env, "init", str(repo))
    assert again.returncode == 0, again.stderr
    explicit = handoff(env, "init", str(repo), "--transport", "a2a", "--executor", "cursor", "--model", "fake-model")
    assert explicit.returncode == 0, explicit.stderr
    assert "unchanged" in explicit.stdout
    for path, data in snapshot.items():
        assert path.read_bytes() == data, path
    different = handoff(env, "init", str(repo), "--transport", "a2a", "--executor", "cursor", "--model", "other-model")
    assert different.returncode == 2
    assert "handoff model" in different.stderr
    for path, data in snapshot.items():
        assert path.read_bytes() == data, path


def test_missing_auth_and_unavailable_model_fail_before_any_file(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    ignore_probes(repo)
    env = managed_env(tmp_path, {"FAKE_PROVIDER_AUTH": "missing"})
    no_auth = handoff(env, "init", str(repo), "--transport", "a2a", "--executor", "cursor", "--model", "fake-model")
    assert no_auth.returncode == 1
    assert "cursor-agent login" in no_auth.stderr
    assert "nothing was configured" in no_auth.stderr
    assert not _generated(repo)
    env = managed_env(tmp_path)
    unknown = handoff(env, "init", str(repo), "--transport", "a2a", "--executor", "cursor", "--model", "grok-9-imaginary")
    assert unknown.returncode == 1
    assert "not in cursor-agent models" in unknown.stderr
    assert not _generated(repo)
    missing_bin = handoff({**env, "HANDOFF_CURSOR_BIN": str(tmp_path / "nope")}, "init", str(repo), "--transport", "a2a", "--executor", "cursor", "--model", "fake-model")
    assert missing_bin.returncode == 1 and "HANDOFF_CURSOR_BIN" in missing_bin.stderr
    assert not _generated(repo)


def test_codex_unlisted_model_is_accepted_but_labeled_unverified(tmp_path: Path) -> None:
    repo, _env = managed_repo(tmp_path, provider="codex", model="gpt-5.6-luna")
    raw = json.loads((repo / ".handoff-logs" / "server.json").read_text())
    assert raw["codex"]["model"] == "gpt-5.6-luna"
    assert raw["selection"]["validation"] == "unverified"


def test_bracketed_model_ids_are_data(tmp_path: Path) -> None:
    model = "fake-model[effort=high,fast=false]"
    repo, env = managed_repo(tmp_path, model=model)
    raw = json.loads((repo / ".handoff-logs" / "server.json").read_text())
    assert raw["cursor"]["model"] == model
    effort = handoff(env, "model", str(repo), "--provider", "cursor", "--model", "other-model", "--reasoning-effort", "high")
    assert effort.returncode == 1 and "not supported for cursor" in effort.stderr


def test_linked_worktree_init_uses_git_plumbing(tmp_path: Path) -> None:
    main = make_repo(tmp_path)
    ignore_probes(main)
    linked = tmp_path / "linked wt"
    git(main, "worktree", "add", "-q", "-b", "feature", str(linked))
    assert (linked / ".git").is_file()
    env = managed_env(tmp_path)
    result = handoff(env, "init", str(linked), "--transport", "a2a", "--executor", "cursor", "--model", "fake-model")
    assert result.returncode == 0, result.stderr
    assert (linked / ".handoff-config.json").is_file()
    assert ".handoff-config.json" in _exclude_lines(linked)
    assert git(linked, "status", "--porcelain").stdout.strip() == ""
    legacy = handoff(env, "init", str(linked), "--transport", "legacy")
    assert legacy.returncode == 0, legacy.stderr


def test_existing_manual_config_is_preserved_until_explicit_migration(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    ignore_probes(repo)
    env = managed_env(tmp_path)
    external_token = tmp_path / "external-token"
    external_token.write_text("manual-secret\n", encoding="utf-8")
    manual = {
        "transport": "a2a",
        "max_rounds": 2,
        "a2a": {"agent_card_url": "http://127.0.0.1:9/.well-known/agent-card.json", "workspace_id": "manual-ws", "credential_file": str(external_token)},
    }
    (repo / ".handoff-config.json").write_text(json.dumps(manual) + "\n", encoding="utf-8")
    original = (repo / ".handoff-config.json").read_bytes()
    bare = handoff(env, "init", str(repo))
    assert bare.returncode == 0, bare.stderr
    assert (repo / ".handoff-config.json").read_bytes() == original
    refused = handoff(env, "server", "status", str(repo))
    assert refused.returncode == 2 and "--transport a2a" in refused.stderr
    (repo / ".handoff-logs").mkdir(exist_ok=True)
    (repo / ".handoff-logs" / "outstanding.json").write_text("{}\n", encoding="utf-8")
    blocked = handoff(env, "init", str(repo), "--transport", "a2a", "--executor", "cursor", "--model", "fake-model")
    assert blocked.returncode == 1 and "outstanding" in blocked.stderr
    assert (repo / ".handoff-config.json").read_bytes() == original
    (repo / ".handoff-logs" / "outstanding.json").unlink()
    migrated = handoff(env, "init", str(repo), "--transport", "a2a", "--executor", "cursor", "--model", "fake-model")
    assert migrated.returncode == 0, migrated.stderr
    config = load_config(repo)
    assert config is not None and config.managed is not None
    assert config.a2a is not None and config.a2a.workspace_id == "manual-ws"  # approval receipts still match
    assert config.max_rounds == 2
    backups = list((repo / ".handoff-logs" / "service" / "backups").iterdir())
    assert len(backups) == 1 and backups[0].read_bytes() == original
    assert external_token.read_text() == "manual-secret\n"  # external credential untouched


def test_collisions_refuse_and_roll_back(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    ignore_probes(repo)
    env = managed_env(tmp_path)
    foreign = repo / ".cursor" / "skills" / "handoff-cli"
    foreign.mkdir(parents=True)
    (foreign / "SKILL.md").write_text("---\nname: handoff-cli\ndescription: someone else's\n---\n", encoding="utf-8")
    result = handoff(env, "init", str(repo), "--transport", "a2a", "--executor", "cursor", "--model", "fake-model", "--planner", "cursor")
    assert result.returncode == 1 and "not overwriting" in result.stderr
    assert (foreign / "SKILL.md").read_text().endswith("someone else's\n---\n")
    assert not _generated(repo)
    (repo / "elsewhere").mkdir()
    os.symlink(repo / "elsewhere", repo / ".handoff-logs")
    linked = handoff(env, "init", str(repo), "--transport", "a2a", "--executor", "cursor", "--model", "fake-model")
    assert linked.returncode == 1 and "symlink" in linked.stderr
    assert not list((repo / "elsewhere").iterdir())
    (repo / ".handoff-logs").unlink()
    (repo / ".handoff-config.json").write_text('{"transport": "legacy"}\n', encoding="utf-8")
    git(repo, "add", "-f", ".handoff-config.json")
    tracked = handoff(env, "init", str(repo), "--transport", "a2a", "--executor", "cursor", "--model", "fake-model")
    assert tracked.returncode != 0 and "tracked" in tracked.stderr


def test_legacy_init_and_draft_status_are_python_free(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    (repo / "HANDOFF.md").unlink()
    env = os.environ.copy()
    env["PATH"] = "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"
    env["HANDOFF_A2A_BIN"] = str(tmp_path / "no-a2a")
    result = handoff(env, "init", str(repo), "--transport", "legacy")
    assert result.returncode == 0, result.stderr
    assert (repo / "HANDOFF.md").is_file()
    text = (repo / "HANDOFF.md").read_text().replace("**Status:** NO TASK", "**Status:** DRAFT")
    (repo / "HANDOFF.md").write_text(text, encoding="utf-8")
    status = handoff(env, "status", str(repo))
    assert status.returncode == 0, status.stderr
    assert field(status.stdout, "turn") == "HUMAN"
    assert "handoff approve" in field(status.stdout, "next")
    assert not (repo / ".handoff-config.json").exists()


def test_models_lists_account_models_and_reports_missing_enumeration(tmp_path: Path) -> None:
    env = managed_env(tmp_path)
    cursor = handoff(env, "models", str(tmp_path), "--provider", "cursor")
    assert cursor.returncode == 0, cursor.stderr
    assert "grok-4.7-high" in cursor.stdout and "cursor-agent models" in cursor.stdout
    claude = handoff(env, "models", str(tmp_path), "--provider", "claude")
    assert claude.returncode == 0 and "enumeration unavailable" in claude.stdout
    denied = handoff({**env, "FAKE_PROVIDER_AUTH": "missing"}, "models", "--provider", "cursor")
    assert denied.returncode == 1 and "cursor-agent login" in denied.stderr


def test_tty_guided_init_asks_only_real_choices(tmp_path: Path) -> None:
    """Drive the Python prompts directly (the Bash TTY check needs a real terminal)."""
    repo = make_repo(tmp_path)
    ignore_probes(repo)
    (repo / "HANDOFF.md").unlink()
    env = managed_env(tmp_path)
    import sys

    proc = subprocess.run(
        [sys.executable, "-m", "handoff_a2a", "setup", str(repo), "--interactive"],
        input="cursor\n2\ncursor\n",
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "Models from cursor-agent models" in proc.stdout
    raw = json.loads((repo / ".handoff-logs" / "server.json").read_text())
    assert raw["cursor"]["model"] == "other-model"  # list entry 2, chosen explicitly
    assert (repo / ".cursor" / "skills" / "handoff-cli").is_dir()
