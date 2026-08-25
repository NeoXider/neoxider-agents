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
  URL is bookmarkable across restarts instead of drifting between manual invocations. That priority
  is fixed; only the busy-port behaviour is smart (next bullet).
- **Loopback by default; `--lan` needs `--token`** (`parse_argv()`, a pure function so the rule is
  unit-testable without binding a socket). This panel is strictly more dangerous than the API
  bridge: `POST /api/run` launches a real subagent in full-auto mode in any directory the caller
  names, so binding the network without authentication is remote code execution — `parse_argv`
  raises `SystemExit` with an explanation rather than starting. Auth itself is `gui_authorized()`:
  no token configured → tokenless loopback mode with JSON/same-origin POST guards; token configured
  → every client, including loopback and reverse-proxy traffic, must pass a constant-time compare
  against `?token=` / `X-Agent-Token` / the `agent_gui_token` cookie. A successful query-token
  bootstrap sets an HttpOnly, `SameSite=Strict` cookie and immediately redirects to a query-free
  URL. `is_our_panel()` authenticates its own probe when a token is configured.
- **Idempotency vs. a squatted port** (`choose_port()`): one GUI for all providers, `LOGDIR` is shared,
  so a repeated `gui` must not crash or duplicate anything. But "the bind failed" and "my panel is
  already running" are NOT the same thing — the old code assumed they were, and when an unrelated
  WebSocket server happened to hold `8765` it printed "already running", opened a browser tab and the
  tab died with `invalid Connection header`. Now the occupant is *identified* first: `is_our_panel()`
  GETs `/api/tasks` and checks the response's shape (shape, not a version header, so an older running
  panel is still recognized). Outcomes:
  `asked` (free → bind it) · `ours` (reuse, just open the browser) · `moved` (foreign occupant → next
  free port + a `!!!!` banner with the real URL) · `none` (nothing free in +50 → an explicit error,
  never a silent no-op). Parallel workers each write to their own `<name>.meta/.log`, so a shared
  overview is safe (and concurrency-safe at the file level too — `meta_set`'s read-modify-write is
  wrapped in a portable `mkdir`-based lock).
- **Task liveness is NOT computed here anymore** — `gui.py`'s `eff_state()` is a literal mirror of
  `agent.sh`'s. It used to have its own rule ("running + log quiet for 5 min = stalled"), which
  contradicted the CLI's ("pid alive = running") on every long task: a codex step buffers its output
  and flushes the log only when it ends, so an honest 10-minute task read as *stalled* in the panel
  while `agent.sh status` called it *running*. The shared machine is: pid dead → `stalled`; pid alive
  + quiet longer than `AGENT_STALE_SEC` → `idle`, rendered as `running (no output for Nm)` (the exact
  CLI wording, via `stateLabel()` in `static/util.js`); otherwise `running`. `idle` counts as live
  everywhere (`LIVE_STATES`) — `/api/wait` keeps waiting, the SSE log stream stays open, and
  `agent.sh clean` (including `--purge`) skips it exactly like `running` so a live process's
  `.log`/`.meta` cannot be deleted while it is still writing.
  The pid check crosses the git-bash/python boundary through `winpid=` in `.meta` (a git-bash pid is
  meaningless to native python). **Never probe with `os.kill(pid, 0)` on Windows** — there it is not a
  probe, it calls `TerminateProcess` and would kill the very task being asked about; `pid_alive()`
  uses `OpenProcess` + `GetExitCodeProcess` instead.
- **Watchdog kills are surfaced, not hidden**: a task killed by `AGENT_TIMEOUT_SEC` carries
  `timeout=<secs>` in its `.meta`, which the chat header shows as a `⏱ killed after Ns` pill and the
  toast reports instead of a bare exit code.
- **Full-dialog chat view** (`GET /api/dialog?task=<name>[&full=1]`, parsed by `parse_dialog()` /
  `parse_output_blocks()` in `gui.py`, rendered by `static/chat.js`): the chat tab shows the WHOLE
  conversation of a task, Claude-Code-style — every `run`/`reply` step in order with a separator
  line (kind · timestamp · duration), the full prompt of each step, and the step's output as
  ordered blocks. Tool calls render as one compact line (tool name + first line of the argument +
  duration when known) and expand on click to the full arguments + result; **expand all / collapse
  all** buttons act on the whole dialog, and the expansion state lives in a `Set` keyed
  `task:step:block`, NOT in the DOM, so the 3s auto-refresh restores it exactly (losing expansions
  on every poll was the old view's worst failure mode). Thinking/reasoning blocks are parsed into
  the data but hidden by default (a 💭 toggle in the header shows them, off by default) — nothing
  is stripped from the log itself. Durations use one short format (`fmt_dur()` in gui.py, mirrored
  by `fmtDur()` in util.js: `1.2s` / `45s` / `3m 20s` / `1h 5m`); per step they come from the next
  step's header timestamp (or the log's mtime for the final step), per tool call from codex's own
  `Wall time: N seconds` / `succeeded in Nms:` lines; a still-running last step ticks live (1s,
  using the server clock offset from the payload). Where the log has no timestamps, NOTHING is
  shown rather than a made-up number. Engines log differently, so the output parser is layered and
  always degrades to raw text — never an empty pane, never an exception: (1) the current
  `========== [run] ts | … ==========` step format; (2) legacy codex plaintext (`user`/`codex`/
  `thinking`/`exec` marker lines; a `user` block becomes the step prompt when there is no
  `> PROMPT:` section, banner chrome and `tokens used` are dropped); (3) raw structured JSONL
  (codex `exec --json` `item.completed` events, kimi stream-json roles with `tool_calls`, claude
  stream-json `assistant`/`user` envelopes) — possible verbatim in a log when a provider's cleanup
  layer cats the raw stream; (4) anything else → one raw text block. A JSON-looking line inside
  ordinary prose does NOT trigger the JSONL path (it requires recognised event/role types).
  Payload bounds: the default response is the LAST 30 steps (`_DIALOG_STEP_LIMIT`) with any single
  block capped at 20 000 chars (`_DIALOG_BLOCK_CAP`, flagged `truncated`); the header then offers
  **show the whole dialog (N steps)** which refetches with `full=1` (every step, every byte), so
  the full text is always reachable without opening the `.log` by hand. Parsed steps are cached
  server-side keyed by `(name, mtime, size)`, so the auto-refresh re-parses only when the log
  actually changed; the client likewise rebuilds `#chat` only on a real change (mtime/size/state/
  view-mode key), preserving scroll and expansions. `/api/thread` (raw log) and the SSE
  `/api/stream` are untouched — `bridgetab.js` still consumes them.
