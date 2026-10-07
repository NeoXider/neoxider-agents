# Task controls. Sourced by agent.sh after its metadata/provider helpers.
# Execution ownership and inbox publication use separate locks: send never waits on a model.
command_help() {
    local usage
    case "$1" in
        run) usage='run [-e ENGINE] [-m MODEL] [-f EFFORT] [-C DIR] [-t NAME] [--no-progress] [--no-terse] (TEXT | --prompt-file F)' ;;
        fan) usage='fan [-e ENGINE] [-m MODEL] [-f EFFORT] [-C DIR] [-t BASE] TEXT...' ;;
        send|reply) usage="$1 NAME [--now] [-e ENGINE] [-m MODEL] [-f EFFORT] [-C DIR] (TEXT | --prompt-file F)\n       agent.sh $1 --flush NAME" ;;
        stop) usage='stop NAME... | --all-mine' ;;
        restart) usage='restart NAME [TEXT | --prompt-file F] [--fresh] [-C DIR]' ;;
        peek) usage='peek NAME [-n N] [-f] [--raw]' ;;
        log) usage='log [NAME] [-f] [-n N] [-l]' ;;
        wait) usage='wait [NAME...] [--timeout SECONDS] [--poll SECONDS]' ;;
        pending) usage='pending [--strict]' ;;
        clean|prune) usage="$1 [--all] [--purge] [-n|--dry-run]" ;;
        list) usage='list [LIMIT]' ;;
        status|last) usage="$1 [NAME]" ;;
        doctor) usage='doctor [--deep|--json]' ;;
        provider-info) usage='provider-info ENGINE' ;;
        test-api) usage='test-api --base-url URL --goal TEXT [--out F] [-e ENGINE] [-m MODEL] [-f EFFORT] [-C DIR] [-t NAME]' ;;
        gui) usage='gui [PORT] [--lan] [--localhost] [--token SECRET]' ;;
        openai-server) usage='openai-server [-e ENGINE] [-m MODEL] [-f EFFORT] [-p PORT] [--api-key SECRET] (see openai_server.py --help)' ;;
        *) usage='help (full command reference)' ;;
    esac
    printf 'Usage: agent.sh %b\n' "$usage"
}

# Validate before dispatch, and normalize legacy hand-written parsers to options-first.
# A literal -- ends parsing; option-looking text beyond it stays text.
_validate_cli_args() {
    local a allowed='' operands='' ended=0
    local -a opts=() args=()
    case "${1:-}" in --help|-h) command_help "$cmd"; exit 0 ;; esac
    case "$cmd" in
        run|fan|test-api|send|reply|restart)
            allowed=' -e -m -f -C -t -P -p --no-progress --no-terse --verbose --prompt-file '
            operands=' -e -m -f -C -t -P --prompt-file '
            case "$cmd" in
                test-api) allowed+=' --base-url --goal --out '; operands+=' --base-url --goal --out ' ;;
                send|reply) allowed+=' --now --flush ' ;;
                restart) allowed+=' --fresh ' ;;
            esac ;;
        log) allowed=' -f -n -l '; operands=' -n ' ;;
        peek) allowed=' -f -n --raw '; operands=' -n ' ;;
        stop) allowed=' --all-mine ' ;;
        wait) allowed=' --timeout --poll '; operands="$allowed" ;;
        pending) allowed=' --strict ' ;;
        clean|prune) allowed=' --all --purge -n --dry-run ' ;;
        doctor) allowed=' --deep --json ' ;;
        gui) allowed=' --lan --localhost --token '; operands=' --token ' ;;
        openai-server)
            allowed=' -e --engine -m --model -f --effort -C --dir -p --port --host --lan --localhost --timeout --retries --no-live-stream --api-key --session-ttl '
            operands=' -e --engine -m --model -f --effort -C --dir -p --port --host --timeout --retries --api-key --session-ttl ' ;;
    esac
    while [ "$#" -gt 0 ]; do
        a="$1"; shift
        if [ "$ended" = 1 ]; then args+=("$a"); continue; fi
        case "$a" in
            --) ended=1 ;;
            --help|-h) command_help "$cmd"; exit 0 ;;
            -*)
                if [[ "$a" == --*=* ]] && { [ "$cmd" = gui ] || [ "$cmd" = openai-server ]; }; then
                    case "$operands" in *" ${a%%=*} "*) opts+=("$a"); continue ;; esac
                fi
                case "$allowed" in *" $a "*) ;; *) command_help "$cmd" >&2; die "$cmd: unknown option '$a'" ;; esac
                opts+=("$a")
                case "$operands" in *" $a "*)
                    [ "$#" -gt 0 ] || { command_help "$cmd" >&2; die "$cmd: option '$a' needs an operand"; }
                    opts+=("$1"); shift ;;
                esac ;;
            *) args+=("$a") ;;
        esac
    done
    case "$cmd" in run|fan|test-api|send|reply|restart|stop) CLI_ARGS=("${opts[@]}" -- "${args[@]}") ;;
        *) CLI_ARGS=("${opts[@]}" "${args[@]}") ;;
    esac
}

