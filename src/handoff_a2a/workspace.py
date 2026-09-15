"""Registered git workspace, execute lock, and HANDOFF.md snapshot checks."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from handoff_a2a.contracts import ELIGIBLE_HANDOFF_STATUSES, READY_FOR_QA, snapshot_sha256

HANDOFF_NAME = "HANDOFF.md"
LOCK_DIRNAME = "execute.lock"
LOG_DIRNAME = ".handoff-logs"

_STATUS_RE = re.compile(r"^\*\*Status:\*\*\s*(.*?)\s*$", re.MULTILINE)
_BRANCH_RE = re.compile(r"^\*\*Branch:\*\*\s*(.*?)\s*$", re.MULTILINE)
_HEADING_RE = re.compile(r"^## .+$", re.MULTILINE)


class WorkspaceError(Exception):
    """Pre-worker workspace/request refusal."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class WorkspaceBusy(WorkspaceError):
    pass


@dataclass(frozen=True)
class GitState:
    branch: str
    head: str
    status: str
    diff: str


@dataclass(frozen=True)
class HandoffDocument:
    text: str
    status: str
    branch: str
    execution_notes: str
    planner_fingerprint: str


def read_status(text: str) -> str:
    match = _STATUS_RE.search(text)
    return match.group(1).strip() if match else ""


def read_declared_branch(text: str) -> str:
    match = _BRANCH_RE.search(text)
    return match.group(1).strip() if match else ""


def _section(text: str, heading: str) -> str:
    matches = list(_HEADING_RE.finditer(text))
    for index, match in enumerate(matches):
        if match.group(0).strip() == heading:
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            return text[match.end() : end]
    return ""


def execution_notes(text: str) -> str:
    return _section(text, "## Execution Notes").strip()


def planner_fingerprint(text: str) -> str:
    """Planner-owned plan + QA text, excluding Status and Execution Notes."""
    matches = list(_HEADING_RE.finditer(text))
    plan = ""
    qa = ""
    for index, match in enumerate(matches):
        heading = match.group(0).strip()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[match.start() : end]
        if heading == "## Current Task":
            plan = _STATUS_RE.sub("**Status:**", body)
        elif heading == "## QA Feedback":
            qa = body
    return f"{plan}\n---\n{qa}"


def parse_handoff(text: str) -> HandoffDocument:
    return HandoffDocument(
        text=text,
        status=read_status(text),
        branch=read_declared_branch(text),
        execution_notes=execution_notes(text),
        planner_fingerprint=planner_fingerprint(text),
    )


class ExecuteLock:
    """Atomic `.handoff-logs/execute.lock` directory lock. Never deletes a foreign lock."""

    def __init__(self, workspace: Path):
        self.path = workspace / LOG_DIRNAME / LOCK_DIRNAME
        self._held = False

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.path.mkdir()
        except FileExistsError as exc:
            raise WorkspaceBusy(
                f"workspace busy (or crashed): remove {self.path} to clear"
            ) from exc
        self._held = True

    def write_owner(self, payload: dict[str, Any]) -> None:
        if not self._held:
            raise WorkspaceError("execute lock is not held")
        (self.path / "owner.json").write_text(
            json.dumps(payload, indent=2) + "\n", encoding="utf-8"
        )

    def owner(self) -> dict[str, Any] | None:
        path = self.path / "owner.json"
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None

    def release(self) -> None:
        if not self._held:
            return
        owner = self.path / "owner.json"
        if owner.exists():
            owner.unlink()
        self.path.rmdir()
        self._held = False

    def held(self) -> bool:
        return self._held


