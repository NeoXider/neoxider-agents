# opencode provider plugin for agent.sh.
# Contract: provider_opencode_resolve, provider_opencode_run_cmd, provider_opencode_resume_cmd,
# provider_opencode_doctor.
#
# Resume IS supported. `opencode run` takes `-s/--session <id>` (and `-c/--continue`), and the JSON
# event stream already carries `sessionID`, which _provider_opencode_emit prints as the `session id:`
# line agent.sh records in the task meta. The old `supports_resume:false` predated those flags and
# cost real work: a running agent could not be corrected mid-task, so a task given a wrong spec had
# to be killed and relaunched from zero.

# alias -> real model id. Sets P_MODEL; P_EFFORT stays empty (opencode takes effort only via the
# separate --variant flag, i.e. agent.sh's -f, never as an alias suffix).
#
# WHY THIS EXISTS: opencode's own model ids are neither guessable nor stable-looking
# ("muse-spark-1.2-contributor-free", "x-preview-f-free"), and the interactive picker is the only
# place they are shown with human names. An orchestrator driving agent.sh headlessly could not name
# a free model without first shelling out to `opencode models`. These aliases pin the OpenCode Zen
# free tier by the label the picker displays.
#
# Zen free tier as listed by `opencode models` on 2026-08-24 (all 7 are free):
#   big-pickle | hy3-free | mimo-v2.5-free | muse-spark-1.2-contributor-free
#   nemotron-3-ultra-free | nemotron-3.5-lightning-free | x-preview-f-free
# The list is a moving target — re-check with `opencode models`; an unknown alias still falls
# through unchanged, so a raw `-m opencode/<whatever>` keeps working.
#
# NOT ranked by any benchmark: `free` points at muse-spark because that is the user's pick, not
# because it was measured against the others.
provider_opencode_resolve() {
    local alias="${1:-}"; P_EFFORT=""
    case "$alias" in
        # 03.09.2026: вышел 1.3. Комментарий держим ПОСЛЕ ;; — иначе он съедает
        # закрытие ветки и весь case ломается синтаксической ошибкой.
        free|spark|muse|muse-spark) P_MODEL="opencode/muse-spark-1.3-contributor-free" ;;
        ox|ox-alpha|alpha)          P_MODEL="opencode/x-preview-f-free" ;;
        pickle|big-pickle)          P_MODEL="opencode/big-pickle" ;;
        hy3)                        P_MODEL="opencode/hy3-free" ;;
        mimo)                       P_MODEL="opencode/mimo-v2.5-free" ;;
        nemotron|ultra)             P_MODEL="opencode/nemotron-3-ultra-free" ;;
        lightning|nemotron-fast)    P_MODEL="opencode/nemotron-3.5-lightning-free" ;;
        *)                          P_MODEL="$alias" ;;
    esac
}

