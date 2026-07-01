# TODO

Roughly in priority order. PRs welcome.

## Bugs

- [x] **Model name isn't resolved in the UI.** Fixed — `run`/`reply` now overwrite
  `<name>.meta`'s `model=` with the resolved model+effort (e.g. `claude-sonnet-5-high`,
  `gpt-5.3-codex-spark-medium`) right after `provider_codex_resolve`/`provider_claude_resolve`
  run, instead of the raw alias/`"default"`. opencode/gemini still show `"default"`
  when no `-m` was given (they have no `provider_<name>_resolve` alias-resolution function —
  see the provider plugin architecture below).

## Provider plugin architecture

- [x] **Done.** Adding a provider now means creating `providers/<name>/provider.sh` +
  `providers/<name>/provider.json`, with zero edits to `agent.sh`/`gui.py`/`gui.html`.
  `agent.sh` sources every `providers/*/provider.sh` at startup and dispatches `run`/`reply`
  generically via `declare -F provider_<engine>_run_cmd` / `_resume_cmd`. `agent.sh
  provider-info <engine>` calls `provider_<engine>_doctor` (single-line JSON); `doctor` loops
  over discovered providers and pretty-prints the same way as before. `gui.py` glob-loads
  `providers/*/provider.json` for display metadata and shells out to `provider-info` for
  `/api/provider` instead of hardcoding per-engine Python. `providers.json` was deleted
  (fully superseded). The 4 existing providers (codex/claude/opencode/gemini) were migrated
  with identical CLI flags, effort resolution, and rate-limit JSON shape.
- This also fixes "doctor and the per-provider limits panel should go through one path
  and be cached together" — see Caching below (caching itself is still open).

## GUI

- [x] **Caching for doctor/limits.** Done — server-side cache, 30s TTL, keyed per
  `doctor`/`provider:<engine>`. A ⟳ refresh button next to the limits panel and inside
  the doctor modal forces a fresh fetch (`?force=1`). Verified: first call ~9s
  (uncached), second call ~0.2s (cached), `?force=1` bypasses it (~9s again). The panel
  never blanks while refreshing — keeps the last-known-good data with a spinner.
- [x] **Remove the redundant status dot.** Done — dropped the colored dot; status is
  now emoji + strikethrough only. A running task's emoji pulses gently so "still live"
  doesn't disappear along with the dot.
- [x] **Split `gui.html` into smaller files.** Done — `static/util.js` (shared
  primitives + state), `i18n.js`, `toast.js`, `tree.js`, `chat.js`, `modals.js`,
  `splitters.js`, `app.js` (entry point), `style.css`. `gui.html` is now a thin shell.
  These are plain classic `<script src>` tags sharing one global scope (no bundler, no
  modules) — every shared primitive (`$`, `esc`, `jget`, state vars) is declared
  exactly once in `util.js` to avoid duplicate-declaration errors across files.
- [x] **i18n.** Done — `locales/en.json` (default) + `locales/ru.json`, a `t(key)`
  helper with automatic English fallback for any key a locale doesn't cover (so a
  partial translation still works — this is what makes adding a locale a drop-in-one-
  file operation), a header language picker persisted in localStorage. `/api/locales`
  lists available locales dynamically, so a third `locales/<code>.json` shows up in the
  picker with no code change.
- [ ] **Diff rendering.** Render unified diffs in the chat thread as a real diff view
  (colored +/- lines, file headers) instead of a plain code block — codex/claude output
  often contains real diffs worth rendering nicely.
- [x] **Prettier scrollbars.** Done — `::-webkit-scrollbar` styling matching the dark
  theme (thin, rounded, using the existing CSS custom properties) for every scrollable
  area, plus a Firefox fallback (`scrollbar-width`/`scrollbar-color`).
- [ ] Re-look at the task-list interaction after CliDeck/agent-of-empires comparison
  (see below) — e.g. a compact "running now" summary strip, or a Claude-Code-style
  background-tasks flyout, if it turns out to be clearer than the current tree.
- [x] **Separate model + effort selectors, for every provider.** Done — a new `-f
  <effort>` flag/CLI arg (separate from `-m`) flows through `agent.sh`'s generic
  dispatch to `P_EFFORT`, overriding whatever a provider's own `_resolve` derived from
  an alias suffix (backward compatible: `sonnet-high` via `-m` alone still works if you
  don't pass `-f`). Each `provider.json` now has an `efforts` array (`codex`:
  medium/high; `claude`: low/medium/high/xhigh/max; `opencode`: minimal/low/medium/
  high/max via `--variant`; `gemini`: none) driving a second GUI dropdown, correctly
  disabled when empty. Verified end-to-end via a real GUI-launched task
  (`model=claude-sonnet-5-low` from separate `model:"sonnet"`/`effort:"low"` fields).
