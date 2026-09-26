from __future__ import annotations

import json
from pathlib import Path

import pytest

from handoff_a2a.adapters.claude import RITUAL_PROMPT, child_environment, parse_claude_json
from handoff_a2a.client import execute_exit_code, task_reason
from handoff_a2a.contracts import (
    CODING_TASK_PROFILE,
    ContractError,
    parse_coding_request,
    request_canonical_hash,
    snapshot_sha256,
)
from handoff_a2a.server import load_server_config
from handoff_a2a.workspace import parse_handoff, planner_fingerprint, read_status


def test_snapshot_hash_is_utf8_bytes() -> None:
    text = "café\n"
    assert snapshot_sha256(text) == snapshot_sha256(text.encode("utf-8"))


def test_request_requires_matching_hash() -> None:
    markdown = "# hi\n"
    with pytest.raises(ContractError, match="request_sha256"):
        parse_coding_request(
            {
                "schema": CODING_TASK_PROFILE,
                "workflow_id": "wf",
                "run_id": "run",
                "execution_id": "11111111-1111-1111-1111-111111111111",
                "iteration": 1,
                "workspace_id": "fixture",
                "expected_branch": "main",
                "expected_head": "abc",
                "handoff_markdown": markdown,
                "request_sha256": "00" * 32,
            }
        )


def test_iteration_must_be_positive() -> None:
    markdown = "x"
    base = {
        "schema": CODING_TASK_PROFILE,
        "workflow_id": "wf",
        "run_id": "run",
        "execution_id": "11111111-1111-1111-1111-111111111111",
        "workspace_id": "fixture",
        "expected_branch": "main",
        "expected_head": "abc",
        "handoff_markdown": markdown,
        "request_sha256": snapshot_sha256(markdown),
    }
    with pytest.raises(ContractError, match="iteration"):
        parse_coding_request({**base, "iteration": 0})
    parsed = parse_coding_request({**base, "iteration": 1.0})
    assert parsed.iteration == 1
    assert "model" not in parsed.to_dict()
    assert "provider" not in parsed.to_dict()


def test_handoff_status_and_planner_fingerprint_ignore_executor_fields() -> None:
    first = """## Current Task

**Status:** READY FOR EXECUTION

**Branch:** main

### Goal

Do the thing.

## Execution Notes

old notes

## QA Feedback

Not run.
"""
    second = """## Current Task

**Status:** READY FOR QA

**Branch:** main

### Goal

Do the thing.

## Execution Notes

new notes from executor

## QA Feedback

Not run.
"""
    assert read_status(first) == "READY FOR EXECUTION"
    assert planner_fingerprint(first) == planner_fingerprint(second)
    changed_plan = second.replace("Do the thing.", "Do something else.")
    assert planner_fingerprint(first) != planner_fingerprint(changed_plan)
    doc = parse_handoff(second)
    assert "new notes from executor" in doc.execution_notes


def test_auth_stripping_and_ritual_prompt() -> None:
    env = child_environment(
        {
            "PATH": "/bin",
            "ANTHROPIC_AUTH_TOKEN": "secret",
            "CLAUDECODE": "1",
            "CURSOR_ASKPASS_SECRET": "x",
            "CURSOR_AGENT": "1",
            "HANDOFF_FAKE_MODE": "ok",
        }
    )
    assert "ANTHROPIC_AUTH_TOKEN" not in env
    assert "CLAUDECODE" not in env
    assert not [k for k in env if k.startswith("CURSOR_")]  # a Cursor Planner shell never leaks
    assert env["HANDOFF_FAKE_MODE"] == "ok"
    assert RITUAL_PROMPT == "execute the handoff"


def test_zero_cost_survives_parse_and_missing_cost_is_invalid_json_object_ok() -> None:
    parsed, invalid = parse_claude_json(
        '{"type": "result", "is_error": false, "total_cost_usd": 0.0, "result": "ok", "usage": {"input_tokens": 0}}'
    )
    assert invalid is False
    assert parsed is not None
    assert parsed["total_cost_usd"] == 0.0
    assert parsed["usage"]["input_tokens"] == 0
    missing_cost, missing_invalid = parse_claude_json(
        '{"type": "result", "is_error": false, "result": "ok"}'
    )
    assert missing_invalid is False
    assert missing_cost is not None
    _, bad = parse_claude_json("not json")
    assert bad is True


