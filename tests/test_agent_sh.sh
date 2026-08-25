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

for alias_in in "" "default"; do
    P_MODEL=""; P_EFFORT=""
    provider_codex_resolve "$alias_in"
    assert_eq "codex alias '$alias_in' -> model gpt-5.6-terra" "gpt-5.6-terra" "$P_MODEL"
    assert_eq "codex alias '$alias_in' -> effort medium" "medium" "$P_EFFORT"
done

P_MODEL=""; P_EFFORT=""
provider_codex_resolve "high"
assert_eq "codex alias 'high' -> model gpt-5.6-sol" "gpt-5.6-sol" "$P_MODEL"
assert_eq "codex alias 'high' -> effort high" "high" "$P_EFFORT"

for alias_in in "luna" "5.6-luna"; do
    P_MODEL=""; P_EFFORT=""
    provider_codex_resolve "$alias_in"
    assert_eq "codex alias '$alias_in' -> model gpt-5.6-luna" "gpt-5.6-luna" "$P_MODEL"
done

for alias_in in "terra" "5.6-terra"; do
    P_MODEL=""; P_EFFORT=""
    provider_codex_resolve "$alias_in"
    assert_eq "codex alias '$alias_in' -> model gpt-5.6-terra" "gpt-5.6-terra" "$P_MODEL"
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
section "provider_kimi_resolve"
# ============================================================================================

for alias_in in "" "default" "k3" "kimi-k3"; do
    P_MODEL=""; P_EFFORT=""
    provider_kimi_resolve "$alias_in"
    assert_eq "kimi alias '$alias_in' -> K3" "kimi-code/k3" "$P_MODEL"
    assert_eq "kimi alias '$alias_in' -> no effort flag" "" "$P_EFFORT"
done

assert_eq "kimi k3-256k alias" "kimi-code/k3-256k" "$(provider_kimi_resolve k3-256k; printf '%s' "$P_MODEL")"
assert_eq "kimi coding alias" "kimi-code/kimi-for-coding" "$(provider_kimi_resolve coding; printf '%s' "$P_MODEL")"
assert_eq "kimi highspeed alias" "kimi-code/kimi-for-coding-highspeed" "$(provider_kimi_resolve highspeed; printf '%s' "$P_MODEL")"

provider_kimi_resolve "custom-provider/custom-model"
assert_eq "kimi unknown alias passes through verbatim" \
    "custom-provider/custom-model" "$P_MODEL"

# ============================================================================================
section "provider_claude_resolve"
# ============================================================================================

P_MODEL=""; P_EFFORT=""
provider_claude_resolve ""
assert_eq "claude default (no alias) -> model claude-opus-5" "claude-opus-5" "$P_MODEL"
assert_eq "claude default (no alias) -> CLI effort default" "" "$P_EFFORT"

P_MODEL=""; P_EFFORT=""
provider_claude_resolve "default"
assert_eq "claude alias 'default' -> model claude-opus-5" "claude-opus-5" "$P_MODEL"
assert_eq "claude alias 'default' -> CLI effort default" "" "$P_EFFORT"

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
# error stays visible, do NOT fabricate an empty answer or a bogus marker, AND exit non-zero so the
# provider's PIPESTATUS[1] guard marks the task failed rather than "done with junk".
emit_err="$(printf '%s\n' \
    'stream error: You have hit your usage limit.' \
    '{"type":"thread.started","thread_id":"aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"}' \
    '{"type":"turn.failed","error":{"message":"usage limit"}}' | _provider_codex_emit)"
emit_err_rc=$?
assert_match "no agent_message -> raw error stream is surfaced" 'usage limit' "$emit_err"
assert_eq "no agent_message -> parser exits non-zero" "3" "$emit_err_rc"
if printf '%s' "$emit_err" | grep -q '^---------- output ----------$'; then
    fail "error passthrough should NOT emit a synthetic output marker"
else
    pass "error passthrough does not emit a synthetic output marker"
fi

# Pathological: the answer itself contains a line exactly equal to the marker. It must NOT truncate
# -- the emitter neutralizes such lines (trailing space) so last_output returns the WHOLE answer.
# Quoted heredoc keeps the JSON `\n` literal (a valid JSON escape) instead of a real line break.
emit_collide="$(_provider_codex_emit <<'COLLIDE_JSON' | awk '/^---------- output ----------$/{buf=""; next}{buf=buf $0 ORS} END{printf "%s", buf}'
{"type":"item.completed","item":{"type":"agent_message","text":"line one\n---------- output ----------\nline three"}}
COLLIDE_JSON
)"
assert_match "answer containing the marker line keeps its first line" 'line one' "$emit_collide"
assert_match "answer containing the marker line keeps its last line" 'line three' "$emit_collide"

# A cp866 OS-notification line (non-UTF-8 bytes) must not crash the parser (errors=ignore).
emit_mojibake="$(printf '{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}\n\x93\xe0\xaf\xa5\n' | _provider_codex_emit \
    | awk '/^---------- output ----------$/{buf=""; next}{buf=buf $0 ORS} END{printf "%s", buf}' | sed 's/[[:space:]]*$//')"
assert_eq "non-UTF-8 mojibake line does not crash the parser" "ok" "$emit_mojibake"

# ============================================================================================
section "_provider_kimi_emit (Kimi stream-json cleanup)"
# ============================================================================================

