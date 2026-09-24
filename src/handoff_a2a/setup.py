"""`handoff init` for managed A2A: generate every JSON/token/skill file.

Layout (all git-excluded, machine-local):

  <repo>/.handoff-config.json                 client routing + managed pointer
  <repo>/.handoff-logs/server.json            authoritative Executor selection
  <repo>/.handoff-logs/service/               process record, logs, lock,
                                              pending selection, history
  <repo>/.handoff-logs/credentials/service-token   local bearer token (0600)
  <repo>/.handoff-logs/server-evidence/       server state DB + run evidence
  <repo>/.cursor/skills/handoff-cli/          only with --planner cursor

Setup never launches a model task, approves a plan, logs into an account, or
downloads anything. Re-running it preserves HANDOFF content, the token, the
current selection, approval/history, and user settings; changing the selection
is `handoff model`'s job. The client config is written last, so a failed
attempt never leaves a configuration that looks ready.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from handoff_a2a.client import write_json_atomic
from handoff_a2a.config import MANAGED_SCHEMA, ConfigError, load_config
from handoff_a2a.adapters.cursor import RULE_EXCLUDE_PATTERN
from handoff_a2a.providers import (
    LOGIN_COMMANDS,
    PROVIDERS,
    ProviderError,
    Validation,
    list_models,
    validate_model,
)
from handoff_a2a.workspace import (
    CONFIG_NAME,
    HANDOFF_NAME,
    LOG_DIRNAME,
    PLANNER_SKILL_DIR,
    DirLock,
    WorkspaceBusy,
)

SERVER_CONFIG_NAME = "server.json"
SERVICE_DIRNAME = "service"
CREDENTIALS_DIRNAME = "credentials"
TOKEN_NAME = "service-token"
EVIDENCE_DIRNAME = "server-evidence"
SERVICE_LOCK_NAME = "service.lock"
REPO_SKILL = Path(__file__).resolve().parents[2] / "skills" / "handoff-cli"
BASE_EXCLUDES = (
    HANDOFF_NAME,
    "HANDOFF-ARCHIVE.md",
    f"{LOG_DIRNAME}/",
    ".claude/settings.local.json",
    CONFIG_NAME,
    RULE_EXCLUDE_PATTERN,
)
PLANNER_EXCLUDE = f"{PLANNER_SKILL_DIR}/"
DEFAULT_TIMEOUTS = {"request_timeout_s": 30, "wait_timeout_s": 180, "poll_interval_s": 1}


class SetupError(Exception):
    def __init__(self, message: str, *, exit_code: int = 1):
        super().__init__(message)
        self.exit_code = exit_code


@dataclass(frozen=True)
class ManagedPaths:
    repo: Path

    @property
    def config(self) -> Path:
        return self.repo / CONFIG_NAME

    @property
    def logs(self) -> Path:
        return self.repo / LOG_DIRNAME

    @property
    def server_config(self) -> Path:
        return self.logs / SERVER_CONFIG_NAME

    @property
    def service_dir(self) -> Path:
        return self.logs / SERVICE_DIRNAME

    @property
    def credentials_dir(self) -> Path:
        return self.logs / CREDENTIALS_DIRNAME

    @property
    def token(self) -> Path:
        return self.credentials_dir / TOKEN_NAME

    @property
    def evidence(self) -> Path:
        return self.logs / EVIDENCE_DIRNAME

    @property
    def state_db(self) -> Path:
        return self.evidence / "state.sqlite"

    @property
    def planner_skill(self) -> Path:
        return self.repo / PLANNER_SKILL_DIR

    @property
    def backups(self) -> Path:
        return self.service_dir / "backups"


def service_lock(paths: ManagedPaths) -> DirLock:
    """Setup/service mutex. Lock order: service.lock, then submit.lock."""
    return DirLock(paths.service_dir / SERVICE_LOCK_NAME)


@contextlib.contextmanager
def holding(lock: DirLock, what: str) -> Iterator[None]:
    try:
        lock.acquire()
    except WorkspaceBusy as exc:
        raise SetupError(f"{what} is busy: {exc}", exit_code=2) from exc
    try:
        yield
    finally:
        lock.release()


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], check=False, capture_output=True, text=True)
    if result.returncode != 0:
        raise SetupError(f"git {' '.join(args)} failed: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout.strip()


def resolve_worktree(path: Path) -> Path:
    """Top level of the Git worktree (linked worktrees have a .git file, not a directory)."""
    if not path.is_dir():
        raise SetupError(f"no such directory: {path}")
    try:
        return Path(git(path, "rev-parse", "--show-toplevel")).resolve()
    except SetupError as exc:
        raise SetupError(f"not a git repository: {path}") from exc


def exclude_file(repo: Path) -> Path:
    return Path(git(repo, "rev-parse", "--path-format=absolute", "--git-path", "info/exclude"))


def is_tracked(repo: Path, rel: str) -> bool:
    result = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "--error-unmatch", "--", rel], check=False, capture_output=True
    )
    return result.returncode == 0


def tracked_under(repo: Path, rel: str) -> list[str]:
    out = subprocess.run(["git", "-C", str(repo), "ls-files", "--", rel], check=False, capture_output=True, text=True)
    return [line for line in out.stdout.splitlines() if line]


def workspace_id_for(repo: Path) -> str:
    digest = hashlib.sha256(str(repo).encode("utf-8")).hexdigest()[:10]
    name = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in repo.name).strip("-") or "repo"
    return f"{name}-{digest}"


def pick_port(repo: Path) -> int:
    """A free loopback port; falls back to a stable per-repo port if probing is not permitted."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])
    except OSError:
        digest = int(hashlib.sha256(str(repo).encode("utf-8")).hexdigest()[:8], 16)
        return 20000 + digest % 20000