def test_cli_exit_codes_distinguish_success_from_unsuccessful_terminal() -> None:
    assert execute_exit_code("TASK_STATE_COMPLETED") == 0
    assert execute_exit_code("TASK_STATE_FAILED") == 1
    assert execute_exit_code("TASK_STATE_REJECTED") == 1
    assert execute_exit_code("TASK_STATE_CANCELED") == 1
    assert execute_exit_code("TASK_STATE_INPUT_REQUIRED") == 2
    assert execute_exit_code(None) == 2
    assert task_reason(
        {"status": {"state": "TASK_STATE_REJECTED", "message": {"parts": [{"data": {"reason": "busy"}}]}}}
    ) == "busy"


def test_canonical_request_hash_covers_more_than_snapshot() -> None:
    markdown = "# hi\n"
    base = {
        "schema": CODING_TASK_PROFILE,
        "workflow_id": "wf",
        "run_id": "run",
        "execution_id": "11111111-1111-1111-1111-111111111111",
        "iteration": 1,
        "workspace_id": "fixture",
        "expected_branch": "main",
        "expected_head": "abc",
        "handoff_markdown": markdown,
        "request_sha256": snapshot_sha256(markdown),
    }
    first = parse_coding_request(base)
    second = parse_coding_request({**base, "expected_head": "def"})
    assert request_canonical_hash(first) != request_canonical_hash(second)
    assert snapshot_sha256(first.handoff_markdown) == snapshot_sha256(second.handoff_markdown)


def test_wrong_shape_json_object_is_invalid_claude_result() -> None:
    parsed, invalid = parse_claude_json('{"unrelated": "not a Claude result"}')
    assert invalid is True
    assert parsed is None
    missing_outcome, missing_invalid = parse_claude_json('{"type": "result"}')
    assert missing_invalid is True
    assert missing_outcome is None


def test_skill_has_valid_metadata_and_existing_cli_intents() -> None:
    text = Path("skills/handoff-cli/SKILL.md").read_text(encoding="utf-8")
    assert text.startswith("---\n")
    closing = text.find("\n---\n", 4)
    assert closing != -1
    front = text[4:closing]
    assert "name: handoff-cli" in front
    description = front.split("description:", 1)[1]
    assert description.strip()
    body = text[closing:]
    for command in ("handoff execute", "handoff status", "handoff runs"):
        assert command in body


SKILL_DIR = Path("skills/handoff-cli")
CLI_VERBS = {
    "init", "skill", "models", "server", "model", "mode", "planner", "permissions", "status", "approve",
    "execute", "watch", "resume", "cancel", "runs", "archive", "preflight", "template", "qa",
}


def _section(text: str, heading: str) -> str:
    start = text.index(heading)
    end = text.find("\n## ", start + 1)
    return text[start : end if end > 0 else len(text)]


def test_skill_mentions_only_real_cli_verbs_and_links_its_reference() -> None:
    import re

    skill = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    reference = (SKILL_DIR / "reference.md").read_text(encoding="utf-8")
    assert "[reference.md](reference.md)" in skill
    assert len(skill.splitlines()) <= 200
    bin_text = Path("bin/handoff").read_text(encoding="utf-8")
    for verb in CLI_VERBS:
        assert re.search(rf"^\s+(\S+ \| )*{verb}( \|[^)]*)?\)", bin_text, re.MULTILINE), verb
    warning = "There is no `handoff plan` or `handoff drive` subcommand"
    assert warning in skill
    for text in (skill.replace(warning, ""), reference):
        used = set(re.findall(r"`handoff ([a-z-]+)", text))
        assert used <= CLI_VERBS, used - CLI_VERBS


def test_skill_setup_interview_never_silently_chooses() -> None:
    skill = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    setup = _section(skill, "## Setup")
    # Existing state first, via --json, and only missing questions.
    for probe in ('handoff status "<repo>" --json', 'handoff model "<repo>" --json', 'handoff skill status "<repo>" --json'):
        assert probe in setup
    assert setup.index("--json") < setup.index("Ask every open question in one round")
    assert "run-mode question" in setup
    # A2A proposed as default, explicit agreement, legacy fallback with limits, migration offer.
    assert "Use managed A2A (recommended)? Yes/No" in setup and "preselected" in setup
    assert "Silence or" in setup and "not agreement" in setup
    assert "--transport legacy" in setup and "only Claude as Executor" in setup and "HANDOFF_MODEL" in setup
    assert "Migrate to managed A2A (recommended)?" in setup
    # Provider, real model IDs, Codex-only effort, run mode, skill location.
    assert "claude, codex, or cursor" in setup
    assert 'handoff models "<repo>" --provider <p> --json' in setup and "explicit provider-native ID" in setup
    assert "Reasoning effort:** only for codex" in setup
    assert "drive =" in setup and "watch =" in setup and "the human chooses" in setup
    assert "Planner skill location" in setup
    assert "Never choose a transport, provider, model, or mode yourself" in setup
    # Live A7 finding: an empty structured-question result is not an answer.
    assert "The questions end your turn." in setup and "returns no answers" in setup
    assert "never fill a gap with a\n   default" in setup
    # The commands it runs carry exactly the human's choices.
    assert "--transport a2a --executor <p>" in setup and "--mode <drive|watch>" in setup
    assert 'handoff server start "<repo>"' in setup and 'handoff mode "<repo>" <drive|watch>' in setup
    assert "login_command" in setup and "sandbox" in setup