kimi_emit="$(_provider_kimi_emit <<'KIMI_JSON'
{"role":"assistant","content":"checking","tool_calls":[{"type":"function","id":"tc_1","function":{"name":"Shell","arguments":"{}"}}]}
{"role":"tool","tool_call_id":"tc_1","content":"ok"}
{"role":"assistant","content":"final answer"}
{"role":"meta","type":"session.resume_hint","session_id":"ses_abc123","command":"kimi -r ses_abc123","content":"To resume this session"}
KIMI_JSON
)"
assert_match "kimi emitter exposes session id" '^session id: ses_abc123' "$kimi_emit"
kimi_answer="$(printf '%s\n' "$kimi_emit" | awk '/^---------- output ----------$/{buf=""; next}{buf=buf $0 ORS} END{printf "%s", buf}' | sed 's/[[:space:]]*$//')"
assert_eq "kimi emitter keeps only final assistant answer" "final answer" "$kimi_answer"

kimi_error="$(printf '%s\n' 'Authentication required. Run kimi login.' | _provider_kimi_emit)"
kimi_error_rc=$?
assert_match "kimi emitter surfaces auth errors" 'Authentication required' "$kimi_error"
assert_eq "kimi emitter returns non-zero without an answer" "3" "$kimi_error_rc"

printf 'engine=kimi\nsession=ses_meta123\n' > "$SCRATCH_LOGDIR/kimi_session.meta"
assert_eq "resolve_session accepts Kimi ses_ ids from meta" "ses_meta123" "$(resolve_session kimi_session)"

unset AGENT_CHAT_ONLY
mapfile -t kimi_normal < <(_provider_kimi_run_args)
assert_eq "kimi normal run has no chat-only profile" "0" "${#kimi_normal[@]}"
AGENT_CHAT_ONLY=1
mapfile -t kimi_chatonly < <(_provider_kimi_run_args)
assert_eq "kimi bridge uses explicit no-tools agent profile" "--agent-file $HERE/providers/kimi/chat-only-agent.md" "${kimi_chatonly[*]}"
unset AGENT_CHAT_ONLY

# Full provider command wiring with a fake CLI: lock in Kimi's real constraint that --prompt
# cannot be combined with --auto, plus clean output and resumable session extraction.
KIMI_ARGS_FILE="$SCRATCH_LOGDIR/kimi-args"
kimi() {
    printf '%s\n' "$*" > "$KIMI_ARGS_FILE"
    printf '%s\n' \
        '{"role":"assistant","content":"KIMI_WRAPPER_OK"}' \
        '{"role":"meta","type":"session.resume_hint","session_id":"ses_fake","command":"kimi -r ses_fake","content":"resume"}'
}
kimi_run_output="$(provider_kimi_run_cmd "$SCRATCH_LOGDIR" "kimi-code/k3" "" "hello")"
assert_eq "kimi provider fake run exits cleanly" "0" "$?"
assert_match "kimi provider passes K3 model and prompt mode" '-m kimi-code/k3 -p hello --output-format stream-json' "$(cat "$KIMI_ARGS_FILE")"
if grep -q -- '--auto' "$KIMI_ARGS_FILE"; then
    fail "kimi prompt-mode command must not include incompatible --auto"
else
    pass "kimi prompt-mode command omits incompatible --auto"
fi
assert_match "kimi provider fake run emits clean answer" 'KIMI_WRAPPER_OK' "$kimi_run_output"

kimi_resume_output="$(provider_kimi_resume_cmd "$SCRATCH_LOGDIR" "ses_fake" "continue")"
assert_match "kimi resume passes the concrete ses_ id" '--session ses_fake -p continue' "$(cat "$KIMI_ARGS_FILE")"
assert_match "kimi provider fake resume emits clean answer" 'KIMI_WRAPPER_OK' "$kimi_resume_output"
unset -f kimi

# ============================================================================================
section "_provider_opencode_emit / provider wiring"
# ============================================================================================
# opencode can spend minutes in tool calls. The JSONL parser must expose session/activity before
# EOF, while keeping the final answer behind a fresh output marker for last_output/openai_server.
OPENCODE_STREAM_FILE="$SCRATCH_LOGDIR/opencode-stream"
(
    {
        printf '%s\n' '{"type":"step_start","sessionID":"ses_stream"}'
        sleep 2
        printf '%s\n' '{"type":"text","sessionID":"ses_stream","part":{"id":"p1","text":"final answer"}}'
    } | _provider_opencode_emit > "$OPENCODE_STREAM_FILE"
) &
opencode_stream_pid=$!
for _ in $(seq 1 15); do
    [ -s "$OPENCODE_STREAM_FILE" ] && break
    sleep 0.1
done
if grep -q '^session id: ses_stream$' "$OPENCODE_STREAM_FILE" 2>/dev/null; then
    pass "opencode emitter flushes session id before EOF"
else
    fail "opencode emitter buffered session id until EOF"
fi
if grep -q '^\[opencode\] activity: step_start$' "$OPENCODE_STREAM_FILE" 2>/dev/null; then
    pass "opencode emitter flushes an activity heartbeat before EOF"
else
    fail "opencode emitter did not expose activity before EOF"
fi
wait "$opencode_stream_pid"
opencode_clean="$(awk '/^---------- output ----------$/{buf=""; next}{buf=buf $0 ORS} END{printf "%s", buf}' \
    "$OPENCODE_STREAM_FILE" | sed 's/[[:space:]]*$//')"
assert_eq "opencode last output remains clean after heartbeats" "final answer" "$opencode_clean"

