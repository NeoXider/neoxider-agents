# TODO

Roughly in priority order. PRs welcome.

## Bugs

- [ ] **Model name isn't resolved in the UI.** `agent.sh` records the raw alias/`"default"`
  passed on the command line into `<name>.meta`'s `model=` field *before* resolving it to
  the real model — so the tree/chat header show `codex/default` instead of the actual
  resolved model, e.g. `gpt-5.5 (medium)` or `claude-sonnet-5 (high)`. Fix: record the
  resolved `M`/`CM` (+ effort) after `codex_model_args`/`claude_model_args` run, for both
  `run` and `reply`.

## Provider plugin architecture

Today adding a provider means editing `providers.json` (display) *and* a `case` branch in
`agent.sh` (invocation) *and* (for rate limits) a branch in `gui.py`'s `provider_info()`.
Goal: **one new file, zero edits to existing files.**

Proposed design (not yet implemented):
- `providers/<name>/provider.sh` — sourced by `agent.sh` at startup (`for f in
  providers/*/provider.sh; do source "$f"; done`). Defines a small contract of functions
  per provider: `provider_<name>_args` (alias→model+effort resolution), `provider_<name>_run_cmd`,
  `provider_<name>_resume_cmd`, `provider_<name>_doctor` (prints version/login/limits as JSON).
- `providers/<name>/provider.json` — display metadata (label, models, default_model,
  whether it has rate limits). `gui.py` glob-loads `providers/*/provider.json` instead of
  one big file.
- New subcommand `agent.sh provider-info <engine>` calls `provider_<engine>_doctor` and
  prints JSON. `agent.sh doctor` becomes "loop over discovered providers, call
  `provider-info` for each, pretty-print." `gui.py`'s `/api/provider` just shells out to
  `provider-info` instead of hardcoding per-engine logic in Python.
- This also fixes "doctor and the per-provider limits panel should go through one path
  and be cached together" — see Caching below.
- Migrate the existing 4 providers (codex/claude/opencode/gemini) into this shape.

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

## Research

- [ ] **CLI-as-API for automated testing.** Separate investigation: for CoreAI and other
  libraries, check whether existing open-source solutions let you drive Claude Code /
  Codex CLI etc. as an API from your own app's test suite (rather than through a human
  terminal session), before building anything custom.
- [ ] **Deeper CliDeck / agent-of-empires comparison.** Both are closer analogs than the
  first pass found. Read their actual source/UX for concrete ideas worth adopting (e.g.
  CliDeck's session sidebar interaction), while keeping our zero-dependency
  stdlib-only + adaptive rate-limit-panel angle, which neither of them has.

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

- [ ] Translate `SKILL.md` (currently partly Russian) fully to English — code/doc
  artifacts should be English regardless of chat language.
