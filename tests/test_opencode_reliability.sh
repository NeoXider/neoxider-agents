#!/usr/bin/env bash
# Reliability tests for providers/opencode/provider.sh (added 2026-09-30):
#   1. small_model is pinned to the run's model (a dead local small model must not kill a session)
#   2. a transient provider failure is retried CONTINUING THE SAME SESSION; rate limits are not
#   3. the retry never runs more often than AGENT_OPENCODE_RETRIES
# Uses a shell-function stub for `opencode` exactly like tests/test_agent_sh.sh does.
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AGENT_TEST_WORKTREE="$HERE"
case "$(uname -s 2>/dev/null)" in
    MINGW*|MSYS*) AGENT_TEST_ROOT="${AGENT_TEST_ROOT:-D:/Temp/agents-core/legacy-tests}" ;;
    *) AGENT_TEST_ROOT="${AGENT_TEST_ROOT:-${TMPDIR:-/tmp}/agents-core/legacy-tests}" ;;
esac
mkdir -p "$AGENT_TEST_ROOT" || exit 1
SCRATCH_LOGS="$(mktemp -d "$AGENT_TEST_ROOT/opencode-logs.XXXXXX")" || exit 1
export AGENT_CLI_LOGS="$SCRATCH_LOGS"
# "list" is the harmless read-only subcommand the main test file uses to source agent.sh
# shellcheck disable=SC1091
source "$AGENT_TEST_WORKTREE/legacy/agent.sh" list >/dev/null 2>&1
# shellcheck disable=SC1091
source "$AGENT_TEST_WORKTREE/legacy/providers/opencode/provider.sh"

