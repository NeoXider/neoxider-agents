---
name: neoxider-agents
description: Work as an ORCHESTRATOR — plan, decompose and delegate coding tasks to CLI subagents (Claude Code / Opus 5 by default; also Kimi Code / Kimi K3, Codex, opencode, and Gemini) through the agent.sh wrapper, then verify and integrate their results. Covers run/fan, model selection, resume/reply, logs, and verification. Use whenever work can be parallelized or offloaded to CLI agents instead of doing everything in one session.
---

# CLI Subagents (Codex Orchestration)

For foreign-engine subagent tasks, resolve the bundled wrapper from the plugin root; a manual
clone/skill install falls back to the conventional skill directory:

```bash
if [ -n "${CLAUDE_PLUGIN_ROOT:-}" ] && [ -f "$CLAUDE_PLUGIN_ROOT/agent.sh" ]; then
  SK="$CLAUDE_PLUGIN_ROOT/agent.sh"
else
  SK="${NEOXIDER_AGENTS_HOME:-$HOME/.claude/skills/neoxider-agents}/agent.sh"
fi
```

When working inside this repository, `SK=./agent.sh` is sufficient.

**NATIVE-FIRST RULE (user, 2026-07-22, permanent):** every orchestrator spawns its OWN
engine's subagents natively; the agent.sh wrapper is ONLY for foreign engines.
- From Claude Code: claude models (opus/sonnet/haiku) → **native Agent tool** (never
  `agent.sh run -e claude`); kimi/codex/opencode/gemini → agent.sh.
- From Codex: codex models → native codex subagents; kimi/claude/opencode/gemini → agent.sh.

## Work as an orchestrator (default mode)

With this skill you are the **orchestrator**, not the implementer. Your value is planning,
routing, verification and integration — not typing code that a subagent could type. Before
touching a multi-part task yourself, ask: "which pieces can I hand off right now?" Only keep
for yourself what genuinely needs this conversation's context or top-tier reasoning
(architecture, security, tricky debugging).

The loop:

1. **Plan.** Decompose the request into small, independent, precisely-scoped tasks — exact
   file paths, exact signatures, "change nothing else", "Do NOT run git commit". A vague task
   wastes a subagent; a precise one almost always succeeds.
2. **Pre-flight.** `agent.sh doctor` before any fan-out (engines up? codex limits OK?).
   Near the limit → route to `-e claude -m sonnet` or `-e opencode`.
3. **Route.** Cheapest model that will succeed (matrix in [ORCHESTRATOR.md](ORCHESTRATOR.md)):
   trivial → `-m haiku` (or `-e codex -m spark`), regular → `-m sonnet`, hard → the default `opus5`.
4. **Delegate.** `run` for one task, `fan` for a parallel batch. Parallel workers only on
   NON-overlapping files. Each keeps its own `PROGRESS.<task>.md`.
