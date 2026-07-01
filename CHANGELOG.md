# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows
[Semantic Versioning](https://semver.org/).

## [Unreleased]

- New `tests/live_smoke_openai_server.py`: a standalone, deliberately-manual end-to-end
  smoke test for `openai_server.py` against a real CLI subagent (not part of the fast
  unit suites, since it costs real subscription usage) — health/error responses, a
  fresh completion, session continuation with real context recall, a tool-call round
  trip, divergence, `/reset`, idle-timeout expiry, streaming, and concurrency, all
  against a scratch `AGENT_CLI_LOGS` that never touches the real one. Verified live:
  23/23 checks passed against `claude`.
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
- Root-caused an occasional garbled/mojibake line in `codex`'s raw output (e.g.
  `ᯥ譮: ,  䨪஬ ...`): it's a Windows OS notification ("process N terminated") printed
  in the console's cp866 codepage, mis-decoded as UTF-8 by the `utf-8`-assuming
  subprocess capture shared with `agent.sh`/`gui.py`. Documented, not fixed (project-
  wide capture behavior, out of scope for this bridge alone).
- Fix: `model` in responses/`/health`/`/v1/models` showed the bare CLI alias with no
  version number (`"claude/sonnet-low"`, `"claude/opus"`), not which real model that
  alias resolves to. Added a `model_labels` alias→display-name map to
  `providers/{claude,codex,gemini}/provider.json` (`"sonnet"` → `"Sonnet 5"`, `"opus"`
  → `"Opus 4.8"`, `"haiku"` → `"Haiku 4.5"`, `"spark"` → `"GPT-5.3 Codex Spark"`, etc.);
  `model_label()` now shows `"claude/Sonnet 5 (low)"` / `"claude/Opus 4.8"`. Verified
  live end-to-end for both aliases.
- Confirmed (live, outside this bridge entirely) that `opencode` currently fails with
  `UnknownError: Unexpected server error` on every model tried, including an
  authenticated one (`zai/glm-4.5-flash`) — reproduces identically via the raw
  `opencode run` CLI with zero `agent.sh`/bridge involvement, so it's an
  environment/opencode-side issue, not a bug in this project.
- **Session-continuation model for `openai-server`, replacing the earlier stateless
  design**: one bridge process now keeps one ongoing chat session, not a fresh agent
  every call. The bridge remembers the `messages` array from the previous call, and
  when a new call's `messages` is a deterministic extension of it (exact prefix check,
  not a guess), only the new tail is sent to the *same* underlying CLI session via
  `agent.sh reply` (resume) instead of resending the whole growing history through a
  brand-new `agent.sh run`. Any mismatch (edited/rolled-back history, a genuinely
  different conversation, the first call ever, or a previous session that ended in
  `error`/`stalled`) falls back safely to a fresh `agent.sh run` with the full history.
  This both avoids resending an ever-growing prompt and lets the underlying provider's
  own prompt caching apply, since the CLI sees one real growing conversation instead of
  a brand-new mega-prompt every time.
- Added `"supports_resume"` to every `provider.json` (`claude`/`codex`: `true`;
  `opencode`/`gemini`: `false`) — engines without resume support always take the
  fresh-run path, every call.
- New `POST .../reset` endpoint: clears the remembered session (drops the remembered
  `messages`/task, wipes the scratch working dir unless `--dir` was pinned to a real
  project) so the next call starts completely fresh. `GET /health` and `GET /` now also
  report `session_active` (bool) and `session_turns` (message count in the remembered
  array).
- New `--session-ttl` flag (default `1800` = 30 minutes): an idle session is treated
  exactly like a dead one once it's gone unused longer than this, so an abandoned
  conversation can't be resumed forever or grow unbounded. `GET /health` now also
  reports `session_idle_seconds`/`session_ttl_seconds`. Verified live with
  `--session-ttl 8`: an extension call sent 12s after the last one correctly fell back
  to a fresh `agent.sh run` with the full history instead of resuming (task count
  incremented, log showed a `[run]` block, not `[reply]`) — same correct answer either
  way, just without the token-saving continuation.
- The session's working directory now persists for the session's whole lifetime
  (previously a disposable per-call scratch dir) — wiped and recreated only when a
  brand-new session starts (divergence, reset, or first-ever call), never touched when
  `--dir` pins a real project path.
- Verified live: Claude — a 2-turn history followed by a 3rd-turn recall question
  produced the correct answer, and the task log showed exactly one `[run]` block
  followed by one `[reply]` block containing only the new tail; the task-file count
  stayed at 1 across 4 sequential calls (8 messages of session state). A genuinely
  different conversation sent next correctly triggered a new session (task count
  1→2, `session_turns` reset to 1). `POST .../reset` correctly cleared the session
  (`session_active` back to `false`), and the next call after reset started yet
  another new session (task count → 3). Streaming (`stream: true`) works on a
  continuation call too, not just fresh sessions.
- Verified live: Codex — the same continuation mechanism reused the same underlying
  session id across 2 calls, task count stayed at 1, and correctly recalled a fact
  from 2 turns earlier.
- Verified live: concurrency safety — two genuinely concurrent, unrelated one-shot
  requests both got their own correct answers with zero cross-contamination; the
  `SESSION_LOCK` serializes overlapping requests, and since the second request's
  messages don't extend the first's, it correctly falls back to its own fresh session.
- Verified live: Gemini (no resume support) — every call, including an "extension"
  one, correctly created a brand-new task, with zero errors, confirming graceful
  degradation for engines without `supports_resume`.
- Documented a new, pre-existing `codex`/`agent.sh` quirk surfaced by this work:
  `provider_codex_resume_cmd` does not forward the `--effort`/model flags on resume
  (unlike `claude`, which needs and gets them re-sent) — a resumed `codex` session may
  silently run at a different reasoning effort than the one it started with. No fix
  needed or possible from this bridge's side.

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