inbox_count() {
    local f count=0
    for f in "$LOGDIR/$1.inbox/"*.msg; do [ -f "$f" ] && count=$((count+1)); done
    printf '%s' "$count"
}

_send_error() {
    local message="$*"
    message="${message//$'\n'/ }"; message="${message//$'\r'/ }"
    if [ -n "${tname:-}" ] && [ -f "$(meta_file "$tname")" ]; then
        meta_set "$tname" last_send_error "$message"
    fi
    command_help "$cmd" >&2
    die "$cmd: $message"
}

_resolve_control_task() {
    local ref="${1:-}" found=""
    if [ -z "$ref" ]; then found="$(latest_task)"
    elif valid_task_name "$ref" && [ -f "$(meta_file "$ref")" ]; then found="$ref"
    else found="$(name_by_session "$ref")"; fi
    if [ -z "$found" ] && [[ "$ref" =~ ^[0-9a-f-]{36}$|^ses_[[:alnum:]_.-]+$|^session_[[:alnum:]_.-]+$ ]]; then
        tname="session-$ref"; require_task_name "$tname"
        CONTROL_DIRECT_SESSION="$ref"; session="$ref"; log="$LOGDIR/$tname.log"
        return 0
    fi
    [ -n "$found" ] || { tname=""; _send_error "no such task '$ref'"; }
    tname="$found"; session="$(resolve_session "$tname")"
    [ "$engine_explicit" = 1 ] || engine="$(meta_get "$tname" engine)"
    [ "$dir_explicit" = 1 ] || dir="$(meta_get "$tname" dir)"
    log="$LOGDIR/$tname.log"
}

_can_resume() {
    local eng="$1" config="$PROVIDERS_DIR/$1/provider.json"
    declare -F "provider_${eng}_resume_cmd" >/dev/null 2>&1 || return 1
    [ -f "$config" ] && grep -qE '"supports_resume"[[:space:]]*:[[:space:]]*false' "$config" && return 1
    return 0
}

_resume_preflight() {
    _can_resume "$engine" || _send_error "engine '$engine' cannot resume (supports_resume=false); start fresh with run or restart --fresh"
    session="${CONTROL_DIRECT_SESSION:-$(resolve_session "$tname")}"
    [ -n "$session" ] || _send_error "could not find a session id (task '$tname'); start fresh"
    [ -d "$dir" ] || _send_error "working directory does not exist: $dir"
    prompt_fits_engine "$engine" "$answer" || _send_error "prompt cannot be carried by engine '$engine'"
    local prepare_fn="provider_${engine}_prepare_resume" prepare_rc=0
    PROVIDER_PREPARE_NOTE=""
    if declare -F "$prepare_fn" >/dev/null 2>&1; then
        "$prepare_fn" "$session" "$tname" || prepare_rc=$?
        [ "$prepare_rc" = 0 ] || _send_error "provider preflight failed (exit=$prepare_rc): ${PROVIDER_PREPARE_NOTE:-session writer did not become available}"
    fi
}