# Fake CLI locks in exact model/variant/prompt argv and verifies stderr remains diagnosable.
OPENCODE_ARGS_FILE="$SCRATCH_LOGDIR/opencode-args"
opencode() {
    printf '%s\n' "$*" > "$OPENCODE_ARGS_FILE"
    printf 'provider diagnostic\n' >&2
    printf '%s\n' '{"type":"text","sessionID":"ses_fake","part":{"id":"p1","text":"OPENCODE_WRAPPER_OK"}}'
}
AGENT_OPENCODE_TIMEOUT_SEC=0
export AGENT_OPENCODE_TIMEOUT_SEC
opencode_stderr="$SCRATCH_LOGDIR/opencode-stderr"
opencode_run_output="$(provider_opencode_run_cmd "$SCRATCH_LOGDIR" \
    "opencode/deepseek-v4-flash-free" "high" "inspect this" 2>"$opencode_stderr")"
assert_eq "opencode provider fake run exits cleanly" "0" "$?"
assert_match "opencode provider preserves exact DeepSeek model, variant and prompt" \
    '--auto --format json -m opencode/deepseek-v4-flash-free --variant high inspect this' \
    "$(cat "$OPENCODE_ARGS_FILE")"
assert_match "opencode provider emits clean final answer" 'OPENCODE_WRAPPER_OK' "$opencode_run_output"
assert_match "opencode provider keeps prefixed stderr diagnostics" \
    '^\[opencode\] provider diagnostic' "$(cat "$opencode_stderr")"
unset -f opencode
unset AGENT_OPENCODE_TIMEOUT_SEC

# A real executable test double exercises GNU timeout (shell functions cannot be exec'd by timeout).
OPENCODE_FAKE_BIN="$SCRATCH_LOGDIR/opencode-bin"
mkdir -p "$OPENCODE_FAKE_BIN"
printf '%s\n' '#!/usr/bin/env bash' 'sleep 30' > "$OPENCODE_FAKE_BIN/opencode"
chmod +x "$OPENCODE_FAKE_BIN/opencode"
old_path="$PATH"
PATH="$OPENCODE_FAKE_BIN:$PATH"
export PATH
AGENT_OPENCODE_TIMEOUT_SEC=1
export AGENT_OPENCODE_TIMEOUT_SEC
timeout_output="$(provider_opencode_run_cmd "$SCRATCH_LOGDIR" \
    "opencode/deepseek-v4-flash-free" "" "timeout probe" 2>&1)"
timeout_rc=$?
assert_eq "opencode provider returns GNU timeout exit code" "124" "$timeout_rc"
assert_match "opencode provider reports the configured timeout" \
    '\[opencode\] timed out after 1s' "$timeout_output"
PATH="$old_path"
export PATH
unset AGENT_OPENCODE_TIMEOUT_SEC

# ============================================================================================
section "AGENT_CHAT_ONLY sandboxing (codex + claude provider flags)"
# ============================================================================================
# openai_server.py sets AGENT_CHAT_ONLY=1 for every subprocess it launches; the provider scripts
# must react by locking the CLI down to text-only completion (no shell/file/MCP access), and must
# NOT do so for a normal `agent.sh run` (AGENT_CHAT_ONLY unset), which legitimately needs full
# access to do real coding work. Verified LIVE (not just here) that --sandbox read-only blocks a
# real file write without hanging, and --ignore-user-config empties codex's configured MCP servers
# (a real unityMCP call otherwise succeeded even with `-c mcp_servers={}`); and that claude's
# --strict-mcp-config + --disallowedTools blocks a real file write/MCP use without hanging or
# breaking the tool-calling prompt. These assertions just lock in the FLAG WIRING so a refactor
# can't silently drop it; the "does it actually restrict" claim is a live/manual check, not here.

# Normal runs are FULL AUTO on every engine (gemini --yolo, opencode --auto, kimi's auto policy,
# claude --dangerously-skip-permissions, codex --sandbox danger-full-access). stdin is closed for
# every task, so a run that stops to ask for permission can never be answered and hangs until the
# watchdog kills it. codex was the odd one out at workspace-write, and on the owner's Windows box
# its sandbox helper is broken outright: every write came back as "writing is blocked by read-only
# sandbox", so the agent reported fixes instead of applying them. Full access is the owner's
# explicit, standing decision -- these assertions exist so a later refactor cannot quietly put the
# broken sandbox back and turn every write task into a no-op again.
unset AGENT_CHAT_ONLY
unset AGENT_CODEX_USER_CONFIG AGENT_CODEX_MCP AGENT_CODEX_SANDBOX
mapfile -t codex_default < <(_provider_codex_chatonly_args)
assert_eq "codex default (no AGENT_CHAT_ONLY): full-access sandbox + user-config isolation" \
    "--sandbox danger-full-access --ignore-user-config" "${codex_default[*]}"

AGENT_CODEX_SANDBOX=workspace-write
mapfile -t codex_sbox < <(_provider_codex_chatonly_args)
assert_eq "codex: AGENT_CODEX_SANDBOX puts a sandbox back for machines where it works" \
    "--sandbox workspace-write --ignore-user-config" "${codex_sbox[*]}"
unset AGENT_CODEX_SANDBOX

AGENT_CHAT_ONLY=1
mapfile -t codex_chatonly < <(_provider_codex_chatonly_args)
assert_eq "codex chat-only: switches to read-only sandbox + ignore-user-config" \
    "--sandbox read-only --ignore-user-config" "${codex_chatonly[*]}"

# The bridge turns arbitrary HTTP callers into CLI invocations, so it must stay read-only no matter
# what the environment says -- otherwise AGENT_CODEX_SANDBOX in a shell profile would silently hand
# every caller of :8801/v1 full shell access.
AGENT_CODEX_SANDBOX=danger-full-access
mapfile -t codex_chat_sbox < <(_provider_codex_chatonly_args)
assert_eq "codex chat-only ignores AGENT_CODEX_SANDBOX (bridge stays read-only)" \
    "--sandbox read-only --ignore-user-config" "${codex_chat_sbox[*]}"
