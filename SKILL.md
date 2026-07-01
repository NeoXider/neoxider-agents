---
name: cli-agents
description: Delegating tasks to CLI subagents (Codex by default; also Claude Code, opencode, gemini) through the agent.sh wrapper — launching, model selection (gpt-5.5 medium by default, spark for trivial tasks), answering agent questions via resume, logs.
---

# CLI Subagents (Codex Orchestration)

For subagent tasks use **Codex CLI** through the wrapper
`C:\Users\User\.claude\skills\cli-agents\agent.sh` (git-bash). Use Claude subagents (Agent tool)
only if the task requires the context of the current conversation.

## Commands

```bash
SK=~/.claude/skills/cli-agents/agent.sh
bash $SK run  -t fix-readme -C /c/Git/Proj "prompt" # codex, gpt-5.5 medium (default); -t = task name
bash $SK run  -p -t big-job -C dir "prompt"         # -p: agent maintains PROGRESS.md (resumable after shutdown)
bash $SK run  -m spark -C /c/Git/Proj "prompt"      # trivial task -> spark
bash $SK run  -e claude -m haiku -C dir "prompt"    # a different CLI: claude/opencode/gemini
bash $SK reply fix-readme "answer"                  # continue the task by name (session/dir taken from meta)
bash $SK reply <session-uuid> "answer"              # or by uuid; with no argument — the last task
bash $SK log  fix-readme                            # the entire task thread (run + all replies in one file)
bash $SK log  -f fix-readme                         # follow a live background agent (tail -f)
bash $SK log  -l fix-readme                         # only the last step
bash $SK last fix-readme                            # only the agent's last answer
bash $SK status fix-readme                          # state: state/stage/changed files/whether a reply is needed
bash $SK list                                       # table: state / engine / model / age / files / session
bash $SK doctor                                     # pre-flight: engines + codex limits (before fanning out!)
bash $SK gui [port]                                 # web control panel over all providers (default :8765)
```

**GUI (`agent.sh gui`).** A lightweight local web control panel (python-stdlib, zero dependencies): a
project→subagents tree on the left, a chat with the agent (markdown + bubbles) in the center, launching
a new task and the selected provider's limits on the right, a `doctor` button with all the limits.
Statuses as a circle+emoji (✅⏳❌⚠️ + activity 📖✏️🔧💭 and task topic 🐛📖🧪…). Files: `gui.py` + `gui.html`.
- **Idempotency**: one GUI for all providers, `LOGDIR` is shared. If a server is already up on the port —
  a repeated `gui` doesn't crash or duplicate it, it just opens the browser. Parallel workers each write
  to their own `<name>.meta/.log`, so a shared overview is safe.
- **Tree**: nesting by `parent` (see `-P`). A parent task → its subagents indented.
  Projects = tasks ∪ explicitly registered ones via 📂＋ (even with 0 tasks, `projects.json` in `LOGDIR`).
  The active project is on top and expanded; each project has its own scroll (`max-height`), so a large
  project (many tasks) doesn't push the others out of view.
- **Path normalization**: the GUI (native Windows python) and `agent.sh` (git-bash) write the same
  path differently (`C:\Git\X` vs `C:/Git/X` vs `/c/Git/X`). `gui.py`'s `to_git_bash_path()` converts
  ANY path to unix-style (`/c/...`) at the moment it's sent to `-C` / saved to `projects.json` — otherwise
  tasks from the GUI would be grouped separately from tasks in the same folder launched from the CLI.
- **Providers are plugins**: each CLI lives entirely under `providers/<name>/` — `provider.json`
  (label/models/limits/default_model) drives the GUI's dropdown, model list, and adaptive limits
  panel; `provider.sh` defines `provider_<name>_resolve`/`_run_cmd`/`_resume_cmd`/`_doctor`.
  `agent.sh` auto-sources every `providers/*/provider.sh` and dispatches generically — adding a
  new CLI is one new directory, zero edits to `agent.sh`/`gui.py`/`gui.html`.
- **Terminal**: an "open in terminal" checkbox (off by default) — when enabled, the GUI spawns `agent.sh`
  in a separate console window (`CREATE_NEW_CONSOLE`) where you can see live output; without the checkbox — as usual, silently.
- **Toasts + history**: any completion/error/agent question — a popup notification for 3s, plus
  history via 🔔 (persisted in localStorage). A universal CSS spinner (`.spinner`) — for doctor,
  the limits panel, and the run/reply buttons while a request is in flight.
- **Splitters**: the left and right panels can be dragged with the mouse, width is saved to localStorage.
- Launch it via `agent.sh gui` (it sets the git-bash path for python); running `python gui.py` directly
  also works — there's a fallback to typical git-bash paths.

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
after a reboot the task continues via `agent.sh reply <name> "continue"`. For long tasks the `-p` flag
makes the agent maintain its own `PROGRESS.md` in the working directory and read it on resume.

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
| `5.5` (default) | `gpt-5.5`, effort medium | regular tasks |
| `5.5-high` | `gpt-5.5`, effort high | harder than usual (rare; big stuff is better done yourself) |
| `spark` / `5.3` | `gpt-5.3-codex-spark` | very simple: renames, minor text/docs edits, one-line fixes |

If the user explicitly names a model ("codex spark 5.3", "5.5") — use that one.
`gpt-5.5-codex` is NOT available on this ChatGPT account — only `gpt-5.5`.

**Claude** (`-e claude`):

| Alias | Model / effort | When |
|---|---|---|
| `sonnet` (default) | `claude-sonnet-5`, effort **high** | regular tasks — the new Sonnet 5, default per the user's request |
| `sonnet-medium` / `sonnet-low` | `claude-sonnet-5`, effort medium/low | cheaper/faster, when high is overkill |
| `opus` / `haiku` | no explicit effort (CLI default) | opus — harder than usual; haiku — trivial tasks |

General pattern: `<model>-<effort>` (low/medium/high/xhigh/max) on any alias overrides the effort,
e.g. `opus-high`. Implementation — `provider_claude_resolve()` in `providers/claude/provider.sh`.

## Rules for setting tasks

- **Small, precisely doable tasks**: one file/one topic, exact paths, exact signatures,
  "do exactly this, nothing more." Don't hand agents tasks with security/architecture decisions "to
  think over."
- In every prompt: **"Do NOT run git commit"** (only the orchestrator commits, after verification).
- After every worker, check the `git diff` of its files; the orchestrator runs the build/tests.
- Parallel workers — only on non-overlapping files.
- Do not include secrets/tokens in prompts.

## For the user (launching from a terminal)

A handy alias (git-bash, add to `~/.bashrc`):

```bash
alias agent='bash ~/.claude/skills/cli-agents/agent.sh'
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
