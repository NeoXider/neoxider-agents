# Ideas (open questions, not decided yet)

## CliDeck / agent-of-empires deep-dive findings

Confirms the earlier first-pass finding: neither tool has anything resembling this
project's rate-limit panel or i18n — a genuine differentiator, not something to copy.
Both are Node/Rust with a real build step (CliDeck: `npx clideck`; agent-of-empires:
`cargo build --release`), vs. this project's zero-dependency Python-stdlib + bash.

Three concrete ideas worth considering as future work (not yet scoped/implemented):

1. **Cross-session "ask another agent" relay (from CliDeck).** A session can inject a
   question into another session's real terminal, wait for it to finish, and pipe the
   answer back as command output. Could let a subagent consult a sibling (e.g. a
   reviewer asking the implementer a clarifying question) without manual copy-paste —
   ties into the "subagents spawning subagents as a real tree" idea above.
2. **`agent_detect_as = "claude"`-style status-detection mapping for custom/forked
   CLIs (from agent-of-empires).** Lets a wrapped/renamed binary reuse an existing
   provider's question-detection heuristic instead of never showing `waiting`. Would
   help anyone running a fork/wrapper of Codex/Claude/etc. under a different binary name.
3. **A raw live-output pane as an escape hatch (from agent-of-empires' raw tmux pane
   next to its structured view).** This project's log/thread model already captures
   full output, so a literal "tail -f, unparsed" panel — separate from the
   markdown-rendered chat view — could help when the question-detection heuristic
   misfires and a user wants to see exactly what the CLI printed.

## Subagents spawning subagents as a real tree

`agent.sh` already supports `-P <parent>` (or `AGENT_PARENT` env var) to record a
task's parent for the GUI tree, and the GUI already renders nesting based on it. But
right now nothing actually *uses* it end-to-end, for two separate reasons:

1. **Subagent-calls-subagent.** A subagent (say, a codex worker) could itself have
   access to this skill (the path to `agent.sh`) and spawn its *own* subagents via
   `-P <its-own-task-name>`, forming a real multi-level tree instead of a flat list.
   This is genuinely useful for a worker that wants to fan out sub-tasks of its own.

2. **Chat-calls-subagent.** When *this conversation* (a regular Claude Code / Codex
   chat session, not a task launched by `agent.sh`) spawns a subagent, that subagent
   should show up in the tree as a child of "this session" — not just of another
   task. That needs a stable identifier for "the current chat session" to pass as
   `-P`, which doesn't exist today (there's no natural `$SESSION_ID` env var the shell
   can read). One option: have the *skill instructions* tell the agent to invent and
   remember one short slug per conversation (e.g. derived from the first task name it
   creates) and pass it via `-P`/`AGENT_PARENT` for every subsequent `run` in that
   conversation, so all of a session's subagents nest under one synthetic root.

Both are appealing but under-specified — in particular, whether a synthetic
per-session root should itself appear as a (fake) task in the tree, or just be a
grouping key with no task file of its own. Left as an open design question rather
than implemented; revisit once there's a concrete use case (e.g. a worker that
genuinely needs to fan out).

## macOS terminal launch

If/when `TODO.md`'s macOS item is picked up, the terminal checkbox's `spawn(...,
terminal=True)` needs a real "open a visible terminal window running this command"
branch for Mac — likely `open -a Terminal.app <script>` or similar — mirroring what
`CREATE_NEW_CONSOLE` does on Windows.

## Concurrency safety when this tool is used from multiple places at once

The design is intentionally provider-agnostic and installation-agnostic: `agent.sh` /
`gui.py` don't care who invoked them — a human terminal, a Claude Code session, a
Codex/opencode/Gemini agent, or several of those at once, all sharing the same
`LOGDIR` (`~/.claude/agent-cli-logs` by default) so the GUI shows one unified tree
regardless of source. That's the right shared-interface design (there's no need for
per-provider silos — one dashboard is the whole point). But sharing one `LOGDIR`
across concurrent, uncoordinated processes has two real gaps, found while thinking
through "what if this skill is invoked from several agents/providers at the same
time":

1. ~~**`meta_set`'s read-modify-write isn't atomic across processes.**~~ **Fixed.**
   Wrapped the read-modify-write in a portable `mkdir`-based mutex (`_meta_lock`/
   `_meta_unlock` in `agent.sh` — `mkdir` is atomic on every POSIX filesystem; `flock`
   isn't reliably available in git-bash, so this doesn't depend on it). Verified with
   10 concurrent writers to one `.meta` file: without the lock, only 1 of 10 keys
   survived (plus outright `mv` errors from colliding temp files); with it, all 10
   survive every time.
2. ~~**Auto-generated task names can collide.**~~ **Fixed.** Default name is now
   `task-<timestamp>-<pid>` — two processes can never share a PID, so they can never
   collide on the same `.meta`/`.log` even starting in the same second.

Both landed as a small, focused follow-up right after the provider-plugin migration
merged (deliberately not done *during* that refactor, to avoid two concurrent edits to
the same file colliding — which would have been a delightfully on-the-nose
demonstration of exactly the problem being fixed).

The `AGENT_CLI_LOGS` env var already gives a clean escape hatch for anyone who *wants*
strict namespace isolation between installs (e.g. two people sharing a machine, or a
"personal" vs "CI" split) — now documented in the README's Installation section,
rather than adding new namespacing machinery on top.