- [x] **Audit full-auto/non-interactive flags for every provider, document in README.**
  Done — codex/claude were already covered. Found and fixed two real gaps: gemini now
  gets `--yolo`, opencode now gets `--dangerously-skip-permissions` (both confirmed
  via each CLI's own `--help`, and smoke-tested — gemini's own output now reads "YOLO
  mode is enabled"). Without these, a non-interactive run (`</dev/null` stdin, by
  design) could have hung forever on an approval prompt it could never answer. README
  now has a table of the flag each provider uses, in the "Adding a provider" section.

## API

- [x] **Stable, documented GUI port.** Done — `gui.py` resolves its port as explicit
  CLI arg > `$AGENT_GUI_PORT` env var > `8765` default. `agent.sh`'s `gui)` case now
  only forwards an explicit port arg through to `gui.py` (previously it always
  injected `${1:-8765}`, which silently defeated the env var since python always saw
  an argv[1]). Verified: `AGENT_GUI_PORT=9000 agent.sh gui` binds 9000 with no CLI
  arg; `agent.sh gui 9100` overrides both.
- [x] **`neoxider` bare-invocation convention changed.** Done — bare `neoxider` (no
  args) now prints a short usage summary instead of silently auto-opening the browser
  GUI (surprising side effect, inconsistent with how most CLIs treat a bare
  invocation). `neoxider gui [port]` explicitly opens the web dashboard;
  `neoxider help` prints the full `agent.sh` command reference via a new `agent.sh
  help` case. Everything else (`neoxider run ...`, `neoxider doctor`, `neoxider
  test-api ...`) is unchanged, passed straight through to `agent.sh`.
- [x] **`/api/stream?task=<name>` (SSE).** Done — a `text/event-stream` endpoint that
  tails a task's `.log` file and pushes each new line as a `data: ...` event as the
  agent produces output, instead of requiring the client to poll `/api/thread`. Ends
  with `event: done` once the task's state leaves `running`, or after a fixed idle
  timeout with no new lines.
- [x] **`/api/wait?task=<name>&timeout=<sec>`.** Done — a blocking-poll convenience
  endpoint: holds the response open (server-side polling `.meta` every ~0.5s) until
  state leaves `running` or `timeout` seconds elapse (capped at 300s), then returns
  one JSON object `{"name":..., "state":..., "model":..., "log":...}`. Makes the API
  usable synchronously from a test harness or a Unity/C# test with a single call
  instead of a manual polling loop.
