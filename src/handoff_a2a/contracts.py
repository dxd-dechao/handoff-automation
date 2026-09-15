"""Neutral coding-task request/result contract (urn:handoff-automation:coding-task:v1)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any, Mapping
from uuid import UUID

CODING_TASK_PROFILE = "urn:handoff-automation:coding-task:v1"
CODING_RESULT_ARTIFACT = "coding-result"
WORKER_LIFECYCLE_ARTIFACT = "worker-lifecycle"
DURABLE_DEDUP_PARAM = "durable_execution_id_deduplication"
WORKSPACE_FINGERPRINT_PARAM = "workspace_code_fingerprint"

ELIGIBLE_HANDOFF_STATUSES = frozenset(
    {"READY FOR EXECUTION", "CHANGES REQUESTED"}
)
READY_FOR_QA = "READY FOR QA"

TERMINAL_TASK_STATES = frozenset(
    {
        "TASK_STATE_COMPLETED",
        "TASK_STATE_FAILED",
        "TASK_STATE_REJECTED",
        "TASK_STATE_CANCELED",
        "COMPLETED",
        "FAILED",
        "REJECTED",
        "CANCELED",
        "CANCELLED",
    }
)

INTERRUPTED_TASK_STATES = frozenset(
    {"TASK_STATE_INPUT_REQUIRED", "INPUT_REQUIRED"}
)


def request_canonical_hash(request: CodingRequest | Mapping[str, Any]) -> str:
    """Hash the complete validated coding request, not only HANDOFF bytes."""
    payload = request.to_dict() if isinstance(request, CodingRequest) else dict(request)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return sha256(encoded.encode("utf-8")).hexdigest()


class ContractError(ValueError):
    """Raised when a coding-task payload fails explicit validation."""


def snapshot_sha256(snapshot: str | bytes) -> str:
    """Hash the exact UTF-8 bytes of a HANDOFF.md snapshot."""
    data = snapshot if isinstance(snapshot, bytes) else snapshot.encode("utf-8")
    return sha256(data).hexdigest()


def _require_str(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ContractError(f"{key} must be a non-empty string")
    return value


def _require_uuid(payload: Mapping[str, Any], key: str) -> str:
    value = _require_str(payload, key)
    try:
        UUID(value)
    except ValueError as exc:
        raise ContractError(f"{key} must be a UUID") from exc
    return value


def _require_iteration(payload: Mapping[str, Any]) -> int:
    value = payload.get("iteration")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractError("iteration must be a positive integer")
    if float(value) != int(value) or int(value) < 1:
        raise ContractError("iteration must be a positive integer")
    return int(value)


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractError("usage counts must be integers or null")
    if float(value) != int(value):
        raise ContractError("usage counts must be integers or null")
    return int(value)


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractError("cost_usd must be a number or null")
    return float(value)


@dataclass(frozen=True)
class CodingRequest:
    schema: str
    workflow_id: str
    run_id: str
    execution_id: str
    iteration: int
    workspace_id: str
    expected_branch: str
    expected_head: str
    handoff_markdown: str
    request_sha256: str
    expected_code_fingerprint: str | None = None

    def utf8_bytes(self) -> bytes:
        return self.handoff_markdown.encode("utf-8")

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "schema": self.schema,
            "workflow_id": self.workflow_id,
            "run_id": self.run_id,
            "execution_id": self.execution_id,
            "iteration": self.iteration,
            "workspace_id": self.workspace_id,
            "expected_branch": self.expected_branch,
            "expected_head": self.expected_head,
            "handoff_markdown": self.handoff_markdown,
            "request_sha256": self.request_sha256,
        }
        if self.expected_code_fingerprint is not None:
            payload["expected_code_fingerprint"] = self.expected_code_fingerprint
        return payload


def parse_coding_request(payload: Mapping[str, Any] | Any) -> CodingRequest:
    if not isinstance(payload, Mapping):
        raise ContractError("coding request must be a JSON object")
    if payload.get("schema") != CODING_TASK_PROFILE:
        raise ContractError("unsupported coding-task schema")
    markdown = payload.get("handoff_markdown")
    if not isinstance(markdown, str) or markdown == "":
        raise ContractError("handoff_markdown must be a non-empty UTF-8 string")
    digest = _require_str(payload, "request_sha256")
    expected = snapshot_sha256(markdown)
    if digest != expected:
        raise ContractError("request_sha256 does not match handoff_markdown bytes")
    fingerprint_raw = payload.get("expected_code_fingerprint")
    fingerprint: str | None
    if fingerprint_raw is None:
        fingerprint = None
    else:
        if not isinstance(fingerprint_raw, str) or not fingerprint_raw:
            raise ContractError("expected_code_fingerprint must be a non-empty string when present")
        fingerprint = fingerprint_raw
    return CodingRequest(
        schema=CODING_TASK_PROFILE,
        workflow_id=_require_str(payload, "workflow_id"),
        run_id=_require_str(payload, "run_id"),
        execution_id=_require_uuid(payload, "execution_id"),
        iteration=_require_iteration(payload),
        workspace_id=_require_str(payload, "workspace_id"),
        expected_branch=_require_str(payload, "expected_branch"),
        expected_head=_require_str(payload, "expected_head"),
        handoff_markdown=markdown,
        request_sha256=digest,
        expected_code_fingerprint=fingerprint,
    )


@dataclass(frozen=True)
class GitRef:
    branch: str | None
    head: str | None


@dataclass(frozen=True)
class CommitRef:
    sha: str
    subject: str


@dataclass(frozen=True)
class Usage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    cache_read_input_tokens: int | None = None

    def to_dict(self) -> dict[str, int | None]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_creation_input_tokens": self.cache_creation_input_tokens,
            "cache_read_input_tokens": self.cache_read_input_tokens,
        }


@dataclass(frozen=True)
class CodingResult:
    schema: str
    workflow_id: str
    run_id: str
    execution_id: str
    task_id: str
    context_id: str
    iteration: int
    workspace_id: str
    summary: str
    execution_notes: str
    exit_code: int | None
    provider_error: bool
    invalid_output: bool
    invalid_handoff_transition: bool
    reason: str | None
    status_before: str | None
    status_after: str | None
    branch_before: str | None
    branch_after: str | None
    head_before: str | None
    head_after: str | None
    commits: tuple[CommitRef, ...]
    git_status: str
    diff: str
    duration_s: float
    usage: Usage | None
    cost_usd: float | None
    usage_provenance: str | None
    cost_provenance: str | None
    evidence_dir: str
    plan_intact: bool
    worker_launched: bool
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema": self.schema,
            "workflow_id": self.workflow_id,
            "run_id": self.run_id,
            "execution_id": self.execution_id,
            "task_id": self.task_id,
            "context_id": self.context_id,
            "iteration": self.iteration,
            "workspace_id": self.workspace_id,
            "summary": self.summary,
            "execution_notes": self.execution_notes,
            "exit_code": self.exit_code,
            "provider_error": self.provider_error,
            "invalid_output": self.invalid_output,
            "invalid_handoff_transition": self.invalid_handoff_transition,
            "reason": self.reason,
            "status_before": self.status_before,
            "status_after": self.status_after,
            "branch_before": self.branch_before,
            "branch_after": self.branch_after,
            "head_before": self.head_before,
            "head_after": self.head_after,
            "commits": [{"sha": c.sha, "subject": c.subject} for c in self.commits],
            "git_status": self.git_status,
            "diff": self.diff,
            "duration_s": self.duration_s,
            "usage": None if self.usage is None else self.usage.to_dict(),
            "cost_usd": self.cost_usd,
            "usage_provenance": self.usage_provenance,
            "cost_provenance": self.cost_provenance,
            "evidence_dir": self.evidence_dir,
            "plan_intact": self.plan_intact,
            "worker_launched": self.worker_launched,
        }
        payload.update(self.extra)
        return payload


def parse_usage(payload: Mapping[str, Any] | None) -> Usage | None:
    if payload is None:
        return None
    if not isinstance(payload, Mapping):
        raise ContractError("usage must be an object or null")
    return Usage(
        input_tokens=_optional_int(payload.get("input_tokens")),
        output_tokens=_optional_int(payload.get("output_tokens")),
        cache_creation_input_tokens=_optional_int(
            payload.get("cache_creation_input_tokens")
        ),
        cache_read_input_tokens=_optional_int(payload.get("cache_read_input_tokens")),
    )


def parse_coding_result(payload: Mapping[str, Any] | Any) -> CodingResult:
    if not isinstance(payload, Mapping):
        raise ContractError("coding result must be a JSON object")
    if payload.get("schema") != CODING_TASK_PROFILE:
        raise ContractError("unsupported coding-result schema")
    commits_raw = payload.get("commits") or []
    if not isinstance(commits_raw, list):
        raise ContractError("commits must be a list")
    commits = []
    for item in commits_raw:
        if not isinstance(item, Mapping):
            raise ContractError("commit entries must be objects")
        commits.append(
            CommitRef(
                sha=_require_str(item, "sha"),
                subject=str(item.get("subject") or ""),
            )
        )
    usage_raw = payload.get("usage")
    return CodingResult(
        schema=CODING_TASK_PROFILE,
        workflow_id=_require_str(payload, "workflow_id"),
        run_id=_require_str(payload, "run_id"),
        execution_id=_require_str(payload, "execution_id"),
        task_id=_require_str(payload, "task_id"),
        context_id=str(payload.get("context_id") or ""),
        iteration=_require_iteration(payload),
        workspace_id=_require_str(payload, "workspace_id"),
        summary=str(payload.get("summary") or ""),
        execution_notes=str(payload.get("execution_notes") or ""),
        exit_code=_optional_int(payload.get("exit_code")),
        provider_error=bool(payload.get("provider_error")),
        invalid_output=bool(payload.get("invalid_output")),
        invalid_handoff_transition=bool(payload.get("invalid_handoff_transition")),
        reason=payload.get("reason") if payload.get("reason") is None else str(payload.get("reason")),
        status_before=payload.get("status_before"),
        status_after=payload.get("status_after"),
        branch_before=payload.get("branch_before"),
        branch_after=payload.get("branch_after"),
        head_before=payload.get("head_before"),
        head_after=payload.get("head_after"),
        commits=tuple(commits),
        git_status=str(payload.get("git_status") or ""),
        diff=str(payload.get("diff") or ""),
        duration_s=float(payload.get("duration_s") or 0),
        usage=parse_usage(usage_raw if isinstance(usage_raw, Mapping) or usage_raw is None else None),
        cost_usd=_optional_float(payload.get("cost_usd")),
        usage_provenance=payload.get("usage_provenance"),
        cost_provenance=payload.get("cost_provenance"),
        evidence_dir=str(payload.get("evidence_dir") or ""),
        plan_intact=bool(payload.get("plan_intact", True)),
        worker_launched=bool(payload.get("worker_launched", False)),
    )


def identities_match(request: CodingRequest, result: Mapping[str, Any], task_id: str) -> bool:
    return (
        result.get("run_id") == request.run_id
        and result.get("execution_id") == request.execution_id
        and result.get("workflow_id") == request.workflow_id
        and result.get("task_id") == task_id
    )
