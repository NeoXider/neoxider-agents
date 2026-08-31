#!/usr/bin/env bash
# agent.sh — run CLI subagents (codex / claude / opencode / gemini, plus any provider
# plugin dropped into providers/<name>/) without interactive input.
# stdin is always closed (</dev/null) so the agent never hangs on a question; answer via `reply`.
#
# "One thread per task" model: every run creates <name>.log (full transcript) and <name>.meta
# (engine/model/dir/session/state/exit/files). All replies are APPENDED to the same <name>.log,
# so the whole conversation with the subagent reads as one file.
#
#   agent.sh run    [-e engine] [-m model] [-f effort] [-C dir] [-t name] [--no-progress] [--no-terse] "prompt"
#                      — new task. By default the agent keeps a PROGRESS.md checkpoint in its working
#                      dir (resumable after a crash; orchestrator-readable summary) and gets a concision
#                      directive to save output/turn tokens. --no-progress / --no-terse opt out of each.
#   agent.sh fan    [-e engine] [-m model] [-f effort] [-C dir] [-t base] "prompt1" "prompt2" ...
#                      — fan out: launch every prompt as its OWN parallel background `run` task
#                      (named <base>-01, -02, ...). Shared engine/model/effort/dir. Returns at once;
#                      poll with `list` / `status`. Replaces writing a bash loop of `run ... &` by hand.
#   agent.sh test-api --base-url <url> --goal "<what to verify>" [-e engine] [-m model]
#                      [-f effort] [-C dir] [-t name] [--out <path>]  — drive an agent to
#                      exercise a local HTTP API via its own shell/curl and report a
#                      structured JSON result (thin wrapper on `run`, tagged kind=api-test)
#   agent.sh reply  [-e engine] [-C dir] [name|session_id] "answer"      — continue a task/session
#   agent.sh log    [-f] [-n N] [-l] [name]                              — thread: -f follow, -n N lines, -l last step
#   agent.sh last   [name]                                               — only the agent's last reply
#   agent.sh status [name]                                               — state: state/step/changed files/needs reply?
#   agent.sh list                                                        — task table (state/engine/model/age/files)
#   agent.sh clean  [--all] [--purge] [-n]                               — delete md clutter (<name>.md +
#                      PROGRESS.<name>.md) of STOPPED tasks (done/error/stalled). --all also cleans
#                      waiting tasks; --purge also drops .log/.meta; -n dry-run. Live tasks
#                      (running AND idle) are never touched.
#   agent.sh doctor [--deep]                                             — pre-flight: engines + codex limits + claude usage (before fan-out).
#                      --deep additionally does one REAL cheap run per engine that supports it and
#                      makes it execute a shell command — the only check that catches "the model
#                      answers but every shell command hangs" (see AGENT_CODEX_USER_CONFIG below).
#   agent.sh provider-info <engine>                                      — single provider's doctor JSON (used by gui.py)
#   agent.sh gui [port] [--lan] [--token SECRET]                         — web control panel.
#                      Loopback-only by default. --lan binds every interface so another computer or
#                      a phone can drive it, and then --token (or $AGENT_GUI_TOKEN) is MANDATORY:
#                      the panel launches full-auto subagents, so an open LAN panel is remote code
#                      execution. Other devices open http://<this-host>:<port>/?token=<secret>.
#   agent.sh openai-server [-e engine] [-m model] [-f effort] [-p port] [--api-key SECRET]  — OpenAI-compatible
#                      /v1/chat/completions bridge over a CLI subagent (see openai_server.py's
#                      own docstring for the full contract/caveats). Point any OpenAI-compatible
#                      client -- including CoreAI's COREAI_TEST_BASE_URL -- at its base_url. Run
#                      several with different -e/-m/-f/-p to compare providers side by side.
#                      One process = one ongoing chat session (deterministic continuation via
#                      `reply`, not a fresh agent every call) -- `POST .../reset` clears it.
#
# Models (alias -> real):
#   codex:  5.6-terra|terra|default -> gpt-5.6-terra (effort medium) [DEFAULT]; sol -> gpt-5.6-sol;
#           high -> sol at effort high; luna -> gpt-5.6-luna; spark|5.3 -> gpt-5.3-codex-spark (very simple
#           tasks); anything else -> passed through as-is (e.g. -m gpt-5.5). (5.6 family needs codex-cli >= 0.144.)
#   claude: opus5|default -> claude-opus-5 [DEFAULT]; sonnet -> claude-sonnet-5, effort high;
#           opus|haiku -> same alias, effort as given (no suffix -> CLI default); <model>-<effort> is the general pattern
#   kimi: k3|default -> kimi-code/k3 [DEFAULT]; k3-256k/coding/highspeed are explicit alternatives
#   opencode/gemini: passed through as-is (-m provider/model)
#
# Environment:
#   AGENT_TIMEOUT_SEC=1800      wall-clock deadline for ONE step (run/reply). On expiry the whole
#                               process tree is killed, the log gets a "!! TIMEOUT" line and the
#                               task ends as state=error exit=124 — never a silent forever-hang.
#                               0 disables the watchdog (use for genuinely long jobs).
#   AGENT_STALE_SEC=300         after this much silence a still-alive task is reported as
#                               "running (no output for Nm)" instead of plain running (CLI + GUI).
#   AGENT_CODEX_USER_CONFIG=1   let codex load ~/.codex/config.toml again (default: NOT loaded —
#                               it hangs codex's tool router on machines with the ChatGPT desktop
#                               app; see providers/codex/provider.sh for the live repro).
#   AGENT_CODEX_MCP="a=url,b=url"  re-add specific MCP servers to the isolated codex config,
#                               e.g. AGENT_CODEX_MCP="unityMCP=http://127.0.0.1:8040/mcp".
#
# Providers are plugins: each providers/<name>/provider.sh defines provider_<name>_resolve,
# provider_<name>_run_cmd, provider_<name>_resume_cmd (optional), provider_<name>_doctor.
# Adding a new engine = adding one new providers/<name>/ directory, zero edits to this file.
if [ "${BASH_VERSINFO[0]:-0}" -lt 4 ]; then
    printf 'agent.sh: Bash 4 or newer is required (found %s)\n' "${BASH_VERSION:-unknown}" >&2
    exit 2
fi

set -uo pipefail

