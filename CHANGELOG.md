# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed

- **A long prompt no longer fails the spawn before the model is ever asked.** Windows caps an entire
  command line at 32767 characters, and every path here handed the prompt to the CLI as an argument,
  so a big conversation died in `CreateProcess` — as `Argument list too long` from bash, or as
  WinError 206 that Python raises as `FileNotFoundError`, which the OpenAI bridge could only report
  as an opaque `500 bridge_failure`. Reproduced live: an 18.5 KB payload worked, a 43 KB one did not,
  and the boundary sits at ~32 KB for a native CLI whether it is spawned from Python or from bash.
  Three parts to the fix. `agent.sh run` and `agent.sh reply` take `--prompt-file PATH`, so a caller
  can hand over text too long to pass as an argument. `claude` and `opencode` — the two CLIs that
  read a prompt from stdin — now receive an over-long prompt that way (from a file, so the headless
  "never block on an interactive stdin" guarantee is kept), with no prompt left in argv. `codex` and
  `gemini` take it only as an argument, so instead of failing opaquely they refuse up front, naming
  the size and pointing at the two engines that can. The threshold is `AGENT_ARGV_PROMPT_MAX`
  (default 16000), well under the platform ceiling to leave room for flags and quoting.
- **The OpenAI bridge stages a long prompt in a file** rather than in argv, using the new
  `--prompt-file`, and removes it afterwards even when the run raises.
- **`wait` no longer calls a live-but-quiet task "settled".** It counted only `running` as still
  going, so a task in the honest `idle` state — alive, but its log silent past `AGENT_STALE_SEC` —
  was reported as settled and `wait` returned while the agent was still mid-turn, handing the
  orchestrator an empty answer. That is the NORMAL case for codex and claude, whose steps flush the
  log only when the step ends, so `wait` was unreliable exactly where it is most useful. Both places
  now treat `idle` like `running`: the settle loop keeps blocking, and whole-wave mode (no names
  given) also picks up idle tasks instead of ignoring them. `clean` already had this right; `wait`
  did not. Regression test added with RED/GREEN evidence.

### Changed

- **A bridge bug is diagnosable again.** The catch-all logged only the exception's type
  (`exception=FileNotFoundError`), which is what made the failure above a dead end — the message
  naming the real cause was thrown away. It now logs the message too, and the traceback at debug
  level.

### Documentation

- **How to handle a turn the PROVIDER killed, as opposed to one the model failed.** The orchestrator
  loop said an `error` task should have "its log read and the task re-scoped", which is wrong advice
  for by far the most common cause: a transport failure (`403 Forbidden`, `tls handshake eof`,
  `stream disconnected before completion`, `os error 10054`) that ends the turn in seconds while the
  task itself was fine. Those sessions are still on disk and `agent.sh reply <name>` resumes them with
  full context, so re-running a fresh `run` with a re-pasted prompt discards work and pays twice.
  A new section documents this, plus the part that actually bites: a failed turn is NOT an atomic
  rollback, so the working tree must be inspected before resuming — observed live, a task had written
  its new failing tests but not the fix they existed to prove, leaving a deliberately red suite with
  nothing recording why. Also records the practical adaptation during an outage: split the work into
  several short `fan`-ed tasks, because short turns survive where one long turn keeps dying.
  The liveness table gains the matching `error` row.

### Added

- **`agent.sh wait <names>` completion primitive.** Blocks until the named tasks (or every
  task currently running) leave `running`, prints each final state and the agent's last answer;
  exit code 2 on timeout. Lets an orchestrator with its own background-job mechanism get a
  completion notification — one background job per watched task.

### Fixed

- **GUI polling error spam.** Consecutive identical error toasts within 60 seconds now share one
  bounded `×N` history record and only the first occurrence creates a visible popup.
- **Windows background launches opened terminal tabs.** GUI run/reply/bridge subprocesses now use
  `CREATE_NO_WINDOW` plus a hidden startup state by default; a visible console is created only when
  the user explicitly enables the terminal checkbox.
- **Windowless long-lived servers on Windows.** `gui` and `openai-server` now prefer a no-console
  interpreter (`pythonw.exe`, overridable via `$AGENT_PYTHON_W`) when one exists, so the server never
  parks in a visible command window; `gui-launcher.bat` starts from a hidden PowerShell host unless you
  set `NEOXIDER_GUI_VISIBLE=1`.

## [0.4.0] - 2026-08-25

### Security

- **GUI request boundary hardened.** A configured token is now required even through loopback
  reverse proxies; tokenless local mutations require JSON and same-origin browser headers. Query
  token bootstrap redirects to a clean URL and uses an HttpOnly cookie. Task identifiers are
  bounded basenames, eliminating log/meta traversal and option injection.
- **API bridge is loopback-first and fail-closed.** The default bind is `127.0.0.1`; every LAN
  bridge requires an API key, while Codex/Gemini and unsafe native OpenCode remain loopback-only
  because their read/tool boundary is incomplete. Claude uses an empty native tool set.
- **Secrets and local state are private.** GUI-started bridge keys travel in the child environment,
  never argv; shell task state is created/repaired with owner-only permissions and HTTP diagnostics
  omit query strings and provider exception details.

### Fixed

- **shell lifecycle and retry correctness.** Validate task names before every file access, replace
  lock stealing with owner-aware recovery and per-writer temporary files, isolate retry attempts,
  stop retries after detectable session/side-effect activity, persist the successful session, and
  inherit the original model/effort on reply.
- **bridge session/protocol correctness.** Persist the exact client-visible assistant turn before
  resume, reject tampered history, invalidate sessions after hidden tool nudges, terminate process
  trees on timeout, validate request schemas as 400s, filter undeclared tools, report failed SSE
  streams without a false success terminator, and make `/health.busy` mean in-flight work.
- **GUI frontend correctness.** Remove inline-handler interpolation, fix whole-dialog selection and
  stale request races, bound bridge transcript caches/polling, handle deleted tasks and corrupt
  local storage, preserve i18n controls, and surface non-2xx/non-JSON responses.