## Local HTTP API test-driver ("api-test" mode)

Design proposal for a separate feature (from research prompted by: "I have CoreAI and
other libraries — I'd like to use Claude/Codex/etc. as an API for automated testing,
pointed at a local HTTP server"). Prior research confirmed there's no ready open-source
solution for this (each vendor has its own official headless mode instead — Claude
Code's `-p --output-format stream-json` + Claude Agent SDK, Codex's `codex exec --json`
+ Codex SDK, Gemini CLI's `-p --output-format json` — none has a dedicated
"drive-me-as-a-test-harness" framework on top). Proposed shape:

**Interface** — a new subcommand, not a new provider:
```
agent.sh test-api --base-url http://localhost:7777 \
  --spec ./unity-debug-api.yaml \        # optional OpenAPI/Swagger, or a flat endpoint list
  --goal "Verify player spawn, inventory add/remove, and save/load round-trip" \
  --provider codex --model gpt-5.5 --out ./results/api-test-run1.json
```

**How it drives the CLI** — no new infrastructure needed: Claude Code, Codex, and
Gemini CLI's non-interactive modes all already support shell/tool use (a `curl`/
`Invoke-WebRequest` call is just another shell command to them). The prompt states the
base URL + spec/endpoint list + goal, instructs the agent to exercise the API via its
own shell tool, and mandates a structured JSON report as the final message. An
MCP HTTP-client server would be a nicer *upgrade* later (structured request/response
capture instead of parsing free text), but curl-via-shell works today across all three
vendors with zero new moving parts.

**Output contract** — small, flat, one entry per endpoint call, meant to be directly
assertable from e.g. a Unity NUnit test:
```json
{
  "base_url": "http://localhost:7777", "goal": "...", "provider": "codex", "model": "gpt-5.5",
  "overall": "pass|fail|partial",
  "endpoints": [
    { "method": "POST", "path": "/debug/spawn", "assertion": "spawns a player and returns an id",
      "result": "pass", "reason": "" },
    { "method": "GET", "path": "/debug/inventory/12", "result": "fail",
      "reason": "expected item count 1 after add, got 0" }
  ],
  "summary": { "total": 6, "passed": 5, "failed": 1 }
}
```

**Integration sketch** — reuses the existing `run` mechanism as-is, no new
provider-plugin machinery: `test-api` builds a prompt from a template
(interpolating base URL/spec/goal + the output-contract instruction above), calls the
normal `run` path for the chosen provider/model, then validates and post-processes the
agent's final JSON message into `--out`, logged in the shared GUI with a `kind=api-test`
tag for filtering. **Smallest viable first version**: skip `--spec`/OpenAPI parsing
entirely — just `--base-url` + `--goal`, let the agent introspect the API itself
(hit `/`, `/health`, `/openapi.json` if present, or ask the goal text to enumerate
endpoints) and report back in the JSON shape above. Add spec-file support once that
loop is proven out.

The core of this (`test-api` subcommand + GUI API tab) has since shipped — see
`TODO.md`'s "Local HTTP API test-driver" entry. The follow-up design questions below
were resolved alongside it.

### One shared API server, not one-per-provider

Resolved design: a single `gui.py` server instance, where every request
(`/api/run`, `/api/test-api`, etc.) carries its own `engine`/`model`/`effort` fields —
not a separate server process per provider/model combo. Rationale: simpler (one port,
one process, one cache), and the CLI (`agent.sh test-api -e claude -m sonnet -f high
--base-url ... --goal ...`) already gives full per-call provider/model control from
the terminal with zero GUI involved, so a multi-server design would add complexity
without adding capability.

### "Does this feel like a real API?" — history, tool calls, streaming

- **History** = the task's own `<name>.log` file, already the full multi-turn
  conversation (every `run`/`reply` appends to the same log with a timestamped
  header) — nothing new needed there; `/api/thread?task=<name>` already exposed it,
  and `/api/stream`/`/api/wait` (below) just make consuming it easier.
- **Tool calls** = the underlying CLI's own real shell/file actions. Each provider
  CLI (Codex/Claude/opencode/Gemini) already executes real commands and file edits
  when it runs a task — that's not a separate abstraction this project adds, it's
  inherent to how the wrapped CLI works, and it shows up verbatim in the log. There is
  no separate "tool call" schema to build — the log *is* the tool-call transcript.
- **Streaming** = new `/api/stream?task=<name>` — a Server-Sent Events
  (`text/event-stream`) endpoint that tails the task's `.log` file and pushes each new
  line as a `data: ...` event as it's produced, instead of requiring the client to
  poll `/api/thread`. Ends with an `event: done` message once the task's state leaves
  `running`, or after a fixed idle timeout with no new lines.
- **Synchronous waiting** = new `/api/wait?task=<name>&timeout=<sec>` — holds the
  response open (server-side polling `.meta` every ~0.5s) until state leaves
  `running` or `timeout` seconds elapse (capped at 300s), then returns one JSON object
  `{"name":..., "state":..., "model":..., "log":...}`. This is what makes the API
  usable synchronously (e.g. from a test harness or a Unity/C# test) instead of a
  manual polling loop: kick off with `/api/test-api` or `/api/run`, then one
  `/api/wait` call gets the final result.

Not implemented yet: `--spec`/OpenAPI parsing (still the smallest-viable-first-version
scope note above still applies).