LOGDIR="${AGENT_CLI_LOGS:-$HOME/.claude/agent-cli-logs}"
( umask 077; mkdir -p "$LOGDIR" )
chmod 700 "$LOGDIR" 2>/dev/null || true
for _agent_state_file in "$LOGDIR"/*.log "$LOGDIR"/*.meta; do
    [ -f "$_agent_state_file" ] && chmod 600 "$_agent_state_file" 2>/dev/null || true
done
unset _agent_state_file

# State contains prompts, source and provider diagnostics, so create it owner-only without changing
# the umask inherited by provider processes (their working-tree files must keep the caller's umask).
_secure_state_file() { ( umask 077; : >> "$1" ) && chmod 600 "$1" 2>/dev/null; }
_secure_state_truncate() { ( umask 077; : > "$1" ) && chmod 600 "$1" 2>/dev/null; }

# Wall-clock deadline for ONE provider step, and the silence threshold after which a still-alive
# task is reported as "no output for Nm". Both are shared with gui.py (same names, same defaults),
# so the CLI and the web panel never disagree about what a task is doing.
AGENT_TIMEOUT_SEC="${AGENT_TIMEOUT_SEC:-1800}"
AGENT_STALE_SEC="${AGENT_STALE_SEC:-300}"
case "$AGENT_TIMEOUT_SEC" in ''|*[!0-9]*) AGENT_TIMEOUT_SEC=1800 ;; esac
case "$AGENT_STALE_SEC"   in ''|*[!0-9]*) AGENT_STALE_SEC=300   ;; esac

die() { echo "agent.sh: $*" >&2; exit 1; }
now() { date '+%Y-%m-%d %H:%M:%S'; }

# --- process control (git-bash safe) ---------------------------------------
# _winpid MSYS_PID -> the Windows pid of that process (empty on Linux/macOS, where there is none).
# MSYS2/git-bash exposes it as /proc/<pid>/winpid; it is what taskkill and python understand,
# while the msys pid is not. Recorded in .meta as winpid= so gui.py can check liveness of the very
# same process the CLI checks (see eff_state).
_winpid() { local p="${1:-}"; [ -n "$p" ] && [ -r "/proc/$p/winpid" ] && cat "/proc/$p/winpid" 2>/dev/null; return 0; }

# _child_pids PID -> msys pids whose PPID is PID (one per line).
_child_pids() {
    local p="${1:-}"
    [ -n "$p" ] || return 0
    # git-bash `ps` (no -o support) prints: PID PPID PGID WINPID TTY UID STIME COMMAND
    # GNU/BSD ps do support -o, which is both cheaper and unambiguous -- try that first.
    if ps -eo pid=,ppid= >/dev/null 2>&1; then
        ps -eo pid=,ppid= 2>/dev/null | awk -v p="$p" '$2==p{print $1}'
    else
        ps 2>/dev/null | awk -v p="$p" 'NR>1 && $2==p{print $1}'
    fi
}

# _kill_tree PID — kill PID and everything it spawned, on Windows too.
# WHY not just `kill` / coreutils `timeout`: on git-bash the interesting process is a NATIVE
# Windows grandchild (bash -> npm shim -> codex.exe/node.exe, which itself spawns powershell.exe).
# Signals to the msys pid do not reap that native tree, so `timeout 60 codex ...` returns while a
# codex.exe keeps running (and keeps holding the model session). taskkill /T on the WINDOWS pid is
# what actually walks the OS process tree; the msys-side recursion below handles the shell wrappers
# (and is the whole story on Linux/macOS, where there is no winpid).
_kill_tree() {
    local pid="${1:-}" k wp
    [ -n "$pid" ] || return 0
    for k in $(_child_pids "$pid"); do
        [ "$k" = "$pid" ] || _kill_tree "$k"
    done
    wp="$(_winpid "$pid")"
    if [ -n "$wp" ] && command -v taskkill >/dev/null 2>&1; then
        # //F //T -> /F /T: the doubled slash stops MSYS from mangling the flag into a path.
        taskkill //F //T //PID "$wp" >/dev/null 2>&1
    fi
    kill -TERM "$pid" 2>/dev/null
    kill -KILL "$pid" 2>/dev/null
    return 0
}

# _guarded_run SECS CMD... — run CMD (a shell function or a program) under a hard wall-clock
# deadline. CMD's stdout/stderr go to OUR stdout unchanged, so callers can keep piping into
# `tee -a "$log" | tail -40` exactly as before. Returns CMD's exit code, or 124 if the deadline
# expired -- in which case the whole process tree is killed and a "!! TIMEOUT" line is printed on
# the same stdout (so it lands in the task log AND in the tail the user sees).
# SECS=0 disables the deadline. Callers treat 124 as "killed by the watchdog".
#
# The deadline is measured against the CLOCK, never by counting loop iterations. Counting was the
# original implementation (`waited=$((waited+1))` per `sleep 1`) and it silently multiplied every
# timeout: one iteration is sleep(1) PLUS a `kill -0` and the fork for `sleep` itself, which under
# git-bash on Windows costs ~3.5s wall per pass. Measured live 2026-08-24: an opencode task with
# AGENT_TIMEOUT_SEC=1800 was still alive at 108 minutes (6480s / 1800 iterations = 3.6s per pass).
# The watchdog is the outer safety net for every engine, so a 3.6x-inflated net is worse than none:
# it reads as "protected" while a wedged task runs for hours.
_guarded_run() {
    local secs="${1:-0}"; shift
    case "$secs" in ''|*[!0-9]*) secs=0 ;; esac
    local child rc=0 deadline now
    ( "$@" ) 2>&1 &
    child=$!
    if [ "$secs" -gt 0 ]; then
        deadline=$(( $(date +%s) + secs ))
        while kill -0 "$child" 2>/dev/null; do
            now=$(date +%s)
            if [ "$now" -ge "$deadline" ]; then
                _kill_tree "$child"
                wait "$child" 2>/dev/null
                printf '\n!! TIMEOUT: step exceeded AGENT_TIMEOUT_SEC=%ss and was killed (process tree terminated)\n' "$secs"
                return 124
            fi
            sleep 1
        done
    fi
    wait "$child"; rc=$?
    return "$rc"
}

# minimal JSON string escaper shared by provider doctor functions (backslash, quote, control chars).
# NB: the backslash substitution MUST run first, before \t/\r/\n are introduced, otherwise those
# new backslashes would themselves get doubled by it (verified against real inputs including
# Windows paths with backslashes, embedded quotes, tabs and newlines — see git history for the
# throwaway test script used to confirm round-tripping through `python -c json.loads`).
_json_str() {
    local s="${1:-}"
    s="${s//\\/\\\\}"; s="${s//\"/\\\"}"
    s="${s//$'\t'/\\t}"; s="${s//$'\r'/}"; s="${s//$'\n'/\\n}"
    printf '"%s"' "$s"
}

# --- shared provider helpers -------------------------------------------------
# _agent_python — shared python discovery for all providers: $AGENT_PYTHON wins, then
# python3/python/py; a candidate counts only if it actually runs (`-c "import sys"` — Windows'
# bare `python` can be an exit-nonzero stub). Sets $_AGENT_PY on success; non-zero return means
# none found (providers degrade to raw passthrough). Cached for the process lifetime.
_AGENT_PY=""
_AGENT_PY_RESOLVED=""
_agent_python() {
    if [ -z "$_AGENT_PY_RESOLVED" ]; then
        _AGENT_PY_RESOLVED=1
        local c
        for c in "${AGENT_PYTHON:-}" python3 python py; do
            [ -n "$c" ] || continue
            if command -v "$c" >/dev/null 2>&1 && "$c" -c "import sys" >/dev/null 2>&1; then
                _AGENT_PY="$c"
                break
            fi
        done
    fi
    [ -n "$_AGENT_PY" ]
}

# _windowless_python -- Windows-only sibling of _agent_python: prefer an interpreter that owns no console
# (pythonw.exe) so a long-lived server (`gui`, `openai-server`) never parks in a visible command-line window.
# Sets $_WINDOWLESS_PY on success; fails silently elsewhere, letting callers fall back to _agent_python.
_WINDOWLESS_PY=""
_WINDOWLESS_PY_RESOLVED=""
_windowless_python() {
    if [ -z "$_WINDOWLESS_PY_RESOLVED" ]; then
        _WINDOWLESS_PY_RESOLVED=1
        case "$(uname -s 2>/dev/null || true)" in MINGW*|MSYS*) ;; *) return 1 ;; esac
        local c
        for c in "${AGENT_PYTHON_W:-}" pythonw; do
            [ -n "$c" ] || continue
            if command -v "$c" >/dev/null 2>&1 && "$c" -c "import sys" >/dev/null 2>&1; then
                _WINDOWLESS_PY="$c"
                break
            fi
        done
    fi
    [ -n "$_WINDOWLESS_PY" ]
}

# --- provider plugin loader ------------------------------------------------
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROVIDERS_DIR="$HERE/providers"
for _p in "$PROVIDERS_DIR"/*/provider.sh; do
    [ -e "$_p" ] || continue
    # shellcheck disable=SC1090
    source "$_p"
done
unset _p

cmd="${1:-}"
[ -n "$cmd" ] || die "usage: agent.sh run|fan|reply|log|last|wait|status|list|clean|doctor|provider-info|gui|openai-server|help ... (run 'agent.sh help' for the full reference)"
shift

engine="claude"; engine_explicit=0; model=""; model_explicit=0; effort_override=""; effort_explicit=0; dir="$(pwd)"; name="task-$(date +%Y%m%d-%H%M%S)-$$"; progress=1; terse=1
# ^ engine=claude by default -> Opus 5 (see providers/claude). Pass -e codex for the gpt-5.6 family.
# ^ progress=1 by default: every task keeps a PROGRESS.md checkpoint (resumable after a crash,
# and an orchestrator can read the summary without re-running the agent). Disable with --no-progress.
# ^ terse=1 by default: append a concision directive (short output, minimal reasoning narration, assume-
# and-note instead of asking, no re-reading/over-exploring) to save output+turn tokens. Off: --no-terse.
# ^ PID suffix makes the default name collision-resistant: two processes (e.g. from two
# different installs/tools sharing one LOGDIR) can never share a PID, so they never race on
# the same .meta/.log even if they start in the same second. Always give tasks a meaningful
# name via -t anyway -- this default just needs to be *safe*, not pretty.
parent="${AGENT_PARENT:-}"   # parent task name (for the tree); can be set via env or -P
task_kind=""; base_url=""; test_goal=""; out_file=""   # test-api only (see that subcommand)

parse_opts() {
    while [ $# -gt 0 ]; do
        case "$1" in
            -e|-m|-f|-C|-t|-P|--base-url|--goal|--out)
                [ $# -ge 2 ] || die "$cmd: option '$1' needs an operand"
                case "$2" in -*) die "$cmd: option '$1' needs an operand (got option '$2')" ;; esac
                case "$1" in
                    -e) engine="$2"; engine_explicit=1 ;;
                    -m) model="$2"; model_explicit=1 ;;
                    -f) effort_override="$2"; effort_explicit=1 ;;
                    -C) dir="$2" ;;
                    -t) name="$2" ;;
                    -P) parent="$2" ;;
                    --base-url) base_url="$2" ;;
                    --goal) test_goal="$2" ;;
                    --out) out_file="$2" ;;
                esac
                shift 2 ;;
            -p) progress=1; shift ;;                 # kept for compat; progress is on by default
            --no-progress) progress=0; shift ;;      # opt out of the PROGRESS.md checkpoint
            --no-terse|--verbose) terse=0; shift ;;  # opt out of the concision directive
            *) break ;;
        esac
    done
    REST=("$@")
}

# PROGRESS checkpoint protocol: each task keeps its OWN file, NAMED BY TASK (PROGRESS.<task>.md), so
# several agents sharing one working directory never clobber each other's progress and resume stays
# per-task. $1 = task name. Emitting per-task avoids the "everyone writes one PROGRESS.md" collision.
progress_proto() {
    printf '%s' "

[Progress protocol] Maintain a Markdown checkpoint file named EXACTLY PROGRESS.$1.md in the working
directory — one file PER TASK, never a shared PROGRESS.md, so parallel agents in the same directory
never collide. It is a durable record: this task can be resumed after any interruption, and an
orchestrator can read what you did and concluded WITHOUT re-running you.
If PROGRESS.$1.md already exists, READ IT FIRST and continue where it left off — do not redo finished
steps. Keep it current with these sections:
  1. Summary (TL;DR) — 2-4 lines: goal, current status, headline result/conclusion.
  2. Checklist — the steps, each marked [x] done / [ ] todo / [~] in-progress.
  3. Log — one short entry per meaningful step: what you did, the outcome, and any finding, error, or
     decision (key file paths, commands run, error messages verbatim).
  4. Conclusions / next steps.
Update it BEFORE and AFTER each significant step, not only at the end. Keep it concise. Do NOT git commit."
}

# reply: the agent already has the full protocol (turn 1 + its own file), so a one-line reminder is
# enough — avoids re-sending ~250 tokens of boilerplate every follow-up. $1 = task name.
progress_proto_reply() {
    printf '%s' "

[Progress] Keep PROGRESS.$1.md current as you continue (summary, checklist, log, conclusions)."
}

# Concision directive (default on): trims OUTPUT and TURN tokens. It does NOT cut hidden reasoning
# tokens — that is the effort flag (-f low / -m spark). Appended once on the first turn only; the
# model keeps it in session context on later replies, so re-sending it every reply would just be
# boilerplate. Balanced on purpose: the agent may still ask when genuinely blocked, so default-on
# does not push it into confidently-wrong guesses on ambiguous tasks.
TERSE_PROTO='

[Style] Work token-efficiently: keep output, explanations, and reasoning narration minimal; do not
restate the task or your plan back at length; give only what is needed. Do not re-read files you have
already seen or explore beyond the task. If something is ambiguous, make a reasonable assumption and
note it in one line rather than stopping — ask only if you are truly blocked and cannot proceed safely.'