- **cross-platform launchers.** Windows wrappers use Git's complete `bin/bash.exe` runtime and
  propagate native exit codes in cmd, Windows PowerShell 5.1, and PowerShell 7. The POSIX installer
  selects its rc file from `$SHELL`; the runtime now states and enforces Bash 4+.
- **gui bridge log: undefined `parseThread` + unsafe curl quoting.** Removed the stale parser call,
  load request details through `/api/dialog`, and quote copied curl commands safely for POSIX shells.
- **gui HTTP: body/route/port/wait hardening.** Validate request bodies, use exact routes, and
  reject malformed port and wait/timeout values with controlled errors.
- **bridge: fail-closed identity and PID termination.** Require matching live and registered
  instance identities, reject unsafe PID values, and preserve the registry while aborting restart
  whenever health, identity, or termination cannot be verified.
- **shell: shared Python/mtime/wait classifier.** Centralize Python discovery, portable file mtime,
  and closing-window input-request classification. All Python entrypoints use the same
  Windows-aware resolver and fail clearly when no interpreter runs.
- **shell: safe dispatch and retry state.** Validate engines before writing task artifacts, reject
  unknown task names in `reply`, validate retry delay, and replace broken control-byte boundaries
  in the transient-failure classifier.
- **openai-server: session and SSE recovery.** Synthetic tool-call nudges no longer poison later
  resume, and late streaming failures close with `[DONE]` instead of a second HTTP response.
- **openai-server: strict canonical POST routes.** Accept the four documented routes with at most
  one trailing slash; reject prefixes, network-path forms, and extra segments.
- **gui: honest legacy bridge restart.** Omit missing `instance_id` values so unverifiable legacy
  rows reach the existing fail-closed backend path without a misleading identity mismatch.

### Changed

- **distribution contracts refreshed.** Plugin metadata is versioned `0.4.0`, includes Kimi, passes
  strict Claude validation, resolves bundled files through `CLAUDE_PLUGIN_ROOT`, and no longer
  publishes stale hard-coded test totals. `TODO.md` now contains only open work.
- **retry ownership is explicit.** The OpenAI bridge disables the wrapper's inner retry layer, so
  its `--retries` value maps directly to provider call count instead of multiplying two loops.
- **cleanup: dead CSS/locale/parser state.** Remove unused CSS, stale locale keys, parser symbols,
  and duplicated provider helpers left from earlier iterations.
- **logging: safe diagnostics.** HTTP request diagnostics are DEBUG opt-in; default warnings expose
  failure type/status only and never log request bodies, prompts, auth headers, or API keys.

### Removed

- **gui: orphan `POST /api/test-api` endpoint.** Its panel tab was removed in 0.2.0 but the backend
  handler survived; the CLI `agent.sh test-api` command remains the supported path.

## [0.3.0] - 2026-08-13

### Security

- **openai-server: `-e opencode` was an unauthenticated remote shell, and it was the default the
  GUI offered.** The bridge's opencode path talked to `opencode serve` directly, which never goes
  through `providers/opencode/provider.sh` and therefore never saw `AGENT_CHAT_ONLY=1` — while the
  bridge binds `0.0.0.0` out of the box. Measured live (opencode 1.18.16, 2026-08-13): a plain
  `POST /v1/chat/completions` session listed `apply_patch, bash, edit, glob, grep, question, read,
  skill, todowrite, webfetch, websearch, write` **plus 40 live `unityMCP` tools**, and a simple
  "create notes.txt" request really created the file on disk. Two things changed:
  - a chat-only agent profile (`providers/opencode/chat-only.json`, tool map `{"*": false}`,
    applied through `OPENCODE_CONFIG` + `--agent neoxider-chat-only`) now locks the CLI path down —
    verified: the same question now answers `NONE`, and the same "create notes.txt" request writes
    nothing;
  - the native `opencode serve` path is **off by default**, because opencode does not honour an
    agent's tool map over HTTP (the identical profile answers `NONE` via `opencode run --agent` and
    still lists every tool via the server API, whether the agent is set on `POST /api/session` or
    switched with `POST /api/session/{id}/agent`). `AGENT_OPENCODE_NATIVE_UNSAFE=1` opts back into
    the faster path, with a banner, for a loopback-only bridge.
- **openai-server: optional API-key authentication** — `--api-key SECRET` / `$AGENT_OPENAI_KEY`.
  Callers present the standard `Authorization: Bearer SECRET` (what every OpenAI client already
  sends as `api_key`) or `X-Api-Key`. `/health` and `/` stay open for liveness probes and the GUI's
  bridge list; `/v1/models`, `/v1/chat/completions` and `/reset` return an OpenAI-shaped 401
  without it. Comparison is constant-time. With no key the bridge stays open exactly as before —
  fine on loopback — but the LAN banner now says so in as many words instead of implying safety.
  The bridge registry records only *whether* a key is required, never the key.
- **gui: `--lan` + mandatory `--token`.** The panel can now be driven from another computer or a
  phone (`agent.sh gui 8765 --lan --token SECRET`, or `$AGENT_GUI_HOST`/`$AGENT_GUI_TOKEN`), which
  is what you want when the files to edit live on the host. Because it launches full-auto
  subagents in any directory a caller names, `--lan` **refuses to start** without a token — an open
  LAN panel is remote code execution, not a convenience. The token is accepted as `?token=` (stored
  in a `SameSite=Strict` cookie on first load, so no frontend change was needed), an
  `X-Agent-Token` header, or that cookie; loopback clients never need it, which also keeps the
  "is our panel already up?" probe working.
- **gemini chat-only is now read-only** (`--approval-mode plan` instead of `--yolo`). Documented
  honestly as the one incomplete lockdown: Gemini CLI has no flag that removes the READ tools, so a
  gemini-backed bridge should not be exposed to a network.

### Fixed

- **openai-server: opencode ignored the working directory entirely.** `opencode serve` was spawned
  with no cwd and sessions were created with no `location`, so the agent operated in the *bridge
  process's* directory: `-C/--dir` did nothing and the scratch-dir isolation was void — the live
  repro wrote `notes.txt` into this repository's own checkout. Sessions now carry
  `location: {directory: …}`.
