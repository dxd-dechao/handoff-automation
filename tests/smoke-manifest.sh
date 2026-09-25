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
export PATH="$OLD_PATH"

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

# ── Final report ─────────────────────────────────────────────────────────────
echo ""
echo "═══════════════════════════════════════"
echo "  Results: $pass passed, $fail failed"
echo "═══════════════════════════════════════"

[[ $fail -eq 0 ]] || exit 1
