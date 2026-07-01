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
- **Config-driven providers.** Adding a provider to the picker (label, models,
  whether it exposes rate limits) is a single edit to `providers.json` — no GUI code
  change required. Wiring up how to actually *invoke* a new CLI is a small `case`
  branch in `agent.sh`.

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
for one-time setup (bash and PowerShell).

## CLI reference

See [`SKILL.md`](SKILL.md) for the full command reference (model aliases, the
question-detection heuristic, path-normalization notes, and known trade-offs) — it
doubles as the operating manual an AI agent reads before using this tool.

## Adding a provider

1. Add an entry to `providers.json` (`label`, `models`, `limits`, `default_model`) —
   the GUI's dropdown, model list, and adaptive rate-limit panel pick it up immediately.
2. If the CLI needs special invocation (flags, resume syntax), add a `case` branch for
   it in `agent.sh`'s `run`/`reply`.

(A fully plugin-based provider system — one new file, zero edits to existing files —
is planned; see [`TODO.md`](TODO.md).)

## Roadmap

See [`TODO.md`](TODO.md) for planned work (provider plugin architecture, i18n,
diff rendering, macOS support, etc.) and [`docs/IDEAS.md`](docs/IDEAS.md) for an
open design question about subagents-spawning-subagents as a real tree.

## Author

[NeoXider](https://github.com/NeoXider)

## License

[MIT](LICENSE)