- **openai-server: the scratch working directory was wiped on every turn of a no-resume
  conversation.** `opencode`/`gemini` take the fresh-run path on *every* call, and that path
  unconditionally deleted and recreated the session directory — pointless within one conversation,
  and destabilising for a live `opencode serve` holding it as the project root (it reproducibly
  emptied the answer of the tool-result turn in the live smoke). It is now wiped only when the
  conversation actually changes.
- **tests: the live smoke reported phantom failures for `claude` and `opencode`.** Its session
  assertions counted `<task>.meta` files, which the native backends never write; a perfectly
  healthy run showed 3–4 FAILs. Those checks now print as `skip` with the reason (never a silent
  pass), `--no-native` forces both engines onto the agent.sh path to cover them for real, and a new
  backend-independent divergence check (`session_turns` drops back to 1) runs everywhere.
  `--api-key` additionally asserts the 401/open-`/health` split.

### Added

- `tests/`: 34 new offline regression tests (API-key auth incl. prefix rejection and the
  `/health` exemption, the opencode session payload, the GUI LAN/token rules and the
  query/header/cookie token sources) — 432 total, still zero dependencies.
- README rewritten: quick start first, a "working from another computer" decision table, and the
  long-form bridge internals folded into collapsible sections instead of one 500-line wall.

### Changed

- `agent.sh gui` forwards all arguments to `gui.py` verbatim (needed for `--lan`/`--token`), while
  still falling back to `$AGENT_GUI_PORT`/`8765` when given none.

## [0.2.0] - 2026-08-12

- gui/doctor: **limits are now usable at a glance.** `doctor` probes provider hooks concurrently
  and `doctor --json` supplies an ordered structured snapshot to the panel, cutting the measured
  Windows run from 32.637s to 8.885s. The modal is persistent cache-first/stale-while-revalidate:
  `gui-doctor-cache.json` survives a panel restart, refresh runs in the background, and an error never
  replaces last-known-good data. It renders Codex/Claude limits first (progress, percentage, window,
  reset), compact engine state/version/login rows next, and keeps raw output behind a toggle. Claude
  uses its authenticated `/api/oauth/usage` utilization only when an existing OAuth token is valid;
  otherwise it explicitly labels local transcript totals as usage-so-far, never as remaining quota.
  `doctor --deep` is available only from an explicit panel button.

- gui: **the chat tab now shows the whole subagent conversation, Claude-Code-style.** New
  `GET /api/dialog?task=<name>[&full=1]` parses a task's `.log` into ordered steps (every
  `run`/`reply` with its full prompt and output) and ordered blocks (text / tool call /
  thinking). Tool calls render collapsed to one compact line (name + short argument + duration)
  and expand on click, with expand-all/collapse-all buttons; the expansion state is kept in a
  `task:step:block` set so it survives the 3s auto-refresh instead of resetting on every poll.
  Thinking/reasoning blocks stay in the data but are hidden by default (💭 toggle in the header,
  off by default). Durations are visible per step (from step-header timestamps / log mtime, live
  ticking while the last step runs) and per tool call where the log records them (codex `Wall
  time` / `succeeded in Nms`), in one short format (`1.2s`, `45s`, `3m 20s`); no timestamps in
  the log → nothing shown, never an invented number. The parser handles every engine's log
  flavour — current step format, legacy codex plaintext (`user`/`codex`/`thinking`/`exec`
  markers), raw codex/kimi/claude JSONL — and any unrecognised content falls back to raw text:
  never an empty pane, never an exception. Long dialogs are bounded by default (last 30 steps,
  20k chars per block) with a "show the whole dialog" control that fetches everything. Raw
  `/api/thread` and the SSE `/api/stream` are unchanged. New labels added to both
  `locales/en.json` and `locales/ru.json`; 26 new regression tests in `tests/test_gui.py`.

- providers: **normal CLI tasks now use each engine's true unattended mode.** Every task runs with
  stdin closed, so a permission prompt can never be answered; without full auto the task only hangs
  until the watchdog kills it. Codex now defaults to `--sandbox danger-full-access` instead of
  `workspace-write`, with `AGENT_CODEX_SANDBOX` as an opt-down override (for example,
  `workspace-write` or `read-only`). This is the repository owner's explicit standing decision: on
  the owner Windows machine the sandbox helper independently rejected every write with
  `patch rejected: writing is blocked by read-only sandbox; rejected by user approval settings`
  (earlier, `windows sandbox: helper_unknown_error: setup refresh had errors`), leaving codex able to
  describe fixes but unable to apply them. Claude now defaults to `--dangerously-skip-permissions`
  instead of `--permission-mode acceptEdits`, which covered edits but could still prompt for other
  tools; `AGENT_CLAUDE_PERMISSION` overrides the normal-run permission arguments. This brings both
  providers in line with Gemini `--yolo`, opencode `--auto`, and Kimi's auto policy.
  `AGENT_CHAT_ONLY=1` is deliberately non-overridable: Codex remains `--sandbox read-only`, Claude
  remains `--permission-mode acceptEdits` with its tools disabled, and both ignore the new variables.
  That mode exposes an HTTP endpoint which turns arbitrary callers into CLI invocations, so it must
  never inherit write, shell, or permission-bypass access from the environment.

- agent.sh: **`clean` now treats `idle` exactly like `running`.** Even with `--purge`, it skips both
  live states, preventing deletion of `.log`, `.meta`, and generated Markdown files while a quiet
  process is still writing to them.

- codex: **stop every shell command from hanging forever — isolate codex from `~/.codex/config.toml`.**
  Symptom: `agent.sh run -e codex …` started, the model answered, and then every command it issued
  blocked forever; the task stayed `state=running` for hours with a log that never grew (sometimes
  reported as `windows sandbox: helper_unknown_error: setup refresh had errors`). Diagnosed live on
  codex-cli 0.144.0/Windows: the ChatGPT desktop app owns that config file and declares a stdio MCP
  `node_repl` — the "code-mode host" — plus a `notify` hook and HTTP MCP servers that only answer
  while the app runs. Started from a plain shell that host is unreachable and codex's tool router
  blocks on the first exec call (`error=code-mode host closed its stdout`); the identical command
  with `--ignore-user-config` returned in 360 ms. Normal runs AND resumes now pass
  `--ignore-user-config` (previously only chat-only/bridge runs did); auth is unaffected
  (`CODEX_HOME` still supplies it), and a trivial run also got ~2.5x faster (37s -> 15s). Escape
  hatches: `AGENT_CODEX_USER_CONFIG=1` restores the old behaviour, and
  `AGENT_CODEX_MCP="name=url,name2=url2"` re-adds only the MCP servers you actually want as
  `-c mcp_servers.<name>.url=…` (names validated, malformed entries reported on stderr).