unset AGENT_CODEX_SANDBOX
unset AGENT_CHAT_ONLY

unset AGENT_CLAUDE_PERMISSION
mapfile -t claude_perm < <(_provider_claude_perm_args)
assert_eq "claude default: full auto, no permission prompts" \
    "--dangerously-skip-permissions" "${claude_perm[*]}"

AGENT_CLAUDE_PERMISSION="--permission-mode acceptEdits"
mapfile -t claude_perm_env < <(_provider_claude_perm_args)
assert_eq "claude: AGENT_CLAUDE_PERMISSION brings the prompts back" \
    "--permission-mode acceptEdits" "${claude_perm_env[*]}"
unset AGENT_CLAUDE_PERMISSION

AGENT_CHAT_ONLY=1
AGENT_CLAUDE_PERMISSION="--dangerously-skip-permissions"
mapfile -t claude_perm_chat < <(_provider_claude_perm_args)
assert_eq "claude chat-only ignores AGENT_CLAUDE_PERMISSION (bridge keeps acceptEdits)" \
    "--permission-mode acceptEdits" "${claude_perm_chat[*]}"
unset AGENT_CLAUDE_PERMISSION
unset AGENT_CHAT_ONLY

unset AGENT_CHAT_ONLY
claude_default="$(_provider_claude_chatonly_args)"
assert_eq "claude default (no AGENT_CHAT_ONLY): no extra flags" "" "$claude_default"

AGENT_CHAT_ONLY=1
mapfile -t claude_chatonly < <(_provider_claude_chatonly_args)
assert_eq "claude chat-only: adds strict-mcp-config" "--strict-mcp-config" "${claude_chatonly[0]}"
assert_eq "claude chat-only: adds disallowedTools flag" "--disallowedTools" "${claude_chatonly[1]}"
assert_match "claude chat-only: denylist blocks Bash/Edit/Write/Task/Web*" \
    'Bash,Edit,Write,NotebookEdit,Task,WebFetch,WebSearch' "${claude_chatonly[2]}"
unset AGENT_CHAT_ONLY

# ============================================================================================
section "codex user-config isolation (AGENT_CODEX_USER_CONFIG / AGENT_CODEX_MCP)"
# ============================================================================================
# Every normal codex run is launched with --ignore-user-config, because ~/.codex/config.toml on a
# machine with the ChatGPT desktop app declares a stdio "code-mode host" that is only reachable
# from that app -- without it, codex answers but the FIRST shell command it issues blocks forever
# (verified live; full repro in providers/codex/provider.sh). These tests lock the wiring in so it
# cannot be silently dropped again, and cover the two escape hatches.

unset AGENT_CHAT_ONLY AGENT_CODEX_USER_CONFIG AGENT_CODEX_MCP
mapfile -t iso_default < <(_provider_codex_isolation_args)
assert_eq "isolation on by default: --ignore-user-config" "--ignore-user-config" "${iso_default[*]}"

AGENT_CODEX_USER_CONFIG=1
mapfile -t iso_optout < <(_provider_codex_isolation_args)
assert_eq "AGENT_CODEX_USER_CONFIG=1 opts back into ~/.codex/config.toml" "" "${iso_optout[*]}"
unset AGENT_CODEX_USER_CONFIG

AGENT_CODEX_MCP="unityMCP=http://127.0.0.1:8040/mcp"
mapfile -t iso_mcp < <(_provider_codex_isolation_args)
assert_eq "AGENT_CODEX_MCP re-adds one server as a -c override" \
    '--ignore-user-config -c mcp_servers.unityMCP.url="http://127.0.0.1:8040/mcp"' "${iso_mcp[*]}"

AGENT_CODEX_MCP="a=http://x/1,,b=http://y/2"
mapfile -t iso_mcp2 < <(_provider_codex_isolation_args 2>/dev/null)
assert_eq "AGENT_CODEX_MCP: several servers, empty segments skipped" \
    '--ignore-user-config -c mcp_servers.a.url="http://x/1" -c mcp_servers.b.url="http://y/2"' "${iso_mcp2[*]}"

AGENT_CODEX_MCP="bad name=http://x/1,ok=http://y/2,noequals"
mapfile -t iso_mcp3 < <(_provider_codex_isolation_args 2>/dev/null)
assert_eq "AGENT_CODEX_MCP: illegal name and name-less entry are skipped, valid one kept" \
    '--ignore-user-config -c mcp_servers.ok.url="http://y/2"' "${iso_mcp3[*]}"
mcp_warn="$(AGENT_CODEX_MCP='bad name=http://x/1' _provider_codex_isolation_args 2>&1 >/dev/null)"
assert_match "AGENT_CODEX_MCP: a bad entry is reported on stderr, not silently dropped" \
    'skipping bad server name' "$mcp_warn"
unset AGENT_CODEX_MCP

# chat-only stays the strictest mode: isolated AND no MCP re-injection, even if asked for one.
AGENT_CHAT_ONLY=1 AGENT_CODEX_MCP="unityMCP=http://127.0.0.1:8040/mcp" AGENT_CODEX_USER_CONFIG=1
mapfile -t iso_chat < <(_provider_codex_isolation_args)
assert_eq "chat-only ignores both escape hatches (text-only lockdown wins)" \
    "--ignore-user-config" "${iso_chat[*]}"
unset AGENT_CHAT_ONLY AGENT_CODEX_MCP AGENT_CODEX_USER_CONFIG

# ============================================================================================
section "step watchdog (_guarded_run / AGENT_TIMEOUT_SEC)"
# ============================================================================================
# A provider step must never hang forever without saying so. _guarded_run enforces a wall-clock
# deadline, kills the whole process tree, prints a "!! TIMEOUT" line into the task log and returns
# 124 (which finish_step turns into state=error).

