# Claude provider plugin for agent.sh.
# Contract: provider_claude_resolve, provider_claude_run_cmd, provider_claude_resume_cmd,
# provider_claude_doctor. See ../../agent.sh for the dispatch that calls these.

# claude's resume command still needs --model/--effort (unlike codex's `exec resume`, which
# takes no model flag at all) -> opt in to resolve-on-resume in agent.sh's generic dispatch.
PROVIDER_CLAUDE_RESUME_NEEDS_MODEL=1

# claude: default (no -m) -> Opus 5. Suffix -low/-medium/-high/-xhigh/-max on any alias overrides
# effort; without a suffix, opus/opus5/haiku go without --effort (CLI default).
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
        # NB: bare aliases on this CLI can resolve to a legacy generation ("sonnet" -> claude-sonnet-4-6,
        # verified), so every current model pins its explicit id. The bare "opus" alias still points at
        # claude-opus-4-8, hence the separate opus5 id.
        ""|default|opus5) P_MODEL="claude-opus-5" ;;
        sonnet) P_MODEL="claude-sonnet-5"; [ -z "$eff" ] && eff="high" ;;
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
        # `--tools ""` is the CLI's fail-closed zero-tool contract. A denylist drifts whenever
        # Claude adds a new tool name and previously exposed Skill/ToolSearch before being updated.
        printf '%s\n' --strict-mcp-config --tools ""
    fi
}

# _provider_claude_invoke DIR PROMPT CARGS... — shared tail of run/resume. Plain mode prints the
# finished answer once; AGENT_STREAM_TEXT=1 (set by openai_server.py's live-streaming path) switches
# the CLI to --output-format stream-json (token deltas as JSONL events) and pipes it through
# stream_text_filter.py, which reprints the SAME answer text incrementally -- so the task log
# (agent.sh tees this stdout into it) GROWS while the model generates and a tailing reader can
# forward real deltas. --verbose is required by the CLI for stream-json in -p mode.
#
# PERMISSIONS: full auto, like every other engine here (gemini --yolo, opencode --auto, codex
# --sandbox danger-full-access, kimi's auto policy). stdin is closed for every task, so a run that
# stops to ask for permission cannot be answered and hangs until the watchdog kills it --
# "autonomous" is the whole point of this wrapper. acceptEdits was not enough: it pre-approves file
# edits but still prompts on other tools.
# Override with AGENT_CLAUDE_PERMISSION="--permission-mode acceptEdits" on machines where you want
# the prompts back. Chat-only (openai_server.py) keeps acceptEdits and ignores the variable -- that
# bridge already registers zero tools via --disallowedTools, and an HTTP endpoint reachable by
# arbitrary callers must not be handed a permission bypass as well.
_provider_claude_perm_args() {
    if [ "${AGENT_CHAT_ONLY:-0}" = 1 ]; then
        printf '%s\n' --permission-mode acceptEdits
        return 0
    fi
    if [ -n "${AGENT_CLAUDE_PERMISSION:-}" ]; then
        printf '%s\n' ${AGENT_CLAUDE_PERMISSION}
        return 0
    fi
    printf '%s\n' --dangerously-skip-permissions
}

