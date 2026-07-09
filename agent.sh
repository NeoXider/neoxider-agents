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
#                      waiting tasks; --purge also drops .log/.meta; -n dry-run. Running tasks untouched.
#   agent.sh doctor                                                      — pre-flight: engines + codex limits + claude usage (before fan-out)
#   agent.sh provider-info <engine>                                      — single provider's doctor JSON (used by gui.py)
#   agent.sh openai-server [-e engine] [-m model] [-f effort] [-p port]  — OpenAI-compatible
#                      /v1/chat/completions bridge over a CLI subagent (see openai_server.py's
#                      own docstring for the full contract/caveats). Point any OpenAI-compatible
#                      client -- including CoreAI's COREAI_TEST_BASE_URL -- at its base_url. Run
#                      several with different -e/-m/-f/-p to compare providers side by side.
#                      One process = one ongoing chat session (deterministic continuation via
#                      `reply`, not a fresh agent every call) -- `POST .../reset` clears it.
#
# Models (alias -> real):
#   codex:  5.6-sol|sol|default -> gpt-5.6-sol (effort medium) [DEFAULT]; high -> effort high;
#           luna -> gpt-5.6-luna; terra -> gpt-5.6-terra; 5.5 -> gpt-5.5; spark|5.3 -> gpt-5.3-codex-spark
#           (very simple tasks); anything else -> passed through as-is. (5.6 family needs codex-cli >= 0.144.)
#   claude: sonnet|default -> claude-sonnet-5, effort HIGH [DEFAULT]; sonnet-medium/-low -> lower effort;
#           opus|haiku -> same alias, effort as given (no suffix -> CLI default); <model>-<effort> is the general pattern
#   opencode/gemini: passed through as-is (-m provider/model)
#
# Providers are plugins: each providers/<name>/provider.sh defines provider_<name>_resolve,
# provider_<name>_run_cmd, provider_<name>_resume_cmd (optional), provider_<name>_doctor.
# Adding a new engine = adding one new providers/<name>/ directory, zero edits to this file.
set -uo pipefail

LOGDIR="${AGENT_CLI_LOGS:-$HOME/.claude/agent-cli-logs}"
mkdir -p "$LOGDIR"

die() { echo "agent.sh: $*" >&2; exit 1; }
now() { date '+%Y-%m-%d %H:%M:%S'; }

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
[ -n "$cmd" ] || die "usage: agent.sh run|reply|log|last|status|list|clean|doctor|provider-info|gui|openai-server|help ... (run 'agent.sh help' for the full reference)"
shift

