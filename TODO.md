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
- [ ] **Local HTTP API test-driver (`test-api` mode).** Scoped, ready-to-build design
  in [`docs/IDEAS.md`](docs/IDEAS.md#local-http-api-test-driver-api-test-mode) — not
  yet implemented. Smallest viable version: `--base-url` + `--goal` only, reusing the
  existing `run` mechanism with a prompt template, no new provider-plugin machinery.
- [ ] **`/plugin install` round-trip, unverified.** The plugin packaging (see
  Distribution above) hasn't been tested by an actual install on a second machine yet.
