#!/usr/bin/env bash
# Native Windows PIDs are outside the old MSYS PID namespace. Keep mixed-state
# controls in Python so legacy liveness checks cannot claim a live native owner.
_legacy_native_control() {
    local cmd="$1"; shift
    case "$cmd" in
        send|reply|stop|restart|peek|log|last|result|wait|status|list|pending|clean|prune) ;;
        *) return 0 ;;
    esac
    local path line native=0
    for path in "$LOGDIR/"*.meta; do
        [ -f "$path" ] || continue
        while IFS= read -r line; do
            case "$line" in core_version=2) native=1; break ;; esac
        done < "$path"
        [ "$native" = 0 ] || break
    done
    [ "$native" = 1 ] || return 0
    local python_cmd
    if command -v py >/dev/null 2>&1 && py -3 -c 'import sys;sys.exit(sys.version_info < (3,8))' >/dev/null 2>&1; then
        exec py -3 "$HERE/../agent.py" "$cmd" "$@"
    fi
    for python_cmd in python python3; do
        if command -v "$python_cmd" >/dev/null 2>&1 && "$python_cmd" -c 'import sys;sys.exit(sys.version_info < (3,8))' >/dev/null 2>&1; then
            exec "$python_cmd" "$HERE/../agent.py" "$cmd" "$@"
        fi
    done
    die 'Python 3.8+ is required to control native tasks. Install Python from python.org and add it to PATH.'
}