guard_out="$(_guarded_run 5 echo hello)"; guard_rc=$?
assert_eq "_guarded_run passes a fast command's output through" "hello" "$guard_out"
assert_eq "_guarded_run returns the command's own exit code (0)" "0" "$guard_rc"

_guarded_run 5 bash -c 'exit 7' >/dev/null 2>&1; guard_rc7=$?
assert_eq "_guarded_run propagates a non-zero exit code" "7" "$guard_rc7"

_guarded_run 0 bash -c 'exit 3' >/dev/null 2>&1; guard_rc_nolimit=$?
assert_eq "AGENT_TIMEOUT_SEC=0 disables the deadline but still returns the exit code" "3" "$guard_rc_nolimit"

# a grandchild that outlives its parent shell is what actually leaks (on Windows the native
# codex.exe): the tree kill must reach it.
guard_pidf="$SCRATCH_LOGDIR/guard-child.pid"
guard_t0=$(date +%s)
guard_slow="$(_guarded_run 2 bash -c "sleep 60 & echo \$! > '$guard_pidf'; wait" 2>&1)"; guard_rc124=$?
guard_elapsed=$(( $(date +%s) - guard_t0 ))
assert_eq "_guarded_run returns 124 when the deadline expires" "124" "$guard_rc124"
assert_match "_guarded_run writes an explicit TIMEOUT line into the step output" \
    '!! TIMEOUT: step exceeded AGENT_TIMEOUT_SEC=2s' "$guard_slow"
if [ "$guard_elapsed" -lt 30 ]; then
    pass "_guarded_run actually stops at the deadline (${guard_elapsed}s, not 60s)"
else
    fail "_guarded_run did not stop at the deadline (took ${guard_elapsed}s)"
fi
sleep 1
guard_child="$(cat "$guard_pidf" 2>/dev/null)"
if [ -n "$guard_child" ] && kill -0 "$guard_child" 2>/dev/null; then
    fail "_guarded_run left an orphaned grandchild process alive (pid $guard_child)"
    kill -9 "$guard_child" 2>/dev/null
else
    pass "_guarded_run kills the whole process tree, no orphaned grandchild"
fi

# ============================================================================================
section "eff_state / state_label (one liveness truth, shared with gui.py)"
# ============================================================================================
# The CLI used to call a task "running (alive)" while the GUI called the same task "stalled":
# one looked only at the pid, the other only at the log's mtime. Now both implement THIS state
# machine (gui.py mirrors it in python).

for st in done waiting error stalled; do
    meta_set "st_$st" state "$st"
    assert_eq "eff_state passes a finished state through untouched ($st)" "$st" "$(eff_state "st_$st")"
done

# alive pid + fresh log -> running
meta_set st_live state running; meta_set st_live pid "$$"
: > "$SCRATCH_LOGDIR/st_live.log"
assert_eq "running + live pid + fresh log -> running" "running" "$(eff_state st_live)"

# alive pid + long-silent log -> idle (honest third state, NOT stalled)
meta_set st_idle state running; meta_set st_idle pid "$$"
: > "$SCRATCH_LOGDIR/st_idle.log"
touch -d "@$(( $(date +%s) - AGENT_STALE_SEC - 120 ))" "$SCRATCH_LOGDIR/st_idle.log" 2>/dev/null \
    || touch -t "$(date -d '-1 hour' '+%Y%m%d%H%M' 2>/dev/null)" "$SCRATCH_LOGDIR/st_idle.log" 2>/dev/null
assert_eq "running + live pid + silent log -> idle (not stalled)" "idle" "$(eff_state st_idle)"

# dead pid -> stalled, whatever the log says
meta_set st_dead state running; meta_set st_dead pid 999999
: > "$SCRATCH_LOGDIR/st_dead.log"
assert_eq "running + dead pid -> stalled" "stalled" "$(eff_state st_dead)"

# no pid recorded at all (pre-existing .meta) + silent log -> stalled, as the GUI always did
meta_set st_nopid state running
: > "$SCRATCH_LOGDIR/st_nopid.log"
touch -d "@$(( $(date +%s) - AGENT_STALE_SEC - 120 ))" "$SCRATCH_LOGDIR/st_nopid.log" 2>/dev/null \
    || touch -t "$(date -d '-1 hour' '+%Y%m%d%H%M' 2>/dev/null)" "$SCRATCH_LOGDIR/st_nopid.log" 2>/dev/null
assert_eq "running + unknown pid + silent log -> stalled (legacy meta)" "stalled" "$(eff_state st_nopid)"

assert_eq "state_label renders idle exactly like the GUI does" \
    "running (no output for 7m)" "$(state_label idle 450)"
assert_eq "state_label leaves other states alone" "running" "$(state_label running 450)"
assert_eq "state_icon knows the idle state" "▷" "$(state_icon idle)"

# `clean` must treat idle exactly like running. Before `idle` existed, a live-but-quiet task read
# as `running` there and was skipped; the moment eff_state started reporting `idle`, `clean --purge`
# began deleting the .log/.meta of a process that was still writing to them. Run as a REAL
# subprocess, because the regression lives in the `case "$st" in running|idle) continue` arm of the
# clean command, not in a sourceable function.
meta_set st_clean_idle state running
meta_set st_clean_idle pid "$$"
meta_set st_clean_idle dir ""
: > "$SCRATCH_LOGDIR/st_clean_idle.md"
: > "$SCRATCH_LOGDIR/st_clean_idle.log"
touch -d "@$(( $(date +%s) - AGENT_STALE_SEC - 120 ))" "$SCRATCH_LOGDIR/st_clean_idle.log" 2>/dev/null \
    || touch -t "$(date -d '-1 hour' '+%Y%m%d%H%M' 2>/dev/null)" "$SCRATCH_LOGDIR/st_clean_idle.log" 2>/dev/null
