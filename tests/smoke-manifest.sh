#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HANDOFF_BIN="$SCRIPT_DIR/../bin/handoff"
TMPDIR_BASE="$(mktemp -d "${TMPDIR:-/tmp}/handoff-smoke.XXXXXX")"
trap 'rm -rf "$TMPDIR_BASE"' EXIT INT TERM

pass=0
fail=0

assert() {
  local desc="$1"; shift
  if "$@"; then
    echo "  PASS: $desc"
    pass=$((pass + 1))
  else
    echo "  FAIL: $desc"
    fail=$((fail + 1))
  fi
}

assert_eq() {
  local desc="$1" expected="$2" actual="$3"
  if [[ "$expected" == "$actual" ]]; then
    echo "  PASS: $desc"
    pass=$((pass + 1))
  else
    echo "  FAIL: $desc (expected '$expected', got '$actual')"
    fail=$((fail + 1))
  fi
}

# ── Syntax gate ──────────────────────────────────────────────────────────────
echo "=== Syntax check ==="
assert "bash -n bin/handoff" bash -n "$HANDOFF_BIN"

# ── Build temp repo ──────────────────────────────────────────────────────────
echo ""
echo "=== Setting up temp repo ==="
REPO="$TMPDIR_BASE/test-repo"
mkdir -p "$REPO"
git -C "$REPO" init -q
git -C "$REPO" config user.email "test@test"
git -C "$REPO" config user.name "Test"
echo "init" > "$REPO/init.txt"
git -C "$REPO" add -A
git -C "$REPO" commit -q -m "initial commit"

cat > "$REPO/HANDOFF.md" <<'HANDOFF'
# HANDOFF.md

## Current Task

**Status:** READY FOR EXECUTION

**Branch:** main

### Goal

Test task
HANDOFF

mkdir -p "$REPO/.handoff-logs"

# ── Stub claude (success) ────────────────────────────────────────────────────
STUB_SUCCESS="$TMPDIR_BASE/claude-stub-success"
cat > "$STUB_SUCCESS" <<'STUB'
#!/usr/bin/env bash
[[ -n "${SMOKE_ARGV:-}" ]] && printf '%s\n' "$@" > "$SMOKE_ARGV"
# Make a commit in the repo (cwd = repo)
echo "hello" > hello.txt
git add hello.txt
git commit -q -m "feat: create hello.txt"
# Rewrite status
sed -i.bak 's/\*\*Status:\*\* READY FOR EXECUTION/**Status:** READY FOR QA/' HANDOFF.md
rm -f HANDOFF.md.bak
# Print JSON result to stdout
cat <<'JSON'
{"type":"result","subtype":"success","is_error":false,"duration_ms":31000,"duration_api_ms":28000,"num_turns":6,"result":"Done. Created hello.txt.","session_id":"stub-session-123","total_cost_usd":0.0421,"usage":{"input_tokens":812,"cache_creation_input_tokens":5000,"cache_read_input_tokens":20000,"output_tokens":950}}
JSON
STUB
chmod +x "$STUB_SUCCESS"

# ── Run 1: success path ──────────────────────────────────────────────────────
echo ""
echo "=== Run 1: success path ==="
export HANDOFF_CLAUDE_BIN="$STUB_SUCCESS"
export SMOKE_ARGV="$TMPDIR_BASE/claude-argv.txt"
RUN1_OUT="$TMPDIR_BASE/run1-stdout.txt"
"$HANDOFF_BIN" execute "$REPO" > "$RUN1_OUT" 2>&1 || true

# Find the manifest
MANIFEST1="$(find "$REPO/.handoff-logs" -name '*-manifest.json' | sort | head -n1)"
assert "manifest exists" test -f "$MANIFEST1"

assert_eq ".exit_code == 0" "0" "$(jq '.exit_code' "$MANIFEST1")"
assert_eq ".status_after" "READY FOR QA" "$(jq -r '.status_after' "$MANIFEST1")"
assert_eq ".claude.usage.input_tokens" "812" "$(jq '.claude.usage.input_tokens' "$MANIFEST1")"
assert_eq ".claude.total_cost_usd" "0.0421" "$(jq '.claude.total_cost_usd' "$MANIFEST1")"
assert_eq ".claude.session_id" "stub-session-123" "$(jq -r '.claude.session_id' "$MANIFEST1")"
assert_eq ".git.commits | length == 1" "1" "$(jq '.git.commits | length' "$MANIFEST1")"
assert_eq ".claude.is_error == false" "false" "$(jq '.claude.is_error' "$MANIFEST1")"

