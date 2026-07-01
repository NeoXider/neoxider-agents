#!/usr/bin/env bash
# agent.sh — запуск CLI-сабагентов (codex / claude / opencode / gemini) без интерактива.
# stdin всегда закрыт (</dev/null), чтобы агент не завис на вопросе; ответить можно через `reply`.
#
# Модель «тред на задачу»: каждый run создаёт <name>.log (полный транскрипт) и <name>.meta
# (engine/model/dir/session/state/exit/files). Все reply ДОПИСЫВАЮТСЯ в тот же <name>.log,
# так что весь диалог с сабагентом читается одним файлом.
#
#   agent.sh run    [-e engine] [-m model] [-C dir] [-t name] "prompt"   — новая задача
#   agent.sh reply  [-e engine] [-C dir] [name|session_id] "answer"      — продолжить задачу/сессию
#   agent.sh log    [-f] [-n N] [-l] [name]                              — тред: -f follow, -n N строк, -l последний шаг
#   agent.sh last   [name]                                               — только последний ответ агента
#   agent.sh status [name]                                               — состояние: state/этап/изменённые файлы/нужен ли reply
#   agent.sh list                                                        — таблица задач (state/engine/model/возраст/files)
#   agent.sh doctor                                                      — пре-флайт: движки + лимиты codex (перед фан-аутом)
#
# Модели (alias -> реальная):
#   codex:  5.5|default -> gpt-5.5 (effort medium) [ДЕФОЛТ]; 5.5-high -> effort high;
#           spark|5.3   -> gpt-5.3-codex-spark (очень простые задачи); иное -> как есть
#   claude: sonnet|default -> claude-sonnet-5, effort HIGH [ДЕФОЛТ]; sonnet-medium/-low -> ниже effort;
#           opus|haiku -> тот же alias, effort как передан (без суффикса — CLI-дефолт); <model>-<effort> общий паттерн
#   opencode/gemini: передаётся как есть (-m provider/model)
set -uo pipefail

LOGDIR="${AGENT_CLI_LOGS:-$HOME/.claude/agent-cli-logs}"
mkdir -p "$LOGDIR"

die() { echo "agent.sh: $*" >&2; exit 1; }
now() { date '+%Y-%m-%d %H:%M:%S'; }

cmd="${1:-}"
[ -n "$cmd" ] || die "usage: agent.sh run|reply|log|last|status|list|doctor ... (см. шапку файла)"
shift

engine="codex"; model=""; dir="$(pwd)"; name="task-$(date +%Y%m%d-%H%M%S)"; progress=0
parent="${AGENT_PARENT:-}"   # имя задачи-родителя (для дерева); можно задать env-ом или -P

parse_opts() {
    while [ $# -gt 0 ]; do
        case "$1" in
            -e) engine="$2"; shift 2 ;;
            -m) model="$2"; shift 2 ;;
            -C) dir="$2"; shift 2 ;;
            -t) name="$2"; shift 2 ;;
            -P) parent="$2"; shift 2 ;;
            -p) progress=1; shift ;;
            *) break ;;
        esac
    done
    REST=("$@")
}

# протокол PROGRESS.md: агент сам ведёт чекпоинт в рабочем каталоге -> резюмируемо после выключения
PROGRESS_PROTO='

[Progress protocol] Maintain a file PROGRESS.md in the working directory. If it already exists, read it FIRST and continue from where it left off (do not redo finished steps). As you work, keep it updated with: the goal, a checklist of steps with done/todo status, and any decisions made. Keep it concise. Do NOT run git commit.'

codex_model_args() {
    local alias="${1:-5.5}"; EFFORT="medium"
    case "$alias" in
        ""|5.5|default) M="gpt-5.5" ;;
        5.5-high|high)  M="gpt-5.5"; EFFORT="high" ;;
        spark|5.3|5.3-spark|codex-spark) M="gpt-5.3-codex-spark" ;;
        *) M="$alias" ;;
    esac
}

