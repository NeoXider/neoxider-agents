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
# output contract: a `session id: <id>` line (for reference), a fresh `---------- output ----------`
# marker, then ONLY the assistant's final text (the concatenated `text` parts). This strips
# opencode's TUI chrome (ANSI colour codes, the "> build · model" header, tool-call/permission noise)
# that otherwise leaks into the bridge/benchmark response. No usable python -> raw passthrough.
_provider_opencode_emit() {
    if ! _provider_opencode_python; then cat; return 0; fi
    PYTHONIOENCODING=utf-8 "$_PROVIDER_OPENCODE_PY" -c '
import sys, json
try:
    sys.stdin.reconfigure(errors="ignore")
except Exception:
    pass
MARK = "---------- output ----------"
sid = None; parts = {}; order = []; raw = []
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
if sid:
    print("session id: %s" % sid)
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
    local args=(--auto --format json)
    [ -n "$model" ] && args+=(-m "$model")
    [ -n "$effort" ] && args+=(--variant "$effort")
    # stdout carries the JSONL we parse; stderr (logs) is dropped so it can't corrupt the stream.
    ( cd "$dir" && opencode run "${args[@]}" "$prompt" </dev/null 2>/dev/null ) | _provider_opencode_emit
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