clean_out="$(AGENT_CLI_LOGS="$SCRATCH_LOGDIR" bash "$HERE/agent.sh" clean -n --purge 2>&1)"
if printf '%s' "$clean_out" | grep -q 'st_clean_idle'; then
    fail "clean must not touch an idle (alive but quiet) task"
else
    pass "clean skips an idle task exactly like a running one"
fi
rm -f "$SCRATCH_LOGDIR/st_clean_idle.md"

# ============================================================================================
section "doctor structured output contract"
# ============================================================================================

# These are source-level contract checks: running a real doctor here would spawn every installed
# CLI and make the pure-logic suite slow/flaky. The GUI tests exercise the resulting JSON parser.
doctor_source="$(sed -n '/    doctor)/,/    gui)/p' "$HERE/agent.sh")"
assert_match "doctor accepts a machine-readable --json mode" '--json' "$doctor_source"
assert_match "doctor launches provider probes in background jobs" 'doctor_pids.*\$!' "$doctor_source"
assert_match "doctor waits for every background provider probe" 'wait.*doctor_pid' "$doctor_source"
assert_match "doctor emits one structured payload with engines and raw fallback" '"engines": engines.*"raw"' "$doctor_source"

# ============================================================================================
section "_agent_python (shared Windows-aware python discovery)"
# ============================================================================================
# Contract: $AGENT_PYTHON wins, then python3/python/py; a candidate counts only if it actually
# runs (`-c "import sys"`), so a Windows stub `python` that exits non-zero is skipped.

FAKE_BIN="$SCRATCH_LOGDIR/fakebin"
mkdir -p "$FAKE_BIN"
make_py_stub() { # make_py_stub NAME EXIT_CODE — a fake interpreter: runs nothing, exits EXIT_CODE
    printf '%s\n' '#!/usr/bin/env bash' "exit $2" > "$FAKE_BIN/$1"
    chmod +x "$FAKE_BIN/$1"
}

PATH_SAVE="$PATH"
while IFS='|' read -r desc py3_rc python_rc py_rc agent_python expected; do
    [ "$desc" = "DESC" ] && continue
    [ -n "$desc" ] || continue
    make_py_stub python3 "$py3_rc"
    make_py_stub python "$python_rc"
    make_py_stub py "$py_rc"
    if [ -n "$agent_python" ]; then make_py_stub "$agent_python" 0; fi
    _AGENT_PY=""; _AGENT_PY_RESOLVED=""
    got="<none>"
    if PATH="$FAKE_BIN:$PATH_SAVE" AGENT_PYTHON="$agent_python" _agent_python; then got="$_AGENT_PY"; fi
    assert_eq "$desc" "$expected" "$got"
done <<'ROWS'
DESC|py3_rc|python_rc|py_rc|agent_python|expected
windows-stub python exits 1, python3 works -> picks python3|0|1|0||python3
every PATH candidate works -> first (python3) wins|0|0|0||python3
python3 and python are broken stubs -> falls through to py|1|1|0||py
AGENT_PYTHON beats all PATH candidates|0|0|0|custom-py|custom-py
no runnable candidate anywhere -> resolver fails|1|1|1||<none>
ROWS

PATH="$PATH_SAVE"; unset AGENT_PYTHON
_AGENT_PY=""; _AGENT_PY_RESOLVED=""
rm -rf "$FAKE_BIN"

# ============================================================================================
section "file_mtime (portable GNU/BSD stat fallback)"
# ============================================================================================

mtime_probe="$SCRATCH_LOGDIR/mtime-probe.txt"
: > "$mtime_probe"
mt="$(file_mtime "$mtime_probe")"
case "$mt" in
    ''|*[!0-9]*) fail "file_mtime returns a numeric epoch for an existing file (got [$mt])" ;;
    *) pass "file_mtime returns a numeric epoch for an existing file ($mt)" ;;
esac
now_s="$(date +%s)"
if [ "$mt" -le "$now_s" ] && [ "$(( now_s - mt ))" -lt 5 ]; then
    pass "file_mtime is close to now"
else
    fail "file_mtime drifted from now (mtime=$mt now=$now_s)"
fi
assert_eq "file_mtime yields empty output for a missing path" "" "$(file_mtime "$SCRATCH_LOGDIR/nope-missing")"

# GNU/BSD fallback: fake `stat` binaries that speak only one flavor each.
STAT_FAKE="$SCRATCH_LOGDIR/statfake"
mkdir -p "$STAT_FAKE"
make_stat_stub() { # make_stat_stub gnu|bsd|broken — respond only to that stat's flag syntax
    case "$1" in
        gnu)    printf '#!/usr/bin/env bash\n[ "$1" = -c ] && { echo 1700000000; exit 0; }\nexit 1\n' ;;
        bsd)    printf '#!/usr/bin/env bash\n[ "$1" = -f ] && { echo 1700000000; exit 0; }\nexit 1\n' ;;
        broken) printf '#!/usr/bin/env bash\nexit 1\n' ;;
    esac > "$STAT_FAKE/stat"
    chmod +x "$STAT_FAKE/stat"
}
while IFS='|' read -r desc kind expected; do
    [ "$desc" = "DESC" ] && continue
    make_stat_stub "$kind"
    got="$(PATH="$STAT_FAKE:$PATH" file_mtime "$mtime_probe")"
    assert_eq "$desc" "$expected" "$got"
