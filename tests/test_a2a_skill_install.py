"""`handoff skill install|status` and `init --planner <host>`: one installer, safe upgrades."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from a2a_harness import git, handoff, make_repo, managed_env, managed_repo
from handoff_a2a import skills
from handoff_a2a.skills import (
    KNOWN_RELEASE_DIGESTS,
    MARKER_NAME,
    PROJECT_DIRS,
    REPO_SKILL,
    project_location,
    skill_state,
    tree_digest,
)
from handoff_a2a.workspace import is_workflow_path

HOSTS = ("cursor", "codex", "claude")
USER_DIRS = {"cursor": ".cursor/skills", "codex": ".agents/skills", "claude": ".claude/skills"}


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = managed_env(tmp_path, {"HOME": str(home)})
    env.pop("CLAUDE_CONFIG_DIR", None)
    env.update(extra)
    return env


def _excludes(repo: Path) -> list[str]:
    path = git(repo, "rev-parse", "--path-format=absolute", "--git-path", "info/exclude").stdout.strip()
    return Path(path).read_text(encoding="utf-8").splitlines() if Path(path).is_file() else []


def _json(result: subprocess.CompletedProcess[str]) -> dict:
    data = json.loads(result.stdout)
    assert data["schema"] == "urn:handoff-automation:cli-output:v1"
    return data


def _states(env: dict[str, str], repo: Path) -> dict[tuple[str, str], str]:
    data = _json(handoff(env, "skill", "status", str(repo), "--json"))
    assert data["kind"] == "skill-status"
    return {(e["scope"], e["host"]): e["state"] for e in data["locations"]}


@pytest.mark.parametrize("host", HOSTS)
def test_project_install_is_marked_excluded_idempotent_and_outside_fingerprints(tmp_path: Path, host: str) -> None:
    repo = make_repo(tmp_path)
    env = _env(tmp_path)
    first = handoff(env, "skill", "install", str(repo), "--host", host)
    assert first.returncode == 0, first.stderr
    target = repo / PROJECT_DIRS[host]
    assert (target / "SKILL.md").read_bytes() == (REPO_SKILL / "SKILL.md").read_bytes()
    marker = json.loads((target / MARKER_NAME).read_text())
    assert marker["digest"] == tree_digest(REPO_SKILL) and marker["host"] == host and marker["scope"] == "project"
    assert f"{PROJECT_DIRS[host]}/" in _excludes(repo)
    assert is_workflow_path(f"{PROJECT_DIRS[host]}/SKILL.md")
    assert is_workflow_path(f"{PROJECT_DIRS[host]}/{MARKER_NAME}")
    assert git(repo, "status", "--porcelain", "--", PROJECT_DIRS[host].split("/")[0]).stdout.strip() == ""
    again = handoff(env, "skill", "install", str(repo), "--host", host, "--json")
    assert again.returncode == 0 and _json(again)["outcome"] == "already current"
    assert _excludes(repo).count(f"{PROJECT_DIRS[host]}/") == 1
    assert _states(env, repo)[("project", host)] == "current"
    assert not (tmp_path / "home" / USER_DIRS[host]).exists()  # project install touches no user folder


def test_outdated_copies_upgrade_and_foreign_copies_are_never_overwritten(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    env = _env(tmp_path)
    assert handoff(env, "skill", "install", str(repo), "--host", "cursor").returncode == 0
    target = repo / PROJECT_DIRS["cursor"]
    # An unmodified copy of an older release: content differs from the source,
    # but the tree still matches the digest recorded when it was installed.
    (target / "SKILL.md").write_text("---\nname: handoff-cli\ndescription: old\n---\nold release\n", encoding="utf-8")
    marker = json.loads((target / MARKER_NAME).read_text())
    marker["digest"] = tree_digest(target)
    (target / MARKER_NAME).write_text(json.dumps(marker), encoding="utf-8")
    assert _states(env, repo)[("project", "cursor")] == "outdated"
    status = handoff(env, "skill", "status", str(repo))
    assert f'handoff skill install "{repo}" --host cursor' in status.stdout
    upgraded = handoff(env, "skill", "install", str(repo), "--host", "cursor", "--json")
    assert _json(upgraded)["outcome"] == "upgraded"
    assert (target / "SKILL.md").read_bytes() == (REPO_SKILL / "SKILL.md").read_bytes()
    assert not list(target.parent.glob(".handoff-cli.*"))  # no staging leftovers

    # User-modified after install: foreign, untouched, with a way forward.
    (target / "SKILL.md").write_text("my local edits\n", encoding="utf-8")
    refused = handoff(env, "skill", "install", str(repo), "--host", "cursor")
    assert refused.returncode == 2
    assert "modified since handoff installed it" in refused.stderr and "move it aside" in refused.stderr
    assert (target / "SKILL.md").read_text() == "my local edits\n"
    refused_json = handoff(env, "skill", "install", str(repo), "--host", "cursor", "--json")
    assert refused_json.returncode == 2 and "error" in _json(refused_json)

    # Unrelated skill with the same name (no marker).
    other = repo / PROJECT_DIRS["codex"]
    other.mkdir(parents=True)
    (other / "SKILL.md").write_text("---\nname: handoff-cli\ndescription: someone else's\n---\n", encoding="utf-8")
    assert handoff(env, "skill", "install", str(repo), "--host", "codex").returncode == 2
    assert "someone else's" in (other / "SKILL.md").read_text()
    states = _states(env, repo)
    assert states[("project", "cursor")] == "foreign" and states[("project", "codex")] == "foreign"
    assert states[("project", "claude")] == "absent"


def test_symlinked_and_tracked_copies_are_foreign(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    env = _env(tmp_path)
    (repo / ".claude" / "skills").mkdir(parents=True)
    (repo / ".claude" / "skills" / "handoff-cli").symlink_to(REPO_SKILL)
    assert handoff(env, "skill", "install", str(repo), "--host", "claude").returncode == 2
    assert (repo / ".claude" / "skills" / "handoff-cli").is_symlink()
    tracked = repo / PROJECT_DIRS["codex"]
    tracked.mkdir(parents=True)
    (tracked / "SKILL.md").write_bytes((REPO_SKILL / "SKILL.md").read_bytes())
    git(repo, "add", "-f", str(tracked / "SKILL.md"))
    git(repo, "commit", "-qm", "team-owned skill")
    assert skill_state(project_location(repo, "codex"))[0] == "foreign"
    assert handoff(env, "skill", "install", str(repo), "--host", "codex").returncode == 2


def test_unmarked_known_release_copy_is_outdated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = make_repo(tmp_path)
    target = repo / PROJECT_DIRS["cursor"]
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("an earlier release\n", encoding="utf-8")
    loc = project_location(repo, "cursor")
    assert skill_state(loc)[0] == "foreign"
    monkeypatch.setitem(KNOWN_RELEASE_DIGESTS, tree_digest(target), "test release")
    assert skill_state(loc) == ("outdated", "unmodified copy of the test release release")
    assert skills.install(loc) == "upgraded"
    assert skill_state(loc) == ("current", "")


def test_a6_release_digest_matches_the_released_skill() -> None:
    """The recorded A6 digest is the SKILL tree at c9f97b9 (checked when history is available)."""
    root = Path(__file__).resolve().parents[1]
    shown = subprocess.run(
        ["git", "-C", str(root), "show", "c9f97b9:skills/handoff-cli/SKILL.md"], capture_output=True
    )
    if shown.returncode != 0:
        pytest.skip("A6 commit not in this checkout's history")
    import hashlib

    digest = hashlib.sha256(f"SKILL.md\0{hashlib.sha256(shown.stdout).hexdigest()}\n".encode()).hexdigest()
    assert KNOWN_RELEASE_DIGESTS.get(digest) == "A6 (c9f97b9)"


@pytest.mark.parametrize("host", HOSTS)
def test_user_install_writes_only_the_user_folder(tmp_path: Path, host: str) -> None:
    repo = make_repo(tmp_path)
    env = _env(tmp_path)
    before = sorted(p.name for p in repo.iterdir())
    excludes = _excludes(repo)
    result = subprocess.run(
        [str(Path(__file__).resolve().parents[1] / "bin" / "handoff"), "skill", "install", "--host", host, "--user"],
        cwd=repo, env=env, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    installed = tmp_path / "home" / USER_DIRS[host] / "handoff-cli"
    assert (installed / "SKILL.md").is_file() and (installed / MARKER_NAME).is_file()
    assert sorted(p.name for p in repo.iterdir()) == before and _excludes(repo) == excludes
    assert _states(env, repo)[("user", host)] == "current"
    named = handoff(env, "skill", "install", str(repo), "--host", host, "--user")
    assert named.returncode == 2  # --user never takes a repository


def test_claude_user_install_refuses_an_unverified_config_dir(tmp_path: Path) -> None:
    env = _env(tmp_path, CLAUDE_CONFIG_DIR=str(tmp_path / "elsewhere"))
    result = handoff(env, "skill", "install", "--host", "claude", "--user")
    assert result.returncode == 2 and "CLAUDE_CONFIG_DIR" in result.stderr
    assert not (tmp_path / "home" / ".claude").exists()
    states = _states(env, make_repo(tmp_path))
    assert states[("user", "claude")] == "unsupported"


@pytest.mark.parametrize("host", ("codex", "claude"))
def test_init_planner_installs_for_each_host_and_upgrades_on_reinit(tmp_path: Path, host: str) -> None:
    repo, env = managed_repo(tmp_path, planner=host)
    target = repo / PROJECT_DIRS[host]
    assert skill_state(project_location(repo, host))[0] == "current"
    assert f"{PROJECT_DIRS[host]}/" in _excludes(repo)
    assert json.loads((repo / ".handoff-config.json").read_text())["managed"]["planner"] == {"host": host}
    (target / "SKILL.md").write_text("older\n", encoding="utf-8")
    marker = json.loads((target / MARKER_NAME).read_text())
    marker["digest"] = tree_digest(target)
    (target / MARKER_NAME).write_text(json.dumps(marker), encoding="utf-8")
    reinit = handoff(env, "init", str(repo), "--planner", host)
    assert reinit.returncode == 0, reinit.stderr
    assert "(upgraded)" in reinit.stdout
    assert skill_state(project_location(repo, host))[0] == "current"
    assert git(repo, "status", "--porcelain").stdout.strip() == ""


def test_planner_launcher_names_the_upgrade_for_an_outdated_copy(tmp_path: Path) -> None:
    repo, env = managed_repo(tmp_path, planner="cursor")
    target = repo / PROJECT_DIRS["cursor"]
    (target / "SKILL.md").write_text("older\n", encoding="utf-8")
    marker = json.loads((target / MARKER_NAME).read_text())
    marker["digest"] = tree_digest(target)
    (target / MARKER_NAME).write_text(json.dumps(marker), encoding="utf-8")
    ok = handoff(env, "planner", str(repo), "--provider", "cursor", "--model", "fake-model", "--print-command")
    assert ok.returncode == 0, ok.stderr
    assert "older release" in ok.stdout and "handoff skill install" in ok.stdout


def test_skill_command_needs_the_runtime(tmp_path: Path) -> None:
    env = os.environ.copy()
    env["HANDOFF_A2A_BIN"] = str(tmp_path / "missing")
    result = handoff(env, "skill", "status", str(tmp_path))
    assert result.returncode == 1 and "uv sync" in result.stderr
