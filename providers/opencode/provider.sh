# opencode provider plugin for agent.sh.
# Contract: provider_opencode_run_cmd, provider_opencode_doctor.
# No provider_opencode_resolve (opencode has no alias->model resolution layer): the raw
# -m value, if any, is passed straight through, and meta model= stays "default" otherwise.
# No provider_opencode_resume_cmd: reply was never supported for opencode (matches today).

# provider_opencode_run_cmd DIR MODEL EFFORT PROMPT — runs the CLI, streams to stdout/stderr.
# MODEL is the raw -m value (may be empty). EFFORT maps to opencode's own --variant flag
# (its equivalent of a reasoning-effort level: high/max/minimal/...), if given.
# --auto: auto-approve permissions that are not explicitly denied -- without it opencode can block on
# a permission prompt, which would hang forever since stdin is closed (</dev/null). The whole point
# of this tool is fully unattended runs, so every provider must run in "full auto" mode (confirmed:
# this is intentional, not a default-on footgun -- see README's "Adding a provider"). NOTE: opencode
# renamed this flag from --dangerously-skip-permissions to --auto; the old flag is now unknown and
# makes `opencode run` fail with an "Unexpected server error" (works interactively, dies here).
provider_opencode_run_cmd() {
    local dir="$1" model="$2" effort="$3" prompt="$4"
    local args=(--auto)
    [ -n "$model" ] && args+=(-m "$model")
    [ -n "$effort" ] && args+=(--variant "$effort")
    ( cd "$dir" && opencode run "${args[@]}" "$prompt" </dev/null 2>&1 )
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