# _provider_opencode_emit — reads opencode `--format json` JSONL on stdin and emits the agent.sh
# output contract: a `session id: <id>` line (for reference), throttled activity heartbeats while
# the tool loop is running, then a fresh `---------- output ----------` marker followed by ONLY the
# assistant's final text (the concatenated `text` parts). Heartbeats deliberately precede the final
# marker, so agent.sh last/openai_server still see a clean answer while status/log -f no longer look
# frozen for the whole run. When AGENT_OPENCODE_ANSWER_FILE is set the marker+answer go to that file
# instead of stdout so the caller can order the stderr diagnostics before the answer (below).
# No usable python -> raw passthrough.
_provider_opencode_emit() {
    if ! _agent_python; then cat; return 0; fi
    PYTHONIOENCODING=utf-8 "$_AGENT_PY" -c '
import sys, json, time, os
try:
    sys.stdin.reconfigure(errors="ignore")
except Exception:
    pass
MARK = "---------- output ----------"
RAW_LIMIT = 262144
# When set, the final MARK+answer block goes to this file instead of stdout, so the caller can
# print the CLI stderr diagnostics FIRST and only then let the answer be the last thing written
# (see _provider_opencode_invoke). Without this, agent.sh merged stdout+stderr put every
# "[opencode] ..." diagnostic INSIDE the answer block and last_output returned them as the answer.
ANSWER_FILE = os.environ.get("AGENT_OPENCODE_ANSWER_FILE") or ""
sid = None; parts = {}; order = []; raw = []; raw_size = 0
last_activity = 0.0
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
    if o.get("sessionID") and sid is None:
        sid = o["sessionID"]
        print("session id: %s" % sid, flush=True)
    event_type = o.get("type") or "event"
    now = time.monotonic()
    if now - last_activity >= 10.0:
        print("[opencode] activity: %s" % event_type, flush=True)
        last_activity = now
    if o.get("type") == "text":
        p = o.get("part") or {}
        txt = p.get("text")
        if txt is not None:
            pid = p.get("id") or len(order)
            if pid not in parts:
                order.append(pid)
            parts[pid] = txt   # full text per part; last write wins on any re-emit
msg = "".join(parts[p] for p in order).strip()
if not msg:
    sys.stdout.write("".join(raw))   # nothing clean -> surface raw for debugging
    raise SystemExit(3)              # non-zero -> agent.sh marks the task failed
msg = "\n".join((ln + " ") if ln == MARK else ln for ln in msg.split("\n"))
block = MARK + "\n" + msg + ("" if msg.endswith("\n") else "\n")
if ANSWER_FILE:
    with open(ANSWER_FILE, "w", encoding="utf-8") as handle:
        handle.write(block)
else:
    sys.stdout.write(block)
'
}

# _provider_opencode_chatonly_args — extra CLI arguments for AGENT_CHAT_ONLY=1 (the
# openai_server.py bridge). opencode has no "disable tools" flag, but its config format has a
# per-agent tool map, and `"tools": {"*": false}` disables EVERY tool — built-ins (bash/write/
# edit/read/webfetch/…) AND any MCP server the user has configured globally. Verified live on
# opencode 1.18.16: without this the bridge's opencode sessions answered
# "apply_patch, bash, edit, glob, grep, question, read, skill, todowrite, webfetch, websearch,
# write" plus 40 unityMCP tools, and actually wrote a file on disk; with it they answer "NONE".
# OPENCODE_CONFIG MERGES with the user's own config (providers/models/auth stay intact), it does
# not replace it — also verified live. Never set for a normal `agent.sh run`, which legitimately
# needs full tool access.
_provider_opencode_chatonly() { [ "${AGENT_CHAT_ONLY:-0}" = 1 ]; }
# `cygpath -m` because opencode is a NATIVE Windows binary: it cannot open a git-bash
# /c/Users/... path, only C:/Users/... . A no-op (plain path) on Linux/macOS.
_provider_opencode_chatonly_config() {
    local p="$HERE/providers/opencode/chat-only.json"
    [ -r "$p" ] || return 1
    command -v cygpath >/dev/null 2>&1 && p="$(cygpath -m "$p" 2>/dev/null || printf '%s' "$p")"
    printf '%s' "$p"
}
_provider_opencode_chatonly_args() {
    _provider_opencode_chatonly && printf '%s\n' --agent neoxider-chat-only
    return 0
}

_provider_opencode_image_args() {
    _provider_opencode_chatonly || return 0
    [ -n "${AGENT_OPENCODE_IMAGE_PATHS:-}" ] || return 0
    local path check
    while IFS= read -r path; do
        [ -n "$path" ] || continue
        check="$path"
        command -v cygpath >/dev/null 2>&1 && check="$(cygpath -u "$path" 2>/dev/null || printf '%s' "$path")"
        [ -f "$check" ] && printf '%s\n' -f "$path"
    done <<< "$AGENT_OPENCODE_IMAGE_PATHS"
}

# ---------------------------------------------------------------------------------------------
# Reliability helpers (2026-09-30). See the header of tools/patch_opencode_provider.py in the
# neoxider-agents changelog for the incidents behind each of them.
# ---------------------------------------------------------------------------------------------