def card_url(port: int) -> str:
    return f"http://127.0.0.1:{port}/.well-known/agent-card.json"


def read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SetupError(f"cannot read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SetupError(f"{path} must be a JSON object")
    return data


def selection_of(server: dict[str, Any]) -> tuple[str, str, str | None]:
    for provider in PROVIDERS:
        block = server.get(provider)
        if isinstance(block, dict):
            return provider, str(block.get("model") or ""), block.get("reasoning_effort")
    raise SetupError("server.json has no executor selection")


def server_block(validation: Validation) -> dict[str, Any]:
    block: dict[str, Any] = {"binary": validation.binary, "model": validation.model}
    if validation.reasoning_effort:
        block["reasoning_effort"] = validation.reasoning_effort
    return block


def selection_record(validation: Validation) -> dict[str, Any]:
    """Provenance of the selection (not a second copy of the authoritative model)."""
    return {
        "validated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "validation": validation.status,
        "source": validation.source,
        "cli_version": validation.version,
        "notes": list(validation.notes),
    }


def build_server_config(
    paths: ManagedPaths,
    *,
    workspace_id: str,
    port: int,
    validation: Validation,
    generation: int,
) -> dict[str, Any]:
    return {
        "host": "127.0.0.1",
        "port": port,
        "workspace_id": workspace_id,
        "workspace_path": str(paths.repo),
        "credential_file": str(paths.token),
        "evidence_dir": str(paths.evidence),
        "state_db": str(paths.state_db),
        "caller_id": "local-planner",
        "execution_timeout_s": 3600,
        "cancel_grace_s": 5,
        "config_generation": generation,
        validation.provider: server_block(validation),
        "selection": selection_record(validation),
    }


def build_client_config(paths: ManagedPaths, *, workspace_id: str, port: int, planner: str | None, max_rounds: int = 3) -> dict[str, Any]:
    return {
        "transport": "a2a",
        "max_rounds": max_rounds,
        "a2a": {
            "agent_card_url": card_url(port),
            "workspace_id": workspace_id,
            "credential_file": str(paths.token),
            **DEFAULT_TIMEOUTS,
        },
        "managed": {
            "schema": MANAGED_SCHEMA,
            "server_config": str(paths.server_config),
            "planner": {"host": planner} if planner else None,
        },
    }


class Journal:
    """Files created/modified by one setup attempt, for rollback on failure."""

    def __init__(self) -> None:
        self.created: list[Path] = []
        self.created_dirs: list[Path] = []
        self.modified: dict[Path, bytes] = {}

    def mkdir(self, path: Path, mode: int = 0o755) -> None:
        missing = []
        probe = path
        while not probe.exists():
            missing.append(probe)
            probe = probe.parent
        path.mkdir(parents=True, exist_ok=True, mode=mode)
        self.created_dirs.extend(reversed(missing))

    def before_write(self, path: Path) -> None:
        if path.exists():
            if path not in self.modified and path not in self.created:
                self.modified[path] = path.read_bytes()
        elif path not in self.created:
            self.created.append(path)

    def write_json(self, path: Path, payload: dict[str, Any]) -> None:
        self.before_write(path)
        write_json_atomic(path, payload)

    def write_text(self, path: Path, text: str) -> None:
        self.before_write(path)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)

    def rollback(self) -> None:
        for path, data in self.modified.items():
            with contextlib.suppress(OSError):
                path.write_bytes(data)
        for path in reversed(self.created):
            with contextlib.suppress(OSError):
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                else:
                    path.unlink()
        for directory in reversed(self.created_dirs):
            with contextlib.suppress(OSError):
                directory.rmdir()


