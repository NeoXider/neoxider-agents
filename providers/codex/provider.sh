# Codex provider plugin for agent.sh.
# Contract: provider_codex_resolve, provider_codex_run_cmd, provider_codex_resume_cmd,
# provider_codex_doctor. See ../../agent.sh for the dispatch that calls these.

# `codex exec resume` DOES accept -m/--model and -c model_reasoning_effort=... (confirmed via
# `codex exec resume --help`, codex-cli 0.130.0) -- without forwarding them, a resumed session
# silently drifted to whatever codex's own default is (observed live: a session started with
# `-m spark` came back reporting `model: gpt-5.5` on resume). Opting in here like claude does.
PROVIDER_CODEX_RESUME_NEEDS_MODEL=1

# alias -> real model + effort. Sets globals P_MODEL / P_EFFORT (P_EFFORT may be empty).
provider_codex_resolve() {
    local alias="${1:-5.5}"; P_EFFORT="medium"
    case "$alias" in
        ""|5.5|default) P_MODEL="gpt-5.5" ;;
        5.5-high|high)  P_MODEL="gpt-5.5"; P_EFFORT="high" ;;
        spark|5.3|5.3-spark|codex-spark) P_MODEL="gpt-5.3-codex-spark" ;;
        *) P_MODEL="$alias" ;;
    esac
}

# _provider_codex_emit — reads codex `--json` JSONL on stdin and emits agent.sh-friendly output:
#   1. a `session id: <uuid>` line (parsed from the `thread.started` event) so agent.sh's own
#      session-id grep keeps working for resume, exactly as it did with codex's plaintext banner;
#   2. a fresh `---------- output ----------` marker followed by ONLY the agent's final message
#      (the last `agent_message` item) — since `last_output` returns everything after the LAST
#      such marker, downstream (`agent.sh last`, the GUI, the openai-server bridge) sees a clean
#      answer with none of codex's banner/session-id/ERROR-log/"tokens used"/cp866-mojibake chrome.
# On any failure (auth/rate-limit/no agent_message) it echoes the full raw stream instead, so the
# error stays visible for debugging. This is why we use `--json` rather than plaintext `exec`.
# NB: the parser is passed via `python -c`, NOT a `python - <<HEREDOC`: a heredoc would itself
# occupy stdin, so the piped codex JSONL would never reach sys.stdin.
_provider_codex_emit() {
    PYTHONIOENCODING=utf-8 python -c '
import sys, json
try:
    sys.stdin.reconfigure(errors="ignore")   # codex prints a cp866 OS-notification line that is not UTF-8
except Exception:
    pass
sid = None; msg = None; raw = []
for line in sys.stdin:
    raw.append(line)
    s = line.strip()
    if not s or s[0] != "{":
        continue
    try:
        o = json.loads(s)
    except Exception:
        continue
    t = o.get("type")
    if t == "thread.started" and o.get("thread_id"):
        sid = o["thread_id"]
    elif t == "item.completed":
        it = o.get("item") or {}
        if it.get("type") == "agent_message" and it.get("text") is not None:
            msg = it["text"]
if msg is None:
    sys.stdout.write("".join(raw))          # surface the raw error stream, nothing clean to show
    raise SystemExit(0)
if sid:
    print("session id: %s" % sid)           # captured by agent.sh grep for resume
print("---------- output ----------")       # last_output slices to AFTER this -> clean answer only
sys.stdout.write(msg)
if not msg.endswith("\n"):
    sys.stdout.write("\n")
'
}

# provider_codex_run_cmd DIR MODEL EFFORT PROMPT — runs the CLI via `--json` and cleans the output.
# codex's plaintext `exec` mixes its banner/session-id/ERROR-log/"tokens used" chrome (and Windows
# cp866 mojibake) into the same stdout stream as the answer, which used to pollute `agent.sh last`,
# the GUI chat view, and the openai-server bridge's answer string. `--json` gives structured events
# instead, from which `_provider_codex_emit` pulls just the session id + the final agent message.
provider_codex_run_cmd() {
    local dir="$1" model="$2" effort="$3" prompt="$4"
    codex exec -m "$model" -c model_reasoning_effort="$effort" \
        --sandbox workspace-write --skip-git-repo-check -C "$dir" \
        --json "$prompt" </dev/null 2>&1 | _provider_codex_emit
    # Surface a parser failure (e.g. python missing/crashed) rather than masking it behind codex's
    # own exit code -- otherwise the task could be marked done with empty/partial output.
    local rc_codex=${PIPESTATUS[0]} rc_emit=${PIPESTATUS[1]}
    [ "$rc_emit" -ne 0 ] && return "$rc_emit"
    return "$rc_codex"
}

# provider_codex_resume_cmd DIR SESSION ANSWER — resumes an existing session. $P_MODEL/$P_EFFORT
# are set by agent.sh's provider_dispatch_resume just before this runs (PROVIDER_CODEX_RESUME_NEEDS_MODEL=1
# above opts into that) -- same pattern provider_claude_resume_cmd uses. Same `--json` cleanup.
provider_codex_resume_cmd() {
    local dir="$1" session="$2" answer="$3" cargs
    cargs=(-c 'sandbox_mode="workspace-write"')
    [ -n "$P_MODEL" ] && cargs+=(-m "$P_MODEL")
    [ -n "$P_EFFORT" ] && cargs+=(-c "model_reasoning_effort=\"$P_EFFORT\"")
    ( cd "$dir" && codex exec resume --skip-git-repo-check \
        "${cargs[@]}" --json "$session" "$answer" </dev/null 2>&1 ) | _provider_codex_emit
    local rc_codex=${PIPESTATUS[0]} rc_emit=${PIPESTATUS[1]}   # see provider_codex_run_cmd
    [ "$rc_emit" -ne 0 ] && return "$rc_emit"
    return "$rc_codex"
}

# provider_codex_doctor — prints a single-line JSON object to stdout:
# {"engine":"codex","version":"...","available":true|false,"login":"...","limits":{...}|null,"note":"..."}
provider_codex_doctor() {
    local ver login
    if command -v codex >/dev/null 2>&1; then
        ver="$(codex --version 2>&1 | head -1)"
        login="$(codex login status 2>&1 | head -1)"
        PYTHONIOENCODING=utf-8 python - "$ver" "$login" <<'PY'
import json, glob, os, sys
ver, login = sys.argv[1], sys.argv[2]
files = sorted(glob.glob(os.path.expanduser('~/.codex/sessions/**/*.jsonl'), recursive=True), key=os.path.getmtime)[-8:]
def find(d):
    if isinstance(d, dict):
        if 'rate_limits' in d:
            return d['rate_limits']
        for v in d.values():
            r = find(v)
            if r:
                return r
    return None
rl = None
for f in files:
    try:
        for line in open(f, encoding='utf-8', errors='ignore'):
            if '"rate_limits"' in line:
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                r = find(o)
                if r:
                    rl = r
    except Exception:
        pass
print(json.dumps({"engine": "codex", "version": ver, "available": True,
                   "login": login, "limits": rl, "note": ""}, separators=(",", ":")))
PY
    else
        printf '{"engine":"codex","version":"NOT_FOUND","available":false,"login":"","limits":null,"note":""}\n'
    fi
}
