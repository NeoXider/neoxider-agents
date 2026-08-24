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
set -uo pipefail

LOGDIR="${AGENT_CLI_LOGS:-$HOME/.claude/agent-cli-logs}"
mkdir -p "$LOGDIR"

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
[ -n "$cmd" ] || die "usage: agent.sh run|fan|reply|log|last|status|list|clean|doctor|provider-info|gui|openai-server|help ... (run 'agent.sh help' for the full reference)"
shift

engine="claude"; engine_explicit=0; model=""; effort_override=""; dir="$(pwd)"; name="task-$(date +%Y%m%d-%H%M%S)-$$"; progress=1; terse=1
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
            -e) engine="$2"; engine_explicit=1; shift 2 ;;
            -m) model="$2"; shift 2 ;;
            -f) effort_override="$2"; shift 2 ;;
            -C) dir="$2"; shift 2 ;;
            -t) name="$2"; shift 2 ;;
            -P) parent="$2"; shift 2 ;;
            -p) progress=1; shift ;;                 # kept for compat; progress is on by default
            --no-progress) progress=0; shift ;;      # opt out of the PROGRESS.md checkpoint
            --no-terse|--verbose) terse=0; shift ;;  # opt out of the concision directive
            --base-url) base_url="$2"; shift 2 ;;
            --goal) test_goal="$2"; shift 2 ;;
            --out) out_file="$2"; shift 2 ;;
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
meta_file() { echo "$LOGDIR/$1.meta"; }
_meta_lock() {
    local lock="$1.lock.d" i=0
    until mkdir "$lock" 2>/dev/null; do
        i=$((i + 1))
        # stale lock (holder crashed / was killed) -> force through after ~5s rather than deadlock forever
        [ "$i" -gt 50 ] && { rm -rf "$lock" 2>/dev/null; break; }
        sleep 0.1
    done
}
_meta_unlock() { rmdir "$1.lock.d" 2>/dev/null; }
meta_set()  { local f; f="$(meta_file "$1")"
    _meta_lock "$f"
    touch "$f"
    grep -v "^$2=" "$f" > "$f.tmp" 2>/dev/null || true; echo "$2=$3" >> "$f.tmp"; mv "$f.tmp" "$f"
    _meta_unlock "$f"
}
meta_get()  { grep -m1 "^$2=" "$(meta_file "$1")" 2>/dev/null | cut -d= -f2- ; }

