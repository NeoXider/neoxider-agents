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

# _provider_claude_chatonly_args — extra flags applied ONLY when AGENT_CHAT_ONLY=1 (set by
# openai_server.py; unset for normal `agent.sh run`, which legitimately needs real file/shell/MCP
# access to do coding work). Locks the model down to text-only completion, verified live:
#   --strict-mcp-config (with no --mcp-config)  -- zero MCP servers loaded for the session, so a
#     real project MCP server (e.g. this machine's unityMCP) is not reachable. NOTE: switching to
#     --permission-mode plan instead of acceptEdits was tried first and rejected -- it produced
#     INCONSISTENT results on the identical tool-calling prompt (worked once, then refused the
#     same prompt as a "prompt injection" on a later run), which is worse for a benchmark than
#     acceptEdits + an explicit tool denylist.
#   --disallowedTools ... -- explicitly blocks Bash/Edit/Write/NotebookEdit (file+shell mutation),
#     Task (this CLI's own subagent/skill launcher), WebFetch/WebSearch (reaching outside the
#     conversation). Read/Grep/Glob stay allowed (read-only, scoped to the isolated scratch dir --
#     no mutation risk, and useful for the model to look at its own prior turns if ever needed).
# Verified live: with both flags, asking it to "use UnityMCP or any tool to write a file" got a
# correct refusal ("I don't have a file-writing or shell-execution tool available... read-only
# Glob/Grep/Read"), no file was created, no hang -- and the SAME flags did not break a normal
# tool-calling completion (still returned a clean fenced JSON tool_calls block).
_provider_claude_chatonly_args() {
    if [ "${AGENT_CHAT_ONLY:-0}" = 1 ]; then
        # Skill added after a live Sonnet 5 G6 refusal: the CLI loads the USER'S global skills
        # into context, and the model declined to emit text tool-calls, offering to run the
        # user's unity-mcp skill instead. Blocking skill invocation keeps the bridge session a
        # plain completion endpoint. (SlashCommand is NOT a valid tool name here -- listing it
        # made the CLI prepend a 'matches no known tool' warning to every answer.)
        printf '%s\n' --strict-mcp-config --disallowedTools \
            Bash,Edit,Write,NotebookEdit,Task,WebFetch,WebSearch,Skill
    fi
}

# _provider_claude_invoke DIR PROMPT CARGS... — shared tail of run/resume. Plain mode prints the
# finished answer once; AGENT_STREAM_TEXT=1 (set by openai_server.py's live-streaming path) switches
# the CLI to --output-format stream-json (token deltas as JSONL events) and pipes it through
# stream_text_filter.py, which reprints the SAME answer text incrementally -- so the task log
# (agent.sh tees this stdout into it) GROWS while the model generates and a tailing reader can
# forward real deltas. --verbose is required by the CLI for stream-json in -p mode.
_provider_claude_invoke() {
    local dir="$1" prompt="$2"; shift 2
    if [ "${AGENT_STREAM_TEXT:-0}" = 1 ]; then
        local py
        py="$(command -v python || command -v python3 || command -v python3.12 || echo python)"
        ( cd "$dir" && claude -p "$@" --permission-mode acceptEdits \
            --output-format stream-json --include-partial-messages --verbose "$prompt" </dev/null 2>&1 \
          | PYTHONIOENCODING=utf-8 "$py" -u "$HERE/stream_text_filter.py" )
    else
        ( cd "$dir" && claude -p "$@" --permission-mode acceptEdits "$prompt" </dev/null 2>&1 )
    fi
}

