---
name: neoxider-agents
description: Work as an ORCHESTRATOR — plan, decompose and delegate coding tasks to CLI subagents (Codex by default; also Claude Code, opencode, gemini) through the agent.sh wrapper, then verify and integrate their results. Covers launching (run/fan), model selection (gpt-5.6-sol medium by default, spark for trivial tasks), answering agent questions via resume, logs. Use whenever work can be parallelized or offloaded to subagents instead of doing everything in one session.
---

# CLI Subagents (Codex Orchestration)

For subagent tasks use **Codex CLI** through the wrapper
`~/.claude/skills/neoxider-agents/agent.sh` (git-bash; when working inside this repo use
`./agent.sh`).

**NATIVE-FIRST RULE (user, 2026-07-22, permanent):** every orchestrator spawns its OWN
engine's subagents natively; the agent.sh wrapper is ONLY for foreign engines.
- From Claude Code: claude models (opus/sonnet/haiku) → **native Agent tool** (never
  `agent.sh run -e claude`); codex/opencode/gemini → agent.sh.
- From Codex: codex models → native codex subagents; claude/opencode/gemini → agent.sh.

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
   trivial → `spark`/`haiku`, regular → default `sol`/`sonnet`, hard → `-m high`/`opus`.
4. **Delegate.** `run` for one task, `fan` for a parallel batch. Parallel workers only on
   NON-overlapping files. Each keeps its own `PROGRESS.<task>.md`.
5. **Watch.** `list` / `status <name>`; a `waiting` task gets `reply <name> "..."`,
   a `stalled`/`error` one gets its log read and the task re-scoped.
6. **Verify.** Read every finished task's diff yourself — never trust "done" blindly. Run
   builds/tests. Reject and re-delegate anything wrong.
7. **Integrate & commit.** YOU own git: stage, review, commit. Workers must not commit.

> Paste-ready orchestrator system prompt + full which-model-for-what matrix:
> [ORCHESTRATOR.md](ORCHESTRATOR.md).

## Commands

```bash
SK=~/.claude/skills/neoxider-agents/agent.sh
bash $SK run  -t fix-readme -C /c/Git/Proj "prompt" # codex, gpt-5.6-sol medium (default); -t = task name
bash $SK fan  -t audit -C dir "prompt A" "prompt B" # N parallel background tasks from one call
                                                     # (audit-01, audit-02, ...); shared -e/-m/-f/-C;
                                                     # returns at once — poll with list/status.
                                                     # Use instead of a hand-written `run ... &` loop
bash $SK run  -t big-job -C dir "prompt"            # agent keeps PROGRESS.<task>.md by default (per-task, resumable + orchestrator-readable); --no-progress opts out
bash $SK run  --no-terse -C dir "prompt"            # terse (concision) directive is ON by default to save output/turn tokens; --no-terse for exploratory/ambiguous work
bash $SK run  -m spark -C /c/Git/Proj "prompt"      # trivial task -> spark
bash $SK run  -e claude -m haiku -C dir "prompt"    # a different CLI: claude/opencode/gemini
bash $SK run  -m sonnet -f low -e claude -C dir "prompt"  # -f <effort>, separate from -m <model>
bash $SK test-api --base-url http://127.0.0.1:8080 --goal "check /health, then POST+GET /item" --out r.json
                                                     # thin wrapper on `run`: agent exercises a local
                                                     # HTTP API via its own curl/shell, returns strict JSON
bash $SK reply fix-readme "answer"                  # continue the task by name (session/dir taken from meta)
bash $SK reply <session-uuid> "answer"              # or by uuid; with no argument — the last task
bash $SK log  fix-readme                            # the entire task thread (run + all replies in one file)
bash $SK log  -f fix-readme                         # follow a live background agent (tail -f)
bash $SK log  -l fix-readme                         # only the last step
bash $SK last fix-readme                            # only the agent's last answer
bash $SK status fix-readme                          # state: state/stage/changed files/whether a reply is needed
bash $SK list                                       # table: state / engine / model / age / files / session
bash $SK clean                                      # delete md clutter (<name>.md + PROGRESS.<name>.md) of STOPPED
                                                     # tasks; --all incl. waiting, --purge also .log/.meta, -n dry-run
bash $SK doctor                                     # pre-flight: engines + codex limits (before fanning out!)
bash $SK gui [port]                                 # web control panel over all providers (stable default :8765,
                                                     # or $AGENT_GUI_PORT, or a one-off port arg)
```

**Self-testing your own work.** If you just built or modified a local web service/API
(e.g. a Unity `HttpListener` debug endpoint, a small backend), you can verify it works
yourself before declaring the task done:

```bash
bash $SK test-api --base-url http://127.0.0.1:<port> \
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
bash $SK openai-server -e claude -m sonnet -f low -p 8801
# then point any OpenAI-compatible client's base_url at http://127.0.0.1:8801/v1
```

**Reaching the bridge from a phone/APK or another computer (LAN).** By default the bridge
binds `127.0.0.1` (localhost only), so nothing off this machine can connect. Add `--lan` to
bind all interfaces and print this host's LAN URL to point the other device at:

```bash
bash $SK openai-server -e claude -m sonnet -p 8801 --lan
# prints e.g. "LAN: reachable ... at http://192.168.1.115:8801/v1"
# -> set that as the base_url in the APK / on the other PC
```

