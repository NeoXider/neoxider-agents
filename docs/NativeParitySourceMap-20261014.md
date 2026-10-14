# Native parity source map — 2026-10-14 (factual only)

**Historical pre-integration snapshot.** The original observations and line anchors
below describe the initial baseline, not the current integrated worktree. They are
retained as history. SDK, receipts/events, reviewed recovery, runtime profiles and
diagnostics have since been connected; use [Core integration](CORE_INTEGRATION.md)
and the [draft final report](NATIVE_PARITY_REPORT.md) for current behavior.
Public ordinary-provider enforcement remains false; Windows changes are still
under review and the independently audited outside-AAP escape baseline was rejected.

Base `origin/main ab3dd6c`. Scope: what the code **already implements** vs what Phase 1–8 of `PLAN.md`
still needs. No architecture decisions, no OS-security design, no implementation.
All anchors are `file:line` in this worktree. Nothing outside this file was modified.

---

## 0. Entry points and contracts that exist today

| Concern | Real integration point | Evidence |
|---|---|---|
| Process entry | `agent.py` → `cli.main` → `cli.dispatch` | `cli.py:338`, `cli.py:198` |
| Option resolution | `config.launch_options` (flags → env → config), keys `engine/model/effort` only | `config.py:47`, `config.py:6` |
| Task identity | `cli.task_name` (slug+4hex), `state.Store.resolve` (name or session id) | `cli.py:171`, `state.py:326` |
| State store | `state.Store` (flat `key=value` `.meta`, `atomic_write`, `Lock`) | `state.py:285`, `state.py:26`, `state.py:65` |
| Provider plugins | `get_provider` loads `providers/<engine>/provider.py`; `BaseProvider.command/resolve/supports_resume` | `providers.py:291`, `providers.py:256` |
| Owner process | `runtime.Turn` runs the provider tree in the wrapper | `runtime.py:56`, `runtime.py:160` |
| GUI | zero-dep `ThreadingHTTPServer`; `gui.html` is a shell, behavior is `gui.py` + `static/*.js` | `gui.py:28`, `gui.html:1` |

There is **no in-process Python SDK**: `neoxider_agents/__init__.py` exports only a docstring and
`__version__`; `services.py:6` only *spawns* `gui.py` / `openai_server.py`. Every lifecycle function
returns an `int` and prints human text.

---

## 1. Simple 5-operation SDK — reuse vs gap

**Reuse (already implemented, CLI-shaped):** spawn `lifecycle.run` → `preflight` → `execute`
(`lifecycle.py:202`, `:96`, `:237`); send `lifecycle.send` (`lifecycle.py:149`); status
`views.status` / `lifecycle.effective` / `ux_tracking.dashboard_data` (`views.py:46`,
`lifecycle.py:50`, `ux_tracking.py:223`); stop `lifecycle.stop` → `reporting.record_stop`
(`lifecycle.py:112`, `reporting.py:510`); resume `lifecycle.restart`, `--fresh` replays stored
original options (`lifecycle.py:398`).

Structured output exists in exactly one place: `ux_tracking.collect_result` (`ux_tracking.py:411`),
printed by `result` (`ux_tracking.py:453`) with `--json`. It is a dict, not a return value.

**Gaps:** no typed handle/result objects, no capability descriptor, no call-without-argv path
(`cli.parse`/`text_prompt`/`integer` assume argv, `cli.py:54-168`), no silent-substitution guard
beyond `config.launch_options` preferring an explicit engine (`config.py:53`); `provider.resolve`
rewrites model *aliases* silently (`providers/opencode/provider.py:22`).
Tests pinning today's behavior: `tests/test_core_contracts.py:126`,
`tests/test_agent_control.py:385`, `tests/test_native_entries.py:65`.

---

## 2. Persistent message IDs, receipts, events

**Reuse:** durable ID = inbox `sequence`, filename `%012d.msg`, monotonic via a `sequence` file
(`state.py:365-375`); atomic ordering + drain under the `.inbox` lock in `state.batch_locked`
(`state.py:377`), `runtime.Turn` (`runtime.py:151`), `lifecycle.execute` (`lifecycle.py:352`);
undelivered-on-failure durability — delivered files unlink **only** when `code == 0`
(`lifecycle.py:353-355`), leftovers stay for `send --flush` (`lifecycle.py:161`, `:197`);
control history `ux_tracking.record_history` → `.history.jsonl` (`ux_tracking.py:26`), read back in
`collect_result` (`ux_tracking.py:433-440`).

**Gaps:**
- A "receipt" today is one printed line, `"queued (#N) task=..."` (`lifecycle.py:178`) — no stored
  state object.
