#!/usr/bin/env bash
# tests/test_agent_sh.sh — zero-dependency regression tests for agent.sh's pure-logic pieces.
#
# Philosophy: no bats-core, no external test framework — plain bash with small assert helpers.
# Sources agent.sh itself (via `source ./agent.sh list` — "list" is a harmless read-only
# subcommand) inside a scratch AGENT_CLI_LOGS dir so every meta_set/meta_get/provider_*_resolve
# function becomes available in-process, without ever invoking a real CLI (codex/claude/...).
#
# Run:  bash tests/test_agent_sh.sh
# Exit: 0 if all tests passed, 1 otherwise. Prints a final "N/N passed" summary.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE" || exit 1

# --- scratch LOGDIR: never touch the real ~/.claude/agent-cli-logs -----------------------
SCRATCH_LOGDIR="$(mktemp -d)"
cleanup() { rm -rf "$SCRATCH_LOGDIR"; }
trap cleanup EXIT

export AGENT_CLI_LOGS="$SCRATCH_LOGDIR"

# Source agent.sh's functions (meta_set/meta_get/provider_*_resolve/etc.) without running any
# real command. "list" is a read-only, harmless subcommand (just lists *.meta in LOGDIR, which
# is empty in our scratch dir) — sourcing (not executing) it means reaching the end of the
# script just returns control to us instead of exiting this test process.
# shellcheck disable=SC1091
source "$HERE/agent.sh" list >/dev/null 2>&1

# --- tiny assert framework ----------------------------------------------------------------
PASS=0
FAIL=0

pass() { PASS=$((PASS + 1)); echo "  ok   - $1"; }
fail() { FAIL=$((FAIL + 1)); echo "  FAIL - $1"; }

assert_eq() {
    local desc="$1" expected="$2" actual="$3"
    if [ "$expected" = "$actual" ]; then
        pass "$desc"
    else
        fail "$desc (expected [$expected], got [$actual])"
    fi
}

assert_match() {
    local desc="$1" pattern="$2" actual="$3"
    if [[ "$actual" =~ $pattern ]]; then
        pass "$desc"
    else
        fail "$desc (expected to match [$pattern], got [$actual])"
    fi
}

section() { echo; echo "=== $1 ==="; }

# ============================================================================================
section "meta_set / meta_get round-trip"
# ============================================================================================

meta_set roundtrip_task foo "hello world"
assert_eq "meta_get returns the value that was set" "hello world" "$(meta_get roundtrip_task foo)"

meta_set roundtrip_task foo "second value"
assert_eq "meta_set overwrites an existing key (not appends)" "second value" "$(meta_get roundtrip_task foo)"
assert_eq "meta_set overwrite leaves exactly one line for that key" \
    "1" "$(grep -c '^foo=' "$SCRATCH_LOGDIR/roundtrip_task.meta")"

meta_set roundtrip_task bar "another"
assert_eq "a second distinct key also round-trips" "another" "$(meta_get roundtrip_task bar)"
assert_eq "first key untouched by setting a second key" "second value" "$(meta_get roundtrip_task foo)"

meta_set roundtrip_task withequals "a=b=c"
assert_eq "values containing '=' round-trip (cut -d= -f2- keeps everything after the first =)" \
    "a=b=c" "$(meta_get roundtrip_task withequals)"

# --- _meta_lock / _meta_unlock -------------------------------------------------------------
lock_file="$SCRATCH_LOGDIR/locktest"
_meta_lock "$lock_file"
if [ -d "${lock_file}.lock.d" ]; then
    pass "_meta_lock creates a .lock.d directory (mkdir-based mutex)"
else
    fail "_meta_lock did not create the expected lock directory"
fi
_meta_unlock "$lock_file"
if [ ! -d "${lock_file}.lock.d" ]; then
    pass "_meta_unlock removes the lock directory"
else
    fail "_meta_unlock left the lock directory behind"
fi

# ============================================================================================
section "meta_set concurrency (regression test for the mkdir-mutex fix)"
# ============================================================================================
# Spawn ~10 background subshells all calling meta_set on the SAME task key at once. Before the
# mkdir-based mutex existed, meta_set's read-modify-write (grep old file -> append -> mv) was not
# atomic across processes, so near-simultaneous writers to the same .meta file could clobber each
# other and lose keys. This asserts all 10 keys survive.

CONC_TASK="concurrent_task"
N=10
pids=()
for i in $(seq 1 "$N"); do
    ( meta_set "$CONC_TASK" "key$i" "val$i" ) &
    pids+=("$!")
done
for p in "${pids[@]}"; do
    wait "$p"
done

survived=0
for i in $(seq 1 "$N"); do
    v="$(meta_get "$CONC_TASK" "key$i")"
    if [ "$v" = "val$i" ]; then
        survived=$((survived + 1))
    fi