PASS=0; FAIL=0
ok()   { PASS=$((PASS + 1)); printf 'PASS  %s\n' "$1"; }
bad()  { FAIL=$((FAIL + 1)); printf 'FAIL  %s\n' "$1"; }
eq()   { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 (expected [$2], got [$3])"; fi; }
match(){ if printf '%s' "$3" | grep -Eq -- "$2"; then ok "$1"; else bad "$1 (no match for [$2] in [$3])"; fi; }

SCRATCH="$(mktemp -d "$AGENT_TEST_ROOT/opencode-work.XXXXXX")" || exit 1
trap 'rm -rf "$SCRATCH" "$SCRATCH_LOGS"' EXIT
export AGENT_OPENCODE_TIMEOUT_SEC=0 AGENT_OPENCODE_TOOL_KEEPALIVE=0

# --- 1. pin env ----------------------------------------------------------------------------
unset OPENCODE_CONFIG_CONTENT AGENT_OPENCODE_KEEP_SMALL_MODEL
pin="$(_provider_opencode_pin_env "opencode/space-bunny-free")"
match "pin_env always emits a marker var" '^AGENT_OPENCODE_WRAPPED=1' "$pin"
match "pin_env pins small_model to the run model" 'OPENCODE_CONFIG_CONTENT=\{"small_model":"opencode/space-bunny-free"\}' "$pin"
eq    "pin_env with no model emits only the marker" "AGENT_OPENCODE_WRAPPED=1" "$(_provider_opencode_pin_env "")"
eq    "pin_env refuses an id it would have to JSON-escape" "AGENT_OPENCODE_WRAPPED=1" "$(_provider_opencode_pin_env 'a"b')"
eq    "pin_env honours KEEP_SMALL_MODEL=1" "AGENT_OPENCODE_WRAPPED=1" "$(AGENT_OPENCODE_KEEP_SMALL_MODEL=1 _provider_opencode_pin_env x/y)"
eq    "pin_env respects a caller-provided OPENCODE_CONFIG_CONTENT" "AGENT_OPENCODE_WRAPPED=1" "$(OPENCODE_CONFIG_CONTENT='{}' _provider_opencode_pin_env x/y)"
eq    "model_of finds -m" "opencode/x" "$(_provider_opencode_model_of --auto -m opencode/x --variant high)"
eq    "model_of is empty without -m" "" "$(_provider_opencode_model_of --auto)"

# --- 2. the child really receives the pinned config ------------------------------------------
opencode() {
    printf '%s\n' "${OPENCODE_CONFIG_CONTENT:-UNSET}" > "$SCRATCH/seen-config"
    printf '%s\n' '{"type":"text","sessionID":"ses_a","part":{"id":"p1","text":"OK"}}'
}
provider_opencode_run_cmd "$SCRATCH" "opencode/space-bunny-free" "" "hi" >/dev/null 2>&1
match "the launched CLI sees small_model pinned" '"small_model":"opencode/space-bunny-free"' "$(cat "$SCRATCH/seen-config")"
eq    "the pin does not leak into the caller's environment" "" "${OPENCODE_CONFIG_CONTENT:-}"

# --- 3. transient failure -> retry continues the same session ---------------------------------
export AGENT_OPENCODE_RETRIES=2
sleep() { :; }   # no real backoff in tests
CALLS="$SCRATCH/calls"; : > "$CALLS"
opencode() {
    printf '%s\n' "$*" >> "$CALLS"
    if [ "$(wc -l < "$CALLS")" -eq 1 ]; then
        printf '%s\n' '{"type":"step_start","sessionID":"ses_keep"}'
        printf 'stream error providerID=bonsai-local small=true\n' >&2
        return 1
    fi
    printf '%s\n' '{"type":"text","sessionID":"ses_keep","part":{"id":"p1","text":"RECOVERED"}}'
}
out="$(provider_opencode_run_cmd "$SCRATCH" "opencode/space-bunny-free" "" "do the long task" 2>"$SCRATCH/err")"
eq    "transient failure ends in success after one retry" "2" "$(wc -l < "$CALLS" | tr -d ' ')"
match "retry answer is delivered" 'RECOVERED' "$out"
match "second attempt continues the SAME session" '-s ses_keep' "$(sed -n 2p "$CALLS")"
match "second attempt carries the continue prompt, not the original" 'transient provider/network error' "$(sed -n 2p "$CALLS")"
match "the retry is announced in the log" 'transient provider/network failure .*retry 1/2' "$(cat "$SCRATCH/err")"

# --- 4. rate limit is NOT retried ------------------------------------------------------------
: > "$CALLS"
opencode() {
    printf '%s\n' "$*" >> "$CALLS"
    printf 'AI_APICallError: Rate limit exceeded (429)\n' >&2
    return 1
}
provider_opencode_run_cmd "$SCRATCH" "opencode/x" "" "p" >/dev/null 2>&1
eq    "rate limit is attempted exactly once" "1" "$(wc -l < "$CALLS" | tr -d ' ')"

# --- 5. unknown failure is NOT retried -------------------------------------------------------
: > "$CALLS"
opencode() { printf '%s\n' "$*" >> "$CALLS"; printf 'some unrelated bug\n' >&2; return 1; }
provider_opencode_run_cmd "$SCRATCH" "opencode/x" "" "p" >/dev/null 2>&1
eq    "an unrecognised failure is attempted exactly once" "1" "$(wc -l < "$CALLS" | tr -d ' ')"

# --- 6. bounded: persistent transient failure stops at RETRIES+1 attempts ----------------------
: > "$CALLS"
opencode() { printf '%s\n' "$*" >> "$CALLS"; printf 'stream error\n' >&2; return 1; }
provider_opencode_run_cmd "$SCRATCH" "opencode/x" "" "p" >/dev/null 2>&1
eq    "persistent transient failure stops after RETRIES+1 attempts" "3" "$(wc -l < "$CALLS" | tr -d ' ')"

# --- 7. RETRIES=0 disables --------------------------------------------------------------------
: > "$CALLS"
AGENT_OPENCODE_RETRIES=0 provider_opencode_run_cmd "$SCRATCH" "opencode/x" "" "p" >/dev/null 2>&1
eq    "AGENT_OPENCODE_RETRIES=0 disables retrying" "1" "$(wc -l < "$CALLS" | tr -d ' ')"

echo; echo "$PASS/$((PASS + FAIL)) passed"
[ "$FAIL" -eq 0 ]