- `record_history`'s whitelist is `by/reason/exit/state/sequence/queued/now/fresh/session`
  (`ux_tracking.py:28`): no accepted/delivered/acknowledged field, no provider-ack vs
  semantic-application split.
- `.msg.inflight` is matched by the GUI (`gui.py:396`) but **written nowhere** in
  `neoxider_agents/` — claimed-in-flight durability is a GUI-only assumption, not a protocol.
- `sequence` restarts after `views.clean` removes the inbox (`views.py:255`, `:272`); no durable
  receipt survives cleanup.
Tests: `tests/test_agent_control.py:135,148,187,408,439`.

---

## 3. Parent delivery and GUI event subscription

**Reuse:**
- Stop/interrupt transport works across processes: `.control` JSON + `process.signal_task`
  (`process.py:300`) / `windows.NamedEvent` (`windows.py:233`, `:250`), generation-checked at
  `runtime.py:157`, `runtime.py:184`, `runtime.retry_wait` (`runtime.py:45`).
- `views.wait` (`views.py:132`, `:173`) is the only wait-any implementation: a **poll loop**
  (`views.py:203`) printing `settled` lines plus a `WAIT_DONE` summary (`views.py:217`).
- GUI SSE `/api/stream` (`gui.py:1923`, `:1953`) tails one task's `.log` with bare `data:` lines and
  `event: done`, closing after 60 s of silence (`gui.py:1966`).

**Gaps:**
- No event bus, no event ids, no `Last-Event-ID`, no replay, no subscription registry. The SSE stream
  is a **log tail, not an event stream** and loses state across a GUI restart.
- No callback adapter and no parent-process subscription API; `wait` returns an exit code and text.
- No GUI delivery of terminal events to a live parent; the browser re-polls `/api/tasks`
  (`gui.py:1848`, `:401`).
- **There is no hosted-chat/callback integration in this codebase** — `static/*.js` and the
  `openai_server.py` bridge are separate surfaces; neither is an event-callback host.

---

## 4. Recovery, generation, history

**Reuse:**
- `generation` = per-launch uuid in meta (`lifecycle.py:186`, `:214`, `:241`); stale control files are
  ignored (`runtime.py:184`) — a solid "dead launcher cannot act" primitive.
- Preserved artifacts: `.original.prompt` + `original_engine/model/effort/dir`
  (`lifecycle.py:228-231`), `previous_session` (`lifecycle.py:412`), `.baseline.json`
  (`reporting.begin_snapshot` `reporting.py:282`), `.stop` (`reporting.py:526`), `.history.jsonl`.
- Orphan reconciliation: `lifecycle.owner_alive` (`lifecycle.py:45`) → `effective()` →
  `orphaned`/`stalled` (`lifecycle.py:50`); session re-derived from the log tail when meta lacks it
  (`state.Store.session` `state.py:336`).

**Gaps:**
- `lifecycle.restart` (`lifecycle.py:398`) does **no** pre-resume tree inspection in Python: it calls
  `stop()` then `send()` with the prompt constant `DEFAULT_RESTART` (`lifecycle.py:15`, `:417-418`).
  Any `git status/diff` review is model-side prompt text, not an enforced step.
- No recovery manifest and no "unknown external effect" record; `.history.jsonl` cannot express it
  (`ux_tracking.py:28`).
- History entries carry no generation and no changed paths, so post-crash ordering cannot be
  reconstructed from history alone.
Tests: `tests/test_agent_control.py:331,340,469,575`, `tests/test_ux_tracking.py:454,490`.

---

## 5. Automatic private provider profiles / runtime isolation

**Reuse:**
- Codex isolated from user config: `--ignore-user-config` (`providers/codex/provider.py:42`) plus
  `-c sandbox_mode=...` (`:40`), MCP re-added via `AGENT_CODEX_MCP`.
- OpenCode bundled private profile **only in chat-only/bridge mode**:
  `providers/opencode/chat-only.json` via `OPENCODE_CONFIG` (`providers/opencode/provider.py:35-39`),
  plus `OPENCODE_CONFIG_CONTENT` for `small_model` (`:43-45`).
- Wrapper identity already pinned: `pid_stamp`/`pid_alive` (`process.py:40`, `:67`;
  `windows.py:96`, `:106`) and a kill-on-close Job (`windows.Job` `windows.py:116`,
  `ensure_lifetime_job` `windows.py:141`).

**Gaps (Phase 6):**
- No per-task provider state/config/cache ownership: the child env is a straight inheritance,
  `env = dict(os.environ, ...)` (`runtime.py:135`). No `HOME`/`XDG_*`/`CODEX_HOME`/`OPENCODE_*`
  redirection anywhere in `neoxider_agents/`.
