# opencode provider plugin for agent.sh.
# Contract: provider_opencode_resolve, provider_opencode_run_cmd, provider_opencode_doctor.
# No provider_opencode_resume_cmd: reply was never supported for opencode (matches today).

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
        free|spark|muse|muse-spark) P_MODEL="opencode/muse-spark-1.2-contributor-free" ;;
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
# frozen for the whole run. No usable python -> raw passthrough.
_provider_opencode_emit() {
    if ! _agent_python; then cat; return 0; fi
    PYTHONIOENCODING=utf-8 "$_AGENT_PY" -c '
import sys, json, time
try:
    sys.stdin.reconfigure(errors="ignore")
except Exception:
    pass
MARK = "---------- output ----------"
sid = None; parts = {}; order = []; raw = []
last_activity = 0.0
for line in sys.stdin:
    raw.append(line)
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
print(MARK)
sys.stdout.write(msg)
if not msg.endswith("\n"):
    sys.stdout.write("\n")
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
    local p="${AGENT_OPENCODE_CHATONLY_CONFIG:-$HERE/providers/opencode/chat-only.json}"
    command -v cygpath >/dev/null 2>&1 && p="$(cygpath -m "$p" 2>/dev/null || printf '%s' "$p")"
    printf '%s' "$p"
}
_provider_opencode_chatonly_args() {
    _provider_opencode_chatonly && printf '%s\n' --agent neoxider-chat-only
    return 0
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
    local timeout_sec="${AGENT_OPENCODE_TIMEOUT_SEC:-${AGENT_TIMEOUT_SEC:-1800}}"
    local args=(--auto --format json)
    [ -n "$model" ] && args+=(-m "$model")
    [ -n "$effort" ] && args+=(--variant "$effort")
    mapfile -t -O ${#args[@]} args < <(_provider_opencode_chatonly_args)
    if _provider_opencode_chatonly; then
        export OPENCODE_CONFIG="$(_provider_opencode_chatonly_config)"
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
    local -a command=(opencode run "${args[@]}" "$prompt")
    local -a statuses
    local errfile; errfile="$(mktemp -t opencode-stderr-XXXXXX 2>/dev/null || printf '%s' "${TMPDIR:-/tmp}/opencode-stderr-$$")"
    if [ "$timeout_sec" -gt 0 ] 2>/dev/null && command -v timeout >/dev/null 2>&1; then
        ( cd "$dir" && timeout --foreground --kill-after=10s "${timeout_sec}s" "${command[@]}" </dev/null 2>"$errfile" ) \
            | _provider_opencode_emit
    else
        ( cd "$dir" && "${command[@]}" </dev/null 2>"$errfile" ) | _provider_opencode_emit
    fi
    statuses=("${PIPESTATUS[@]}")
    if [ -s "$errfile" ]; then
        while IFS= read -r line; do printf '[opencode] %s\n' "$line" >&2; done <"$errfile"
    fi
    rm -f "$errfile"
    if [ "${statuses[0]}" -eq 124 ]; then
        printf '[opencode] timed out after %ss\n' "$timeout_sec" >&2
        return 124
    fi
    [ "${statuses[0]}" -ne 0 ] && return "${statuses[0]}"
    return "${statuses[1]}"
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