- agent.sh: **a step can no longer hang silently — `AGENT_TIMEOUT_SEC` (default 1800s) watchdog.**
  Every `run`/`reply` provider call goes through `_guarded_run`, which enforces a wall-clock
  deadline, appends `!! TIMEOUT: step exceeded AGENT_TIMEOUT_SEC=<N>s and was killed` to the task
  log, and ends the task as `state=error exit=124` with `timeout=<N>` in `.meta` (surfaced by
  `status` as `⏱ killed by the step watchdog after <N>s`, and in the GUI as a pill/toast instead of
  a bare exit code). The kill is a real tree kill: on git-bash the CLI is a native Windows
  grandchild (`bash → npm shim → codex.exe → powershell.exe`) that plain `kill`/coreutils `timeout`
  leaves orphaned, so `_kill_tree` recurses over msys children AND `taskkill /F /T`s the process's
  Windows pid (`/proc/<pid>/winpid`). Verified live: a real codex run killed at 8s left no
  `codex.exe` behind. `AGENT_TIMEOUT_SEC=0` disables the deadline. `openai-server` now passes its
  own `--timeout` down (minus 5s), so a hung bridge call can no longer leave an orphaned CLI
  grandchild appending to the log.

- agent.sh/gui: **one liveness truth — the CLI and the panel cannot contradict each other anymore.**
  `agent.sh status` reported `▶ running (alive, pid N)` for the very same task the web panel showed
  as `stalled`: the CLI looked only at the pid, the GUI only at the log's mtime — and a codex step
  buffers its output, flushing the log only when the step ENDS, so every honest long task looked
  dead in the panel. Both sides now run the same state machine (`eff_state` in `agent.sh`, mirrored
  in `gui.py`): pid dead → `stalled`; pid alive but silent longer than `AGENT_STALE_SEC` (300s) →
  the new honest state `idle`, rendered identically on both sides as `running (no output for Nm)`
  (icon `▷`); otherwise `running`. `idle` counts as live everywhere (`/api/wait`, the SSE log
  stream, the tree spinner). The pid check crosses the git-bash↔python boundary through a new
  `winpid=` field in `.meta`, since a git-bash pid means nothing to native python — and
  `pid_alive()` probes it with `OpenProcess`/`GetExitCodeProcess`, never `os.kill(pid, 0)`, which
  on Windows *terminates* the process instead of probing it.

- doctor: **`agent.sh doctor --deep` — the check that actually catches a broken engine.** Everything
  the old doctor printed was metadata (binary, version, login, limits), none of which notices "the
  model answers but every shell command hangs". `--deep` runs one real, cheap, read-only one-shot
  per engine implementing the new `provider_<engine>_doctor_deep` hook (codex today): a throwaway
  temp dir holding one randomly-named file, and the model is asked to list the directory — the
  random name can only come back if the whole tool-call round trip works. Prints
  `codex shell: ok (…, 10s, real command executed)` or `codex shell: BROKEN — <reason>` plus the
  config-isolation hint, under a hard 60s deadline (`AGENT_CODEX_DOCTOR_TIMEOUT`/`_MODEL`). Plain
  `doctor` stays instant and just points at `--deep`. `agent.sh help` now prints its whole header
  comment instead of a hardcoded line range that silently truncated it as the header grew.

- gui: **`agent.sh gui` no longer pretends to have started on a port someone else owns.** Any bind
  failure used to be read as "the panel is already running", so with an unrelated WebSocket server
  squatting on the default 8765 the command printed success and opened a browser tab that died with
  `invalid Connection header`. `choose_port()` now identifies the occupant first (`is_our_panel()`
  probes `/api/tasks` for its shape, so an older running panel is still recognized) and either
  reuses it, or moves to the next free port and prints the real URL in an unmissable banner. Port
  priority is unchanged: CLI arg > `$AGENT_GUI_PORT` > 8765.

- provider: add Kimi Code CLI as a first-class engine (`-e kimi`) with Kimi K3 as the default,
  verified managed alternatives, clean stream-json output, resumable `ses_*` sessions, doctor/GUI
  metadata, and a no-tools profile for OpenAI-bridge calls. Session-id capture is now provider-
  agnostic. Added provider/parser/resume/model-label tests and refreshed stale default-model tests.

- gui: **make the bridge "logs" button a real toggle that survives the periodic refresh.** The
  request-log panel opened then vanished within a few seconds because the API tab rebuilds its
  whole list on a timer, wiping any expanded panel from the DOM. Open/expanded state now lives in
  module Sets (`BRG_OPEN_LOGS`/`BRG_OPEN_REQS`) and is re-applied after every rebuild, with content
  caches so the restore is flicker-free. Clicking again closes it and it stays closed.

- gui: **fix "clicked start, nothing appeared" + show the LAN address.** A busy default port
  (8801) used to fail silently; now `start_bridge` walks up to the next free port and the toast
  says which port it landed on, and the port field auto-bumps after each launch. The port-free
  check was also wrong on Windows for LAN bridges — it bind-tested 127.0.0.1, which Windows lets
  you bind even while 0.0.0.0 holds the port, so a busy LAN port read as free (two bridges raced
  for the same port and died). It now binds 0.0.0.0 with SO_EXCLUSIVEADDRUSE, a strict "is anything
  on this port" test. LAN bridges now record their reachable **LAN URLs** (`_lan_ips()`), shown per
  row with a copy button so a phone/other PC can reach the endpoint (127.0.0.1 only works locally).
  After launch the list polls a few times so a slower-binding opencode bridge still appears without
  a manual refresh. Also: `model_label` no longer doubles the prefix (`opencode/opencode/big-pickle`
  → `opencode/big-pickle`).