- **Tree**: nesting by `parent` (see `-P`). A parent task → its subagents indented.
  Projects = tasks ∪ explicitly registered ones via 📂＋ (even with 0 tasks, `projects.json` in `LOGDIR`).
  The active project is on top and expanded; each project has its own scroll (`max-height`), so a large
  project (many tasks) doesn't push the others out of view. Scrollbars are custom-styled to match the
  dark theme (`::-webkit-scrollbar`).
- **Path normalization**: the GUI (native Windows python) and `agent.sh` (git-bash) write the same
  path differently (`C:\Git\X` vs `C:/Git/X` vs `/c/Git/X`). `gui.py`'s `to_git_bash_path()` converts
  ANY path to unix-style (`/c/...`) at the moment it's sent to `-C` / saved to `projects.json` — otherwise
  tasks from the GUI would be grouped separately from tasks in the same folder launched from the CLI.
- **"API" tab (OpenAI bridges)**: start/stop `agent.sh openai-server` bridges without the CLI. This
  is the only non-Tasks tab — the old standalone test-api tab was folded away (that feature lives in
  the CLI); `switchTab` now lives in `bridgetab.js`. Pick provider+model (default opencode/big-pickle,
  +effort/port/dir, localhost-vs-LAN, **API key**) → `POST /api/bridge/start` spawns the bridge in
  the background. The key is passed only through the child environment (`AGENT_OPENAI_KEY`), never
  process argv, and is never written into `bridges/bridge-<port>.json` — only `auth: true/false`.
  LAN startup without a key is refused by the bridge. Restarting an authenticated bridge requires
  re-entering its key so a model switch cannot silently downgrade protection.
  Each bridge self-registers a `bridges/bridge-<port>.json` in `LOGDIR` (see `openai_server.py`'s
  `register_bridge`); `GET /api/bridges` lists them and probes each one's `/health` for live status
  (session active/idle + turns). **Pruning is port-based, not health-based**: a registry file is
  removed only when `port_available()` says nothing is listening — a bound-but-slow bridge (busy
  handling a request, whose `/health` is momentarily unresponsive) is shown as **busy**, never
  deleted. (`/health` itself is lock-free so it stays instant even mid-completion.) Each row browses
  its request logs **inline**: `toggleBridgeLogs` lists the port's `openai-<port>-*` tasks and
  `toggleBridgeReq` expands one call's prompt+output in place. `POST /api/bridge/stop` kills the
  recorded pid (`taskkill /F /T` on Windows) and drops the file. The port is bind-checked before
  launch so a busy/reserved port fails fast. opencode's model list is fetched live via
  `GET /api/models?engine=` (`opencode models` through git-bash, since the npm shim isn't resolvable
  by native-Windows python); other engines fall back to `provider.json`. Frontend:
  `static/bridgetab.js` (ids are `brg-*` to avoid colliding with the folder-browser's `br-*` ids).
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
  keeps the last-known-good data until new data arrives.
- **The doctor modal is cache-first** (stale-while-revalidate): on open it paints the cached text
  instantly via a non-blocking `/api/doctor?cached=1` (which never shells out — it returns `empty=1`
  only when nothing is cached yet), then loads fresh data on top and replaces once it arrives. A small
  header spinner shows while that fresh load is in flight over the cache; the body "running doctor…"
  spinner appears only on the genuine empty-cache first open (before prewarm has populated it).
- **Doctor snapshot update (current behaviour):** `agent.sh doctor --json` probes provider hooks
  concurrently and returns ordered structured data. `GET /api/doctor?cached=1` never shells out;
  it returns the last valid snapshot immediately, including `gui-doctor-cache.json` persisted beside
  the logs. A normal doctor request starts a background stale-while-revalidate refresh. The modal
  shows limit progress/reset cards first, then compact engine state/version/login rows; raw terminal
  output stays available behind a toggle. The explicit `deep check` button is the only path that runs
  `doctor --deep`, because it costs a real model call. Failed refreshes keep the last good snapshot.
- **i18n**: English by default, Russian as a second locale (`locales/en.json`/`ru.json`), switchable
  via the header picker (persisted in localStorage). Adding a locale is a drop-in `locales/<code>.json`
  — any key it doesn't cover falls back to English automatically, so a partial translation still works.
- **Splitters**: the left and right panels can be dragged with the mouse, width is saved to localStorage.
- Launch it via `agent.sh gui` (it sets the git-bash path for python); running `python gui.py` directly
  also works — there's a fallback to typical git-bash paths.