5. **Watch.** `list` / `status <name>`; a `waiting` task gets `reply <name> "..."`.
   A `stalled`/`error` one gets its log read FIRST — the tail says whether the model failed the task
   (re-scope it) or the provider dropped the turn (just resume it, see
   ["A turn that died on the provider"](#a-turn-that-died-on-the-provider-resume-it-dont-restart-it)).
6. **Verify.** Read every finished task's diff yourself — never trust "done" blindly. Run
   builds/tests. Reject and re-delegate anything wrong.
7. **Integrate & commit.** YOU own git: stage, review, commit. Workers must not commit.

> Paste-ready orchestrator system prompt + full which-model-for-what matrix:
> [ORCHESTRATOR.md](ORCHESTRATOR.md).

## Commands

```bash
bash "$SK" run  -t fix-readme -C /c/Git/Proj "prompt" # claude, claude-opus-5 (default); -t = task name
bash "$SK" fan  -t audit -C dir "prompt A" "prompt B" # N parallel background tasks from one call
                                                     # (audit-01, audit-02, ...); shared -e/-m/-f/-C;
                                                     # returns at once — poll with list/status.
                                                     # Use instead of a hand-written `run ... &` loop
bash "$SK" run  -t big-job -C dir "prompt"            # agent keeps PROGRESS.<task>.md by default (per-task, resumable + orchestrator-readable); --no-progress opts out
bash "$SK" run  --no-terse -C dir "prompt"            # terse (concision) directive is ON by default to save output/turn tokens; --no-terse for exploratory/ambiguous work
bash "$SK" run  --prompt-file p.txt -C dir            # prompt from a FILE, for text too long to pass as an argument
                                                     # (the platform caps a command line near 32000 chars); works for `reply` too,
                                                     # where the positional argument is then the task name
bash "$SK" run  -m spark -C /c/Git/Proj "prompt"      # trivial task -> spark
bash "$SK" run  -e claude -m haiku -C dir "prompt"    # a different CLI: claude/opencode/gemini
bash "$SK" run  -m sonnet -f low -e claude -C dir "prompt"  # -f <effort>, separate from -m <model>
bash "$SK" run  -e kimi -C dir "prompt"              # Kimi Code, Kimi K3 by default
bash "$SK" run  -e kimi -m highspeed -C dir "prompt" # explicit managed high-speed coding model
bash "$SK" test-api --base-url http://127.0.0.1:8080 --goal "check /health, then POST+GET /item" --out r.json
                                                     # thin wrapper on `run`: agent exercises a local
                                                     # HTTP API via its own curl/shell, returns strict JSON
bash "$SK" reply fix-readme "answer"                  # continue the task by name (session/dir taken from meta)
bash "$SK" reply SESSION_UUID "answer"                 # or by uuid; with no argument — the last task
                                                     # reply works on claude/codex/kimi/opencode. Use it to
                                                     # CORRECT a task in flight instead of killing it: the
                                                     # agent keeps everything it has already read and done.
bash "$SK" log  fix-readme                            # the entire task thread (run + all replies in one file)
bash "$SK" log  -f fix-readme                         # follow a live background agent (tail -f)
bash "$SK" log  -l fix-readme                         # only the last step
bash "$SK" last fix-readme                            # only the agent's last answer
bash "$SK" status fix-readme                          # state: state/stage/changed files/whether a reply is needed
bash "$SK" wait name-a name-b                         # BLOCK until named tasks settle, then print each final answer;
                                                      # exit 0 = settled, exit 2 = --timeout hit while still running
bash "$SK" wait --timeout 3600                        # no names = watch ALL currently-running tasks (whole wave); default poll 5s
bash "$SK" list                                       # table: state / engine / model / age / files / session
bash "$SK" clean                                      # delete md clutter (<name>.md + PROGRESS.<name>.md) of STOPPED
                                                     # tasks; live running/idle tasks are never touched;
                                                     # --all incl. waiting, --purge also .log/.meta, -n dry-run
bash "$SK" doctor                                     # pre-flight: engines + codex limits (before fanning out!)
bash "$SK" doctor --json                              # machine-readable snapshot (used by the panel)
bash "$SK" doctor --deep                              # + one REAL cheap run per engine that must EXECUTE a shell
                                                     # command — catches "answers fine, every command hangs"
bash "$SK" gui [port]                                 # web control panel over all providers (stable default :8765,
                                                     # or $AGENT_GUI_PORT, or a one-off port arg; if that port is
                                                     # held by someone else it moves to the next free one, loudly)
bash "$SK" gui 8765 --lan --token SECRET              # ...reachable from another PC/phone. --token is MANDATORY with
                                                     # --lan (the panel launches full-auto agents); the other device
                                                     # opens http://<this-host>:8765/?token=SECRET once
```

## Windows / PowerShell invocation (verified 2026-08-17)

- On Windows, `bash` on PATH may be WSL's (`C:\WINDOWS\system32\bash.exe`), which mangles
  Windows paths (`C:UsersUser...`) and cannot find the script. Always call git-bash explicitly:
  `& "C:\Program Files\Git\bin\bash.exe" "C:/Users/User/.claude/skills/neoxider-agents/agent.sh" doctor`
  (forward slashes for the script argument; `-C` and paths inside prompts stay POSIX-style:
  `/c/Git/Proj`).
- From PowerShell, piping agent.sh output can surface a noisy `Unknown: ChildProcess.kill
  (powershell.exe ...)` message even when the command succeeded — verify with
  `agent.sh list` / `agent.sh status <name>` before assuming failure. `fan` returns immediately;
  the launched task keeps running in the background.
- **Completion notifications for orchestrators with their own background-job mechanism**
  (an agent runtime that notifies you when a shell command exits, e.g. DeepSeek Harness):
  launch one `agent.sh wait <task-name>` per task as a separate BACKGROUND job — it blocks
  until the subagent finishes and then prints its final answer to stdout, so your job-
  completion notification arrives with everything you need to verify. For an entire fan-out
  wave use ONE call without names (it watches every running task; exits when all settle,
  or at --timeout with exit code 2).
- **Model ids with a provider prefix must be passed verbatim**: `-e opencode -m
  opencode/muse-spark-1.2-contributor-free` works; the bare id without the `opencode/` prefix
  errors. Short aliases (`-m free`, `-m ox`, …) resolve to the full id for you.
- **OpenCode Zen free tier (live list 2026-08-28):** `big-pickle`, `hy3-free`, `mimo-v2.5-free`,
  `muse-spark-1.2-contributor-free`, `nemotron-3-ultra-free`, `nemotron-3.5-lightning-free`.
  Aliases: `free`/`spark`/`muse`, `ox`/`alpha`, `pickle`, `hy3`, `mimo`, `nemotron`/`ultra`,
  `lightning`. Re-check with `opencode models` — the tier moves, and it moved: **`x-preview-f-free`
  is gone** as of 2026-08-28. Write the id exactly as `opencode models` prints it; near-misses like
  `mimo-free` or `nemotron-ultra-free` are not aliases and fail as unknown models.
  **Unranked**: `free` points at Muse Spark by user preference, not measurement. In the one
  head-to-head actually run (2026-08-24, a 5-step PowerShell disk survey) *neither* Muse Spark nor
  Ox Alpha produced an answer — Ox hit the 1800s timeout, Muse never emitted a line. Treat the free
  tier as fine for one-shot Q&A and unproven for multi-step tool work.
  Liveness varies inside the tier: on 2026-08-28 a one-line probe came back from `mimo-v2.5-free`,
  `nemotron-3-ultra-free` and `hy3-free`, while `nemotron-3.5-lightning-free` sat past a 120s
  deadline without a byte. A silent provider is a provider outage, not a slow model — give it its
  own timeout rather than reading the silence as a result.
- **DeepSeek is gone from opencode:** the Zen `deepseek-v4-flash-free` entry no longer appears in
  `opencode models`, and the Ollama route (`ollama/deepseek-v4-flash:cloud`) returns
  `403 this model requires a subscription`. The `ollama` and `zai` providers are therefore listed
  in `disabled_providers` in `~/.config/opencode/opencode.json`.
- **Local LM Studio (`-e opencode` via the lmstudio-local provider):** LM Studio has ONE
  loaded-model slot. If the model was unloaded (idle timeout), the subagent dies instantly with
  `{"message":"Model is unloaded."}`. Reload it via the API with an explicit JSON body
  (no body → HTTP 415):
  `curl -s -X POST http://127.0.0.1:1234/api/v0/model/<model-id>/load -H "Content-Type: application/json" -d '{}'`
  To avoid slot contention with the orchestrator's own opencode session, prefer a remote model
  (`-m free`, i.e. an OpenCode Zen entry) for subagents when available.

**Environment knobs** (all optional, all with safe defaults):

| Variable | Default | What it does |
|---|---|---|
| `AGENT_TIMEOUT_SEC` | `1800` | Wall-clock deadline for ONE step (`run`/`reply`). On expiry the whole process tree is killed, `!! TIMEOUT: step exceeded AGENT_TIMEOUT_SEC=<N>s and was killed` is appended to the log, and the task ends as `state=error exit=124`. `0` disables it (only for genuinely long jobs). |
| `AGENT_RETRIES` | `2` | Additional provider attempts after a transient failure. Set `0` when an outer caller (such as the API bridge) owns retries. Attempts stop when retrying could repeat autonomous side effects. |
| `AGENT_RETRY_DELAY` | `8` | Seconds between safe transient retries. |
| `AGENT_STALE_SEC` | `300` | Silence after which a still-alive task is reported as `running (no output for Nm)` instead of plain `running` — same wording in the CLI and in the GUI. |
| `AGENT_CODEX_USER_CONFIG` | unset | `1` = let codex load `~/.codex/config.toml` again. Off by default **on purpose** — see ["Codex: every shell command hangs"](#codex-every-shell-command-hangs-environment-issue) below. |
| `AGENT_CODEX_MCP` | unset | Re-add specific MCP servers to the isolated codex config: `AGENT_CODEX_MCP="unityMCP=http://127.0.0.1:8040/mcp,other=http://…"` → repeated `-c mcp_servers.<name>.url="<url>"`. |
| `AGENT_CODEX_SANDBOX` | `danger-full-access` | Sandbox for normal Codex `run`/`reply` tasks. Set `workspace-write` or `read-only` to opt down. `AGENT_CHAT_ONLY=1` always forces `read-only` and ignores this variable. |
| `AGENT_CLAUDE_PERMISSION` | `--dangerously-skip-permissions` | Permission arguments for normal Claude `run`/`reply` tasks; for example, set `--permission-mode acceptEdits` to opt down. `AGENT_CHAT_ONLY=1` always uses `--permission-mode acceptEdits` and ignores this variable. |
| `AGENT_CODEX_DOCTOR_MODEL` / `AGENT_CODEX_DOCTOR_TIMEOUT` | `spark` / `60` | Model and hard deadline for `doctor --deep`'s codex shell check. |
| `AGENT_GUI_PORT` | `8765` | GUI port (an explicit `gui <port>` argument still wins). |
| `AGENT_GUI_HOST` | `127.0.0.1` | GUI bind address. Anything but loopback also requires `AGENT_GUI_TOKEN`, or the panel refuses to start. `--lan` / `--localhost` override it. |
| `AGENT_GUI_TOKEN` | unset | Shared secret the GUI demands from every request when configured (`?token=` bootstrap, `X-Agent-Token`, or its HttpOnly cookie). Mandatory for `--lan`. |
| `AGENT_OPENAI_KEY` | unset | Default `--api-key` for `openai-server`: callers must send `Authorization: Bearer <key>`. Unset = open bridge (fine on loopback only). |
| `AGENT_OPENAI_HOST` | `127.0.0.1` | Bridge bind address (`--localhost` / `--lan` override it); every non-loopback bind requires an API key. |

Normal provider runs are intentionally full-auto: Codex uses `--sandbox danger-full-access`, Claude
uses `--dangerously-skip-permissions`, Gemini uses `--yolo`, opencode uses `--auto`, and Kimi uses its
auto policy. Every task has stdin closed, so an approval question cannot be answered and would hang
until the watchdog kills the process. Codex's full-access default is also an explicit repository-owner
decision for Windows, where its sandbox helper rejected every attempted write (including
`patch rejected: writing is blocked by read-only sandbox; rejected by user approval settings`) and
made agents report changes instead of applying them. Use the two opt-down variables above only where
their restricted modes work and suit the task.

**Self-testing your own work.** If you just built or modified a local web service/API
(e.g. a Unity `HttpListener` debug endpoint, a small backend), you can verify it works
yourself before declaring the task done:

```bash
bash "$SK" test-api --base-url http://127.0.0.1:PORT \
  --goal "check /health returns ok, then POST /item and GET it back" --out result.json
```

This spawns another agent that exercises the API with real HTTP calls (its own
curl/shell, no MCP needed) and reports one strict pass/fail JSON object — a quick,
cheap self-check before you tell the user it's done.

**Need an OpenAI-compatible LLM backend pinned to a specific model?** If a task calls
for something that speaks the OpenAI `/v1/chat/completions` wire format (e.g. another
tool's test harness that only knows how to point at `COREAI_TEST_BASE_URL`-style env
vars, or any OpenAI-client library), use `agent.sh openai-server` instead of setting up
a real provider API key:

```bash
bash "$SK" openai-server -e claude -m sonnet -f low -p 8801
# then point any OpenAI-compatible client's base_url at http://127.0.0.1:8801/v1
```

**Reaching the bridge from a phone/APK or another computer (LAN).** The bridge is loopback-only
by default. Pass `--lan` together with an API key; non-loopback startup without a key is refused:

```bash
bash "$SK" openai-server -e claude -m sonnet -p 8801 --lan --api-key "$(openssl rand -hex 16)"
# prints e.g. "LAN: reachable ... at http://192.168.1.115:8801/v1"
# -> set that as the base_url in the APK / on the other PC, with the key as the OpenAI api_key
```

**Off this machine, always set a key.** `--api-key SECRET` (or `AGENT_OPENAI_KEY=SECRET`) makes
every caller present `Authorization: Bearer SECRET` — the standard OpenAI header, so any
OpenAI-compatible client already sends it as `api_key`. `X-Api-Key: SECRET` works too. `/health`
and `/` stay open so liveness probes and the GUI's bridge list keep working; `/v1/models`,
`/v1/chat/completions` and `/reset` all 401 without the key. With no key the bridge is available
only on loopback. Codex and Gemini remain loopback-only even with a key because read capability
cannot currently be removed from those CLI sessions.

It only exposes the bridge on the local network, not the internet. Because the bridge drives
a CLI agent with your credentials/tools, only run it on a trusted network, and open the port
in the firewall (the startup banner prints the exact `New-NetFirewallRule` command for
Windows). To restrict back to this machine only, pass `--localhost` (or set
`AGENT_OPENAI_HOST=127.0.0.1`); `--lan` forces all-interfaces explicitly.

**Truly public (over the internet)?** Technically yes — the port-forward + the printed WAN
address are all it takes — but do not do it bare. There is no TLS here, so a Bearer key crosses
the network in clear text, and the thing behind the port spends your subscription. Put it behind
a VPN/overlay network (Tailscale, WireGuard, ZeroTier) or a TLS reverse proxy (Caddy/nginx +
Let's Encrypt) that terminates HTTPS and forwards to `127.0.0.1:<port>`, and keep `--api-key` on
as a second lock.

**What the remote caller actually gets — read this before planning a setup.** The agent always
runs on the machine hosting the bridge, so any file it touches is a file on THAT machine. A
second PC calling the API is a *client*: its own files are never read or written. On top of
that, bridge sessions are deliberately chat-only (see the lockdown bullet below) — they answer
in text, they do not edit code. So:

| What you want | What to do |
|---|---|
| An LLM endpoint for an app/benchmark/phone, backed by your CLI subscription | `openai-server` + `--api-key`. This is exactly what it is for. |
| Edit files that live on the SERVER, driven from another PC | Not the API — use `agent.sh gui <port> --lan --token <secret>` and launch tasks from the browser on the other PC. |
| Edit files that live on the OTHER PC | Install the skill there and run agents locally on it. No API can do this; the agent has no access to the caller's disk. |

This starts a standalone HTTP server that translates `/v1/chat/completions` calls into
`agent.sh run`/`agent.sh reply` invocations of the chosen CLI subagent and translates
the answer back into an OpenAI-shaped response. Before relying on it, know what you're
actually getting — it is a wire-compatible shim, not a real low-latency LLM API:

- **One ongoing chat session per process, not a fresh agent every call.** The bridge
  remembers the `messages` array from the previous call; when a new call's `messages`
  is that exact array plus new messages appended at the end (a deterministic prefix
  check, not a guess), it resumes the *same* CLI session via `agent.sh reply` with only
  the new tail, instead of resending the whole growing history via a brand-new
  `agent.sh run`. Any mismatch (edited history, an unrelated conversation, the first
  call, or a dead/errored session) falls back safely to a fresh `agent.sh run` with the
  full history. `claude`/`codex`/`kimi`/`opencode` support this (`provider.json`'s
  `supports_resume`); `gemini` always takes the fresh-run path.
- **Trade-off: one bridge process serves one conversation at a time** — a lock
  serializes every request, so don't point multiple unrelated tasks at the same bridge
  port expecting them to stay independent (start one process per port per conversation
  instead). `POST .../reset` clears the remembered session so the next call starts
  completely fresh; `GET /health` reports `session_active`/`session_turns`. An idle
  session also auto-expires after `--session-ttl` seconds (default 1800 = 30 min) —
  the next call after that just starts fresh instead of resuming.
- **Slow to first token** — each call starts a full CLI subprocess (several seconds
  before the first delta). Don't use it anywhere real-time first-token latency matters.
- **`stream: true` is REAL token streaming on the `claude` engine** — the CLI runs in
  stream-json mode, the bridge tails the growing task log and forwards deltas as SSE
  chunks while the model generates; canonical fenced tool calls become
  `delta.tool_calls` chunks as each call's JSON closes. A short holdback keeps limit
  banners convertible to HTTP 429, and an end-of-turn full parse reconciles any
  non-canonical call spellings (they arrive late but correct). `--no-live-stream`
  reverts to the legacy replay; non-live engines (codex/kimi/opencode/gemini) always use
  the legacy replay of the finished answer as word-sized SSE chunks.
- **`tools`/function-calling is emulated via prompting**, not native — best-effort. The
  bridge accepts every spelling seen live: a JSON `{"tool_calls":[...]}` block; one
  fenced OpenAI-shaped call object PER CALL (Sonnet 5's habit; known tool names only);
  literal call lines as `name(arg=value, ...)` pairs (codex's habit), `name({"arg":
  "value"})` with one positional JSON object (gpt-5.5's habit), or `name("scalar")`
  mapped onto a one-parameter function's sole schema property; and whole-message bare
  argument-object lines whose keys fit exactly one tool (spark's habit). The prompt
  warns that describing an action in prose is ignored/failed. Echo protection: a
  call-syntax line exactly repeating an already-executed call (same name + canonical
  args) is summary prose, not re-executed; fenced `{"tool_calls":[...]}` stays exempt.
  Re-sent on every `tools` call, even a continuation turn.
- **Empty completions retry, bridge bugs return OpenAI-style errors.** An empty or
  `error`-state CLI invocation is re-run (`--retries`, default 1); an unexpected bridge
  exception returns `{"error": {...}}` HTTP 500 instead of a bare connection reset; a
  provider usage-limit banner becomes an HTTP 429 `rate_limit_error`, never a normal
  completion.
- **The wrapped CLI is locked to text-only completion — real CLI flags, not just a
  prompt ask.** Every subprocess gets `AGENT_CHAT_ONLY=1`, which makes codex run with
  `--sandbox read-only --ignore-user-config` (no writes, and skips
  `~/.codex/config.toml` so real configured MCP servers like a live `unityMCP` aren't
  reachable; host reads remain, so Codex bridges are loopback-only), Claude runs with
  `--strict-mcp-config --tools ""`, Kimi uses an explicit
  `tools: []` agent profile, and **opencode runs under an agent profile whose tool map is
  `{"*": false}`** (`providers/opencode/chat-only.json`, applied through `OPENCODE_CONFIG` +
  `--agent neoxider-chat-only`; the unsafe native `serve` path is loopback-only). Codex also
  ignores `AGENT_CODEX_SANDBOX`, and Claude keeps
  `--permission-mode acceptEdits` while ignoring `AGENT_CLAUDE_PERMISSION`. This is a security
  boundary: the HTTP endpoint turns arbitrary callers into CLI invocations, so environment overrides
  must never grant it write, shell, or permission-bypass access. These restrictions apply only to
  bridge subprocesses — a normal `agent.sh run` keeps full access. Verified live: `-c
  mcp_servers={}` alone did NOT stop a real MCP call from succeeding; the flags above
  do. opencode was verified the same way and used to FAIL it (2026-08-13, opencode 1.18.16): a
  plain LAN request answered with `apply_patch, bash, edit, glob, grep, question, read, skill,
  todowrite, webfetch, websearch, write` plus 40 live `unityMCP` tools and really created a file
  on disk; with the profile the same question answers `NONE`. **`-e gemini` is the one engine
  with no full lockdown** — Gemini CLI can drop writes/exec (`--approval-mode plan`, which
  chat-only now uses instead of `--yolo`) but has no flag that removes the READ tools, so a
  gemini-backed bridge can still be asked to read local files. Don't put that one on a network.
- **`usage` token counts are estimates** (~4 chars/token, `"neoxider_estimated": true`)
  — useful for cost panels, not billing-grade.
- **`content` is a clean answer for every bundled engine.** `codex` would otherwise mix
  its startup banner/session-id/error-log/"tokens used" chrome (and a cp866-mojibake line
  on Windows) into the answer, so its provider runs `codex exec --json` and extracts only
  the final agent message (`_provider_codex_emit`) — this also cleaned up `agent.sh last`
  and the GUI chat view for codex. Kimi's stream-json provider performs the same clean extraction;
  `claude`/`opencode`/`gemini` were already clean.
- **A very long conversation is handed over as a file, not as an argument.** The bridge
  stages any prompt over ~16000 characters in a temp file and passes `--prompt-file`,
  because a command line is capped near 32000 characters and the spawn fails outright
  above it. From there only `claude` and `opencode` can carry it (their CLIs read a
  prompt from stdin); `codex` and `gemini` refuse with a message naming the size.
- One process = one fixed engine/model/effort for its whole lifetime. To compare
  models, run the command again with different `-e/-m/-f/-p` on another port.

**GUI (`agent.sh gui`).** A lightweight local web control panel (python-stdlib, zero
dependencies): project→subagents tree, chat with each agent, launching new tasks, provider
limits and `doctor` — one GUI covers all providers, shared `LOGDIR`, safe alongside CLI-launched
tasks (paths are normalized, meta writes are locked). An **"API" tab** starts/stops
`openai-server` bridges from the browser — pick provider+model+port (default opencode/big-pickle),
get a running-bridges list with live `/health`, inline per-bridge request logs, a copy-`/v1`-URL
button and a stop button (no CLI needed). Task status is conveyed by the
activity/topic emoji (✅⏳❌⚠️📖✏️🔧💭🐛🧪…) plus strikethrough for finished tasks. Stable
port: CLI arg > `$AGENT_GUI_PORT` > `8765`; re-running `gui` while one is up just opens the
browser.

**Driving the panel from another computer.** `agent.sh gui <port> --lan --token SECRET` binds all
interfaces so a second PC (or a phone) can open the panel and launch/reply to tasks in the
browser — the agents still run, and change files, on the HOST machine. The token is not optional:
this panel starts subagents in full-auto mode (`--dangerously-skip-permissions`,
`--sandbox danger-full-access`, `--yolo`, `--auto`) in any directory the caller names, so an
unauthenticated LAN panel is remote code execution and `--lan` **refuses to start** without
`--token` (or `$AGENT_GUI_TOKEN`). The other device opens
`http://<this-host>:<port>/?token=SECRET` once; the panel redirects to a query-free URL and stores
the token in an HttpOnly cookie. When configured, the token is required even on loopback, so an
HTTPS reverse proxy cannot bypass it. Over the internet, put it behind a VPN or an HTTPS reverse proxy; there
is no TLS in the panel itself. If that port is held by **something else** (it happened: an unrelated WebSocket server on
8765, which used to make `gui` print success while the browser tab failed with
`invalid Connection header`), the panel now identifies the occupant, moves to the next free port and
prints the chosen URL in a banner you cannot miss. Providers are plugins (`providers/<name>/provider.json` + `provider.sh`) — adding a
CLI is one new directory, zero edits to `agent.sh`/`gui.py`. Implementation details
(tree/i18n/toasts/splitters/caching/path normalization): [docs/GUI.md](docs/GUI.md).

**Task state.** After every step the wrapper sets in `.meta`:
`state` (`running`/`done`/`waiting`/`error`), the `exit` code, `files` (how many files the agent changed
per `git status`), `pid` + `winpid`, `started`, and `timeout` (set only when the step watchdog killed the
task). Icons in `list`/`status`: `▶` running, `▷` running-but-silent, `✔` done, `⏳` waiting,
`✖` error, `⚠` stalled.

**Working or stuck (liveness) — one truth for CLI and GUI.** Both `agent.sh status`/`list` and the web
panel run the SAME state machine (`eff_state` in `agent.sh` and in `gui.py`):

| Situation | State | Shown as |
|---|---|---|
| process dead, meta still says running | `stalled` | `⚠ stalled` → `agent.sh reply <name> "continue"` |
| process alive, log growing | `running` | `▶ running (alive, pid N)` |
| process alive, no output for > `AGENT_STALE_SEC` | `idle` | `▷ running (no output for Nm)` |
| process exited after the provider dropped the turn | `error` | `✖ error` → read the log tail, then resume with `reply` (see below) |

`idle` is an honest third state, not an error: a codex/claude step flushes its log only when the step
ENDS, so silence alone never means dead. This is what used to make the CLI say *running* and the GUI say
*stalled* about the very same task — the CLI looked only at the pid, the GUI only at the log's mtime.
(On Windows the two now compare notes through `winpid`, because a git-bash pid means nothing to python.)
`agent.sh clean`, including `clean --purge`, skips `idle` exactly like `running`; otherwise it could
delete a live quiet process's `.log`/`.meta` while that process was still writing to them.

**No silent forever-hangs.** Every step runs under `AGENT_TIMEOUT_SEC` (default 30 min). On expiry the
whole process tree is killed — including the native Windows grandchild (`codex.exe` and whatever it
spawned), which plain `timeout`/`kill` leaves orphaned — the log gets an explicit
`!! TIMEOUT: step exceeded AGENT_TIMEOUT_SEC=<N>s and was killed` line, and the task ends as
`state=error exit=124`. `agent.sh status` then says `⏱ killed by the step watchdog after <N>s` instead of
pretending the task is still working.

opencode emits throttled activity heartbeats into that log while preserving a clean final-answer block.
Its unattended run has a 30-minute hard deadline by default; override it with
`AGENT_OPENCODE_TIMEOUT_SEC=<seconds>` (`0` disables the deadline). opencode stderr is retained with an
`[opencode]` prefix, so auth/network/plugin failures are diagnosable instead of looking like silent hangs.

**Durable checkpoint (survives shutdown).** After every step a `<name>.md` is generated — a
human-readable markdown file: a header (state/engine/session/dir/changed files/resume command) + the
whole thread. Plus the codex/claude/kimi sessions themselves live on disk, so even
after a reboot the task continues via `agent.sh reply <name> "continue"`. By default the agent also
maintains its own **per-task** `PROGRESS.<task>.md` in the working directory (Summary/TL;DR, checklist,
step-by-step log with findings, conclusions) and reads it on resume — resumable after a crash and
readable by an orchestrator without re-running the agent. The filename is keyed by task name so several
agents sharing one working directory never clobber each other's progress. Pass `--no-progress` to
disable it for trivial one-shots. (Tip: add `PROGRESS.*.md` to the project's `.gitignore`.)

**"Agent asked a question" detection.** If the last lines of output look like a question, `state=waiting`
and the wrapper prints `⏳ the agent appears to have ASKED a question — reply: agent.sh reply <name> "..."`.
This makes it visible that the subagent is waiting for an answer rather than stuck. `agent.sh status <name>`
shows the question itself and the current stage of work.

**Pre-flight `doctor`.** Before launching a batch of subagents, run `agent.sh doctor`: it checks the
presence and versions of the CLIs (codex/claude/kimi/opencode/gemini), login state where available,
and codex's **remaining
limits** — primary (5h window) and secondary (weekly) with % and time until reset (from session-jsonl).
At >80% it prints a warning — in that case it's better to throttle the fan-out.

`agent.sh doctor --deep` adds what none of the above can see: a **real** one-shot run per engine that
must actually EXECUTE a shell command (codex today — `provider_<engine>_doctor_deep` is a plugin hook,
so another provider can add its own). It runs in a throwaway temp dir holding a single randomly-named
file and asks the model to list the directory; the random name can only come back if the whole
tool-call round trip works, and the command itself only reads. Output is one line:

```
  codex shell: ok   (gpt-5.3-codex-spark, 10s, real command executed)
  codex shell: BROKEN — no answer within 60s, the tool router is hung (hint: …)
```

It costs one cheap model call (~10–15s), so plain `doctor` stays instant and just prints a reminder
that the deep check exists. Run `--deep` when an agent "answers but does nothing", after a codex-cli
upgrade, or on a new machine.

### A turn that died on the provider: resume it, don't restart it

**Symptom:** a task ends `state=error` after only seconds or minutes, its log tail is full of
`{"type":"error","message":"Reconnecting... n/5 ..."}` followed by `{"type":"turn.failed", ...}`, and
the cause is transport, not the task — `unexpected status 403 Forbidden`, `tls handshake eof`,
`stream disconnected before completion`, or `os error 10054`. Provider-side outages hit long turns
hardest, so an xhigh/high-effort worker doing a big audit dies while a short one beside it survives.

**The session is not lost.** The CLI session lives on disk, so `agent.sh reply <name> "..."` picks the
task up with its full context — the same mechanism `stalled` uses. Do NOT start a fresh `run` with a
re-pasted prompt: you throw away everything the agent already worked out, and you pay for it twice.

**But check the working tree BEFORE you resume.** A failed turn is not an atomic rollback. The agent
may have written some files and not others before the transport died, and it does not know which. Seen
live: one task had added its new failing tests but not the fix they were meant to prove, leaving the
repository in a state where the suite was red on purpose but nothing said so. So:

1. `git status --short` / read the files and establish what actually landed.
2. Resume with that state stated explicitly — "your turn died on a provider network error; I checked
   the tree, X landed and Y did not, continue from there" — rather than letting the agent re-derive
   it, which is where it will guess wrong.
3. Prefer resuming over re-running even when little landed; the session context is the expensive part.

**Adapt the shape of the work while an outage lasts.** Long single turns keep dying, so split the job
into several short focused tasks and `fan` them: each turn is shorter, more of them survive, and the
ones that die are cheap to resume. A four-part audit that completes beats one exhaustive audit that
never finishes.

### Codex: every shell command hangs (environment issue)

**Symptom:** `agent.sh run -e codex …` starts, the model replies, but every shell command it issues
hangs forever; the task sits in `state=running` for hours with a log that never grows. Sometimes the
agent reports `windows sandbox: helper_unknown_error: setup refresh had errors`.

**Cause (diagnosed live, codex-cli 0.144.0, Windows):** `~/.codex/config.toml` is owned by the ChatGPT
desktop app. It declares a stdio MCP `node_repl` — the "code-mode host" living under
`AppData\Local\OpenAI\Codex\runtimes\cua_node\…` — plus a `notify` hook into the desktop helper and
HTTP MCP servers that only answer while the app is up. Started from a plain shell, that host is
unreachable and codex's tool router blocks on the FIRST exec call:
`ERROR codex_core::tools::router: error=code-mode host closed its stdout`. The same command with
`--ignore-user-config` finished in 360 ms.

**What the wrapper does:** every codex run and resume now passes `--ignore-user-config`. Auth is
unaffected (`codex exec --help`: the flag skips `config.toml` only, "auth still uses `CODEX_HOME`"),
and as a bonus a trivial run got ~2.5× faster (37s → 15s) even with the desktop app running.
Need one of those MCP servers back? Do it surgically:
`AGENT_CODEX_MCP="unityMCP=http://127.0.0.1:8040/mcp"`. Only if you truly want the old behaviour:
`AGENT_CODEX_USER_CONFIG=1`.

**Other junk in `~/.codex/` (not touched by the wrapper, your call as the user):** `codex doctor` on
this machine dies with `memory allocation of 1060480 bytes failed`, and traces show
`codex_models_manager: failed to renew cache TTL: missing field 'base_instructions'` — a stale models
cache. The directory also holds multi-megabyte orphaned `..codex-global-state.json.tmp-*` /
`.bak.tmp-*` files dating back months. Neither breaks agent.sh runs, and neither is deleted for you:
if `codex doctor` matters to you, clean those leftovers by hand (that directory is your environment,
not the wrapper's).

**"One thread per task" model**: every `run` creates a `<name>.log` (full transcript) and a `<name>.meta`
(engine/model/dir/session). All `reply` calls are APPENDED to that same `<name>.log` with headers
`========== [run|reply] ... ==========`, so the whole dialogue can be read as a single file by both you
and the user. `reply <name>` fetches the session id and directory from meta by itself — there's no need
to specify `-C`.

It can be run in the background (`run_in_background`) — stdin is closed in the script, so the agent
won't hang waiting for input. While the agent is working in the background — watch it via
`agent.sh log -f <name>`.
NEVER call `codex exec` without `</dev/null` outside this wrapper.

Gotchas (verified):
- Always call `reply` with the task name (`-t` at run time) or the session id — with no argument it
  picks up the last task, which is DANGEROUS with parallel workers (you'll end up in someone else's
  session).
- `reply <name>` does its own `cd` into the task's directory from meta (the resume session's cwd = the
  process's cwd).
- Give the task a meaningful name via `-t` — otherwise the name will be `task-<timestamp>`.

## Model selection

**Codex** (`-e codex`; no longer the default engine):

| Alias | Model | When |
|---|---|---|
| `5.6-terra` / `terra` (default) | `gpt-5.6-terra`, effort medium | regular tasks |
| `5.6-sol` / `sol` | `gpt-5.6-sol`, effort medium | 5.6 variant / explicit Sol request |
| `high` | `gpt-5.6-sol`, effort high | harder than usual (rare; big stuff is better done yourself) |
| `luna` | `gpt-5.6-luna` | 5.6 variant |
| `spark` / `5.3` | `gpt-5.3-codex-spark` | very simple: renames, minor text/docs edits, one-line fixes |

If the user explicitly names a model ("codex luna", "spark 5.3") — use that one. A raw model id still
passes through unchanged, so `-m gpt-5.5` reaches the older model on demand.
The 5.6 family (`sol`/`luna`/`terra`) requires **codex-cli >= 0.144** (older CLIs get a 400
"requires a newer version of Codex"); update with `npm install -g @openai/codex@latest`.

**Claude** (`-e claude`, **the default engine**):

| Alias | Model / effort | When |
|---|---|---|
| `opus5` (default) | `claude-opus-5` | everything, unless a cheaper model clearly suffices — the default per the user's request |
| `sonnet` | `claude-sonnet-5`, effort **high** | cheaper/faster than Opus 5 for routine work |
| `sonnet-medium` / `sonnet-low` | `claude-sonnet-5`, effort medium/low | cheaper/faster, when high is overkill |
| `opus` / `haiku` | no explicit effort (CLI default) | `opus` is the OLD Opus 4.8 alias (kept deliberately); haiku — trivial tasks |

General pattern: `<model>-<effort>` (low/medium/high/xhigh/max) on any alias overrides the effort,
e.g. `opus-high`. Implementation — `provider_claude_resolve()` in `providers/claude/provider.sh`.

**Kimi Code** (`-e kimi`):

| Alias | Model | When |
|---|---|---|
| `k3` / `default` (default) | `kimi-code/k3` | regular and hard agentic work |
| `k3-256k` | `kimi-code/k3-256k` | explicit smaller-context K3 route |
| `coding` | `kimi-code/kimi-for-coding` | managed coding route |
| `highspeed` | `kimi-code/kimi-for-coding-highspeed` | managed high-speed coding route |

Kimi has no CLI effort flag, so `-f` is ignored. Authenticate once with `kimi login`; then verify
the provisioned aliases with `kimi provider list`. Do not invent K2.5/K2.7 aliases: they are not
present in the live managed catalog verified on 2026-08-09. Raw configured aliases pass through.

## Rules for setting tasks

The scoping/verification/git rules live in the orchestrator loop above — follow them for every
delegation, plus:

- Do not include secrets/tokens in prompts.
- Don't hand agents security or architecture decisions "to think over" — decide yourself,
  delegate the mechanical execution.

## For the user (launching from a terminal)

A handy alias (git-bash, add to `~/.bashrc`):

```bash
alias agent='bash ~/.claude/skills/neoxider-agents/agent.sh'
# agent run -t readme -C /c/Git/CoreAI "fix the typo in the README"
# agent log -f readme     # watch what the agent is doing, in real time
# agent last readme       # short summary: the agent's last answer
# agent reply readme "also fix the CHANGELOG"   # append a message to the same session
```

## Ready-made analogues (research, 2026-07)

There's no ready-made tool for this headless niche (launch → per-task log → reply/resume →
machine-readable state) — all the mature "managers" (claude-squad, vibe-kanban, uzi, crystal) are
built for interactive parallel development in a git worktree with a TUI/GUI. A custom wrapper is
justified. Ideas worth borrowing:
- **CCManager** (github.com/kbwo/ccmanager) — the best 4-state "waiting for input" detection with
  per-CLI patterns and status hooks; multi-provider. If question detection starts lying — compare
  patterns with it.
- **caut** (github.com/Dicklesworthstone/coding_agent_usage_tracker) — a generalized `doctor`: limits
  across 16+ providers, JSON/Markdown output. Could be shelled out to instead of our own codex parser
  (requires cargo).
- The closest bash wrappers in spirit: **sage**, **Agent AFK**, **agx** (checkpoint wake/work/sleep) —
  small, but useful as a source of ideas.