- gui: **fold the two API tabs into one.** Removed the standalone "Test API" tab from the GUI
  (the `agent.sh test-api` feature stays in the CLI) and renamed the bridge tab to just **"API"** —
  one place to serve models, no more confusing near-duplicate tabs. Each running bridge now
  **browses its own request logs inline**: the "logs (N)" button expands the port's requests under
  the row, and clicking one shows that call's full prompt+output in place (no jump to the Tasks
  tab). Default bridge model is now **opencode/big-pickle**.

- gui/openai-server: **fix live bridges vanishing mid-request.** `/health` acquired SESSION_LOCK,
  which a completion holds for its entire CLI call, so a status probe hung and the GUI pruned the
  (alive, busy) bridge from its registry. `/health` now reads session state lock-free (instant even
  mid-completion), and `list_bridges` only prunes a registry file when the port is genuinely free
  (nothing listening) — a bound-but-slow port is shown as **busy**, never deleted.

- gui: **new "LLM API" tab — start/stop OpenAI-compatible bridges from the web panel.** Pick a
  provider + model (+ effort, port, working dir, localhost-vs-LAN) and launch an
  `agent.sh openai-server` in the background; the running-bridges list shows each endpoint's
  live `/health` (session active/idle, turns), a copy-`/v1`-URL button, a ready curl snippet,
  and a stop button. The bridge port is bind-checked first, so a busy/reserved port (e.g.
  Windows WinError 10013) is rejected instantly instead of dying silently. opencode's dynamic
  model catalog (`opencode models`, 27+ ids across every configured backend) is fetched live to
  populate the model datalist; other engines use provider.json. New endpoints: `GET /api/bridges`,
  `GET /api/models?engine=`, `POST /api/bridge/start`, `POST /api/bridge/stop`. EN+RU localized.
  Renamed the old **"API" tab to "Test API"** so it no longer reads as a duplicate of "LLM API"
  (they are different: Test API drives an agent to *check* a local HTTP API; LLM API *serves* a
  model as one). Each running bridge now shows a live **request count** and a **"logs" button**
  that jumps to the Tasks tab and opens that call's full transcript — every bridge request
  (claude/codex/gemini) is already an `openai-<port>-<hex>` task with the whole prompt+answer
  logged; opencode proxies to `opencode serve` and is labelled as such (no per-request task log).

- openai-server: **self-register a `bridges/bridge-<port>.json` in LOGDIR on bind, remove it on
  clean exit** — so the GUI (and any tool) can discover, inspect and stop bridges it didn't
  launch. Records engine/model/effort/dir/pid/port/base_url; the GUI probes `/health` for
  liveness and prunes stale files whose port stopped answering.

- openai-server: accept "parameters"/"params"/"input" as argument-key aliases (Haiku 4.5 live:
  {"function": {"name": ..., "parameters": {...}}} silently produced EMPTY arguments -- every
  call failed schema validation, "9 failed, 0 spawns"). _call_shaped normalizes every accepted
  shape to flat {name, arguments}; _to_calls honors the aliases for the legacy tool_calls
  wrapper too. Also: drop SlashCommand from the chat-only --disallowedTools list (not a valid
  tool name -- the CLI prepended a warning line to every answer). +4 tests.

- openai-server: accept alias wrapper keys around the call array -- {"actions":[...]},
  {"calls":[...]}, {"function_calls":[...]} (Fable 5 live: the Dungeon-win-logic scenario came
  as {"actions":[OpenAI-shaped calls]} and scored tools=0). Stricter than "tool_calls": the
  dict must contain ONLY the wrapper key and every element must be call-shaped (or the list a
  bare-args array). The live emitter streams all wrapper spellings incrementally. +4 tests.

- openai-server: accept a fenced JSON ARRAY of BARE ARGUMENT OBJECTS (Fable 5 live: a 75-object
  G6 castle as ```json [ {"action":"spawn","targetName":...}, ... ] ``` with no function name
  anywhere scored tools=0). Same exactly-one-tool key-fit gate as the bare-object-lines
  spelling; the live emitter also streams plain-array fences OBJECT BY OBJECT (both Opus-style
  call objects and Fable-style bare argument objects), so this spelling now execute-as-streams
  too. +5 tests.

- openai-server: accept the {"action": "<tool>", "arguments": {...}} call spelling (Opus 4.8
  live: G5 Ordered spawn scored tools=0 on a perfectly-shaped 3-call array using "action"
  instead of "name"). Exact-keys rule as for {name, arguments}; normalized in _call_shaped so
  every consumer (fence loop, JSONL, arrays, live emitter) inherits it. +5 tests.

- openai-server: REAL token streaming for the `claude` engine. `stream: true` now forwards
  live deltas while the CLI generates instead of replaying the finished answer: the claude
  provider runs `--output-format stream-json --include-partial-messages` piped through the new
  `stream_text_filter.py` (events -> plain answer text, written incrementally), so the task log
  grows during generation; the bridge tails the log (`_tail_task_log`, byte-offset + incremental
  UTF-8 decode, CR-normalized) and emits each piece as an SSE `delta.content` chunk. Canonical
  fenced `{"tool_calls":[...]}` calls are converted into native `delta.tool_calls` chunks AS
  EACH CALL'S JSON OBJECT CLOSES (`LiveToolCallEmitter` — a 100-call build turn streams its
  calls one by one); non-canonical fences are parsed complete at fence close, and an
  end-of-turn full-parser reconciliation emits anything the incremental scanner missed (late
  but correct, no double emits — matched by name + canonical args). The first ~350 chars are
  held back so a provider limit banner can still surface as HTTP 429 (SSE headers are sent
  lazily on the first real chunk); invisible retries/resume-fallbacks are only permitted while
  nothing has reached the client (`LiveStreamDied` finalizes with what was already sent
  otherwise). `--no-live-stream` reverts to the legacy replay; non-live engines keep it.
  Verified live on haiku: ~0.2 s chunk spacing, per-call tool_calls chunks, clean resume turn.
  +29 tests (emitter, canonical-prefix matcher, JSON object scanner, stream filter, log tail).