`--lan` is equivalent to `--host 0.0.0.0`. It only exposes the bridge on the local network,
not the internet. Because the bridge drives a CLI agent with your credentials/tools, use it
only on a trusted network, and open the port in the firewall (the startup banner prints the
exact `New-NetFirewallRule` command for Windows).

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
  full history. Only `claude`/`codex` support this (`provider.json`'s
  `supports_resume`); `opencode`/`gemini` always take the fresh-run path.
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
  reverts to the legacy replay; non-live engines (codex/opencode/gemini) always use
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
  `--sandbox read-only --ignore-user-config` (no shell/file writes, and skips
  `~/.codex/config.toml` so real configured MCP servers like a live `unityMCP` aren't
  reachable) and claude run with `--strict-mcp-config --disallowedTools
  Bash,Edit,Write,NotebookEdit,Task,WebFetch,WebSearch`. Only applies to bridge
  subprocesses — a normal `agent.sh run` keeps full access. Verified live: `-c
  mcp_servers={}` alone did NOT stop a real MCP call from succeeding; the flags above
  do.
- **`usage` token counts are estimates** (~4 chars/token, `"neoxider_estimated": true`)
  — useful for cost panels, not billing-grade.
- **`content` is a clean answer for every bundled engine.** `codex` would otherwise mix
  its startup banner/session-id/error-log/"tokens used" chrome (and a cp866-mojibake line
  on Windows) into the answer, so its provider runs `codex exec --json` and extracts only
  the final agent message (`_provider_codex_emit`) — this also cleaned up `agent.sh last`
  and the GUI chat view for codex. `claude`/`opencode`/`gemini` were already clean.
- One process = one fixed engine/model/effort for its whole lifetime. To compare
  models, run the command again with different `-e/-m/-f/-p` on another port.

**GUI (`agent.sh gui`).** A lightweight local web control panel (python-stdlib, zero
dependencies): project→subagents tree, chat with each agent, launching new tasks, provider
limits and `doctor` — one GUI covers all providers, shared `LOGDIR`, safe alongside CLI-launched
tasks (paths are normalized, meta writes are locked). Task status is conveyed by the
activity/topic emoji (✅⏳❌⚠️📖✏️🔧💭🐛🧪…) plus strikethrough for finished tasks. Stable
port: CLI arg > `$AGENT_GUI_PORT` > `8765`; re-running `gui` while one is up just opens the
browser. Providers are plugins (`providers/<name>/provider.json` + `provider.sh`) — adding a
CLI is one new directory, zero edits to `agent.sh`/`gui.py`. Implementation details
(tree/i18n/toasts/splitters/caching/path normalization): [docs/GUI.md](docs/GUI.md).

**Task state.** After every step the wrapper sets in `.meta`:
`state` (`running`/`done`/`waiting`/`error`), the `exit` code, `files` (how many files the agent changed
per `git status`), `pid`, and `started`. Icons in `list`/`status`: `▶` running, `✔` done, `⏳` waiting,
`✖` error, `⚠` stalled.

**Working or stuck (liveness).** `status`/`list` check whether the process is alive (`kill -0 pid`).
If `state=running` but the process is dead (the machine was shut down / it was killed) → it shows
`⚠ stalled` with the hint `agent.sh reply <name> "continue"`. A live process shows `▶ running (alive)`;
to watch it: `agent.sh log -f <name>`.

**Durable checkpoint (survives shutdown).** After every step a `<name>.md` is generated — a
human-readable markdown file: a header (state/engine/session/dir/changed files/resume command) + the
whole thread. Plus the codex/claude sessions themselves live on disk (`~/.codex/sessions`), so even
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
presence and versions of the CLIs (codex/claude/opencode/gemini), codex login, and codex's **remaining
limits** — primary (5h window) and secondary (weekly) with % and time until reset (from session-jsonl).
At >80% it prints a warning — in that case it's better to throttle the fan-out.

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

**Codex** (`-e codex`, the default engine):

| Alias | Model | When |
|---|---|---|
| `5.6-sol` / `sol` (default) | `gpt-5.6-sol`, effort medium | regular tasks |
| `high` | `gpt-5.6-sol`, effort high | harder than usual (rare; big stuff is better done yourself) |
| `luna` | `gpt-5.6-luna` | 5.6 variant |
| `terra` | `gpt-5.6-terra` | 5.6 variant |
| `spark` / `5.3` | `gpt-5.3-codex-spark` | very simple: renames, minor text/docs edits, one-line fixes |

If the user explicitly names a model ("codex luna", "spark 5.3") — use that one. A raw model id still
passes through unchanged, so `-m gpt-5.5` reaches the older model on demand.
The 5.6 family (`sol`/`luna`/`terra`) requires **codex-cli >= 0.144** (older CLIs get a 400
"requires a newer version of Codex"); update with `npm install -g @openai/codex@latest`.

**Claude** (`-e claude`):

| Alias | Model / effort | When |
|---|---|---|
| `sonnet` (default) | `claude-sonnet-5`, effort **high** | regular tasks — the new Sonnet 5, default per the user's request |
| `sonnet-medium` / `sonnet-low` | `claude-sonnet-5`, effort medium/low | cheaper/faster, when high is overkill |
| `opus` / `haiku` | no explicit effort (CLI default) | opus — harder than usual; haiku — trivial tasks |

General pattern: `<model>-<effort>` (low/medium/high/xhigh/max) on any alias overrides the effort,
e.g. `opus-high`. Implementation — `provider_claude_resolve()` in `providers/claude/provider.sh`.

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