RUN1_ID="$(jq -r '.run_id' "$MANIFEST1")"
assert "summary mentions run id" grep -q "$RUN1_ID" "$RUN1_OUT"
assert "executor launch denies the handoff CLI and skill" \
  grep -qxF 'Bash(handoff:*),Bash(handoff-a2a:*),Bash(*/handoff:*),Bash(*/handoff-a2a:*),Skill(handoff-cli)' "$SMOKE_ARGV"

# ── Run 2: failure path ──────────────────────────────────────────────────────
echo ""
echo "=== Run 2: failure path ==="

# Reset status for next run
sed -i.bak 's/\*\*Status:\*\* READY FOR QA/**Status:** READY FOR EXECUTION/' "$REPO/HANDOFF.md"
rm -f "$REPO/HANDOFF.md.bak"

STUB_FAIL="$TMPDIR_BASE/claude-stub-fail"
cat > "$STUB_FAIL" <<'STUB'
#!/usr/bin/env bash
# Print garbage (not JSON) and exit with code 3
echo "this is not json at all"
exit 3
STUB
chmod +x "$STUB_FAIL"

export HANDOFF_CLAUDE_BIN="$STUB_FAIL"
RUN2_OUT="$TMPDIR_BASE/run2-stdout.txt"
RUN2_RC=0
"$HANDOFF_BIN" execute "$REPO" > "$RUN2_OUT" 2>&1 || RUN2_RC=$?

assert_eq "exit code propagated" "3" "$RUN2_RC"

# Find second manifest
MANIFEST2="$(find "$REPO/.handoff-logs" -name '*-manifest.json' | sort | tail -n1)"
assert "second manifest exists" test -f "$MANIFEST2"
assert "two manifests total" test "$(find "$REPO/.handoff-logs" -name '*-manifest.json' | wc -l | tr -d ' ')" = "2"

assert_eq ".exit_code == 3" "3" "$(jq '.exit_code' "$MANIFEST2")"
assert_eq ".claude.session_id == null" "null" "$(jq '.claude.session_id' "$MANIFEST2")"

# ── handoff runs ─────────────────────────────────────────────────────────────
echo ""
echo "=== handoff runs ==="
RUNS_OUT="$TMPDIR_BASE/runs-stdout.txt"
"$HANDOFF_BIN" runs "$REPO" > "$RUNS_OUT" 2>&1

assert "runs output contains run1 id" grep -q "$RUN1_ID" "$RUNS_OUT"
RUN2_ID="$(jq -r '.run_id' "$MANIFEST2")"
assert "runs output contains run2 id" grep -q "$RUN2_ID" "$RUNS_OUT"

# ── Legacy --json and run mode (Python-free) ────────────────────────────────
echo ""
echo "=== legacy status --json / mode refusal ==="
STATUS_JSON="$(HANDOFF_A2A_BIN="$TMPDIR_BASE/no-a2a" "$HANDOFF_BIN" status "$REPO" --json)"
assert_eq "status --json kind/transport/managed" "status legacy false urn:handoff-automation:cli-output:v1" \
  "$(jq -r '[.kind, .transport, (.managed | tostring), .schema] | join(" ")' <<<"$STATUS_JSON")"
MODE_RC=0
HANDOFF_A2A_BIN="$TMPDIR_BASE/no-a2a" "$HANDOFF_BIN" mode "$REPO" drive > "$TMPDIR_BASE/mode.txt" 2>&1 || MODE_RC=$?
assert_eq "legacy mode change refused (exit 2)" "2" "$MODE_RC"
assert "refusal names the managed A2A migration" grep -q "requires managed A2A setup" "$TMPDIR_BASE/mode.txt"
assert_eq "legacy status preflight is null" "null" "$(jq -r '.preflight' <<<"$STATUS_JSON")"
PRE_RC=0
HANDOFF_A2A_BIN="$TMPDIR_BASE/no-a2a" "$HANDOFF_BIN" preflight "$REPO" > "$TMPDIR_BASE/preflight.txt" 2>&1 || PRE_RC=$?
assert_eq "legacy preflight refuses without Python" "1" "$PRE_RC"
assert "legacy preflight names managed A2A" grep -q "preflight needs managed A2A" "$TMPDIR_BASE/preflight.txt"

