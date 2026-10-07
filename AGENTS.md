# neoxider-agents — agent instructions

This repo is the Python `neoxider_agents/` core + native launchers + `gui.py`/`gui.html`: a non-interactive wrapper for launching
and managing CLI coding subagents (Codex, Claude Code, Kimi Code, opencode, Gemini CLI) across a
tracked task state and a redacted activity digest, plus an optional zero-dependency local web GUI.

If you (an AI agent) are working *in* this repo, see [`SKILL.md`](SKILL.md) for the
full command reference, model-alias tables, the question-detection heuristic, and
known trade-offs — it's the canonical operating manual and takes precedence over this
file if anything here goes stale. This file exists so Codex CLI and opencode (which
read `AGENTS.md` natively) and Claude Code (which reads it as secondary context) all
pick up the same baseline instructions without extra setup.

## Quick reference

```powershell
$SK = './agent.ps1'
& $SK "<prompt>"                            # implicit run; cwd/config/detected engine
& $SK ask "<prompt>"                        # final answer only; exit code = task result
& $SK -p prompt.txt                          # file prompt; or pipe text into & $SK -
& $SK run  -t <name> -C <dir> "<prompt>"     # tracked foreground task
& $SK run  -e kimi -t <name> -C <dir> "..."  # Kimi Code, Kimi K3 by default
& $SK run  -e claude -t <name> -C <dir> "..." # -e: codex|claude|kimi|opencode|gemini
& $SK fan  -t <base> -C <dir> "p1" "p2" ...   # N parallel background tasks (<base>-01, -02, ...)
& $SK send <name> "<message>"                 # queue while running; resume otherwise (reply is an alias)
& $SK send <name> --prompt-file F             # options also work after the task name
& $SK send <name> --now "<message>"           # interrupt the turn, then resume with the message + inbox
& $SK send --flush <name>                     # recover an undelivered inbox
& $SK stop <name>...                          # stop by name; --all-mine uses current ownership
& $SK restart <name> ["<message>"]            # resume saved session; --fresh replays original prompt
& $SK peek <name> [-n N] [-f] [--raw]          # readable activity; default last 25 entries
& $SK watch <name>                           # follow activity with a status footer
& $SK top --once                             # orchestrator tasks; --json or dashboard alias
& $SK diff <name> --stat                      # baseline delta; --names for paths only
& $SK result <name> --json                    # structured completion report
& $SK brief --outcome "Tests pass" --owns "src/**" --not-touch "providers/**" --return "Files and evidence" | & $SK run -t fix --owns "src/**" -
& $SK config list                            # config get KEY / config set KEY VALUE
& $SK completion powershell --install       # task/flag/engine/model completion; also bash/zsh
& $SK log  -f <name>                          # follow a task live
& $SK status <name>                           # state / current step / needs a reply?
& $SK doctor                                   # foreign CLI availability + limits, when needed
& $SK doctor --deep                            # + one REAL run per engine that must EXECUTE a shell command
& $SK gui                                      # web GUI (stable default port 8765; or: ./bin/neoxider gui)
```

