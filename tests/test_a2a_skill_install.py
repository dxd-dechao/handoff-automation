"""`handoff skill install|status` and `init --planner <host>`: one installer, safe upgrades."""

from __future__ import annotations

import errno
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from a2a_harness import git, handoff, ignore_probes, make_repo, managed_env, managed_repo, write_handoff
from handoff_a2a import skills
from handoff_a2a.skills import (
    KNOWN_RELEASE_DIGESTS,
    MARKER_NAME,
    PROJECT_DIRS,
    REPO_SKILL,
    any_skill_available,
    project_location,
    skill_state,
    tree_digest,
    user_location,
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


def test_stale_copies_are_marked_and_left_unchanged(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    env = _env(tmp_path)
    home = Path(env["HOME"])
    copy = home / ".cursor" / "skills" / "_handoff-cli__skills__handoff-cli"
    copy.mkdir(parents=True)
    (copy / "SKILL.md").write_text("---\nname: handoff-cli\ndescription: skillshare\n---\nold body\n", encoding="utf-8")
    before = tree_digest(copy)
    status = handoff(env, "skill", "status", str(repo), "--json")
    assert status.returncode == 0, status.stderr
    cursor = next(item for item in _json(status)["locations"] if item["host"] == "cursor" and item["scope"] == "user")
    assert cursor["state"] == "elsewhere" and cursor["current_content"] is False and cursor["stale"] is True
    assert cursor["found_path"] == str(copy)
    text = handoff(env, "skill", "status", str(repo))
    assert "stale (content differs from this checkout's skills/handoff-cli)" in text.stdout
    assert "update it with the tool that installed it (e.g. skillshare); handoff never modifies it" in text.stdout
    assert tree_digest(copy) == before

    current = home / ".agents" / "skills" / "renamed-handoff"
    shutil.copytree(REPO_SKILL, current)
    current_before = tree_digest(current)
    again = _json(handoff(env, "skill", "status", str(repo), "--json"))
    codex = next(item for item in again["locations"] if item["host"] == "codex" and item["scope"] == "user")
    assert codex["state"] == "elsewhere" and codex["current_content"] is True and codex["stale"] is False
    assert tree_digest(current) == current_before and tree_digest(copy) == before

    assert handoff(env, "skill", "install", str(repo), "--host", "claude").returncode == 0
    target = repo / PROJECT_DIRS["claude"]
    (target / "SKILL.md").write_text("---\nname: handoff-cli\ndescription: old\n---\nold release\n", encoding="utf-8")
    marker = json.loads((target / MARKER_NAME).read_text())
    marker["digest"] = tree_digest(target)
    (target / MARKER_NAME).write_text(json.dumps(marker), encoding="utf-8")
    outdated_before = tree_digest(target)
    listed = _json(handoff(env, "skill", "status", str(repo), "--json"))
    project = next(item for item in listed["locations"] if item["host"] == "claude" and item["scope"] == "project")
    assert project["state"] == "outdated" and project["stale"] is True
    assert tree_digest(target) == outdated_before and tree_digest(copy) == before


def test_init_names_a_stale_elsewhere_copy_and_installs_nothing(tmp_path: Path) -> None:
    repo, env = managed_repo(tmp_path)
    home = Path(env["HOME"])
    copy = home / ".claude" / "skills" / "_handoff-cli__skills__handoff-cli"
    copy.mkdir(parents=True)
    (copy / "SKILL.md").write_text("---\nname: handoff-cli\ndescription: skillshare\n---\nstale\n", encoding="utf-8")
    before = tree_digest(copy)
    init = handoff(env, "init", str(repo), "--planner", "claude")
    assert init.returncode == 0, init.stderr
    assert str(copy) in init.stdout
    assert "update it with the tool that installed it (e.g. skillshare); handoff never modifies it" in init.stdout
    assert "handoff skill install" not in init.stdout
    assert not (repo / ".claude" / "skills" / "handoff-cli").exists()
    assert tree_digest(copy) == before


def _deny_root() -> None:
    if os.geteuid() == 0:
        pytest.skip("chmod 000 does not deny access when running as root")


def _skill_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("skill:")]


def test_unreadable_skill_root_keeps_status_and_skill_status(tmp_path: Path) -> None:
    _deny_root()
    repo, env = managed_repo(tmp_path)
    write_handoff(repo, status="DRAFT", branch="main")
    before = handoff(env, "status", str(repo))
    before_json = _json(handoff(env, "status", str(repo), "--json"))
    assert before.returncode == 0 and before_json["planner_skill"] == {"stale": [], "unknown": []}
    locked = Path(env["HOME"]) / ".cursor" / "skills"
    locked.mkdir(parents=True)
    locked.chmod(0)
    try:
        status = handoff(env, "status", str(repo), "--json")
        human = handoff(env, "status", str(repo))
        skill = handoff(env, "skill", "status", str(repo), "--json")
        skill_text = handoff(env, "skill", "status", str(repo))
    finally:
        locked.chmod(0o755)
    assert status.returncode == before.returncode == 0
    data = _json(status)
    unreadable = str(locked / "handoff-cli")
    assert data["planner_skill"]["unknown"] == [unreadable]
    assert data["planner_skill"]["stale"] == []
    for key in ("workflow", "execution", "next", "turn", "status", "preflight"):
        assert data[key] == before_json[key]
    assert _skill_lines(human.stdout) == [
        f"skill:  could not read {unreadable} (permission denied); stale check skipped"
    ]
    rest = [line for line in human.stdout.splitlines() if not line.startswith("skill:")]
    assert rest == before.stdout.splitlines()
    assert human.returncode == 0
    assert skill.returncode == 0 and skill_text.returncode == 0
    listed = _json(skill)
    cursor = next(item for item in listed["locations"] if item["host"] == "cursor" and item["scope"] == "user")
    assert cursor["state"] == "unknown" and cursor["probe"] == "not_permitted"
    assert cursor["stale"] is False and cursor["unknown"] is True
    assert unreadable in skill_text.stdout and "unknown (" in skill_text.stdout
    others = [item for item in listed["locations"] if item is not cursor]
    assert others and all(item["state"] != "unknown" and item["unknown"] is False and "probe" not in item for item in others)


def test_unreadable_root_is_not_a_copy_and_init_still_suggests_install(tmp_path: Path) -> None:
    _deny_root()
    repo, env = managed_repo(tmp_path)
    write_handoff(repo, status="DRAFT", branch="main")
    home = Path(env["HOME"])
    copy = home / ".claude" / "skills" / "_handoff-cli__skills__handoff-cli"
    copy.mkdir(parents=True)
    (copy / "SKILL.md").write_text("---\nname: handoff-cli\ndescription: skillshare\n---\nstale\n", encoding="utf-8")
    locked = home / ".cursor" / "skills"
    locked.mkdir(parents=True)
    locked.chmod(0)
    try:
        shown = _json(handoff(env, "status", str(repo), "--json"))
        assert any_skill_available(repo, env) == str(copy)
    finally:
        locked.chmod(0o755)
    assert shown["planner_skill"]["stale"] == [str(copy)]
    assert shown["planner_skill"]["unknown"] == [str(locked / "handoff-cli")]

    fresh_root = tmp_path / "fresh"
    fresh_root.mkdir()
    fresh = make_repo(fresh_root)
    ignore_probes(fresh)
    none_env = _env(fresh_root)
    none_locked = Path(none_env["HOME"]) / ".cursor" / "skills"
    none_locked.mkdir(parents=True)
    none_locked.chmod(0)
    try:
        assert any_skill_available(fresh, none_env) is None
        init = handoff(
            none_env,
            "init",
            str(fresh),
            "--transport",
            "a2a",
            "--executor",
            "cursor",
            "--model",
            "fake-model",
        )
    finally:
        none_locked.chmod(0o755)
    assert init.returncode == 0, init.stderr
    assert "make the Planner skill available: handoff skill install" in init.stdout
    assert f"could not read {none_locked / 'handoff-cli'}; the skill check skipped it" in init.stdout


def test_install_refuses_an_unreadable_target_and_init_warns(tmp_path: Path) -> None:
    _deny_root()
    repo = make_repo(tmp_path)
    ignore_probes(repo)
    env = _env(tmp_path)
    locked = Path(env["HOME"]) / ".cursor" / "skills"
    locked.mkdir(parents=True)
    before = git(repo, "status", "--porcelain").stdout
    locked.chmod(0)
    try:
        refused = handoff(env, "skill", "install", "--host", "cursor", "--user")
        refused_json = handoff(env, "skill", "install", "--host", "cursor", "--user", "--json")
    finally:
        locked.chmod(0o755)
    assert refused.returncode == 2
    assert f"cannot read {locked / 'handoff-cli'} (Permission denied); not installing" in refused.stderr
    assert refused_json.returncode == 2
    assert "not installing" in _json(refused_json)["error"]
    assert not (locked / "handoff-cli").exists()
    assert git(repo, "status", "--porcelain").stdout == before

    project = repo / ".cursor" / "skills"
    project.mkdir(parents=True)
    project.chmod(0)
    try:
        init = handoff(
            env,
            "init",
            str(repo),
            "--transport",
            "a2a",
            "--executor",
            "cursor",
            "--model",
            "fake-model",
            "--planner",
            "cursor",
        )
    finally:
        project.chmod(0o755)
    assert init.returncode == 0, init.stderr
    assert "warning:" in init.stdout
    assert f"cannot read {project / 'handoff-cli'} (Permission denied); not installing" in init.stdout
    assert f"could not read {project / 'handoff-cli'}; the skill check skipped it" in init.stdout
    assert list(project.iterdir()) == []


def test_non_permission_oserror_is_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = make_repo(tmp_path)
    env = _env(tmp_path)
    loc = project_location(repo, "cursor")

    def boom(_loc: object) -> tuple[str, str]:
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(skills, "_classify", boom)
    state, detail = skill_state(loc)
    assert state == "unknown" and "Input/output error" in detail
    entry = skills._entry(loc)
    assert entry["probe"] == "error" and entry["stale"] is False and entry["unknown"] is True
    entries = skills.status_entries(repo, env)
    assert entries
    assert all(item["state"] == "unknown" and item["probe"] == "error" for item in entries)


def test_unreadable_child_is_skipped_and_unlistable_root_is_unknown(tmp_path: Path) -> None:
    _deny_root()
    env = _env(tmp_path)
    home = Path(env["HOME"])
    root = home / ".claude" / "skills"
    locked = root / "locked"
    locked.mkdir(parents=True)
    copy = root / "renamed-handoff"
    copy.mkdir()
    (copy / "SKILL.md").write_text("---\nname: handoff-cli\ndescription: skillshare\n---\nbody\n", encoding="utf-8")
    locked.chmod(0)
    try:
        found = skills.elsewhere_copy(user_location("claude", env))
    finally:
        locked.chmod(0o755)
    assert found is not None and found["path"] == str(copy) and not found.get("unreadable")

    codex = home / ".agents" / "skills"
    codex.mkdir(parents=True)
    codex.chmod(0o111)
    try:
        loc = user_location("codex", env)
        assert skill_state(loc)[0] == "absent"
        entry = skills._entry(loc)
    finally:
        codex.chmod(0o755)
    assert entry["state"] == "unknown" and entry["path"] == str(codex)
    assert entry["probe"] == "not_permitted" and entry["stale"] is False and entry["unknown"] is True


def test_skill_command_needs_the_runtime(tmp_path: Path) -> None:
    env = os.environ.copy()
    env["HANDOFF_A2A_BIN"] = str(tmp_path / "missing")
    result = handoff(env, "skill", "status", str(tmp_path))
    assert result.returncode == 1 and "uv sync" in result.stderr