done
assert_eq "all $N keys survive ~$N concurrent meta_set calls on the same task" "$N" "$survived"

total_lines="$(grep -c . "$SCRATCH_LOGDIR/$CONC_TASK.meta" 2>/dev/null || echo 0)"
assert_eq "meta file has exactly $N lines (no duplicate/partial writes)" "$N" "$total_lines"

# no stray lock directory left behind after all writers finished
if [ ! -d "$SCRATCH_LOGDIR/$CONC_TASK.meta.lock.d" ]; then
    pass "no stray lock directory left after concurrent writers finish"
else
    fail "stray lock directory left behind: $SCRATCH_LOGDIR/$CONC_TASK.meta.lock.d"
fi

# ============================================================================================
section "provider_codex_resolve"
# ============================================================================================

for alias_in in "" "5.5" "default"; do
    P_MODEL=""; P_EFFORT=""
    provider_codex_resolve "$alias_in"
    assert_eq "codex alias '$alias_in' -> model gpt-5.5" "gpt-5.5" "$P_MODEL"
    assert_eq "codex alias '$alias_in' -> effort medium" "medium" "$P_EFFORT"
done

for alias_in in "5.5-high" "high"; do
    P_MODEL=""; P_EFFORT=""
    provider_codex_resolve "$alias_in"
    assert_eq "codex alias '$alias_in' -> model gpt-5.5" "gpt-5.5" "$P_MODEL"
    assert_eq "codex alias '$alias_in' -> effort high" "high" "$P_EFFORT"
done

for alias_in in "spark" "5.3"; do
    P_MODEL=""; P_EFFORT=""
    provider_codex_resolve "$alias_in"
    assert_eq "codex alias '$alias_in' -> model gpt-5.3-codex-spark" "gpt-5.3-codex-spark" "$P_MODEL"
done

# passthrough for an unrecognized alias
P_MODEL=""; P_EFFORT=""
provider_codex_resolve "some-other-model"
assert_eq "codex unknown alias passes through verbatim as model" "some-other-model" "$P_MODEL"

# ============================================================================================
section "provider_claude_resolve"
# ============================================================================================

P_MODEL=""; P_EFFORT=""
provider_claude_resolve ""
assert_eq "claude default (no alias) -> model claude-sonnet-5" "claude-sonnet-5" "$P_MODEL"
assert_eq "claude default (no alias) -> effort high" "high" "$P_EFFORT"

P_MODEL=""; P_EFFORT=""
provider_claude_resolve "default"
assert_eq "claude alias 'default' -> model claude-sonnet-5" "claude-sonnet-5" "$P_MODEL"
assert_eq "claude alias 'default' -> effort high" "high" "$P_EFFORT"

P_MODEL=""; P_EFFORT=""
provider_claude_resolve "sonnet"
assert_eq "claude alias 'sonnet' -> model claude-sonnet-5" "claude-sonnet-5" "$P_MODEL"
assert_eq "claude alias 'sonnet' -> effort high" "high" "$P_EFFORT"

# suffix parsing on an arbitrary base (not just "sonnet")
for suffix_eff in "low" "medium" "high" "xhigh" "max"; do
    P_MODEL=""; P_EFFORT=""
    provider_claude_resolve "some-base-model-$suffix_eff"
    assert_eq "claude suffix -$suffix_eff strips to base 'some-base-model'" \
        "some-base-model" "$P_MODEL"
    assert_eq "claude suffix -$suffix_eff resolves effort=$suffix_eff" "$suffix_eff" "$P_EFFORT"
done

# opus/haiku: resolve with no forced effort (empty P_EFFORT, CLI default applies)
P_MODEL=""; P_EFFORT=""
provider_claude_resolve "opus"
assert_eq "claude alias 'opus' -> model opus" "opus" "$P_MODEL"
assert_eq "claude alias 'opus' -> no forced effort" "" "$P_EFFORT"

P_MODEL=""; P_EFFORT=""
provider_claude_resolve "haiku"
assert_eq "claude alias 'haiku' -> model haiku" "haiku" "$P_MODEL"
assert_eq "claude alias 'haiku' -> no forced effort" "" "$P_EFFORT"

# opus/haiku WITH an effort suffix still parses the suffix
P_MODEL=""; P_EFFORT=""
provider_claude_resolve "opus-low"
assert_eq "claude alias 'opus-low' -> model opus" "opus" "$P_MODEL"
assert_eq "claude alias 'opus-low' -> effort low" "low" "$P_EFFORT"

# ============================================================================================
section "collision-resistant default task name"
# ============================================================================================
# agent.sh sets: name="task-$(date +%Y%m%d-%H%M%S)-$$" at top level. We already sourced the
# script (as "list"), so $name reflects that exact expression evaluated in THIS process.

