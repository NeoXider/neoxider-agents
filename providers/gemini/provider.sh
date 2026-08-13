# Gemini provider plugin for agent.sh.
# Contract: provider_gemini_run_cmd, provider_gemini_doctor.
# No provider_gemini_resolve (gemini has no alias->model resolution layer): the raw
# -m value, if any, is passed straight through, and meta model= stays "default" otherwise.
# No provider_gemini_resume_cmd: reply was never supported for gemini (matches today).

# provider_gemini_run_cmd DIR MODEL EFFORT PROMPT — runs the CLI, streams to stdout/stderr.
# MODEL is the raw -m value (may be empty); EFFORT is unused by this provider.
# --yolo: auto-approve all tool actions -- without it gemini can block on a permission prompt,
# which would hang forever since stdin is closed (</dev/null). The whole point of this tool is
# fully unattended runs, so every provider must run in its own equivalent of "full auto" mode.
#
# AGENT_CHAT_ONLY=1 (the openai_server.py bridge) swaps --yolo for --approval-mode plan, Gemini
# CLI's own read-only mode: no writes, no shell execution. Be honest about the limit -- unlike
# codex (--sandbox read-only + --ignore-user-config), claude (--disallowedTools), kimi (tools: [])
# and opencode (an agent profile with "tools": {"*": false}), Gemini has no flag that removes the
# READ tools, so a gemini-backed bridge can still be asked to read local files. Do not put one on
# a network; prefer claude/codex/opencode there.
provider_gemini_run_cmd() {
    local dir="$1" model="$2" prompt="$4"
    local -a args=()
    [ -n "$model" ] && args+=(-m "$model")
    if [ "${AGENT_CHAT_ONLY:-0}" = 1 ]; then
        args+=(--approval-mode plan)
    else
        args+=(--yolo)
    fi
    ( cd "$dir" && gemini "${args[@]}" -p "$prompt" </dev/null 2>&1 )
}

# provider_gemini_doctor — prints a single-line JSON object to stdout.
provider_gemini_doctor() {
    local ver
    if command -v gemini >/dev/null 2>&1; then
        ver="$(gemini --version 2>&1 | head -1)"
        printf '{"engine":"gemini","version":%s,"available":true,"login":"","limits":null,"note":"No CLI limits endpoint for this provider."}\n' \
            "$(_json_str "$ver")"
    else
        printf '{"engine":"gemini","version":"NOT_FOUND","available":false,"login":"","limits":null,"note":"No CLI limits endpoint for this provider."}\n'
    fi
}
