#!/bin/sh
# Python engine; the Bash engine remains available for one migration release.
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ "${AGENT_LEGACY:-0}" = 1 ]; then
    exec bash "$HERE/legacy/agent.sh" "$@"
fi
if command -v py >/dev/null 2>&1 && py -3 -c 'import sys;sys.exit(sys.version_info < (3,8))' >/dev/null 2>&1; then
    exec py -3 "$HERE/agent.py" "$@"
fi
for python_cmd in python python3; do
    if command -v "$python_cmd" >/dev/null 2>&1 && "$python_cmd" -c 'import sys;sys.exit(sys.version_info < (3,8))' >/dev/null 2>&1; then
        exec "$python_cmd" "$HERE/agent.py" "$@"
    fi
done
printf '%s\n' 'neoxider: Python 3.8+ is required. Install Python from python.org and enable Add Python to PATH, then open a new shell.' >&2
exit 127