- openai-server: prompt rewritten to prescribe ONE canonical tool-call format — a single fenced
  ```json {"tool_calls":[...]} block — instead of offering multiple. Reduces the format sprawl
  that kept surfacing new unparsed spellings, and drops the "write the call text for the
  application to run" framing (which Claude Code's policy layer flagged for some models as
  duplicating tool-use); the new wording asks for "a structured JSON request the application
  carries out", which reads as ordinary structured output.
- openai-server: also accept a JSON ARRAY of call objects in one fence
  (```json [ {"name":...,"arguments":...}, ... ] ```) — Opus 4.8's dominant spelling, which
  scored tools=0 across G1/G3/G4/G6/G7 before this. Consumed only when every element is
  call-shaped, so a plain data array survives as content. +2 tests.

- openai-server: a language-tagged fence that IS the whole answer (little/no prose outside)
  holds real calls, not an example — spark wraps its actual multiline world_command(...) calls
  in ```python. Tagged fences are masked as examples only when there is real prose (>=40 chars)
  outside them. +1 test.

- openai-server: accept the ONE-FENCED-JSON-PER-CALL spelling — each call in its own fenced
  block shaped like an OpenAI tool-call object ({"type":"function","function":{...}}), no
  `tool_calls` wrapper. Observed live from Sonnet 5: every world_command scenario scored
  tools=0 while the transcripts held perfectly-shaped calls (suite 56.3 → 76.1 after the fix).
  Fences are collected in order, the flat {"name":...,"arguments":{...}} shape is accepted too,
  the name must be a KNOWN tool (a plain JSON answer is never eaten), and an explicit
  `tool_calls` block still wins. +5 tests.

- openai-server: a CLI answer that IS the provider's usage-limit banner ("You've hit your
  session limit · resets 7:40am") now surfaces as an OpenAI-style HTTP 429 `rate_limit_error`
  instead of a normal 200 completion — a live benchmark run scored every scenario ~0 as a MODEL
  failure when the account was simply rate-limited. Narrow gate: short text matching the banner
  wording; prose that merely discusses limits is untouched. +4 tests.

- openai-server: recognize the NAMELESS call spelling — the entire message is bare JSON argument
  objects, one per line, with no function name at all. Observed live from gpt-5.3-codex-spark in
  the single-tool G6 scenario ({"action":"spawn","targetName":...} x~400 — the model "saved" the
  redundant tool name; the run scored tools=0/Fail 15.2 with a fully designed castle in the
  transcript). Deterministic gate, deliberately strict: EVERY non-blank, non-fence-marker line
  must parse as a non-empty JSON object AND the keys must fit (subset of schema parameter names)
  exactly ONE tool across all lines — any prose line or ambiguity rejects the whole message, so
  ordinary answers can never trigger it. Echo dedup applies. +6 tests (115 total), including the
  verbatim live-failure lines.

