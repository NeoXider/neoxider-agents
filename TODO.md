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
  concept, falling back to "auto" (provider/CLI default) when not selected or not
  applicable (e.g. opencode/gemini today have no effort concept at all).
- [ ] **Audit full-auto/non-interactive flags for every provider, document in README.**
  The whole point of this tool is zero prompts/confirmations blocking a subagent run.
  codex (`--sandbox workspace-write --skip-git-repo-check`) and claude
  (`--permission-mode acceptEdits`) are already wired for this. Double-check opencode
  and gemini have an equivalent "don't ask for approval" flag and that it's actually
  passed (they may currently rely on CLI defaults that could still prompt in some
  configs) — then add a clear README section stating explicitly that every provider is
  expected to run fully unattended (auto/full mode), so a user setting up a new CLI
  knows what flag to add if it isn't there yet.

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