- Providers still read **global** user state: `~/.claude/projects/**.jsonl` and
  `~/.claude/.credentials.json` (`providers/claude/provider.py:63`, `:110`); `$CODEX_HOME|~/.codex`
  thread locks and session index (`providers/codex/provider.py:63`, `:132`).
- Normal (non-bridge) runs get **no** private provider profile: the OpenCode profile is gated on
  `chat_only` (`providers/opencode/provider.py:34`) and `config.py` has no profile/cache keys
  (`config.py:6`). No credential-safe inheritance policy; no pinned runtime beyond
  `providers.executable` (`providers.py:66`).

**Compatibility risk:** redirecting HOME/`CODEX_HOME` breaks session discovery and resume
(`providers/claude/provider.py:63`, `providers/codex/provider.py:132`), hence `supports_resume`
(`providers.py:258`) and the resume validation in `lifecycle.preflight` (`lifecycle.py:104-108`).

---

## 6. Actual scope-enforcement gap (Phase 5)

**What `--owns` really is today: pre-flight glob-overlap advice + post-hoc report filtering.**
- `ux_tracking.guard_ownership` (`ux_tracking.py:136`) compares the new task's globs against other
  *running* tasks' globs and warns or, with `--strict-owns`, raises (`ux_tracking.py:155`). It never
  inspects what a task actually wrote.
- `reporting._ownership_patterns` (`reporting.py:62`), `snapshot(..., owns=)` (`reporting.py:89`,
  `:126-133`) and `owned_paths` (`reporting.py:390`) only **filter which changed paths are reported**
  (`reporting.changed_files` `reporting.py:406`).
- Attribution is explicitly unprovable: `ATTRIBUTION` (`ux_tracking.py:14`) and
  `ux_tracking.overlaps` (`ux_tracking.py:304`) report only *possible* shared writers.
- Flags declared at `cli.py:9`, re-checked in `lifecycle.select_options` (`lifecycle.py:87-89`).

**This is not an OS sandbox and must not be described as one.** The wrapper deliberately runs
providers fully auto: codex default `danger-full-access` (`providers/codex/provider.py:39`), claude
`--dangerously-skip-permissions`, gemini `--yolo` (`providers/gemini/provider.py:19`), opencode
`--auto` (`providers/opencode/provider.py:26`), kimi auto policy. `windows.Job` (`windows.py:116`)
exists for kill-on-close lifetime, not write scoping.

**Gaps:** no enforcement mechanism, no capability declaration, no fail-closed mode; no escape tests for
child processes, path traversal, symlink/junction, hardlink or rename. Symlink handling is hashing
only — `reporting._linked_directory` (`reporting.py:71`), test `tests/test_ux_tracking.py:241`.
Overlap tests to extend: `tests/test_ux_tracking.py:317-395`. **Root owns the OS-security design
decision; this map only records that nothing enforcing exists today.**

---

## 7. Costs and failure classification (Phase 7)

**Reuse:**
- Usage/cost parsing from provider JSON streams: `output.py:249-264`, `output.py:64`; accumulated per
  task in `lifecycle.execute` (`lifecycle.py:290-295`); read back by `ux_tracking._usage`
  (`ux_tracking.py:187`) → `dashboard_data` (`:250`), compact `top` columns TOKENS/COST USD
  (`ux_tracking.py:283-290`) and `collect_result` (`ux_tracking.py:430`).
- Real verification receipts: changed files + line counts via `reporting.file_changes`
  (`reporting.py:417`) and `.baseline.json` (`reporting.py:282`); `collect_result` exposes
  `baseline_known`/`baseline_partial`/`baseline_warning` (`ux_tracking.py:445-446`).
- Failure detection is deliberately scoped to engine output after the last
  `---------- output ----------` marker (`providers.failure_reason` `providers.py:321-333`), with a
  live-vs-post-mortem split (`providers.py:329`).
- Distinct exit codes already exist: 124 overall timeout, 125 silence watchdog, 126 provider
  limit/failure, 130 stopped (`runtime.py:205-213`, `lifecycle.py:344-351`, `:367`).

**Gaps:**
- Classification is one bucket, not typed classes: `state=limited` with a single free-text `reason=`
  (`lifecycle.py:344`) plus a reset hint (`providers.parse_limit_reset` `providers.py:340`). No
  separate TLS / DNS / proxy / auth-expiry / quota / model-unavailable classes.
- Retry decision is a boolean only (`providers.is_transient_failure` `providers.py:360`,
  `runtime.retry_wait` `runtime.py:24`) — never surfaced as a classification.
