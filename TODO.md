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

- [ ] **Caching for doctor/limits.** Both should read from one server-side cache (TTL,
  e.g. 30s) instead of shelling out on every panel switch / poll. Add an explicit
  "refresh" button (separate from switching providers) that force-bypasses the cache.
  While refreshing, keep showing the last-known-good data with a small loading
  indicator — don't blank the panel.
- [ ] **Remove the redundant status dot.** The colored dot, the state emoji
  (✅⏳❌⚠️), and the strikethrough all encode the same "is this task done/waiting/
  errored" signal. Drop the dot, keep emoji + strikethrough.
- [ ] **Split `gui.html` into smaller files.** It's grown into one large file (tree,
  chat, doctor modal, browse modal, toasts, splitters all inline). Break into logical
  pieces (e.g. `static/tree.js`, `static/chat.js`, `static/modals.js`, `static/toast.js`,
  `static/style.css`) served by `gui.py`, keeping `gui.html` as a thin shell.
- [ ] **i18n.** English by default, Russian as a second locale, and adding a new locale
  should be "drop one file in." Proposed: `locales/en.json`, `locales/ru.json` (flat
  key→string maps), a tiny `t(key)` helper in the frontend, language picker in the
  header, `?lang=` query param or localStorage to persist choice.
- [ ] **Diff rendering.** Render unified diffs in the chat thread as a real diff view
  (colored +/- lines, file headers) instead of a plain code block — codex/claude output
  often contains real diffs worth rendering nicely.
- [ ] **Prettier scrollbars.** Custom `::-webkit-scrollbar` styling to match the dark
  theme instead of the OS default.
- [ ] Re-look at the task-list interaction after CliDeck/agent-of-empires comparison
  (see below) — e.g. a compact "running now" summary strip, or a Claude-Code-style
  background-tasks flyout, if it turns out to be clearer than the current tree.
- [ ] **Separate model + effort selectors, for every provider.** Today effort is baked
  into the model alias string (`sonnet-high`, `5.5-high`) — the picker should offer two
  independent dropdowns (model, then effort: auto/low/medium/high/xhigh/max, only the
  levels a given provider actually supports) whenever a provider exposes an effort
  concept, falling back to "auto" (provider/CLI default) when not selected. Gemini has
  no effort concept; **opencode does** (`--variant`, e.g. high/max/minimal) —
  `provider_opencode_run_cmd` already forwards `EFFORT` to `--variant` if set, but
  nothing in the current CLI/GUI args flow ever populates it (no `_resolve` function
  for opencode yet) — this selector work is what will actually make it reachable.
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
- [ ] **Deeper CliDeck / agent-of-empires comparison.** Queued but not yet delivered —
  was mid-flight in a workflow that got stopped before returning results (see git
  history around 2026-07-01). Worth re-running: both are closer analogs than the first
  research pass found, and their actual source/UX may have concrete ideas worth
  adopting, while keeping this tool's zero-dependency stdlib-only + adaptive
  rate-limit-panel angle, which neither of them has.
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
