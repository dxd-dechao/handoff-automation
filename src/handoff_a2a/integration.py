"""Production CLI integration: approve/execute/status/resume/cancel/watch/archive."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from handoff_a2a.client import (
    ClientError,
    CodingClient,
    UnresolvedExecution,
    _recovery_required,
    _update_record_from_task,
    cancel_from_record,
    card_supports_durable_dedup,
    coding_result_from_task,
    execute_exit_code,
    initial_run_record,
    load_run_record,
    normalize_agent_card_url,
    persist_run_record,
    resume_from_record,
    run_record_path,
    status_from_record,
    task_reason,
    verify_agent_card,
)
from handoff_a2a.config import ConfigError, HandoffConfig, recovery_timing, require_a2a_config
from handoff_a2a.contracts import (
    CODING_TASK_PROFILE,
    ELIGIBLE_HANDOFF_STATUSES,
    INTERRUPTED_TASK_STATES,
    READY_FOR_QA,
    TERMINAL_TASK_STATES,
    WORKER_LIFECYCLE_ARTIFACT,
    WORKSPACE_FINGERPRINT_PARAM,
    parse_coding_request,
    snapshot_sha256,
)
from handoff_a2a.reporting import (
    append_event,
    load_manifest,
    log_path,
    request_snapshot_path,
    result_path,
    usage_fields,
    write_manifest,
)
from handoff_a2a.workflow import (
    WorkflowError,
    approve as record_approval,
    archive_current,
    clear_outstanding,
    consume_round,
    exhausted,
    load_outstanding,
    load_workflow,
    release_reservation,
    require_workflow,
    reserve_round,
    rounds_available,
    save_outstanding,
    save_workflow,
    set_dispatch_hold,
    utc_now,
)
from handoff_a2a.workspace import (
    GitWorkspace,
    HANDOFF_NAME,
    LOCK_DIRNAME,
    LOG_DIRNAME,
    WorkspaceBusy,
    WorkspaceError,
    approved_plan_hash,
    parse_handoff,
    replace_status,
    submit_lock,
)
import httpx
from google.protobuf.json_format import MessageToDict

from handoff_a2a.reporting import print_json, print_json_error
from handoff_a2a.selection import (
    SelectionError,
    apply_pending as apply_pending_selection,
    describe as managed_describe,
    expected_card as managed_expected_card,
    is_managed,
    run_mode,
    status_lines as managed_status_lines,
)
from handoff_a2a.processes import permission_denied
from handoff_a2a.service import (
    PROBE_NOT_PERMITTED,
    UNKNOWN_SERVICE,
    ServiceError,
    claim_watcher,
    inspect as inspect_service,
    load_managed,
    release_watcher,
    watcher_state,
)
from handoff_a2a.setup import SetupError


HOLD_AFTER_RECORD = "HANDOFF_A2A_HOLD_AFTER_RECORD"


class IntegrationError(Exception):
    def __init__(self, message: str, *, exit_code: int = 1):
        super().__init__(message)
        self.exit_code = exit_code


class DispatchDeferred(IntegrationError):
    """Dispatch cannot happen yet (lock busy, managed service not verified).

    Watch retries at its next poll instead of waiting for a status change.
    """


@dataclass
class Blocker:
    code: str
    message: str
    fix: str


@dataclass
class Preflight:
    ready: bool
    branch: dict[str, Any]
    baseline_clean: bool
    dirty_paths: list[str] = field(default_factory=list)
    blockers: list[Blocker] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    create_branch: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "branch": self.branch,
            "baseline_clean": self.baseline_clean,
            "dirty_paths": list(self.dirty_paths),
            "blockers": [{"code": item.code, "message": item.message, "fix": item.fix} for item in self.blockers],
            "notes": list(self.notes),
        }


def _dirty_message(paths: list[str]) -> str:
    shown = paths[:10]
    extra = len(paths) - len(shown)
    tail = f" (+{extra} more)" if extra else ""
    return "initial A2A execution requires a clean code baseline: " + ", ".join(shown) + tail


def _dirty_fix() -> str:
    return (
        "commit the changes or run git stash -u; "
        "for a file that must stay untracked, add it to .git/info/exclude"
    )


SERVICE_BLOCKER_CODES = frozenset({"service_stopped", "service_unknown"})


def format_blockers(preflight: Preflight) -> str:
    lines = ["refusing to execute:"]
    for item in preflight.blockers:
        lines.append(f"- {item.message}")
        lines.append(f"  fix: {item.fix}")
    return "\n".join(lines)


def raise_preflight(preflight: Preflight) -> None:
    """Refuse dispatch. Service-only blockers defer so watch retries."""
    if not preflight.blockers:
        return
    message = format_blockers(preflight)
    if all(item.code in SERVICE_BLOCKER_CODES for item in preflight.blockers):
        raise DispatchDeferred(message)
    raise IntegrationError(message)


def endpoint_unavailable(exc: BaseException) -> str:
    if permission_denied(exc):
        return "local service connection not permitted (sandbox?); run handoff outside the sandbox"
    return f"Executor endpoint unavailable ({exc}); run handoff server start"


def wait_timeout_payload(repo: Path, *, timeout_s: float, execution_id: str) -> dict[str, Any]:
    return {
        "unresolved": True,
        "wait_timeout": True,
        "timeout_s": timeout_s,
        "execution_id": execution_id,
        "run_still_in_progress": True,
        "message": (
            f"only this command stopped waiting after {timeout_s}s; the run is still in progress"
        ),
        "next": f'handoff resume "{repo}"',
        "status_command": f'handoff status "{repo}" --json',
    }


def _report_unresolved(repo: Path, exc: UnresolvedExecution, *, as_json: bool) -> int:
    if exc.details.get("wait_timeout"):
        payload = wait_timeout_payload(
            repo,
            timeout_s=float(exc.details.get("timeout_s") or 0),
            execution_id=exc.execution_id,
        )
        if as_json:
            print_json("unresolved", payload)
        else:
            _print_wait_timeout(repo, exc)
        return 2
    if as_json:
        return print_json_error("unresolved", str(exc), 2)
    print(f"handoff: unresolved: {exc}", file=sys.stderr)
    return 2


def _print_wait_timeout(repo: Path, exc: UnresolvedExecution) -> None:
    details = exc.details or {}
    timeout_s = details.get("timeout_s")
    execution_id = exc.execution_id
    print(
        "handoff: unresolved: wait timeout: only this command stopped waiting"
        + (f" after {timeout_s}s" if timeout_s is not None else "")
        + f"; the run is still in progress (execution {execution_id}). "
        + f'Next: handoff resume "{repo}" or handoff status "{repo}" --json',
        file=sys.stderr,
    )


def collect_preflight(repo: Path, config: HandoffConfig, settings: Any) -> Preflight:
    """Every dispatch precondition. Does not stop at the first blocker."""
    markdown = (repo / HANDOFF_NAME).read_text(encoding="utf-8")
    document = parse_handoff(markdown)
    workflow = require_workflow(repo)
    blockers: list[Blocker] = []
    notes: list[str] = []
    q = f'"{repo}"'
    if document.status not in ELIGIBLE_HANDOFF_STATUSES:
        blockers.append(
            Blocker(
                "status",
                f"Status is {document.status!r} (need READY FOR EXECUTION or CHANGES REQUESTED)",
                "set Status to READY FOR EXECUTION or CHANGES REQUESTED",
            )
        )
    if approved_plan_hash(markdown) != workflow.get("approved_plan_hash"):
        blockers.append(
            Blocker(
                "plan_hash",
                "HANDOFF plan does not match the approval receipt",
                f"handoff approve {q}",
            )
        )
    if workflow.get("workspace_id") != settings.workspace_id:
        blockers.append(
            Blocker(
                "workspace_id",
                "configured workspace_id does not match the approval receipt",
                f"handoff approve {q} after confirming the workspace",
            )
        )
    expected = str(document.branch or workflow.get("branch") or "")
    actual = ""
    exists = False
    dirty: list[str] = []
    baseline_clean = False
    create_branch = False
    git = GitWorkspace(settings.workspace_id, repo)
    try:
        git.require_git_checkout()
    except WorkspaceError as exc:
        blockers.append(Blocker("git_checkout", str(exc), "run handoff inside the git working tree"))
    else:
        actual = git.current_branch()
        exists = bool(expected) and git.local_branch_exists(expected)
        dirty = git.dirty_code_paths()
        fingerprint = git.code_fingerprint()
        baseline = workflow.get("post_run_fingerprint")
        first = not (workflow.get("consumed_execution_ids") or []) and not baseline
        if not baseline:
            baseline_clean = not dirty
            if dirty:
                blockers.append(Blocker("baseline", _dirty_message(dirty), _dirty_fix()))
        elif fingerprint != baseline:
            baseline_clean = False
            blockers.append(
                Blocker(
                    "fingerprint",
                    "workspace code does not match the last recorded post-run snapshot",
                    "restore the recorded snapshot, or revise Current Task and re-approve it to record the current workspace as the new baseline",
                )
            )
        else:
            baseline_clean = True
        approved = str(workflow.get("branch") or "")
        if actual == expected and actual == approved and actual:
            pass
        elif first and not exists and baseline_clean and not dirty:
            create_branch = True
            notes.append(f"execute will create branch {expected} from the current HEAD")
        elif first and not exists:
            notes.append(f"execute will create branch {expected} from HEAD once the code baseline is clean")
        elif exists and actual != expected:
            blockers.append(
                Blocker(
                    "branch",
                    f"checked-out branch {actual or '(detached)'} does not match approved branch {expected}",
                    f"git switch {expected}",
                )
            )
        elif not exists:
            blockers.append(
                Blocker(
                    "branch",
                    f"approved branch {expected} does not exist locally",
                    "create it only by hand if this is not the first execution; a later execution will not",
                )
            )
        else:
            blockers.append(
                Blocker(
                    "branch",
                    f"checked-out branch {actual or '(detached)'} does not match approved branch {expected}",
                    f"git switch {expected}",
                )
            )
    if load_outstanding(repo):
        blockers.append(
            Blocker(
                "outstanding",
                "outstanding A2A run exists",
                f"handoff resume {q} or handoff cancel {q}",
            )
        )
    if rounds_available(workflow) < 1:
        blockers.append(
            Blocker(
                "rounds",
                "execution round limit reached",
                "Planner must review remaining scope",
            )
        )
    if is_managed(repo):
        try:
            state = inspect_service(load_managed(repo))
        except ServiceError as exc:
            blockers.append(Blocker("service_stopped", str(exc), f"handoff server start {q}"))
        else:
            if state.probe == PROBE_NOT_PERMITTED:
                blockers.append(
                    Blocker("service_unknown", UNKNOWN_SERVICE, "run handoff outside the sandbox")
                )
            elif not state.running:
                blockers.append(
                    Blocker(
                        "service_stopped",
                        "managed service is stopped",
                        f"handoff server start {q}",
                    )
                )
    return Preflight(
        ready=not blockers,
        branch={"expected": expected, "actual": actual, "exists": exists},
        baseline_clean=baseline_clean,
        dirty_paths=dirty,
        blockers=blockers,
        notes=notes,
        create_branch=create_branch,
    )


def _die(message: str, code: int = 1) -> int:
    print(f"handoff: {message}", file=sys.stderr)
    return code


def _goal(markdown: str) -> str:
    lines = markdown.splitlines()
    found = False
    for line in lines:
        if line.startswith("### Goal"):
            found = True
            continue
        if found and line.strip():
            return line.strip()
    return ""


def _turn(status: str) -> str:
    if status in {"READY FOR EXECUTION", "CHANGES REQUESTED"}:
        return "EXECUTOR"
    if status == READY_FOR_QA:
        return "PLANNER"
    if status == "APPROVED":
        return "HUMAN"
    if status == "DRAFT":
        return "HUMAN"
    return "UNKNOWN"


def _artifact_data(task: dict[str, Any] | None, name: str) -> dict[str, Any] | None:
    if not task:
        return None
    for artifact in task.get("artifacts") or []:
        if artifact.get("name") != name:
            continue
        for part in artifact.get("parts") or []:
            data = part.get("data")
            if isinstance(data, dict):
                return data
    return None


def _card_supports_fingerprint(card: Any) -> bool:
    for ext in card.capabilities.extensions:
        if ext.uri != CODING_TASK_PROFILE:
            continue
        params = MessageToDict(ext.params) if ext.HasField("params") else {}
        return bool(params.get(WORKSPACE_FINGERPRINT_PARAM))
    return False


def _card_params(card: Any) -> dict[str, Any]:
    for ext in card.capabilities.extensions:
        if ext.uri == CODING_TASK_PROFILE:
            return MessageToDict(ext.params) if ext.HasField("params") else {}
    return {}


def executor_identity(card: Any) -> dict[str, Any]:
    """Identity the endpoint advertised when this run was submitted (immutable per run)."""
    params = _card_params(card)
    generation = params.get("config_generation")
    if isinstance(generation, float) and generation.is_integer():
        generation = int(generation)
    return {
        "provider": params.get("executor_provider"),
        "model": params.get("executor_model"),
        "config_generation": generation,
        "endpoint_name": card.name,
    }


def verify_managed_card(repo: Path, card: Any) -> None:
    """A managed service must be running exactly the selected configuration."""
    expected = managed_expected_card(repo)
    if expected is None:
        return
    params = _card_params(card)
    if isinstance(params.get("config_generation"), float):
        params["config_generation"] = int(params["config_generation"])
    wrong = [key for key, value in expected.items() if params.get(key) != value]
    if wrong:
        raise DispatchDeferred(
            "the managed service is not running the selected Executor "
            f"(mismatch: {', '.join(wrong)}); run handoff server start (or handoff model to inspect)"
        )


def _lock_owner(repo: Path) -> dict[str, Any] | None:
    path = repo / LOG_DIRNAME / LOCK_DIRNAME / "owner.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def worker_stop_confirmed(
    repo: Path,
    task: dict[str, Any] | None,
    execution_id: str,
    result: dict[str, Any] | None,
) -> bool:
    if not task:
        return False
    if _recovery_required(task):
        return False
    state = (task.get("status") or {}).get("state")
    if state not in TERMINAL_TASK_STATES:
        return False
    life = _artifact_data(task, WORKER_LIFECYCLE_ARTIFACT) or {}
    if life.get("worker_stopped") is True or life.get("lock_held") is False:
        return True
    if result is not None and result.get("worker_launched") is False:
        return True
    if state in {"TASK_STATE_REJECTED", "REJECTED"}:
        return True
    lock = repo / LOG_DIRNAME / LOCK_DIRNAME
    if not lock.exists():
        return True
    owner = _lock_owner(repo)
    if owner and owner.get("execution_id") == execution_id:
        return False
    return False


def _pause_if_held() -> None:
    raw = os.environ.get(HOLD_AFTER_RECORD)
    if not raw:
        return
    path = Path(raw)
    while path.exists():
        time.sleep(0.05)


async def _connect(settings: Any) -> CodingClient:
    credential = settings.credential_file.read_text(encoding="utf-8").strip()
    if not credential:
        raise IntegrationError("credential-file is empty")
    client = CodingClient(
        settings.agent_card_url,
        credential,
        timeout=settings.request_timeout_s,
    )
    card_json = await client.connect()
    assert client._card is not None
    verify_agent_card(client._card, client.agent_card_url)
    if not card_supports_durable_dedup(client._card):
        await client.close()
        raise IntegrationError(
            "endpoint does not advertise durable_execution_id_deduplication"
        )
    if not _card_supports_fingerprint(client._card):
        await client.close()
        raise IntegrationError(
            "endpoint does not advertise workspace_code_fingerprint; refusing integrated CLI"
        )
    _ = card_json
    return client


def _write_log(repo: Path, run_id: str, text: str) -> None:
    path = log_path(repo, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text if text.endswith("\n") else text + "\n")


def _next_action(
    *,
    markdown_status: str,
    execution: str,
    workflow: dict[str, Any] | None,
) -> str:
    if execution in {"WORKING", "SUBMITTED", "UNRESOLVED"}:
        return "wait or run handoff resume"
    if execution == "RECOVERY":
        return "inspect recovery_required; do not dispatch again"
    last = (workflow or {}).get("last_outcome") or {}
    if (
        workflow
        and workflow.get("dispatch_hold")
        and last.get("outcome") in {"failed", "canceled"}
        and execution in {"IDLE", "FAILED", "CANCELED"}
    ):
        if exhausted(workflow):
            return "Planner scope review; archive --superseded if a smaller successor is needed"
        return (
            f"Planner review of {last['outcome']} delivery (not QA); "
            "explicit handoff execute only after review (watch will not retry)"
        )
    if workflow and exhausted(workflow) and execution in {"IDLE", "FAILED", "CANCELED"}:
        return "Planner scope review; archive --superseded if a smaller successor is needed"
    if markdown_status == READY_FOR_QA and execution in {"COMPLETED", "IDLE"}:
        return "Planner QA"
    if markdown_status in ELIGIBLE_HANDOFF_STATUSES:
        if workflow and workflow.get("dispatch_hold") and execution != "WORKING":
            return "explicit execute of a reviewed correction (watch will not retry)"
        return "handoff execute"
    if markdown_status == "DRAFT":
        return "handoff approve after chat approval"
    if markdown_status == "APPROVED":
        return "human merge; handoff archive"
    return "handoff status"


def cmd_approve(repo: Path) -> int:
    _config, settings = require_a2a_config(repo)
    before = load_workflow(repo)
    git = GitWorkspace(settings.workspace_id, repo)
    workflow = record_approval(
        repo,
        workspace_id=settings.workspace_id,
        max_rounds=_config.max_rounds,
        current_fingerprint=git.code_fingerprint(),
        current_branch=git.current_branch(),
        dirty_code_paths=git.dirty_code_paths(),
    )
    print(f"approved workflow {workflow['workflow_id']}")
    print(f"plan_hash {workflow['approved_plan_hash']}")
    if workflow.get("parent_workflow_id"):
        print(f"parent_workflow_id {workflow['parent_workflow_id']}")
    previous_stamp = None if before is None else before.get("rebaselined_at")
    if workflow.get("rebaselined_at") and workflow.get("rebaselined_at") != previous_stamp:
        previous = str(workflow.get("previous_post_run_fingerprint") or "")
        print(f"rebaselined code snapshot (previous {previous[:12]})")
    return 0


def _prepare_request(
    repo: Path, config: HandoffConfig, settings: Any, *, check_only: bool = False
) -> tuple[dict[str, Any], dict[str, Any], Any]:
    markdown = (repo / HANDOFF_NAME).read_text(encoding="utf-8")
    document = parse_handoff(markdown)
    workflow = require_workflow(repo)
    preflight = collect_preflight(repo, config, settings)
    raise_preflight(preflight)
    git = GitWorkspace(settings.workspace_id, repo)
    created = None
    if preflight.create_branch and not check_only:
        git.create_branch_from_head(str(preflight.branch["expected"]))
        created = preflight.branch["expected"]
    if workflow.get("dispatch_hold") and not check_only:
        workflow = set_dispatch_hold(repo, workflow, False)
    if check_only:
        return {}, workflow, None
    branch = git.current_branch()
    fingerprint = git.code_fingerprint()
    execution_id = str(uuid.uuid4())
    iteration = len(workflow.get("consumed_execution_ids") or []) + 1
    run_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"
    request = parse_coding_request(
        {
            "schema": CODING_TASK_PROFILE,
            "workflow_id": workflow["workflow_id"],
            "run_id": run_id,
            "execution_id": execution_id,
            "iteration": iteration,
            "workspace_id": settings.workspace_id,
            "expected_branch": branch,
            "expected_head": git.current_head(),
            "handoff_markdown": markdown,
            "request_sha256": snapshot_sha256(markdown),
            "expected_code_fingerprint": fingerprint,
        }
    )
    record = initial_run_record(
        request,
        agent_card_url=normalize_agent_card_url(settings.agent_card_url),
        credential_file=settings.credential_file,
        workspace_id=settings.workspace_id,
    )
    record_path = run_record_path(repo, execution_id)
    persist_run_record(record_path, record, latest_repo=repo)
    request_snapshot_path(repo, run_id).write_text(
        json.dumps(request.to_dict(), indent=2) + "\n", encoding="utf-8"
    )
    reserve_round(repo, workflow, execution_id)
    save_outstanding(
        repo,
        {
            "transport": "a2a",
            "execution_id": execution_id,
            "run_id": run_id,
            "workflow_id": workflow["workflow_id"],
            "run_record": str(record_path),
            "agent_card_url": record["agent_card_url"],
            "credential_file": str(settings.credential_file),
            "created_at": utc_now(),
            **({"branch_created": created} if created else {}),
        },
    )
    append_event(repo, run_id, "submitted", {"execution_id": execution_id})
    _write_log(repo, run_id, f"=== Executor run {run_id} ===\nsubmitted {execution_id}\n")
    return record, workflow, request


def _set_ready_for_qa(repo: Path) -> None:
    path = repo / HANDOFF_NAME
    text = path.read_text(encoding="utf-8")
    if parse_handoff(text).status != READY_FOR_QA:
        path.write_text(replace_status(text, READY_FOR_QA), encoding="utf-8")


def _submitted_status(outstanding: dict[str, Any]) -> str | None:
    try:
        record = load_run_record(Path(str(outstanding["run_record"])))
        markdown = (record.get("request") or {}).get("handoff_markdown")
    except (OSError, ValueError, KeyError, ClientError):
        return None
    if not isinstance(markdown, str):
        return None
    return parse_handoff(markdown).status or None


def _restore_submitted_status(repo: Path, outstanding: dict[str, Any]) -> str | None:
    """Undo an executor-written Status after an unsuccessful delivery.

    Only a valid COMPLETED result may reach READY FOR QA; a failed or canceled
    worker's early READY FOR QA (or APPROVED) must not route the Planner to QA
    or the human to merge. Notes and code are left untouched as evidence.
    """
    submitted = _submitted_status(outstanding)
    path = repo / HANDOFF_NAME
    text = path.read_text(encoding="utf-8")
    current = parse_handoff(text).status
    if submitted and current != submitted:
        path.write_text(replace_status(text, submitted), encoding="utf-8")
        return current
    return None


def _reconcile(
    repo: Path,
    *,
    outstanding: dict[str, Any],
    task: dict[str, Any] | None,
    result: dict[str, Any] | None,
    unresolved: bool,
    reason: str | None,
) -> dict[str, Any]:
    workflow = load_workflow(repo) or {}
    run_id = str(outstanding.get("run_id") or "")
    execution_id = str(outstanding.get("execution_id") or "")
    state = (task.get("status") or {}).get("state") if task else None
    recovery = bool(task and _recovery_required(task))
    started = bool((result or {}).get("worker_launched")) or bool(
        (_artifact_data(task, WORKER_LIFECYCLE_ARTIFACT) or {}).get("worker_started")
    )
    if result:
        result_path(repo, run_id).write_text(
            json.dumps(result, indent=2, default=str) + "\n", encoding="utf-8"
        )
    stopped = worker_stop_confirmed(repo, task, execution_id, result)
    outcome = "unresolved"
    if recovery:
        outcome = "recovery_required"
    elif state in {"TASK_STATE_COMPLETED", "COMPLETED"} and stopped:
        outcome = "completed"
    elif state in {"TASK_STATE_CANCELED", "CANCELED", "CANCELLED"} and stopped:
        outcome = "canceled"
    elif state in {"TASK_STATE_REJECTED", "REJECTED"} and stopped:
        outcome = "rejected"
    elif state in {"TASK_STATE_FAILED", "FAILED"} and stopped:
        outcome = "failed"
    git = GitWorkspace(str(workflow.get("workspace_id") or "local"), repo)
    try:
        snap = git.snapshot()
        fingerprint = git.code_fingerprint()
    except WorkspaceError:
        snap = None
        fingerprint = None
    manifest = load_manifest(repo, run_id) or {}
    manifest.update(
        {
            "run_id": run_id,
            "workflow_id": outstanding.get("workflow_id"),
            "execution_id": execution_id,
            "task_id": (task or {}).get("id") or outstanding.get("task_id"),
            "context_id": (task or {}).get("contextId") or (task or {}).get("context_id"),
            "transport": "a2a",
            "endpoint": outstanding.get("agent_card_url"),
            "endpoint_name": outstanding.get("agent_name") or manifest.get("endpoint_name"),
            "executor": {
                **(outstanding.get("executor") or manifest.get("executor") or {}),
                "reported_model": (result or {}).get("executor_reported_model")
                or (manifest.get("executor") or {}).get("reported_model"),
            },
            "started_at": outstanding.get("created_at") or manifest.get("started_at"),
            "finished_at": utc_now() if stopped and not unresolved else None,
            "outcome": outcome,
            "reason": reason or (result or {}).get("reason"),
            "branch_created": outstanding.get("branch_created"),
            "boundary_note": (result or {}).get("boundary_note"),
            "status_after": parse_handoff((repo / HANDOFF_NAME).read_text(encoding="utf-8")).status,
            "git": {
                "branch": None if snap is None else snap.branch,
                "head": None if snap is None else snap.head,
                "snapshot": fingerprint,
                "commits": (result or {}).get("commits") or [],
            },
            "evidence": {
                "run_record": outstanding.get("run_record"),
                "evidence_dir": (result or {}).get("evidence_dir"),
                "events": str(repo / LOG_DIRNAME / f"{run_id}-events.jsonl"),
            },
            **usage_fields(result),
        }
    )
    write_manifest(repo, manifest)
    append_event(repo, run_id, "reconcile", {"outcome": outcome, "state": state})
    if outcome == "rejected":
        release_reservation(repo, workflow, execution_id)
        clear_outstanding(repo)
        return manifest
    if started or outcome in {"completed", "failed", "canceled"}:
        consume_round(repo, workflow, execution_id)
        workflow = load_workflow(repo) or workflow
    if outcome in {"failed", "canceled", "recovery_required"}:
        set_dispatch_hold(repo, workflow, True)
        workflow = load_workflow(repo) or workflow
    if outcome == "completed":
        ids_ok = True
        if result:
            ids_ok = (
                result.get("execution_id") == execution_id
                and result.get("workflow_id") == outstanding.get("workflow_id")
                and result.get("run_id") == run_id
            )
        plan_ok = bool(result.get("plan_intact", True)) if result else False
        if ids_ok and plan_ok and stopped:
            _set_ready_for_qa(repo)
            workflow["last_outcome"] = {
                "execution_id": execution_id,
                "run_id": run_id,
                "outcome": outcome,
                "reason": None,
            }
            if fingerprint:
                workflow["post_run_fingerprint"] = fingerprint
                workflow["post_run_branch"] = None if snap is None else snap.branch
            save_workflow(repo, workflow)
            clear_outstanding(repo)
            manifest["status_after"] = READY_FOR_QA
            write_manifest(repo, manifest)
        return manifest
    if stopped and outcome in {"failed", "canceled"}:
        overwritten = _restore_submitted_status(repo, outstanding)
        workflow["last_outcome"] = {
            "execution_id": execution_id,
            "run_id": run_id,
            "outcome": outcome,
            "reason": manifest.get("reason"),
            "executor_status_discarded": overwritten,
        }
        if fingerprint:
            workflow["post_run_fingerprint"] = fingerprint
            workflow["post_run_branch"] = None if snap is None else snap.branch
        save_workflow(repo, workflow)
        clear_outstanding(repo)
        if overwritten:
            manifest["executor_status_discarded"] = overwritten
            manifest["status_after"] = parse_handoff((repo / HANDOFF_NAME).read_text(encoding="utf-8")).status
            write_manifest(repo, manifest)
            append_event(repo, run_id, "status_restored", {"discarded": overwritten})
    return manifest


async def _wait_task(client: CodingClient, task_id: str, settings: Any) -> dict[str, Any]:
    return await client.wait(
        task_id,
        timeout=settings.wait_timeout_s,
        interval=settings.poll_interval_s,
    )


def dispatch_gate(repo: Path) -> None:
    """Apply a queued Executor selection (managed repos) before a new worker can start."""
    if not is_managed(repo):
        return
    try:
        outcome = apply_pending_selection(repo)
    except SelectionError as exc:
        raise DispatchDeferred(str(exc)) from exc
    except SetupError as exc:
        raise IntegrationError(str(exc)) from exc
    if outcome == "deferred":
        raise DispatchDeferred("a queued Executor change is waiting for the service to be idle")


async def cmd_execute_async(repo: Path, *, from_watch: bool = False, as_json: bool = False) -> int:
    require_a2a_config(repo)
    if from_watch:
        workflow = load_workflow(repo)
        if workflow and workflow.get("dispatch_hold"):
            raise IntegrationError("watch will not clear a dispatch hold; use explicit execute")
    dispatch_gate(repo)
    try:
        lock = submit_lock(repo)
        lock.acquire()
    except WorkspaceBusy as exc:
        raise DispatchDeferred(f"submission or service change in progress: {exc}") from exc
    client: CodingClient | None = None
    try:
        # Snapshot the effective configuration under the lock that a model
        # switch also takes, and verify the endpoint before anything is saved.
        config, settings = require_a2a_config(repo)
        _prepare_request(repo, config, settings, check_only=True)
        try:
            client = await _connect(settings)
        except (httpx.HTTPError, OSError) as exc:
            # A denied connect and a stopped service both defer: watch retries.
            raise DispatchDeferred(endpoint_unavailable(exc)) from exc
        verify_managed_card(repo, client._card)
        record, _workflow, request = _prepare_request(repo, config, settings)
    except BaseException:
        if client is not None:
            await client.close()
        lock.release()
        raise
    lock.release()
    _pause_if_held()
    record_path = Path(str(run_record_path(repo, request.execution_id)))
    outstanding = load_outstanding(repo) or {}
    try:
        assert client._card is not None
        identity = executor_identity(client._card)
        outstanding["agent_name"] = client._card.name
        outstanding["executor"] = identity
        save_outstanding(repo, outstanding)
        record["executor"] = identity
        persist_run_record(record_path, record, latest_repo=repo)
        task = await client.submit(request)
        record = _update_record_from_task(record, task)
        persist_run_record(record_path, record, latest_repo=repo)
        outstanding["task_id"] = task.get("id")
        save_outstanding(repo, outstanding)
        append_event(repo, request.run_id, "acked", {"task_id": task.get("id")})
        task_id = task.get("id")
        if not isinstance(task_id, str) or not task_id:
            raise IntegrationError("submit returned a task without an id")
        try:
            terminal = await _wait_task(client, task_id, settings)
        except TimeoutError as exc:
            raise UnresolvedExecution(
                "wait timeout",
                task_id=task_id,
                execution_id=request.execution_id,
                details={"wait_timeout": True, "timeout_s": settings.wait_timeout_s},
            ) from exc
        result = coding_result_from_task(terminal)
        record = _update_record_from_task(record, terminal)
        persist_run_record(record_path, record, latest_repo=repo)
        manifest = _reconcile(
            repo,
            outstanding=load_outstanding(repo) or outstanding,
            task=terminal,
            result=result,
            unresolved=False,
            reason=task_reason(terminal),
        )
        payload = {"run_id": request.run_id, "outcome": manifest.get("outcome"), "task_id": task_id}
        if outstanding.get("branch_created"):
            payload["branch_created"] = outstanding["branch_created"]
            print(f"created branch {outstanding['branch_created']} from HEAD")
        print(json.dumps(payload, indent=2))
        return execute_exit_code((terminal.get("status") or {}).get("state"))
    except UnresolvedExecution as exc:
        _reconcile(
            repo,
            outstanding=load_outstanding(repo) or outstanding,
            task=None,
            result=None,
            unresolved=True,
            reason=str(exc),
        )
        return _report_unresolved(repo, exc, as_json=as_json)
    finally:
        await client.close()


def _saved_credential(outstanding: dict[str, Any]) -> Path:
    """Credential reference saved with the run; the current config is never consulted."""
    raw = outstanding.get("credential_file")
    if not raw:
        try:
            raw = load_run_record(Path(str(outstanding["run_record"]))).get("credential_file")
        except (OSError, ValueError, KeyError, ClientError):
            raw = None
    if not raw:
        raise UnresolvedExecution(
            "saved run has no credential reference; cannot reach its endpoint",
            task_id=outstanding.get("task_id"),
            execution_id=str(outstanding.get("execution_id") or ""),
        )
    path = Path(str(raw))
    if not path.is_file():
        raise UnresolvedExecution(
            f"saved credential reference is unavailable: {path}",
            task_id=outstanding.get("task_id"),
            execution_id=str(outstanding.get("execution_id") or ""),
        )
    return path


async def cmd_resume_async(repo: Path, *, as_json: bool = False) -> int:
    outstanding = load_outstanding(repo)
    if outstanding is None:
        # Without an outstanding run, resume is only meaningful for A2A repos.
        require_a2a_config(repo)
        raise IntegrationError("no outstanding A2A run to resume")
    timing = recovery_timing(repo)
    record_path = Path(str(outstanding["run_record"]))
    try:
        payload = await resume_from_record(
            record_path=record_path,
            credential_file=_saved_credential(outstanding),
            timeout=timing.wait_timeout_s,
        )
    except UnresolvedExecution as exc:
        _reconcile(repo, outstanding=outstanding, task=None, result=None, unresolved=True, reason=str(exc))
        return _report_unresolved(repo, exc, as_json=as_json)
    task = payload.get("task") or {}
    result = payload.get("result")
    manifest = _reconcile(
        repo,
        outstanding=outstanding,
        task=task if isinstance(task, dict) else None,
        result=result if isinstance(result, dict) else None,
        unresolved=False,
        reason=payload.get("reason"),
    )
    print(json.dumps({"run_id": outstanding.get("run_id"), "outcome": manifest.get("outcome")}, indent=2))
    return execute_exit_code((task.get("status") or {}).get("state") if isinstance(task, dict) else None)


async def cmd_cancel_async(repo: Path) -> int:
    outstanding = load_outstanding(repo)
    if outstanding is None:
        require_a2a_config(repo)
        raise IntegrationError("no outstanding A2A run to cancel")
    record_path = Path(str(outstanding["run_record"]))
    try:
        payload = await cancel_from_record(
            record_path=record_path,
            credential_file=_saved_credential(outstanding),
            timeout=recovery_timing(repo).request_timeout_s,
        )
    except UnresolvedExecution as exc:
        _reconcile(repo, outstanding=outstanding, task=None, result=None, unresolved=True, reason=str(exc))
        print(f"handoff: unresolved: {exc}", file=sys.stderr)
        return 2
    task = payload.get("task") or {}
    result = coding_result_from_task(task) if isinstance(task, dict) else None
    manifest = _reconcile(
        repo,
        outstanding=outstanding,
        task=task if isinstance(task, dict) else None,
        result=result,
        unresolved=False,
        reason=payload.get("reason"),
    )
    print(json.dumps({"run_id": outstanding.get("run_id"), "outcome": manifest.get("outcome")}, indent=2))
    return 1


async def cmd_status_async(repo: Path, *, as_json: bool = False) -> int:
    markdown = (repo / HANDOFF_NAME).read_text(encoding="utf-8")
    document = parse_handoff(markdown)
    workflow = load_workflow(repo)
    outstanding = load_outstanding(repo)
    execution = "IDLE"
    reason = None
    if outstanding:
        execution = "UNRESOLVED"
        record_path = Path(str(outstanding["run_record"]))
        try:
            cred = _saved_credential(outstanding)
            payload = await status_from_record(
                record_path=record_path,
                credential_file=cred,
                timeout=recovery_timing(repo).request_timeout_s,
            )
            state = payload.get("state")
            task = payload.get("task") if isinstance(payload.get("task"), dict) else {
                "id": payload.get("task_id"),
                "status": {"state": state},
                "artifacts": [],
            }
            if payload.get("recovery_required"):
                execution = "RECOVERY"
            elif state in TERMINAL_TASK_STATES:
                _reconcile(
                    repo,
                    outstanding=outstanding,
                    task=task,
                    result=payload.get("result") if isinstance(payload.get("result"), dict) else None,
                    unresolved=False,
                    reason=payload.get("reason"),
                )
                outstanding = load_outstanding(repo)
                document = parse_handoff((repo / HANDOFF_NAME).read_text(encoding="utf-8"))
                markdown = (repo / HANDOFF_NAME).read_text(encoding="utf-8")
                workflow = load_workflow(repo)
                if outstanding:
                    execution = "UNRESOLVED"
                elif state in {"TASK_STATE_COMPLETED", "COMPLETED"}:
                    execution = "COMPLETED"
                elif state in {"TASK_STATE_CANCELED", "CANCELED", "CANCELLED"}:
                    execution = "CANCELED"
                else:
                    execution = "FAILED"
            elif state in INTERRUPTED_TASK_STATES:
                execution = "RECOVERY"
            else:
                execution = "WORKING"
            reason = payload.get("reason")
        except Exception as exc:  # noqa: BLE001 — status must stay honest when the endpoint is down
            if permission_denied(exc):
                reason = "local service connection not permitted (sandbox?); run handoff outside the sandbox"
            else:
                reason = f"saved endpoint or credential unavailable: {exc}"
            execution = "UNRESOLVED"
    turn = _turn(document.status)
    last = (workflow or {}).get("last_outcome") or {}
    if (
        workflow
        and workflow.get("dispatch_hold")
        and last.get("outcome") in {"failed", "canceled"}
        and not outstanding
    ):
        turn = "PLANNER"
    if execution in {"WORKING", "SUBMITTED", "UNRESOLVED", "RECOVERY"}:
        turn = "WAIT" if execution != "RECOVERY" else "RECOVERY"
    transport, managed, mode = run_mode(repo)
    watcher = watcher_state(repo)
    next_action = _mode_next(
        _next_action(markdown_status=document.status, execution=execution, workflow=workflow), mode, watcher, repo
    )
    code = 2 if execution in {"UNRESOLVED", "RECOVERY"} else 0
    if as_json:
        print_json(
            "status",
            {
                "repo": str(repo),
                "transport": transport,
                "managed": managed,
                "status": document.status,
                "turn": turn,
                "goal": _goal(markdown),
                "execution": execution,
                "workflow": _workflow_json(workflow, markdown),
                "mode": mode,
                "watcher": watcher,
                "executor": _executor_json(repo) if managed else None,
                "run": None
                if not outstanding
                else {"run_id": outstanding.get("run_id"), "task_id": outstanding.get("task_id")},
                "reason": reason,
                "next": next_action,
                "preflight": _status_preflight(repo),
            },
        )
        return code
    print(f"repo:   {repo}")
    print(f"status: {document.status}")
    print(f"turn:   {turn}")
    print(f"goal:   {_goal(markdown)}")
    print(f"execution: {execution}")
    if workflow:
        print(f"workflow: {workflow.get('workflow_id')}")
        print(
            f"rounds: {workflow.get('rounds_used')}/{workflow.get('max_rounds')}"
            + (f" reserved={workflow.get('reserved_execution_id')}" if workflow.get("reserved_execution_id") else "")
        )
        if workflow.get("dispatch_hold"):
            print("hold:   automatic dispatch blocked")
        last = workflow.get("last_outcome") or {}
        if last.get("outcome"):
            detail = f" ({last['reason']})" if last.get("reason") else ""
            print(f"last:   {last['outcome']} run {last.get('run_id')}{detail}")
            if last.get("executor_status_discarded"):
                print(f"        executor-written Status {last['executor_status_discarded']!r} was not accepted")
        if workflow.get("approved_plan_hash") and approved_plan_hash(markdown) != workflow.get("approved_plan_hash"):
            print("plan:   changed since approval; re-approval required before execution")
        if workflow.get("parent_workflow_id"):
            print(f"parent: {workflow.get('parent_workflow_id')}")
    if managed:
        print(f"mode:   {mode or 'not set'}")
        if watcher["running"]:
            print(f"watcher: running, pid {watcher['pid']} since {watcher['started_at']}")
        elif watcher["stale"]:
            print("watcher: not running (stale watcher record ignored)")
        elif mode == "watch":
            print("watcher: not running")
    for line in managed_status_lines(repo):
        print(line)
    if outstanding:
        print(f"run:    {outstanding.get('run_id')}")
        print(f"task:   {outstanding.get('task_id') or '(ack unknown; resume to retransmit)'}")
    if reason:
        print(f"reason: {reason}")
    preflight = _status_preflight(repo)
    if isinstance(preflight, dict) and not preflight.get("ready"):
        print("preflight: blocked")
        for item in preflight.get("blockers") or []:
            print(f"  - {item['message']}")
            print(f"    fix: {item['fix']}")
    print(f"next:   {next_action}")
    return code


def _status_preflight(repo: Path) -> dict[str, Any] | None:
    if load_workflow(repo) is None:
        return None
    try:
        _config, settings = require_a2a_config(repo)
        return collect_preflight(repo, _config, settings).to_json()
    except (ConfigError, WorkflowError, WorkspaceError, OSError):
        return None


def _mode_next(action: str, mode: str | None, watcher: dict[str, Any], repo: Path) -> str:
    """Run-mode-aware guidance; unchanged when no mode is recorded."""
    if mode == "drive" and action == "handoff execute":
        return "handoff execute (drive: the Planner runs execute, then QA)"
    if mode == "watch" and action == "handoff execute":
        if watcher.get("running"):
            return "handoff watch dispatches (watch mode); the Planner does QA"
        return f'start the watcher: handoff watch "{repo}" (watch mode; the Planner does QA)'
    if mode == "watch" and action == "Planner QA":
        return "Planner QA (watch dispatches; the Planner does QA)"
    if mode == "drive" and action == "Planner QA":
        return "Planner QA (drive: the Planner runs execute, then QA)"
    return action


def _workflow_json(workflow: dict[str, Any] | None, markdown: str) -> dict[str, Any] | None:
    if not workflow:
        return None
    last = workflow.get("last_outcome") or {}
    return {
        "workflow_id": workflow.get("workflow_id"),
        "rounds_used": workflow.get("rounds_used"),
        "max_rounds": workflow.get("max_rounds"),
        "reserved_execution_id": workflow.get("reserved_execution_id"),
        "dispatch_hold": bool(workflow.get("dispatch_hold")),
        "last_outcome": {k: last.get(k) for k in ("outcome", "run_id", "reason", "executor_status_discarded")}
        if last.get("outcome")
        else None,
        "plan_changed_since_approval": bool(
            workflow.get("approved_plan_hash") and approved_plan_hash(markdown) != workflow.get("approved_plan_hash")
        ),
        "parent_workflow_id": workflow.get("parent_workflow_id"),
    }


def _executor_json(repo: Path) -> dict[str, Any] | None:
    try:
        described = managed_describe(load_managed(repo))
    except (SetupError, OSError) as exc:
        return {"error": str(exc)}
    described.pop("repo", None)
    return described


# Refusals watch reports and keeps observing through (e.g. config switched mid-watch).
_WATCH_ERRORS = (IntegrationError, ConfigError, WorkflowError, ClientError, WorkspaceBusy, WorkspaceError, SetupError)


def _idle_selection_tick(repo: Path, last_message: str | None) -> str | None:
    """Apply a queued selection once ownership is certain; report changes once."""
    if not is_managed(repo):
        return None
    try:
        apply_pending_selection(repo)
    except (SelectionError, SetupError) as exc:
        message = f"handoff: {exc}"
        if message != last_message:
            print(message, file=sys.stderr)
        return message
    return None


def _drive_refusal(repo: Path) -> str:
    return (
        "run mode is drive: the Planner runs handoff execute and QA itself, so handoff watch does not start. "
        f'To use a watcher instead: handoff mode "{repo}" watch'
    )


def _terminate(_signum: int, _frame: Any) -> None:
    raise SystemExit(143)


async def cmd_watch_async(repo: Path, interval: float) -> int:
    if run_mode(repo)[2] == "drive":
        raise IntegrationError(_drive_refusal(repo), exit_code=2)
    record = claim_watcher(repo, interval)
    previous = signal.signal(signal.SIGTERM, _terminate)
    try:
        return await _watch_loop(repo, interval)
    finally:
        signal.signal(signal.SIGTERM, previous)
        release_watcher(repo, record)


async def _watch_loop(repo: Path, interval: float) -> int:
    last_status = ""
    last_notified = None
    last_deferred: str | None = None
    print(f"watching {repo}/HANDOFF.md every {int(interval)}s (Ctrl-C to stop)")
    while True:
        outstanding = load_outstanding(repo)
        if not outstanding and run_mode(repo)[2] == "drive":
            # Poll boundary with nothing in flight: the switch takes effect
            # here, after any run this watcher dispatched was reconciled.
            print(f"run mode changed to drive; handoff watch stops (no run in flight). To resume watching: handoff mode \"{repo}\" watch")
            return 0
        if not outstanding:
            last_deferred = _idle_selection_tick(repo, last_deferred) or last_deferred
        if outstanding:
            await cmd_status_async(repo)
            record_path = Path(str(outstanding["run_record"]))
            record = load_run_record(record_path)
            if not record.get("task_id"):
                print("handoff: acknowledgement unknown; run handoff resume (watch will not retransmit)")
            else:
                try:
                    await cmd_resume_async(repo)
                except _WATCH_ERRORS as exc:
                    print(f"handoff: {exc}", file=sys.stderr)
            workflow = load_workflow(repo) or {}
            still = load_outstanding(repo)
            document = parse_handoff((repo / HANDOFF_NAME).read_text(encoding="utf-8"))
            if (
                not still
                and document.status == READY_FOR_QA
                and workflow.get("last_notified_execution_id") != outstanding.get("execution_id")
            ):
                print(f"NOTIFY: Handoff: {READY_FOR_QA} — {_goal((repo / HANDOFF_NAME).read_text(encoding='utf-8'))}")
                workflow["last_notified_execution_id"] = outstanding.get("execution_id")
                save_workflow(repo, workflow)
            last_status = document.status
            time.sleep(interval)
            continue
        document = parse_handoff((repo / HANDOFF_NAME).read_text(encoding="utf-8"))
        workflow = load_workflow(repo)
        status = document.status
        if status != last_status:
            if last_deferred is None or not last_deferred.startswith("handoff: dispatch deferred"):
                print(f"[{time.strftime('%H:%M:%S')}] status: {status} ({_turn(status)}'s turn)")
            if status in ELIGIBLE_HANDOFF_STATUSES:
                if workflow and workflow.get("dispatch_hold"):
                    print("execute held after failed delivery; watch will not retry")
                elif workflow and exhausted(workflow):
                    print("round limit reached; Planner scope review is the next action")
                else:
                    try:
                        await cmd_execute_async(repo, from_watch=True)
                    except DispatchDeferred as exc:
                        message = f"handoff: dispatch deferred: {exc}"
                        if message != last_deferred:
                            print(message, file=sys.stderr)
                            print("watch will retry at the next poll")
                        last_deferred = message
                        time.sleep(interval)
                        continue  # keep last_status: retry this dispatch
                    except _WATCH_ERRORS as exc:
                        print(f"handoff: {exc}", file=sys.stderr)
                        print("execute failed; watching for the next status change")
                    last_deferred = None
            elif status in {READY_FOR_QA, "APPROVED"}:
                key = (status, (workflow or {}).get("workflow_id"))
                if key != last_notified:
                    print(f"NOTIFY: Handoff: {status} — {_goal((repo / HANDOFF_NAME).read_text(encoding='utf-8'))}")
                    last_notified = key
            last_status = status
        time.sleep(interval)


def cmd_preflight(repo: Path, *, as_json: bool) -> int:
    config, settings = require_a2a_config(repo)
    if config.transport != "a2a":
        raise IntegrationError("preflight needs managed A2A")
    preflight = collect_preflight(repo, config, settings)
    if as_json:
        print_json("preflight", preflight.to_json())
    else:
        if preflight.ready:
            print("preflight: ready")
            for note in preflight.notes:
                print(f"note: {note}")
        else:
            print(format_blockers(preflight))
    return 0 if preflight.ready else 1


def cmd_archive(repo: Path, *, superseded: bool) -> int:
    config = None
    try:
        config, _settings = require_a2a_config(repo)
    except ConfigError:
        config = None
    if config is None or config.transport != "a2a":
        raise IntegrationError("archive --superseded is an A2A workflow transition")
    goal = _goal((repo / HANDOFF_NAME).read_text(encoding="utf-8"))
    archive_current(repo, superseded=superseded, goal=goal)
    print(f"archived '{goal}' ({'superseded' if superseded else 'approved'})")
    return 0


def _repo(path: str) -> Path:
    repo = Path(path).expanduser().resolve()
    if not (repo / HANDOFF_NAME).is_file():
        raise IntegrationError(f"no {HANDOFF_NAME} in {repo}")
    return repo


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="handoff-a2a cli")
    parser.add_argument("--repo", required=True, type=str)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("approve")
    execute = sub.add_parser("execute")
    execute.add_argument("--json", action="store_true")
    resume = sub.add_parser("resume")
    resume.add_argument("--json", action="store_true")
    preflight = sub.add_parser("preflight")
    preflight.add_argument("--json", action="store_true")
    sub.add_parser("cancel")
    status = sub.add_parser("status")
    status.add_argument("--json", action="store_true")
    watch = sub.add_parser("watch")
    watch.add_argument("--interval", type=float, default=30.0)
    archive = sub.add_parser("archive")
    archive.add_argument("--superseded", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        repo = _repo(args.repo)
        if args.command == "approve":
            return cmd_approve(repo)
        if args.command == "execute":
            return asyncio.run(cmd_execute_async(repo, as_json=bool(getattr(args, "json", False))))
        if args.command == "resume":
            return asyncio.run(cmd_resume_async(repo, as_json=bool(getattr(args, "json", False))))
        if args.command == "preflight":
            return cmd_preflight(repo, as_json=bool(getattr(args, "json", False)))
        if args.command == "cancel":
            return asyncio.run(cmd_cancel_async(repo))
        if args.command == "status":
            return asyncio.run(cmd_status_async(repo, as_json=bool(args.json)))
        if args.command == "watch":
            return asyncio.run(cmd_watch_async(repo, args.interval))
        if args.command == "archive":
            return cmd_archive(repo, superseded=bool(args.superseded))
    except (ConfigError, WorkflowError, IntegrationError, ClientError, WorkspaceBusy, WorkspaceError, SetupError) as exc:
        code = getattr(exc, "exit_code", 1)
        if isinstance(exc, UnresolvedExecution):
            code = 2
        if getattr(args, "json", False):
            return print_json_error(args.command, str(exc), code)
        return _die(str(exc), code)
    except KeyboardInterrupt:
        return 130
    return _die("unknown command")