# --- meta sidecar (key=value) ---------------------------------------------
# meta_set's read-modify-write isn't atomic across processes on its own, so we wrap it in a
# portable mkdir-based mutex (mkdir is atomic on every POSIX filesystem; no dependency on
# flock, which isn't reliably available in git-bash). This matters once this tool is invoked
# concurrently from multiple agents/providers/installs sharing the same LOGDIR — without it,
# two near-simultaneous writers to the same task's .meta could silently clobber each other's update.
valid_task_name() {
    local n="${1:-}"
    [ -n "$n" ] && [ ${#n} -le 128 ] || return 1
    case "$n" in
        [A-Za-z0-9]*) ;;
        *) return 1 ;;
    esac
    case "$n" in
        *[!A-Za-z0-9._-]*) return 1 ;;
    esac
    return 0
}
require_task_name() {
    valid_task_name "${1:-}" || die "invalid task name '${1:-}': use 1-128 ASCII letters/digits followed by letters, digits, '.', '_' or '-'"
}
meta_file() { valid_task_name "${1:-}" || return 1; printf '%s/%s.meta\n' "$LOGDIR" "$1"; }
_meta_prune_nested_candidates() {
    local lock="$1" nested="" state=""
    for nested in "$lock/${lock##*/}.candidate."*; do
        [ -d "$nested" ] || continue
        # A nested candidate is necessarily a lost two-operand-mv race: only a top-level owner can
        # hold the lock. Once no top-level owner exists, removing it is safe even if its creator is
        # paused and alive; that creator will observe no ownership, clean up, and retry.
        for state in "$nested"/owner.*; do
            [ -e "$state" ] || continue
            rm -- "$state" 2>/dev/null
        done
        rmdir "$nested" 2>/dev/null
    done
}
_meta_lock() {
    local lock="$1.lock.d" token owner_pid="${BASHPID:-$$}" candidate="" nested=""
    local state="" owner_state="" state_pid="" state_token="" state_mt=0 state_age=0
    local found=0
    token="$owner_pid-${RANDOM:-0}-$(date +%s)"
    while :; do
        # Prepare the complete immutable generation out of sight. Plain two-operand `mv` is the
        # portable Bash 3 / POSIX primitive available in Git Bash and macOS: absent destination
        # publishes by rename; an existing directory receives the candidate as a nested child.
        # The top-level owner check below distinguishes those outcomes without mv -T/flock.
        if [ ! -e "$lock" ]; then
            candidate="$lock.candidate.$token.${RANDOM:-0}"
            owner_state="$candidate/owner.$token"
            if ( umask 077; mkdir "$candidate" \
                 && printf '%s %s\n' "$owner_pid" "$token" > "$owner_state" ) 2>/dev/null; then
                if declare -F _meta_lock_before_publish_hook >/dev/null 2>&1; then
                    _meta_lock_before_publish_hook "$candidate" "$lock"
                fi
                if mv "$candidate" "$lock" 2>/dev/null; then
                    state="$lock/owner.$token"
                    state_pid=""; state_token=""
                    if { read -r state_pid state_token < "$state"; } 2>/dev/null \
                        && [ "$state_pid" = "$owner_pid" ] && [ "$state_token" = "$token" ]; then
                        _META_LOCK_TOKEN="$token"
                        _META_LOCK_STATE="$state"
                        return 0
                    fi
                    nested="$lock/${candidate##*/}"
                    if [ -d "$nested" ] \
                        && declare -F _meta_lock_after_nested_publish_hook >/dev/null 2>&1; then
                        _meta_lock_after_nested_publish_hook "$nested" "$lock"
                    fi
                fi
            fi
            # A lost publication race may have moved our candidate inside the winner. Both cleanup
            # paths name only our unique token; rmdir of the primary succeeds only when truly empty.
            rm -f -- "$owner_state" 2>/dev/null
            rmdir "$candidate" 2>/dev/null
            nested="$lock/${candidate##*/}"
            rm -f -- "$nested/owner.$token" 2>/dev/null
            rmdir "$nested" 2>/dev/null
            rmdir "$lock" 2>/dev/null
        fi

        state=""; found=0
        for candidate in "$lock"/owner "$lock"/owner.*; do
            [ -r "$candidate" ] || continue
            found=$((found + 1))
            state="$candidate"
        done
        if [ "$found" -gt 1 ]; then
            sleep 0.1
            continue
        fi
        if [ "$found" = 0 ]; then
            if [ -d "$lock" ]; then
                _meta_prune_nested_candidates "$lock"
                rmdir "$lock" 2>/dev/null
            fi
            sleep 0.1
            continue
        fi
        if [ "$found" = 1 ]; then
            state_pid=""; state_token=""
            if { read -r state_pid state_token < "$state"; } 2>/dev/null; then
                case "$state_pid" in
                    ''|*[!0-9]*) ;;
                    *) kill -0 "$state_pid" 2>/dev/null && { sleep 0.1; continue; } ;;
                esac
            fi
            state_mt="$(file_mtime "$state")"
        fi
        case "$state_mt" in ''|*[!0-9]*) state_mt=0 ;; esac
        state_age=$(( $(date +%s) - state_mt ))
        if [ "$state_age" -ge 10 ]; then
            if [ "$found" = 1 ]; then
                # Published generations are immutable. Removing their unique owner path is the
                # compare-and-delete token; it can never name a later generation.
                if rm -- "$state" 2>/dev/null; then rmdir "$lock" 2>/dev/null; fi
            fi
        fi
        sleep 0.1
    done
}
_meta_unlock() {
    local lock="$1.lock.d" state="${_META_LOCK_STATE:-}" owner_pid="" owner_token=""
    [ -n "$state" ] && [ -r "$state" ] || return 0
    read -r owner_pid owner_token < "$state" || return 0
    [ "$owner_token" = "${_META_LOCK_TOKEN:-}" ] || return 0
    rm -f -- "$state"
    rmdir "$lock" 2>/dev/null
}
meta_set()  { local f tmp key="${2:-}"; f="$(meta_file "${1:-}")" || return 1
    case "$key" in ''|*[!A-Za-z0-9_-]*) return 1 ;; esac
    _meta_lock "$f" || return 1
    tmp="$f.tmp.$BASHPID.${RANDOM:-0}"
    if ( umask 077
         { [ ! -e "$f" ] || grep -v "^$key=" "$f" || true; } > "$tmp" 2>/dev/null \
             && printf '%s=%s\n' "$key" "${3:-}" >> "$tmp"
       ) \
        && mv "$tmp" "$f"; then
        chmod 600 "$f" 2>/dev/null || true
        _meta_unlock "$f"
        return 0
    fi
    rm -f -- "$tmp"
    _meta_unlock "$f"
    return 1
}
meta_get()  { local f; f="$(meta_file "${1:-}")" || return 1; grep -m1 "^${2:-}=" "$f" 2>/dev/null | cut -d= -f2- ; }

resolve_session() {
    local n="$1" s; s="$(meta_get "$n" session)"; [ -n "$s" ] && { echo "$s"; return; }
    grep -m1 -oE 'session id: [[:alnum:]_.-]+' "$LOGDIR/$n.log" 2>/dev/null | cut -d' ' -f3
}
name_by_session() { local s="$1" f
    for f in "$LOGDIR"/*.meta; do [ -e "$f" ] || continue
        if grep -Fqx "session=$s" "$f"; then
            local n; n="$(basename "$f" .meta)"; valid_task_name "$n" && { printf '%s\n' "$n"; return; }
        fi
    done
}
_latest_file() {
    local suffix="$1" f mt best="" best_mt=-1
    for f in "$LOGDIR"/*."$suffix"; do
        [ -f "$f" ] || continue
        mt="$(file_mtime "$f")"
        case "$mt" in ''|*[!0-9]*) mt=0 ;; esac
        if [ "$mt" -gt "$best_mt" ]; then best="$f"; best_mt="$mt"; fi
    done
    printf '%s' "$best"
}
_sorted_meta_files() {
    local limit="${1:-20}" f i j best_idx best_mt mt
    case "$limit" in ''|*[!0-9]*) return 1 ;; esac
    _SORTED_META_FILES=("$LOGDIR"/*.meta)
    [ -e "${_SORTED_META_FILES[0]}" ] || { _SORTED_META_FILES=(); return 0; }
    for ((i=0; i<${#_SORTED_META_FILES[@]}; i++)); do
        best_idx=$i; best_mt=-1
        for ((j=i; j<${#_SORTED_META_FILES[@]}; j++)); do
            mt="$(file_mtime "${_SORTED_META_FILES[j]}")"; case "$mt" in ''|*[!0-9]*) mt=0 ;; esac
            if [ "$mt" -gt "$best_mt" ]; then best_idx=$j; best_mt=$mt; fi
        done
        f="${_SORTED_META_FILES[i]}"; _SORTED_META_FILES[i]="${_SORTED_META_FILES[best_idx]}"; _SORTED_META_FILES[best_idx]="$f"
        [ "$((i + 1))" -ge "$limit" ] && { _SORTED_META_FILES=("${_SORTED_META_FILES[@]:0:limit}"); break; }
    done
    return 0
}
latest_task() { local f; f="$(_latest_file meta)"; [ -n "$f" ] && basename "$f" .meta; }

is_alive() { [ -n "${1:-}" ] && kill -0 "$1" 2>/dev/null; }

# file_mtime PATH -> mtime as epoch seconds (empty if missing/unreadable).
# GNU stat (-c %Y) first, BSD/macOS stat (-f %m) as fallback.
file_mtime() {
    local m
    m="$(stat -c %Y "$1" 2>/dev/null)" && { printf '%s\n' "$m"; return 0; }
    stat -f %m "$1" 2>/dev/null
}

# log_idle_sec NAME -> seconds since the task's log last grew (empty if there is no log).
log_idle_sec() {
    local f="$LOGDIR/$1.log" m
    [ -f "$f" ] || return 0
    m="$(file_mtime "$f")"
    [ -n "$m" ] || return 0
    echo $(( $(date +%s) - m ))
}

# eff_state NAME -> the ONE state machine, mirrored verbatim in gui.py's eff_state():
#   not running        -> whatever meta says (done/waiting/error/…)
#   running, pid dead  -> stalled   (machine rebooted, process killed)
#   running, pid alive, silent for > AGENT_STALE_SEC -> idle
#   running, pid alive -> running
#   running, pid UNKNOWN (old .meta without pid=) + silent -> stalled (the pre-existing GUI rule)
# WHY `idle` exists: the CLI used to look only at the pid ("alive -> running") and the GUI only at
# the log's mtime ("quiet 5m -> stalled"), so the same task was reported as running by one and
# stalled by the other. Both are half-truths: a codex step writes its log only when it FINISHES
# (output is piped through a buffering parser), so every honest 10-minute task looked stalled in
# the GUI. `idle` = "the process is alive, it just has not produced output for Nm" -- rendered the
# same way on both sides (see state_label / util.js stateLabel).
eff_state() {
    local n="$1" st pid idle; st="$(meta_get "$n" state)"
    [ "$st" = running ] || { echo "$st"; return; }
    pid="$(meta_get "$n" pid)"
    if [ -n "$pid" ] && ! is_alive "$pid"; then echo stalled; return; fi
    idle="$(log_idle_sec "$n")"
    if [ -n "$idle" ] && [ "$idle" -gt "$AGENT_STALE_SEC" ]; then
        if [ -z "$pid" ]; then echo stalled; else echo idle; fi
    else
        echo running
    fi
}
# state_label STATE [IDLE_SEC] -> the human-readable form (identical wording in gui.py/util.js).
state_label() {
    case "$1" in
        idle) printf 'running (no output for %dm)' "$(( ${2:-0} / 60 ))" ;;
        *)    printf '%s' "$1" ;;
    esac
}
state_icon() { case "$1" in running) echo "▶";; idle) echo "▷";; done) echo "✔";; waiting) echo "⏳";; error) echo "✖";; stalled) echo "⚠";; *) echo "•";; esac; }

hdr() { # kind "info" LABEL "text" logfile
    { echo; echo "========== [$1] $(now) | $2 =========="; echo "> $3:"; echo "$4";
      echo "---------- output ----------"; } >> "$5"; }

# last output block (after the last separator)
last_output() { awk '/^---------- output ----------$/{buf=""; next}{buf=buf $0 ORS} END{printf "%s", buf}' "$1"; }

# durable md checkpoint of the task: header from meta + the whole thread in markdown.
# Survives a shutdown; the task can be resumed from it (or from the codex/claude session).
render_md() {
    local n="$1" log="$LOGDIR/$1.log" md="$LOGDIR/$1.md" st reason; st="$(eff_state "$n")"
    reason="$(meta_get "$n" reason)"
    {
        echo "# Subagent task: $n"
        echo
        echo "- **State:** $st"
        echo "- **Engine:** $(meta_get "$n" engine) / $(meta_get "$n" model)"
        echo "- **Dir:** \`$(meta_get "$n" dir)\`"
        echo "- **Session:** \`$(meta_get "$n" session)\`"
        echo "- **Exit:** $(meta_get "$n" exit)  **Changed files:** $(meta_get "$n" files)"
        [ "$st" = error ] && echo "- **Failure:** Turn died; work may or may not have landed. Inspect the working tree. Reason: ${reason:-provider failure}"
        echo "- **Started:** $(meta_get "$n" started)  **Updated:** $(now)"
        echo "- **Resume:** \`agent.sh reply $n \"...\"\`  |  **Log:** \`agent.sh log $n\`"
        awk '
            function closeout(){ if(inout){ print "```"; inout=0 } }
            /^========== \[/ {
                closeout(); line=$0
                sub(/^========== \[/,"",line); k=line; sub(/\].*/,"",k)
                t=line; sub(/^[^]]*\] /,"",t); sub(/ \|.*/,"",t)
                printf "\n## %s — %s\n", toupper(k), t; next }
            /^> PROMPT:$/ { print "\n**Prompt:**\n"; next }
            /^> ANSWER:$/ { print "\n**Reply:**\n"; next }
            /^---------- output ----------$/ { print "\n**Output:**\n"; print "```text"; inout=1; next }
            { print }
            END { closeout() }
        ' "$log"
    } > "$md"
}

