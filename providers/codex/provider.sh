# Codex provider plugin for agent.sh.
# Contract: provider_codex_resolve, provider_codex_run_cmd, provider_codex_resume_cmd,
# provider_codex_doctor. See ../../agent.sh for the dispatch that calls these.

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

# provider_codex_run_cmd DIR MODEL EFFORT PROMPT — runs the CLI, streams to stdout/stderr.
provider_codex_run_cmd() {
    local dir="$1" model="$2" effort="$3" prompt="$4"
    codex exec -m "$model" -c model_reasoning_effort="$effort" \
        --sandbox workspace-write --skip-git-repo-check -C "$dir" \
        "$prompt" </dev/null 2>&1
}

# provider_codex_resume_cmd DIR SESSION ANSWER — resumes an existing session.
provider_codex_resume_cmd() {
    local dir="$1" session="$2" answer="$3"
    ( cd "$dir" && codex exec resume --skip-git-repo-check \
        -c 'sandbox_mode="workspace-write"' "$session" "$answer" </dev/null 2>&1 )
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
