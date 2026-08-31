# Codex provider plugin for agent.sh.
# Contract: provider_codex_resolve, provider_codex_run_cmd, provider_codex_resume_cmd,
# provider_codex_doctor. See ../../agent.sh for the dispatch that calls these.

# `codex exec resume` DOES accept -m/--model and -c model_reasoning_effort=... (confirmed via
# `codex exec resume --help`, codex-cli 0.130.0) -- without forwarding them, a resumed session
# silently drifted to whatever codex's own default is (observed live: a session started with
# `-m spark` came back reporting `model: gpt-5.5` on resume). Opting in here like claude does.
PROVIDER_CODEX_RESUME_NEEDS_MODEL=1

# alias -> real model + effort. Sets globals P_MODEL / P_EFFORT (P_EFFORT may be empty).
# The 5.6 family (luna/sol/terra) needs codex-cli >= 0.144 -- older CLIs get a 400
# "The '<model>' model requires a newer version of Codex" from the server. gpt-5.6-terra is the
# default (user preference). A raw model id passed via -m still falls through unchanged (e.g.
# -m gpt-5.5 for an older model on demand).
provider_codex_resolve() {
    local alias="${1:-5.6-terra}"; P_EFFORT="medium"
    case "$alias" in
        ""|5.6|5.6-terra|terra|default) P_MODEL="gpt-5.6-terra" ;;
        5.6-sol|sol)                P_MODEL="gpt-5.6-sol" ;;
        5.6-high|high)              P_MODEL="gpt-5.6-sol"; P_EFFORT="high" ;;
        5.6-luna|luna)              P_MODEL="gpt-5.6-luna" ;;
        spark|5.3|5.3-spark|codex-spark) P_MODEL="gpt-5.3-codex-spark" ;;
        *) P_MODEL="$alias" ;;
    esac
}

# _provider_codex_emit — reads codex `--json` JSONL on stdin and emits agent.sh-friendly output:
#   1. a `session id: <uuid>` line (parsed from the `thread.started` event) so agent.sh's own
#      session-id grep keeps working for resume, exactly as it did with codex's plaintext banner;
#   2. a fresh `---------- output ----------` marker followed by ONLY the agent's final message
#      (the last `agent_message` item) — since `last_output` returns everything after the LAST
#      such marker, downstream (`agent.sh last`, the GUI, the openai-server bridge) sees a clean
#      answer with none of codex's banner/session-id/ERROR-log/"tokens used"/cp866-mojibake chrome.
# If no agent message is present (auth/rate-limit/schema drift) it echoes the raw stream AND exits
# non-zero, so the PIPESTATUS[1] guard below marks the task failed instead of "done with junk".
# If NO python is available at all, it degrades to a raw `cat` (functional, just with codex chrome)
# rather than hard-failing the whole codex run -- the openai-server bridge itself is python, so when
# the bridge is what's calling, python is guaranteed present and the clean path is always taken.
# NB: the parser is passed via `python -c`, NOT a `python - <<HEREDOC`: a heredoc would itself
# occupy stdin, so the piped codex JSONL would never reach sys.stdin.
_provider_codex_emit() {
    if ! _agent_python; then
        cat   # no usable python -> raw passthrough (degraded but not broken)
        return 0
    fi
    PYTHONIOENCODING=utf-8 "$_AGENT_PY" -c '
import sys, json
try:
    sys.stdin.reconfigure(errors="ignore")   # codex prints a cp866 OS-notification line that is not UTF-8
except Exception:
    pass
MARK = "---------- output ----------"
RAW_LIMIT = 262144
sid = None; msg = None; raw = []; raw_size = 0
for line in sys.stdin:
    raw.append(line)
    raw_size += len(line.encode("utf-8", "ignore"))
    while raw_size > RAW_LIMIT and len(raw) > 1:
        raw_size -= len(raw.pop(0).encode("utf-8", "ignore"))
    s = line.strip()
    if not s or s[0] != "{":
        continue
    try:
        o = json.loads(s)
    except Exception:
        continue
    t = o.get("type")
    if t == "thread.started" and o.get("thread_id"):
        sid = o["thread_id"]
        # Emit IMMEDIATELY, not after the stream ends. agent.sh greps the live log to fill
        # meta `session=`, and `reply` needs it while the task is still running - that is the
        # whole point of replying to a live agent. Printing it at EOF made `reply <name>` fail
        # with "could not find a session id" on every running codex task (observed 31.08.2026),
        # and the id only appeared once the answer was already delivered, when it is useless.
        # The opencode provider already emitted its id inline; codex now matches.
        # NO APOSTROPHES ANYWHERE IN THIS PARSER, INCLUDING COMMENTS. The whole thing is passed to
        # python -c inside a SINGLE-QUOTED bash string, so a single apostrophe closes that string
        # early and bash parses the rest of the python as shell. The symptom is a syntax error on a
        # line far below the real cause, and it broke agent.sh reply for every codex task. The
        # offending word was the possessive form of opencode. Writing that word again while
        # documenting the fix reintroduced the same break one line lower, so: no apostrophes.
        print("session id: %s" % sid, flush=True)
    elif t == "item.completed":
        it = o.get("item") or {}
        if it.get("type") == "agent_message" and it.get("text") is not None:
            msg = it["text"]
if msg is None:
    sys.stdout.write("".join(raw))          # surface the raw error stream, nothing clean to show
    raise SystemExit(3)                      # non-zero -> PIPESTATUS[1] guard marks the task failed
# Defuse the (pathological) case where the answer itself contains a line exactly equal to MARK:
# a trailing space stops last_output from treating it as the answer-boundary and truncating there.
msg = "\n".join((ln + " ") if ln == MARK else ln for ln in msg.split("\n"))
# The id was already printed inline when thread.started arrived; do not print it twice.
print(MARK)                                  # last_output slices to AFTER this -> clean answer only
sys.stdout.write(msg)
if not msg.endswith("\n"):
    sys.stdout.write("\n")
'
}

