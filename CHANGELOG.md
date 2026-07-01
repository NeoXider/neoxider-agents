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
- New `agent.sh openai-server` command + standalone `openai_server.py`: an
  OpenAI-compatible `/v1/chat/completions` HTTP bridge backed by a CLI subagent
  (claude/codex/opencode/gemini), so any OpenAI-compatible client can use a CLI-agent
  subscription as its "model" instead of a real provider API key.
- `stream: true` and `tools`/function-calling support in the bridge, both emulated on
  top of the underlying CLI: streaming replays an already-finished answer as
  word-sized SSE chunks (connection closed after `data: [DONE]`); tool-calling is
  prompted (fenced JSON tool-call block) and reparsed into a real OpenAI `tool_calls`
  response.
- One bridge process = one fixed engine/model/effort; run several instances on
  different ports to compare models/providers side by side.
- Verified live: non-streaming, streaming, and tool-calling round-trips against a real
  CLI subagent (Claude) all confirmed working end-to-end via curl. Wire-compatibility
  with CoreAI's Game-Creation Benchmark integration point (`COREAI_TEST_BASE_URL`) was
  confirmed by design/code-reading, not by running the actual Unity benchmark suite.

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
