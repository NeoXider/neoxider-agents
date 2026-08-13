# Kimi Code provider plugin for agent.sh.
# Contract: provider_kimi_resolve, provider_kimi_run_cmd, provider_kimi_resume_cmd,
# provider_kimi_doctor. Kimi Code 0.34+ is required for structured prompt output.

# Kimi's -m flag expects a configured model alias. These ids are verified against the live
# managed:kimi-code catalog (Kimi Code 0.34.0, 2026-08-09). K3 is the wrapper default.
# K2.5/K2.7 are intentionally not aliased because the managed catalog does not expose them.
# Raw configured aliases still pass through unchanged.
provider_kimi_resolve() {
    local alias="${1:-k3}"
    P_EFFORT=""
    case "$alias" in
        ""|default|k3|kimi-k3)              P_MODEL="kimi-code/k3" ;;
        k3-256k)                             P_MODEL="kimi-code/k3-256k" ;;
        coding|kimi-for-coding)              P_MODEL="kimi-code/kimi-for-coding" ;;
        highspeed|kimi-for-coding-highspeed) P_MODEL="kimi-code/kimi-for-coding-highspeed" ;;
        *) P_MODEL="$alias" ;;
    esac
}

_PROVIDER_KIMI_PY=""
_PROVIDER_KIMI_PY_RESOLVED=""
_provider_kimi_python() {
    if [ -z "$_PROVIDER_KIMI_PY_RESOLVED" ]; then
        _PROVIDER_KIMI_PY_RESOLVED=1
        local c
        for c in "${AGENT_PYTHON:-}" python3 python py; do
            [ -n "$c" ] || continue
            if "$c" -c "import sys" >/dev/null 2>&1; then _PROVIDER_KIMI_PY="$c"; break; fi
        done
    fi
    [ -n "$_PROVIDER_KIMI_PY" ]
}

# Convert Kimi's documented stream-json messages into the wrapper's clean-output contract.
# Tool-call preambles are excluded; only assistant messages without tool_calls are joined into the
# final answer. The session.resume_hint meta record supplies the resumable ses_* id.
_provider_kimi_emit() {
    if ! _provider_kimi_python; then
        cat
        return 0
    fi
    PYTHONIOENCODING=utf-8 "$_PROVIDER_KIMI_PY" -c '
import json, sys
try:
    sys.stdin.reconfigure(errors="ignore")
except Exception:
    pass
MARK = "---------- output ----------"
raw, answers, sid = [], [], None
for line in sys.stdin:
    raw.append(line)
    s = line.strip()
    if not s.startswith("{"):
        continue
    try:
        obj = json.loads(s)
    except Exception:
        continue
    if obj.get("role") == "assistant" and not obj.get("tool_calls"):
        content = obj.get("content")
        if isinstance(content, str) and content:
            answers.append(content)
    elif obj.get("role") == "meta" and obj.get("type") == "session.resume_hint":
        sid = obj.get("session_id") or sid
if not answers:
    sys.stdout.write("".join(raw))
    raise SystemExit(3)
answer = "\n".join(answers)
answer = "\n".join((line + " ") if line == MARK else line for line in answer.split("\n"))
if sid:
    print("session id: %s" % sid)
print(MARK)
sys.stdout.write(answer)
if not answer.endswith("\n"):
    sys.stdout.write("\n")
'
}

_provider_kimi_run_args() {
    if [ "${AGENT_CHAT_ONLY:-0}" = 1 ]; then
        printf '%s\n' --agent-file "$HERE/providers/kimi/chat-only-agent.md"
    fi
}

provider_kimi_run_cmd() {
    local dir="$1" model="$2" prompt="$4" rc_kimi rc_emit
    # Print mode already uses Kimi's auto permission policy and explicitly rejects --auto together
    # with --prompt, so do not add the interactive-mode flag here.
    # Проверено на kimi-code 0.34.0: `-y`/`--auto` — флаги ИНТЕРАКТИВНОГО режима, и CLI отвергает их
    # вместе с `-p` («Cannot combine --prompt with --yolo»), то есть прогон падает на старте, ещё не
    # начавшись. Авто-подтверждение в print-режиме и так включено, отдельный флаг для него не нужен.
    local -a args=(-m "$model" -p "$prompt" --output-format stream-json)
    mapfile -t -O ${#args[@]} args < <(_provider_kimi_run_args)
    ( cd "$dir" && kimi "${args[@]}" </dev/null 2>&1 ) | _provider_kimi_emit
    local -a pipe_status=("${PIPESTATUS[@]}")
    rc_kimi=${pipe_status[0]}; rc_emit=${pipe_status[1]}
    [ "$rc_emit" -ne 0 ] && return "$rc_emit"
    return "$rc_kimi"
}

provider_kimi_resume_cmd() {
    local dir="$1" session="$2" answer="$3" rc_kimi rc_emit
    # The chat-only agent profile is persisted in the session and must not be repeated on resume;
    # Kimi rejects --agent-file together with --session.
    ( cd "$dir" && kimi --session "$session" -p "$answer" \
        --output-format stream-json </dev/null 2>&1 ) | _provider_kimi_emit
    local -a pipe_status=("${PIPESTATUS[@]}")
    rc_kimi=${pipe_status[0]}; rc_emit=${pipe_status[1]}
    [ "$rc_emit" -ne 0 ] && return "$rc_emit"
    return "$rc_kimi"
}

provider_kimi_doctor() {
    local ver login="" py
    if command -v kimi >/dev/null 2>&1; then
        ver="$(kimi --version 2>&1 | head -1)"
        py="$(command -v python3 || command -v python || command -v py || echo python)"
        if kimi provider list --json 2>/dev/null | "$py" -c '
import json, sys
try:
    obj = json.load(sys.stdin)
    ok = bool(obj.get("default_model") or obj.get("defaultModel") or obj.get("models"))
except Exception:
    ok = False
raise SystemExit(0 if ok else 1)
'; then login="configured"; else login="login required: run kimi login"; fi
        printf '{"engine":"kimi","version":%s,"available":true,"login":%s,"limits":null,"note":"K3 default; no CLI limits endpoint."}\n' \
            "$(_json_str "$ver")" "$(_json_str "$login")"
    else
        printf '{"engine":"kimi","version":"NOT_FOUND","available":false,"login":"","limits":null,"note":"Install Kimi Code CLI 0.34+."}\n'
    fi
}