done <<'ROWS'
DESC|kind|expected
GNU-only stat on PATH (-c %Y) -> used directly|gnu|1700000000
BSD/macOS-only stat on PATH (-f %m) -> fallback hits it|bsd|1700000000
stat fails in both flavors -> empty output|broken|
ROWS
rm -rf "$STAT_FAKE"

# ============================================================================================
section "looks_waiting classifier (closing-window question detection)"
# ============================================================================================

LOOKS_WAITING_POSITIVE=(
    "Should I proceed with the refactor?"
    "do you want me to continue?"
    "Which option do you prefer?"
    "which of these files should I edit?"
    "Please confirm before I delete anything."
    "Shall I apply the fix?"
    "уточни формат вывода"
    "подтверди продолжение."
    "как мне поступить дальше?"
    "какой из вариантов тебе нужен?"
    'Continue? (yes/no)'
    'Ready?"'
    "y?"
)
LOOKS_WAITING_NEGATIVE=(
    "?"
    "?!"
    "? ? ?"
    "..."
    ""
    "Task complete."
    "All done!"
    "Fixed the bug and ran the tests."
    "should"
    "Error: file not found."
    "TODO"
    # soft follow-up prose / documentary prose: declarative, NOT a blocking ask (regression:
    # bare "let me know" and "which options ..." used to flip finished tasks to waiting)
    "let me know if you need anything else."
    "I'll keep you posted and let you know the outcome."
    "This section documents which options the CLI supports."
    "Below we describe which of these approaches fits each case."
    "Please see the README for the full option list."
)
for l in "${LOOKS_WAITING_POSITIVE[@]}"; do
    if looks_waiting "$l"; then
        pass "waiting: $l"
    else
        fail "waiting: '$l' classified as not-waiting"
    fi
done
for l in "${LOOKS_WAITING_NEGATIVE[@]}"; do
    if looks_waiting "$l"; then
        fail "not-waiting: '$l' wrongly classified as waiting"
    else
        pass "not-waiting: $l"
    fi
done

# Closing WINDOW, not just the last line (finish_step feeds the last few non-empty lines):
# a question followed by a bookkeeping footer must still be caught...
assert_eq "waiting: question above a footer line is caught in the closing window" "0" \
    "$(looks_waiting "$(printf 'Working on it...\nShould I proceed with the refactor?\nPROGRESS.t.md updated.')"; echo $?)"
assert_eq "waiting: question two footer lines below is still caught" "0" \
    "$(looks_waiting "$(printf 'Which option do you prefer?\nFiles changed.\nAll checks passed.')"; echo $?)"
# ...while a clean window stays clean even when earlier blocks asked questions
assert_eq "not-waiting: plain completion window" "1" \
    "$(looks_waiting "$(printf 'Refactor applied.\nTests green.\nReport written.')"; echo $?)"

# ============================================================================================
section "finish_step end-to-end: closing window decides waiting vs done"
# ============================================================================================
# finish_step slices the LAST output block (last_output), takes its closing window and lets
# looks_waiting decide state=waiting. A question followed by a footer line used to read as done
# because only the very last line was classified.

fs_write_log() { # fs_write_log NAME LINE... — a minimal one-block task log ending with $* lines
    local n="$1"; shift
    { printf '========== [run] 2026-01-01 00:00:00 | engine=test model=default ==========\n'
      printf '> PROMPT:\nwork\n---------- output ----------\n'
      printf '%s\n' "$@"
    } > "$SCRATCH_LOGDIR/$n.log"
}

meta_set fs_wait state running; meta_set fs_wait dir ""
fs_write_log fs_wait "Working on it..." "Should I proceed with the refactor?" "PROGRESS.fs_wait.md updated."
finish_step fs_wait 0 2>/dev/null
assert_eq "question above a footer -> state=waiting" "waiting" "$(meta_get fs_wait state)"

meta_set fs_done state running; meta_set fs_done dir ""
fs_write_log fs_done "Refactor applied." "Tests green." "Report written."
finish_step fs_done 0 2>/dev/null
assert_eq "plain completion -> state=done" "done" "$(meta_get fs_done state)"

meta_set fs_oldq state running; meta_set fs_oldq dir ""
{ printf '========== [run] t ==========\n> PROMPT:\nwork\n---------- output ----------\nShould I stop here?\n'
  printf '========== [reply] t ==========\n> ANSWER:\nyes\n---------- output ----------\nDone.\n'
} > "$SCRATCH_LOGDIR/fs_oldq.log"
finish_step fs_oldq 0 2>/dev/null
assert_eq "a question in an EARLIER output block does not leak into the final verdict" \
    "done" "$(meta_get fs_oldq state)"
rm -f "$SCRATCH_LOGDIR/fs_wait.md" "$SCRATCH_LOGDIR/fs_done.md" "$SCRATCH_LOGDIR/fs_oldq.md"

# ============================================================================================
section "_is_transient_failure (retry classifier)"
# ============================================================================================
# Positive/negative lock-in for the retry decision. The regex once carried LITERAL 0x08 backspace
# bytes where \b word boundaries were intended, so numeric codes never matched; the portable
# (^|[^...]) spelling must match standalone codes and reject digits glued to them.