- doctor/GUI: the Claude limits panel now shows a LOCAL USAGE ESTIMATE instead of "no data" —
  tokens burned in the current 5h window and the last 7 days (in+out and cache separately),
  summed ccusage-style from the CLI's own transcript files (~/.claude/projects/**/*.jsonl usage
  blocks). The Claude CLI exposes no remaining-limit API, so this reports what was SPENT, not
  what remains (the plan cap isn't available locally); wired through the existing doctor `note`
  field, so the GUI needed no changes.

- openai-server: 8 parser/robustness defects fixed after an independent adversarial audit (every
  one reproduced live against the real functions before fixing):
  - **String argument ending in a backslash silently killed the whole call** (a JSON-escaped
    Windows path like `"C:\\Games\\"`): the quote scanner's single-char lookbehind couldn't tell
    `\"` from `\\"`, so the string "never closed" and the call was dropped. Now an odd/even
    backslash-run check (`_is_escaped`) in both the call scanner and `_split_top_level`.
  - **A known-tool name inside another call's string argument became a phantom second call**
    (`execute_lua(code="world_command(action=1)")` executed BOTH) — scan cursor now skips
    matches inside an already-accepted call's span.
  - **A malformed `tool_calls` fence swallowed a following valid one** (non-greedy DOTALL match
    ran past its own closing fence) — fence bodies now exclude backticks.
  - **Display-text corruption when stripping multiple fences** with leading whitespace (a
    mid-loop `.strip()` shifted the remaining reversed-match offsets) — strip once at the end.
  - **Format-2 call syntax inside a language-tagged code fence was executed** (a ```lua example
    mentioning `world_command(...)` ran for real) — tagged fences are masked (same-length, so
    spans stay valid) before the func-syntax scan; untagged fences still parse, since models
    often wrap genuine call lines in a bare fence.
  - **```JSON (uppercase tag) fences were not recognized** — fence regex is case-insensitive.
  - **Bare unfenced `{"tool_calls":...}` followed by trailing prose was lost** (`endswith("}")`
    gate) — `raw_decode` now takes the JSON object and keeps the prose as display text.
  - **Resume-after-timeout hole**: a session whose meta state was still `running` (wrapper
    killed by timeout, orphaned CLI grandchild possibly still appending to the log) counted as
    "healthy" and could be resumed, misattributing orphan output — healthy now means positively
    finished (`done`/`waiting`).
  - Dedup false-positive contract made explicit: a Format-2 line identical to an executed call
    is ALWAYS summary prose; the prompt now documents Format 1 (fenced JSON, dedup-exempt) as
    the deliberate-repeat escape hatch. `--retries` is clamped non-negative. +13 tests
    (109 total in test_openai_server.py) covering every defect above plus pins for
    multiline/two-per-line calls that already worked.

- openai-server stability (toward "almost a real OpenAI API" for benchmark use):
  - **Retry-on-empty**: a completion whose CLI invocation came back empty or in an `error` meta
    state is re-run (`--retries`, default 1, 2s backoff) before the bridge gives up — a real
    OpenAI endpoint effectively never returns an empty 200, and one transient CLI hiccup
    (rate-limit blip, session startup race) should not zero a whole benchmark scenario. A resume
    that "succeeds" but produces an EMPTY answer now also falls back to a fresh run instead of
    returning `""`.
  - **OpenAI-style error surface**: an unexpected exception inside the bridge now returns
    `{"error": {"message": ..., "type": "server_error"}}` with HTTP 500 instead of a bare
    connection reset the client can't distinguish from a network failure.
  - **Estimated `usage`**: responses now carry ~4-chars/token estimates (flagged
    `"neoxider_estimated": true`) instead of hardcoded `0/0/0` — useful for cost panels, not
    billing-grade.
  - **Anti-echo prompt**: `TOOLCALL_INSTRUCTIONS` now explicitly tells the model not to restate
    already-executed calls in call syntax after a tool result — the prompt-side half of the echo
    defense (the parser-side dedup landed earlier).
  - `GET /health` additionally reports `timeout_seconds`/`retries`. +9 tests (96 total in
    test_openai_server.py): retry/fallback behavior (H._run exercised unbound with fakes),
    usage estimates, anti-echo prompt regression guard.

- openai-server: the Format-2 call parser now accepts a single positional JSON object argument —
  `world_command({"action":"spawn","targetName":"Enemy1"})` — in addition to `name=value` pairs.
  This is gpt-5.5's DOMINANT spelling (literally how an OpenAI SDK call is written); before this,
  every such line was silently dropped as prose, which zeroed whole CoreAI benchmark groups
  (a live gpt-5.5 run scored G5 50/100 with 4 scenarios at `tools=0` whose transcripts contained
  perfectly good calls — replaying the fixed parser over those exact captured outputs recovers
  10/10 previously-dropped call-shaped messages, 0 still dropped). A single positional SCALAR now
  also maps onto the function's sole parameter when the OpenAI `tools` JSON schema says it has
  exactly one (`execute_lua("print(1)")` → `{"code":"print(1)"}`); a scalar that looks like a
  failed `{...}` parse is deliberately NOT wrapped that way (would double-wrap into nonsense).
  New `tool_param_names()` helper; `extract_tool_calls()` takes an optional `tools=` argument
  (old call sites without it keep working). `TOOLCALL_INSTRUCTIONS` documents the accepted
  spelling. +12 parser tests (79 total), including verbatim shapes from the live failing run.

- Security/correctness hardening (the bridge is now genuinely a chat-only completion endpoint,
  not just "a CLI told not to use its other tools"): every subprocess the bridge launches now gets
  `AGENT_CHAT_ONLY=1` in its env, which `providers/{codex,claude}/provider.sh` react to with REAL
  CLI-level restrictions rather than a prompt-only ask. codex runs with `--sandbox read-only
  --ignore-user-config` (blocks file writes/shell execution; skips `~/.codex/config.toml`, where
  this machine's real `[mcp_servers.*]` are defined). claude runs with `--strict-mcp-config` (zero
  MCP servers) plus `--disallowedTools Bash,Edit,Write,NotebookEdit,Task,WebFetch,WebSearch`.
  Motivation: a CLI subagent has its own real, separately-configured tool access (this machine has
  a live `unityMCP` registered for both codex and claude) — without this, a model could reach for
  the REAL tool instead of answering in the format the calling application expects, silently
  mutating live state the calling application never asked it to touch. Verified live:
  `-c mcp_servers={}` on the codex command line did NOT actually stop a real `unityMCP.manage_tools`
  call from succeeding against a live Unity Editor (list_groups came back with real data) --
  `--ignore-user-config` does (list_mcp_resources came back empty, and the model correctly reported
  no MCP tools available). Asking the hardened bridge directly to "use unityMCP" got a correct
  refusal from both engines, no side effects, no hang. A normal `agent.sh run`/`reply` OUTSIDE the
  bridge (AGENT_CHAT_ONLY unset) is completely unaffected and keeps full file/shell/MCP access,
  confirmed live (wrote a real file successfully). `openai_server.py`'s prompt was also rewritten:
  a new `BASE_INSTRUCTIONS` block (prepended to every prompt, tools or not) states plainly that the
  session has no MCP/skills/tools, and `TOOLCALL_INSTRUCTIONS` was reframed away from "You are
  acting as X, NOT an autonomous agent" -- that identity-override phrasing was OBSERVED LIVE to get
  refused by Claude Code as a prompt-injection attempt; the new framing ("the external application
  wants you to draft a call for its own downstream execution") does not. opencode/gemini have no
  equivalent restriction yet -- their CLIs weren't verified to have an analogous flag, documented
  as a known gap rather than guessed at. Tests: new `ChatOnlyEnvTests` (python) +
  "AGENT_CHAT_ONLY sandboxing" section (bash).
- Fix (tool-calling recognition — the big benchmark win): the bridge's tool-call reparser now
  ALSO recognizes literal `name(arg=value, ...)` call syntax, not only a JSON
  `{"tool_calls":[...]}` block. Root cause surfaced by running CoreAI's Game-Creation Benchmark
  through the bridge on `gpt-5.3-codex-spark`: the model actually SOLVED scenarios (e.g. a full
  50+ object castle in G6, `world_command`/`execute_lua` calls in G1–G5) but wrote the calls the
  way a CLI agent naturally would — `world_command(action="spawn", targetName="Tower_NW", x=-6,
  ...)` as plain text — instead of the prompted JSON, so the JSON-only reparser saw zero tool
  calls and scored those scenarios 0%. `extract_func_calls` parses every `name(...)` whose name
  is a known tool (parenthesis-balanced, quote-aware, top-level arg split so a value containing
  commas/braces like `"{10,20,30}"` stays intact; values typed via JSON→literal→bare-string), and
  `extract_tool_calls(text, names)` falls back to it after the JSON paths. Engine-agnostic (helps
  claude/opencode/gemini too). Verified live: a multi-object `world_command` build returned 3
  correct `tool_calls`. Covered by new `FuncCallSyntaxTests`.
- Stronger tool-calling prompt: it now states the model has no shell/filesystem and the ONLY way
  to act is to emit a call; that *describing* an action in prose ("I called X", "Execution
  succeeded") is IGNORED and treated as a failed turn; that it may emit as MANY calls as the task
  needs; and it advertises BOTH accepted formats (JSON block or `name(arg=value)` lines). Observed
  live steering codex to emit clean JSON on its own.
- Note on speed (asked about while benchmarking): each bridge turn is a full cold `codex`/`claude`
  CLI process (no warm daemon exists to reuse), so per-turn latency (~15–50s) is inference +
  process start, not something the bridge can cache away — measured `-c mcp_servers={}` made no
  difference. The existing session model already resumes (sends only the new tail) within one
  conversation; across the benchmark's many independent scenarios a fresh session per scenario is
  correct, not waste. The real throughput win here is fewer FAILED scenarios (hence fewer retries)
  from the tool-call fix above.
- Hardening (from a second independent codex audit of the fixes below):
  - The codex provider resolves a working `python3`/`python`/`py` (with a real smoke test, so a
    Windows WindowsApps alias stub is skipped; override via `AGENT_PYTHON`) and, if none works,
    degrades to a raw `cat` passthrough instead of hard-failing the whole codex run.
  - `_provider_codex_emit` now exits non-zero when there is no `agent_message` (auth/rate-limit/
    schema drift), so the `PIPESTATUS[1]` guard marks the task failed instead of "done with junk";
    and it neutralizes the pathological case where the answer itself contains a line exactly equal
    to `---------- output ----------` (a trailing space stops `last_output` truncating there).
  - `reply_agent` (bridge) additionally rejects a reply whose task did not end in a good state
    (`done`/`waiting`): agent.sh writes the reply header before dispatch, so a provider that fails
    AFTER the header grows the log — the earlier length-only guard would have returned that failed
    block as the answer. Now `_run` falls back to a fresh run.
  - `openai_server.last_output` is line-anchored (whole-line marker match) like agent.sh's awk,
    instead of a substring `rfind` that could truncate an answer containing the marker text
    mid-sentence.
  - Verified live after these changes against `gpt-5.3-codex-spark`: a 6-turn growing-history
    chain stayed ONE underlying session (1 `[run]` + 5 `[reply]` blocks) and recalled facts from
    turns 1–3 at turn 6; fresh/continuation/tool-calling all still clean. Tests: 84 python + 58
    bash.
- Fix (codex clean output): the `codex` provider now runs `codex exec --json` and extracts
  ONLY the final agent message (`_provider_codex_emit` in `providers/codex/provider.sh`),
  instead of letting codex's plaintext `exec` dump its startup banner / session id / ERROR-log
  lines / "tokens used" footer / cp866-mojibake OS-notification line into the same stream as the
  answer. This supersedes the earlier "documented, not fixed" codex-chrome and mojibake notes
  below: `agent.sh last`, the GUI chat view, and the openai-server bridge's `content` now all
  get a clean codex answer. The parser re-emits a `session id: <uuid>` line (so agent.sh's
  resume grep keeps working) plus a synthetic `---------- output ----------` marker so
  `last_output` slices to just the answer; on any failure with no agent message it passes the
  raw stream through so auth/rate-limit errors stay visible. Verified live end-to-end against
  `gpt-5.3-codex-spark`: fresh completion, multi-turn context recall via resume, and a
  divergent unrelated turn (new session) all returned clean one-word answers. Covered by new
  unit tests (`_provider_codex_emit` cases in `tests/test_agent_sh.sh`).
- Fix (bridge stale-answer guard): `reply_agent` in `openai_server.py` returned
  `last_output(read_log(name))` unconditionally, so if a resume appended nothing to the log
  (e.g. `agent.sh reply` died before writing a block because no session id resolved) the bridge
  silently echoed the PREVIOUS answer as if it were the new one. It now detects that the log
  didn't grow, returns `None`, and `_run` falls back to a fresh run with the full history.
  Covered by new `ReplyAgentStaleGuardTests` in `tests/test_openai_server.py`.
- Fix (codex provider exit code): `provider_codex_run_cmd`/`provider_codex_resume_cmd` now
  surface a `_provider_codex_emit` (Python parser) failure via `PIPESTATUS[1]` instead of
  masking it behind codex's own exit code, so a missing/crashed Python interpreter can't mark a
  task "done" with empty output.
- Known trade-off (accepted, systemic): the answer boundary is still the exact line
  `---------- output ----------`; a provider answer that literally contains that line on its own
  would be truncated by `last_output`. This has always been true for every provider (agent.sh
  writes one such marker per turn) — the codex `--json` path adds a second synthetic one but no
  new failure mode, since the trigger is the same never-observed literal string.
- Fix: `codex` sessions resumed via `agent.sh reply` silently drifted to a different
  model/effort than they started with (a `-m spark` session came back as `gpt-5.5` on
  resume) — `codex exec resume` does support `-m`/`-c model_reasoning_effort=`
  (confirmed via `--help`), it just wasn't being forwarded.
  `provider_codex_resume_cmd` now passes both, matching `claude`'s existing resume
  behavior (`PROVIDER_CODEX_RESUME_NEEDS_MODEL=1`). Verified live: the resumed
  session's own banner correctly reports the original model/effort after the fix.
  Caveat: a bare `reply` with no explicit `-m` still resolves to the provider's
  default rather than the task's original model — this was already true for
  `claude` and is now consistent for `codex` too.
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
  **(Superseded above: the codex provider now uses `codex exec --json` and returns a clean
  answer for all three surfaces.)**
- Root-caused an occasional garbled/mojibake line in `codex`'s raw output (e.g.
  `ᯥ譮: ,  䨪஬ ...`): it's a Windows OS notification ("process N terminated") printed
  in the console's cp866 codepage, mis-decoded as UTF-8 by the `utf-8`-assuming
  subprocess capture shared with `agent.sh`/`gui.py`. Documented, not fixed (project-
  wide capture behavior, out of scope for this bridge alone).
  **(Superseded above for the openai-server/`agent.sh last` codex path: the `--json` parser
  reads with `errors="ignore"` and keeps only the structured final message, so the mojibake
  line no longer reaches the answer.)**
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
  silently run at a different reasoning effort than the one it started with.
  **(Superseded above: `provider_codex_resume_cmd` now forwards `-m`/`-c
  model_reasoning_effort=`, `PROVIDER_CODEX_RESUME_NEEDS_MODEL=1`.)**

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