# _provider_opencode_model_of ARGS... — prints the value following -m/--model, if any.
_provider_opencode_model_of() {
    local prev=""
    for a in "$@"; do
        if [ "$prev" = "-m" ] || [ "$prev" = "--model" ]; then printf '%s' "$a"; return 0; fi
        prev="$a"
    done
    return 0
}

# _provider_opencode_pin_env MODEL — prints KEY=VALUE lines for `env`. ALWAYS prints at least one
# (keeps `export` from being called with no operands).
#
# small_model pinning: opencode uses the "small model" of the user's config for background work
# (titles, summaries) EVEN WHEN -m names a different model. A user whose small_model is a local
# server that is not running (LM Studio) got sessions killed with `stream error ... small=true` while
# the main hosted model was healthy. The run's own model is the only one we know works, so it also
# becomes small_model - via OPENCODE_CONFIG_CONTENT, which merges over the user's files and never
# edits them. Opt out with AGENT_OPENCODE_KEEP_SMALL_MODEL=1; a caller-provided
# OPENCODE_CONFIG_CONTENT is respected untouched.
_provider_opencode_pin_env() {
    local model="$1"
    printf '%s\n' "AGENT_OPENCODE_WRAPPED=1"
    [ "${AGENT_OPENCODE_KEEP_SMALL_MODEL:-0}" = 1 ] && return 0
    [ -n "${OPENCODE_CONFIG_CONTENT:-}" ] && return 0
    [ -n "$model" ] || return 0
    # ids are provider/model with [A-Za-z0-9._:/-] only; anything else is not worth JSON-escaping
    printf '%s' "$model" | grep -qE '^[A-Za-z0-9._:/-]+$' || return 0
    printf '%s\n' "OPENCODE_CONFIG_CONTENT={\"small_model\":\"${model}\"}"
}

# _provider_opencode_keepalive STATEFILE — background loop. Every AGENT_OPENCODE_KEEPALIVE_SEC (60)
# seconds prints a heartbeat line IF a build/tool process exists under this task's process tree.
# Why: a tool call (dotnet build, an IL lift, a test run) makes opencode emit no events for many
# minutes, and agent.sh's no-output watchdog (AGENT_SILENCE_SEC=600) killed healthy agents as
# state=silent. The heartbeat is conditional on a live tool process, so a genuinely wedged engine
# (no tool running) is still caught by the watchdog, and AGENT_TIMEOUT_SEC still bounds everything.
# Windows only (needs /proc/<pid>/winpid and PowerShell); a no-op elsewhere.
_provider_opencode_keepalive() {
    [ "${AGENT_OPENCODE_TOOL_KEEPALIVE:-1}" = 1 ] || return 0
    local root; root="$(cat "/proc/$$/winpid" 2>/dev/null)"
    [ -n "$root" ] || return 0
    command -v powershell >/dev/null 2>&1 || return 0
    local every="${AGENT_OPENCODE_KEEPALIVE_SEC:-60}" n
    local ps='$root=[int]$args[0]; $all=Get-CimInstance Win32_Process; $ids=@{$root=1}; do{$c=0; foreach($p in $all){ if($ids.ContainsKey([int]$p.ParentProcessId) -and -not $ids.ContainsKey([int]$p.ProcessId)){$ids[[int]$p.ProcessId]=1;$c++} }}while($c); $names="dotnet","MSBuild","csc","VBCSCompiler","git","python","python3","pwsh","robocopy","unzip","cl","link","ninja","make","curl","cargo","node","npm","ilspycmd","Cpp2IL"; @($all | Where-Object { $ids.ContainsKey([int]$_.ProcessId) -and $_.ProcessId -ne $PID -and $_.ParentProcessId -ne $PID -and ($names -contains ($_.Name -replace "\.exe$","")) }).Count'
    while :; do
        sleep "$every"
        n="$(powershell -NoProfile -ExecutionPolicy Bypass -Command "& { $ps }" "$root" 2>/dev/null | tr -d '\r' | tail -1)"
        case "$n" in ''|*[!0-9]*) n=0 ;; esac
        [ "$n" -gt 0 ] && printf '[opencode] activity: tool running (%s helper processes)\n' "$n"
    done
}

