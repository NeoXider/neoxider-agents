# Claude provider plugin for agent.sh.
# Contract: provider_claude_resolve, provider_claude_run_cmd, provider_claude_resume_cmd,
# provider_claude_doctor. See ../../agent.sh for the dispatch that calls these.

# claude's resume command still needs --model/--effort (unlike codex's `exec resume`, which
# takes no model flag at all) -> opt in to resolve-on-resume in agent.sh's generic dispatch.
PROVIDER_CLAUDE_RESUME_NEEDS_MODEL=1

# claude: default (no -m) -> sonnet + effort HIGH (new Sonnet 5). Suffix -low/-medium/-high/-xhigh/-max
# on any alias overrides effort; without a suffix, opus/haiku go without --effort (CLI default).
# Sets globals P_MODEL / P_EFFORT (P_EFFORT may be empty).
provider_claude_resolve() {
    local alias="${1:-}" base eff=""
    base="$alias"
    case "$alias" in
        *-low)    base="${alias%-low}";    eff="low" ;;
        *-medium) base="${alias%-medium}"; eff="medium" ;;
        *-high)   base="${alias%-high}";   eff="high" ;;
        *-xhigh)  base="${alias%-xhigh}";  eff="xhigh" ;;
        *-max)    base="${alias%-max}";     eff="max" ;;
    esac
    case "$base" in
        # NB: alias "sonnet" on this CLI resolves to the legacy claude-sonnet-4-6 (verified),
        # so the default pins the explicit id of the new Sonnet 5. The "opus" alias is current
        # (-> claude-opus-4-8), leave it alone.
        ""|default|sonnet) P_MODEL="claude-sonnet-5"; [ -z "$eff" ] && eff="high" ;;
        opus)  P_MODEL="opus" ;;
        haiku) P_MODEL="haiku" ;;
        *) P_MODEL="$base" ;;
    esac
    P_EFFORT="$eff"
}

# provider_claude_run_cmd DIR MODEL EFFORT PROMPT — runs the CLI, streams to stdout/stderr.
provider_claude_run_cmd() {
    local dir="$1" model="$2" effort="$3" prompt="$4" cargs
    cargs=(--model "$model"); [ -n "$effort" ] && cargs+=(--effort "$effort")
    ( cd "$dir" && claude -p "${cargs[@]}" --permission-mode acceptEdits "$prompt" </dev/null 2>&1 )
}

# provider_claude_resume_cmd DIR SESSION ANSWER — resumes an existing session.
# claude doesn't log a session id in text mode -> fall back to --continue when session is empty.
# NOTE: model/effort for resume come from the caller's $model var via provider_claude_resolve,
# already invoked by agent.sh before this is called; we re-derive cargs the same way run_cmd does.
provider_claude_resume_cmd() {
    local dir="$1" session="$2" answer="$3" cargs
    cargs=(--model "$P_MODEL"); [ -n "$P_EFFORT" ] && cargs+=(--effort "$P_EFFORT")
    if [ -n "$session" ]; then cargs+=(--resume "$session"); else cargs+=(--continue); fi
    ( cd "$dir" && claude -p "${cargs[@]}" --permission-mode acceptEdits "$answer" </dev/null 2>&1 )
}

# provider_claude_doctor — prints a single-line JSON object to stdout.
provider_claude_doctor() {
    local ver login note
    if command -v claude >/dev/null 2>&1; then
        ver="$(claude --version 2>&1 | head -1)"
        login="CLI ok"
        note="Claude CLI does not expose remaining limits via API - version/availability only."
        printf '{"engine":"claude","version":%s,"available":true,"login":%s,"limits":null,"note":%s}\n' \
            "$(_json_str "$ver")" "$(_json_str "$login")" "$(_json_str "$note")"
    else
        printf '{"engine":"claude","version":"NOT_FOUND","available":false,"login":"","limits":null,"note":""}\n'
    fi
}
