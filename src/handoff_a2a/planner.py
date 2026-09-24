"""`handoff planner`: launch a human-facing Cursor CLI Planner session.

The session is interactive and editable (native `--mode plan` is read-only
and cannot write the HANDOFF draft). Planner guidance is the project-local
`handoff-cli` skill that `handoff skill install --host cursor` (or
`handoff init --planner cursor`) installs; invoke it
with `/handoff-cli` in the session. This launcher never starts an Executor,
never approves a draft, never reads the session's exit as approval, and never
touches the Executor selection (server.json) or a running task.
"""

from __future__ import annotations

import argparse
import os
import shlex
import sys
from pathlib import Path

from handoff_a2a.providers import ProviderError, validate_model
from handoff_a2a.setup import SetupError, resolve_worktree
from handoff_a2a.skills import install_command, project_location, skill_state
from handoff_a2a.workspace import HANDOFF_NAME, PLANNER_SKILL_DIR

SUPPORTED = ("cursor",)


def planner_argv(binary: str, repo: Path, model: str) -> list[str]:
    return [binary, "--model", model, "--workspace", str(repo)]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="handoff planner")
    parser.add_argument("repo", nargs="?", default=".")
    parser.add_argument("--provider", choices=SUPPORTED, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--print-command", action="store_true", help="print the launch command instead of starting it")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        repo = resolve_worktree(Path(args.repo).expanduser().resolve())
        if not (repo / HANDOFF_NAME).is_file():
            raise SetupError(f'no {HANDOFF_NAME} in {repo}; run handoff init "{repo}" --planner cursor first')
        location = project_location(repo, "cursor")
        state, _detail = skill_state(location)
        if state == "absent":
            raise SetupError(
                f"the Planner skill is not installed in {repo / PLANNER_SKILL_DIR}; "
                f"run {install_command(location)} (project-local; nothing global)"
            )
        validation = validate_model(args.provider, args.model)
    except (SetupError, ProviderError) as exc:
        print(f"handoff: {exc}", file=sys.stderr)
        return getattr(exc, "exit_code", 1)
    command = planner_argv(validation.binary, repo, validation.model)
    if state == "foreign":
        print(f"note: {PLANNER_SKILL_DIR} differs from this installation's skill; it is used as found")
    elif state == "outdated":
        print(f"note: {PLANNER_SKILL_DIR} is an older release; upgrade it with: {install_command(location)}")
    print(f"planner:  {args.provider} / {validation.model} ({validation.describe()})")
    print(f"repo:     {repo}")
    print("guidance: type /handoff-cli in the session (plan, QA, status). The Executor selection is not changed.")
    print("          Approval stays yours: the Planner writes a DRAFT and waits; exiting is not approval.")
    if args.print_command:
        print("command:  " + " ".join(shlex.quote(part) for part in command))
        return 0
    sys.stdout.flush()
    os.chdir(repo)
    os.execv(command[0], command)
    return 0  # not reached