def release_lock_if_owner(workspace: Path, execution_id: str) -> bool:
    """Remove this execution's leftover lock directory after verified stop. Never a foreign lock."""
    lock = workspace / LOG_DIRNAME / LOCK_DIRNAME
    owner_path = lock / "owner.json"
    if not lock.is_dir() or not owner_path.is_file():
        return False
    try:
        data = json.loads(owner_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(data, dict) or data.get("execution_id") != execution_id:
        return False
    owner_path.unlink()
    try:
        lock.rmdir()
    except OSError:
        return False
    return True


class GitWorkspace:
    def __init__(self, workspace_id: str, path: Path):
        self.workspace_id = workspace_id
        self.path = path.resolve()
        self.lock = ExecuteLock(self.path)
        self.handoff_path = self.path / HANDOFF_NAME

    def git(self, *args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(self.path), *args],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise WorkspaceError(
                f"git {' '.join(args)} failed: {result.stderr.strip() or result.stdout.strip()}"
            )
        return result.stdout.strip("\n")

    def require_git_checkout(self) -> None:
        if not (self.path / ".git").exists() and not (self.path / ".git").is_file():
            # git rev-parse is the authority for worktrees / linked checkouts
            try:
                self.git("rev-parse", "--is-inside-work-tree")
            except WorkspaceError as exc:
                raise WorkspaceError(f"workspace is not a git working tree: {self.path}") from exc
        if not self.handoff_path.is_file():
            raise WorkspaceError(f"no {HANDOFF_NAME} in {self.path}")

    def current_branch(self) -> str:
        return self.git("branch", "--show-current")

    def current_head(self) -> str:
        return self.git("rev-parse", "HEAD")

    def snapshot(self) -> GitState:
        return GitState(
            branch=self.current_branch(),
            head=self.current_head(),
            status=self.git("status", "--porcelain"),
            diff=self.git("diff", "HEAD"),
        )

    def commits_between(self, before: str, after: str) -> list[tuple[str, str]]:
        if not before or not after or before == after:
            return []
        output = self.git("log", "--format=%H\t%s", f"{before}..{after}")
        if not output.strip():
            return []
        commits = []
        for line in output.splitlines():
            sha, _, subject = line.partition("\t")
            commits.append((sha, subject))
        return commits

    def read_handoff_bytes(self) -> bytes:
        return self.handoff_path.read_bytes()

    def read_handoff_text(self) -> str:
        return self.handoff_path.read_text(encoding="utf-8")

    def validate_request(
        self,
        *,
        workspace_id: str,
        expected_branch: str,
        expected_head: str,
        handoff_markdown: str,
        request_sha256: str,
    ) -> HandoffDocument:
        if workspace_id != self.workspace_id:
            raise WorkspaceError(
                f"workspace_id {workspace_id!r} is not this server's registered workspace"
            )
        self.require_git_checkout()
        local_bytes = self.read_handoff_bytes()
        incoming_bytes = handoff_markdown.encode("utf-8")
        local_digest = snapshot_sha256(local_bytes)
        if request_sha256 != snapshot_sha256(incoming_bytes) or incoming_bytes != local_bytes:
            raise WorkspaceError(
                "incoming HANDOFF snapshot does not match the local file (hash or bytes)"
            )
        if local_digest != request_sha256:
            raise WorkspaceError("request_sha256 does not match local HANDOFF.md bytes")
        document = parse_handoff(local_bytes.decode("utf-8"))
        if document.status not in ELIGIBLE_HANDOFF_STATUSES:
            raise WorkspaceError(
                f"HANDOFF status {document.status!r} is not eligible for execution"
            )
        checked_out = self.current_branch()
        head = self.current_head()
        if not document.branch:
            raise WorkspaceError("HANDOFF.md has no Branch field")
        if expected_branch != document.branch or expected_branch != checked_out:
            raise WorkspaceError(
                "expected_branch, HANDOFF Branch, and checked-out branch must agree"
            )
        if expected_head != head:
            raise WorkspaceError("stale expected_head: does not match current HEAD")
        return document

    def evaluate_transition(self, before: HandoffDocument) -> tuple[HandoffDocument, bool, str | None]:
        after = parse_handoff(self.read_handoff_text())
        plan_intact = after.planner_fingerprint == before.planner_fingerprint
        if after.status == "APPROVED":
            return after, False, "executor must not set Status to APPROVED"
        if not plan_intact:
            return after, False, "executor changed Planner-owned plan or QA sections"
        if after.status != READY_FOR_QA:
            return (
                after,
                False,
                f"expected Status {READY_FOR_QA}, found {after.status!r}",
            )
        return after, True, None
