"""Planner skill installation: `handoff skill install|status` and `init --planner`.

One installer for every Planner host. A copy at the target path is replaced
only when it is provably this project's skill and unmodified:

  absent    nothing at the path
  current   identical to this installation's skill
  outdated  an unmodified copy of an earlier release: its tree still matches
            the digest in its install marker, or it is an unmarked copy of a
            known earlier release. Upgraded in place on install / re-init.
  foreign   anything else (user-modified, unknown unmarked content, a
            symlink, or tracked in git). Never overwritten.

Project locations (the host's documented project skill folder):

  cursor  .cursor/skills/handoff-cli    https://cursor.com/docs/skills
  codex   .agents/skills/handoff-cli    verified with `codex debug prompt-input` (A4)
  claude  .claude/skills/handoff-cli    https://code.claude.com/docs/en/skills

User locations (`--user`, only when explicitly requested) are the host's
documented user folder under $HOME. Claude Code documents ~/.claude/skills;
whether a relocated CLAUDE_CONFIG_DIR reads another folder is not documented,
so `--user --host claude` is refused while CLAUDE_CONFIG_DIR points elsewhere.
A user install writes nothing into any repository.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

HOSTS = ("cursor", "codex", "claude")
SKILL_NAME = "handoff-cli"
PROJECT_DIRS = {
    "cursor": ".cursor/skills/handoff-cli",
    "codex": ".agents/skills/handoff-cli",
    "claude": ".claude/skills/handoff-cli",
}
USER_DIRS = {
    "cursor": ".cursor/skills",
    "codex": ".agents/skills",
    "claude": ".claude/skills",
}
MARKER_NAME = ".handoff-skill.json"
MARKER_SCHEMA = "urn:handoff-automation:planner-skill:v1"
REPO_SKILL = Path(__file__).resolve().parents[2] / "skills" / SKILL_NAME
# Tree digests (see tree_digest) of earlier released copies that were
# installed without a marker. Recorded here, never computed from git.
KNOWN_RELEASE_DIGESTS = {
    "8890122edca4d7d792309b5a620c2be20fa47c3373555d6329feedd441f56929": "A6 (c9f97b9)",
}


class SkillError(Exception):
    def __init__(self, message: str, *, exit_code: int = 1):
        super().__init__(message)
        self.exit_code = exit_code


@dataclass(frozen=True)
class Location:
    host: str
    scope: str  # "project" | "user"
    path: Path
    repo: Path | None = None  # project installs only

    @property
    def rel(self) -> str:
        return PROJECT_DIRS[self.host] if self.scope == "project" else str(self.path)

    @property
    def display(self) -> str:
        return PROJECT_DIRS[self.host] if self.scope == "project" else str(self.path)


def project_location(repo: Path, host: str) -> Location:
    require_host(host)
    return Location(host, "project", repo / PROJECT_DIRS[host], repo)


def user_location(host: str, env: Mapping[str, str] | None = None) -> Location:
    require_host(host)
    env = os.environ if env is None else env
    home = Path(env.get("HOME") or Path.home())
    if host == "claude":
        configured = env.get("CLAUDE_CONFIG_DIR")
        if configured and Path(configured).expanduser().resolve() != (home / ".claude").resolve():
            raise SkillError(
                "CLAUDE_CONFIG_DIR is set to another folder; Claude Code documents user skills in "
                "~/.claude/skills and it is not verified which folder this configuration reads. "
                'Install per project instead: handoff skill install "<repo>" --host claude',
                exit_code=2,
            )
    return Location(host, "user", home / USER_DIRS[host] / SKILL_NAME)


def require_host(host: str) -> str:
    if host not in HOSTS:
        raise SkillError(f"unknown Planner host {host!r} (choose {', '.join(HOSTS)})", exit_code=2)
    return host


# ── state ───────────────────────────────────────────────────────────────────


def tree_digest(root: Path) -> str:
    """Digest of file paths and contents, excluding the install marker."""
    entries = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if rel == MARKER_NAME:
            continue
        entries.append((rel, hashlib.sha256(path.read_bytes()).hexdigest()))
    digest = hashlib.sha256()
    for rel, file_digest in sorted(entries):
        digest.update(f"{rel}\0{file_digest}\n".encode("utf-8"))
    return digest.hexdigest()


def source_digest() -> str:
    return tree_digest(REPO_SKILL)


def _tracked(repo: Path, rel: str) -> bool:
    out = subprocess.run(["git", "-C", str(repo), "ls-files", "--", rel], check=False, capture_output=True, text=True)
    return bool(out.stdout.strip())


def _symlinked_ancestor(loc: Location) -> Path | None:
    """A symlink between the repository (or $HOME) and the skill folder."""
    base = loc.repo if loc.repo is not None else Path.home()
    try:
        parts = loc.path.parent.relative_to(base).parts
    except ValueError:
        return None
    probe = base
    for part in parts:
        probe = probe / part
        if probe.is_symlink():
            return probe
    return None


def read_marker(path: Path) -> dict[str, Any] | None | bool:
    """The install marker; None when absent, False when unreadable."""
    marker = path / MARKER_NAME
    if not marker.exists():
        return None
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(data, dict) or data.get("schema") != MARKER_SCHEMA or not isinstance(data.get("digest"), str):
        return False
    return data


def _frontmatter_name(skill_md: Path) -> str | None:
    try:
        text = skill_md.read_text(encoding="utf-8")
    except OSError:
        return None
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end < 0:
        return None
    for line in text[3:end].splitlines():
        if line.startswith("name:"):
            return line.split(":", 1)[1].strip().strip("\"'")
    return None


def elsewhere_copy(loc: Location) -> dict[str, Any] | None:
    """A handoff-cli skill under another folder name in this host's skill root."""
    root = loc.path.parent
    if not root.is_dir():
        return None
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        try:
            if child.resolve() == loc.path.resolve():
                continue
        except OSError:
            continue
        skill_md = child / "SKILL.md"
        if not skill_md.is_file() or _frontmatter_name(skill_md) != SKILL_NAME:
            continue
        try:
            current = tree_digest(child) == source_digest()
        except OSError:
            current = False
        return {"path": str(child), "current_content": current}
    return None