_provider_claude_invoke() {
    local dir="$1" prompt="$2"; shift 2
    local -a perm; mapfile -t perm < <(_provider_claude_perm_args)
    # A prompt too long for argv goes in on stdin: `claude -p` with no positional prompt reads it
    # from there, and a file redirect hits EOF at once, so the headless "never block on an
    # interactive stdin" guarantee of the </dev/null below is preserved.
    local -a text=("$prompt")
    local promptfile=""
    if prompt_needs_stdin "$prompt"; then
        promptfile="$(prompt_stdin_file "$prompt")" \
            || { printf '[claude] cannot stage a long prompt for stdin\n' >&2; return 1; }
        text=()
    fi
    local stdin_src="/dev/null"; [ -n "$promptfile" ] && stdin_src="$promptfile"
    local rc=0
    if [ "${AGENT_STREAM_TEXT:-0}" = 1 ]; then
        _agent_python || { printf 'agent.sh: claude streaming needs a runnable python interpreter\n' >&2; return 1; }
        ( cd "$dir" && claude -p "$@" "${perm[@]}" \
            --output-format stream-json --include-partial-messages --verbose "${text[@]}" <"$stdin_src" 2>&1 \
          | PYTHONIOENCODING=utf-8 "$_AGENT_PY" -u "$HERE/stream_text_filter.py" )
        rc=$?
    else
        ( cd "$dir" && claude -p "$@" "${perm[@]}" "${text[@]}" <"$stdin_src" 2>&1 )
        rc=$?
    fi
    [ -n "$promptfile" ] && rm -f "$promptfile"
    return "$rc"
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

# provider_claude_doctor — prints a single-line JSON object to stdout. Claude Code 2.1.198 has no
# usage/limits/quota CLI command, but its own binary fetches subscription utilization from the
# authenticated GET /api/oauth/usage endpoint. Use that real source when the persisted OAuth access
# token is still current; never refresh/rotate credentials from a read-only doctor command. The
# fallback is an explicitly labelled LOCAL USAGE-SO-FAR estimate from transcript usage blocks.
provider_claude_doctor() {
    local ver auth
    if command -v claude >/dev/null 2>&1; then
        ver="$(claude --version 2>&1 | head -1)"
        auth="$(claude auth status --json 2>/dev/null || true)"
        if ! _agent_python; then
            printf '{"engine":"claude","version":%s,"available":true,"login":"","limits":null,"note":"Python unavailable; usage probe skipped."}\n' "$(_json_str "$ver")"
            return 0
        fi
        PYTHONIOENCODING=utf-8 "$_AGENT_PY" - "$ver" "$auth" <<'PY'
import glob, io, json, os, sys, time, urllib.error, urllib.request
ver, auth_raw = sys.argv[1:3]
now = time.time()
h5, d7 = now - 5 * 3600, now - 7 * 86400
sums = {"5h": [0, 0], "7d": [0, 0]}  # [in+out, cache read+creation]
MAXLINE = 4_000_000  # a usage-bearing line is tiny; skip pathological giant lines (a single huge
                     # tool-result/attachment line was read whole by `for line in open(...)` -> MemoryError)
def bounded_lines(path):
    with io.open(path, 'rb') as fb:
        buf = b''; skipping = False
        while True:
            data = fb.read(1 << 20)
            if not data:
                break
            buf += data
            while True:
                nl = buf.find(b'\n')
                if nl < 0:
                    if len(buf) > MAXLINE:
                        buf = b''; skipping = True   # drop the start of an oversized line, keep sync
                    break
                seg = buf[:nl]; buf = buf[nl + 1:]
                if skipping:
                    skipping = False                 # this seg is the tail of the dropped line
                    continue
                if len(seg) <= MAXLINE:
                    yield seg
for f in glob.glob(os.path.expanduser('~/.claude/projects/**/*.jsonl'), recursive=True):
    try:
        if os.path.getmtime(f) < d7:
            continue
        for raw in bounded_lines(f):
            if b'"usage"' not in raw or b'"assistant"' not in raw:
                continue
            try:
                o = json.loads(raw.decode('utf-8', 'ignore'))
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
usage = {"source": "local_transcripts", "estimated": True,
         "windows": [{"label": "5h", "window_minutes": 300,
                       "input_output_tokens": sums['5h'][0], "cache_tokens": sums['5h'][1]},
                      {"label": "7d", "window_minutes": 10080,
                       "input_output_tokens": sums['7d'][0], "cache_tokens": sums['7d'][1]}]}
note = ('Local transcript estimate (usage so far, not a provider limit): '
        '5h ~%s input+output (+%s cache); 7d ~%s input+output (+%s cache).'
        % (fmt(sums['5h'][0]), fmt(sums['5h'][1]), fmt(sums['7d'][0]), fmt(sums['7d'][1])))
try:
    auth = json.loads(auth_raw)
except ValueError:
    auth = {}
login = "CLI ok" if auth.get("loggedIn") else "not logged in"
limits = None
# The CLI refreshes OAuth credentials internally, but doctor is read-only: only use an access token
# that is already valid. A stale token falls back honestly instead of rotating a refresh token.
try:
    creds = json.load(open(os.path.expanduser('~/.claude/.credentials.json'), encoding='utf-8'))
    oauth = creds.get('claudeAiOauth') or {}
    if oauth.get('accessToken') and (oauth.get('expiresAt') or 0) / 1000 > now + 30:
        req = urllib.request.Request('https://api.anthropic.com/api/oauth/usage', headers={
            'Authorization': 'Bearer ' + oauth['accessToken'],
            'anthropic-beta': 'oauth-2025-04-20', 'User-Agent': 'claude-code/' + ver.split()[0]})
        with urllib.request.urlopen(req, timeout=5) as response:
            live = json.load(response)
        def window(key, label, minutes):
            value = live.get(key) or {}
            percent = value.get('utilization')
            if percent is None:
                percent = value.get('used_percentage')
            if percent is None:
                return None
            return {"label": label, "used_percent": float(percent),
                    "window_minutes": minutes, "resets_at": value.get('resets_at')}
        windows = [w for w in (window('five_hour', '5h', 300),
                               window('seven_day', '7d', 10080)) if w]
        if windows:
            limits = {"source": "provider", "plan_type": oauth.get('subscriptionType') or
                      auth.get('subscriptionType') or "?", "windows": windows}
            note = "Live remaining-limit utilization from Claude's authenticated /api/oauth/usage endpoint."
except (OSError, ValueError, urllib.error.URLError, TimeoutError):
    pass
print(json.dumps({"engine": "claude", "version": ver, "available": True,
                   "login": login, "limits": limits, "usage": usage,
                   "note": note}, separators=(",", ":")))
PY
    else
        printf '{"engine":"claude","version":"NOT_FOUND","available":false,"login":"","limits":null,"note":""}\n'
    fi
}
