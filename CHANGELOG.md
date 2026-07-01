# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows
[Semantic Versioning](https://semver.org/).

## [Unreleased]

- Stable, documented GUI port: `gui.py` resolves explicit CLI arg > `$AGENT_GUI_PORT`
  env var > `8765` default, instead of drifting across manual invocations.
- `neoxider` bare invocation now prints a usage summary instead of auto-opening the
  browser GUI; `neoxider gui [port]` opens it explicitly, `neoxider help` prints the
  full `agent.sh` command reference.
- New `/api/stream?task=<name>` (Server-Sent Events) and `/api/wait?task=<name>&timeout=<sec>`
  endpoints — real-time log tailing and a synchronous blocking-poll convenience call,
  so the API can be consumed without a manual polling loop.
- Provider plugin architecture: `providers/<name>/provider.sh` + `provider.json`,
  `agent.sh provider-info <engine>` — adding a provider is now one new directory, zero
  edits to `agent.sh`/`gui.py`.
- GUI refactor: modular `static/*.js`/`static/style.css` instead of one large
  `gui.html`, i18n (English default, Russian second locale, easy to add more),
  cached + manually-refreshable doctor/rate-limit panels, dropped the redundant
  status dot, prettier scrollbars.
- Concurrency-safety fixes for shared `LOGDIR` access from multiple concurrent
  processes/installs (atomic `meta_set`, collision-resistant auto task names).
- Separate model + effort selectors in the GUI (today effort is baked into the model
  alias string).
- Audited full-auto/non-interactive flags for every provider; documented in the README.

## [0.1.0] - 2026-07-01

Initial public version.

### Added

- `agent.sh`: non-interactive CLI-subagent wrapper (Codex, Claude Code, opencode,
  Gemini CLI) — `run`/`reply`/`log`/`last`/`status`/`list`/`doctor`/`gui`. One
  thread-per-task log+meta model, durable markdown checkpoints, liveness/state
  detection ("did the agent ask a question?"), `-p` `PROGRESS.md` protocol for
  long-running tasks.
- `gui.py` + `gui.html`: zero-dependency local web GUI — project/subagent tree,
  chat-style thread view with markdown, provider/model picker with an adaptive
  rate-limit panel, folder browser, resizable panels, toast notifications with
  history, optional "open in a real terminal" checkbox.
- `neoxider` launcher command (bash + PowerShell): no-arg opens the GUI, any other
  argument passes through to `agent.sh`.
- Claude Code plugin packaging (`.claude-plugin/plugin.json` + `marketplace.json`) —
  installable via `/plugin marketplace add` + `/plugin install`, no file relocation
  needed (root-level `SKILL.md` auto-detects as a single-skill plugin).
- MIT license.

### Fixed

- `<name>.meta`'s `model=` field now records the *resolved* model + effort (e.g.
  `claude-sonnet-5-high`, `gpt-5.3-codex-spark-medium`) instead of the raw CLI alias
  or the literal string `"default"`.
- Pinned Claude's default model to the explicit id `claude-sonnet-5` (the `sonnet`
  CLI alias was resolving to a stale `claude-sonnet-4-6` on this account/CLI version)
  with `effort high` by default.