# provider_claude_run_cmd DIR MODEL EFFORT PROMPT — runs the CLI, streams to stdout/stderr.
provider_claude_run_cmd() {
    local dir="$1" model="$2" effort="$3" prompt="$4" cargs
    cargs=(--model "$model"); [ -n "$effort" ] && cargs+=(--effort "$effort")
    mapfile -t -O ${#cargs[@]} cargs < <(_provider_claude_chatonly_args)
    _provider_claude_invoke "$dir" "$prompt" "${cargs[@]}"
}

# provider_claude_resume_cmd DIR SESSION ANSWER — resumes an existing session.
# claude doesn't log a session id in text mode -> fall back to --continue when session is empty.
# NOTE: model/effort for resume come from the caller's $model var via provider_claude_resolve,
# already invoked by agent.sh before this is called; we re-derive cargs the same way run_cmd does.
provider_claude_resume_cmd() {
    local dir="$1" session="$2" answer="$3" cargs
    cargs=(--model "$P_MODEL"); [ -n "$P_EFFORT" ] && cargs+=(--effort "$P_EFFORT")
    if [ -n "$session" ]; then cargs+=(--resume "$session"); else cargs+=(--continue); fi
    mapfile -t -O ${#cargs[@]} cargs < <(_provider_claude_chatonly_args)
    _provider_claude_invoke "$dir" "$answer" "${cargs[@]}"
}

# provider_claude_doctor — prints a single-line JSON object to stdout. The Claude CLI exposes no
# remaining-limit API, so instead of "no data" the note carries a LOCAL USAGE ESTIMATE summed from
# the CLI's own transcript files (~/.claude/projects/**/*.jsonl carry a per-assistant-message
# `usage` block + timestamp): tokens burned in the current 5h window and the last 7 days — the two
# windows Anthropic actually rate-limits on. An estimate of what was SPENT, not what REMAINS
# (the cap depends on the plan and isn't exposed anywhere locally).
provider_claude_doctor() {
    local ver py
    if command -v claude >/dev/null 2>&1; then
        ver="$(claude --version 2>&1 | head -1)"
        # `python` can resolve to the non-executable WindowsApps shim in some shells; fall back
        # through real interpreters so the usage estimate works everywhere.
        py="$(command -v python || command -v python3 || command -v python3.12 || echo python)"
        PYTHONIOENCODING=utf-8 "$py" - "$ver" <<'PY'
import glob, io, json, os, sys, time
ver = sys.argv[1]
now = time.time()
h5, d7 = now - 5 * 3600, now - 7 * 86400
sums = {"5h": [0, 0], "7d": [0, 0]}  # [in+out, cache read+creation]
for f in glob.glob(os.path.expanduser('~/.claude/projects/**/*.jsonl'), recursive=True):
    try:
        if os.path.getmtime(f) < d7:
            continue
        for line in io.open(f, encoding='utf-8', errors='ignore'):
            if '"usage"' not in line or '"assistant"' not in line:
                continue
            try:
                o = json.loads(line)
            except ValueError:
                continue
            u = (o.get('message') or {}).get('usage') or {}
            ts = o.get('timestamp') or ''
            try:
                t = time.mktime(time.strptime(ts[:19], '%Y-%m-%dT%H:%M:%S')) - time.timezone
            except ValueError:
                continue
            if t < d7:
                continue
            io_tok = (u.get('input_tokens') or 0) + (u.get('output_tokens') or 0)
            cache = (u.get('cache_read_input_tokens') or 0) + (u.get('cache_creation_input_tokens') or 0)
            sums['7d'][0] += io_tok; sums['7d'][1] += cache
            if t >= h5:
                sums['5h'][0] += io_tok; sums['5h'][1] += cache
    except OSError:
        continue
def fmt(n):
    if n >= 1e9:
        return '%.1fB' % (n / 1e9)
    return '%.1fM' % (n / 1e6) if n >= 1e6 else ('%.0fk' % (n / 1e3) if n >= 1e3 else str(n))
note = ('usage burned (local transcript estimate): 5h window ~%s in+out (+%s cache) / '
        'last 7d ~%s in+out (+%s cache). Claude CLI exposes no remaining-limit %%.'
        % (fmt(sums['5h'][0]), fmt(sums['5h'][1]), fmt(sums['7d'][0]), fmt(sums['7d'][1])))
print(json.dumps({"engine": "claude", "version": ver, "available": True,
                   "login": "CLI ok", "limits": None, "note": note}, separators=(",", ":")))
PY
    else
        printf '{"engine":"claude","version":"NOT_FOUND","available":false,"login":"","limits":null,"note":""}\n'
    fi
}