# ── template refresh (Python-free) ───────────────────────────────────────────
echo ""
echo "=== template refresh without Python ==="
OLD_PATH="$PATH"
export PATH="/usr/bin:/bin"
REFRESH_REPO="$TMPDIR_BASE/refresh-repo"
mkdir -p "$REFRESH_REPO"
TASK_BODY="$(awk 'found || $0 == "## Current Task" { found=1 } found { print }' "$SCRIPT_DIR/../templates/HANDOFF.md")"
printf '%s\n' "# stale preamble" "" "$TASK_BODY" > "$REFRESH_REPO/HANDOFF.md"
BEFORE_TAIL="$(awk 'found || $0 == "## Current Task" { found=1 } found { print }' "$REFRESH_REPO/HANDOFF.md")"
"$HANDOFF_BIN" template refresh "$REFRESH_REPO" > "$TMPDIR_BASE/refresh.txt"
assert "refresh replaces the preamble" grep -q "replaced the preamble" "$TMPDIR_BASE/refresh.txt"
AFTER_TAIL="$(awk 'found || $0 == "## Current Task" { found=1 } found { print }' "$REFRESH_REPO/HANDOFF.md")"
assert_eq "task bytes unchanged" "$BEFORE_TAIL" "$AFTER_TAIL"
assert "preamble backup exists" test -n "$(find "$REFRESH_REPO/.handoff-logs" -name 'HANDOFF.preamble-*.md' -print -quit)"
"$HANDOFF_BIN" template refresh "$REFRESH_REPO" > "$TMPDIR_BASE/refresh-again.txt"
assert "second refresh is already current" grep -q "already current" "$TMPDIR_BASE/refresh-again.txt"

CRLF_REPO="$TMPDIR_BASE/crlf-repo"
mkdir -p "$CRLF_REPO"
python3 - "$SCRIPT_DIR/../templates/HANDOFF.md" "$CRLF_REPO/HANDOFF.md" "$TMPDIR_BASE/crlf-task.bin" <<'PY'
import sys
from pathlib import Path
template = Path(sys.argv[1]).read_bytes()
_preamble, sep, rest = template.partition(b"## Current Task")
body = (b"# stale\n\n" + sep + rest).replace(b"\n", b"\r\n")
path = Path(sys.argv[2])
path.write_bytes(body)
path.chmod(0o644)
Path(sys.argv[3]).write_bytes(body[body.index(b"## Current Task"):])
PY
"$HANDOFF_BIN" template refresh "$CRLF_REPO" > "$TMPDIR_BASE/crlf-refresh.txt"
assert "CRLF refresh succeeds" grep -q "replaced the preamble" "$TMPDIR_BASE/crlf-refresh.txt"
python3 - "$CRLF_REPO/HANDOFF.md" "$TMPDIR_BASE/crlf-task.bin" <<'PY'
import sys
from pathlib import Path
path = Path(sys.argv[1])
data = path.read_bytes()
task = Path(sys.argv[2]).read_bytes()
assert data.endswith(task), "task bytes changed"
preamble = data[: data.index(b"## Current Task")]
assert preamble.endswith(b"\r\n"), preamble[-8:]
assert b"\n" not in preamble.replace(b"\r\n", b"")
assert (path.stat().st_mode & 0o777) == 0o644
PY
assert "CRLF task bytes and mode 0644" test $? -eq 0
export PATH="$OLD_PATH"

echo ""
echo "=== --wait refusal ==="
WAIT_RC=0
"$HANDOFF_BIN" execute --wait 0 "$REPO" > "$TMPDIR_BASE/wait.txt" 2>&1 || WAIT_RC=$?
assert_eq "--wait 0 refused" "1" "$WAIT_RC"
assert "--wait message" grep -q "positive number up to 1800" "$TMPDIR_BASE/wait.txt"
WAIT_RC=0
"$HANDOFF_BIN" resume --wait abc "$REPO" > "$TMPDIR_BASE/wait-abc.txt" 2>&1 || WAIT_RC=$?
assert_eq "--wait abc refused" "1" "$WAIT_RC"