# claude: дефолт (без -m) -> sonnet + effort HIGH (новый Sonnet 5). Суффикс -low/-medium/-high/-xhigh/-max
# на любом алиасе переопределяет effort; без суффикса opus/haiku идут без --effort (CLI-дефолт).
claude_model_args() {
    local alias="${1:-}" base eff=""
    base="$alias"
    case "$alias" in
        *-low)    base="${alias%-low}";    eff="low" ;;
        *-medium) base="${alias%-medium}"; eff="medium" ;;
        *-high)   base="${alias%-high}";   eff="high" ;;
        *-xhigh)  base="${alias%-xhigh}";  eff="xhigh" ;;
        *-max)    base="${alias%-max}";    eff="max" ;;
    esac
    case "$base" in
        # NB: alias "sonnet" на этом CLI резолвится в устаревший claude-sonnet-4-6 (проверено),
        # поэтому дефолт пинуем на явный id нового Sonnet 5. "opus" alias актуален (-> claude-opus-4-8),
        # его не трогаем.
        ""|default|sonnet) CM="claude-sonnet-5"; [ -z "$eff" ] && eff="high" ;;
        opus)  CM="opus" ;;
        haiku) CM="haiku" ;;
        *) CM="$base" ;;
    esac
    CEFFORT="$eff"
}

# --- meta-сайдкар (key=value) --------------------------------------------
meta_file() { echo "$LOGDIR/$1.meta"; }
meta_set()  { local f; f="$(meta_file "$1")"; touch "$f"
    grep -v "^$2=" "$f" > "$f.tmp" 2>/dev/null || true; echo "$2=$3" >> "$f.tmp"; mv "$f.tmp" "$f"; }
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
# фактическое состояние: running с мёртвым pid -> stalled (комп выключался / процесс убит)
eff_state() { local n="$1" st; st="$(meta_get "$n" state)"
    if [ "$st" = running ] && ! is_alive "$(meta_get "$n" pid)"; then echo stalled; else echo "$st"; fi; }
state_icon() { case "$1" in running) echo "▶";; done) echo "✔";; waiting) echo "⏳";; error) echo "✖";; stalled) echo "⚠";; *) echo "•";; esac; }

hdr() { # kind "info" LABEL "text" logfile
    { echo; echo "========== [$1] $(now) | $2 =========="; echo "> $3:"; echo "$4";
      echo "---------- output ----------"; } >> "$5"; }

# последний блок output (после последнего разделителя)
last_output() { awk '/^---------- output ----------$/{buf=""; next}{buf=buf $0 ORS} END{printf "%s", buf}' "$1"; }

# durable md-чекпоинт задачи: заголовок из meta + весь тред в markdown.
# Переживает выключение компа; по нему (или по codex/claude session) задачу можно продолжить.
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

# после завершения шага: exit-код, изменённые файлы, состояние, детект вопроса
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
        echo "[agent.sh] ⏳ agent, похоже, ЗАДАЛ вопрос — ответь: agent.sh reply $n \"...\"  (вопрос: agent.sh last $n)" >&2
    else
        meta_set "$n" state done
        echo "[agent.sh] ✔ done  task=$n  files=$nfiles  (log: agent.sh log $n | итог: agent.sh last $n)" >&2
    fi
    render_md "$n"
}

