"""Local approval receipts, round accounting, and outstanding-run pointers."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from handoff_a2a.client import write_json_atomic
from handoff_a2a.contracts import snapshot_sha256
from handoff_a2a.workspace import (
    ARCHIVE_NAME,
    HANDOFF_NAME,
    LOG_DIRNAME,
    approved_plan_hash,
    parse_handoff,
    replace_status,
)

WORKFLOW_SCHEMA = "urn:handoff-automation:coding-task:v1#workflow"
OUTSTANDING_NAME = "outstanding.json"
WORKFLOW_NAME = "workflow.json"
CLOSED_DIRNAME = "closed-workflows"


class WorkflowError(ValueError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def workflow_path(repo: Path) -> Path:
    return repo / LOG_DIRNAME / WORKFLOW_NAME


def outstanding_path(repo: Path) -> Path:
    return repo / LOG_DIRNAME / OUTSTANDING_NAME


def load_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else None


def save_workflow(repo: Path, payload: dict[str, Any]) -> Path:
    payload = dict(payload)
    payload["updated_at"] = utc_now()
    return write_json_atomic(workflow_path(repo), payload)


def load_workflow(repo: Path) -> dict[str, Any] | None:
    return load_json(workflow_path(repo))


def load_outstanding(repo: Path) -> dict[str, Any] | None:
    return load_json(outstanding_path(repo))


def save_outstanding(repo: Path, payload: dict[str, Any]) -> Path:
    return write_json_atomic(outstanding_path(repo), payload)


def clear_outstanding(repo: Path) -> None:
    path = outstanding_path(repo)
    if path.is_file():
        path.unlink()


def snapshot_hash(text: str) -> str:
    return snapshot_sha256(text)


def latest_superseded(repo: Path) -> dict[str, Any] | None:
    closed = repo / LOG_DIRNAME / CLOSED_DIRNAME
    if not closed.is_dir():
        return None
    newest: tuple[float, dict[str, Any]] | None = None
    for path in closed.glob("*.json"):
        data = load_json(path)
        if not data or data.get("disposition") != "superseded":
            continue
        mtime = path.stat().st_mtime
        if newest is None or mtime >= newest[0]:
            newest = (mtime, data)
    return None if newest is None else newest[1]


def approve(
    repo: Path,
    *,
    workspace_id: str,
    max_rounds: int,
    parent_workflow_id: str | None = None,
    inherit_baseline: str | None = None,
    inherit_branch: str | None = None,
) -> dict[str, Any]:
    markdown = (repo / HANDOFF_NAME).read_text(encoding="utf-8")
    document = parse_handoff(markdown)
    if document.status in {"", "NO TASK"}:
        raise WorkflowError("there is no task to approve")
    if document.status == "READY FOR QA":
        raise WorkflowError("task is already awaiting QA; do not re-approve a delivery")
    if document.status not in {"DRAFT", "READY FOR EXECUTION", "CHANGES REQUESTED"}:
        raise WorkflowError(f"cannot approve status {document.status!r}")
    if not document.branch:
        raise WorkflowError("HANDOFF.md has no Branch field")
    plan_hash = approved_plan_hash(markdown)
    full_hash = snapshot_hash(markdown)
    existing = load_workflow(repo)
    outstanding = load_outstanding(repo)
    if existing and existing.get("disposition") in {"archived", "superseded"}:
        existing = None
    if existing and existing.get("approved_plan_hash") == plan_hash:
        if outstanding:
            raise WorkflowError("reapproval cannot grant another run while work is unresolved")
        return existing
    if existing and outstanding:
        raise WorkflowError("cannot change the approved plan while work is unresolved")
    if existing:
        existing["approved_plan_hash"] = plan_hash
        existing["submitted_snapshot_hash"] = full_hash
        existing["approved_at"] = utc_now()
        existing["branch"] = document.branch
        existing["workspace_id"] = workspace_id
        existing["max_rounds"] = int(existing.get("max_rounds") or max_rounds)
        saved = save_workflow(repo, existing)
        _set_status(repo, "READY FOR EXECUTION")
        return load_json(saved) or existing
    if parent_workflow_id is None:
        parent = latest_superseded(repo)
        if parent is not None:
            parent_workflow_id = str(parent.get("workflow_id") or "") or None
            inherit_baseline = inherit_baseline or parent.get("post_run_fingerprint")
            inherit_branch = inherit_branch or parent.get("post_run_branch") or parent.get("branch")
    workflow = {
        "schema": WORKFLOW_SCHEMA,
        "workflow_id": str(uuid4()),
        "parent_workflow_id": parent_workflow_id,
        "branch": document.branch,
        "workspace_id": workspace_id,
        "approved_plan_hash": plan_hash,
        "submitted_snapshot_hash": full_hash,
        "approved_at": utc_now(),
        "max_rounds": max_rounds,
        "rounds_used": 0,
        "reserved_execution_id": None,
        "consumed_execution_ids": [],
        "dispatch_hold": False,
        "inherited_baseline": inherit_baseline,
        "inherited_branch": inherit_branch,
        "post_run_fingerprint": inherit_baseline,
        "post_run_branch": inherit_branch or document.branch,
        "last_notified_execution_id": None,
        "disposition": None,
    }
    save_workflow(repo, workflow)
    _set_status(repo, "READY FOR EXECUTION")
    return load_workflow(repo) or workflow


def _set_status(repo: Path, status: str) -> None:
    path = repo / HANDOFF_NAME
    path.write_text(replace_status(path.read_text(encoding="utf-8"), status), encoding="utf-8")


def require_workflow(repo: Path) -> dict[str, Any]:
    workflow = load_workflow(repo)
    if workflow is None or workflow.get("disposition") in {"archived", "superseded"}:
        raise WorkflowError("no active approved workflow; run handoff approve first")
    return workflow


def rounds_available(workflow: dict[str, Any]) -> int:
    used = int(workflow.get("rounds_used") or 0)
    reserved = 1 if workflow.get("reserved_execution_id") else 0
    return int(workflow.get("max_rounds") or 3) - used - reserved


def reserve_round(repo: Path, workflow: dict[str, Any], execution_id: str) -> dict[str, Any]:
    if workflow.get("reserved_execution_id"):
        raise WorkflowError("a round is already reserved for an unresolved submission")
    if rounds_available(workflow) < 1:
        raise WorkflowError(
            "execution round limit reached; Planner must review scope before another run"
        )
    workflow["reserved_execution_id"] = execution_id
    save_workflow(repo, workflow)
    return workflow


def release_reservation(repo: Path, workflow: dict[str, Any], execution_id: str) -> dict[str, Any]:
    if workflow.get("reserved_execution_id") == execution_id:
        workflow["reserved_execution_id"] = None
        save_workflow(repo, workflow)
    return workflow


def consume_round(repo: Path, workflow: dict[str, Any], execution_id: str) -> dict[str, Any]:
    consumed = list(workflow.get("consumed_execution_ids") or [])
    if execution_id not in consumed:
        consumed.append(execution_id)
        workflow["consumed_execution_ids"] = consumed
        workflow["rounds_used"] = len(consumed)
    if workflow.get("reserved_execution_id") == execution_id:
        workflow["reserved_execution_id"] = None
    save_workflow(repo, workflow)
    return workflow


def set_dispatch_hold(repo: Path, workflow: dict[str, Any], held: bool) -> dict[str, Any]:
    workflow["dispatch_hold"] = held
    save_workflow(repo, workflow)
    return workflow


def exhausted(workflow: dict[str, Any]) -> bool:
    return rounds_available(workflow) < 1 and not workflow.get("reserved_execution_id")


def archive_current(
    repo: Path,
    *,
    superseded: bool,
    goal: str,
) -> Path:
    workflow = load_workflow(repo)
    outstanding = load_outstanding(repo)
    if outstanding:
        raise WorkflowError("cannot archive while execution is outstanding; resume or cancel first")
    markdown = (repo / HANDOFF_NAME).read_text(encoding="utf-8")
    document = parse_handoff(markdown)
    archive = repo / ARCHIVE_NAME
    if superseded:
        if workflow is None:
            raise WorkflowError("no workflow to supersede")
        if int(workflow.get("rounds_used") or 0) < int(workflow.get("max_rounds") or 3):
            raise WorkflowError("archive --superseded requires an exhausted round budget")
        if workflow.get("reserved_execution_id"):
            raise WorkflowError("cannot supersede while a round is still reserved")
        disposition = "superseded"
        title = "Superseded"
    else:
        if document.status != "APPROVED":
            raise WorkflowError("ordinary archive requires Status APPROVED")
        disposition = "archived"
        title = "Archived"
    if workflow is not None:
        workflow["disposition"] = disposition
        save_workflow(repo, workflow)
        closed = repo / LOG_DIRNAME / CLOSED_DIRNAME
        closed.mkdir(parents=True, exist_ok=True)
        write_json_atomic(closed / f"{workflow['workflow_id']}.json", workflow)
    if not archive.is_file():
        archive.write_text(
            "# HANDOFF Archive\n\nCompleted tasks, appended verbatim by `handoff archive`. "
            "Local-only (git-excluded).\n",
            encoding="utf-8",
        )
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    extra = ""
    if workflow is not None:
        extra = (
            f"\n\nWorkflow-ID: {workflow.get('workflow_id')}\n"
            f"Disposition: {disposition}\n"
            f"Rounds-used: {workflow.get('rounds_used')}/{workflow.get('max_rounds')}\n"
        )
        if workflow.get("parent_workflow_id"):
            extra += f"Parent-workflow: {workflow['parent_workflow_id']}\n"
    with archive.open("a", encoding="utf-8") as handle:
        handle.write(f"\n---\n\n# {title} {stamp} — {goal or '(no goal)'}\n{extra}\n")
        in_task = False
        for line in markdown.splitlines(True):
            if line.startswith("## Current Task"):
                in_task = True
            if in_task:
                handle.write(line)
    workflow_path(repo).unlink(missing_ok=True)
    return archive