assert_match "default name matches task-<timestamp>-<pid> shape" \
    '^task-[0-9]{8}-[0-9]{6}-[0-9]+$' "$name"

# two invocations in the same process differ only if PIDs differ (they will, since each
# `source ./agent.sh ...` subprocess below is distinct) — verify distinctness by sourcing again
# in a subshell with a different PID and comparing names.
name_a="$name"
name_b="$(bash -c "export AGENT_CLI_LOGS='$SCRATCH_LOGDIR'; source '$HERE/agent.sh' list >/dev/null 2>&1; echo \"\$name\"")"
if [ "$name_a" != "$name_b" ]; then
    pass "two separate process invocations produce distinct default task names (PID differs)"
else
    fail "two separate invocations produced the SAME default task name: $name_a"
fi

# ============================================================================================
section "_provider_codex_emit (codex --json cleanup)"
# ============================================================================================
# The codex provider runs `codex exec --json` and pipes the JSONL through _provider_codex_emit,
# which must (a) emit a `session id: <uuid>` line for agent.sh's resume grep, (b) emit a synthetic
# `---------- output ----------` marker so last_output slices to a CLEAN answer, and (c) drop all
# of codex's banner/ERROR-log/"tokens used"/cp866-mojibake noise. These feed representative JSONL
# (plus noise lines) on stdin and assert the cleaned output.

emit_normal="$(printf '%s\n' \
    'Reading additional input from stdin...' \
    '{"type":"thread.started","thread_id":"019f1ecd-dc5c-7e11-a080-94c162d3b5b9"}' \
    '{"type":"turn.started"}' \
    '2026-07-01T17:50:42Z ERROR codex_memories_write: failed to claim job' \
    '{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"pong"}}' \
    '{"type":"turn.completed","usage":{"input_tokens":32146}}' | _provider_codex_emit)"

assert_match "emit surfaces the session id line for resume" \
    'session id: 019f1ecd-dc5c-7e11-a080-94c162d3b5b9' "$emit_normal"
assert_match "emit writes a synthetic output marker before the answer" \
    '---------- output ----------' "$emit_normal"
# last_output (agent.sh's own slicer) applied to the emitted block must yield exactly the answer.
emit_clean="$(printf '%s' "$emit_normal" | awk '/^---------- output ----------$/{buf=""; next}{buf=buf $0 ORS} END{printf "%s", buf}' | sed 's/[[:space:]]*$//')"
assert_eq "last_output over emit yields ONLY the clean answer (no chrome)" "pong" "$emit_clean"

# When several agent_message items arrive, the LAST one is codex's final consolidated answer.
emit_multi="$(printf '%s\n' \
    '{"type":"thread.started","thread_id":"aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"}' \
    '{"type":"item.completed","item":{"type":"agent_message","text":"first"}}' \
    '{"type":"item.completed","item":{"type":"agent_message","text":"final"}}' | _provider_codex_emit \
    | awk '/^---------- output ----------$/{buf=""; next}{buf=buf $0 ORS} END{printf "%s", buf}' | sed 's/[[:space:]]*$//')"
assert_eq "multiple agent_message items -> the last (final) one wins" "final" "$emit_multi"

# No agent_message at all (e.g. an auth/rate-limit failure) -> pass the raw stream through so the
# error stays visible, and do NOT fabricate an empty answer or a bogus marker.
emit_err="$(printf '%s\n' \
    'stream error: You have hit your usage limit.' \
    '{"type":"thread.started","thread_id":"aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"}' \
    '{"type":"turn.failed","error":{"message":"usage limit"}}' | _provider_codex_emit)"
assert_match "no agent_message -> raw error stream is surfaced" 'usage limit' "$emit_err"
if printf '%s' "$emit_err" | grep -q '^---------- output ----------$'; then
    fail "error passthrough should NOT emit a synthetic output marker"
else
    pass "error passthrough does not emit a synthetic output marker"
fi

# A cp866 OS-notification line (non-UTF-8 bytes) must not crash the parser (errors=ignore).
emit_mojibake="$(printf '{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}\n\x93\xe0\xaf\xa5\n' | _provider_codex_emit \
    | awk '/^---------- output ----------$/{buf=""; next}{buf=buf $0 ORS} END{printf "%s", buf}' | sed 's/[[:space:]]*$//')"
assert_eq "non-UTF-8 mojibake line does not crash the parser" "ok" "$emit_mojibake"

# ============================================================================================
section "summary"
# ============================================================================================
TOTAL=$((PASS + FAIL))
echo
echo "$PASS/$TOTAL passed"
if [ "$FAIL" -gt 0 ]; then
    echo "$FAIL test(s) FAILED"
    exit 1
fi
exit 0