# looks_waiting CLOSING_WINDOW — true if the step's closing output asks for input (-> state=waiting).
# CLOSING_WINDOW is the last few non-empty lines of the final output block (finish_step supplies
# them): a genuine question followed by a bookkeeping footer ("PROGRESS.x.md updated.") used to
# read as a clean done because only the very last line was classified — so EVERY line in this small
# window goes through _looks_waiting_line.
# Per line (after stripping trailing quotes/brackets and a `(yes/no)` aside):
#   * real (alphanumeric) text before a trailing '?' — bare '?'/punctuation-only never counts;
#   * an EXPLICIT ask: self-directed should/shall-I, do-you-want, please confirm/clarify/specify,
#     Russian уточни/подтверд/как поступить — or a which-option phrase ONLY when it leads into a
#     '?', so declarative prose stays declarative: "...documents which options the CLI supports"
#     and soft sign-offs like "let me know if you need anything else." do NOT flip state=waiting.
looks_waiting() {
    local window="${1:-}" line
    while IFS= read -r line; do
        [ -n "$line" ] || continue
        _looks_waiting_line "$line" && return 0
    done <<< "$window"
    return 1
}

_looks_waiting_line() {
    local body="${1:-}" last
    body="${body%$'\r'}"
    body="${body% \(*\)}"
    while [ -n "$body" ]; do
        last="${body#"${body%?}"}"   # POSIX-safe last char (no ${var: -1}, for bash 3/macOS)
        case "$last" in
            " "|$'\t'|'"'|"'"|")"|"]"|"}") body="${body%?}" ;;
            *) break ;;
        esac
    done
    case "$body" in
        *\?)
            local q="${body%\?}"
            if _agent_python; then
                printf '%s' "$q" | PYTHONIOENCODING=utf-8 "$_AGENT_PY" -c \
                    'import sys; raise SystemExit(0 if any(c.isalnum() for c in sys.stdin.read()) else 1)' \
                    && return 0
            elif [ -n "$(printf '%s' "$q" | tr -cd '[:alnum:]')" ]; then
                return 0
            fi
            ;;
    esac
    printf '%s' "$body" | grep -qiE \
        'should i |shall i |do you want|please (confirm|clarify|specify)|which (one|option|approach|of)[^.?!]*\?|уточни|подтверд|как (мне |)поступ|какой из[^.?!]*\?' \
        && return 0
    return 1
}

# --- transient-failure retry ------------------------------------------------
# Провайдеры регулярно рвут поток на середине шага, причём ошибка сама себя
# помечает повторяемой: opencode отдаёт {"isRetryable":true,...,"message":
# "Provider finish_reason: network_error"}, codex обрывает SSE. Повторов не
# было вовсе, поэтому один сетевой сбой стоил агенту всей работы — за день так
# умерли два аудита подряд и агент по вакансиям, каждый раз не успев записать
# отчёт. Частичный вывод при этом остаётся в логе: tee пишет по ходу.
#
# Что НЕ повторяем: rc=124 (собственный watchdog — повтор упрётся в тот же
# лимит), неверный ключ, отсутствующую модель и исчерпанную квоту. Это не
# рассосётся само, а повтор сожжёт время и лимиты.
AGENT_RETRIES="${AGENT_RETRIES:-2}"
case "$AGENT_RETRIES" in ''|*[!0-9]*) AGENT_RETRIES=2 ;; esac
AGENT_RETRY_DELAY="${AGENT_RETRY_DELAY:-8}"
case "$AGENT_RETRY_DELAY" in ''|*[!0-9]*) AGENT_RETRY_DELAY=8 ;; esac

# A Codex thread-writer conflict is not reliably reflected by process liveness: the process may
# already be gone while the thread store still owns its lock. Keep this signature shared by
# finish_step (last-resort detection) and the Codex provider's one automatic resume retry.
_has_thread_writer_conflict() {
    grep -qiE 'thread-store conflict|already has an active writer|code[[:space:]]+-32600'
}

# NB: the two patterns below deliberately spell word boundaries out as (^|[^...]) groups instead
# of \b: an earlier revision carried LITERAL 0x08 backspace bytes where \b was meant (they survive
# copy/paste and every editor, and silently broke the classifier). [[:<:]]/[[:>:]] would be the
# tidy spelling but are GNU-only, so the explicit character-class form is the portable one.
_is_transient_failure() {
    local log="$1" tailtxt
    tailtxt="$(tail -c 4000 "$log" 2>/dev/null)"
    printf '%s' "$tailtxt" | grep -qiE 'unauthorized|invalid api key|authentication|forbidden|(^|[^_[:alnum:]])model .*(not found|unavailable|unknown)|insufficient (quota|credit)|quota exceeded|payment required' && return 1
    printf '%s' "$tailtxt" | grep -qiE '"isretryable"[[:space:]]*:[[:space:]]*true|network_error|providerresponsestreamerror|stream (error|closed|interrupted)|econnreset|etimedout|enotfound|socket hang up|(^|[^_0-9a-zA-Z])(429|500|502|503|504)($|[^_0-9a-zA-Z])|overloaded|temporarily unavailable|rate.?limit'
}

# Совет по восстановлению зависит от движка: у opencode и gemini возобновления
# сессии нет (supports_resume=false), и предлагать им `reply` — значит посылать
# человека в тупик. Обёртка так и делала после таймаута opencode-задачи.
_recovery_hint() {
    local n="$1" eng
    eng="$(meta_get "$n" engine)"
    if declare -F "provider_${eng}_resume_cmd" >/dev/null 2>&1; then
        printf 'продолжить: agent.sh reply %s "continue"' "$n"
    else
        printf 'движок %s не умеет возобновлять сессию — запустить заново: agent.sh run -t %s ...' "$eng" "$n"
    fi
}

# after a step finishes: exit code, changed files, state, question detection
finish_step() {
    local n="$1" rc="$2" log tdir nfiles=0 closing output nonblank reason=""
    log="$LOGDIR/$n.log"; tdir="$(meta_get "$n" dir)"; [ -n "$tdir" ] || tdir="$dir"
    output="$(last_output "$log")"
    nonblank="$(printf '%s' "$output" | tr -d '[:space:]')"
    if printf '%s' "$output" | _has_thread_writer_conflict; then
        [ "$rc" -ne 0 ] || rc=3
        reason="Codex thread writer conflict after bounded wait/retry; inspect the working tree (changes may have landed)"
    elif [ -z "$nonblank" ]; then
        [ "$rc" -ne 0 ] || rc=3
        reason="provider returned an empty answer; inspect the working tree (changes may have landed)"
    elif [ "$rc" = 124 ]; then
        reason="step watchdog timeout; inspect the working tree (changes may have landed)"
    elif [ "$rc" -ne 0 ]; then
        reason="provider exited $rc; inspect the working tree (changes may have landed)"
    fi
    meta_set "$n" exit "$rc"
    meta_set "$n" reason "$reason"
    if git -C "$tdir" rev-parse --git-dir >/dev/null 2>&1; then
        nfiles=$(git -C "$tdir" status --porcelain 2>/dev/null | grep -c .)
    fi
    meta_set "$n" files "$nfiles"
    # Closing window, not just the last line: the last few non-empty lines of the final output
    # block. A question followed by a footer ("PROGRESS.x.md updated.") used to read as done;
    # looks_waiting classifies every line in this window so the question is still caught.
    closing="$(printf '%s' "$output" | grep -v '^[[:space:]]*$' | tail -6)"
    if [ "$rc" = 124 ]; then
        # killed by the step watchdog (_guarded_run). Recorded in meta so `status`/`list`/the GUI
        # can say WHY the task died instead of showing a bare exit code.
        meta_set "$n" state error
        meta_set "$n" timeout "$AGENT_TIMEOUT_SEC"
        echo "[agent.sh] ⏱ TIMEOUT after ${AGENT_TIMEOUT_SEC}s — task=$n killed (raise AGENT_TIMEOUT_SEC or $(_recovery_hint "$n"))" >&2
    elif [ "$rc" -ne 0 ]; then
        meta_set "$n" state error
        echo "[agent.sh] ✖ error exit=$rc  task=$n  reason=$reason  (the turn died; work may have landed — inspect the working tree | log: agent.sh log $n | $(_recovery_hint "$n"))" >&2
    elif looks_waiting "$closing"; then
        meta_set "$n" state waiting
        echo "[agent.sh] ⏳ the agent appears to have ASKED a question — reply: agent.sh reply $n \"...\"  (question: agent.sh last $n)" >&2
    else
        meta_set "$n" state done
        echo "[agent.sh] ✔ done  task=$n  files=$nfiles  (log: agent.sh log $n | result: agent.sh last $n)" >&2
    fi
    render_md "$n"
    return "$rc"
}