mk_tf_log() { printf '%s' "$2" > "$SCRATCH_LOGDIR/tf.log"; }
TRANSIENT_POSITIVE=(
    '{"error":{"isRetryable":true,"message":"Provider finish_reason: network_error"}}'
    'stream error: ECONNRESET'
    'ETIMEDOUT while reading upstream'
    'socket hang up'
    'HTTP 429 Too Many Requests'
    'status: 503'
    '(502)'
    'provider is overloaded, try later'
    'temporarily unavailable'
    'rate limit exceeded'
)
TRANSIENT_NEGATIVE=(
    '401 unauthorized'
    'invalid api key sk-xxx'
    'payment required'
    'insufficient quota for this request'
    'model gpt-9 not found'      # auth-class wins over the transient list (checked first)
    'exit code 15002'            # digits glued around 500: boundary must reject
    'reference x502y'            # word chars around 502: boundary must reject
    'all 200 OK'
    'supermodel unavailable'     # boundary before `model`: must NOT hit the auth class either
    ''
)
for s in "${TRANSIENT_POSITIVE[@]}"; do
    mk_tf_log tf "$s"
    if _is_transient_failure "$SCRATCH_LOGDIR/tf.log"; then
        pass "transient: $s"
    else
        fail "transient: '$s' classified as permanent"
    fi
done
for s in "${TRANSIENT_NEGATIVE[@]}"; do
    mk_tf_log tf "$s"
    if _is_transient_failure "$SCRATCH_LOGDIR/tf.log"; then
        fail "permanent: '$s' wrongly classified as transient"
    else
        pass "permanent: $s"
    fi
done

# no literal backspace bytes anywhere in agent.sh (the original \b regression)
if LC_ALL=C grep -q "$(printf '\010')" "$HERE/agent.sh"; then
    fail "agent.sh contains literal 0x08 backspace bytes"
else
    pass "agent.sh contains no literal 0x08 bytes (portable boundaries instead)"
fi

# AGENT_RETRY_DELAY input validation mirrors AGENT_RETRIES/AGENT_TIMEOUT_SEC
assert_eq "non-numeric AGENT_RETRY_DELAY falls back to the default" "8" \
    "$(AGENT_RETRY_DELAY='soon' bash -c "export AGENT_CLI_LOGS='$SCRATCH_LOGDIR'; source '$HERE/agent.sh' list >/dev/null 2>&1; echo \"\$AGENT_RETRY_DELAY\"")"

# ============================================================================================
section "run/fan validate the engine BEFORE any task artifact exists"
# ============================================================================================
# An unknown engine used to die only inside provider_dispatch_run — after .log/.meta with
# state=running + pid= were already written (ghost task reading `stalled` forever); under fan the
# dying subshell's message was discarded entirely while "launched N parallel task(s)" still printed.

run_out="$(AGENT_CLI_LOGS="$SCRATCH_LOGDIR" bash "$HERE/agent.sh" run -e nosuchengine -t ghost_run "hi" 2>&1)"
run_rc=$?
assert_eq "run with an unknown engine exits non-zero" "1" "$run_rc"
assert_match "run names the offending engine" 'unknown engine: nosuchengine' "$run_out"
if [ -e "$SCRATCH_LOGDIR/ghost_run.meta" ] || [ -e "$SCRATCH_LOGDIR/ghost_run.log" ]; then
    fail "run with an unknown engine created a ghost task record"
else
    pass "run with an unknown engine creates NO .meta/.log"
fi

fan_out="$(AGENT_CLI_LOGS="$SCRATCH_LOGDIR" bash "$HERE/agent.sh" fan -e nosuchengine -t ghost_fan "p1" "p2" 2>&1)"
fan_rc=$?
assert_eq "fan with an unknown engine exits non-zero synchronously" "1" "$fan_rc"
assert_match "fan reports the unknown engine visibly" 'unknown engine: nosuchengine' "$fan_out"
if [ -n "$(ls "$SCRATCH_LOGDIR"/ghost_fan-* 2>/dev/null)" ]; then
    fail "fan with an unknown engine created ghost task records"
else
    pass "fan with an unknown engine launches nothing and leaves no records"
fi

# ============================================================================================
section "reply fails safely on a nonexistent task"
# ============================================================================================
# A typo'd name used to fall through every lookup empty-handed: engine stayed at default claude,
# the session guard was bypassed, a ghost .meta was written and `claude --continue` resumed
# whichever conversation happened to be LATEST. It must die before touching any file.

reply_out="$(AGENT_CLI_LOGS="$SCRATCH_LOGDIR" bash "$HERE/agent.sh" reply definitely_not_a_task "continue" 2>&1)"
reply_rc=$?
assert_eq "reply to a nonexistent task exits non-zero" "1" "$reply_rc"
assert_match "reply names the missing task" 'no such task.*definitely_not_a_task' "$reply_out"
if [ -e "$SCRATCH_LOGDIR/definitely_not_a_task.meta" ] || [ -e "$SCRATCH_LOGDIR/definitely_not_a_task.log" ]; then
    fail "reply to a nonexistent task wrote ghost artifacts"
else
    pass "reply to a nonexistent task writes NO .meta/.log"
fi

# ============================================================================================
section "python entrypoints fail clearly without an interpreter"
# ============================================================================================
# Resolve bash NOW — the stripped PATH below must not break launching the interpreter itself.
TEST_BASH="$(command -v bash)"
gui_out="$(PATH=/nonexistent AGENT_CLI_LOGS="$SCRATCH_LOGDIR" "$TEST_BASH" "$HERE/agent.sh" gui 2>&1)"
gui_rc=$?
assert_eq "gui without python exits non-zero" "1" "$gui_rc"
assert_match "gui without python says so clearly" 'needs python' "$gui_out"

bridge_out="$(PATH=/nonexistent AGENT_CLI_LOGS="$SCRATCH_LOGDIR" "$TEST_BASH" "$HERE/agent.sh" openai-server 2>&1)"
bridge_rc=$?
assert_eq "openai-server without python exits non-zero" "1" "$bridge_rc"
assert_match "openai-server without python says so clearly" 'needs python' "$bridge_out"

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