# _provider_codex_mcp_args — turns AGENT_CODEX_MCP="name=url,name2=url2" into repeated
#   -c mcp_servers.<name>.url="<url>"
# overrides. Isolation (below) drops ~/.codex/config.toml wholesale, which also drops the MCP
# servers that ARE useful (e.g. unityMCP); this is the surgical way to bring back only the ones a
# task actually needs, without re-enabling the desktop app's stdio hosts. Names are validated
# ([A-Za-z0-9_-] only) because they land inside a config key; empty segments are skipped, and a
# malformed segment is reported on stderr rather than silently dropped.
_provider_codex_mcp_args() {
    local spec="${AGENT_CODEX_MCP:-}" seg nm url
    [ -n "$spec" ] || return 0
    # read -ra, not an unquoted $spec: splitting an unquoted variable also PATHNAME-EXPANDS it, and
    # a URL query string ("...?x=1") would then be silently replaced by a matching filename.
    local -a segs; IFS=',' read -ra segs <<< "$spec"
    for seg in "${segs[@]}"; do
        seg="${seg#"${seg%%[![:space:]]*}"}"; seg="${seg%"${seg##*[![:space:]]}"}"   # trim
        [ -n "$seg" ] || continue
        nm="${seg%%=*}"; url="${seg#*=}"
        if [ "$nm" = "$seg" ] || [ -z "$nm" ] || [ -z "$url" ]; then
            echo "agent.sh: AGENT_CODEX_MCP: skipping malformed entry '$seg' (want name=url)" >&2
            continue
        fi
        case "$nm" in
            *[!A-Za-z0-9_-]*) echo "agent.sh: AGENT_CODEX_MCP: skipping bad server name '$nm' (allowed: A-Z a-z 0-9 _ -)" >&2; continue ;;
        esac
        printf '%s\n' -c "mcp_servers.$nm.url=\"$url\""
    done
}