# --- generic provider dispatch ---------------------------------------------
# provider_dispatch_run ENGINE MODEL_ALIAS DIR PROMPT NAME -> sets rc, writes to $log via tee
provider_dispatch_run() {
    local eng="$1" alias="$2" d="$3" prompt="$4" n="$5" fn="provider_${1}_run_cmd"
    declare -F "$fn" >/dev/null 2>&1 || die "unknown engine: $eng"
    local resolve_fn="provider_${eng}_resolve"
    P_MODEL=""; P_EFFORT=""
    if declare -F "$resolve_fn" >/dev/null 2>&1; then
        "$resolve_fn" "$alias"
    else
        P_MODEL="$alias"
    fi
    # -f/effort_override is a genuinely separate "model" and "effort" input (e.g. from the GUI's
    # two-dropdown picker) rather than a suffix baked into the alias string (e.g. "sonnet-high").
    # It wins over whatever the alias/resolve function derived, and is the ONLY way to set effort
    # for providers with no _resolve function at all (opencode, gemini) -- those get P_MODEL="$alias"
    # verbatim above with P_EFFORT always empty, since there's no suffix parsing to find it in.
    [ -n "$effort_override" ] && P_EFFORT="$effort_override"
    if [ -n "$P_MODEL" ]; then
        meta_set "$n" model "$P_MODEL${P_EFFORT:+-$P_EFFORT}"  # resolved model, not the raw alias
        meta_set "$n" resolved_model "$P_MODEL"
        meta_set "$n" effort "$P_EFFORT"
    fi
    local _try=0 attempt_log attempt_sid="" worktree_before="" worktree_after=""
    if git -C "$d" rev-parse --git-dir >/dev/null 2>&1; then
        worktree_before="$(git -C "$d" status --porcelain 2>/dev/null)"
    fi
    while : ; do
        attempt_log="$(mktemp "$LOGDIR/.agent-attempt.XXXXXX" 2>/dev/null)"
        if [ -z "$attempt_log" ] || [ ! -f "$attempt_log" ]; then
            printf 'agent.sh: cannot create retry attempt log in %s\n' "$LOGDIR" | tee -a "$log" >&2
            rc=1
            break
        fi
        _guarded_run "$AGENT_TIMEOUT_SEC" "$fn" "$d" "$P_MODEL" "$P_EFFORT" "$prompt" 2>&1 \
            | tee -a "$log" "$attempt_log" | tail -40
        rc=${PIPESTATUS[0]}
        attempt_sid="$(grep -m1 -oE 'session id: [[:alnum:]_.-]+' "$attempt_log" 2>/dev/null | cut -d' ' -f3)"
        if [ "$rc" = 0 ] && [ -n "$attempt_sid" ]; then
            meta_set "$n" session "$attempt_sid"
        fi
        { [ "$rc" = 0 ] || [ "$rc" = 124 ]; } && break
        [ "$_try" -ge "$AGENT_RETRIES" ] && break
        _is_transient_failure "$attempt_log" || break
        # A provider session means the failed attempt may already have executed tools. Starting a
        # fresh retry could duplicate writes or external side effects; leave that session resumable.
        if [ -n "$attempt_sid" ]; then
            meta_set "$n" session "$attempt_sid"
            break
        fi
        if git -C "$d" rev-parse --git-dir >/dev/null 2>&1; then
            worktree_after="$(git -C "$d" status --porcelain 2>/dev/null)"
            [ "$worktree_after" = "$worktree_before" ] || break
        fi
        _try=$((_try+1))
        echo "[agent.sh] ↻ временный сбой провайдера (exit=$rc) — повтор $_try/$AGENT_RETRIES через ${AGENT_RETRY_DELAY}s  task=$n" >&2
        printf '
--- RETRY %s/%s after transient provider failure ---
' "$_try" "$AGENT_RETRIES" >> "$log"
        sleep "$AGENT_RETRY_DELAY"
        rm -f -- "$attempt_log"
    done
    [ -n "${attempt_log:-}" ] && rm -f -- "$attempt_log"
    [ "$_try" -gt 0 ] && meta_set "$n" retries "$_try"   # 124 = killed by the step watchdog (see _guarded_run / finish_step)
}

provider_dispatch_resume() {
    local eng="$1" d="$2" session="$3" answer="$4" n="$5" fn="provider_${1}_resume_cmd"
    # Distinguish the two failures the old single message conflated: an engine nobody implements vs
    # an engine that exists but has no resume path (supports_resume=false, e.g. opencode/gemini).
    if ! declare -F "$fn" >/dev/null 2>&1; then
        declare -F "provider_${eng}_run_cmd" >/dev/null 2>&1 \
            && die "engine '$eng' does not support resuming a session (supports_resume=false) — use 'run' to start a new task"
        die "unknown engine: $eng"
    fi
    local needs_var="PROVIDER_${eng^^}_RESUME_NEEDS_MODEL" resolve_fn="provider_${eng}_resolve"
    P_MODEL=""; P_EFFORT=""
    if [ "${!needs_var:-0}" = 1 ] && declare -F "$resolve_fn" >/dev/null 2>&1; then
        if [ "$model_explicit" = 1 ]; then
            "$resolve_fn" "$model"
        else
            P_MODEL="$(meta_get "$n" resolved_model)"
            P_EFFORT="$(meta_get "$n" effort)"
            if [ -z "$P_MODEL" ]; then
                local legacy_model
                legacy_model="$(meta_get "$n" model)"
                # Older Codex metadata stored one display string, e.g.
                # model=gpt-5.6-terra-medium. Split only the known effort suffixes; a raw model id
                # without one still goes through the provider resolver unchanged.
                if [ "$eng" = codex ]; then
                    case "$legacy_model" in
                        *-low|*-medium|*-high|*-xhigh|*-max)
                            P_EFFORT="${legacy_model##*-}"
                            P_MODEL="${legacy_model%-*}"
                            ;;
                        *) "$resolve_fn" "$legacy_model" ;;
                    esac
                else
                    "$resolve_fn" "$legacy_model"
                fi
            fi
        fi
        [ "$effort_explicit" = 1 ] && P_EFFORT="$effort_override"
        if [ -n "$P_MODEL" ]; then
            meta_set "$n" model "$P_MODEL${P_EFFORT:+-$P_EFFORT}"  # resolved model, not the raw alias
            meta_set "$n" resolved_model "$P_MODEL"
            meta_set "$n" effort "$P_EFFORT"
        fi
    fi
    _guarded_run "$AGENT_TIMEOUT_SEC" "$fn" "$d" "$session" "$answer" 2>&1 | tee -a "$log" | tail -40
    rc=${PIPESTATUS[0]}   # 124 = killed by the step watchdog (see _guarded_run / finish_step)
}

# _require_engine_run ENGINE — die if no provider implements this engine's run command. This MUST
# happen before _do_run_dispatch touches the filesystem: provider_dispatch_run would die too, but
# only after state=running and pid= were already written, leaving a ghost task that reads `stalled`
# forever. Worse, under `fan` each dispatch runs in a background subshell with output discarded, so
# `agent.sh fan -e typo ...` reported "launched N parallel task(s)" while starting nothing.
_require_engine_run() {
    declare -F "provider_${1}_run_cmd" >/dev/null 2>&1 \
        || die "unknown engine: $1 (no providers/$1/provider.sh defines it) — see 'agent.sh doctor'"
}

# shared body for `run` and `test-api` (identical except test-api also tags kind=api-test via
# $task_kind) -- creates the log/meta, dispatches to the provider, finishes the step.
# Expects $name/$engine/$model/$dir/$parent/$prompt (and optionally $task_kind) already set.
_do_run_dispatch() {
    _require_engine_run "$engine"   # BEFORE the first .log/.meta write: no ghost tasks
    require_task_name "$name"
    log="$LOGDIR/$name.log"; _secure_state_truncate "$log" || die "cannot create protected task log: $log"
    meta_set "$name" engine "$engine"; meta_set "$name" model "${model:-default}"
    meta_set "$name" resolved_model ""; meta_set "$name" effort ""; meta_set "$name" session ""
    meta_set "$name" dir "$dir"; meta_set "$name" state running; meta_set "$name" reason ""; meta_set "$name" exit ""
    # NB: capture the pid into a variable FIRST. $BASHPID inside a command substitution reports
    # the substitution's own throwaway subshell, so `winpid "$(_winpid "$BASHPID")"` would record
    # the winpid of a process that is already dead -- and gui.py would call the task stalled while
    # the CLI called it running (exactly the bug this pairing is meant to end).
    step_pid="$BASHPID"
    meta_set "$name" pid "$step_pid"; meta_set "$name" winpid "$(_winpid "$step_pid")"
    meta_set "$name" started "$(now)"; meta_set "$name" timeout ""   # clear a previous run's marker
    [ -n "$parent" ] && meta_set "$name" parent "$parent"
    [ -n "$task_kind" ] && meta_set "$name" kind "$task_kind"
    echo "[agent.sh] ▶ run task=$name engine=$engine model=${model:-default} dir=$dir" >&2
    hdr run "engine=$engine model=${model:-default} dir=$dir" PROMPT "$prompt" "$log"
    rc=0
    provider_dispatch_run "$engine" "$model" "$dir" "$prompt" "$name"
    finish_step "$name" "$rc"
}

# builds the instructive prompt for `test-api`: exercise a local HTTP API via the agent's own
# shell/tool-use capability (curl et al. -- all bundled providers' non-interactive modes already
# support this, no new architecture needed), and report back one strict JSON object.
build_api_test_prompt() {
    local url="$1" goal="$2"
    cat <<PROMPT
You are testing a local HTTP API at $url .

Goal: $goal

Instructions:
1. Use your shell/tool-use capability (curl or equivalent) to explore and exercise this API
   according to the goal above. If you don't already know its shape, first try common
   introspection paths against $url (/, /health, /openapi.json, /swagger.json) to discover
   what's available, then proceed based on the goal.
2. Make REAL HTTP requests against $url and observe the actual responses -- do not guess or
   assume behavior without calling it.
3. When done, your FINAL message must be ONLY a single JSON object -- no markdown code fences,
   no prose before or after it -- matching exactly this shape:
{"base_url":"$url","goal":"<restate the goal>","overall":"pass|fail|partial","endpoints":[{"method":"...","path":"...","assertion":"...","result":"pass|fail","reason":"..."}],"summary":{"total":N,"passed":N,"failed":N}}

Do NOT run git commit. Do NOT modify any files unless the goal explicitly requires it.
PROMPT
}