case "$cmd" in
    run)
        parse_opts "$@"
        prompt="${REST[0]:-}"; [ -n "$prompt" ] || die "run: нужен prompt"
        [ "$progress" = 1 ] && prompt="$prompt$PROGRESS_PROTO"
        log="$LOGDIR/$name.log"; : > "$log"
        meta_set "$name" engine "$engine"; meta_set "$name" model "${model:-default}"
        meta_set "$name" dir "$dir"; meta_set "$name" state running
        meta_set "$name" pid "$$"; meta_set "$name" started "$(now)"
        [ -n "$parent" ] && meta_set "$name" parent "$parent"
        echo "[agent.sh] ▶ run task=$name engine=$engine model=${model:-default} dir=$dir" >&2
        hdr run "engine=$engine model=${model:-default} dir=$dir" PROMPT "$prompt" "$log"
        rc=0
        case "$engine" in
            codex)
                codex_model_args "$model"
                meta_set "$name" model "$M${EFFORT:+-$EFFORT}"  # резолвнутая модель, не сырой alias
                codex exec -m "$M" -c model_reasoning_effort="$EFFORT" \
                    --sandbox workspace-write --skip-git-repo-check -C "$dir" \
                    "$prompt" </dev/null 2>&1 | tee -a "$log" | tail -40; rc=${PIPESTATUS[0]}
                sid=$(grep -m1 -oE 'session id: [0-9a-f-]{36}' "$log" | cut -d' ' -f3)
                [ -n "$sid" ] && meta_set "$name" session "$sid"
                ;;
            claude)
                claude_model_args "$model"
                meta_set "$name" model "$CM${CEFFORT:+-$CEFFORT}"  # резолвнутая модель, не сырой alias
                cargs=(--model "$CM"); [ -n "$CEFFORT" ] && cargs+=(--effort "$CEFFORT")
                ( cd "$dir" && claude -p "${cargs[@]}" --permission-mode acceptEdits "$prompt" </dev/null 2>&1 ) \
                    | tee -a "$log" | tail -40; rc=${PIPESTATUS[0]}
                ;;
            opencode)
                [ -n "$model" ] && set -- -m "$model" || set --
                ( cd "$dir" && opencode run "$@" "$prompt" </dev/null 2>&1 ) | tee -a "$log" | tail -40; rc=${PIPESTATUS[0]}
                ;;
            gemini)
                [ -n "$model" ] && set -- -m "$model" || set --
                ( cd "$dir" && gemini "$@" -p "$prompt" </dev/null 2>&1 ) | tee -a "$log" | tail -40; rc=${PIPESTATUS[0]}
                ;;
            *) die "неизвестный engine: $engine" ;;
        esac
        finish_step "$name" "$rc"
        ;;
    reply)
        parse_opts "$@"
        if [ ${#REST[@]} -ge 2 ]; then ref="${REST[0]}"; answer="${REST[1]}"; else ref=""; answer="${REST[0]:-}"; fi
        [ -n "$answer" ] || die "reply: нужен текст ответа"
        [ "$progress" = 1 ] && answer="$answer$PROGRESS_PROTO"
        if [ -z "$ref" ]; then tname="$(latest_task)"; [ -n "$tname" ] || die "reply: нет задач — укажи имя/session id"
        elif [[ "$ref" =~ ^[0-9a-f-]{36}$ ]]; then tname="$(name_by_session "$ref")"
        else tname="$ref"; fi
        if [ -n "${tname:-}" ]; then
            session="$(resolve_session "$tname")"
            mdir="$(meta_get "$tname" dir)"; [ -n "$mdir" ] && dir="$mdir"
            meng="$(meta_get "$tname" engine)"; [ -n "$meng" ] && [ "$engine" = codex ] && engine="$meng"
            log="$LOGDIR/$tname.log"
        else session="$ref"; tname="session-$ref"; log="$LOGDIR/$tname.log"; meta_set "$tname" dir "$dir"; fi
        [ -n "${session:-}" ] || [ "$engine" = claude ] || die "reply: не нашёл session id (задача '$tname'); укажи uuid явно"
        touch "$log"; meta_set "$tname" state running; meta_set "$tname" pid "$$"
        echo "[agent.sh] ▶ reply task=$tname session=$session dir=$dir" >&2
        hdr reply "task=$tname session=$session" ANSWER "$answer" "$log"
        rc=0
        case "$engine" in
            codex)
                ( cd "$dir" && codex exec resume --skip-git-repo-check \
                    -c 'sandbox_mode="workspace-write"' "$session" "$answer" </dev/null 2>&1 ) \
                    | tee -a "$log" | tail -40; rc=${PIPESTATUS[0]} ;;
            claude)
                # claude не логирует session id в текстовом режиме -> при отсутствии session fallback на --continue
                claude_model_args "$model"
                meta_set "$tname" model "$CM${CEFFORT:+-$CEFFORT}"  # резолвнутая модель, не сырой alias
                cargs=(--model "$CM"); [ -n "$CEFFORT" ] && cargs+=(--effort "$CEFFORT")
                if [ -n "${session:-}" ]; then cargs+=(--resume "$session"); else cargs+=(--continue); fi
                ( cd "$dir" && claude -p "${cargs[@]}" --permission-mode acceptEdits "$answer" </dev/null 2>&1 ) \
                    | tee -a "$log" | tail -40; rc=${PIPESTATUS[0]} ;;
            *) die "reply поддержан для codex и claude" ;;
        esac
        finish_step "$tname" "$rc"
        ;;
    log)
        follow=0; lines=0; lastonly=0
        while [ $# -gt 0 ]; do case "$1" in
            -f) follow=1; shift ;; -n) lines="$2"; shift 2 ;; -l) lastonly=1; shift ;; *) break ;; esac; done
        f="${1:-}"
        if [ -n "$f" ]; then log="$LOGDIR/$f.log"; [ -e "$log" ] || log="$LOGDIR/$f"
        else log="$(ls -t "$LOGDIR"/*.log 2>/dev/null | head -1)"; fi
        [ -e "${log:-}" ] || die "лог не найден: ${f:-<последний>}"
        if   [ "$follow" = 1 ]; then tail -f "$log"
        elif [ "$lastonly" = 1 ]; then awk '/^========== \[/{buf=""} {buf=buf $0 ORS} END{printf "%s", buf}' "$log"
        elif [ "$lines" -gt 0 ]; then tail -n "$lines" "$log"
        else cat "$log"; fi
        ;;
    last)
        f="${1:-}"
        if [ -n "$f" ]; then log="$LOGDIR/$f.log"; else log="$(ls -t "$LOGDIR"/*.log 2>/dev/null | head -1)"; fi
        [ -e "${log:-}" ] || die "лог не найден: ${f:-<последний>}"
        last_output "$log"
        ;;
    status)
        n="${1:-$(latest_task)}"; [ -n "$n" ] || die "нет задач"
        [ -e "$(meta_file "$n")" ] || die "нет задачи: $n"
        st="$(eff_state "$n")"; e="$(meta_get "$n" engine)"; mo="$(meta_get "$n" model)"
        ex="$(meta_get "$n" exit)"; nf="$(meta_get "$n" files)"; s="$(meta_get "$n" session)"; d="$(meta_get "$n" dir)"
        live=""; [ "$st" = running ] && live=" (alive, pid $(meta_get "$n" pid))"
        echo "$(state_icon "$st") task=$n  state=${st}${live}  engine=$e/${mo}  exit=${ex:-–}  files=${nf:-0}"
        echo "   dir=$d"; echo "   session=${s:-–}"
        echo "   started=$(meta_get "$n" started)  md=$LOGDIR/$n.md"
        [ "$st" = waiting ]  && echo "   → нужен ОТВЕТ: agent.sh reply $n \"...\""
        [ "$st" = stalled ]  && echo "   ⚠ процесс не жив (комп выключался / убит) — продолжить: agent.sh reply $n \"continue\""
        [ "$st" = running ]  && echo "   ⟳ ещё работает — следить: agent.sh log -f $n"
        if [ -n "$d" ] && [ "${nf:-0}" != 0 ] && git -C "$d" rev-parse --git-dir >/dev/null 2>&1; then
            echo "   --- изменённые файлы ---"; git -C "$d" status --porcelain 2>/dev/null | sed 's/^/   /'
        fi
        echo "   --- текущий этап (последние строки) ---"
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
    doctor)
        echo "=== движки (CLI) ==="
        for eng in codex claude opencode gemini; do
            if command -v "$eng" >/dev/null 2>&1; then
                ver="$("$eng" --version 2>&1 | head -1)"
                printf '  %-9s ok   %s\n' "$eng" "$ver"
            else
                printf '  %-9s —    (не в PATH)\n' "$eng"
            fi
        done
        command -v codex >/dev/null 2>&1 && { echo "  codex login: $(codex login status 2>&1 | head -1)"; }
        echo "=== codex rate limits (from latest session) ==="
        PYTHONIOENCODING=utf-8 python - <<'PY' 2>/dev/null || echo "  (could not read limits - need python + at least one codex session)"
import json, glob, os, time
files = sorted(glob.glob(os.path.expanduser('~/.codex/sessions/**/*.jsonl'), recursive=True), key=os.path.getmtime)[-8:]
rl = None
def find(d):
    if isinstance(d, dict):
        if 'rate_limits' in d: return d['rate_limits']
        for v in d.values():
            r = find(v)
            if r: return r
    return None
for f in files:
    try:
        for line in open(f, encoding='utf-8', errors='ignore'):
            if '"rate_limits"' in line:
                try: o = json.loads(line)
                except Exception: continue
                r = find(o)
                if r: rl = r
    except Exception:
        pass
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
        ;;
    gui)
        # лёгкий локальный веб-пульт над всеми провайдерами: http://127.0.0.1:<port>
        # python (нативный win) резолвит `bash` в WSL -> передаём ему ТОЧНЫЙ путь git-bash.
        export AGENT_SH_BASH="$(cygpath -w "$BASH" 2>/dev/null || echo bash)"
        exec python "$(dirname "$0")/gui.py" "${1:-8765}"
        ;;
    *) die "неизвестная команда: $cmd" ;;
esac