# Caller holds the inbox lock. Sequence survives acknowledgements, stops and restarts.
_inbox_append_locked() {
    local n="$1" text="$2" box="$LOGDIR/$1.inbox" seq=0 tmp file existing num
    ( umask 077; mkdir -p "$box" ) || return 1
    [ -r "$box/sequence" ] && read -r seq < "$box/sequence"
    case "$seq" in ''|*[!0-9]*) return 1 ;; esac
    for existing in "$box/"*.msg; do
        [ -f "$existing" ] || continue
        num="${existing##*/}"; num="${num%.msg}"
        [ "$((10#$num))" -le "$seq" ] || seq="$((10#$num))"
    done
    seq=$((seq+1)); printf -v file '%012d.msg' "$seq"
    tmp="$box/.message.$BASHPID.${RANDOM:-0}"
    ( umask 077; printf '%s' "$text" > "$tmp" ) && mv "$tmp" "$box/$file" || return 1
    # Recover the counter from published messages if interrupted between these two renames.
    printf '%s\n' "$seq" > "$box/.sequence.$BASHPID"
    mv "$box/.sequence.$BASHPID" "$box/sequence" || return 1
    QUEUED_SEQUENCE="$seq"
}

_inbox_batch_locked() {
    BATCH_FILES=(); BATCH_TEXT=""
    local f seq
    for f in "$LOGDIR/$1.inbox/"*.msg; do
        [ -f "$f" ] || continue
        BATCH_FILES+=("$f"); seq="${f##*/}"; seq="${seq%.msg}"
        BATCH_TEXT="$BATCH_TEXT${BATCH_TEXT:+$'\n\n'}Message #$((10#$seq)):"$'\n'"$(cat "$f")"
    done
}

_claim_task() {
    _meta_lock "$LOGDIR/$1.owner" || die "cannot claim task '$1'"
    TASK_OWNER_STATE="$_META_LOCK_STATE"; TASK_OWNER_TOKEN="$_META_LOCK_TOKEN"; TASK_OWNER_NAME="$1"
    trap '_release_task' EXIT
}

