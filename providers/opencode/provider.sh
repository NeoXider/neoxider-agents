# opencode provider plugin for agent.sh.
# Contract: provider_opencode_run_cmd, provider_opencode_doctor.
# No provider_opencode_resolve (opencode has no alias->model resolution layer): the raw
# -m value (format provider/model, e.g. zai/glm-4.6, opencode/hy3-free, lmstudio/...) is passed
# straight through; meta model= stays "default" otherwise. Discover models with `opencode models`.
# No provider_opencode_resume_cmd: reply was never supported for opencode (matches today).

# _provider_opencode_python — first working python (mirrors codex's finder) for the JSON emitter.
_provider_opencode_python() {
    [ -n "${_PROVIDER_OPENCODE_PY:-}" ] && { [ "$_PROVIDER_OPENCODE_PY" = NONE ] && return 1 || return 0; }
    local c
    for c in "${AGENT_PYTHON:-}" python3 python py; do
        [ -n "$c" ] || continue
        command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys' >/dev/null 2>&1 && { _PROVIDER_OPENCODE_PY="$c"; return 0; }
    done
    _PROVIDER_OPENCODE_PY=NONE; return 1
}

# _provider_opencode_emit — reads opencode `--format json` JSONL on stdin and emits the agent.sh
# output contract: a `session id: <id>` line (for reference), throttled activity heartbeats while
# the tool loop is running, then a fresh `---------- output ----------` marker followed by ONLY the
# assistant's final text (the concatenated `text` parts). Heartbeats deliberately precede the final
# marker, so agent.sh last/openai_server still see a clean answer while status/log -f no longer look
# frozen for the whole run. No usable python -> raw passthrough.
_provider_opencode_emit() {
    if ! _provider_opencode_python; then cat; return 0; fi
    PYTHONIOENCODING=utf-8 "$_PROVIDER_OPENCODE_PY" -c '
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
    local timeout_sec="${AGENT_OPENCODE_TIMEOUT_SEC:-1800}"
    local args=(--auto --format json)
    [ -n "$model" ] && args+=(-m "$model")
    [ -n "$effort" ] && args+=(--variant "$effort")
    # stdout carries JSONL only. Keep stderr visible without corrupting that stream: diagnostics are
    # prefixed and sent through the provider stderr, which generic dispatch records in the task log.
    # A bounded default prevents an unattended provider/plugin deadlock from living forever. Set
    # AGENT_OPENCODE_TIMEOUT_SEC=0 to disable it (also useful for shell-function test doubles).
    local -a command=(opencode run "${args[@]}" "$prompt")
    local -a statuses
    if [ "$timeout_sec" -gt 0 ] 2>/dev/null && command -v timeout >/dev/null 2>&1; then
        ( cd "$dir" && timeout --foreground --kill-after=10s "${timeout_sec}s" "${command[@]}" </dev/null \
            2> >(while IFS= read -r line; do printf '[opencode] %s\n' "$line" >&2; done) \
        ) | _provider_opencode_emit
    else
        ( cd "$dir" && "${command[@]}" </dev/null \
            2> >(while IFS= read -r line; do printf '[opencode] %s\n' "$line" >&2; done) \
        ) | _provider_opencode_emit
    fi
    statuses=("${PIPESTATUS[@]}")
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