# _provider_codex_isolation_args — ALWAYS run codex with --ignore-user-config (opt out with
# AGENT_CODEX_USER_CONFIG=1). This is the fix for "the agent answers but every shell command hangs
# forever".
#
# VERIFIED LIVE (codex-cli 0.144.0, Windows, git-bash):
#   codex exec -m gpt-5.6-terra --sandbox workspace-write --skip-git-repo-check "Run the shell
#   command: ls ..."                                     -> HANGS forever. Trace shows
#     ERROR codex_core::tools::router: error=code-mode host closed its stdout
#   preceded by
#     ERROR rmcp::transport::worker: worker quit with fatal: Transport channel closed ...
#     http://127.0.0.1:8000/mcp
#   The SAME command with --ignore-user-config finished in 360 ms with the correct answer.
# WHY: ~/.codex/config.toml on a machine with the ChatGPT desktop app installed is owned by that
# app. It declares a stdio MCP `node_repl` (the "code-mode host", a binary under
# AppData\Local\OpenAI\Codex\runtimes\cua_node\...), a `notify` hook into the desktop helper, and
# HTTP MCP servers that are only up while the app is (127.0.0.1:8000, unity-cli). Launched from a
# plain shell instead of from the desktop app, that host is unreachable and codex's tool router
# blocks on the FIRST exec call -- forever, with no output, so the task sits in state=running for
# hours. (Seen in the wild as `windows sandbox: helper_unknown_error: setup refresh had errors`.)
# Even when the desktop app IS running, loading its config costs real time: the same trivial
# one-shot took 37s with the user config vs 15s with --ignore-user-config (measured back to back).
# Auth is NOT affected: `codex exec --help` says the flag only skips $CODEX_HOME/config.toml,
# "auth still uses CODEX_HOME" (confirmed -- isolated runs log in fine).
# DO NOT remove this flag to "get MCP back": use AGENT_CODEX_MCP=name=url instead (above).
_provider_codex_isolation_args() {
    # chat-only is an even stricter mode (openai_server.py): always isolated, and never re-adds
    # MCP servers -- that mode is text completion only.
    if [ "${AGENT_CHAT_ONLY:-0}" = 1 ]; then
        printf '%s\n' --ignore-user-config
        return 0
    fi
    [ "${AGENT_CODEX_USER_CONFIG:-0}" = 1 ] && return 0   # escape hatch: old behaviour
    printf '%s\n' --ignore-user-config
    _provider_codex_mcp_args
}

# _provider_codex_chatonly_args — the sandbox flag plus the isolation flags above.
# AGENT_CHAT_ONLY=1 (set by openai_server.py; unset for normal `agent.sh run`, which legitimately
# needs real file/shell access) locks the model down to text-only completion:
#   --sandbox read-only     -- no file writes (verified live: a 'write a file' request is rejected
#                               by policy, model answers in text, no hang, no side effect).
#   --ignore-user-config    -- skips ~/.codex/config.toml entirely, which is where this machine's
#                               [mcp_servers.*] live (verified live: `-c mcp_servers={}` on the
#                               command line did NOT stop codex from actually calling a configured
#                               MCP server -- e.g. a real unityMCP tool against a live Unity Editor
#                               -- but --ignore-user-config does: list_mcp_resources came back
#                               empty and the model correctly reported no MCP tools available).
#                               Auth still works (`--help`: "auth still uses CODEX_HOME").
#
# SANDBOX MODE for normal runs: full auto, like every other engine here.
# gemini runs with --yolo, opencode with --auto, claude with --permission-mode acceptEdits, kimi with
# its auto policy -- an unattended run whose stdin is closed cannot answer a permission prompt, so
# every provider must be in its own "full auto" equivalent or it hangs forever.
# codex was the odd one out at --sandbox workspace-write, and on this Windows machine its sandbox
# helper is broken independently of the config problem above: every write came back as
#   patch rejected: writing is blocked by read-only sandbox; rejected by user approval settings
# (and earlier, as `windows sandbox: helper_unknown_error: setup refresh had errors`), so codex could
# run commands but never edit a file -- it reported the fix instead of applying it.
# Default is therefore danger-full-access, explicitly authorised by the repository owner.
# Override per run with AGENT_CODEX_SANDBOX=workspace-write (or read-only) if you want the sandbox
# back on a machine where it actually works.
# NOT negotiable: AGENT_CHAT_ONLY=1 (the openai_server.py bridge) stays read-only and ignores the
# variable. That mode exposes an HTTP endpoint that turns arbitrary callers into CLI invocations --
# it must never gain write or shell access, no matter what the environment says.
_provider_codex_chatonly_args() {
    if [ "${AGENT_CHAT_ONLY:-0}" = 1 ]; then
        printf '%s\n' --sandbox read-only
    else
        printf '%s\n' --sandbox "${AGENT_CODEX_SANDBOX:-danger-full-access}"
    fi
    _provider_codex_isolation_args
}