def skill_available(repo: Path | None, host: str, env: Mapping[str, str] | None = None) -> str | None:
    """Path of a current, outdated, or elsewhere copy for this host, if any."""
    locations: list[Location] = []
    if repo is not None:
        locations.append(project_location(repo, host))
    try:
        locations.append(user_location(host, env))
    except SkillError:
        pass
    for loc in locations:
        state, _detail = skill_state(loc)
        if state in {"current", "outdated"}:
            return str(loc.path)
        if state == "absent":
            found = elsewhere_copy(loc)
            if found:
                return str(found["path"])
    return None


def any_skill_available(repo: Path, env: Mapping[str, str] | None = None) -> str | None:
    for host in HOSTS:
        found = skill_available(repo, host, env)
        if found:
            return found
    return None


def skill_state(loc: Location) -> tuple[str, str]:
    """(state, detail) for one location; see the module docstring."""
    path = loc.path
    if path.is_symlink():
        return "foreign", "a symlink (managed outside handoff)"
    linked = _symlinked_ancestor(loc)
    if linked is not None:
        return "foreign", f"{linked} is a symlink"
    if not path.exists():
        return "absent", ""
    if not path.is_dir():
        return "foreign", "not a directory"
    if loc.repo is not None and _tracked(loc.repo, PROJECT_DIRS[loc.host]):
        return "foreign", "tracked in git (the project owns it)"
    if any(p.is_symlink() for p in path.rglob("*")):
        return "foreign", "contains symlinks"
    digest = tree_digest(path)
    source = source_digest()
    marker = read_marker(path)
    if marker is False:
        return "foreign", f"unreadable {MARKER_NAME}"
    if marker:
        if marker["digest"] != digest:
            return "foreign", "modified since handoff installed it"
        if digest == source:
            return "current", ""
        return "outdated", "unmodified copy of an earlier release"
    if digest == source:
        return "current", "no install marker"
    if digest in KNOWN_RELEASE_DIGESTS:
        return "outdated", f"unmodified copy of the {KNOWN_RELEASE_DIGESTS[digest]} release"
    return "foreign", "not installed by handoff (different content, no install marker)"


# ── install ─────────────────────────────────────────────────────────────────


def _package_version() -> str:
    try:
        from importlib.metadata import version

        return version("handoff-automation")
    except Exception:  # noqa: BLE001 — informational only
        return "unknown"