# ── Legacy qa refusal and archive structure check ────────────────────────────
echo ""
echo "=== legacy qa and archive structure ==="
printf 'approved\n' > "$TMPDIR_BASE/qa.txt"
QA_RC=0
HANDOFF_A2A_BIN="$TMPDIR_BASE/no-a2a" "$HANDOFF_BIN" qa "$REPO" --status APPROVED --file "$TMPDIR_BASE/qa.txt" \
  > "$TMPDIR_BASE/qa.txt.out" 2>&1 || QA_RC=$?
assert_eq "legacy qa refuses" "1" "$QA_RC"
assert "legacy qa names the hand edit" grep -q "handoff qa is A2A only; on legacy, edit Status and QA Feedback in HANDOFF.md by hand" "$TMPDIR_BASE/qa.txt.out"
STRUCT="$TMPDIR_BASE/struct-repo"
mkdir -p "$STRUCT"
git -C "$STRUCT" init -q
cat > "$STRUCT/HANDOFF.md" <<'EOF'
## Current Task

**Status:** APPROVED

### Goal

Keep

## Execution Notes

done

## QA Feedback

ok

## QA Feedback

again
EOF
ARCH_RC=0
"$HANDOFF_BIN" archive "$STRUCT" > "$TMPDIR_BASE/arch.txt" 2>&1 || ARCH_RC=$?
assert_eq "legacy archive refuses a duplicated heading" "1" "$ARCH_RC"
assert "legacy archive names the heading" grep -q '## QA Feedback' "$TMPDIR_BASE/arch.txt"
assert "archive file was not created" test ! -f "$STRUCT/HANDOFF-ARCHIVE.md"

# ── Archive list/show and the duplicate guard, with no Python ───────────────
echo ""
echo "=== archive list/show without Python ==="
NOPY="$TMPDIR_BASE/nopython-path"
mkdir -p "$NOPY"
printf '#!/bin/sh\necho "python invoked" >&2\nexit 99\n' > "$NOPY/python3"
cp "$NOPY/python3" "$NOPY/python"
chmod +x "$NOPY/python3" "$NOPY/python"
JQ_DIR="$(dirname "$(command -v jq)")"
LIST_PATH="$PATH"
# The python stub is first, so list/show cannot call Python. jq stays the real binary.
export PATH="$NOPY:$JQ_DIR:/usr/bin:/bin"
hash -r

LIST_REPO="$TMPDIR_BASE/list-repo"
mkdir -p "$LIST_REPO"
printf '%s\n' '# HANDOFF.md' '' '## Current Task' '' '**Status:** DRAFT' > "$LIST_REPO/HANDOFF.md"
LONG_GOAL="$(printf 'x%.0s' $(seq 1 180))"
E1='# Archived 2026-09-01 09:00 — Keep the "first" goal
Disposition: archived
Workflow-ID: wf-1

## Current Task

**Status:** APPROVED

**Task ID:** A-ONE

quote `## QA Feedback` inline

## Execution Notes

notes one

## QA Feedback

qa one
'
E2="# Superseded 2026-09-02 10:00 — A smaller follow-up ${LONG_GOAL}
Disposition: **superseded, not approved**
Workflow-ID: wf-2

## Current Task

**Status:** READY FOR QA

**Task ID:** A-TWO

## Execution Notes

notes two

## QA Feedback

qa two
"
E3='# Archived 2026-09-03 11:00 — No disposition on this one
Workflow-ID: wf-3

## Current Task

**Status:** APPROVED

**Task ID:** A-DUP

## Execution Notes

notes three

## QA Feedback

qa three
'
E4='# Archived 2026-09-04 12:00 — Duplicate id later
Disposition: archived

## Current Task

**Status:** APPROVED

**Task ID:** A-DUP

## Execution Notes

notes four

## QA Feedback

qa four
'
E5=$'# Archived 2026-09-05 13:00 — CRLF entry\r\n\r\n## Current Task\r\n\r\n**Status:** APPROVED\r\n\r\n**Task ID:** A-CRLF\r\n\r\n## Execution Notes\r\n\r\nnotes\r\n\r\n## QA Feedback\r\n\r\nqa\r\n'
PREAMBLE='# HANDOFF Archive

