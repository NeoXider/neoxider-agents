# Gemini provider plugin for agent.sh.
# Contract: provider_gemini_run_cmd, provider_gemini_doctor.
# No provider_gemini_resolve (gemini has no alias->model resolution layer): the raw
# -m value, if any, is passed straight through, and meta model= stays "default" otherwise.
# No provider_gemini_resume_cmd: reply was never supported for gemini (matches today).

# provider_gemini_run_cmd DIR MODEL EFFORT PROMPT — runs the CLI, streams to stdout/stderr.
# MODEL is the raw -m value (may be empty); EFFORT is unused by this provider.
provider_gemini_run_cmd() {
    local dir="$1" model="$2" prompt="$4"
    if [ -n "$model" ]; then
        ( cd "$dir" && gemini -m "$model" -p "$prompt" </dev/null 2>&1 )
    else
        ( cd "$dir" && gemini -p "$prompt" </dev/null 2>&1 )
    fi
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