_pid_stamp() {
    local stat_line rest stamp
    local -a fields
    if [ ! -r "/proc/$1/stat" ]; then
        stamp="$(LC_ALL=C TZ=UTC ps -p "$1" -o lstart= 2>/dev/null)"
        stamp="${stamp#"${stamp%%[![:space:]]*}"}"
        stamp="${stamp%"${stamp##*[![:space:]]}"}"
        [ -z "$stamp" ] || printf 'ps:%s' "$stamp"
        return 0
    fi
    IFS= read -r stat_line < "/proc/$1/stat" || return 0
    rest="${stat_line##*) }"; read -r -a fields <<< "$rest"
    printf '%s' "${fields[19]:-}"
}

_pid_command() {
    if [ -r "/proc/$1/cmdline" ]; then
        tr '\0' ' ' < "/proc/$1/cmdline" 2>/dev/null
    else
        LC_ALL=C ps -ww -p "$1" -o command= 2>/dev/null
    fi
}

_release_task() {
    [ -n "${TASK_OWNER_STATE:-}" ] || return 0
    _META_LOCK_STATE="$TASK_OWNER_STATE"; _META_LOCK_TOKEN="$TASK_OWNER_TOKEN"
    _meta_unlock "$LOGDIR/$TASK_OWNER_NAME.owner"
    TASK_OWNER_STATE=""
}

_start_resume_turn() {
    local n="$1" text="$2" step_pid="$BASHPID" allow_stopped="${3:-1}" lock="$LOGDIR/$1.inbox"
    _secure_state_file "$log" || _send_error "cannot open protected log"
    _meta_lock "$lock"
    if [ "$allow_stopped" = 0 ] && [ "$(meta_get "$n" state)" = stopped ]; then
        _meta_unlock "$lock"; rc=0; return 1
    fi
    if [ ! -f "$(meta_file "$n")" ]; then
        meta_set_many "$n" engine "$engine" dir "$dir" session "$session" model "${model:-default}" started "$(now)"
    fi
    meta_set_many "$n" state running engine "$engine" dir "$dir" pid "$step_pid" pid_start "$(_pid_stamp "$step_pid")" winpid "$(_winpid "$step_pid")" timeout "" reason "" exit "" last_send_error "" \
        || { _meta_unlock "$lock"; _send_error "cannot initialize send metadata"; }
    _meta_unlock "$lock"
    export AGENT_ACTIVITY_FILE="$LOGDIR/$n.activity.jsonl"
    hdr reply "task=$n session=$session" ANSWER "$text" "$log"
    echo "[agent.sh] ▶ send task=$n session=$session dir=$dir" >&2
    rc=0
    provider_dispatch_resume "$engine" "$dir" "$session" "$text" "$n"
}

# No settled state is published until the inbox is empty under its publication lock.
# A failed delivery retains its batch for manual recovery; a bounded drain avoids starvation.
_finish_with_inbox() {
    local n="$1" turns=0 limit="${AGENT_INBOX_MAX_TURNS:-32}" lock="$LOGDIR/$1.inbox" f delivery_rc
    case "$limit" in ''|*[!0-9]*|0) limit=32 ;; esac
    while :; do
        _meta_lock "$lock"
        if [ "$(meta_get "$n" state)" = stopped ]; then
            _meta_unlock "$lock"; return 0
        fi
        _inbox_batch_locked "$n"
        if [ "${#BATCH_FILES[@]}" = 0 ]; then
            finish_step "$n" "$rc"; delivery_rc=$?
            _meta_unlock "$lock"; return "$delivery_rc"
        fi
        if [ "$turns" -ge "$limit" ] || ! _can_resume "$engine"; then
            meta_set_many "$n" state waiting exit "$rc" reason "$(inbox_count "$n") undelivered message(s); send --flush $n"
            echo "[agent.sh] ⏳ waiting: $n; $(inbox_count "$n") undelivered message(s); send --flush $n" >&2
            render_md "$n"
            _meta_unlock "$lock"; return "$rc"
        fi
        local -a delivered=("${BATCH_FILES[@]}")
        answer="$BATCH_TEXT"
        _meta_unlock "$lock"
        session="$(resolve_session "$n")"
        if [ -z "$session" ]; then
            rc=3; finish_step "$n" "$rc"; return "$rc"
        fi
        _start_resume_turn "$n" "$answer" 0 || return 0
        turns=$((turns+1))
        _meta_lock "$lock"
        if [ "$rc" = 0 ] && [ "$(meta_get "$n" state)" != stopped ]; then
            for f in "${delivered[@]}"; do rm -f -- "$f"; done
            _meta_unlock "$lock"
        else
            [ "$(meta_get "$n" state)" = stopped ] || finish_step "$n" "$rc"
            _meta_unlock "$lock"; return "$rc"
        fi
    done
}

_stop_task() {
    local n="$1" lock="$LOGDIR/$1.inbox" st pid sid expected actual cmdline
    require_task_name "$n"
    [ -f "$(meta_file "$n")" ] || die "stop: no such task '$n'"
    _meta_lock "$lock"
    st="$(eff_state "$n")"; pid="$(meta_get "$n" pid)"
    case "$st" in
        running|idle|stalled)
            if [ -n "$pid" ] && is_alive "$pid"; then
                expected="$(meta_get "$n" pid_start)"; actual="$(_pid_stamp "$pid")"
                if [ -n "$expected" ]; then
                    [ "$expected" = "$actual" ] || { _meta_unlock "$lock"; die "stop: refusing reused/unverifiable pid '$pid' for '$n'"; }
                else
                    cmdline="$(_pid_command "$pid")"
                    case "$cmdline" in *agent.sh*) ;; *) _meta_unlock "$lock"; die "stop: cannot verify legacy wrapper pid '$pid' for '$n'" ;; esac
                fi
            fi
            sid="$(resolve_session "$n")"
            meta_set_many "$n" state stopped reason "stopped by orchestrator" session "$sid"
            # The recorded wrapper is the root of the owned process tree. Never kill our caller.
            if [ -n "$pid" ] && [ "$pid" != "$$" ] && is_alive "$pid"; then _kill_tree "$pid"; fi
            echo "[agent.sh] ⏹ stopped: $n (session kept)" ;;
        *) echo "[agent.sh] $n already ${st:-finished}; OK" ;;
    esac
    _meta_unlock "$lock"
}