Windows launchers use Python 3.8+ directly (PowerShell 5.1/7 or cmd), without Git Bash/WSL.
POSIX `agent.sh` is a thin shim; `AGENT_LEGACY=1 ./agent.sh ...` selects the retained Bash
engine for this release. Existing state files remain compatible. `stop` reaches the owning
launcher with a STOPPED report and exit 130; cancelling the launcher kills its provider tree.
See [SKILL.md](SKILL.md#windows--powershell-invocation) for native-parity and migration details.

**Phase 2B defaults.** Directory = cwd; name = prompt slug plus collision-checked four-character
ID; engine/model/effort = flags, then `AGENT_ENGINE` / `AGENT_MODEL` / `AGENT_EFFORT`, then config.
Choosing an explicit different engine skips configured model/effort. Detection prefers installed
Claude, otherwise the first installed provider. Config lives at
`%APPDATA%/neoxider-agents/config.json` or `~/.config/neoxider-agents/config.json`;
`XDG_CONFIG_HOME` is respected and `AGENT_CONFIG` overrides the file. Keys: engine/model/effort.
The wrapper writes no project files by default. `--progress` / `AGENT_PROGRESS=1` opts into
`PROGRESS.<task>.md`; `--no-progress` is accepted silently. Output is one start line plus a final
block; `-v` / `--verbose` restores the stream. Raw logs are a bounded 2 MiB state-directory tail,
removed by `clean` or after 24 h on the next CLI/GUI state read (`AGENT_LOG_MAX_BYTES`,
`AGENT_LOG_TTL_HOURS`); TTL pruning is lazy, not a daemon. Default `fan` discards launcher
stdout; only `--log` creates `.launcher.log`.
`--log` / `AGENT_KEEP_LOGS=1` keeps full raw logs. Metadata, final `.answer`, compact redacted
`.activity.jsonl`, baseline/history, prompts, session ID and inbox remain under `AGENT_CLI_LOGS`
(default `~/.claude/agent-cli-logs`); provider-native sessions remain in provider storage.
`peek`/`top`/`diff`/`result` and lifecycle commands continue working after raw pruning.
`--notify` / `AGENT_NOTIFY=1` requests best-effort hidden desktop notifications.
Provider prompts always travel via file/stdin; use `-p FILE` / `--prompt-file FILE` or stdin for
large Unicode inputs. Trailing `-p` or `-p` followed by another option still enables progress;
ambiguous old `-p TEXT` gives a fix to use `--progress TEXT`. Ordinary errors are one line with
a fix; `--debug` enables traces.

Record file ownership with `run --owns "glob,glob"`; `--strict-owns` refuses running declaration
overlaps. `brief --owns` writes contract text only. Baseline deltas include initially dirty
trees and non-git directories; concurrent edits in a shared directory cannot prove authorship.
Observed shared-file changes warn. Use disjoint ownership or isolated worktrees.

Baselines enumerate Git tracked/unignored candidates; fallback scans skip Unity caches.
`AGENT_BASELINE_BUDGET_SEC` defaults to 30 seconds. `AGENT_BASELINE_MAX_FILES` defaults to
200000 Git candidates or 20000 fallback files; fallback scans also stop at
`AGENT_BASELINE_MAX_BYTES` (536870912 bytes). `AGENT_BASELINE_SIZE_CAP_BYTES` (8388608)
selects size/mtime plus first/last 64 KiB fingerprints.
Partial baselines report incomplete tracking explicitly. Dead-launcher reads never scan the
workspace; explicit stop/result/diff caches deltas. Dead-owner task/global locks recover and
lock errors name the holder PID. See [performance limits](docs/PERFORMANCE.md).

**Control running workers.** Prefer `send`/`peek`/`stop`/`restart` over blind waiting.
A running `send` returns `queued (#N)` and saves ordered messages atomically in
`$AGENT_CLI_LOGS/NAME.inbox/`; the owner drains them in the same session after the turn,
before finishing. A tracked `wait NAME` includes the drain. `reply` has identical behavior.
Use `--now` only when the current work must change immediately; interruption can leave
partial edits, so inspect the working tree before continuing. `stop` is idempotent and
preserves session, retained results/activity, inbox and any opt-in `PROGRESS.<task>.md`, with state `⏹ stopped`.
`restart` defaults to checking `git status/diff` and continuing unfinished work;
`--fresh` uses the stored original prompt/engine/model/effort/directory in a new session.
Keep `AGENT_PARENT` consistent for ownership-based `stop --all-mine`/`wait`/`pending`;
`AGENT_ORCHESTRATOR_ID` is the fallback, and `--all-mine` refuses without ownership.
Stop uses process-tree termination and cannot guarantee graceful native cancellation.
`list`/`status` show last activity and queued count; `pending` flags stopped tasks and
`N undelivered message(s)` if the wrapper died. Recover those with `send --flush NAME`
or `restart NAME`. `clean` protects undelivered inboxes unless `--all`/`--purge` is explicit.
Gemini (`supports_resume=false`) refuses running follow-ups and requires a fresh run
after stopping. All commands accept `--help` without starting work, reject unknown flags,
and accept options before/after positionals; `--` ends option parsing. Failed send/reply
preflight preserves the previous state/exit/answer and records `last_send_error`.

**Never a silent hang.** Every step runs under `AGENT_TIMEOUT_SEC` (default 1800s): on
expiry the whole process tree is killed, the log gets a `!! TIMEOUT …` line and the task
ends `state=error exit=124`. A SEPARATE no-output watchdog, `AGENT_SILENCE_SEC` (default
600s), kills the tree and ends the task as the distinct `state=silent` if no NEW log
activity appears for that long — "stuck", as opposed to `error exit=124`'s "took too long
overall". Claude tasks stream by default (`--output-format stream-json` piped through
`stream_text_filter.py`) so the log grows while the model works and a partial answer
survives a killed turn: plain `claude -p` prints nothing until the turn ends, which made
healthy workers look stuck. That filter also writes one `[agent-activity] tool <Name>`
line per tool call, so a long tool-only stretch counts as activity; those lines show in
`log` and are stripped from `last`/`status`. `AGENT_STREAM_TEXT=0` restores the buffered
path.
A task that is alive but quiet longer than `AGENT_STALE_SEC` (300s, reporting
only) is reported as `running (no output for Nm)` — the same wording in the CLI and the
GUI, which share one liveness rule (the CLI additionally checks for a live ENGINE
descendant under the wrapper pid before calling a quiet task `idle`, not just `stalled`).
A provider-level failure (usage/rate limit, quota, auth expiry, unavailable model) ends
the task immediately as `state=limited`, with the provider's own message in meta
`reason=`. Only ENGINE output is scanned for it — the log after the last
`---------- output ----------` marker, never the echoed prompt/reply above it — and while
the step is still running only the explicit `AGENT_PROVIDER_ERROR:` tag from the
codex/kimi/opencode filters counts; the generic wording regex runs post-mortem (rc != 0)
only, so a prompt or an answer that merely discusses a "rate limit" does not kill the
task. Codex runs are isolated from `~/.codex/config.toml`
(`--ignore-user-config`), because the ChatGPT desktop app's config there hangs codex's
tool router on the first shell command; re-add a specific MCP server with
`AGENT_CODEX_MCP="name=url"`, or opt out entirely with `AGENT_CODEX_USER_CONFIG=1`.

