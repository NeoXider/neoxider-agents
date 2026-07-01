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
- Verified live: non-streaming, streaming, tool-calling, multi-turn history, and a
  full tool-call → tool-result → final-answer round-trip against a real CLI subagent
  (Claude) all confirmed working end-to-end via curl, including two concurrent
  requests with no task-name collision. Wire-compatibility with CoreAI's
  Game-Creation Benchmark integration point (`COREAI_TEST_BASE_URL`) was confirmed by
  design/code-reading, not by running the actual Unity benchmark suite.
- Fix: the bridge could leak a stray fenced ```` ```json {"tool_calls":[]} ```` ````
  block into a plain-prose `content` string — observed live when a tool result was
  fed back and the model (correctly) decided no further call was needed but still
  echoed an empty tool-call block out of habit. `extract_tool_calls` now strips any
  recognized tool-call JSON fence from the displayed text regardless of whether it
  produced a real (non-empty) call, and the prompt instructions were tightened to
  discourage emitting it in the first place.
- Added a `messages` array required/non-empty validation (`400` instead of silently
  running an agent with an empty prompt).
- `/v1/chat/completions` requests now correctly return `400` for both an empty and a
  missing `messages` field, and `404` for any path that doesn't end in
  `/chat/completions` — verified live.
- Documented a real, pre-existing caveat surfaced by manual testing: `codex`'s
  non-interactive `exec` mode mixes its own startup banner/session-id/error-log lines
  into the same output stream as the answer (same raw text `agent.sh last`/the GUI's
  chat view already show for codex tasks) — this bridge does not attempt
  engine-specific cleanup, so `claude`/`opencode`/`gemini` are recommended when a
  clean `content` string matters to the caller.

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