# _provider_opencode_invoke DIR PROMPT EXTRA... — the single CLI invocation both run and resume use.
# EXTRA are session flags (`-s <id>`) or nothing. Kept as one function on purpose: run and resume
# must share the stdin/stderr/timeout handling below, or a fix to one silently skips the other.
_provider_opencode_invoke() {
    local dir="$1" prompt="$2"; shift 2
    local timeout_sec="${AGENT_OPENCODE_TIMEOUT_SEC:-${AGENT_TIMEOUT_SEC:-1800}}"
    # --print-logs --log-level ERROR: without them opencode keeps provider failures to itself and the
    # task looks like an unexplained hang. Observed live 2026-09-04: the free tier answered
    # `AI_APICallError: Rate limit exceeded` ONE SECOND into the request, opencode did not exit on it,
    # and the step sat until the watchdog killed it 30 minutes later — five tasks in a row, each
    # reported as "turn died" with an empty log. The reason existed the whole time; nobody asked for
    # it. Logs go to stderr, so the JSONL on stdout stays clean.
    local args=(--auto --format json --print-logs --log-level ERROR "$@")
    mapfile -t -O ${#args[@]} args < <(_provider_opencode_chatonly_args)
    local -a image_args; mapfile -t image_args < <(_provider_opencode_image_args)
    [ ${#image_args[@]} -gt 0 ] && args+=("${image_args[@]}")
    if _provider_opencode_chatonly; then
        local chat_config
        chat_config="$(_provider_opencode_chatonly_config)" \
            || { printf '[opencode] bundled chat-only config is unavailable\n' >&2; return 1; }
        [ -n "$chat_config" ] \
            || { printf '[opencode] bundled chat-only config resolved to an empty path\n' >&2; return 1; }
        export OPENCODE_CONFIG="$chat_config"
    fi
    # stdout carries JSONL only. Keep stderr visible without corrupting that stream: diagnostics are
    # prefixed and sent through the provider stderr, which generic dispatch records in the task log.
    # A bounded default prevents an unattended provider/plugin deadlock from living forever. Set
    # AGENT_OPENCODE_TIMEOUT_SEC=0 to disable it (also useful for shell-function test doubles).
    #
    # stderr goes to a TEMP FILE, never to a `2> >(...)` process substitution. The substitution
    # deadlocked agent.sh: its reader is orphaned (reparented to PID 1) and the write end of its
    # pipe gets inherited by the `| tee -a "$log" | tail -40` that generic dispatch wraps us in, so
    # the reader never sees EOF, tee/tail never finish, and the task hangs after opencode itself is
    # long gone. Observed live 2026-08-24: task `scan-muse` sat "running" for 143 minutes with a
    # process tree of exactly {bash, tee, tail, orphaned reader} and NO opencode process — the
    # `timeout` below had already done its job and killed the CLI. A temp file has no reader to
    # deadlock on.
    # A prompt too long for argv goes in on stdin instead. `opencode run` with no positional
    # message reads the message from stdin, and a file redirect hits EOF immediately, so this
    # keeps the "never wait on an interactive stdin" property the redirect below is there for.
    local -a command=(opencode run "${args[@]}")
    local promptfile=""
    # OpenCode's repeatable -f consumes following positional words as more file paths. With an
    # attachment, send the prompt on stdin so it can never be parsed as a filename.
    if [ ${#image_args[@]} -gt 0 ] || prompt_needs_stdin "$prompt"; then
        promptfile="$(prompt_stdin_file "$prompt")" \
            || { printf '[opencode] cannot stage a long prompt for stdin\n' >&2; return 1; }
    else
        command+=("$prompt")
    fi
    local -a statuses
    local errfile; errfile="$(mktemp -t opencode-stderr-XXXXXX 2>/dev/null)" \
        || { printf '[opencode] cannot create temporary stderr file\n' >&2; return 1; }
    [ -n "$errfile" ] && [ -f "$errfile" ] \
        || { printf '[opencode] mktemp returned an invalid stderr file\n' >&2; return 1; }
    local stdin_src="/dev/null"; [ -n "$promptfile" ] && stdin_src="$promptfile"
    # The final answer is parked in a file and printed AFTER the stderr diagnostics below. Without
    # this, everything agent.sh merges into one log made the CLI's own diagnostics part of the
    # answer: `--print-logs --log-level ERROR` makes this model emit ~80
    # `[opencode] unknown format "uint32" ignored in schema ...` lines per turn, and since they were
    # written after the marker, last_output/openai_server returned them as the completion content
    # (verified live on the `Red` + 81-lines case, task state=done exit=0). Heartbeats keep
    # streaming to stdout during the run, so the silence watchdog is unaffected.
    local answerfile; answerfile="$(mktemp -t opencode-answer-XXXXXX 2>/dev/null)"
    if [ -z "$answerfile" ] || [ ! -f "$answerfile" ]; then
        printf '[opencode] cannot create temporary answer file\n' >&2; return 1
    fi
    local previous_answer_file="${AGENT_OPENCODE_ANSWER_FILE:-}"
    export AGENT_OPENCODE_ANSWER_FILE="$answerfile"
    local -a pin_env; mapfile -t pin_env < <(_provider_opencode_pin_env "$(_provider_opencode_model_of "$@")")
    local keepalive_pid=""
    _provider_opencode_keepalive & keepalive_pid=$!
    if [ "$timeout_sec" -gt 0 ] 2>/dev/null && command -v timeout >/dev/null 2>&1; then
        ( cd "$dir" && export "${pin_env[@]}" && timeout --foreground --kill-after=10s "${timeout_sec}s" "${command[@]}" <"$stdin_src" 2>"$errfile" ) \
            | _provider_opencode_emit
    else
        ( cd "$dir" && export "${pin_env[@]}" && "${command[@]}" <"$stdin_src" 2>"$errfile" ) | _provider_opencode_emit
    fi
    statuses=("${PIPESTATUS[@]}")
    [ -n "$keepalive_pid" ] && { kill "$keepalive_pid" 2>/dev/null; wait "$keepalive_pid" 2>/dev/null; }
    if [ -n "$previous_answer_file" ]; then
        export AGENT_OPENCODE_ANSWER_FILE="$previous_answer_file"
    else
        unset AGENT_OPENCODE_ANSWER_FILE
    fi
    [ -n "$promptfile" ] && rm -f "$promptfile"
    local rate_limited="" rate_line=""
    if [ -s "$errfile" ]; then
        # Read the reason BEFORE the file is removed: on a timeout the exit code alone says nothing
        # about why, and "rate limit" is the one cause an orchestrator must not mistake for a broken
        # task — retrying it immediately just burns another watchdog window.
        rate_line="$(grep -aiE 'rate limit|429|quota' "$errfile" | tail -1)"
        [ -n "$rate_line" ] && rate_limited=1
        # Transient = the CLI failed AND said so in a way that a retry can fix. Decided here, while
        # the stderr is still on disk; consumed by _provider_opencode_invoke_retry via a state file.
        if [ -z "$rate_limited" ] && [ "${statuses[0]}" -ne 124 ] \
           && { [ "${statuses[0]}" -ne 0 ] || [ "${statuses[1]}" -ne 0 ]; } \
           && grep -aiEq 'stream error|Failed to execute|ECONNRESET|ECONNREFUSED|ETIMEDOUT|EAI_AGAIN|socket hang up|fetch failed|network error|overloaded|Bad Gateway|Service Unavailable|Gateway Time-?out|HTTP (502|503|504)|status[ =:]+(502|503|504)' "$errfile"; then
            [ -n "${AGENT_OPENCODE_STATE_FILE:-}" ] && printf 'transient\n' >"$AGENT_OPENCODE_STATE_FILE"
        fi
        while IFS= read -r line; do printf '[opencode] %s\n' "$line" >&2; done <"$errfile"
    fi
    rm -f "$errfile"
    # The answer goes out LAST, so every diagnostic above lands BEFORE the output marker and stays
    # readable in the log without ever becoming part of the answer block.
    if [ -s "$answerfile" ]; then cat "$answerfile"; fi
    rm -f "$answerfile"
    # Tagged the same way codex/kimi tag their own provider errors (see agent.sh's
    # _extract_provider_reason): this is what turns a generic error/timeout into agent.sh's distinct
    # state=limited, with the CLI's own message in meta reason= instead of just this stderr line.
    [ -n "$rate_limited" ] && printf 'AGENT_PROVIDER_ERROR: %s\n' "$rate_line" >&2
    if [ "${statuses[0]}" -eq 124 ]; then
        if [ -n "$rate_limited" ]; then
            printf '[opencode] provider rate limit hit; opencode did not exit on it and the step ran out its %ss budget. Wait for the limit to reset or switch engine/model — this is NOT a task failure.\n' \
                "$timeout_sec" >&2
        else
            printf '[opencode] timed out after %ss\n' "$timeout_sec" >&2
        fi
        return 124
    fi
    if [ -n "$rate_limited" ]; then
        printf '[opencode] provider reported a rate limit — the answer may be missing or truncated.\n' >&2
    fi
    [ "${statuses[0]}" -ne 0 ] && return "${statuses[0]}"
    return "${statuses[1]}"
}

# _provider_opencode_invoke_retry DIR PROMPT EXTRA... — _provider_opencode_invoke plus bounded automatic
# recovery from TRANSIENT provider/network failures (stream errors, connection resets, 502/503/504,
# "Failed to execute"). Before 2026-09-30 such a failure ended the task as state=error and a human
# had to reply "continue"; with eight agents running that happened three times in an hour.
# The retry CONTINUES THE SAME SESSION (-s <id>, taken from the `session id:` line the first attempt
# printed) with a short continue prompt, so nothing already read or done is thrown away; with no
# session id yet it re-runs the original prompt. Backoff 15s, 30s, 60s...
# NEVER retried: rate limits (retrying just burns the next window), watchdog timeouts (rc 124),
# and any failure whose cause is not recognisably transient. AGENT_OPENCODE_RETRIES=0 disables.
_provider_opencode_invoke_retry() {
    local dir="$1" prompt="$2"; shift 2
    local max="${AGENT_OPENCODE_RETRIES:-3}" attempt=0 rc tmp sid delay
    local -a base=("$@") cur
    case "$max" in ''|*[!0-9]*) max=3 ;; esac
    local model; model="$(_provider_opencode_model_of "$@")"
    local statefile; statefile="$(mktemp -t opencode-state-XXXXXX 2>/dev/null)" || statefile=""
    export AGENT_OPENCODE_STATE_FILE="$statefile"
    cur=("${base[@]}"); local cur_prompt="$prompt"
    while :; do
        : >"$statefile" 2>/dev/null
        tmp="$(mktemp -t opencode-try-XXXXXX 2>/dev/null)"
        _provider_opencode_invoke "$dir" "$cur_prompt" "${cur[@]}" | tee "$tmp"
        rc=${PIPESTATUS[0]}
        if [ "$rc" -eq 0 ] || [ "$max" -eq 0 ] || [ "$attempt" -ge "$max" ] \
           || ! grep -q '^transient$' "$statefile" 2>/dev/null; then
            rm -f "$tmp" "$statefile"; unset AGENT_OPENCODE_STATE_FILE
            return "$rc"
        fi
        attempt=$((attempt + 1))
        sid="$(grep -a '^session id:' "$tmp" | head -1 | awk '{print $3}')"
        # prefer the session from this attempt; otherwise keep whatever session the caller passed
        if [ -z "$sid" ]; then
            local prev="" a
            for a in "${base[@]}"; do [ "$prev" = "-s" ] && sid="$a"; prev="$a"; done
        fi
        rm -f "$tmp"
        delay=$((15 * (1 << (attempt - 1)))); [ "$delay" -gt 120 ] && delay=120
        printf '[opencode] transient provider/network failure (rc=%s) - retry %s/%s in %ss%s\n' \
            "$rc" "$attempt" "$max" "$delay" "${sid:+, continuing session $sid}" >&2
        sleep "$delay"
        cur=(); local skip=0 a
        for a in "${base[@]}"; do
            if [ "$skip" -eq 1 ]; then skip=0; continue; fi
            case "$a" in -s|--session) skip=1; continue ;; --continue) continue ;; esac
            cur+=("$a")
        done
        if [ -n "$sid" ]; then
            cur+=(-s "$sid")
            cur_prompt="Your previous turn was interrupted by a transient provider/network error. Continue exactly where you left off - read your PROGRESS file if you need to - and do not redo finished steps."
        else
            cur_prompt="$prompt"
        fi
    done
}

# provider_opencode_run_cmd DIR MODEL EFFORT PROMPT — runs the CLI and emits clean final text.
# MODEL is the raw -m value (may be empty). EFFORT maps to opencode's --variant flag (its
# reasoning-effort equivalent: high/max/minimal/...), if given.
# --format json: machine-readable event stream we parse for the final assistant message (see emit).
# --auto: auto-approve permissions that are not explicitly denied -- without it opencode can block on
# a permission prompt, which would hang forever since stdin is closed (</dev/null). Fully unattended
# runs need it. NOTE: opencode renamed this from --dangerously-skip-permissions to --auto; the old
# flag now fails `opencode run` with "Unexpected server error".
provider_opencode_run_cmd() {
    local dir="$1" model="$2" effort="$3" prompt="$4"
    local -a extra=()
    [ -n "$model" ] && extra+=(-m "$model")
    [ -n "$effort" ] && extra+=(--variant "$effort")
    _provider_opencode_invoke_retry "$dir" "$prompt" "${extra[@]}"
}

# agent.sh resolves the alias into $P_MODEL/$P_EFFORT before calling resume when this is 1. opencode
# remembers neither across `run -s`, so a resumed turn without them would silently switch model.
PROVIDER_OPENCODE_RESUME_NEEDS_MODEL=1

# provider_opencode_resume_cmd DIR SESSION ANSWER — continues an existing session.
# `-s <id>` continues that exact session; with no id `--continue` takes the last one in this
# directory, which is why agent.sh records the session from the `session id:` line at run time.
# WHY this matters: without resume a task given a wrong or incomplete spec could only be killed and
# restarted from zero, throwing away everything it had already read and done.
provider_opencode_resume_cmd() {
    local dir="$1" session="$2" answer="$3"
    local -a extra=()
    [ -n "$P_MODEL" ] && extra+=(-m "$P_MODEL")
    [ -n "$P_EFFORT" ] && extra+=(--variant "$P_EFFORT")
    if [ -n "$session" ]; then extra+=(-s "$session"); else extra+=(--continue); fi
    _provider_opencode_invoke_retry "$dir" "$answer" "${extra[@]}"
}

# provider_opencode_doctor — prints a single-line JSON object to stdout.
provider_opencode_doctor() {
    local ver
    if command -v opencode >/dev/null 2>&1; then
        ver="$(opencode --version 2>&1 | head -1)"
        printf '{"engine":"opencode","version":%s,"available":true,"login":"","limits":null,"note":"No CLI limits endpoint for this provider."}\n' \
            "$(_json_str "$ver")"
    else
        printf '{"engine":"opencode","version":"NOT_FOUND","available":false,"login":"","limits":null,"note":"No CLI limits endpoint for this provider."}\n'
    fi
}