**Self-testing your own work.** If you (the agent reading this) just built or modified
a local web service/API, you can verify it yourself before declaring the task done:

```bash
bash agent.sh test-api --base-url http://127.0.0.1:<port> \
  --goal "<what to verify>" --out result.json
```

This spawns another agent that exercises your API with real HTTP calls and reports
structured pass/fail JSON — a quick self-check before you say a task is finished.

**Need an OpenAI-compatible LLM backend pinned to a specific model?** Use
`agent.sh openai-server` instead of wiring up a real provider API key — e.g. when
another tool's test harness only knows how to point at an OpenAI-style base URL:

```bash
bash agent.sh openai-server -e claude -m sonnet -f low -p 8801
# then point any OpenAI-compatible client's base_url at http://127.0.0.1:8801/v1
```

This is a wire-compatible shim, not a real low-latency LLM API — know the trade-offs
before relying on it: it keeps **one ongoing chat session per process**, not a fresh
agent every call — when a new call's `messages` is a deterministic extension of what
it saw last time (exact prefix match, not a guess), it resumes the *same* underlying
CLI session via `agent.sh reply` with only the new tail; any mismatch (edited history,
an unrelated conversation, the first call, or a dead/errored session) falls back safely
to a brand-new `agent.sh run` with the full history. `claude`/`codex`/`kimi`/`opencode` support
resume (`gemini` always takes the fresh-run path). Consequence: **one bridge
process serves one conversation at a time** — a lock serializes every request, so don't
point multiple unrelated tasks at the same port expecting independence (run one process
per port per conversation instead); `POST .../reset` clears the remembered session,
and `GET /health` reports `session_active`/`session_turns`. An idle session also
auto-expires after `--session-ttl` seconds (default 1800 = 30 min) — the next call
after that just starts fresh instead of resuming. Beyond that: latency is a
**full CLI subprocess invocation** (seconds to low minutes, not a token stream);
`stream: true` **is REAL per-token streaming on the `claude` engine** (deltas stream as SSE
chunks while the model generates); non-live engines (codex/kimi/opencode/gemini) and
`--no-live-stream` instead **replay an already-finished answer** as word-sized SSE chunks;
`tools`/function-calling is **emulated via prompting**
(best-effort; the bridge accepts the call as EITHER a JSON `{"tool_calls":[...]}` block
OR literal `name(arg=value, ...)` lines — codex tends to write the latter — and the
prompt warns that prose describing an action is ignored; re-sent on every `tools` call);
`usage` token counts are an **explicit approximate estimate** (~4 chars/token, flagged
"neoxider_estimated": true) — not billing-grade; and **`content` is a clean answer for every bundled engine** (`codex` would
otherwise mix its banner/session-id/error-log/"tokens used" chrome into the answer, so its
provider runs `codex exec --json` and extracts only the final agent message — this also
cleaned up `agent.sh last`/the GUI for codex). One process = one fixed engine/model/effort
— run it again on another port to compare models. **The wrapped CLI is locked to
chat-only execution for bridge calls**: `AGENT_CHAT_ONLY=1` makes codex run
`--sandbox read-only --ignore-user-config`, claude keeps `--permission-mode acceptEdits`
and runs `--strict-mcp-config --disallowedTools
Bash,Edit,Write,NotebookEdit,Task,WebFetch,WebSearch`, Kimi uses a
`tools: []` agent profile, and opencode uses an agent profile whose tool map is
`{"*": false}` (`providers/opencode/chat-only.json` via `OPENCODE_CONFIG`), so the bridge can't
reach a real MCP server (verified live against a configured `unityMCP`) or write files
instead of answering in the expected format — a normal `agent.sh run` outside the
bridge is unaffected. `gemini` is the exception: chat-only downgrades it to
`--approval-mode plan` (no writes/exec) but its READ tools cannot be removed, so don't expose a
gemini bridge on a network. opencode's native `serve` path is OFF by default for the same
reason — it does not honour that profile over HTTP (`AGENT_OPENCODE_NATIVE_UNSAFE=1` opts in,
loopback only). Chat-only ignores `AGENT_CODEX_SANDBOX` and
`AGENT_CLAUDE_PERMISSION`: this HTTP-to-CLI boundary must never let arbitrary callers
gain write, shell, or permission-bypass access through the environment.