_task_is_mine() {
    local task_parent="$(meta_get "$1" parent)"
    if [ -n "$parent" ]; then [ "$task_parent" = "$parent" ]
    else [ -n "${AGENT_ORCHESTRATOR_ID:-}" ] && [ "$(meta_get "$1" orchestrator)" = "$AGENT_ORCHESTRATOR_ID" ]; fi
}

_control_send() {
    _resolve_control_task "$ref"
    _can_resume "$engine" || _send_error "engine '$engine' cannot resume (supports_resume=false); start fresh"
    local lock="$LOGDIR/$tname.inbox" st
    [ -d "$dir" ] || _send_error "working directory does not exist: $dir"
    prompt_fits_engine "$engine" "$answer" || _send_error "prompt cannot be carried by engine '$engine'"
    _meta_lock "$lock"
    st="$(eff_state "$tname")"
    case "$st" in
        running|idle)
            if [ "$send_flush" = 1 ]; then
                _meta_unlock "$lock"; echo "[agent.sh] $tname running; inbox will drain after turn"; return 0
            fi
            if [ "$send_now" = 1 ] && [ -z "$(resolve_session "$tname")" ]; then
                _meta_unlock "$lock"
                _send_error "session id is not available yet; use send without --now to queue while the current turn starts"
            fi
            _inbox_append_locked "$tname" "$answer" || { _meta_unlock "$lock"; _send_error "cannot publish inbox message"; }
            _meta_unlock "$lock"
            echo "[agent.sh] queued (#$QUEUED_SEQUENCE) task=$tname"
            [ "$send_now" = 1 ] || return 0
            _stop_task "$tname"
            send_flush=1 ;;
        *) _meta_unlock "$lock" ;;
    esac
    _claim_task "$tname"
    if [ "$send_flush" = 1 ]; then
        _meta_lock "$lock"; _inbox_batch_locked "$tname"; _meta_unlock "$lock"
        [ "${#BATCH_FILES[@]}" -gt 0 ] || { echo "[agent.sh] inbox empty: $tname"; _release_task; return 0; }
        answer="$BATCH_TEXT"
    fi
    _resume_preflight
    # Publish every message before launching: even a caller crash cannot lose a follow-up.
    _meta_lock "$lock"
    if [ "$send_flush" != 1 ]; then _inbox_append_locked "$tname" "$answer" || { _meta_unlock "$lock"; _send_error "cannot publish inbox message"; }; fi
    _inbox_batch_locked "$tname"
    local -a delivered=("${BATCH_FILES[@]}")
    answer="$BATCH_TEXT"
    _meta_unlock "$lock"
    _start_resume_turn "$tname" "$answer"
    if [ "$rc" = 0 ]; then
        _meta_lock "$lock"
        [ "$(meta_get "$tname" state)" = stopped ] || rm -f -- "${delivered[@]}"
        _meta_unlock "$lock"
        _finish_with_inbox "$tname"; local result=$?
    else
        _meta_lock "$lock"
        finish_step "$tname" "$rc"; local result=$?
        _meta_unlock "$lock"
    fi
    _release_task
    return "$result"
}

activity_summary() {
    _agent_python || return 0
    local result kind age
    result="$(PYTHONIOENCODING=utf-8 "$_AGENT_PY" "$HERE/activity.py" "$LOGDIR/$1.log" --summary 2>/dev/null)"
    [ -n "$result" ] || return 0
    kind="${result%%|*}"; age="${result#*|}"
    printf 'last activity=%s (%ss ago)' "$kind" "$age"
}
