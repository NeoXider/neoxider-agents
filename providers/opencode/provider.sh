# opencode provider plugin for agent.sh.
# Contract: provider_opencode_run_cmd, provider_opencode_doctor.
# No provider_opencode_resolve (opencode has no alias->model resolution layer): the raw
# -m value, if any, is passed straight through, and meta model= stays "default" otherwise.
# No provider_opencode_resume_cmd: reply was never supported for opencode (matches today).

# provider_opencode_run_cmd DIR MODEL EFFORT PROMPT — runs the CLI, streams to stdout/stderr.
# MODEL is the raw -m value (may be empty); EFFORT is unused by this provider.
provider_opencode_run_cmd() {
    local dir="$1" model="$2" prompt="$4"
    if [ -n "$model" ]; then
        ( cd "$dir" && opencode run -m "$model" "$prompt" </dev/null 2>&1 )
    else
        ( cd "$dir" && opencode run "$prompt" </dev/null 2>&1 )
    fi
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