# Codex keeps one persistent file per thread and owns an OS-level exclusive lock on it while a
# writer is active. The files remain after release, so existence is not a liveness check. Probe the
# same lock non-blockingly (LockFileEx-compatible msvcrt on Windows, flock on Unix); return 0 only
# while held, 1 when free/missing, 2 when the probe is unavailable.
_provider_codex_thread_lock_file() {
    printf '%s/thread-writer-locks/%s.lock\n' "${CODEX_HOME:-$HOME/.codex}" "$1"
}

_provider_codex_thread_writer_held() {
    local lock; lock="$(_provider_codex_thread_lock_file "$1")"
    [ -e "$lock" ] || return 1
    _agent_python || return 2
    PYTHONIOENCODING=utf-8 "$_AGENT_PY" -c '
import errno, os, sys
path = sys.argv[1]
try:
    f = open(path, "r+b", buffering=0)
except FileNotFoundError:
    raise SystemExit(1)
except OSError:
    raise SystemExit(2)
try:
    if os.name == "nt":
        import msvcrt
        try:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN, getattr(errno, "EDEADLK", -1)):
                raise SystemExit(0)
            raise SystemExit(2)
        try:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
    else:
        import fcntl
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN, getattr(errno, "EWOULDBLOCK", -1)):
                raise SystemExit(0)
            raise SystemExit(2)
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
finally:
    f.close()
raise SystemExit(1)
' "$lock"
}

_provider_codex_writer_wait_limit() {
    local max="${AGENT_CODEX_WRITER_WAIT_SEC:-${AGENT_TIMEOUT_SEC:-1800}}"
    case "$max" in ''|*[!0-9]*) max=1800 ;; esac
    printf '%s\n' "$max"
}

_provider_codex_wait_for_writer() {
    local session="$1" max start deadline current remaining pause=1 probe_rc elapsed=0
    max="$(_provider_codex_writer_wait_limit)"; start="$(date +%s)"; deadline=$((start + max))
    PROVIDER_CODEX_WAITED_SEC=0; PROVIDER_CODEX_WAIT_NOTE=""
    while :; do
        _provider_codex_thread_writer_held "$session"; probe_rc=$?
        case "$probe_rc" in
            1)
                PROVIDER_CODEX_WAITED_SEC=$(( $(date +%s) - start ))
                if [ "$PROVIDER_CODEX_WAITED_SEC" -gt 0 ]; then
                    PROVIDER_CODEX_WAIT_NOTE="Codex thread writer released after ${PROVIDER_CODEX_WAITED_SEC}s; resume continuing"
                fi
                return 0
                ;;
            2)
                PROVIDER_CODEX_WAIT_NOTE="Codex thread-writer lock probe unavailable; relying on conflict detection/retry"
                echo "[agent.sh] ! $PROVIDER_CODEX_WAIT_NOTE" >&2
                return 0
                ;;
        esac
        current="$(date +%s)"; elapsed=$((current - start))
        if [ "$current" -ge "$deadline" ]; then
            PROVIDER_CODEX_WAITED_SEC="$elapsed"
            PROVIDER_CODEX_WAIT_NOTE="Codex thread writer still held after bounded ${max}s wait; no resume was started"
            echo "[agent.sh] ✖ $PROVIDER_CODEX_WAIT_NOTE" >&2
            return 75
        fi
        remaining=$((deadline - current)); [ "$pause" -le "$remaining" ] || pause="$remaining"
        echo "[agent.sh] ⏳ Codex thread writer is held for $session — waiting ${pause}s (bounded ${max}s)" >&2
        sleep "$pause"
        [ "$pause" -ge 8 ] || pause=$((pause * 2))
    done
}