def test_skill_is_mode_aware_and_keeps_the_safety_rules() -> None:
    skill = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    run = _section(skill, "## Approve and run")
    assert "explicit chat approval" in run and "never self-approve" in run
    assert "**drive:**" in run and "**watch:** do not run `handoff execute`" in run
    assert "watcher.running" in run and 'handoff watch "<repo>"' in run
    assert (
        "start it as a background process only if your host supports long-lived background commands and the human agrees"
        in run
    )
    plan = _section(skill, "## Plan")
    assert "**DRAFT**" in plan and "Do not dispatch a DRAFT" in plan
    drive = _section(skill, "## Drive loop")
    assert "three launched executions" in drive and "Merge and push are the human's" in drive
    assert "must **not** run" in _section(skill, "## Boundaries")
    assert "Never hand-edit" in skill
    recovery = _section(skill, "## Status and recovery")
    assert "(also in watch mode)" in recovery
    setup = _section(skill, "## Setup")
    assert "sandboxed" in setup.lower() or "Sandboxed shell" in setup
    for command in ("status", "server", "execute", "resume", "runs", "preflight"):
        assert command in setup
    assert "sandbox.excludedCommands" in setup and "allowLocalBinding" in setup
    assert "OS sandbox" in setup and "permission" in setup
    assert "unsandboxed" in setup
    assert "Never suggest `approve` or `Bash(handoff:*)`" in setup
    assert "probe not permitted" in setup
    assert "10–20 s" in setup
    drive = _section(skill, "## Drive loop")
    assert "handoff preflight" in drive and drive.index("preflight") < drive.index("handoff execute")


def test_skill_appends_later_qa_rounds_and_reports_stale_copies() -> None:
    skill = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    qa = _section(skill, "## QA")
    assert "--append" in qa and "later round" in qa and "earlier rounds" in qa
    assert "planner_skill.stale" in skill and "mention them once" in skill
    assert "for example skillshare" in skill
    assert "Never edit, overwrite, or install over an `elsewhere` copy." in skill
    setup = _section(skill, "## Setup")
    assert "Report a `stale` entry like `outdated`" in setup
    assert "skillshare" in setup
    reference = (SKILL_DIR / "reference.md").read_text(encoding="utf-8")
    assert "--append` from READY FOR QA" in reference
    assert "`stale`" in reference and "planner_skill" in reference


def test_skill_reports_unreadable_locations_without_fixing_permissions() -> None:
    skill = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert skill.count("planner_skill.unknown") == 1
    assert "could not be read" in skill and "stale check skipped" in skill
    assert "not block anything" in skill
    assert "Never try to fix permissions" in skill
    assert "chmod" not in skill
    reference = (SKILL_DIR / "reference.md").read_text(encoding="utf-8")
    assert "`unknown`" in reference and "not_permitted" in reference
    assert "does not block anything" in reference


def test_installed_copies_of_every_host_are_workflow_paths() -> None:
    from handoff_a2a.skills import PROJECT_DIRS
    from handoff_a2a.workspace import is_workflow_path

    for rel in PROJECT_DIRS.values():
        assert is_workflow_path(f"{rel}/reference.md")
    assert not is_workflow_path(".agents/skills/other/SKILL.md")
    assert not is_workflow_path(".claude/skills/handoff-cli-extra/SKILL.md")



