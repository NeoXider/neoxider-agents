# neoxider-agents

A tiny local control room for AI coding subagents across multiple CLI providers —
**Codex, Claude Code, opencode, Gemini CLI** (and any future CLI you add) — from one
non-interactive bash wrapper plus an optional zero-dependency web GUI.

No daemon, no database, no npm/cargo build step. `agent.sh` is plain POSIX shell;
`gui.py` is Python stdlib only; `gui.html` is one static file with vanilla JS.

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
  topic emoji), a chat-style thread view with basic markdown, a provider/model picker
  whose rate-limit panel adapts to whichever provider is selected, a folder browser to
  add new projects, resizable panels, toast notifications with history, and an
  optional "open in a real terminal" checkbox per task.
- **Plugin providers.** Every CLI provider (invocation, model/effort resolution,
  `doctor` info, and GUI display metadata) is one `providers/<name>/` directory —
  adding a provider means creating that directory, with zero edits to `agent.sh`,
  `gui.py`, or `gui.html`.

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
# run a task
bash agent.sh run -t fix-readme -C /path/to/project "fix the typo in the README"

# watch it live
bash agent.sh log -f fix-readme

# it asked a question? reply in the same thread
bash agent.sh reply fix-readme "yes, use option B"

# check state / diff / limits before a big batch
bash agent.sh status fix-readme
bash agent.sh doctor

# or drive all of the above from a browser
neoxider
```

### The `neoxider` command

`neoxider` with no arguments opens the web GUI in your browser. Any other argument is
passed straight through to `agent.sh`, so `neoxider run ...`, `neoxider doctor`, etc.
all work exactly like `bash agent.sh run ...`. See [`bin/README.md`](bin/README.md)
for one-time setup (installer scripts for PowerShell/bash, or manual PATH edit).

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

- `provider.json`: `label`, `models`, `default_model`, `limits` (`"codex"`-style tag or
  `null`) — picked up by the GUI's dropdown, model list, and rate-limit panel.
- `provider.sh` defines a small function contract, named `provider_<name>_*`:
  - `provider_<name>_resolve MODEL_ALIAS` (optional) — sets `P_MODEL`/`P_EFFORT` from an
    alias. Providers without alias resolution (e.g. opencode/gemini today) can skip this;
    the raw `-m` value is passed straight through.
  - `provider_<name>_run_cmd DIR MODEL EFFORT PROMPT` — runs the CLI for a new task.
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

## Roadmap

See [`TODO.md`](TODO.md) for planned work (i18n, diff rendering, macOS support, etc.)
and [`docs/IDEAS.md`](docs/IDEAS.md) for an open design question about
subagents-spawning-subagents as a real tree.

## Author

[NeoXider](https://github.com/NeoXider)

## License

[MIT](LICENSE)