# The Codex lock is released just before the old agent.sh process writes its final answer/state.
# Give that already-recorded wrapper pid a short bounded settling window too, so the reply header
# cannot interleave with the old answer or have its final state overwritten afterward.
_provider_codex_wait_for_prior_wrapper() {
    local task="${1:-}" pid state start deadline max current
    [ -n "$task" ] || return 0
    state="$(meta_get "$task" state)"; pid="$(meta_get "$task" pid)"
    [ "$state" = running ] && [ -n "$pid" ] && is_alive "$pid" || return 0
    max="$(_provider_codex_writer_wait_limit)"; [ "$max" -le 30 ] || max=30
    start="$(date +%s)"; deadline=$((start + max))
    while [ "$(meta_get "$task" state)" = running ] && is_alive "$pid"; do
        current="$(date +%s)"
        if [ "$current" -ge "$deadline" ]; then
            PROVIDER_CODEX_WAIT_NOTE="prior wrapper still finalizing after bounded ${max}s wait; no resume was started"
            echo "[agent.sh] ✖ $PROVIDER_CODEX_WAIT_NOTE" >&2
            return 75
        fi
        sleep 0.2
    done
    current=$(( $(date +%s) - start ))
    [ "$current" -gt 0 ] && PROVIDER_CODEX_WAIT_NOTE="${PROVIDER_CODEX_WAIT_NOTE:+$PROVIDER_CODEX_WAIT_NOTE; }prior wrapper settled after ${current}s"
}

# Optional agent.sh hook: critically, the dispatcher calls this before it writes the reply header
# or replaces the prior turn's pid/state in .meta.
provider_codex_prepare_resume() {
    local session="$1" task="${2:-}" rc
    PROVIDER_PREPARE_NOTE=""
    _provider_codex_wait_for_writer "$session" || {
        rc=$?; PROVIDER_PREPARE_NOTE="$PROVIDER_CODEX_WAIT_NOTE"; return "$rc"
    }
    _provider_codex_wait_for_prior_wrapper "$task" || {
        rc=$?; PROVIDER_PREPARE_NOTE="$PROVIDER_CODEX_WAIT_NOTE"; return "$rc"
    }
    [ -n "$PROVIDER_CODEX_WAIT_NOTE" ] && PROVIDER_PREPARE_NOTE="[agent.sh] $PROVIDER_CODEX_WAIT_NOTE"
    return 0
}

# provider_codex_run_cmd DIR MODEL EFFORT PROMPT — runs the CLI via `--json` and cleans the output.
# codex's plaintext `exec` mixes its banner/session-id/ERROR-log/"tokens used" chrome (and Windows
# cp866 mojibake) into the same stdout stream as the answer, which used to pollute `agent.sh last`,
# the GUI chat view, and the openai-server bridge's answer string. `--json` gives structured events
# instead, from which `_provider_codex_emit` pulls just the session id + the final agent message.
provider_codex_run_cmd() {
    local dir="$1" model="$2" effort="$3" prompt="$4"
    local -a sbargs; mapfile -t sbargs < <(_provider_codex_chatonly_args)
    codex exec -m "$model" -c model_reasoning_effort="$effort" \
        "${sbargs[@]}" --skip-git-repo-check -C "$dir" \
        --json "$prompt" </dev/null 2>&1 | _provider_codex_emit
    # Surface a parser failure (e.g. python missing/crashed) rather than masking it behind codex's
    # own exit code -- otherwise the task could be marked done with empty/partial output.
    local rc_codex=${PIPESTATUS[0]} rc_emit=${PIPESTATUS[1]}
    [ "$rc_emit" -ne 0 ] && return "$rc_emit"
    return "$rc_codex"
}

# provider_codex_resume_cmd DIR SESSION ANSWER — resumes an existing session. $P_MODEL/$P_EFFORT
# are set by agent.sh's provider_dispatch_resume just before this runs (PROVIDER_CODEX_RESUME_NEEDS_MODEL=1
# above opts into that) -- same pattern provider_claude_resume_cmd uses. Same `--json` cleanup.
_provider_codex_resume_once() {
    local raw="$1" dir="$2" session="$3" answer="$4"; shift 4
    ( cd "$dir" && codex exec resume --skip-git-repo-check \
        "$@" --json "$session" "$answer" </dev/null 2>&1 ) | tee "$raw" | _provider_codex_emit
    local statuses=("${PIPESTATUS[@]}")
    [ "${statuses[0]}" -ne 0 ] && return "${statuses[0]}"
    [ "${statuses[1]}" -ne 0 ] && return "${statuses[1]}"
    return "${statuses[2]}"
}

