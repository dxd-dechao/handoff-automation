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

# ── Final report ─────────────────────────────────────────────────────────────
echo ""
echo "═══════════════════════════════════════"
echo "  Results: $pass passed, $fail failed"
echo "═══════════════════════════════════════"

[[ $fail -eq 0 ]] || exit 1
