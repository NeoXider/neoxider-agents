# GUI internals (`agent.sh gui`)

Design notes and implementation details for the web control panel. The short operating
summary lives in [SKILL.md](../SKILL.md); this file is for anyone changing `gui.py` /
`gui.html` / `static/*`.

A lightweight local web control panel (python-stdlib, zero dependencies): a
project→subagents tree on the left, a chat with the agent (markdown + bubbles) in the center,
launching a new task and the selected provider's limits on the right, a `doctor` button with all
the limits. Status is conveyed by the activity/topic emoji (✅⏳❌⚠️📖✏️🔧💭🐛🧪…) plus
strikethrough for finished tasks — no separate colored dot (that was a redundant third encoding
of the same signal, removed).

Files: `gui.py` (backend) + `gui.html` (thin shell) + `static/*.js`/`static/style.css` (modular
frontend, one file per concern — tree/chat/modals/toasts/splitters/i18n/app) + `locales/*.json`.

- **Stable port**: resolved as explicit CLI arg > `$AGENT_GUI_PORT` env var > `8765` default, so the
  URL is bookmarkable across restarts instead of drifting between manual invocations.
- **Idempotency**: one GUI for all providers, `LOGDIR` is shared. If a server is already up on the port —
  a repeated `gui` doesn't crash or duplicate it, it just opens the browser. Parallel workers each write
  to their own `<name>.meta/.log`, so a shared overview is safe (and concurrency-safe at the file
  level too — `meta_set`'s read-modify-write is wrapped in a portable `mkdir`-based lock).
- **Tree**: nesting by `parent` (see `-P`). A parent task → its subagents indented.
  Projects = tasks ∪ explicitly registered ones via 📂＋ (even with 0 tasks, `projects.json` in `LOGDIR`).
  The active project is on top and expanded; each project has its own scroll (`max-height`), so a large
  project (many tasks) doesn't push the others out of view. Scrollbars are custom-styled to match the
  dark theme (`::-webkit-scrollbar`).
- **Path normalization**: the GUI (native Windows python) and `agent.sh` (git-bash) write the same
  path differently (`C:\Git\X` vs `C:/Git/X` vs `/c/Git/X`). `gui.py`'s `to_git_bash_path()` converts
  ANY path to unix-style (`/c/...`) at the moment it's sent to `-C` / saved to `projects.json` — otherwise
  tasks from the GUI would be grouped separately from tasks in the same folder launched from the CLI.
- **"LLM API" tab (OpenAI bridges)**: start/stop `agent.sh openai-server` bridges without the CLI.
  Pick provider+model (+effort/port/dir, localhost-vs-LAN) → `POST /api/bridge/start` spawns the
  bridge in the background. Each bridge self-registers a `bridges/bridge-<port>.json` in `LOGDIR`
  (see `openai_server.py`'s `register_bridge`); `GET /api/bridges` lists them and probes each one's
  `/health` for live status (session active/idle + turns), pruning files whose port stopped
  answering. `POST /api/bridge/stop` kills the recorded pid (`taskkill /F /T` on Windows) and drops
  the file. The port is bind-checked (`port_available`) before launch so a busy/reserved port fails
  fast with a clear message. opencode's model list is fetched live via `GET /api/models?engine=`
  (`opencode models` through git-bash, since the npm shim isn't resolvable by native-Windows python);
  other engines fall back to `provider.json`. Frontend: `static/bridgetab.js` (ids are `brg-*` to
  avoid colliding with the folder-browser's `br-*` ids).
- **Providers are plugins**: each CLI lives entirely under `providers/<name>/` — `provider.json`
  (label/models/**efforts**/limits/default_model/default_effort) drives the GUI's dropdown, model
  list, **separate effort dropdown**, and adaptive limits panel; `provider.sh` defines
  `provider_<name>_resolve`/`_run_cmd`/`_resume_cmd`/`_doctor`. `agent.sh` auto-sources every
  `providers/*/provider.sh` and dispatches generically — adding a new CLI is one new directory,
  zero edits to `agent.sh`/`gui.py`/`gui.html`.
- **Model + effort are separate inputs** (`-f <effort>` on the CLI, its own dropdown in the GUI) —
  not baked into the model alias string anymore. `-f` overrides whatever a provider's own
  `_resolve` would have derived from an alias suffix, and is the *only* way to set effort for
  providers with no `_resolve` at all (opencode's `--variant`, still empty for gemini which has no
  effort concept). Backward compatible: an alias like `sonnet-high` still works if you don't pass `-f`.
- **Terminal**: an "open in terminal" checkbox (off by default) — when enabled, the GUI spawns `agent.sh`
  in a separate console window (`CREATE_NEW_CONSOLE`) where you can see live output; without the checkbox — as usual, silently.
- **Toasts + history**: any completion/error/agent question — a popup notification for 3s, plus
  history via 🔔 (persisted in localStorage). A universal CSS spinner (`.spinner`) — for doctor,
  the limits panel, and the run/reply buttons while a request is in flight.
- **Doctor/limits are cached** (30s TTL) server-side, so switching providers or an idle poll doesn't
  re-shell-out to `agent.sh` every few seconds; a ⟳ refresh button next to the limits panel and inside
  the doctor modal forces a fresh fetch (`?force=1`). The panel never blanks while refreshing — it
  keeps the last-known-good data with a small spinner until new data arrives.
- **i18n**: English by default, Russian as a second locale (`locales/en.json`/`ru.json`), switchable
  via the header picker (persisted in localStorage). Adding a locale is a drop-in `locales/<code>.json`
  — any key it doesn't cover falls back to English automatically, so a partial translation still works.
- **Splitters**: the left and right panels can be dragged with the mouse, width is saved to localStorage.
- Launch it via `agent.sh gui` (it sets the git-bash path for python); running `python gui.py` directly
  also works — there's a fallback to typical git-bash paths.