- [x] **"Does the API feel like a real API?" (history/tool-calls/streaming) and
  "one shared server vs. one-per-provider" — both answered.** Design write-up in
  [`docs/IDEAS.md`](docs/IDEAS.md#local-http-api-test-driver-api-test-mode): history
  is just the existing `.log` file, tool calls are the wrapped CLI's own real
  shell/file actions (no new schema), streaming/waiting are the two endpoints above;
  and the server design stays a single shared `gui.py` instance where every request
  carries its own `engine`/`model`/`effort`, not a server-per-provider.

## Distribution

- [x] **Package as an installable Claude Code plugin.** Done —
  `.claude-plugin/plugin.json` + `.claude-plugin/marketplace.json` at the repo root.
  `SKILL.md` stays flat at the root (no `skills/` subfolder needed — Claude Code
  v2.1.142+ auto-detects a root-level `SKILL.md` with no `skills/` dir and no `skills`
  manifest field as a single-skill plugin). Install via
  `/plugin marketplace add NeoXider/neoxider-agents` then
  `/plugin install neoxider-agents@neoxider-agents`. Not yet verified with a real
  `/plugin install` round-trip on a second machine — worth double-checking once
  someone else tries it.

## Research

- [x] **CLI-as-API for automated testing.** Answered — no ready cross-vendor solution
  exists; each vendor has its own official headless/SDK mode instead (Claude Code's
  `-p --output-format stream-json` + Claude Agent SDK; Codex's `codex exec --json` +
  Codex SDK; Gemini CLI's `-p --output-format json`; opencode has CLI flags only, no
  SDK). Purpose-built eval harnesses (Promptfoo, Harbor/Terminal-Bench) target agent
  *quality* benchmarking, not embedding control in another app. Recommendation: extend
  this tool's own pattern with a thin `test-api` wrapper rather than adopt a framework
  — full design proposal in [`docs/IDEAS.md`](docs/IDEAS.md#local-http-api-test-driver-api-test-mode).
- [x] **Deeper CliDeck / agent-of-empires comparison.** Done — confirms neither
  competitor has anything like this project's rate-limit panel or i18n (genuine
  differentiators); both require a real build step (Node/Rust) vs. this project's
  zero-dependency stdlib+bash. Three concrete future ideas captured in
  [`docs/IDEAS.md`](docs/IDEAS.md#clideck--agent-of-empires-deep-dive-findings):
  cross-session "ask a sibling agent" relay, `agent_detect_as`-style status-detection
  mapping for custom/forked CLI binaries, and a raw unparsed live-output pane as an
  escape hatch alongside the markdown chat view.
- [x] **Cross-tool instruction-file conventions.** Answered — Codex CLI and opencode
  both natively read `AGENTS.md` (an open, Linux-Foundation-governed cross-tool
  standard as of Dec 2025, supported by 28+ tools); Claude Code reads it as secondary
  context (`CLAUDE.md`/`SKILL.md` stay primary); Gemini CLI has its own `GEMINI.md`
  convention and does not support `AGENTS.md`. Implemented: `AGENTS.md` and
  `GEMINI.md` now ship at the repo root alongside `SKILL.md`.

## Platform

- [ ] **macOS support — unverified.** Nothing in the design is Windows-only in
  *principle* (bash + python stdlib), but it has only been tested on Windows/git-bash.
  Known Windows-specific bits to double check on Mac:
  - `gui.py`'s `AGENT_SH_BASH` git-bash path fallback (`C:\Program Files\Git\...`) —
    harmless no-op on Mac (falls through to plain `bash`, which is correct there).
  - `to_git_bash_path()`'s `C:/...`→`/c/...` regex — only matches Windows drive-letter
    paths, so it's a no-op on POSIX paths (should be safe, not yet tested).
  - `spawn(..., terminal=True)`'s `CREATE_NEW_CONSOLE` — already branches on
    `os.name == "nt"`; the `else` branch uses `start_new_session=True` but doesn't
    actually open a *visible* terminal window on Mac/Linux (no equivalent of
    `open -a Terminal` is wired up yet) — needs a real Mac terminal-launch branch.
  - Never tested end-to-end on an actual Mac. Needs someone with a Mac to verify.

## Nice to have

- [x] Translate `SKILL.md` fully to English — done, verified no stray Cyrillic remains
  (one leftover Russian diagnostic-string quote was found and fixed by hand after the
  bulk translation pass).
- [x] **Local HTTP API test-driver (`test-api` mode).** Done — `agent.sh test-api
  --base-url <url> --goal "<what to verify>"` (plus the usual `-e`/`-m`/`-f`/`-C`/`-t`
  and an optional `--out <path>`), a thin wrapper on `run` (shares `_do_run_dispatch`,
  tagged `kind=api-test`) with zero new provider-plugin machinery, exactly per the
  design in [`docs/IDEAS.md`](docs/IDEAS.md#local-http-api-test-driver-api-test-mode).
  The agent exercises the API via its own shell/curl capability (no MCP/new tool-use
  needed) and returns one strict JSON object; `--out` extracts it robustly (tolerates
  a markdown-fenced or annotated answer despite the instruction not to). Also exposed
  in the GUI as a new **API tab**: a form (base URL/goal/provider/model/effort), a
  results list parsing each run's JSON into a pass/fail summary + per-endpoint detail,
  and ready-made curl/C# (Unity `UnityWebRequest`) snippets for calling the same
  `/api/test-api` GUI endpoint from your own test suite. Verified end-to-end twice
  against a real local Python HTTP server (via the CLI directly and via the GUI form)
  — real HTTP calls, real structured JSON, correctly rendered pass/fail in the tab.
- [ ] **`/plugin install` round-trip, unverified.** The plugin packaging (see
  Distribution above) hasn't been tested by an actual install on a second machine yet.
- [x] **Test coverage.** Done — `tests/test_agent_sh.sh` (48 bash assertions: the
  `meta_set` concurrency lock incl. a 10-way concurrent regression test, both
  providers' `_resolve` alias parsing, the collision-resistant default task name) and
  `tests/test_gui.py` (28 Python `unittest` tests: `to_git_bash_path`, `eff_state`,
  activity/topic emoji, `list_locales`, the `_serve_static` traversal guard). Zero
  dependencies (stdlib/bash only, no pytest/bats), scratch temp dirs so running them
  never touches the real `~/.claude/agent-cli-logs`. Independently re-verified both
  suites still pass (48/48, 28/28 OK) after the API-tab/animation changes above.
- [x] **GUI animations.** Done — researched Apple's actual HIG motion principles and
  the real `CAMediaTimingFunction easeInEaseOut` curve (`cubic-bezier(.42,0,.58,1)`,
  not the commonly-confused Material-design curve), applied consistently (buttons,
  task rows, modals sliding up + fading per Apple's default sheet transition, toasts)
  at HIG-cited durations (~0.2s micro-interactions, ~0.3s modal reveal), and added
  `prefers-reduced-motion` support per HIG's accessibility guidance.
- [x] **GUI branding + doctor/limits pre-warming.** Header now reads "Neoxider"
  (linked to the repo) instead of generic "agent". `gui.py` pre-warms the doctor +
  every provider's cache in a background thread on server startup, so switching
  providers doesn't eat a ~9s cold shell-out the first time — verified a provider
  shows `cached: true` on its very first request after the server comes up.
