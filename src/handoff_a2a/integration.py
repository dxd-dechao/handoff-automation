"""Production CLI integration: approve/execute/status/resume/cancel/watch/archive."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import uuid
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
from google.protobuf.json_format import MessageToDict


HOLD_AFTER_RECORD = "HANDOFF_A2A_HOLD_AFTER_RECORD"


class IntegrationError(Exception):
    def __init__(self, message: str, *, exit_code: int = 1):
        super().__init__(message)
        self.exit_code = exit_code


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
    workflow = record_approval(
        repo,
        workspace_id=settings.workspace_id,
        max_rounds=_config.max_rounds,
    )
    print(f"approved workflow {workflow['workflow_id']}")
    print(f"plan_hash {workflow['approved_plan_hash']}")
    if workflow.get("parent_workflow_id"):
        print(f"parent_workflow_id {workflow['parent_workflow_id']}")
    return 0


def _prepare_request(repo: Path, config: HandoffConfig, settings: Any) -> tuple[dict[str, Any], dict[str, Any], Any]:
    markdown = (repo / HANDOFF_NAME).read_text(encoding="utf-8")
    document = parse_handoff(markdown)
    workflow = require_workflow(repo)
    if document.status not in ELIGIBLE_HANDOFF_STATUSES:
        raise IntegrationError(
            f"refusing to execute: Status is {document.status!r} "
            "(need READY FOR EXECUTION or CHANGES REQUESTED)"
        )
    if approved_plan_hash(markdown) != workflow.get("approved_plan_hash"):
        raise IntegrationError("HANDOFF plan does not match the approval receipt; re-approve the new scope")
    if workflow.get("workspace_id") != settings.workspace_id:
        raise IntegrationError("configured workspace_id does not match the approval receipt")
    git = GitWorkspace(settings.workspace_id, repo)
    git.require_git_checkout()
    branch = git.current_branch()
    if branch != document.branch or branch != workflow.get("branch"):
        raise IntegrationError("checked-out branch does not match the approved workflow branch")
    if load_outstanding(repo):
        raise IntegrationError("outstanding A2A run exists; resume or cancel it instead of starting another")
    if workflow.get("dispatch_hold"):
        workflow = set_dispatch_hold(repo, workflow, False)
    if rounds_available(workflow) < 1:
        raise IntegrationError(
            "execution round limit reached; Planner must review remaining scope"
        )
    fingerprint = git.code_fingerprint()
    baseline = workflow.get("post_run_fingerprint")
    if not baseline:
        if not git.code_is_clean():
            raise IntegrationError(
                "initial A2A execution requires a clean code baseline "
                "(HANDOFF/workflow files excluded)"
            )
    elif fingerprint != baseline:
        raise IntegrationError(
            "workspace code does not match the last recorded post-run snapshot"
        )
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
            "started_at": outstanding.get("created_at") or manifest.get("started_at"),
            "finished_at": utc_now() if stopped and not unresolved else None,
            "outcome": outcome,
            "reason": reason or (result or {}).get("reason"),
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


async def cmd_execute_async(repo: Path, *, from_watch: bool = False) -> int:
    config, settings = require_a2a_config(repo)
    if from_watch:
        workflow = load_workflow(repo)
        if workflow and workflow.get("dispatch_hold"):
            raise IntegrationError("watch will not clear a dispatch hold; use explicit execute")
    with submit_lock(repo):
        require_a2a_config(repo)
        if load_outstanding(repo):
            raise IntegrationError("outstanding A2A run exists; resume or cancel it")
        record, _workflow, request = _prepare_request(repo, config, settings)
    _pause_if_held()
    record_path = Path(str(run_record_path(repo, request.execution_id)))
    outstanding = load_outstanding(repo) or {}
    client = await _connect(settings)
    try:
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
                f"wait timeout: {exc}",
                task_id=task_id,
                execution_id=request.execution_id,
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
        print(json.dumps({"run_id": request.run_id, "outcome": manifest.get("outcome"), "task_id": task_id}, indent=2))
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
        print(f"handoff: unresolved: {exc}", file=sys.stderr)
        return 2
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


async def cmd_resume_async(repo: Path) -> int:
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
        print(f"handoff: unresolved: {exc}", file=sys.stderr)
        return 2
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


async def cmd_status_async(repo: Path) -> int:
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
    if outstanding:
        print(f"run:    {outstanding.get('run_id')}")
        print(f"task:   {outstanding.get('task_id') or '(ack unknown; resume to retransmit)'}")
    if reason:
        print(f"reason: {reason}")
    print(f"next:   {_next_action(markdown_status=document.status, execution=execution, workflow=workflow)}")
    if execution in {"UNRESOLVED", "RECOVERY", "WORKING"}:
        return 2 if execution != "WORKING" else 0
    return 0


# Refusals watch reports and keeps observing through (e.g. config switched mid-watch).
_WATCH_ERRORS = (IntegrationError, ConfigError, WorkflowError, ClientError, WorkspaceBusy, WorkspaceError)


async def cmd_watch_async(repo: Path, interval: float) -> int:
    last_status = ""
    last_notified = None
    print(f"watching {repo}/HANDOFF.md every {int(interval)}s (Ctrl-C to stop)")
    while True:
        outstanding = load_outstanding(repo)
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
            print(f"[{time.strftime('%H:%M:%S')}] status: {status} ({_turn(status)}'s turn)")
            if status in ELIGIBLE_HANDOFF_STATUSES:
                if workflow and workflow.get("dispatch_hold"):
                    print("execute held after failed delivery; watch will not retry")
                elif workflow and exhausted(workflow):
                    print("round limit reached; Planner scope review is the next action")
                else:
                    try:
                        await cmd_execute_async(repo, from_watch=True)
                    except _WATCH_ERRORS as exc:
                        print(f"handoff: {exc}", file=sys.stderr)
                        print("execute failed; watching for the next status change")
            elif status in {READY_FOR_QA, "APPROVED"}:
                key = (status, (workflow or {}).get("workflow_id"))
                if key != last_notified:
                    print(f"NOTIFY: Handoff: {status} — {_goal((repo / HANDOFF_NAME).read_text(encoding='utf-8'))}")
                    last_notified = key
            last_status = status
        time.sleep(interval)


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
    sub.add_parser("execute")
    sub.add_parser("resume")
    sub.add_parser("cancel")
    sub.add_parser("status")
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
            return asyncio.run(cmd_execute_async(repo))
        if args.command == "resume":
            return asyncio.run(cmd_resume_async(repo))
        if args.command == "cancel":
            return asyncio.run(cmd_cancel_async(repo))
        if args.command == "status":
            return asyncio.run(cmd_status_async(repo))
        if args.command == "watch":
            return asyncio.run(cmd_watch_async(repo, args.interval))
        if args.command == "archive":
            return cmd_archive(repo, superseded=bool(args.superseded))
    except (ConfigError, WorkflowError, IntegrationError, ClientError, WorkspaceBusy, WorkspaceError) as exc:
        code = getattr(exc, "exit_code", 1)
        if isinstance(exc, UnresolvedExecution):
            code = 2
        return _die(str(exc), code)
    except KeyboardInterrupt:
        return 130
    return _die("unknown command")