**Exposing either server off this machine.** The bridge takes `--api-key SECRET`
(`$AGENT_OPENAI_KEY`) and then requires `Authorization: Bearer SECRET` on everything except
`/health`. The GUI is loopback-only unless started as `gui <port> --lan --token SECRET`, and it
REFUSES to bind the network without that token because it launches full-auto subagents. Remember
which machine does the work: the agent always runs on the host, so the caller's own files are
never touched.

### Recipe: standing in for a local model server

A test suite that needs a live OpenAI-compatible model does not need LM Studio or a
provider key. Bring the bridge up and point the suite at it:

```bash
SK=~/.claude/skills/neoxider-agents/agent.sh
bash "$SK" openai-server -e opencode -m muse -p 8801 # foreground; track in the harness
curl -s http://127.0.0.1:8801/health     # {"ok": true, "engine": "opencode", ...}
curl -s http://127.0.0.1:8801/v1/models  # the id the client must ask for
```

Verified end to end on 2026-09-10 against CoreAI's PlayMode suite, which until then
failed with "No models loaded" from LM Studio:

```bash
export COREAI_PLAYMODE_LLM_BACKEND=http
export COREAI_TEST_BASE_URL="http://127.0.0.1:8801/v1"
export COREAI_TEST_MODEL="opencode/muse"
```

`RuntimeBackendSwitchLivePlayModeTests` went from failing to passing in 55 s.

Know what this proves and what it does not. It proves the code path works against a real
OpenAI-compatible endpoint. It is NOT a substitute for a run against the real backend:
every call spawns a CLI subprocess and a lock serializes them, so a fixture that makes
nine role calls or builds a whole scene takes many minutes and can hit its own timeouts;
and tool calling is emulated through prompting rather than native, so a test whose subject
IS the tool-call protocol may behave differently than it would on a real provider. Check
`/health` for `session_active` and `POST /v1/reset` between unrelated suites — one process
serves one conversation at a time.

## Rules for using this tool as a subagent orchestrator

Follow the ownership, routing and verification loop in [SKILL.md](SKILL.md). Delegate only
useful bounded work, honor explicit model choices, and serialize shared runtime mutations.
For Windows launches or shared-service changes, read [runtime discipline](docs/RUNTIME-DISCIPLINE.md).
Native workers do not require CLI `doctor`; a worker report alone is not acceptance.

- Give coordinated tasks a stable name via `-t`; the auto-generated prompt slug plus
  four-character ID is collision checked.
- Always `reply` by task name (or session id) — never rely on "last task" when more
  than one subagent might be running, or you'll answer into the wrong session.
- Every provider runs fully unattended: Codex defaults to `--sandbox danger-full-access`,
  Claude to `--dangerously-skip-permissions`, Gemini to `--yolo`, opencode to `--auto`,
  and Kimi to its auto policy (see `providers/*/provider.py`
  and the "Adding a provider" section of [`README.md`](README.md) for the exact flag
  and the Codex/Claude opt-down variables). Do not remove those flags; a subagent's stdin
  is always closed, so a provider that blocks on a prompt hangs forever instead of failing loudly.
- Keep this file, `GEMINI.md`, and `SKILL.md` in sync when the tool's interface changes
  — they intentionally overlap so every CLI convention picks up the same instructions.
