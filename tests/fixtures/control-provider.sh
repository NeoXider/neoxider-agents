# Offline provider for lifecycle tests. Each turn records its exact prompt and session.
provider_fixture_resolve() { P_MODEL="${1:-fixture}"; P_EFFORT=""; }
PROVIDER_FIXTURE_RESUME_NEEDS_MODEL=1
# Stretch temp-file writes so a test observer can detect premature publication reliably.
printf() {
    if [ "${FIXTURE_SLOW_INBOX:-0}" = 1 ] && [ "${1:-}" = '%s' ]; then
        case "${2:-}" in FIRST*|SECOND*)
            local text="$2" half=$(( ${#2} / 2 ))
            builtin printf '%s' "${text:0:half}"
            sleep 0.5
            builtin printf '%s' "${text:half}"
            return ;;
        esac
    fi
    builtin printf "$@"
}
provider_fixture_run_cmd() {
    local sid="ses_fixture_${BASHPID:-$$}"
    printf 'session id: %s\n' "$sid"
    _fixture_turn "$1" "$4" "$sid"
}
provider_fixture_resume_cmd() { _fixture_turn "$1" "$3" "$2"; }
_fixture_turn() {
    local d="$1" text="$2" sid="$3" delay="${FIXTURE_DELAY:-0}" turn=1 tick=0
    [ ! -f "$d/turns" ] || turn=$(( $(wc -l < "$d/turns") + 1 ))
    printf '%s\n' "$text" >> "$d/prompts"
    printf '%s\n' "$sid" >> "$d/sessions"
    printf '%s' "$text" > "$d/turn.$turn.prompt"
    printf '%s' "$sid" > "$d/turn.$turn.session"
    printf 'partial edit survives\n' > "$d/partial.txt"
    printf 'started\n' >> "$d/turns"
    printf '{"type":"item.started","item":{"type":"command_execution","command":"sleep %s"}}\n' "$delay"
    if [ "${FIXTURE_NATIVE_GRANDCHILD:-0}" = 1 ]; then
        "$FIXTURE_PYTHON" -c 'import pathlib, subprocess, sys
d = pathlib.Path(sys.argv[1])
marker = d / "native-child.pid"
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(90)", str(marker)],
                         stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
marker.write_text(str(child.pid))
ready = d / (".turn." + sys.argv[2] + ".started")
ready.write_text("ready")
ready.replace(d / ("turn." + sys.argv[2] + ".started"))
child.wait()' "$d" "$turn"
    fi
    printf 'ready\n' > "$d/.turn.$turn.started"
    mv "$d/.turn.$turn.started" "$d/turn.$turn.started"
    case " ${FIXTURE_BLOCK_TURNS:-} " in *" $turn "*)
        while [ ! -f "$d/turn.$turn.release" ]; do
            sleep 0.1
            tick=$((tick+1))
            [ "$tick" -lt 900 ] || return 99
        done ;;
    esac
    sleep "$delay"
    [ ! -f "$d/fail-turn-$turn" ] || { printf 'fixture delivery failed\n'; return 42; }
    case " ${FIXTURE_FAIL_TURNS:-} " in *" $turn "*) printf 'fixture delivery failed\n'; return 42 ;; esac
    printf '%s\n' "$text" >> "$d/delivered"
    printf 'finished\n' > "$d/turn.$turn.finished"
    printf '%s\n%s\n' '---------- output ----------' "ANSWER: $text"
    if [ "${FIXTURE_ORPHAN_STDOUT:-0}" = 1 ]; then
        "$FIXTURE_PYTHON" -c 'import os, pathlib, subprocess, sys
flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", sys.argv[1]],
                         stdin=subprocess.DEVNULL, creationflags=flags)
pathlib.Path(sys.argv[1]).write_text(str(child.pid))' "$d/orphan.pid"
    fi
}

# Source-only portability checks: the absent /proc PID is never passed to a real kill.
_fixture_portability_stop() {
    local scenario="$1" fake_pid=73199999 stamp expected
    local fixture_stamp='Mon Oct  5 12:34:56 2026' fixture_command='bash /fixture/agent.sh run -t task ORIGINAL'
    local fixture_ps_unavailable=0
    [ ! -e "/proc/$fake_pid" ] || return 97
    ps() {
        [ "$fixture_ps_unavailable" = 0 ] || return 1
        case "$*" in
            *lstart*) builtin printf '%s\n' "$fixture_stamp" ;;
            *command*|*args*) builtin printf '%s\n' "$fixture_command" ;;
            *) return 1 ;;
        esac
    }
    is_alive() { [ "$1" != "$fake_pid" ] || return 0; builtin kill -0 "$1" 2>/dev/null; }
    eff_state() { meta_get "$1" state; }
    _kill_tree() { builtin printf '%s\n' "$1" >> "$LOGDIR/killed"; }
    stamp="$(_pid_stamp "$fake_pid")"
    case "$scenario" in
        stamp) builtin printf 'STAMP=%s\n' "$stamp"; return 0 ;;
        mismatch) expected='different-start-time' ;;
        valid) [ -n "$stamp" ] || return 98; expected="$stamp" ;;
        legacy) expected='' ;;
        unavailable) expected='known-start-time'; fixture_ps_unavailable=1 ;;
        unavailable-legacy) expected=''; fixture_ps_unavailable=1 ;;
        *) return 96 ;;
    esac
    meta_set_many task engine fixture session ses_portable state running pid "$fake_pid" pid_start "$expected" dir "$LOGDIR" parent portability-test
    _stop_task task
}