provider_codex_resume_cmd() {
    local dir="$1" session="$2" answer="$3" cargs attempt_log rc
    local -a isoargs; mapfile -t isoargs < <(_provider_codex_isolation_args)
    # Same sandbox policy as a fresh run (see _provider_codex_chatonly_args): full auto by default,
    # read-only and unoverridable for the chat-only bridge.
    if [ "${AGENT_CHAT_ONLY:-0}" = 1 ]; then
        cargs=(-c 'sandbox_mode="read-only"')
    else
        cargs=(-c "sandbox_mode=\"${AGENT_CODEX_SANDBOX:-danger-full-access}\"")
    fi
    # Same config isolation as a fresh run -- a resumed session goes through the exact same tool
    # router, so without it `reply` hangs on the first shell command just like `run` did.
    [ ${#isoargs[@]} -gt 0 ] && cargs+=("${isoargs[@]}")
    [ -n "$P_MODEL" ] && cargs+=(-m "$P_MODEL")
    [ -n "$P_EFFORT" ] && cargs+=(-c "model_reasoning_effort=\"$P_EFFORT\"")
    # Repeat the lock check immediately before exec to close most of the preflight/launch race.
    # If Codex still wins that race and returns its explicit conflict, wait again and retry ONCE.
    _provider_codex_wait_for_writer "$session" || return $?
    attempt_log="$(mktemp "$LOGDIR/.codex-resume-attempt.XXXXXX" 2>/dev/null)"
    [ -n "$attempt_log" ] && [ -f "$attempt_log" ] || {
        echo "agent.sh: cannot create Codex resume attempt log in $LOGDIR" >&2
        return 1
    }
    _provider_codex_resume_once "$attempt_log" "$dir" "$session" "$answer" "${cargs[@]}"; rc=$?
    if _has_thread_writer_conflict < "$attempt_log"; then
        echo "[agent.sh] ↻ Codex thread-writer conflict detected (exit=$rc) — bounded wait, then retry 1/1" >&2
        if _provider_codex_wait_for_writer "$session"; then
            _provider_codex_resume_once "$attempt_log" "$dir" "$session" "$answer" "${cargs[@]}"; rc=$?
        else
            rc=$?
        fi
    fi
    rm -f -- "$attempt_log"
    return "$rc"
}

# provider_codex_doctor_deep — the "does a shell command actually RUN" check for `agent.sh doctor
# --deep`. Everything else in doctor is metadata (binary present, version, login, limits); NONE of
# it catches the failure this provider is built around: codex answers normally but every exec call
# blocks forever on an unreachable code-mode host (see _provider_codex_isolation_args). So this
# does the only thing that can catch it -- one real, cheap, read-only run with a hard deadline.
#
# It runs in a throwaway temp dir containing a single randomly-named marker file and asks the model
# to LIST the directory with a shell command. The model cannot know that random name without
# actually executing something, so the marker coming back in the answer is proof that the whole
# tool-call round trip works -- while the command itself only reads.
# Env: AGENT_CODEX_DOCTOR_TIMEOUT (default 60s), AGENT_CODEX_DOCTOR_MODEL (default spark = the
# cheapest/fastest alias; the failure mode is model-independent).
provider_codex_doctor_deep() {
    if ! command -v codex >/dev/null 2>&1; then
        printf '  codex shell: —    (codex not in PATH)\n'; return 0
    fi
    local secs="${AGENT_CODEX_DOCTOR_TIMEOUT:-60}" tmpd marker out rc t0 elapsed
    provider_codex_resolve "${AGENT_CODEX_DOCTOR_MODEL:-spark}"
    tmpd="$(mktemp -d 2>/dev/null)" || { printf '  codex shell: BROKEN — cannot create temporary directory\n'; return 0; }
    [ -n "$tmpd" ] && [ -d "$tmpd" ] || { printf '  codex shell: BROKEN — invalid temporary directory\n'; return 0; }
    marker="RSDOCTOR${RANDOM}${RANDOM}"
    : > "$tmpd/$marker.txt"
    local -a sbargs; mapfile -t sbargs < <(_provider_codex_chatonly_args)
    t0=$(date +%s)
    if declare -F _guarded_run >/dev/null 2>&1; then
        out="$(_guarded_run "$secs" codex exec -m "$P_MODEL" -c model_reasoning_effort="$P_EFFORT" \
            "${sbargs[@]}" --skip-git-repo-check -C "$tmpd" --json \
            "Run one shell command that lists the files in the current directory, then reply with ONLY the file names." \
            </dev/null 2>&1)"
        rc=$?
    else
        out="$(codex exec -m "$P_MODEL" -c model_reasoning_effort="$P_EFFORT" \
            "${sbargs[@]}" --skip-git-repo-check -C "$tmpd" --json \
            "Run one shell command that lists the files in the current directory, then reply with ONLY the file names." \
            </dev/null 2>&1)"
        rc=$?
    fi
    elapsed=$(( $(date +%s) - t0 ))
    rm -rf "$tmpd" 2>/dev/null
    local hint="hint: ~/.codex/config.toml (owned by the ChatGPT desktop app) can hang codex's tool router; agent.sh passes --ignore-user-config by default -- do not set AGENT_CODEX_USER_CONFIG=1"
    if [ "$rc" = 124 ]; then
        printf '  codex shell: BROKEN — no answer within %ss, the tool router is hung (%s)\n' "$secs" "$hint"
        case "$out" in *"code-mode host"*) printf '  codex shell: signature — "code-mode host closed its stdout" in the trace\n' ;; esac
        return 0
    fi
    case "$out" in
        *"$marker"*) printf '  codex shell: ok   (%s, %ss, real command executed)\n' "$P_MODEL" "$elapsed"; return 0 ;;
    esac
    if [ -z "$out" ]; then
        printf '  codex shell: BROKEN — codex produced no output at all (exit %s) (%s)\n' "$rc" "$hint"
    else
        printf '  codex shell: BROKEN — the shell command never came back (exit %s, %ss); last line: %s\n' \
            "$rc" "$elapsed" "$(printf '%s' "$out" | tr -d '\r' | grep -v '^[[:space:]]*$' | tail -1 | cut -c1-160)"
        printf '  codex shell: %s\n' "$hint"
    fi
}