case "$cmd" in
    run)
        parse_opts "$@"
        prompt="${REST[0]:-}"; [ -n "$prompt" ] || die "run: needs a prompt"
        [ "$progress" = 1 ] && prompt="$prompt$(progress_proto "$name")"
        [ "$terse" = 1 ] && prompt="$prompt$TERSE_PROTO"
        _do_run_dispatch
        dispatch_rc=$?
        ;;
    fan)
        # Launch N agents IN PARALLEL from one call: each positional prompt becomes its own
        # background `run` task, named <base>-01, <base>-02, ... (base = -t NAME, else the default).
        # Shared -e/-m/-f/-C options apply to all. Returns immediately; poll with `agent.sh list`
        # / `status <name>`. Saves writing a bash loop of backgrounded `run &` calls by hand.
        parse_opts "$@"
        [ ${#REST[@]} -ge 1 ] || die "fan: needs >=1 prompt (agent.sh fan [opts] \"p1\" \"p2\" ...)"
        _require_engine_run "$engine"   # synchronously + visibly; the backgrounded dispatches would swallow the error
        require_task_name "$name"
        fan_base="$name"; fan_i=0
        for fan_p in "${REST[@]}"; do
            fan_i=$((fan_i+1))
            fan_n="$fan_base-$(printf '%02d' "$fan_i")"
            fan_prompt="$fan_p"
            [ "$progress" = 1 ] && fan_prompt="$fan_prompt$(progress_proto "$fan_n")"
            [ "$terse" = 1 ] && fan_prompt="$fan_prompt$TERSE_PROTO"
            ( name="$fan_n"; prompt="$fan_prompt"; _do_run_dispatch ) >/dev/null 2>&1 &
            echo "[agent.sh] ⇉ fanned $fan_n (pid $!)" >&2
        done
        echo "[agent.sh] launched $fan_i parallel task(s) under '$fan_base'. Poll: agent.sh list" >&2
        ;;
    test-api)
        # Thin wrapper on top of `run`, not a new provider: builds a prompt instructing the
        # agent to exercise a local HTTP API via its own shell/curl capability and report back
        # one strict JSON object, then dispatches through the exact same path as `run`.
        parse_opts "$@"
        [ -n "$base_url" ] || die "test-api: needs --base-url <url>"
        [ -n "$test_goal" ] || die "test-api: needs --goal \"<what to verify>\""
        prompt="$(build_api_test_prompt "$base_url" "$test_goal")"
        [ "$progress" = 1 ] && prompt="$prompt$(progress_proto "$name")"
        [ "$terse" = 1 ] && prompt="$prompt$TERSE_PROTO"
        task_kind="api-test"
        _do_run_dispatch
        if [ -n "$out_file" ]; then
            # Extract the agent's final JSON despite real-world variance (models sometimes wrap
            # it in a markdown ```json fence or add a sentence before/after despite the
            # instruction not to) -- take from the first "{" to the last "}" in the last output
            # block, which tolerates both a bare JSON line and a fenced/annotated one.
            # JSON extraction needs a real interpreter: route it through the shared _agent_python
            # (Windows' bare `python` can be an exit-nonzero stub) and fail clearly when none runs.
            if _agent_python; then
                last_output "$log" | PYTHONIOENCODING=utf-8 "$_AGENT_PY" -c "
import json, sys
s = sys.stdin.read()
i, j = s.find('{'), s.rfind('}')
extracted = s[i:j+1] if i != -1 and j != -1 and j > i else s.strip()
try:
    json.loads(extracted)
    print(extracted, end='')
except Exception:
    sys.stderr.write('warning: could not extract valid JSON from the agent output -- writing raw output instead\n')
    print(s, end='')
" > "$out_file"
            else
                die "test-api: --out needs python to validate the JSON (python3/python/py in PATH or \$AGENT_PYTHON) — none runnable"
            fi
            echo "[agent.sh] wrote $out_file" >&2
        fi
        exit "$dispatch_rc"
        ;;
    reply)
        parse_opts "$@"
        if [ ${#REST[@]} -ge 2 ]; then ref="${REST[0]}"; answer="${REST[1]}"; else ref=""; answer="${REST[0]:-}"; fi
        [ -n "$answer" ] || die "reply: needs an answer text"
        if [ -z "$ref" ]; then tname="$(latest_task)"; [ -n "$tname" ] || die "reply: no tasks — specify name/session id"
        elif valid_task_name "$ref" && [ -e "$(meta_file "$ref")" ]; then tname="$ref"
        elif [[ "$ref" =~ ^[0-9a-f-]{36}$|^ses_[[:alnum:]_.-]+$|^session_[[:alnum:]_.-]+$ ]]; then tname="$(name_by_session "$ref")"
        else
            require_task_name "$ref"
            tname="$ref"
            # An explicit NAME must point at a real task. Without this check a typo fell through
            # every lookup empty-handed: engine stayed at the default claude, the session guard
            # was bypassed by `[ "$engine" = claude ]`, a ghost .meta/.log got created, and
            # `claude --continue` resumed WHATEVER conversation happened to be latest — an answer
            # delivered into the wrong thread. Fail loudly instead.
            [ -e "$(meta_file "$tname")" ] || die "reply: no such task '$ref' ($LOGDIR/$ref.meta missing) — see 'agent.sh list'"
        fi
        if [ -n "${tname:-}" ]; then
            session="$(resolve_session "$tname")"
            mdir="$(meta_get "$tname" dir)"; [ -n "$mdir" ] && dir="$mdir"
            meng="$(meta_get "$tname" engine)"; [ -n "$meng" ] && [ "$engine_explicit" = 0 ] && engine="$meng"
            log="$LOGDIR/$tname.log"
        else session="$ref"; tname="session-$ref"; require_task_name "$tname"; log="$LOGDIR/$tname.log"; meta_set "$tname" dir "$dir"; fi
        # Validate the engine BEFORE touching .meta. provider_dispatch_resume dies further down, but
        # by then state=running and pid= have already been written, so the dead task shows up as
        # `⚠ stalled` and status helpfully advises re-running the very reply that cannot work. Seen
        # on opencode: provider.json says supports_resume=false, so there is no
        # provider_opencode_resume_cmd, and the generic dispatcher blamed it on an "unknown engine".
        declare -F "provider_${engine}_run_cmd" >/dev/null 2>&1 \
            || die "reply: unknown engine '$engine' (task '$tname') — no providers/$engine/provider.sh defines it"
        if ! declare -F "provider_${engine}_resume_cmd" >/dev/null 2>&1; then
            rmodel="$(meta_get "$tname" model)"
            die "reply: engine '$engine' cannot resume a session (supports_resume=false), so task '$tname' cannot be continued — start a fresh one instead: agent.sh run -t <name> -e $engine${rmodel:+ -m ${rmodel}} -C $dir \"...\""
        fi
        [ "$progress" = 1 ] && answer="$answer$(progress_proto_reply "$tname")"   # per-task reminder; needs resolved $tname
        [ -n "${session:-}" ] || [ "$engine" = claude ] || die "reply: could not find a session id (task '$tname'); specify uuid explicitly"
        # Optional provider preflight happens BEFORE touching the shared log/meta. Codex uses this
        # to wait on the actual thread-store file lock and for the prior wrapper to finish its final
        # log/meta writes. Appending a reply header earlier interleaves both turns and lets the old
        # turn overwrite the failed reply's state (the live defect this ordering fixes).
        prepare_fn="provider_${engine}_prepare_resume"; PROVIDER_PREPARE_NOTE=""; prepare_rc=0
        if declare -F "$prepare_fn" >/dev/null 2>&1; then
            "$prepare_fn" "$session" "$tname" || prepare_rc=$?
            [ "$prepare_rc" = 0 ] || die "reply: provider preflight failed for task '$tname' (exit=$prepare_rc): ${PROVIDER_PREPARE_NOTE:-session writer did not become available}"
        fi
        _secure_state_file "$log" || die "cannot create protected task log: $log"
        meta_set "$tname" state running; meta_set "$tname" pid "$$"
        meta_set "$tname" winpid "$(_winpid "$$")"   # $$ is the SHELL's pid, safe inside $( ) — unlike $BASHPID
        meta_set "$tname" timeout ""; meta_set "$tname" reason ""; meta_set "$tname" exit ""
        echo "[agent.sh] ▶ reply task=$tname session=$session dir=$dir" >&2
        hdr reply "task=$tname session=$session" ANSWER "$answer" "$log"
        [ -n "$PROVIDER_PREPARE_NOTE" ] && printf '%s\n' "$PROVIDER_PREPARE_NOTE" >> "$log"
        rc=0
        provider_dispatch_resume "$engine" "$dir" "$session" "$answer" "$tname"
        finish_step "$tname" "$rc"
        ;;
    log)
        follow=0; lines=0; lastonly=0
        while [ $# -gt 0 ]; do case "$1" in
            -f) follow=1; shift ;;
            -n) [ $# -ge 2 ] || die "log: option '-n' needs an operand"; case "$2" in ''|*[!0-9]*) die "log: -n needs a non-negative integer" ;; esac; lines="$2"; shift 2 ;;
            -l) lastonly=1; shift ;; *) break ;; esac; done
        f="${1:-}"
        if [ -n "$f" ]; then require_task_name "$f"; log="$LOGDIR/$f.log"
        else log="$(_latest_file log)"; fi
        [ -e "${log:-}" ] || die "log not found: ${f:-<latest>}"
        if   [ "$follow" = 1 ]; then tail -f "$log"
        elif [ "$lastonly" = 1 ]; then awk '/^========== \[/{buf=""} {buf=buf $0 ORS} END{printf "%s", buf}' "$log"
        elif [ "$lines" -gt 0 ]; then tail -n "$lines" "$log"
        else cat "$log"; fi
        ;;
    last)
        f="${1:-}"
        if [ -n "$f" ]; then require_task_name "$f"; log="$LOGDIR/$f.log"; else log="$(_latest_file log)"; fi
        [ -e "${log:-}" ] || die "log not found: ${f:-<latest>}"
        last_output "$log"
        ;;
    # wait [names...] — completion primitive for orchestrators that have their own background-job
    # mechanism: blocks until every named task leaves `running` (the SAME eff_state machine as list/
    # gui), prints a one-line settled note the moment each task settles, then prints each final state
    # + the agent's last answer block to stdout. Launch ONE background job per task — that job's exit
    # IS your completion notification and its output is the subagent's final answer (exit 0 = settled,
    # exit 2 = --timeout hit while a task was still running). With no names it watches every task
    # currently state=running — one call covers a whole fan-out wave.
    wait)
        w_timeout="${AGENT_WAIT_TIMEOUT:-0}"; w_poll="${AGENT_WAIT_POLL:-5}"; w_names=()
        while [ $# -gt 0 ]; do case "$1" in
            --timeout) [ $# -ge 2 ] || die "wait: option '--timeout' needs an operand (seconds, 0 = forever)"; w_timeout="$2"; shift 2 ;;
            --poll)    [ $# -ge 2 ] || die "wait: option '--poll' needs an operand (seconds)"; w_poll="$2"; shift 2 ;;
            *) w_names+=("$1"); shift ;;
        esac; done
        case "$w_timeout" in ''|*[!0-9]*) die "wait: --timeout must be a non-negative integer (0 = wait forever)" ;; esac
        case "$w_poll"    in ''|*[!0-9]*) die "wait: --poll must be a positive integer" ;; esac
        for a in "${w_names[@]}"; do require_task_name "$a"; done
        if [ ${#w_names[@]} -eq 0 ]; then
            # no names given -> watch every task currently running (whole-wave mode)
            for mf in "$LOGDIR"/*.meta; do
                [ -e "$mf" ] || continue
                n="${mf##*/}"; n="${n%.meta}"
                valid_task_name "$n" || continue
                case "$(eff_state "$n")" in running|idle) w_names+=("$n") ;; esac
            done
        fi
        if [ ${#w_names[@]} -eq 0 ]; then echo "[agent.sh] wait: no tasks to watch — nothing to do"; exit 0; fi
        for n in "${w_names[@]}"; do
            [ -e "$(meta_file "$n")" ] || echo "[agent.sh] wait: warning: task '$n' not found ($LOGDIR/$n.meta missing) yet" >&2
        done
        w_start="$(date +%s)"; settled=" "; w_rc=0
        while :; do
            still=""
            for n in "${w_names[@]}"; do
                case "$settled" in *" $n "*) continue ;; esac
                st="$(eff_state "$n")"
                # WHY: idle means alive but quiet — a codex/claude step flushes its log only when it
                # ENDS, so treating idle as settled makes wait return while the task is still working.
                if [ "$st" = running ] || [ "$st" = idle ]; then still="$still $n"
                else
                    settled="${settled}${n} "
                    echo "[agent.sh] $(state_icon "$st") settled: $n -> $(state_label "$st" "$(log_idle_sec "$n")") (exit=$(meta_get "$n" exit), files=$(meta_get "$n" files))" >&2
                fi
            done
            [ -z "${still// /}" ] && break
            if [ "$w_timeout" -gt 0 ] && [ $(( $(date +%s) - w_start )) -ge "$w_timeout" ]; then
                echo "[agent.sh] wait: timeout after ${w_timeout}s; still running:${still}" >&2
                w_rc=2; break
            fi
            sleep "$w_poll"
        done
        for n in "${w_names[@]}"; do
            logf="$LOGDIR/$n.log"
            echo ""
            echo "========== wait | $n | $(eff_state "$n") =========="
            if [ -e "$logf" ]; then last_output "$logf"; else echo "(no log for $n)"; fi
        done
        echo "WAIT_DONE tasks=${#w_names[@]} rc=$w_rc"
        exit "$w_rc"
        ;;
    status)
        n="${1:-$(latest_task)}"; [ -n "$n" ] || die "no tasks"
        require_task_name "$n"
        [ -e "$(meta_file "$n")" ] || die "no such task: $n"
        st="$(eff_state "$n")"; e="$(meta_get "$n" engine)"; mo="$(meta_get "$n" model)"
        ex="$(meta_get "$n" exit)"; nf="$(meta_get "$n" files)"; s="$(meta_get "$n" session)"; d="$(meta_get "$n" dir)"
        reason="$(meta_get "$n" reason)"
        idle_s="$(log_idle_sec "$n")"
        live=""; [ "$st" = running ] || [ "$st" = idle ] && live=" (alive, pid $(meta_get "$n" pid))"
        echo "$(state_icon "$st") task=$n  state=$(state_label "$st" "${idle_s:-0}")${live}  engine=$e/${mo}  exit=${ex:-–}  files=${nf:-0}"
        echo "   dir=$d"; echo "   session=${s:-–}"
        echo "   started=$(meta_get "$n" started)  md=$LOGDIR/$n.md"
        [ "$st" = waiting ]  && echo "   → needs a REPLY: agent.sh reply $n \"...\""
        [ "$st" = stalled ]  && echo "   ⚠ process not alive (computer shut down / killed) — continue: agent.sh reply $n \"continue\""
        [ "$st" = running ]  && echo "   ⟳ still working — follow: agent.sh log -f $n"
        # honest third state: the process IS alive, it just has not written anything for a while
        # (a codex/claude step only flushes its log when the step ends). Not an error by itself.
        [ "$st" = idle ]     && echo "   ⟳ process alive but SILENT for $(( ${idle_s:-0} / 60 ))m — it is still working unless AGENT_TIMEOUT_SEC(${AGENT_TIMEOUT_SEC}s) kills it; follow: agent.sh log -f $n"
        [ "$st" = error ]    && echo "   ✖ TURN DIED — work may or may not have landed; inspect the working tree. reason=${reason:-provider failure}"
        [ -n "$(meta_get "$n" timeout)" ] && [ "$st" = error ] && \
            echo "   ⏱ killed by the step watchdog after $(meta_get "$n" timeout)s (AGENT_TIMEOUT_SEC) — raise it, or $(_recovery_hint "$n")"
        if [ -n "$d" ] && [ "${nf:-0}" != 0 ] && git -C "$d" rev-parse --git-dir >/dev/null 2>&1; then
            echo "   --- changed files ---"; git -C "$d" status --porcelain 2>/dev/null | sed 's/^/   /'
        fi
        echo "   --- current step (last lines) ---"
        last_output "$LOGDIR/$n.log" | grep -v '^[[:space:]]*$' | tail -4 | sed 's/^/   /'
        ;;
    list)
        list_limit="${1:-20}"; case "$list_limit" in ''|*[!0-9]*) die "list: limit must be a non-negative integer" ;; esac
        printf '%-2s %-24s %-8s %-9s %-13s %-6s %-6s %-8s %s\n' "" TASK STATE ENGINE MODEL AGE FILES SESSION DETAIL
        _sorted_meta_files "$list_limit" || die "list: invalid limit '$list_limit'"
        for m in "${_SORTED_META_FILES[@]}"; do
            n="$(basename "$m" .meta)"
            valid_task_name "$n" || continue
            e="$(meta_get "$n" engine)"; mo="$(meta_get "$n" model)"; s="$(meta_get "$n" session)"
            st="$(eff_state "$n")"; nf="$(meta_get "$n" files)"; reason="$(meta_get "$n" reason)"; detail=""
            [ "$st" = error ] && detail="turn died; inspect tree — ${reason:-provider failure}"
            fm="$(file_mtime "$m")"
            age="$(( ($(date +%s) - ${fm:-0}) / 60 ))m"
            printf '%-2s %-24s %-8s %-9s %-13s %-6s %-6s %-8s %s\n' "$(state_icon "$st")" "$n" "${st:-?}" "${e:-?}" "${mo:-?}" "$age" "${nf:-0}" "${s:0:8}" "$detail"
        done
        ;;
    provider-info)
        eng="${1:-}"; [ -n "$eng" ] || die "provider-info: needs an engine name"
        fn="provider_${eng}_doctor"
        declare -F "$fn" >/dev/null 2>&1 || die "unknown engine: $eng"
        "$fn"
        ;;
    doctor)
        # --deep: additionally run each provider's real one-shot check (see the deep-checks block
        # at the end). Off by default because it costs a real (cheap) model call per engine.
        deep=0; doctor_json=0
        for a in "$@"; do case "$a" in
            --deep) deep=1 ;;
            --json) doctor_json=1 ;;
            *) die "doctor: unknown option '$a' (use --deep or --json)" ;;
        esac; done
        [ "$deep" = 1 ] && [ "$doctor_json" = 1 ] && die "doctor: --deep and --json cannot be combined"

        # Provider CLIs are slow to spawn on Windows. Probe each engine concurrently into its own
        # file (so output can never interleave), wait once, then parse/render every JSON object in a
        # single Python process. The old implementation serialized all probes, spawned Python three
        # times per JSON object, and then repeated Codex + Claude for their limit blocks.
        _agent_python || die "doctor: needs python to render the report (python3/python/py in PATH or \$AGENT_PYTHON) — none runnable"
        doctor_tmp="$(mktemp -d "${TMPDIR:-/tmp}/agent-doctor.XXXXXX")" || die "doctor: cannot create temp dir"
        [ -n "$doctor_tmp" ] && [ -d "$doctor_tmp" ] || die "doctor: mktemp returned an invalid temp dir"
        _doctor_cleanup() {
            local base
            base="$(basename "${doctor_tmp:-}")"
            case "$base" in agent-doctor.*) [ -n "${doctor_tmp:-}" ] && [ -d "$doctor_tmp" ] && rm -rf -- "$doctor_tmp" ;; esac
        }
        trap _doctor_cleanup EXIT
        trap '_doctor_cleanup; exit 130' HUP INT TERM
        doctor_engines=(); doctor_pids=(); doctor_deep_engines=()
        for pdir in "$PROVIDERS_DIR"/*/; do
            [ -d "$pdir" ] || continue
            eng="$(basename "$pdir")"
            fn="provider_${eng}_doctor"
            declare -F "$fn" >/dev/null 2>&1 || continue
            doctor_engines+=("$eng")
            ( "$fn" >"$doctor_tmp/$eng.json" 2>/dev/null ||
              printf '{"engine":%s,"version":"ERROR","available":false,"login":"","limits":null,"note":"probe failed"}\n' \
                "$(_json_str "$eng")" >"$doctor_tmp/$eng.json" ) &
            doctor_pids+=("$!")
            declare -F "provider_${eng}_doctor_deep" >/dev/null 2>&1 && doctor_deep_engines+=("$eng")
        done
        for doctor_pid in "${doctor_pids[@]}"; do wait "$doctor_pid" 2>/dev/null || true; done

        doctor_engine_csv="$(IFS=,; echo "${doctor_engines[*]}")"
        doctor_deep_csv="$(IFS=,; echo "${doctor_deep_engines[*]}")"
        PYTHONIOENCODING=utf-8 "$_AGENT_PY" - "$doctor_tmp" "$doctor_json" "$doctor_engine_csv" "$doctor_deep_csv" "$deep" <<'PY'
import datetime, json, os, sys, time
root, as_json, engine_csv, deep_csv, deep = sys.argv[1:6]
now = time.time()
engines = []

def _epoch(value):
    """A window's resets_at arrives however the provider's upstream API spelled it: a unix epoch
    (claude's gui.py path), a numeric string, or an ISO-8601 timestamp -- Anthropic's
    /api/oauth/usage sends the last one, which used to reach `epoch - now` as a str and abort the
    ENTIRE doctor report with a TypeError. Never raise: an unreadable limits field must cost us
    that one field, not the whole report."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        return float(text)
    except ValueError:
        pass
    try:
        stamp = datetime.datetime.fromisoformat(text.replace('Z', '+00:00'))
    except ValueError:
        return None
    if stamp.tzinfo is None:          # upstream sends UTC; assuming local here would skew by the
        stamp = stamp.replace(tzinfo=datetime.timezone.utc)   # machine's offset, silently.
    return stamp.timestamp()

def normalize_limits(info):
    """Rewrite every window's resets_at to a float epoch (or None) IN PLACE, so the text renderer
    and every --json consumer (static/app.js, static/modals.js, which both do `resets_at - now`
    and would quietly produce NaN on a string) see one type."""
    limits = info.get('limits')
    if not isinstance(limits, dict):
        return
    buckets = [w for w in (limits.get('windows') or []) if isinstance(w, dict)]
    buckets += [limits[k] for k in ('primary', 'secondary') if isinstance(limits.get(k), dict)]
    for win in buckets:
        win['resets_at'] = _epoch(win.get('resets_at'))
for name in filter(None, engine_csv.split(',')):
    try:
        with open(os.path.join(root, name + '.json'), encoding='utf-8') as f:
            info = json.load(f)
    except Exception:
        info = {"engine": name, "version": "ERROR", "available": False, "login": "",
                "limits": None, "note": "probe returned invalid JSON"}
    info.setdefault('engine', name)
    normalize_limits(info)
    login = (info.get('login') or '').lower()
    if not info.get('available'):
        info['state'] = 'not_installed'
    elif any(x in login for x in ('required', 'not logged', 'logged out')):
        info['state'] = 'not_logged_in'
    else:
        info['state'] = 'ok'
    engines.append(info)

def windows(info):
    limits = info.get('limits') or {}
    if isinstance(limits.get('windows'), list):
        return limits['windows']
    out = []
    for key, label in (('primary', 'primary'), ('secondary', 'secondary')):
        if isinstance(limits.get(key), dict):
            item = dict(limits[key]); item.setdefault('label', label); out.append(item)
    return out

def window_label(minutes):
    if not minutes: return '?'
    if minutes % 1440 == 0: return '%dd' % (minutes // 1440)
    if minutes % 60 == 0: return '%dh' % (minutes // 60)
    return '%dm' % minutes

def reset_text(value):
    epoch = _epoch(value)   # defensive: normalize_limits already ran, but a provider added later
    if not epoch: return ''  # must not be able to kill the report by inventing a new spelling.
    left = int(epoch - now)
    if left <= 0: return ' resets soon'
    h, rem = divmod(left, 3600)
    return ' resets in %dh%02dm' % (h, rem // 60)

raw = ['=== engines (CLI) ===']
for info in engines:
    name = info['engine']
    if info['state'] == 'not_installed':
        raw.append('  %-9s -    (not in PATH)' % name)
    else:
        raw.append('  %-9s ok   %s' % (name, info.get('version') or '?'))
        if info.get('login'): raw.append('  %-9s auth %s' % (name, info['login']))
for info in engines:
    if info['engine'] not in ('codex', 'claude'): continue
    live = windows(info)
    if live:
        source = 'latest session' if info['engine'] == 'codex' else 'provider API'
        raw.append('=== %s rate limits (%s) ===' % (info['engine'], source))
        raw.append('  plan: %s' % ((info.get('limits') or {}).get('plan_type') or '?'))
        for win in live:
            used = float(win.get('used_percent') or win.get('utilization') or 0)
            n = max(0, min(10, int(used // 10)))
            raw.append('  %-9s [%s] %4.0f%% (window %s)%s' %
                       (win.get('label') or 'window', '#'*n + '-'*(10-n), used,
                        window_label(win.get('window_minutes')), reset_text(win.get('resets_at'))))
    elif info['engine'] == 'codex':
        raw.extend(['=== codex rate limits (from latest session) ===',
                    '  no rate-limit data in recent sessions'])
claude = next((x for x in engines if x['engine'] == 'claude'), None)
if claude and not windows(claude):
    raw.append('=== claude usage (local transcript estimate) ===')
    raw.append('  ' + (claude.get('note') or 'no local usage data'))
raw.append('=== deep checks (real one-shot runs) ===')
if not deep_csv:
    raw.append('  (no provider implements a deep check)')
elif deep != '1':
    raw.append("  skipped - run 'agent.sh doctor --deep' to execute a shell command through each supported engine")
    raw.append("  (that is the only check that catches 'the model answers but every command hangs')")

payload = {"generated_at": now, "engines": engines, "raw": '\n'.join(raw) + '\n',
           "deep_engines": [x for x in deep_csv.split(',') if x]}
if as_json == '1':
    print(json.dumps(payload, ensure_ascii=False, separators=(',', ':')))
else:
    sys.stdout.write(payload['raw'])
PY
        doctor_render_rc=$?
        _doctor_cleanup
        trap - EXIT HUP INT TERM
        [ "$doctor_render_rc" = 0 ] || die "doctor: failed to render provider report (exit $doctor_render_rc)"
        # --- deep checks --------------------------------------------------------------------
        # Everything above is metadata: binary present, version, login, limits. None of it can
        # catch the failure that actually bit us -- the model answers fine but every shell command
        # it issues blocks forever, so the task sits in state=running for hours. Only a REAL run
        # that must execute a command catches that, so it lives behind --deep (one cheap model
        # call per engine that implements provider_<eng>_doctor_deep).
        if [ "$deep" = 1 ]; then
            for eng in "${doctor_deep_engines[@]}"; do "provider_${eng}_doctor_deep"; done
        fi
        ;;
    gui)
        # lightweight local web dashboard over all providers: http://127.0.0.1:<port>
        # native python (win) resolves `bash` to WSL -> pass it the EXACT git-bash path.
        # Every argument is forwarded verbatim (port, --lan, --token SECRET, --localhost) and
        # gui.py owns the parsing -- with NO argument it still falls back to $AGENT_GUI_PORT or
        # its own stable 8765 default (an unconditional "${1:-8765}" here used to defeat
        # AGENT_GUI_PORT, because python always saw an argv).
        _agent_python || die "gui: needs python to run gui.py (python3/python/py in PATH or \$AGENT_PYTHON) — none runnable"
        export AGENT_SH_BASH="$(cygpath -w "$BASH" 2>/dev/null || echo bash)"
        # Long-lived server: on Windows prefer the windowless interpreter so no console window survives.
        if _windowless_python; then exec "$_WINDOWLESS_PY" "$(dirname "$0")/gui.py" "$@"; fi
        exec "$_AGENT_PY" "$(dirname "$0")/gui.py" "$@"
        ;;
    openai-server)
        # One process = one fixed engine/model/effort; run several (different -e/-m/-f/-p) to compare
        # providers/models side by side. Foreground like `gui`, not backgrounded here.
        _agent_python || die "openai-server: needs python to run openai_server.py (python3/python/py in PATH or \$AGENT_PYTHON) — none runnable"
        export AGENT_SH_BASH="$(cygpath -w "$BASH" 2>/dev/null || echo bash)"
        # Long-lived server: on Windows prefer the windowless interpreter so no console window survives.
        if _windowless_python; then exec "$_WINDOWLESS_PY" "$(dirname "$0")/openai_server.py" "$@"; fi
        exec "$_AGENT_PY" "$(dirname "$0")/openai_server.py" "$@"
        ;;
    clean|prune)
        # Remove md clutter left by STOPPED tasks: the generated <name>.md thread in LOGDIR and the
        # agent's per-task PROGRESS.<name>.md in its working dir. Only stopped tasks (done/error/
        # stalled) are touched; live (running/idle) and waiting (needs-reply) tasks are left intact. Only the
        # unambiguously agent-generated PROGRESS.<name>.md is removed -- a generic PROGRESS.md is
        # never touched.
        #   --all     also clean waiting (needs-reply) tasks
        #   --purge   also delete each task's .log and .meta record (fully forget it)
        #   -n        dry run: list what would be removed, delete nothing
        incl_waiting=0; purge=0; dry=0
        for a in "$@"; do case "$a" in
            --all) incl_waiting=1 ;;
            --purge) purge=1 ;;
            -n|--dry-run) dry=1 ;;
            *) die "clean: unknown option '$a' (use --all, --purge, -n)" ;;
        esac; done
        tasks=0; files=0
        for f in "$LOGDIR"/*.meta; do
            [ -e "$f" ] || continue
            n="$(basename "$f" .meta)"; valid_task_name "$n" || continue; st="$(eff_state "$n")"
            case "$st" in
                # `idle` is a LIVE task (alive pid, just quiet) -- it must be skipped exactly like
                # `running`, otherwise --purge deletes the .log/.meta of a process that is still
                # writing to them. Before the idle state existed, such a task simply read as
                # `running` here, so leaving idle out silently turned clean into a data loss.
                running|idle) continue ;;
                waiting) [ "$incl_waiting" = 1 ] || continue ;;
            esac
            d="$(meta_get "$n" dir)"
            targets=("$LOGDIR/$n.md")
            [ -n "$d" ] && targets+=("$d/PROGRESS.$n.md")
            [ "$purge" = 1 ] && targets+=("$LOGDIR/$n.log" "$f")
            hit=0
            for t in "${targets[@]}"; do
                [ -n "$t" ] && [ -f "$t" ] || continue
                if [ "$dry" = 1 ]; then echo "  would remove: $t"; else rm -f "$t"; fi
                files=$((files+1)); hit=1
            done
            [ "$hit" = 1 ] && tasks=$((tasks+1))
        done
        if [ "$dry" = 1 ]; then
            echo "[agent.sh] clean (dry-run): $files file(s) across $tasks stopped task(s) would be removed" >&2
        else
            echo "[agent.sh] clean: removed $files file(s) across $tasks stopped task(s)" >&2
        fi
        ;;
    help|--help|-h)
        # print this file's own header comment as the command reference -- one source of
        # truth instead of a duplicated usage string that can drift out of sync. Printed by
        # SHAPE (line 2 up to the first non-comment line) rather than by a hardcoded line range,
        # which silently truncated the reference every time the header grew.
        awk 'NR==1{next} /^#/{sub(/^# ?/,""); print; next} {exit}' "$0"
        ;;
    *) die "unknown command: $cmd (see: agent.sh help)" ;;
esac
