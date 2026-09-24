"""Executor model selection within a workflow: `handoff model`.

A selection is configuration, not plan: it never enters the approved plan
hash, and switching never touches the workflow ID, approval, round counts,
dispatch hold, Git branch/HEAD, or continuation fingerprint.

- Immediate switch (between executions): validate the candidate, take
  service.lock then submit.lock, verify nothing is outstanding, stop the owned
  server, publish the new server.json (next config generation) atomically,
  start it, and verify its identity. A failed start restores and restarts the
  previous selection and records the failure; nothing dispatches to an
  unverified service. With the service stopped, the selection is saved for the
  next `handoff server start`.
- `--after-current`: record a validated pending selection without touching the
  live service, the current run, or its saved endpoint. The dispatch gate
  (watch, and the next execute after reconciliation) applies it once under the
  same locks before a new worker can start, and only when ownership is
  certain. A failed application is kept with its error and blocks dispatch
  until the human replaces or cancels it.
- A switch is never a move of an in-progress conversation: interrupting
  means `handoff cancel`, review, and a new execution under the same rules.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

from handoff_a2a.client import write_json_atomic
from handoff_a2a.providers import PROVIDERS, ProviderError, Validation, validate_model
from handoff_a2a.service import (
    Managed,
    ServiceError,
    inspect,
    load_managed,
    refusal_guidance,
    start_locked,
    stop_locked,
    unresolved,
)
from handoff_a2a.setup import (
    SetupError,
    append_history,
    holding,
    selection_of,
    selection_record,
    server_block,
    service_lock,
)
from handoff_a2a.workspace import HANDOFF_NAME, submit_lock

PENDING_NAME = "pending-selection.json"
_EXECUTOR_LINE = re.compile(r"^\*\*Executor:\*\*\s*(.+?)\s*$", re.MULTILINE)


class SelectionError(SetupError):
    pass


def pending_path(managed: Managed) -> Path:
    return managed.paths.service_dir / PENDING_NAME


def load_pending(managed: Managed) -> dict[str, Any] | None:
    path = pending_path(managed)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"invalid": True}
    return data if isinstance(data, dict) else {"invalid": True}


def label(provider: str, model: str, effort: str | None = None) -> str:
    return f"{provider} / {model}" + (f" (reasoning effort {effort})" if effort else "")


def plan_constraint(repo: Path) -> str | None:
    """An explicit `**Executor:**` line in the Current Task constrains the selection."""
    path = repo / HANDOFF_NAME
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8")
    start = text.find("## Current Task")
    if start < 0:
        return None
    end = text.find("\n## ", start + 1)
    match = _EXECUTOR_LINE.search(text[start : end if end > 0 else len(text)])
    return match.group(1) if match else None


def _normalize(value: str) -> str:
    return re.sub(r"\s*/\s*", "/", value.strip().strip("`").lower())


def check_plan_constraint(repo: Path, provider: str, model: str) -> None:
    constraint = plan_constraint(repo)
    if constraint and _normalize(constraint) != _normalize(f"{provider}/{model}"):
        raise SelectionError(
            f"the approved plan constrains the Executor to {constraint!r}; changing that requirement "
            "needs a revised, re-approved plan (the selection never rewrites the HANDOFF contract)",
            exit_code=2,
        )


def _validate(provider: str, model: str, effort: str | None) -> Validation:
    try:
        return validate_model(provider, model, reasoning_effort=effort)
    except ProviderError as exc:
        raise SelectionError(f"{exc}\nthe current selection is unchanged") from exc


def _candidate_server(managed: Managed, validation: Validation) -> dict[str, Any]:
    server = {k: v for k, v in managed.server.items() if k not in PROVIDERS}
    server[validation.provider] = server_block(validation)
    server["config_generation"] = managed.generation + 1
    server["selection"] = selection_record(validation)
    return server


def _same_selection(managed: Managed, provider: str, model: str, effort: str | None) -> bool:
    current = selection_of(managed.server)
    return current == (provider, model, effort)


def switch_locked(managed: Managed, validation: Validation, *, reason: str) -> list[str]:
    """Publish a validated selection. Caller holds service.lock and submit.lock."""
    paths = managed.paths
    blocked = unresolved(managed, holding_submit=True)
    if blocked:
        raise SelectionError(
            f"cannot switch now: {blocked}. {refusal_guidance(paths.repo)}, "
            "or queue the change with --after-current",
            exit_code=2,
        )
    state = inspect(managed)
    if state.running and not state.verified:
        raise SelectionError(
            f"the running service cannot be verified ({'; '.join(state.detail)}); "
            f'inspect handoff server status "{paths.repo}" before switching'
        )
    previous = dict(managed.server)
    candidate = _candidate_server(managed, validation)
    previous_label = label(*selection_of(previous))
    lines: list[str] = []
    write_json_atomic(paths.service_dir / "previous-server.json", previous)
    if not state.running:
        write_json_atomic(paths.server_config, candidate)
        append_history(paths, {"event": "selected", "reason": reason, "generation": candidate["config_generation"], "provider": validation.provider, "model": validation.model, "previous": previous_label, "service": "stopped"})
        lines.append(f"selected {label(validation.provider, validation.model, validation.reasoning_effort)} (generation {candidate['config_generation']})")
        lines.append("service is stopped; the selection applies at the next handoff server start")
        return lines
    lines.append(f"stopping service running {previous_label} (generation {managed.generation})")
    stop_locked(managed, holding_submit=True)
    write_json_atomic(paths.server_config, candidate)
    fresh = load_managed(paths.repo)
    try:
        started = start_locked(fresh)
    except ServiceError as exc:
        write_json_atomic(paths.server_config, previous)
        append_history(paths, {"event": "switch_failed", "reason": reason, "provider": validation.provider, "model": validation.model, "error": str(exc).splitlines()[0], "restored": previous_label})
        restored = "restored the previous selection"
        try:
            start_locked(load_managed(paths.repo))
            restored += " and restarted it"
        except ServiceError as again:
            restored += f" but it did not restart ({str(again).splitlines()[0]}); run handoff server start"
        raise SelectionError(f"new Executor service failed to start: {str(exc).splitlines()[0]}; {restored}") from exc
    append_history(paths, {"event": "selected", "reason": reason, "generation": candidate["config_generation"], "provider": validation.provider, "model": validation.model, "previous": previous_label, "service": "restarted"})
    lines.append(f"active: {started.active_identity} (verified)")
    return lines


def cmd_switch(repo: Path, provider: str, model: str, effort: str | None) -> int:
    managed = load_managed(repo)
    check_plan_constraint(managed.paths.repo, provider, model)
    if _same_selection(managed, provider, model, effort):
        print(f"already selected: {label(provider, model, effort)} (generation {managed.generation}); nothing changed")
        return 0
    validation = _validate(provider, model, effort)
    with holding(service_lock(managed.paths), "service"):
        with holding(submit_lock(managed.paths.repo), "submission"):
            managed = load_managed(repo)  # re-read under the locks
            lines = switch_locked(managed, validation, reason="immediate")
            pending = load_pending(managed)
            if pending:
                pending_path(managed).unlink()
                append_history(managed.paths, {"event": "pending_superseded", "pending": pending})
                lines.append("a queued --after-current selection was superseded by this switch")
    print(f"model check: {validation.describe()}")
    for line in lines:
        print(line)
    print("workflow, approval, rounds, hold, and Git state are unchanged")
    return 0


def cmd_queue(repo: Path, provider: str, model: str, effort: str | None) -> int:
    managed = load_managed(repo)
    check_plan_constraint(managed.paths.repo, provider, model)
    existing = load_pending(managed)
    if existing and not existing.get("failed") and (existing.get("provider"), existing.get("model"), existing.get("reasoning_effort")) == (provider, model, effort):
        print(f"already queued: {label(provider, model, effort)}; nothing changed")
        return 0
    if _same_selection(managed, provider, model, effort):
        if existing:
            with holding(service_lock(managed.paths), "service"):
                pending_path(managed).unlink(missing_ok=True)
            append_history(managed.paths, {"event": "pending_canceled", "pending": existing, "reason": "requested the current selection"})
            print(f"{label(provider, model, effort)} is already selected; removed the queued change")
        else:
            print(f"{label(provider, model, effort)} is already selected; nothing queued")
        return 0
    validation = _validate(provider, model, effort)
    record = {
        "provider": provider,
        "model": model,
        "reasoning_effort": effort,
        "requested_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "base_generation": managed.generation,
        "validation": validation.status,
        "source": validation.source,
    }
    with holding(service_lock(managed.paths), "service"):
        write_json_atomic(pending_path(managed), record)
    append_history(managed.paths, {"event": "pending_replaced" if existing else "pending_queued", **record, "replaced": existing})
    current = label(*selection_of(managed.server))
    busy = unresolved(managed)
    print(f"model check: {validation.describe()}")
    print(f"queued: {label(provider, model, effort)}")
    if busy:
        print(f"current run keeps {current}: {busy}")
    else:
        print(f"nothing is running now; {current} stays selected until the queued change applies")
    print(
        "applies once, after the current run is reconciled and before the next worker starts "
        "(handoff watch, or the next handoff execute)"
    )
    return 0


def cmd_cancel_pending(repo: Path) -> int:
    managed = load_managed(repo)
    with holding(service_lock(managed.paths), "service"):
        pending = load_pending(managed)
        if not pending:
            print("no queued selection")
            return 0
        pending_path(managed).unlink()
    append_history(managed.paths, {"event": "pending_canceled", "pending": pending})
    print(f"removed queued selection {label(str(pending.get('provider')), str(pending.get('model')))}")
    return 0


def run_identity(repo: Path) -> dict[str, Any] | None:
    path = repo / ".handoff-logs" / "outstanding.json"
    if not path.is_file():
        return None
    with contextlib.suppress(OSError, json.JSONDecodeError):
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("executor") if isinstance(data, dict) else None
    return None


def describe_lines(managed: Managed) -> list[str]:
    provider, model, effort = selection_of(managed.server)
    state = inspect(managed)
    lines = [f"selected: {label(provider, model, effort)} (generation {managed.generation}; {(managed.server.get('selection') or {}).get('validation', 'recorded')})"]
    if state.verified:
        lines.append(f"active:   {state.active_identity} (service verified)")
    elif state.running:
        lines.append(f"active:   UNVERIFIED service ({'; '.join(state.detail)})")
    else:
        lines.append("active:   none (service stopped)")
    run = run_identity(managed.paths.repo)
    if run:
        lines.append(
            f"current run: {run.get('provider')} / {run.get('model')} (generation {run.get('config_generation')}; recorded at submission)"
        )
    pending = load_pending(managed)
    if pending:
        status = "FAILED: " + str(pending.get("error")) if pending.get("failed") else "applies before the next worker starts"
        lines.append(f"pending:  {label(str(pending.get('provider')), str(pending.get('model')), pending.get('reasoning_effort'))} ({status})")
    return lines


def cmd_show(repo: Path) -> int:
    managed = load_managed(repo)
    for line in describe_lines(managed):
        print(line)
    return 0


# ── dispatch boundary (used by integration.py; provider-neutral interface) ──


def is_managed(repo: Path) -> bool:
    try:
        load_managed(repo)
    except SetupError:
        return False
    return True


def apply_pending(repo: Path, *, emit=print) -> str:
    """Apply a queued selection when ownership is certain.

    Returns "none", "applied", or "deferred". Raises SelectionError when a
    queued change failed; dispatch must not proceed until the human acts.
    """
    managed = load_managed(repo)
    pending = load_pending(managed)
    if not pending:
        return "none"
    if pending.get("invalid"):
        raise SelectionError(f"queued selection {pending_path(managed)} is unreadable; run handoff model --cancel-pending")
    if pending.get("failed"):
        raise SelectionError(
            f"queued Executor change to {label(str(pending.get('provider')), str(pending.get('model')))} failed "
            f"({pending.get('error')}); replace it (handoff model ... --after-current) or cancel it "
            "(handoff model --cancel-pending) before dispatching"
        )
    if unresolved(managed):
        return "deferred"
    provider, model, effort = str(pending["provider"]), str(pending["model"]), pending.get("reasoning_effort")
    try:
        check_plan_constraint(managed.paths.repo, provider, model)
        validation = _validate(provider, model, effort)
        with holding(service_lock(managed.paths), "service"):
            with holding(submit_lock(managed.paths.repo), "submission"):
                managed = load_managed(repo)
                current = load_pending(managed)
                if current != pending:
                    return "deferred"  # replaced or canceled meanwhile; next pass sees it
                if _same_selection(managed, provider, model, effort):
                    lines = [f"{label(provider, model, effort)} was already selected"]
                else:
                    lines = switch_locked(managed, validation, reason="after-current")
                pending_path(managed).unlink()
    except SelectionError as exc:
        if exc.exit_code == 2 and "cannot switch now" in str(exc):
            return "deferred"
        failed = dict(pending, failed=True, error=str(exc).splitlines()[0], failed_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        write_json_atomic(pending_path(managed), failed)
        append_history(managed.paths, {"event": "pending_failed", **failed})
        raise
    except SetupError as exc:
        if exc.exit_code == 2:
            return "deferred"  # a lock is busy; try again at the next boundary
        raise
    append_history(managed.paths, {"event": "pending_applied", **pending})
    emit(f"applied queued Executor selection: {label(provider, model, effort)}")
    for line in lines:
        emit(f"  {line}")
    return "applied"


def expected_card(repo: Path) -> dict[str, Any] | None:
    """Identity the managed service must advertise (None for unmanaged endpoints)."""
    try:
        managed = load_managed(repo)
    except SetupError:
        return None
    provider, model, _effort = selection_of(managed.server)
    return {
        "workspace_id": managed.workspace_id,
        "config_generation": managed.generation,
        "executor_provider": provider,
        "executor_model": model,
    }


def status_lines(repo: Path) -> list[str]:
    try:
        managed = load_managed(repo)
    except SetupError:
        return []
    try:
        return describe_lines(managed)
    except (SetupError, OSError) as exc:
        return [f"executor: unavailable ({exc})"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="handoff model")
    parser.add_argument("repo", nargs="?", default=".")
    parser.add_argument("--provider", choices=PROVIDERS)
    parser.add_argument("--model")
    parser.add_argument("--reasoning-effort")
    parser.add_argument("--after-current", action="store_true")
    parser.add_argument("--cancel-pending", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo = Path(args.repo).expanduser().resolve()
    try:
        if args.cancel_pending:
            if args.provider or args.model or args.after_current:
                raise SelectionError("--cancel-pending takes no other options", exit_code=2)
            return cmd_cancel_pending(repo)
        if not args.provider and not args.model:
            if args.after_current or args.reasoning_effort:
                raise SelectionError("--provider and --model are required to change the selection", exit_code=2)
            return cmd_show(repo)
        if not args.provider or not args.model:
            raise SelectionError("--provider and --model are both required to change the selection", exit_code=2)
        if args.after_current:
            return cmd_queue(repo, args.provider, args.model, args.reasoning_effort)
        return cmd_switch(repo, args.provider, args.model, args.reasoning_effort)
    except SetupError as exc:
        print(f"handoff: {exc}", file=sys.stderr)
        return exc.exit_code
