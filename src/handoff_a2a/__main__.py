"""Development CLI: `handoff-a2a serve` and client helpers."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from handoff_a2a.client import (
    ClientError,
    UnresolvedExecution,
    cancel_from_record,
    execute_exit_code,
    execute_repo,
    resume_from_record,
    status_from_record,
)
from handoff_a2a.integration import main as integration_main
from handoff_a2a.server import load_server_config, serve


def _module(name: str, entry: str = "main"):
    import importlib

    return lambda: getattr(importlib.import_module(f"handoff_a2a.{name}"), entry)


MANAGED_COMMANDS = {
    "setup": _module("setup"),
    "models": _module("providers"),
    "model": _module("selection"),
    "mode": _module("selection", "mode_main"),
    "server": _module("service"),
    "planner": _module("planner"),
    "skill": _module("skills"),
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="handoff-a2a",
        description=(
            "Experimental A2A development helper and production CLI integration. "
            "Does not replace `handoff execute` unless invoked via `handoff`. "
            "There is no arbitrary instruction/prompt argument."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # Managed setup/service/selection commands parse their own arguments.
    for name, help_text in (
        ("setup", "generate managed A2A configuration (handoff init)"),
        ("models", "list models the installed provider CLI reports (handoff models)"),
        ("model", "show or change the Executor selection (handoff model)"),
        ("server", "start/status/stop the managed local service (handoff server)"),
        ("planner", "launch an interactive Cursor Planner (handoff planner)"),
        ("mode", "show or set the run mode, drive or watch (handoff mode)"),
        ("skill", "install or inspect the Planner skill (handoff skill)"),
    ):
        managed = sub.add_parser(name, help=help_text, add_help=False)
        managed.add_argument("args", nargs=argparse.REMAINDER)

    serve_cmd = sub.add_parser("serve", help="start a loopback-only Executor server")
    serve_cmd.add_argument("--config", required=True, type=Path, help="server.json")

    cli_cmd = sub.add_parser("cli", help="internal production-CLI integration entry point")
    cli_cmd.add_argument("--repo", required=True, type=Path)
    cli_sub = cli_cmd.add_subparsers(dest="cli_command", required=True)
    cli_sub.add_parser("approve")
    cli_execute = cli_sub.add_parser("execute")
    cli_execute.add_argument("--json", action="store_true")
    cli_execute.add_argument("--wait", default=None)
    cli_resume = cli_sub.add_parser("resume")
    cli_resume.add_argument("--json", action="store_true")
    cli_resume.add_argument("--wait", default=None)
    cli_preflight = cli_sub.add_parser("preflight")
    cli_preflight.add_argument("--json", action="store_true")
    cli_sub.add_parser("cancel")
    cli_status = cli_sub.add_parser("status")
    cli_status.add_argument("--json", action="store_true")
    cli_watch = cli_sub.add_parser("watch")
    cli_watch.add_argument("--interval", type=float, default=30.0)
    cli_archive = cli_sub.add_parser("archive")
    cli_archive.add_argument("--superseded", action="store_true")
    cli_qa = cli_sub.add_parser("qa")
    cli_qa.add_argument("--status", required=True)
    cli_qa.add_argument("--file", required=True)
    cli_qa.add_argument("--append", action="store_true")
    cli_qa.add_argument("--json", action="store_true")

    execute_cmd = sub.add_parser(
        "execute",
        help="submit the local HANDOFF.md snapshot once, poll, and print the terminal result",
    )
    execute_cmd.add_argument("--repo", required=True, type=Path)
    execute_cmd.add_argument("--agent-card-url", required=True)
    execute_cmd.add_argument("--credential-file", required=True, type=Path)
    execute_cmd.add_argument("--workspace-id", required=True)
    execute_cmd.add_argument("--timeout", type=float, default=180.0)

    status_cmd = sub.add_parser("status", help="inspect a saved run record via GetTask")
    status_cmd.add_argument("--run-record", required=True, type=Path)
    status_cmd.add_argument("--credential-file", required=True, type=Path)

    resume_cmd = sub.add_parser(
        "resume",
        help="poll a known task or retransmit the exact saved request",
    )
    resume_cmd.add_argument("--run-record", required=True, type=Path)
    resume_cmd.add_argument("--credential-file", required=True, type=Path)
    resume_cmd.add_argument("--timeout", type=float, default=180.0)

    cancel_cmd = sub.add_parser("cancel", help="CancelTask for a known saved task ID")
    cancel_cmd.add_argument("--run-record", required=True, type=Path)
    cancel_cmd.add_argument("--credential-file", required=True, type=Path)
    return parser


def _print_unresolved(exc: UnresolvedExecution) -> int:
    print(
        json.dumps(
            {
                "unresolved": True,
                "task_id": exc.task_id,
                "execution_id": exc.execution_id,
                "error": str(exc),
            },
            indent=2,
        ),
        file=sys.stderr,
    )
    return 2


def _oserror_message(exc: OSError) -> str:
    import errno

    path = exc.filename or getattr(exc, "filename2", None) or ""
    err = exc.strerror or "OS error"
    suffix = ""
    if isinstance(exc, PermissionError) or exc.errno in (errno.EACCES, errno.EPERM):
        suffix = " (permission denied; sandbox?)"
    if path:
        return f"{path}: {err}{suffix}"
    return f"{err}{suffix}"


def main(argv: list[str] | None = None) -> int:
    raw = sys.argv[1:] if argv is None else list(argv)
    try:
        return _main(raw)
    except OSError as exc:
        message = _oserror_message(exc)
        kind = raw[0] if raw else "handoff"
        if "--json" in raw:
            from handoff_a2a.reporting import print_json_error

            return print_json_error(kind, message)
        print(f"handoff: {message}", file=sys.stderr)
        return 1


def _main(raw: list[str]) -> int:
    if raw and raw[0] in MANAGED_COMMANDS:
        # argparse REMAINDER cannot start with an option, so hand off directly.
        return MANAGED_COMMANDS[raw[0]]()(raw[1:])
    parser = build_parser()
    args = parser.parse_args(raw)
    if args.command == "serve":
        config = load_server_config(args.config)
        # Test seam: refuse one config generation to exercise failed-switch recovery.
        refused = os.environ.get("HANDOFF_A2A_REFUSE_GENERATION")
        if refused and str(config.config_generation) == refused:
            print(f"handoff-a2a: refusing config generation {refused} (test seam)", file=sys.stderr)
            return 3
        serve(config)
        return 0
    if args.command == "cli":
        argv = ["--repo", str(args.repo), args.cli_command]
        if args.cli_command == "watch":
            argv.extend(["--interval", str(getattr(args, "interval", 30.0))])
        if args.cli_command == "archive" and getattr(args, "superseded", False):
            argv.append("--superseded")
        if args.cli_command == "qa":
            argv.extend(["--status", args.status, "--file", args.file])
            if args.append:
                argv.append("--append")
        if getattr(args, "json", False) and args.cli_command in {"status", "execute", "resume", "preflight", "qa"}:
            argv.append("--json")
        if args.cli_command in {"execute", "resume"} and getattr(args, "wait", None) is not None:
            argv.extend(["--wait", str(args.wait)])
        return integration_main(argv)
    if args.command == "execute":
        try:
            payload = asyncio.run(
                execute_repo(
                    repo=args.repo,
                    agent_card_url=args.agent_card_url,
                    credential_file=args.credential_file,
                    workspace_id=args.workspace_id,
                    timeout=args.timeout,
                )
            )
        except UnresolvedExecution as exc:
            return _print_unresolved(exc)
        except (ClientError, OSError, ValueError) as exc:
            print(f"handoff-a2a: {exc}", file=sys.stderr)
            return 1
        result = payload.get("result") or {}
        state = (payload.get("task") or {}).get("status", {}).get("state")
        print(
            json.dumps(
                {
                    "task_id": payload.get("task_id"),
                    "execution_id": payload.get("execution_id"),
                    "state": state,
                    "reason": payload.get("reason") or result.get("reason"),
                    "evidence_dir": result.get("evidence_dir"),
                    "local_metadata": payload.get("local_metadata"),
                    "run_record": payload.get("run_record"),
                    "result": result,
                },
                indent=2,
            )
        )
        return execute_exit_code(state)
    if args.command == "status":
        try:
            payload = asyncio.run(
                status_from_record(
                    record_path=args.run_record,
                    credential_file=args.credential_file,
                )
            )
        except UnresolvedExecution as exc:
            return _print_unresolved(exc)
        except (ClientError, OSError, ValueError) as exc:
            print(f"handoff-a2a: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(payload, indent=2, default=str))
        if payload.get("unresolved"):
            return 2
        return 0
    if args.command == "resume":
        try:
            payload = asyncio.run(
                resume_from_record(
                    record_path=args.run_record,
                    credential_file=args.credential_file,
                    timeout=args.timeout,
                )
            )
        except UnresolvedExecution as exc:
            return _print_unresolved(exc)
        except (ClientError, OSError, ValueError) as exc:
            print(f"handoff-a2a: {exc}", file=sys.stderr)
            return 1
        state = (payload.get("task") or {}).get("status", {}).get("state")
        print(
            json.dumps(
                {
                    "task_id": payload.get("task_id"),
                    "execution_id": payload.get("execution_id"),
                    "state": state,
                    "reason": payload.get("reason"),
                    "run_record": payload.get("run_record"),
                    "recovery_required": payload.get("recovery_required"),
                    "result": payload.get("result"),
                },
                indent=2,
                default=str,
            )
        )
        return execute_exit_code(state)
    if args.command == "cancel":
        try:
            payload = asyncio.run(
                cancel_from_record(
                    record_path=args.run_record,
                    credential_file=args.credential_file,
                )
            )
        except UnresolvedExecution as exc:
            return _print_unresolved(exc)
        except (ClientError, OSError, ValueError) as exc:
            print(f"handoff-a2a: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(payload, indent=2, default=str))
        return 0
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
