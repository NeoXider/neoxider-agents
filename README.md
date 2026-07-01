# neoxider-agents

A tiny local control room for AI coding subagents across multiple CLI providers —
**Codex, Claude Code, opencode, Gemini CLI** (and any future CLI you add) — from one
non-interactive bash wrapper plus an optional zero-dependency web GUI.

No daemon, no database, no npm/cargo build step. `agent.sh` is plain POSIX shell;
`gui.py` is Python stdlib only; the frontend is plain classic `<script>` files (no
bundler, no framework).

By [NeoXider](https://github.com/NeoXider).

## Why

Every mature multi-agent "manager" (claude-squad, Vibe Kanban, Uzi, Crystal, CliDeck,
agent-of-empires…) is built for *interactive* parallel development in git worktrees,
with a TUI/GUI on top of Node/Go/Rust tooling. None of them gives a clean **headless**
API — launch a task, log it as one readable thread, reply/resume by name, read back a
machine-readable state — that you can script against, with zero external dependencies
and a rate-limit panel that adapts to whichever provider you picked. That gap is what
this project fills.

## Features

- **One thread per task.** `run` creates `<name>.log` + `<name>.meta`; every `reply`
  appends to the *same* log with a timestamped header, so the whole conversation with
  a subagent reads as one file — no session hunting.
- **Live state, not just logs.** `running` / `waiting` (agent asked a question) /
  `done` / `error` / `stalled` (process died — e.g. the machine was turned off),
  detected automatically and shown everywhere.
- **Durable checkpoints.** A markdown snapshot (`<name>.md`) is written after every
  step. Combined with the CLI's own resumable sessions, a task survives a reboot —
  `agent.sh reply <name> "continue"` picks up where it left off. `-p` makes the agent
  keep its own `PROGRESS.md` checklist in the working directory too.
- **`doctor`**: which CLIs are installed, codex login state, and live rate-limit bars
  (primary/secondary window, % used, time to reset) before you fan out a batch of
  subagents.
- **Web GUI** (`neoxider` / `agent.sh gui`): a project tree of subagents (activity +
  topic emoji, no redundant status dot), a chat-style thread view with basic markdown,
  a provider/model/**effort** picker whose cached, manually-refreshable rate-limit
  panel adapts to whichever provider is selected, a folder browser to add new
  projects, resizable panels, toast notifications with history, an optional "open in a
  real terminal" checkbox per task, and a language picker (English/Russian, more via
  one dropped-in locale file).
- **Plugin providers.** Every CLI provider (invocation, model/effort resolution,
  `doctor` info, and GUI display metadata) is one `providers/<name>/` directory —
  adding a provider means creating that directory, with zero edits to `agent.sh`,
  `gui.py`, or `gui.html`.
- **Model and effort are separate.** `-m <model> -f <effort>` (or two dropdowns in the
  GUI) — not baked into one alias string — for every provider that has an effort
  concept (codex, claude, opencode's `--variant`; gemini has none).
- **`test-api` mode + GUI "API" tab.** Point an agent at a local HTTP API
  (`agent.sh test-api --base-url <url> --goal "<what to verify>"`) and get back one
  strict JSON pass/fail report — the agent exercises your API with real HTTP calls via
  its own shell/curl capability, no new architecture needed. The GUI's API tab wraps
  the same `/api/test-api` endpoint with a form + results view, and shows ready-made
  curl/C# (Unity `UnityWebRequest`) snippets for calling it from your own test suite.
- **A real API, not just a GUI backend.** History = the task's own `<name>.log` file,
  already the full multi-turn conversation (every `run`/`reply` appends to the same
  log with a timestamped header) — `/api/thread?task=<name>` exposes it as-is, nothing
  extra to build. Tool calls = the underlying CLI's own real shell/file actions (each
  provider — Codex/Claude/opencode/Gemini — already executes real commands and edits
  when it runs a task; those show up verbatim in the log, there's no separate
  "tool call" schema layered on top). Streaming = `/api/stream` below, and synchronous
  waiting = `/api/wait` below — both consume that same log/state, they don't add a new
  data model.
- **`/api/stream?task=<name>`** — a Server-Sent Events (`text/event-stream`) endpoint
  that tails a task's `.log` file in real time and pushes each new line as a `data:
  ...` event as the agent produces output, instead of requiring the client to poll
  `/api/thread`. It ends (an `event: done` message, then closes) once the task's state
  leaves `running`, or after a fixed idle timeout with no new lines. Consumable from
  any language that can read an SSE/chunked HTTP stream — `EventSource` in JS, or a
  simple line-buffered HTTP GET in curl/Python/C#.
- **`/api/wait?task=<name>&timeout=<sec>`** — a convenience blocking-poll endpoint:
  holds the HTTP response open (polling the task's `.meta` state server-side every
  ~0.5s) until the task's state leaves `running` or `timeout` seconds elapse (capped
  at 300s), then returns one JSON object `{"name":..., "state":..., "model":...,
  "log":...}`. Turns a kick-off call (`/api/test-api` or `/api/run`) plus one
  `/api/wait` call into a synchronous round-trip, for callers like a test harness or a
  Unity/C# test that don't want to hand-roll a polling loop.

## Installation

**No package manager, no dependencies to install.** `agent.sh` is plain POSIX shell;
`gui.py` uses only the Python 3 standard library (`http.server`, `json`, `subprocess`,
`urllib`, `glob`, ...) — there is no `requirements.txt`, no `pip install`, no
`npm install`, nothing to build.

**Option A — as a Claude Code plugin** (recommended if you use Claude Code):

```
/plugin marketplace add NeoXider/neoxider-agents
/plugin install neoxider-agents@neoxider-agents
```

That registers `SKILL.md` for you automatically — no manual file copying.

**Option B — plain git clone** (works regardless of which CLI(s) you use it from):

```bash
git clone https://github.com/NeoXider/neoxider-agents.git
cd neoxider-agents
```

That's it — you're installed. Requirements (already have these if you use any of the
CLIs below):

- `bash` (git-bash on Windows, already installed with Git for Windows; native on macOS/Linux)
- Python 3 (any recent version; standard library only, needed only for the GUI, not
  for `agent.sh` itself)
- At least one of the CLIs it wraps: [Codex CLI](https://github.com/openai/codex),
  [Claude Code](https://github.com/anthropics/claude-code), opencode, or the Gemini CLI
  — install and log in to whichever one(s) you plan to use, `agent.sh doctor` will tell
  you what it can see.

Optional one-time setup: the [`neoxider`](bin/README.md) command (a one-word launcher
for the GUI, from bash or PowerShell).

## Quick start

```bash
# run a task (add -f <effort> to set effort separately from -m <model>, e.g. -f high)
bash agent.sh run -t fix-readme -C /path/to/project "fix the typo in the README"

# watch it live (log's own -f means "follow" -- a different meaning of -f, only for `log`)
bash agent.sh log -f fix-readme

# it asked a question? reply in the same thread
bash agent.sh reply fix-readme "yes, use option B"

# check state / diff / limits before a big batch
bash agent.sh status fix-readme
bash agent.sh doctor

# point an agent at a local HTTP API and get a structured pass/fail report
bash agent.sh test-api --base-url http://127.0.0.1:8080 \
  --goal "check /health returns ok, then POST /item and GET it back" --out result.json

# or drive all of the above from a browser
neoxider gui
```

### The `neoxider` command

Bare `neoxider` (no arguments) prints a short usage summary — it does **not** open
the GUI, on the theory that a bare CLI invocation should be help, not a side effect.
`neoxider gui [port]` explicitly opens the web dashboard in your browser.
`neoxider help` prints the full `agent.sh` command reference. Everything else —
`neoxider run ...`, `neoxider doctor`, `neoxider test-api ...`, etc. — is passed
straight through to `agent.sh`, so it all works exactly like `bash agent.sh run ...`.
See [`bin/README.md`](bin/README.md) for one-time setup (installer scripts for
PowerShell/bash, or manual PATH edit).

### GUI port

`gui.py`'s web server listens on a stable, documented port: an explicit CLI arg, else
`$AGENT_GUI_PORT`, else `8765` — resolved in that order, so the same URL is
bookmarkable across restarts instead of drifting between manual invocations. Set a
different fixed port once via `export AGENT_GUI_PORT=9000`, or override it for a
single launch with `neoxider gui 9000` (equivalently `agent.sh gui 9000`).

### Multiple installs / shared machines

All state (`.meta`/`.log`/`.md` per task) lives under `AGENT_CLI_LOGS`
(`~/.claude/agent-cli-logs` by default) — shared on purpose, so one GUI shows every
subagent regardless of which provider or which install launched it, with concurrent
writes to the same task made safe by a portable file lock. If you want strict
isolation instead (e.g. two people sharing a machine, or a "personal" vs "CI" split),
point different installs at different directories:

```bash
export AGENT_CLI_LOGS="$HOME/.neoxider-ci-logs"
```

### Working with other AI coding CLIs

This repo's usage instructions are duplicated across the conventions different tools
read automatically: [`SKILL.md`](SKILL.md) (Claude Code), [`AGENTS.md`](AGENTS.md)
(Codex CLI and opencode natively; Claude Code as secondary context), and
[`GEMINI.md`](GEMINI.md) (Gemini CLI, which hasn't adopted the shared `AGENTS.md`
convention). Whichever CLI you're chatting with, it should pick up the same baseline
instructions with no extra setup.

## CLI reference

See [`SKILL.md`](SKILL.md) for the full command reference (model aliases, the
question-detection heuristic, path-normalization notes, and known trade-offs) — it
doubles as the operating manual an AI agent reads before using this tool.

## Adding a provider

Create `providers/<name>/provider.sh` and `providers/<name>/provider.json` — nothing
else needs to change. `agent.sh` auto-discovers and sources every `providers/*/provider.sh`
at startup; `gui.py` glob-loads every `providers/*/provider.json` for display metadata.

- `provider.json`: `label`, `models`, `efforts` (list of effort levels this provider
  supports, e.g. `["low","medium","high"]`, or `[]` if it has no effort concept),
  `default_model`, `default_effort`, `limits` (`"codex"`-style tag or `null`) — picked
  up by the GUI's model dropdown, the *separate* effort dropdown, and the rate-limit panel.
- `provider.sh` defines a small function contract, named `provider_<name>_*`:
  - `provider_<name>_resolve MODEL_ALIAS` (optional) — sets `P_MODEL`/`P_EFFORT` from an
    alias (e.g. a `-high` suffix). Providers without alias resolution (opencode/gemini
    today) can skip this; the raw `-m` value is passed straight through as `P_MODEL`,
    and `-f <effort>` (see below) is the only way to set their `P_EFFORT`.
  - `provider_<name>_run_cmd DIR MODEL EFFORT PROMPT` — runs the CLI for a new task.
    `EFFORT` is whatever `-f` (or `_resolve`'s own suffix parsing) produced — pass it to
    whatever flag your CLI uses for a reasoning-effort/verbosity level, if it has one.
  - `provider_<name>_resume_cmd DIR SESSION ANSWER` (optional) — resumes an existing
    session for `reply`; omit it if the CLI has no resume/continue support.
  - `provider_<name>_doctor` — prints one line of JSON to stdout:
    `{"engine":...,"version":...,"available":true|false,"login":...,"limits":{...}|null,"note":...}`.

**Every provider must run fully unattended — no confirmation prompts.** This tool always
runs CLIs with stdin closed (`</dev/null`), by design, so a subagent never hangs waiting
for input — but that also means a provider that *can* block on an approval prompt will
hang forever if `provider_<name>_run_cmd` doesn't pass its "don't ask, just do it" flag.
When adding a provider, find and pass that flag:

| Provider | Flag |
|---|---|
| Codex | `--sandbox workspace-write --skip-git-repo-check` |
| Claude Code | `--permission-mode acceptEdits` |
| Gemini CLI | `--yolo` (auto-approve all tool actions) |
| opencode | `--dangerously-skip-permissions` |

See `providers/codex/` and `providers/claude/` for full worked examples (alias
resolution, effort suffixes, and codex's rate-limit JSON parsing).

## Development

```bash
bash tests/test_agent_sh.sh   # bash logic: meta_set locking, provider alias resolution, ...
python tests/test_gui.py      # gui.py's pure functions: path normalization, state, ...
```

Zero dependencies here too — stdlib/bash only, no pytest or bats. See
[`tests/README.md`](tests/README.md).

## Roadmap

See [`TODO.md`](TODO.md) for planned work (diff rendering, macOS support, etc.) and
[`docs/IDEAS.md`](docs/IDEAS.md) for open design questions (subagents spawning
subagents as a real tree, ideas borrowed from CliDeck/agent-of-empires).

## Author

[NeoXider](https://github.com/NeoXider)

## License

[MIT](LICENSE)