- No estimate-vs-reported-vs-billing distinction here (the bridge's approximate token estimate lives in
  `openai_server.py`, outside the mapped modules).
- **No cost or quota guard exists**: no budget threshold, no pre-flight remaining-quota check, no
  `estimated` flag on `cost_usd` (`ux_tracking.py:203`). Advertised per-model cost exists only in the
  catalogue (`model_catalog.normalize` `model_catalog.py:64`, `probe_status` `model_catalog.py:130`).
Tests: `tests/test_limit_reporting.py`, `tests/test_ux_tracking.py:397,414`.

---

## 8. Compact fleet interface (Phase 8)

**Reuse:**
- Fleet row builder exists CLI-side: `ux_tracking.dashboard_data` (`ux_tracking.py:223`) →
  `ux_tracking.top` (`:265`) with queue depth (`:257`), tokens, cost, files, activity age.
- GUI task rows: `gui.list_tasks` (`gui.py:401-440`) with `queued_messages` (`gui.py:389`),
  `eff_state` (`:286`), `LIVE_STATES` (`:305`), activity/topic heuristics (`:366`, `:382`).
- Conflict signal exists: `ux_tracking.overlaps` (`ux_tracking.py:304`), surfaced by `result`
  (`ux_tracking.py:468`).
- Detail panel parsing: `gui.parse_dialog` (`gui.py:717`), `gui.dialog_payload` (`:769`), cached and
  bounded (`gui.py:765-767`). Control endpoints: `/api/run` (`gui.py:2012`),
  `/api/task/send|stop` (`:2041`), `/api/reply` (`:2044`), `/api/wait` (`:1911`),
  `/api/stream` (`:1923`).

**Gaps:**
- No grouping/rollup in the GUI; the compact column set exists only in `top`.
- No conflict/overlap or ownership view in the GUI.
- No queued-message *content* view (count only, `gui.py:389`); no `.history.jsonl` view despite the
  file existing.
- No attention/questions surface: the only heuristic is `lifecycle.looks_waiting`
  (`lifecycle.py:35`), used for CLI state (`:367`) and never surfaced as a fleet alert.
Tests: `tests/test_gui.py`, `tests/test_ux_tracking.py:397-452`.

---

## 9. Cross-cutting compatibility risks

1. `.meta` is a flat single-line `key=value` file; new keys must match `[A-Za-z0-9_-]+` with no
   newline (`state.py:301-304`). No nested structures — receipts must be separate files.
2. `Store.read` prunes logs as a side effect (`state.py:294-299`, `logs.prune_task`); durable state
   must not live in the pruned set, or `result` must be extended first (`tests/test_ux_tracking.py:454`,
   `tests/test_prune_housekeeping.py`).
3. Legacy tasks lack `core_version`, `generation`, `detached` — new fields need the same fallbacks at
   `lifecycle.py:129`, `views.py:145`, `lifecycle.py:207`.
4. `activity.redact` is applied to stored text (`ux_tracking.py:21`, `reporting.py:485`); new event
   payloads are secrets-sensitive.
5. Legacy Bash owner compatibility is checked via `core_version` guards (`lifecycle.py:129`, `:176`);
   new features must degrade rather than break a legacy owner. `state.Lock` generations
   (`state.py:65-140`) interlock Python and Bash writers — new shared files need the same discipline.

---

## 10. Disjoint file ownership proposals for future workers

| Worker | Exclusive files | Touches shared core? |
|---|---|---|
| W1 SDK facade | NEW `neoxider_agents/sdk.py`, NEW `tests/test_sdk_facade.py` | no (imports `lifecycle`, `views`, `ux_tracking`) |
| W2 receipts/events | NEW `neoxider_agents/events.py`, NEW `tests/test_receipts_events.py` | **yes** — sole editor of `state.py` (`enqueue_locked`) and `lifecycle.py` (drain `:352-360`) |
| W3 GUI fleet | `gui.py`, `gui.html`, `static/`, `locales/`, `tests/test_gui.py` | no (HTTP only) |
| W4 Runtime isolation | `providers/*/provider.py`, `neoxider_agents/runtime.py` (env at `:135`) | partial — needs W2's meta keys |
| W5 Verification/reporting | `reporting.py`, `ux_tracking.py`, NEW `tests/test_scope_enforcement.py` | **yes** — sole editor of these two |
| W6 Docs | `docs/`, `SKILL.md`, `AGENTS.md`, `GEMINI.md`, `README.md` | no |

Unassigned / Root-only: OS-security or sandbox design (Phase 5), release and commit.