def test_non_loopback_config_is_rejected(tmp_path: Path) -> None:
    token = tmp_path / "token"
    token.write_text("abc\n", encoding="utf-8")
    cfg = tmp_path / "server.json"
    cfg.write_text(
        json_dumps(
            {
                "host": "0.0.0.0",
                "port": 9,
                "workspace_id": "x",
                "workspace_path": str(tmp_path),
                "credential_file": str(token),
                "evidence_dir": str(tmp_path / "ev"),
                "claude": {"binary": "/bin/false", "model": "x"},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="loopback"):
        load_server_config(cfg)


def json_dumps(payload: dict) -> str:
    return json.dumps(payload)


def test_manifest_usage_counts_are_integers_after_struct_transport() -> None:
    from handoff_a2a.reporting import usage_fields

    fields = usage_fields(
        {"usage": {"input_tokens": 2428.0, "output_tokens": 0.0, "cache_creation_input_tokens": None}, "cost_usd": 0.0}
    )
    assert fields["usage"] == {"input_tokens": 2428, "output_tokens": 0, "cache_creation_input_tokens": None}
    assert isinstance(fields["usage"]["input_tokens"], int)
    assert fields["cost_usd"] == 0.0


def test_documented_commands_use_real_verbs_and_flags() -> None:
    """Every `handoff ...` line in the README / CLI guide code blocks names a real verb and real flags."""
    import re

    known_flags = set(re.findall(r"(--[a-z][a-z-]+)", Path("bin/handoff").read_text(encoding="utf-8")))
    for source in Path("src/handoff_a2a").glob("*.py"):
        known_flags |= set(re.findall(r'add_argument\(\s*"(--[a-z-]+)"', source.read_text(encoding="utf-8")))
    checked = 0
    for doc in (Path("README.md"), Path("docs/guides/cli-setup-and-models.md")):
        text = doc.read_text(encoding="utf-8")
        for block in re.findall(r"```sh\n(.*?)```", text, re.DOTALL):
            for line in block.replace("\\\n", " ").splitlines():
                line = line.split("#", 1)[0].strip()
                if not line.startswith("handoff "):
                    continue
                verb = line.split()[1]
                assert verb in CLI_VERBS or verb == "--help", (doc, line)
                for flag in re.findall(r"(?<![\w-])(--[a-z][a-z-]+)", line):
                    assert flag in known_flags, (doc, line, flag)
                checked += 1
    assert checked >= 20


def test_template_preamble_is_slim_and_outside_the_hashes() -> None:
    from handoff_a2a.workspace import approved_plan_hash, planner_fingerprint

    raw = Path("templates/HANDOFF.md").read_bytes()
    preamble, sep, rest = raw.partition(b"## Current Task")
    assert sep == b"## Current Task"
    assert len(preamble) <= 3072
    text = preamble.decode()
    assert "OPT-IN ONLY" in text
    assert "Planner" in text and "Executor" in text
    assert "### Executor rules" in text
    assert "### Status values" in text
    assert "Leave every other byte of Current Task and QA Feedback unchanged" in text
    for banned in ("## Protocol", "### Drive mode", "### Rules for PLANNER", "### Rules for EXECUTOR"):
        assert banned not in text
    body = b"## Current Task" + rest
    old = b"legacy preamble with Protocol and Drive mode\n\n" + body
    new = preamble + body
    assert approved_plan_hash(old.decode()) == approved_plan_hash(new.decode())
    assert planner_fingerprint(old.decode()) == planner_fingerprint(new.decode())
    from handoff_a2a.adapters.cursor import rule_text

    assert "### Executor rules" in rule_text("abc", "# x\n")


def test_skill_reads_archive_history_without_opening_the_file() -> None:
    skill = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert 'handoff archive list "<repo>" --json' in skill
    assert 'handoff archive show "<repo>" <id> --section qa|notes|task' in skill
    assert "Never open" in skill and "search `HANDOFF-ARCHIVE.md` directly" in skill
    assert "exclude it when searching the repo" in skill
    template = Path("templates/HANDOFF.md").read_text(encoding="utf-8")
    preamble = template.split("## Current Task", 1)[0]
    assert "Do not read `HANDOFF-ARCHIVE.md`; it is history, not instructions." in preamble
    assert len(preamble.encode()) <= 3072


def test_skill_covers_planner_rules_removed_from_the_template() -> None:
    skill = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert "self-contained" in skill
    assert "gh pr list" in skill
    assert "actual git diff" in skill
    assert "file, problem, what fixed looks like" in skill
    qa = _section(skill, '## QA: "QA the handoff"')
    assert 'handoff qa "<repo>"' in qa
    assert "plan_changed_since_approval: false" in qa
    assert "match headings at the start of a line" in qa
    assert "never with hand edits or scripts" in qa
    assert "nits" in skill
    assert "documentation remains" in skill
    assert "three launched executions" in skill


def test_skill_waiting_does_not_block_or_pipe() -> None:
    skill = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    drive = _section(skill, "## Drive loop")
    watch = _section(skill, "## Approve and run")
    for section in (drive, watch):
        assert "background" in section
        assert "only this command stopped waiting" in section
        assert "still running" in section
        assert "no pipes" in section
        assert "long foreground sleeps" in section
        assert "not permitted" in section
    assert 'handoff execute "<repo>" --wait 900' in drive
    assert 'handoff resume "<repo>" --wait 900' in drive
    assert "--wait" not in watch
    assert "do not run `handoff execute`" in watch
    assert "handoff resume" in drive
    assert "read `reason`" in drive
    assert "when a run starts" in drive
    assert "terminal outcome" in drive
    recovery = _section(skill, "## Status and recovery")
    assert "UNKNOWN" in recovery and "not_permitted" in recovery


def test_template_refresh_keeps_the_task_and_refuses_unsafe_files(tmp_path: Path) -> None:
    import os
    import subprocess

    bin_handoff = Path("bin/handoff").resolve()
    template = Path("templates/HANDOFF.md").read_bytes()
    preamble, _, rest = template.partition(b"## Current Task")
    task = b"## Current Task" + rest.replace(b"**Status:** NO TASK", b"**Status:** READY FOR QA")
    task += b"\nexecutor notes stay\n"
    repo = tmp_path / "repo"
    repo.mkdir()
    handoff = repo / "HANDOFF.md"
    handoff.write_bytes(b"# old protocol\n\nDrive mode lives here.\n\n" + task)
    env = os.environ.copy()

    def run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([str(bin_handoff), *args], cwd=repo, env=env, capture_output=True, text=True)

    first = run("template", "refresh", str(repo))
    assert first.returncode == 0, first.stderr
    assert "replaced the preamble" in first.stdout
    refreshed = handoff.read_bytes()
    assert refreshed.startswith(preamble)
    assert refreshed[len(preamble):] == task
    backups = list((repo / ".handoff-logs").glob("HANDOFF.preamble-*.md"))
    assert len(backups) == 1
    assert backups[0].read_bytes().startswith(b"# old protocol")
    again = run("template", "refresh", str(repo))
    assert again.returncode == 0 and "already current" in again.stdout
    assert handoff.read_bytes() == refreshed
    assert len(list((repo / ".handoff-logs").glob("HANDOFF.preamble-*.md"))) == 1

    missing = tmp_path / "missing"
    missing.mkdir()
    (missing / "HANDOFF.md").write_text("no heading\n", encoding="utf-8")
    refused = run("template", "refresh", str(missing))
    assert refused.returncode != 0 and "no '## Current Task'" in refused.stderr
    assert (missing / "HANDOFF.md").read_text() == "no heading\n"

    dup = tmp_path / "dup"
    dup.mkdir()
    (dup / "HANDOFF.md").write_bytes(task + b"\n" + task)
    refused_dup = run("template", "refresh", str(dup))
    assert refused_dup.returncode != 0 and "appears 2 times" in refused_dup.stderr
    assert (dup / "HANDOFF.md").read_bytes() == task + b"\n" + task

    outstanding = tmp_path / "busy"
    outstanding.mkdir()
    (outstanding / "HANDOFF.md").write_bytes(b"# old\n\n" + task)
    (outstanding / ".handoff-logs").mkdir()
    (outstanding / ".handoff-logs" / "outstanding.json").write_text("{}\n", encoding="utf-8")
    before = (outstanding / "HANDOFF.md").read_bytes()
    refused_run = run("template", "refresh", str(outstanding))
    assert refused_run.returncode != 0 and "outstanding" in refused_run.stderr
    assert (outstanding / "HANDOFF.md").read_bytes() == before
    assert not list((outstanding / ".handoff-logs").glob("HANDOFF.preamble-*.md"))


def test_init_names_refresh_only_when_the_preamble_differs(tmp_path: Path) -> None:
    import os
    import subprocess

    bin_handoff = Path("bin/handoff").resolve()
    template = Path("templates/HANDOFF.md").read_bytes()
    env = os.environ.copy()

    def init_repo(name: str, body: bytes) -> subprocess.CompletedProcess[str]:
        repo = tmp_path / name
        repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"], check=True)
        (repo / "HANDOFF.md").write_bytes(body)
        (repo / "keep.txt").write_text("x\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "keep.txt"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "init"], check=True)
        before = (repo / "HANDOFF.md").read_bytes()
        result = subprocess.run(
            [str(bin_handoff), "init", str(repo), "--transport", "legacy"],
            env=env, capture_output=True, text=True,
        )
        assert (repo / "HANDOFF.md").read_bytes() == before
        return result

    stale = init_repo("stale", b"# old\n\n## Current Task\n\n**Status:** DRAFT\n")
    assert stale.returncode == 0, stale.stderr
    assert "already initialized" in stale.stdout
    assert 'handoff template refresh' in stale.stdout
    current = init_repo("current", template)
    assert current.returncode == 0, current.stderr
    assert "already initialized" in current.stdout
    assert "template refresh" not in current.stdout
    crlf = template.replace(b"\n", b"\r\n")
    crlf_repo = init_repo("crlf", crlf)
    assert crlf_repo.returncode == 0, crlf_repo.stderr
    assert "template refresh" not in crlf_repo.stdout

    locked = tmp_path / "locked-tmp"
    locked.mkdir()
    locked.chmod(0o500)
    locked_env = dict(env)
    locked_env["TMPDIR"] = str(locked)
    repo = tmp_path / "locked"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"], check=True)
    (repo / "HANDOFF.md").write_bytes(b"# old\n\n## Current Task" + template.split(b"## Current Task", 1)[1])
    (repo / "keep.txt").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "keep.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "init"], check=True)
    before = (repo / "HANDOFF.md").read_bytes()
    locked_init = subprocess.run(
        [str(bin_handoff), "init", str(repo), "--transport", "legacy"],
        env=locked_env, capture_output=True, text=True,
    )
    assert locked_init.returncode == 0, locked_init.stderr
    assert "template refresh" in locked_init.stdout
    assert (repo / "HANDOFF.md").read_bytes() == before
    locked.chmod(0o700)


def test_permission_denied_status_is_unknown_not_unresolved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    import asyncio
    import errno

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "HANDOFF.md").write_text(
        "## Current Task\n\n**Status:** READY FOR QA\n\n**Branch:** main\n\n## Execution Notes\n\nx\n\n## QA Feedback\n\n",
        encoding="utf-8",
    )
    token = repo / "token"
    token.write_text("secret\n", encoding="utf-8")
    logs = repo / ".handoff-logs"
    logs.mkdir()
    (logs / "outstanding.json").write_text(
        '{"run_id":"r","task_id":"t","run_record":"missing.json","credential_file":"' + str(token) + '"}\n',
        encoding="utf-8",
    )
    from handoff_a2a.integration import cmd_status_async

    async def denied(**_kwargs):
        raise PermissionError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr("handoff_a2a.integration.status_from_record", denied)
    assert asyncio.run(cmd_status_async(repo, as_json=True)) == 2
    body = json.loads(capsys.readouterr().out)
    assert body["execution"] == "UNKNOWN"
    assert body["probe"] == "not_permitted"
    assert "not permitted" in body["reason"]
    assert body["turn"] == "WAIT"
    assert "QA" not in body["next"]

    async def down(**_kwargs):
        raise ConnectionError("connection refused")

    monkeypatch.setattr("handoff_a2a.integration.status_from_record", down)
    assert asyncio.run(cmd_status_async(repo, as_json=True)) == 2
    other = json.loads(capsys.readouterr().out)
    assert other["execution"] == "UNRESOLVED"
    assert other["probe"] is None


def test_executor_active_state_is_unknown_when_probe_is_denied(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from handoff_a2a.selection import describe
    from handoff_a2a.service import PROBE_NOT_PERMITTED, Managed, ServiceState

    state = ServiceState(
        running=False, verified=False, port=9, generation=1, provider="cursor", model="m", probe=PROBE_NOT_PERMITTED
    )
    state.detail.append("unknown (probe not permitted; run outside the sandbox)")
    monkeypatch.setattr("handoff_a2a.selection.inspect", lambda _managed: state)
    monkeypatch.setattr("handoff_a2a.selection.load_pending", lambda _managed: None)
    managed = Managed(
        paths=type("Paths", (), {"repo": tmp_path})(),
        config=None,  # type: ignore[arg-type]
        server={"port": 9, "cursor": {"model": "m"}, "selection": {}},
    )
    assert describe(managed)["active"]["state"] == "unknown"