def _write_marker(loc: Location, digest: str) -> None:
    payload = {
        "schema": MARKER_SCHEMA,
        "skill": SKILL_NAME,
        "version": _package_version(),
        "digest": digest,
        "host": loc.host,
        "scope": loc.scope,
        "installed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    (loc.path / MARKER_NAME).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def install_command(loc: Location) -> str:
    if loc.scope == "user":
        return f"handoff skill install --host {loc.host} --user"
    return f'handoff skill install "{loc.repo}" --host {loc.host}'


def install(loc: Location, *, journal: Any = None) -> str:
    """Install or upgrade; returns "installed", "upgraded", or "already current".

    `journal` (setup's rollback journal) records a fresh copy so a failed
    `handoff init` removes it. An upgrade replaces an unmodified older copy
    and is not rolled back.
    """
    state, detail = skill_state(loc)
    if state == "foreign":
        raise SkillError(
            f"{loc.display} already exists and is not an unmodified handoff install ({detail}); "
            "it was not overwritten. Keep it, or move it aside and run: " + install_command(loc),
            exit_code=2,
        )
    source = source_digest()
    if state == "current":
        if not read_marker(loc.path):
            _write_marker(loc, source)  # adopt an identical unmarked copy
        return "already current"
    if state == "outdated":
        staging = loc.path.with_name(f".{SKILL_NAME}.new-{os.getpid()}")
        retired = loc.path.with_name(f".{SKILL_NAME}.old-{os.getpid()}")
        shutil.rmtree(staging, ignore_errors=True)
        shutil.copytree(REPO_SKILL, staging)
        _write_marker(Location(loc.host, loc.scope, staging, loc.repo), source)
        loc.path.rename(retired)
        staging.rename(loc.path)
        shutil.rmtree(retired, ignore_errors=True)
        return "upgraded"
    if journal is not None:
        journal.mkdir(loc.path.parent)
        journal.before_write(loc.path)
    else:
        loc.path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(REPO_SKILL, loc.path)
    _write_marker(loc, source)
    return "installed"


def exclude_pattern(host: str) -> str:
    return f"{PROJECT_DIRS[host]}/"


def _exclude_file(repo: Path) -> Path:
    out = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--path-format=absolute", "--git-path", "info/exclude"],
        check=False,
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        raise SkillError(f"cannot locate the git exclude file for {repo}")
    return Path(out.stdout.strip())


def ensure_exclude(repo: Path, pattern: str) -> bool:
    path = _exclude_file(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    if pattern in existing.splitlines():
        return False
    text = existing if not existing or existing.endswith("\n") else existing + "\n"
    path.write_text(text + pattern + "\n", encoding="utf-8")
    return True


def resolve_repo(path: Path) -> Path:
    if not path.is_dir():
        raise SkillError(f"no such directory: {path}")
    out = subprocess.run(["git", "-C", str(path), "rev-parse", "--show-toplevel"], check=False, capture_output=True, text=True)
    if out.returncode != 0:
        raise SkillError(f"not a git repository: {path} (project installs need Git for the local exclude)")
    return Path(out.stdout.strip()).resolve()


# ── `handoff skill` ─────────────────────────────────────────────────────────


STALE_DIFFERS = "stale (content differs from this checkout's skills/handoff-cli)"
ELSEWHERE_UPDATE = "update it with the tool that installed it (e.g. skillshare); handoff never modifies it"


def _with_stale(entry: dict[str, Any]) -> dict[str, Any]:
    """``stale`` is derived: outdated, or an elsewhere copy whose content differs."""
    entry["stale"] = entry.get("state") == "outdated" or (
        entry.get("state") == "elsewhere" and entry.get("current_content") is False
    )
    return entry


def _entry(loc: Location) -> dict[str, Any]:
    state, detail = skill_state(loc)
    entry: dict[str, Any] = {"host": loc.host, "scope": loc.scope, "path": str(loc.path), "state": state, "detail": detail}
    if state == "absent":
        found = elsewhere_copy(loc)
        if found:
            entry["state"] = "elsewhere"
            entry["found_path"] = found["path"]
            entry["current_content"] = found["current_content"]
            entry["detail"] = found["path"]
            return _with_stale(entry)
    if state in {"absent", "outdated"}:
        entry["install_command"] = install_command(loc)
    return _with_stale(entry)


def status_entries(repo: Path | None, env: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
    entries = [_entry(project_location(repo, host)) for host in HOSTS] if repo is not None else []
    for host in HOSTS:
        try:
            entries.append(_entry(user_location(host, env)))
        except SkillError as exc:
            entries.append(
                _with_stale({"host": host, "scope": "user", "path": None, "state": "unsupported", "detail": str(exc)})
            )
    return entries


def _copy_path(entry: dict[str, Any]) -> str | None:
    path = entry.get("found_path") or entry.get("path")
    return str(path) if path else None


def planner_skill_report(repo: Path | None, env: Mapping[str, str] | None = None) -> tuple[list[str], str | None]:
    """Stale copy paths, and one human ``status`` line when that list is non-empty.

    Uses the same entries as ``skill status``. The line is omitted when nothing is stale.
    """
    stale = [entry for entry in status_entries(repo, env) if entry.get("stale")]
    paths: list[str] = []
    for entry in stale:
        path = _copy_path(entry)
        if path:
            paths.append(path)
    if not paths:
        return [], None
    line = f"skill:  {', '.join(paths)}: {STALE_DIFFERS}"
    if all(entry.get("state") == "elsewhere" for entry in stale):
        line += f"; {ELSEWHERE_UPDATE}"
    return paths, line


def stale_available_notice(repo: Path | None, host: str, env: Mapping[str, str] | None = None) -> str | None:
    """When the copy ``skill_available`` would return is stale, one update line.

    Walks locations in the same order as ``skill_available``. An elsewhere copy
    names the tool that installed it. An outdated copy names its upgrade command.
    """
    for entry in status_entries(repo, env):
        if entry.get("host") != host:
            continue
        state = entry.get("state")
        if state in {"current", "outdated"}:
            if not entry.get("stale"):
                return None
            path = _copy_path(entry)
            return f"{path}: {STALE_DIFFERS}; upgrade: {entry['install_command']}"
        if state == "elsewhere":
            if not entry.get("stale"):
                return None
            path = _copy_path(entry)
            return f"{path}: {STALE_DIFFERS}; {ELSEWHERE_UPDATE}"
    return None


def cmd_install(repo_arg: str | None, host: str, user: bool, as_json: bool) -> int:
    require_host(host)
    if user:
        loc = user_location(host)
        outcome = install(loc)
        excluded = False
        repo = None
    else:
        repo = resolve_repo(Path(repo_arg or ".").expanduser().resolve())
        loc = project_location(repo, host)
        outcome = install(loc)
        excluded = ensure_exclude(repo, exclude_pattern(host))
    if as_json:
        from handoff_a2a.reporting import print_json

        print_json(
            "skill-install",
            {"host": host, "scope": loc.scope, "path": str(loc.path), "outcome": outcome, "repo": None if repo is None else str(repo)},
        )
        return 0
    print(f"skill:   {outcome}: {loc.path}")
    if excluded:
        print(f"exclude: {exclude_pattern(host)} (local git exclude; nothing to commit)")
    if loc.scope == "user":
        print("scope:   every repository on this machine for this host; nothing was written into a repository")
    print(f"next:    in your {host.capitalize()} Planner chat: /handoff-cli set up handoff in this repo")
    return 0


def cmd_status(repo_arg: str | None, as_json: bool) -> int:
    repo = None
    note = None
    try:
        repo = resolve_repo(Path(repo_arg or ".").expanduser().resolve())
    except SkillError as exc:
        note = str(exc)
    entries = status_entries(repo)
    if as_json:
        from handoff_a2a.reporting import print_json

        print_json(
            "skill-status",
            {"repo": None if repo is None else str(repo), "source": str(REPO_SKILL), "source_digest": source_digest(), "locations": entries, "note": note},
        )
        return 0
    print(f"source:  {REPO_SKILL}")
    if note:
        print(f"note:    project locations skipped: {note}")
    for entry in entries:
        detail = f" ({entry['detail']})" if entry.get("detail") else ""
        mark = f" {STALE_DIFFERS}" if entry.get("stale") else ""
        print(f"{entry['scope']:<8}{entry['host']:<7} {entry['path'] or '-'}: {entry['state']}{detail}{mark}")
        if entry.get("state") == "elsewhere" and entry.get("stale"):
            print(f"         {ELSEWHERE_UPDATE}")
        if entry["state"] == "outdated":
            print(f"         upgrade: {entry['install_command']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="handoff skill", description="Install the handoff-cli Planner skill for a host.")
    sub = parser.add_subparsers(dest="action", required=True)
    inst = sub.add_parser("install")
    inst.add_argument("repo", nargs="?")
    inst.add_argument("--host", choices=HOSTS, required=True)
    inst.add_argument("--user", action="store_true", help="install for every repository on this machine (host's user skill folder)")
    inst.add_argument("--json", action="store_true")
    stat = sub.add_parser("status")
    stat.add_argument("repo", nargs="?")
    stat.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.action == "install":
            if args.user and args.repo:
                raise SkillError("--user installs for every repository; do not name a repository with it", exit_code=2)
            return cmd_install(args.repo, args.host, args.user, args.json)
        return cmd_status(args.repo, args.json)
    except (SkillError, OSError) as exc:
        if args.json:
            from handoff_a2a.reporting import print_json_error

            return print_json_error(f"skill-{args.action}", str(exc), getattr(exc, "exit_code", 1))
        print(f"handoff: {exc}", file=sys.stderr)
        return getattr(exc, "exit_code", 1)