# provider_codex_doctor — prints a single-line JSON object to stdout:
# {"engine":"codex","version":"...","available":true|false,"login":"...","limits":{...}|null,"note":"..."}
provider_codex_doctor() {
    local ver login
    if command -v codex >/dev/null 2>&1; then
        ver="$(codex --version 2>&1 | head -1)"
        login="$(codex login status 2>&1 | head -1)"
        if ! _agent_python; then
            printf '{"engine":"codex","version":%s,"available":true,"login":%s,"limits":null,"note":"Python unavailable; limits probe skipped."}\n' \
                "$(_json_str "$ver")" "$(_json_str "$login")"
            return 0
        fi
        PYTHONIOENCODING=utf-8 "$_AGENT_PY" - "$ver" "$login" <<'PY'
import json, glob, os, sys
ver, login = sys.argv[1], sys.argv[2]
files = sorted(glob.glob(os.path.expanduser('~/.codex/sessions/**/*.jsonl'), recursive=True), key=os.path.getmtime)[-8:]
def find(d):
    if isinstance(d, dict):
        if 'rate_limits' in d:
            return d['rate_limits']
        for v in d.values():
            r = find(v)
            if r:
                return r
    return None
rl = None
for f in files:
    try:
        for line in open(f, encoding='utf-8', errors='ignore'):
            if '"rate_limits"' in line:
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                r = find(o)
                if r:
                    rl = r
    except Exception:
        pass
print(json.dumps({"engine": "codex", "version": ver, "available": True,
                   "login": login, "limits": rl, "note": ""}, separators=(",", ":")))
PY
    else
        printf '{"engine":"codex","version":"NOT_FOUND","available":false,"login":"","limits":null,"note":""}\n'
    fi
}