Completed tasks, appended verbatim by `handoff archive`. Local-only (git-excluded).
'
{
  printf '%s' "$PREAMBLE"
  printf '\n---\n\n%s' "$E1"
  printf '\n---\n\n%s' "$E2"
  printf '\n---\n\n%s' "$E3"
  printf '\n---\n\n%s' "$E4"
  printf '\n---\n\n%s' "$E5"
} > "$LIST_REPO/HANDOFF-ARCHIVE.md"
printf '%s' "$E4" > "$TMPDIR_BASE/e4.txt"
printf '%s' "$E5" > "$TMPDIR_BASE/e5.txt"
BEFORE_CKSUM="$(cksum "$LIST_REPO/HANDOFF-ARCHIVE.md")"
"$HANDOFF_BIN" archive list "$LIST_REPO" > "$TMPDIR_BASE/list.txt"
assert_eq "list prints five entries" "5" "$(wc -l < "$TMPDIR_BASE/list.txt" | tr -d ' ')"
assert "list names the superseded entry" grep -q 'A-TWO superseded' "$TMPDIR_BASE/list.txt"
assert "list keeps both copies of a task id" test "$(grep -c 'A-DUP' "$TMPDIR_BASE/list.txt")" -eq 2
assert "list names the CRLF entry" grep -q 'A-CRLF archived' "$TMPDIR_BASE/list.txt"
LIST_FIT=0
while IFS= read -r list_line; do
  if ((${#list_line} > 120)); then LIST_FIT=1; fi
done < "$TMPDIR_BASE/list.txt"
assert_eq "list lines fit 120 columns" "0" "$LIST_FIT"
assert "long goal is truncated" grep -q '\.\.\.$' "$TMPDIR_BASE/list.txt"
assert_eq "list does not append" "$BEFORE_CKSUM" "$(cksum "$LIST_REPO/HANDOFF-ARCHIVE.md")"
"$HANDOFF_BIN" archive list "$LIST_REPO" --json > "$TMPDIR_BASE/list.json"
assert_eq "list json kind" "archive_list" "$(jq -r .kind "$TMPDIR_BASE/list.json")"
assert_eq "list json schema" "urn:handoff-automation:cli-output:v1" "$(jq -r .schema "$TMPDIR_BASE/list.json")"
assert_eq "list json count" "5" "$(jq '.entries | length' "$TMPDIR_BASE/list.json")"
assert_eq "missing disposition is null" "null" "$(jq -r '.entries[2].disposition' "$TMPDIR_BASE/list.json")"
assert_eq "missing workflow id is null" "null" "$(jq -r '.entries[3].workflow_id' "$TMPDIR_BASE/list.json")"
assert_eq "superseded kind" "superseded" "$(jq -r '.entries[1].kind' "$TMPDIR_BASE/list.json")"
"$HANDOFF_BIN" archive show "$LIST_REPO" A-DUP > "$TMPDIR_BASE/dup.txt" 2> "$TMPDIR_BASE/dup.err"
assert "show prints the last duplicate" cmp -s "$TMPDIR_BASE/dup.txt" "$TMPDIR_BASE/e4.txt"
assert "duplicate indexes on stderr" grep -q '2 entries match A-DUP (indexes 3, 4)' "$TMPDIR_BASE/dup.err"
"$HANDOFF_BIN" archive show "$LIST_REPO" A-CRLF > "$TMPDIR_BASE/crlf-show.txt"
assert "show keeps CRLF bytes" cmp -s "$TMPDIR_BASE/crlf-show.txt" "$TMPDIR_BASE/e5.txt"
"$HANDOFF_BIN" archive show "$LIST_REPO" A-ONE --section task > "$TMPDIR_BASE/section-task.txt"
assert "task section keeps the inline heading quote" grep -F -q 'quote `## QA Feedback` inline' "$TMPDIR_BASE/section-task.txt"
if grep -qx '## Execution Notes' "$TMPDIR_BASE/section-task.txt" || grep -qx '## QA Feedback' "$TMPDIR_BASE/section-task.txt"; then
  assert "inline QA heading does not split the task" false
else
  assert "inline QA heading does not split the task" true
fi
SHOW_RC=0
"$HANDOFF_BIN" archive show "$LIST_REPO" NOPE > /dev/null 2> "$TMPDIR_BASE/missing.err" || SHOW_RC=$?
assert_eq "unknown id exits 1" "1" "$SHOW_RC"
assert "unknown id names list" grep -q 'no archived task NOPE; see handoff archive list' "$TMPDIR_BASE/missing.err"
EMPTY_REPO="$TMPDIR_BASE/empty-archive"
mkdir -p "$EMPTY_REPO"
printf '%s\n' '# HANDOFF.md' '' '## Current Task' '' '**Status:** DRAFT' > "$EMPTY_REPO/HANDOFF.md"
"$HANDOFF_BIN" archive list "$EMPTY_REPO" > "$TMPDIR_BASE/empty.txt"
assert_eq "no archive yet" "no archive yet" "$(cat "$TMPDIR_BASE/empty.txt")"
"$HANDOFF_BIN" archive list "$EMPTY_REPO" --json > "$TMPDIR_BASE/empty.json"
assert_eq "empty entries" "0" "$(jq '.entries | length' "$TMPDIR_BASE/empty.json")"

GUARD_REPO="$TMPDIR_BASE/guard-repo"
mkdir -p "$GUARD_REPO"
cat > "$GUARD_REPO/HANDOFF.md" <<'EOF'
# HANDOFF.md

## Current Task

**Status:** APPROVED

### Goal

Keep the gate

---

## Execution Notes

done

---

## QA Feedback

ok
EOF
"$HANDOFF_BIN" archive "$GUARD_REPO" > "$TMPDIR_BASE/guard1.txt"
cp "$GUARD_REPO/HANDOFF-ARCHIVE.md" "$TMPDIR_BASE/guard-once.md"
GUARD_RC=0
"$HANDOFF_BIN" archive "$GUARD_REPO" > "$TMPDIR_BASE/guard2.txt" 2> "$TMPDIR_BASE/guard2.err" || GUARD_RC=$?
assert_eq "second archive exits 1" "1" "$GUARD_RC"
assert "second archive names the entry" grep -q 'already archived as entry 1' "$TMPDIR_BASE/guard2.err"
assert "second archive tells the planner to continue" grep -q 'plan the next task first' "$TMPDIR_BASE/guard2.err"
assert "second archive leaves the file unchanged" cmp -s "$GUARD_REPO/HANDOFF-ARCHIVE.md" "$TMPDIR_BASE/guard-once.md"
sed 's/APPROVED/READY FOR QA/' "$GUARD_REPO/HANDOFF.md" > "$TMPDIR_BASE/guard-status.md"
mv "$TMPDIR_BASE/guard-status.md" "$GUARD_REPO/HANDOFF.md"
GUARD_RC=0
"$HANDOFF_BIN" archive "$GUARD_REPO" > /dev/null 2> "$TMPDIR_BASE/guard-status.err" || GUARD_RC=$?
assert_eq "status-only archive exits 1" "1" "$GUARD_RC"
assert "status-only archive is still a duplicate" cmp -s "$GUARD_REPO/HANDOFF-ARCHIVE.md" "$TMPDIR_BASE/guard-once.md"
sed 's/Keep the gate/A different plan/' "$GUARD_REPO/HANDOFF.md" > "$TMPDIR_BASE/guard-next.md"
mv "$TMPDIR_BASE/guard-next.md" "$GUARD_REPO/HANDOFF.md"
"$HANDOFF_BIN" archive "$GUARD_REPO" > "$TMPDIR_BASE/guard3.txt"
assert_eq "a different task archives" "2" "$("$HANDOFF_BIN" archive list "$GUARD_REPO" | wc -l | tr -d ' ')"
SUPER_RC=0
"$HANDOFF_BIN" archive --superseded "$GUARD_REPO" > /dev/null 2> "$TMPDIR_BASE/super.err" || SUPER_RC=$?
assert_eq "legacy superseded still needs A2A" "1" "$SUPER_RC"
assert "legacy superseded names A2A" grep -q 'archive --superseded requires A2A workflow state' "$TMPDIR_BASE/super.err"

export PATH="$LIST_PATH"
hash -r

# ── Final report ─────────────────────────────────────────────────────────────
echo ""
echo "═══════════════════════════════════════"
echo "  Results: $pass passed, $fail failed"
echo "═══════════════════════════════════════"

[[ $fail -eq 0 ]] || exit 1