def write_token(journal: Journal, paths: ManagedPaths) -> None:
    journal.mkdir(paths.credentials_dir, 0o700)
    os.chmod(paths.credentials_dir, 0o700)
    journal.before_write(paths.token)
    fd = os.open(paths.token, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(secrets.token_urlsafe(32) + "\n")


def ensure_excludes(journal: Journal, repo: Path, patterns: list[str]) -> list[str]:
    path = exclude_file(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    lines = existing.splitlines()
    added = [p for p in patterns if p not in lines]
    if not added:
        return []
    journal.before_write(path)
    text = existing if not existing or existing.endswith("\n") else existing + "\n"
    path.write_text(text + "".join(p + "\n" for p in added), encoding="utf-8")
    return added


def _tree_digest(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def planner_skill_state(paths: ManagedPaths) -> str:
    """'absent', 'current' (identical copy), or 'foreign' (never overwritten)."""
    target = paths.planner_skill
    if target.is_symlink():
        return "foreign"
    if not target.exists():
        return "absent"
    if tracked_under(paths.repo, PLANNER_SKILL_DIR):
        return "foreign"
    return "current" if _tree_digest(target) == _tree_digest(REPO_SKILL) else "foreign"


def install_planner_skill(journal: Journal, paths: ManagedPaths) -> str:
    state = planner_skill_state(paths)
    if state == "current":
        return "already installed (identical)"
    if state == "foreign":
        raise SetupError(
            f"{PLANNER_SKILL_DIR} already exists with different or tracked content; "
            "not overwriting an unrelated skill (move it aside or install manually)"
        )
    for parent in (paths.repo / ".cursor", paths.repo / ".cursor" / "skills"):
        if parent.is_symlink():
            raise SetupError(f"{parent.relative_to(paths.repo)} is a symlink; refusing to install the Planner skill")
    journal.mkdir(paths.planner_skill.parent)
    journal.before_write(paths.planner_skill)
    shutil.copytree(REPO_SKILL, paths.planner_skill)
    return "installed"


def check_collisions(paths: ManagedPaths) -> None:
    repo = paths.repo
    for rel in (CONFIG_NAME, f"{LOG_DIRNAME}", HANDOFF_NAME):
        target = repo / rel
        if target.is_symlink():
            raise SetupError(f"{rel} is a symlink; refusing to write generated files through it")
    if is_tracked(repo, CONFIG_NAME):
        raise SetupError(f"{CONFIG_NAME} is tracked in git; refuse to treat a credential reference as local")
    tracked_logs = tracked_under(repo, LOG_DIRNAME)
    if tracked_logs:
        raise SetupError(f"{LOG_DIRNAME}/ has tracked files ({tracked_logs[0]}); refusing to write service state there")
    if is_tracked(repo, HANDOFF_NAME):
        raise SetupError(f"{HANDOFF_NAME} is tracked in git; the handoff file must stay local")
    for sub in (paths.service_dir, paths.credentials_dir, paths.evidence):
        if sub.is_symlink():
            raise SetupError(f"{sub.relative_to(repo)} is a symlink; refusing to write through it")


def unresolved_work(paths: ManagedPaths) -> str | None:
    logs = paths.logs
    if (logs / "outstanding.json").is_file():
        return "an outstanding A2A run exists (handoff status / resume / cancel first)"
    if (logs / "execute.lock").exists():
        return "execute.lock is held (a worker may be running)"
    if (logs / "submit.lock").exists():
        return "a submission is in progress (submit.lock)"
    return None


@dataclass
class SetupOptions:
    repo: Path
    executor: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    planner: str | None = None
    port: int | None = None
    template: Path | None = None
    interactive: bool = False


@dataclass
class SetupReport:
    repo: Path
    created: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    selection: str = ""
    validation: str = ""
    next_steps: list[str] = field(default_factory=list)


def _q(path: Path) -> str:
    text = str(path)
    return f'"{text}"'


def prompt_choices(opts: SetupOptions) -> SetupOptions:
    """Guided setup on a TTY: only real choices, never a silent paid default."""

    def ask(question: str, allowed: tuple[str, ...]) -> str:
        while True:
            answer = input(f"{question} [{'/'.join(allowed)}]: ").strip().lower()
            if answer in allowed:
                return answer
            print(f"  choose one of: {', '.join(allowed)}")

    if not opts.executor:
        opts.executor = ask("Executor provider", PROVIDERS)
    if not opts.model:
        try:
            listing = list_models(opts.executor)
        except ProviderError as exc:
            raise SetupError(str(exc)) from exc
        if listing.available:
            print(f"Models from {listing.source}:")
            for index, item in enumerate(listing.models, 1):
                print(f"  {index:3d}. {item.id}" + (f"  ({item.name})" if item.name else ""))
        else:
            print(listing.note)
        while not opts.model:
            answer = input("Executor model ID (or list number): ").strip()
            if answer.isdigit() and listing.available and 1 <= int(answer) <= len(listing.models):
                opts.model = listing.models[int(answer) - 1].id
            elif answer:
                opts.model = answer
    if opts.planner is None:
        opts.planner = ask("Planner host integration", ("cursor", "none"))
    return opts


def run_setup(opts: SetupOptions) -> SetupReport:
    repo = resolve_worktree(opts.repo)
    paths = ManagedPaths(repo)
    report = SetupReport(repo=repo)
    planner = None if opts.planner in (None, "none") else opts.planner
    if planner not in (None, "cursor"):
        raise SetupError(f"unknown planner host {opts.planner!r} (cursor or none)")
    check_collisions(paths)
    try:
        existing = load_config(repo)
    except ConfigError as exc:
        raise SetupError(
            f"existing {CONFIG_NAME} is not usable ({exc}); fix or move it aside — setup does not overwrite it"
        ) from exc
    managed = existing is not None and existing.managed is not None
    if paths.logs.exists() and not paths.logs.is_dir():
        raise SetupError(f"{LOG_DIRNAME} exists and is not a directory")
    fresh_dirs = [d for d in (paths.logs, paths.service_dir) if not d.exists()]
    paths.logs.mkdir(exist_ok=True)
    try:
        with holding(service_lock(paths), "service configuration"):
            journal = Journal()
            try:
                if managed:
                    _reinit(paths, opts, planner, report, journal)
                else:
                    _fresh(paths, opts, planner, existing, report, journal)
            except BaseException:
                journal.rollback()
                raise
    except BaseException:
        for directory in reversed(fresh_dirs):
            with contextlib.suppress(OSError):
                directory.rmdir()  # only if the failed attempt left it empty
        raise
    return report


def _require_choice(opts: SetupOptions) -> None:
    if not opts.executor or not opts.model:
        raise SetupError(
            "choose an Executor provider and model: "
            f'handoff init {_q(opts.repo)} --transport a2a --executor <{"|".join(PROVIDERS)}> '
            '--model "<model-id>" [--planner cursor]',
            exit_code=2,
        )


def _validate(opts: SetupOptions) -> Validation:
    try:
        return validate_model(opts.executor or "", opts.model or "", reasoning_effort=opts.reasoning_effort)
    except ProviderError as exc:
        raise SetupError(f"{exc}\nnothing was configured") from exc


def _fresh(
    paths: ManagedPaths,
    opts: SetupOptions,
    planner: str | None,
    existing: Any,
    report: SetupReport,
    journal: Journal,
) -> None:
    _require_choice(opts)
    if existing is not None:
        reason = unresolved_work(paths)
        if reason:
            raise SetupError(f"refusing to migrate to managed A2A: {reason}")
    validation = _validate(opts)
    if planner == "cursor" and planner_skill_state(paths) == "foreign":
        raise SetupError(
            f"{PLANNER_SKILL_DIR} already exists with different or tracked content; not overwriting it"
        )
    if paths.server_config.exists():
        raise SetupError(f"{paths.server_config} already exists without a managed {CONFIG_NAME}; move it aside first")
    workspace_id = workspace_id_for(paths.repo)
    if existing is not None:
        # Migration: keep the workspace identity the approval receipt names,
        # and a recoverable copy of the previous settings. External
        # credentials and databases the old config referenced are not touched.
        if existing.a2a is not None:
            workspace_id = existing.a2a.workspace_id
        journal.mkdir(paths.backups)
        backup = paths.backups / f"{time.strftime('%Y%m%d-%H%M%S')}-{CONFIG_NAME.lstrip('.')}"
        journal.before_write(backup)
        shutil.copy2(existing.path, backup)
        report.notes.append(f"previous {CONFIG_NAME} saved to {backup}")
    port = opts.port or pick_port(paths.repo)
    journal.mkdir(paths.service_dir)
    journal.mkdir(paths.evidence)
    if paths.token.exists():
        report.kept.append(str(paths.token))
    else:
        write_token(journal, paths)
        report.created.append(f"{paths.token} (mode 0600)")
    journal.write_json(
        paths.server_config,
        build_server_config(paths, workspace_id=workspace_id, port=port, validation=validation, generation=1),
    )
    report.created.append(str(paths.server_config))
    _common_files(paths, opts, planner, report, journal)
    journal.write_json(
        paths.config,
        build_client_config(
            paths,
            workspace_id=workspace_id,
            port=port,
            planner=planner,
            max_rounds=existing.max_rounds if existing is not None else 3,
        ),
    )
    report.created.append(str(paths.config))
    _record_history(paths, journal, {"event": "selected", "generation": 1, "provider": validation.provider, "model": validation.model})
    report.selection = f"{validation.provider} / {validation.model}"
    report.validation = validation.describe()
    report.notes.extend(validation.notes)
    _next_steps(paths, validation.provider, planner, report)


def _reinit(
    paths: ManagedPaths,
    opts: SetupOptions,
    planner: str | None,
    report: SetupReport,
    journal: Journal,
) -> None:
    server = read_json(paths.server_config) if paths.server_config.is_file() else None
    if server is None:
        raise SetupError(f"managed {CONFIG_NAME} points to missing {paths.server_config}; restore it or move the config aside")
    provider, model, effort = selection_of(server)
    if (opts.executor and opts.executor != provider) or (opts.model and opts.model != model) or (
        opts.reasoning_effort and opts.reasoning_effort != effort
    ):
        raise SetupError(
            f"already configured for {provider} / {model}; change the Executor with "
            f'handoff model {_q(paths.repo)} --provider <name> --model "<model-id>"',
            exit_code=2,
        )
    report.kept.extend([str(paths.config), str(paths.server_config)])
    if paths.token.is_file():
        report.kept.append(str(paths.token))
    else:
        raise SetupError(
            f"service token {paths.token} is missing; setup does not rotate credentials silently — "
            "stop the service, restore the file, or move the managed config aside and re-run init"
        )
    _common_files(paths, opts, planner, report, journal)
    config = read_json(paths.config)
    current_planner = ((config.get("managed") or {}).get("planner") or {}).get("host")
    if planner and planner != current_planner:
        config["managed"]["planner"] = {"host": planner}
        journal.write_json(paths.config, config)
        report.notes.append(f"Planner host integration added: {planner}")
    report.selection = f"{provider} / {model} (unchanged)"
    report.validation = str((server.get("selection") or {}).get("validation") or "recorded at selection time")
    _next_steps(paths, provider, planner or current_planner, report)


def _common_files(paths: ManagedPaths, opts: SetupOptions, planner: str | None, report: SetupReport, journal: Journal) -> None:
    patterns = list(BASE_EXCLUDES) + ([PLANNER_EXCLUDE] if planner == "cursor" else [])
    added = ensure_excludes(journal, paths.repo, patterns)
    if added:
        report.created.append(f"local git excludes: {', '.join(added)}")
    handoff = paths.repo / HANDOFF_NAME
    if handoff.is_file():
        report.kept.append(str(handoff))
    elif opts.template is not None:
        if not opts.template.is_file():
            raise SetupError(f"template not found: {opts.template}")
        journal.before_write(handoff)
        shutil.copyfile(opts.template, handoff)
        report.created.append(str(handoff))
    if planner == "cursor":
        outcome = install_planner_skill(journal, paths)
        target = report.created if outcome == "installed" else report.kept
        target.append(f"{paths.planner_skill} ({outcome})")


def _record_history(paths: ManagedPaths, journal: Journal, event: dict[str, Any]) -> None:
    path = paths.service_dir / "history.jsonl"
    journal.before_write(path)
    append_history(paths, event)


def append_history(paths: ManagedPaths, event: dict[str, Any]) -> None:
    paths.service_dir.mkdir(parents=True, exist_ok=True)
    record = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **event}
    with (paths.service_dir / "history.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")


def _next_steps(paths: ManagedPaths, provider: str, planner: str | None, report: SetupReport) -> None:
    q = _q(paths.repo)
    login = LOGIN_COMMANDS.get(provider)
    report.notes.append(f"Executor login stays yours: if needed, run `{login}` for the Executor account")
    report.next_steps = [f"handoff server start {q}"]
    if planner == "cursor":
        report.next_steps.append(f'handoff planner {q} --provider cursor --model "<model-id>"   (or the Cursor editor: /handoff-cli)')
    report.next_steps += [
        "write a DRAFT plan in HANDOFF.md (Planner), get explicit human approval, then:",
        f"handoff approve {q}",
        f"handoff watch {q}      (or: handoff execute {q})",
    ]


def print_report(report: SetupReport) -> None:
    print(f"repo:      {report.repo}")
    print(f"executor:  {report.selection}")
    print(f"model check: {report.validation}")
    for item in report.created:
        print(f"created:   {item}")
    for item in report.kept:
        print(f"kept:      {item}")
    for note in report.notes:
        print(f"note:      {note}")
    print("next:")
    for step in report.next_steps:
        print(f"  {step}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="handoff init", description="Generate managed A2A configuration.")
    parser.add_argument("repo", nargs="?", default=".")
    parser.add_argument("--executor", choices=PROVIDERS)
    parser.add_argument("--model")
    parser.add_argument("--reasoning-effort")
    parser.add_argument("--planner", choices=("cursor", "none"))
    parser.add_argument("--port", type=int)
    parser.add_argument("--template", type=Path)
    parser.add_argument("--interactive", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    opts = SetupOptions(
        repo=Path(args.repo).expanduser().resolve(),
        executor=args.executor,
        model=args.model,
        reasoning_effort=args.reasoning_effort,
        planner=args.planner,
        port=args.port,
        template=args.template,
        interactive=args.interactive,
    )
    if opts.port is not None and not (1024 <= opts.port <= 65535):
        print("handoff: --port must be 1024-65535", file=sys.stderr)
        return 2
    try:
        if opts.interactive:
            try:
                existing = load_config(resolve_worktree(opts.repo))
            except ConfigError:
                existing = None
            if existing is None or existing.managed is None:
                opts = prompt_choices(opts)
        report = run_setup(opts)
    except (SetupError, ProviderError) as exc:
        print(f"handoff: {exc}", file=sys.stderr)
        return getattr(exc, "exit_code", 1)
    except (EOFError, KeyboardInterrupt):
        print("\nhandoff: setup canceled; nothing was configured", file=sys.stderr)
        return 130
    print_report(report)
    return 0