resolve_session() {
    local n="$1" s; s="$(meta_get "$n" session)"; [ -n "$s" ] && { echo "$s"; return; }
    grep -m1 -oE 'session id: [[:alnum:]_.-]+' "$LOGDIR/$n.log" 2>/dev/null | cut -d' ' -f3
}
name_by_session() { local s="$1" f
    for f in "$LOGDIR"/*.meta; do [ -e "$f" ] || continue
        if grep -q "^session=$s$" "$f"; then basename "$f" .meta; return; fi; done; }
latest_task() { ls -t "$LOGDIR"/*.meta 2>/dev/null | head -1 | xargs -r basename | sed 's/\.meta$//'; }

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
    local n="$1" log="$LOGDIR/$1.log" md="$LOGDIR/$1.md" st; st="$(eff_state "$n")"
    {
        echo "# Subagent task: $n"
        echo
        echo "- **State:** $st"
        echo "- **Engine:** $(meta_get "$n" engine) / $(meta_get "$n" model)"
        echo "- **Dir:** \`$(meta_get "$n" dir)\`"
        echo "- **Session:** \`$(meta_get "$n" session)\`"
        echo "- **Exit:** $(meta_get "$n" exit)  **Changed files:** $(meta_get "$n" files)"
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

# looks_waiting LINE — true if this final output line asks for input (-> state=waiting):
# real (alphanumeric) text before a trailing '?' — bare '?'/'?!'/punctuation-only do NOT count —
# or an explicit ask-phrase (English + Russian phrasings seen live). Trailing quotes/brackets and
# a `(yes/no)` aside are stripped first.
looks_waiting() {
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
            [ -n "$(printf '%s' "$q" | tr -cd '[:alnum:]')" ] && return 0
            ;;
    esac
    printf '%s' "$body" | grep -qiE \
        'should i |do you want|which (one|option|approach|of)|please (confirm|clarify|specify)|let me know|shall i |уточни|подтверд|как (мне |)поступ|какой из' \
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

_is_transient_failure() {
    local log="$1" tailtxt
    tailtxt="$(tail -c 4000 "$log" 2>/dev/null)"
    printf '%s' "$tailtxt" | grep -qiE 'unauthorized|invalid api key|authentication|forbidden|model .* (not found|unavailable|unknown)|insufficient (quota|credit)|quota exceeded|payment required' && return 1
    printf '%s' "$tailtxt" | grep -qiE '"isretryable"[[:space:]]*:[[:space:]]*true|network_error|providerresponsestreamerror|stream (error|closed|interrupted)|econnreset|etimedout|enotfound|socket hang up|(429|500|502|503|504)|overloaded|temporarily unavailable|rate.?limit'
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
    local n="$1" rc="$2" log tdir nfiles=0 last_line
    log="$LOGDIR/$n.log"; tdir="$(meta_get "$n" dir)"; [ -n "$tdir" ] || tdir="$dir"
    meta_set "$n" exit "$rc"
    if git -C "$tdir" rev-parse --git-dir >/dev/null 2>&1; then
        nfiles=$(git -C "$tdir" status --porcelain 2>/dev/null | grep -c .)
    fi
    meta_set "$n" files "$nfiles"
    last_line="$(last_output "$log" | grep -v '^[[:space:]]*$' | tail -1)"
    if [ "$rc" = 124 ]; then
        # killed by the step watchdog (_guarded_run). Recorded in meta so `status`/`list`/the GUI
        # can say WHY the task died instead of showing a bare exit code.
        meta_set "$n" state error
        meta_set "$n" timeout "$AGENT_TIMEOUT_SEC"
        echo "[agent.sh] ⏱ TIMEOUT after ${AGENT_TIMEOUT_SEC}s — task=$n killed (raise AGENT_TIMEOUT_SEC or $(_recovery_hint "$n"))" >&2
    elif [ "$rc" -ne 0 ]; then
        meta_set "$n" state error
        echo "[agent.sh] ✖ error exit=$rc  task=$n  (log: agent.sh log $n | $(_recovery_hint "$n"))" >&2
    elif looks_waiting "$last_line"; then
        meta_set "$n" state waiting
        echo "[agent.sh] ⏳ the agent appears to have ASKED a question — reply: agent.sh reply $n \"...\"  (question: agent.sh last $n)" >&2
    else
        meta_set "$n" state done
        echo "[agent.sh] ✔ done  task=$n  files=$nfiles  (log: agent.sh log $n | result: agent.sh last $n)" >&2
    fi
    render_md "$n"
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
    fi
    local _try=0
    while : ; do
        _guarded_run "$AGENT_TIMEOUT_SEC" "$fn" "$d" "$P_MODEL" "$P_EFFORT" "$prompt" 2>&1 | tee -a "$log" | tail -40
        rc=${PIPESTATUS[0]}
        { [ "$rc" = 0 ] || [ "$rc" = 124 ]; } && break
        [ "$_try" -ge "$AGENT_RETRIES" ] && break
        _is_transient_failure "$log" || break
        _try=$((_try+1))
        echo "[agent.sh] ↻ временный сбой провайдера (exit=$rc) — повтор $_try/$AGENT_RETRIES через ${AGENT_RETRY_DELAY}s  task=$n" >&2
        printf '
--- RETRY %s/%s after transient provider failure ---
' "$_try" "$AGENT_RETRIES" >> "$log"
        sleep "$AGENT_RETRY_DELAY"
    done
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
    # Only re-resolve model/effort on resume for providers whose resume command actually takes a
    # model flag (claude and codex both do; PROVIDER_{CLAUDE,CODEX}_RESUME_NEEDS_MODEL=1). Every
    # other provider opts out by simply not setting this flag, so reply never overwrites their
    # model= meta.
    # CAVEAT for providers that DO opt in: a bare `reply` with no explicit -m resolves to that
    # provider's DEFAULT alias (e.g. codex -> gpt-5.6-sol medium), not whatever model the task
    # actually started with -- confirmed live: a codex session started with `-m spark` silently
    # ran under gpt-5.5 on a `reply` that didn't repeat `-m spark`. Callers that need to guarantee
    # the SAME model across every turn (e.g. openai_server.py) must pass -m/-f explicitly on every
    # reply, not rely on it being remembered.
    local needs_var="PROVIDER_${eng^^}_RESUME_NEEDS_MODEL" resolve_fn="provider_${eng}_resolve"
    P_MODEL=""; P_EFFORT=""
    if [ "${!needs_var:-0}" = 1 ] && declare -F "$resolve_fn" >/dev/null 2>&1; then
        "$resolve_fn" "$model"
        [ -n "$effort_override" ] && P_EFFORT="$effort_override"  # see provider_dispatch_run
        if [ -n "$P_MODEL" ]; then
            meta_set "$n" model "$P_MODEL${P_EFFORT:+-$P_EFFORT}"  # resolved model, not the raw alias
        fi
    fi
    _guarded_run "$AGENT_TIMEOUT_SEC" "$fn" "$d" "$session" "$answer" 2>&1 | tee -a "$log" | tail -40
    rc=${PIPESTATUS[0]}   # 124 = killed by the step watchdog (see _guarded_run / finish_step)
}

# shared body for `run` and `test-api` (identical except test-api also tags kind=api-test via
# $task_kind) -- creates the log/meta, dispatches to the provider, finishes the step.
# Expects $name/$engine/$model/$dir/$parent/$prompt (and optionally $task_kind) already set.
_do_run_dispatch() {
    log="$LOGDIR/$name.log"; : > "$log"
    meta_set "$name" engine "$engine"; meta_set "$name" model "${model:-default}"
    meta_set "$name" dir "$dir"; meta_set "$name" state running
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
    # Every resumable provider emits a normalized `session id: ...` line. Capture it
    # generically so adding a provider does not require another engine special-case.
    sid=$(grep -m1 -oE 'session id: [[:alnum:]_.-]+' "$log" 2>/dev/null | cut -d' ' -f3)
    [ -n "$sid" ] && meta_set "$name" session "$sid"
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
        ;;
    fan)
        # Launch N agents IN PARALLEL from one call: each positional prompt becomes its own
        # background `run` task, named <base>-01, <base>-02, ... (base = -t NAME, else the default).
        # Shared -e/-m/-f/-C options apply to all. Returns immediately; poll with `agent.sh list`
        # / `status <name>`. Saves writing a bash loop of backgrounded `run &` calls by hand.
        parse_opts "$@"
        [ ${#REST[@]} -ge 1 ] || die "fan: needs >=1 prompt (agent.sh fan [opts] \"p1\" \"p2\" ...)"
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
            if command -v python >/dev/null 2>&1; then
                last_output "$log" | PYTHONIOENCODING=utf-8 python -c "
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
                last_output "$log" | grep -v '^[[:space:]]*$' | tail -1 > "$out_file"
            fi
            echo "[agent.sh] wrote $out_file" >&2
        fi
        ;;
    reply)
        parse_opts "$@"
        if [ ${#REST[@]} -ge 2 ]; then ref="${REST[0]}"; answer="${REST[1]}"; else ref=""; answer="${REST[0]:-}"; fi
        [ -n "$answer" ] || die "reply: needs an answer text"
        if [ -z "$ref" ]; then tname="$(latest_task)"; [ -n "$tname" ] || die "reply: no tasks — specify name/session id"
        elif [[ "$ref" =~ ^[0-9a-f-]{36}$|^ses_[[:alnum:]_.-]+$|^session_[[:alnum:]_.-]+$ ]]; then tname="$(name_by_session "$ref")"
        else tname="$ref"; fi
        if [ -n "${tname:-}" ]; then
            session="$(resolve_session "$tname")"
            mdir="$(meta_get "$tname" dir)"; [ -n "$mdir" ] && dir="$mdir"
            meng="$(meta_get "$tname" engine)"; [ -n "$meng" ] && [ "$engine_explicit" = 0 ] && engine="$meng"
            log="$LOGDIR/$tname.log"
        else session="$ref"; tname="session-$ref"; log="$LOGDIR/$tname.log"; meta_set "$tname" dir "$dir"; fi
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
        touch "$log"; meta_set "$tname" state running; meta_set "$tname" pid "$$"
        meta_set "$tname" winpid "$(_winpid "$$")"   # $$ is the SHELL's pid, safe inside $( ) — unlike $BASHPID
        meta_set "$tname" timeout ""
        echo "[agent.sh] ▶ reply task=$tname session=$session dir=$dir" >&2
        hdr reply "task=$tname session=$session" ANSWER "$answer" "$log"
        rc=0
        provider_dispatch_resume "$engine" "$dir" "$session" "$answer" "$tname"
        finish_step "$tname" "$rc"
        ;;
    log)
        follow=0; lines=0; lastonly=0
        while [ $# -gt 0 ]; do case "$1" in
            -f) follow=1; shift ;; -n) lines="$2"; shift 2 ;; -l) lastonly=1; shift ;; *) break ;; esac; done
        f="${1:-}"
        if [ -n "$f" ]; then log="$LOGDIR/$f.log"; [ -e "$log" ] || log="$LOGDIR/$f"
        else log="$(ls -t "$LOGDIR"/*.log 2>/dev/null | head -1)"; fi
        [ -e "${log:-}" ] || die "log not found: ${f:-<latest>}"
        if   [ "$follow" = 1 ]; then tail -f "$log"
        elif [ "$lastonly" = 1 ]; then awk '/^========== \[/{buf=""} {buf=buf $0 ORS} END{printf "%s", buf}' "$log"
        elif [ "$lines" -gt 0 ]; then tail -n "$lines" "$log"
        else cat "$log"; fi
        ;;
    last)
        f="${1:-}"
        if [ -n "$f" ]; then log="$LOGDIR/$f.log"; else log="$(ls -t "$LOGDIR"/*.log 2>/dev/null | head -1)"; fi
        [ -e "${log:-}" ] || die "log not found: ${f:-<latest>}"
        last_output "$log"
        ;;
    status)
        n="${1:-$(latest_task)}"; [ -n "$n" ] || die "no tasks"
        [ -e "$(meta_file "$n")" ] || die "no such task: $n"
        st="$(eff_state "$n")"; e="$(meta_get "$n" engine)"; mo="$(meta_get "$n" model)"
        ex="$(meta_get "$n" exit)"; nf="$(meta_get "$n" files)"; s="$(meta_get "$n" session)"; d="$(meta_get "$n" dir)"
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
        [ -n "$(meta_get "$n" timeout)" ] && [ "$st" = error ] && \
            echo "   ⏱ killed by the step watchdog after $(meta_get "$n" timeout)s (AGENT_TIMEOUT_SEC) — raise it, or $(_recovery_hint "$n")"
        if [ -n "$d" ] && [ "${nf:-0}" != 0 ] && git -C "$d" rev-parse --git-dir >/dev/null 2>&1; then
            echo "   --- changed files ---"; git -C "$d" status --porcelain 2>/dev/null | sed 's/^/   /'
        fi
        echo "   --- current step (last lines) ---"
        last_output "$LOGDIR/$n.log" | grep -v '^[[:space:]]*$' | tail -4 | sed 's/^/   /'
        ;;
    list)
        printf '%-2s %-24s %-8s %-9s %-13s %-6s %-6s %s\n' "" TASK STATE ENGINE MODEL AGE FILES SESSION
        for m in $(ls -t "$LOGDIR"/*.meta 2>/dev/null | head -"${1:-20}"); do
            n="$(basename "$m" .meta)"
            e="$(meta_get "$n" engine)"; mo="$(meta_get "$n" model)"; s="$(meta_get "$n" session)"
            st="$(eff_state "$n")"; nf="$(meta_get "$n" files)"
            fm="$(file_mtime "$m")"
            age="$(( ($(date +%s) - ${fm:-0}) / 60 ))m"
            printf '%-2s %-24s %-8s %-9s %-13s %-6s %-6s %s\n' "$(state_icon "$st")" "$n" "${st:-?}" "${e:-?}" "${mo:-?}" "$age" "${nf:-0}" "${s:0:8}"
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
        doctor_tmp="$(mktemp -d "${TMPDIR:-/tmp}/agent-doctor.XXXXXX")" || die "doctor: cannot create temp dir"
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
        PYTHONIOENCODING=utf-8 python - "$doctor_tmp" "$doctor_json" "$doctor_engine_csv" "$doctor_deep_csv" "$deep" <<'PY'
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
        rm -f "$doctor_tmp"/*.json 2>/dev/null || true
        rmdir "$doctor_tmp" 2>/dev/null || true
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
        export AGENT_SH_BASH="$(cygpath -w "$BASH" 2>/dev/null || echo bash)"
        exec python "$(dirname "$0")/gui.py" "$@"
        ;;
    openai-server)
        # OpenAI-compatible /v1/chat/completions bridge over a CLI subagent -- one process =
        # one fixed engine/model/effort; run several (different -e/-m/-f/-p) to compare
        # providers/models side by side. Foreground like `gui`, not backgrounded here.
        export AGENT_SH_BASH="$(cygpath -w "$BASH" 2>/dev/null || echo bash)"
        exec python "$(dirname "$0")/openai_server.py" "$@"
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
            n="$(basename "$f" .meta)"; st="$(eff_state "$n")"
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