engine="codex"; model=""; effort_override=""; dir="$(pwd)"; name="task-$(date +%Y%m%d-%H%M%S)-$$"; progress=1; terse=1
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
            -e) engine="$2"; shift 2 ;;
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
    grep -m1 -oE 'session id: [0-9a-f-]{36}' "$LOGDIR/$n.log" 2>/dev/null | cut -d' ' -f3
}
name_by_session() { local s="$1" f
    for f in "$LOGDIR"/*.meta; do [ -e "$f" ] || continue
        if grep -q "^session=$s$" "$f"; then basename "$f" .meta; return; fi; done; }
latest_task() { ls -t "$LOGDIR"/*.meta 2>/dev/null | head -1 | xargs -r basename | sed 's/\.meta$//'; }

is_alive() { [ -n "${1:-}" ] && kill -0 "$1" 2>/dev/null; }
# actual state: running with a dead pid -> stalled (computer shut down / process killed)
eff_state() { local n="$1" st; st="$(meta_get "$n" state)"
    if [ "$st" = running ] && ! is_alive "$(meta_get "$n" pid)"; then echo stalled; else echo "$st"; fi; }
state_icon() { case "$1" in running) echo "▶";; done) echo "✔";; waiting) echo "⏳";; error) echo "✖";; stalled) echo "⚠";; *) echo "•";; esac; }

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

# after a step finishes: exit code, changed files, state, question detection
finish_step() {
    local n="$1" rc="$2" log tdir nfiles=0 tail3
    log="$LOGDIR/$n.log"; tdir="$(meta_get "$n" dir)"; [ -n "$tdir" ] || tdir="$dir"
    meta_set "$n" exit "$rc"
    if git -C "$tdir" rev-parse --git-dir >/dev/null 2>&1; then
        nfiles=$(git -C "$tdir" status --porcelain 2>/dev/null | grep -c .)
    fi
    meta_set "$n" files "$nfiles"
    tail3="$(last_output "$log" | grep -v '^[[:space:]]*$' | tail -3)"
    if [ "$rc" -ne 0 ]; then
        meta_set "$n" state error
        echo "[agent.sh] ✖ error exit=$rc  task=$n  (log: agent.sh log $n)" >&2
    elif printf '%s' "$tail3" | grep -qiE '\?[)"'\'' ]*$|should i |do you want|which (one|option|approach|of)|please (confirm|clarify|specify)|let me know|shall i |уточни|подтверд|как (мне |)поступ|какой из'; then
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
    "$fn" "$d" "$P_MODEL" "$P_EFFORT" "$prompt" 2>&1 | tee -a "$log" | tail -40
    rc=${PIPESTATUS[0]}
}

provider_dispatch_resume() {
    local eng="$1" d="$2" session="$3" answer="$4" n="$5" fn="provider_${1}_resume_cmd"
    declare -F "$fn" >/dev/null 2>&1 || die "unknown engine: $eng"
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
    "$fn" "$d" "$session" "$answer" 2>&1 | tee -a "$log" | tail -40
    rc=${PIPESTATUS[0]}
}

# shared body for `run` and `test-api` (identical except test-api also tags kind=api-test via
# $task_kind) -- creates the log/meta, dispatches to the provider, finishes the step.
# Expects $name/$engine/$model/$dir/$parent/$prompt (and optionally $task_kind) already set.
_do_run_dispatch() {
    log="$LOGDIR/$name.log"; : > "$log"
    meta_set "$name" engine "$engine"; meta_set "$name" model "${model:-default}"
    meta_set "$name" dir "$dir"; meta_set "$name" state running
    meta_set "$name" pid "$$"; meta_set "$name" started "$(now)"
    [ -n "$parent" ] && meta_set "$name" parent "$parent"
    [ -n "$task_kind" ] && meta_set "$name" kind "$task_kind"
    echo "[agent.sh] ▶ run task=$name engine=$engine model=${model:-default} dir=$dir" >&2
    hdr run "engine=$engine model=${model:-default} dir=$dir" PROMPT "$prompt" "$log"
    rc=0
    provider_dispatch_run "$engine" "$model" "$dir" "$prompt" "$name"
    if [ "$engine" = codex ]; then
        sid=$(grep -m1 -oE 'session id: [0-9a-f-]{36}' "$log" | cut -d' ' -f3)
        [ -n "$sid" ] && meta_set "$name" session "$sid"
    fi
    finish_step "$name" "$rc"
}

# builds the instructive prompt for `test-api`: exercise a local HTTP API via the agent's own
# shell/tool-use capability (curl et al. -- all four providers' non-interactive modes already
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
        elif [[ "$ref" =~ ^[0-9a-f-]{36}$ ]]; then tname="$(name_by_session "$ref")"
        else tname="$ref"; fi
        if [ -n "${tname:-}" ]; then
            session="$(resolve_session "$tname")"
            mdir="$(meta_get "$tname" dir)"; [ -n "$mdir" ] && dir="$mdir"
            meng="$(meta_get "$tname" engine)"; [ -n "$meng" ] && [ "$engine" = codex ] && engine="$meng"
            log="$LOGDIR/$tname.log"
        else session="$ref"; tname="session-$ref"; log="$LOGDIR/$tname.log"; meta_set "$tname" dir "$dir"; fi
        [ "$progress" = 1 ] && answer="$answer$(progress_proto_reply "$tname")"   # per-task reminder; needs resolved $tname
        [ -n "${session:-}" ] || [ "$engine" = claude ] || die "reply: could not find a session id (task '$tname'); specify uuid explicitly"
        touch "$log"; meta_set "$tname" state running; meta_set "$tname" pid "$$"
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
        live=""; [ "$st" = running ] && live=" (alive, pid $(meta_get "$n" pid))"
        echo "$(state_icon "$st") task=$n  state=${st}${live}  engine=$e/${mo}  exit=${ex:-–}  files=${nf:-0}"
        echo "   dir=$d"; echo "   session=${s:-–}"
        echo "   started=$(meta_get "$n" started)  md=$LOGDIR/$n.md"
        [ "$st" = waiting ]  && echo "   → needs a REPLY: agent.sh reply $n \"...\""
        [ "$st" = stalled ]  && echo "   ⚠ process not alive (computer shut down / killed) — continue: agent.sh reply $n \"continue\""
        [ "$st" = running ]  && echo "   ⟳ still working — follow: agent.sh log -f $n"
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
            age="$(( ($(date +%s) - $(stat -c %Y "$m" 2>/dev/null || echo 0)) / 60 ))m"
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
        # extract a top-level string/bool field from a provider doctor JSON blob (whitespace-tolerant,
        # unlike a hand-rolled grep regex — provider plugins may emit JSON with or without spaces).
        json_field() { PYTHONIOENCODING=utf-8 python -c '
import json, sys
try:
    o = json.loads(sys.argv[2])
except Exception:
    o = {}
v = o.get(sys.argv[1])
print(v if v is not None else "")
' "$1" "$2" 2>/dev/null; }
        echo "=== engines (CLI) ==="
        for pdir in "$PROVIDERS_DIR"/*/; do
            [ -d "$pdir" ] || continue
            eng="$(basename "$pdir")"
            fn="provider_${eng}_doctor"
            declare -F "$fn" >/dev/null 2>&1 || continue
            info="$("$fn" 2>/dev/null)"
            avail="$(json_field available "$info")"
            ver="$(json_field version "$info")"
            if [ "$avail" = "True" ]; then
                printf '  %-9s ok   %s\n' "$eng" "$ver"
            else
                printf '  %-9s —    (not in PATH)\n' "$eng"
            fi
        done
        codex_login="$(json_field login "$(provider_codex_doctor 2>/dev/null)")"
        [ -n "$codex_login" ] && echo "  codex login: $codex_login"
        echo "=== codex rate limits (from latest session) ==="
        codex_info="$(provider_codex_doctor 2>/dev/null)"
        PYTHONIOENCODING=utf-8 python - "$codex_info" <<'PY' 2>/dev/null || echo "  (could not read limits - need python + at least one codex session)"
import json, sys, time
try:
    info = json.loads(sys.argv[1])
except Exception:
    info = {}
rl = info.get("limits")
if not rl:
    print("  no rate-limit data in recent sessions"); raise SystemExit
def fmt(win, lbl):
    if not win: return
    up = win.get('used_percent') or 0; wm = win.get('window_minutes'); ra = win.get('resets_at')
    left = ''
    if ra:
        d = ra - int(time.time())
        left = ('  resets in %dh%02dm' % (d//3600, (d%3600)//60)) if d > 0 else '  resets soon'
    wl = ('%dh' % (wm//60)) if wm and wm % 60 == 0 and wm < 1440 else (('%dd' % (wm//1440)) if wm else '?')
    n = int(up//10); bar = '#'*n + '-'*(10-n)
    print("  %-9s [%s] %4.0f%% (window %s)%s" % (lbl, bar, up, wl, left))
print("  plan: %s" % rl.get('plan_type','?'))
fmt(rl.get('primary'),   'primary')
fmt(rl.get('secondary'), 'secondary')
hi = max((rl.get('primary') or {}).get('used_percent',0), (rl.get('secondary') or {}).get('used_percent',0))
if hi >= 80: print("  WARNING: limits nearly exhausted - hold off on subagent fan-out")
PY
        # Claude exposes no remaining-limit API, so surface the provider's local usage estimate
        # (tokens burned in the 5h / 7d windows) -- previously computed but never printed here.
        if declare -F provider_claude_doctor >/dev/null 2>&1; then
            echo "=== claude usage (local transcript estimate) ==="
            claude_note="$(json_field note "$(provider_claude_doctor 2>/dev/null)")"
            if [ -n "$claude_note" ]; then echo "  $claude_note"; else echo "  (no claude usage data)"; fi
        fi
        ;;
    gui)
        # lightweight local web dashboard over all providers: http://127.0.0.1:<port>
        # native python (win) resolves `bash` to WSL -> pass it the EXACT git-bash path.
        # Only forward an explicit port arg -- if none is given, let gui.py itself fall back
        # to $AGENT_GUI_PORT or its own stable 8765 default (previously this always injected
        # "${1:-8765}", which silently defeated AGENT_GUI_PORT since python always saw an argv).
        export AGENT_SH_BASH="$(cygpath -w "$BASH" 2>/dev/null || echo bash)"
        if [ -n "${1:-}" ]; then
            exec python "$(dirname "$0")/gui.py" "$1"
        else
            exec python "$(dirname "$0")/gui.py"
        fi
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
        # stalled) are touched; running and waiting (needs-reply) tasks are left intact. Only the
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
                running) continue ;;
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
        # truth instead of a duplicated usage string that can drift out of sync.
        sed -n '2,46p' "$0" | sed 's/^# \{0,1\}//'
        ;;
    *) die "unknown command: $cmd (see: agent.sh help)" ;;
esac
