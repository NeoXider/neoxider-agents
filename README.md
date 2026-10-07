<div align="center">

# neoxider-agents

**A tiny local control room for AI coding subagents.**

One Python core, native PowerShell/cmd/POSIX launchers, a zero-dependency web panel, and an OpenAI-compatible HTTP bridge —
across **Codex · Claude Code · Kimi Code · opencode · Gemini CLI**.

[![zero dependencies](https://img.shields.io/badge/dependencies-zero-brightgreen)](#installation)
[![python stdlib only](https://img.shields.io/badge/python-stdlib%20only-blue)](#installation)
[![tests](https://img.shields.io/badge/tests-offline%20suites-success)](#development)
[![license MIT](https://img.shields.io/badge/license-MIT-lightgrey)](LICENSE)

</div>

---

## PowerShell quick start

Install Python 3.8+ from python.org with **Add Python to PATH**, clone this repository,
and install the command with `powershell -NoProfile -ExecutionPolicy Bypass -File .\bin\install.ps1`.
Open a new shell and use these three commands:

```powershell
neoxider ask "Explain this project in two sentences"
neoxider -t fix-readme "Fix the README typo and report the change"
neoxider result fix-readme --json
```

Run these from your project directory. No subcommand means `run`; `ask` prints only the final
answer and returns the task exit code. `run` stays in the foreground until the answer is ready,
printing one start line and a final-result block. Track that command as a background
job through your orchestrator's harness to receive completion notifications. From another
shell use `neoxider peek fix-readme`, `neoxider stop fix-readme` or `neoxider restart fix-readme`.
Without installing PATH, use `.\agent.ps1` or `.\bin\neoxider.cmd`.

### Defaults and files

| Setting | Default | Change it |
|---|---|---|
| Working directory | Current directory | `-C DIR` |
| Task name | Short prompt slug plus four random characters; collision checked | `-t NAME` |
| Engine/model/effort | Flags, then `AGENT_ENGINE` / `AGENT_MODEL` / `AGENT_EFFORT`, then config; otherwise installed-engine detection | `config set engine codex`, `config set model MODEL`, `config set effort low` |
| Project progress file | Off | `--progress` or `AGENT_PROGRESS=1`; `--no-progress` remains accepted |
| Terminal output | One start line and one final block; `ask` is answer only | `-v` / `--verbose` streams output; `--no-terse` changes prompt concision |
| Raw provider logs | Ephemeral tail, at most 2 MiB per task, expires after 24 hours | `--log` / `AGENT_KEEP_LOGS=1` keeps full logs; `AGENT_LOG_MAX_BYTES` / `AGENT_LOG_TTL_HOURS` |
| Desktop notifications | Off | `--notify` / `AGENT_NOTIFY=1`, best effort |

The wrapper writes no project files by default. `--progress` asks the worker to maintain
`PROGRESS.<task>.md` there; provider work can still change the files the task requests.
All task state lives under `AGENT_CLI_LOGS` (default `~/.claude/agent-cli-logs`): `.meta`
(state/session/ownership), `.answer` (final answer), `.activity.jsonl` (compact redacted digest),
baseline/history/control sidecars, original prompt and `.inbox/` (durable messages). The `.log`
tail is optional diagnostic data: `peek`, `top`, `diff`, `result`, `last`, controls and `wait`
continue working after it is pruned. TTL pruning is lazy: it occurs on the next CLI or GUI
state read, without a background deletion daemon;
`clean` removes settled raw logs immediately. `--log` skips automatic TTL pruning until cleaned.
Snapshots (`.md`) summarize retained results; they do not preserve a second raw transcript.
The provider's own native sessions remain in its normal provider directories.

| State artifact (prefix = task name) | Purpose / retention |
|---|---|
| `.meta`, `.answer`, `.activity.jsonl` | Task state/session ID, clean final or partial answer, compact redacted activity; kept while listed |
| `.baseline.json`, `.baseline.files/` | Start status/hashes and private text snapshots; at most 2 MiB text per file / 16 MiB per task |
| `.changes.json`, `.history.jsonl` | Frozen final deltas and allowlisted redacted send/stop/restart metadata; history excludes prompts |
| `.original.prompt`, `.prompt`, `.fan.prompt` | Original/resume/provider input, kept in state rather than provider argv |
| `.inbox/` | Ordered queued messages, including in-flight messages preserved for recovery |
| `.stop`, `.seen`, `.md` | Cancellation report, result-read marker and final-result Markdown summary |
| `.log` | Ephemeral raw tail by default; `--log` retains full output; `clean` removes settled raw logs |
| `.launcher.log` | Fan launcher output only with `--log`; default fan stdout is discarded; `clean` removes settled launcher logs |
| `.control`, lock/reservation/answer-spool files | Transient control, atomic publication and streaming transport; normally removed by the owner |

Baseline hashes cover binary/large files, with line counts unavailable beyond snapshot limits.
The manifest ignores VCS/dependency/cache/build directories (`.git`, `.hg`, `.svn`,
`node_modules`, `.venv`, `venv`, `__pycache__`, `.pytest_cache`, `.mypy_cache`, `.ruff_cache`,
`.cache`, `dist`, `build`), `PROGRESS.*.md`, `.agent*` and the configured state subtree.
Declared `--owns` scopes reported file paths. `clean --purge` also removes settled task state;
undelivered inboxes are protected unless `--all` / `--purge` is explicit.

Config lives in `%APPDATA%/neoxider-agents/config.json` on Windows or
`$XDG_CONFIG_HOME/neoxider-agents/config.json` or `~/.config/neoxider-agents/config.json` on POSIX.
`AGENT_CONFIG` selects a different config file. Config keys are `engine`, `model` and `effort`;
an explicit different engine skips that config's model/effort. Without an engine setting,
local detection prefers installed Claude and otherwise the first installed provider.
Use `neoxider config list`,
`neoxider config get engine`, and `neoxider config set engine codex`.
Install completions once with `neoxider completion powershell --install`
(`bash` and `zsh` are also supported).

Prompts can come from `neoxider -p prompt.txt`, `neoxider --prompt-file prompt.txt`, or
`Get-Content -Raw prompt.txt | neoxider -`. Provider prompts always travel through a file or
stdin, preserving Unicode and avoiding Windows' command-line length limit. Use a prompt file
or stdin for large inputs. Errors fit on one line with a fix; `--debug` exposes diagnostics.

```powershell
neoxider brief --outcome "Fix the parser; all parser tests pass" --owns "src/parser.py,tests/test_parser.py" --not-touch "providers/**" --return "Changed files and test evidence" | neoxider run -t parser -
neoxider top --once
neoxider diff parser --stat
```

`top` (alias `dashboard`) shows your orchestrator's tasks; `--json` emits a snapshot.
`watch NAME` follows readable activity with a status footer. `diff NAME` compares file content
with the start baseline, including pre-existing dirty files; `--names` and `--stat` summarize it.
In a shared directory these are changes during the task's lifetime, not proof of authorship.
`--owns "glob,glob"` warns about running ownership overlaps; `--strict-owns` refuses them.
Observed shared-file changes also warn. Use disjoint ownership or isolated worktrees when
several agents may edit at once.

No daemon, database or dependency install. Python uses only the standard library; Windows
needs no Git Bash or WSL, and Linux/macOS launch through the POSIX shim.

By [NeoXider](https://github.com/NeoXider).

### Three ways to drive it

| Surface | Command | What it's for |
|---|---|---|
| 🖥️ **CLI** | `agent.sh run / fan / send / peek / stop / restart / status / doctor` | scripting, orchestration, CI |
| 🌐 **Web panel** | `agent.sh gui` → http://127.0.0.1:8765 | a project tree of subagents, chat with each one, launch new tasks, live limits |
| 🔌 **OpenAI API** | `agent.sh openai-server -e claude -m sonnet -p 8801` | point any OpenAI client / benchmark / phone app at your CLI subscription |

### Why it exists

Every mature multi-agent "manager" (claude-squad, Vibe Kanban, Uzi, Crystal, CliDeck,
agent-of-empires…) is built for *interactive* parallel development in git worktrees, with a
TUI/GUI on top of Node/Go/Rust tooling. None gives a clean **headless** contract — launch a
task, log it as one readable thread, reply/resume by name, read back machine-readable state —
that you can script against with zero dependencies and a rate-limit panel that adapts to
whichever provider you picked. That gap is what this fills.

---

## POSIX quick start

```sh
git clone https://github.com/NeoXider/neoxider-agents.git
cd neoxider-agents
./agent.sh doctor
./agent.sh run -t hello -C . "Say hello"
./agent.sh gui
```

**Free lane, no OpenRouter API key.** With OpenCode authenticated locally, its Zen free tier works through the same wrapper, and
`opencode/space-bunny-free` is a current free model (verified 2026-09-29) with no short alias — pass
the id verbatim, `opencode/` prefix included:

```bash
bash agent.sh run -e opencode -m opencode/space-bunny-free -t spot-check -C . \
  "Summarize what changed in the last commit"
bash agent.sh log -f spot-check          # watch it work, same as any other engine
```

An unknown `-m` is passed through to the CLI unchanged rather than rejected, so brand-new Zen ids
work the day they appear without touching any provider file. Free-tier liveness is uneven, so keep
a bounded timeout and read the log rather than assuming silence means failure.

<details>
<summary><b>Installation options & requirements</b></summary>

**No package manager, no dependencies to install.** There is no `requirements.txt`, no
`pip install`, no `npm install`, nothing to build.

**Option A — as a Claude Code plugin** (recommended if you use Claude Code):

```
/plugin marketplace add NeoXider/neoxider-agents
/plugin install neoxider-agents@neoxider-agents
```

That registers `SKILL.md` for you automatically — no manual file copying.

**Option B — plain git clone** (works regardless of which CLI(s) you use it from):

```bash
git clone https://github.com/NeoXider/neoxider-agents.git
cd neoxider-agents
```

Requirements (you already have these if you use any of the CLIs below):

- Python 3.8+, standard library only, for core, GUI and bridge
- A POSIX shell on Linux/macOS; native PowerShell 5.1/7 or cmd on Windows
- At least one of the wrapped CLIs: [Codex CLI](https://github.com/openai/codex),
  [Claude Code](https://github.com/anthropics/claude-code),
  [Kimi Code](https://github.com/MoonshotAI/kimi-code), opencode, or the Gemini CLI —
  install and log in to whichever you plan to use; `agent.sh doctor` reports what it can see.

Optional one-time setup: the [`neoxider`](bin/README.md) command, a one-word launcher usable
from bash or PowerShell. Bare `neoxider` prints usage (a bare invocation should be help, not a
side effect); `neoxider gui [port]` opens the panel; everything else passes straight through to
`agent.sh`.

</details>

---

## Native task controls

| Action | Native task contract | neoxider contract |
|---|---|---|
| Start / resume | Tracked job completes with its answer | `run`, `reply` and `send` block; track the invocation in your harness |
| Follow-up | Deliver to the same worker | `send NAME TEXT` queues ordered messages and drains before completion |
| Interrupt | Completion reaches the launcher | `stop NAME` stops the tree and the owner prints `■ STOPPED`; owner and tracked `wait` exit 130 |
| Change direction | Keep the worker identity | `send NAME --now TEXT` interrupts and resumes inside the existing wrapper |
| Launcher cancelled | No abandoned worker | Job Object / process group kills descendants; recovery reports `launcher stopped` |
| Recover | Continue saved context | `restart NAME`, or `restart NAME --fresh` for a new session |
| Quick answer | Foreground answer with task exit code | `ask TEXT`; output contains only the final answer |
| Observe a wave | Activity and queued messages | `top [--once] [--json]`, `peek NAME`, `watch NAME` |
| Review changes | Compare with the task's starting tree | `diff NAME [--names or --stat]`; dirty trees and non-git directories supported |
| Review completion | Retained structured report | `result NAME [--json]`, including answer, files, duration, usage when known and control history |

Command reference (native PowerShell/cmd: `neoxider`; POSIX: `./agent.sh`):

| Commands | Arguments / purpose |
|---|---|
| `run`, `ask` | `[task options] TEXT`, `-p FILE` / `--prompt-file FILE`, or `-` stdin; no subcommand also runs; ask prints only the final answer |
| `brief` | `--outcome TEXT --owns GLOBS --not-touch GLOBS --return TEXT [--context-file FILE]`; prints a contract for piping to `run -` |
| `fan` | `[task options] TEXT...`; launch a wave, then track `wait` |
| `send`, `reply`, `restart` | `NAME [TEXT or --prompt-file FILE]`; send/reply accept `--now`/`--flush`, restart accepts `--fresh` |
| `stop`, `peek`, `log` | `stop NAME...` / `--all-mine`; `peek NAME [-n N] [-f] [--raw]`; `log [NAME] [-f] [-n N] [-l]` |
| `last`, `status`, `list` | `[NAME]` for final answer/status; `list [LIMIT]` defaults to 20 |
| `top`, `dashboard`, `watch` | `top [--once] [--json]`; `watch NAME` follows digest and status |
| `diff`, `result` | `diff NAME [--stat or --names]`; `result NAME [--json]` returns structured completion |
| `pending`, `wait` | `pending [--strict]`; `wait [NAME...] [--timeout SEC] [--poll SEC]` |
| `clean`, `prune` | `[--all] [--purge] [-n or --dry-run]`; `prune` aliases `clean` |
| `doctor`, `provider-info` | `doctor [--json or --deep]`; `provider-info ENGINE` prints one provider's diagnostic JSON |
| `test-api` | `--base-url URL --goal TEXT [--out FILE] [task options]` |
| `gui`, `openai-server` | `gui [PORT] [--localhost or --lan --token SECRET]`; `openai-server [-e ENGINE] [-m MODEL] [-f EFFORT] [-p PORT] [--api-key SECRET]` |
| `config`, `completion`, `help` | `config get KEY`, `config set KEY VALUE`, `config list`; `completion powershell or bash or zsh [--install]`; `help [COMMAND]` / `COMMAND --help` |

Task options: `-e ENGINE -m MODEL -f EFFORT -C DIR -t NAME -P PARENT`.
`-P` sets ownership. `--owns GLOBS` / `--strict-owns` guard declared ownership. Progress is
opt-in (`--progress`); legacy bare `-p` remains a compatibility flag when trailing or followed
by another option, while `-p FILE` reads a prompt. Ambiguous old `-p TEXT` gives a fix to use
`--progress TEXT`. `--no-progress` is accepted silently. `--no-terse` suppresses the terse directive;
`-v` / `--verbose` streams output, `--log` keeps full logs, and `--notify` enables optional
desktop notifications. `--terminal` explicitly permits a visible provider console.
Service `openai-server -p` sets its port. See [SKILL.md](SKILL.md) for examples.

### Python core and migration

Python 3.8+ and its standard library now implement the engine. PowerShell 5.1/7 and cmd
invoke it natively; Linux and macOS keep the thin POSIX `agent.sh` shim. Git Bash and WSL
are not Windows dependencies. Bash forks were expensive on Windows and failed under memory
pressure; its launcher could expose terminal windows, select the WSL stub, or leave provider
children alive. Python uses in-process polling/watchdogs, `CREATE_NO_WINDOW` and hidden
startup info, Windows Job Objects with `KILL_ON_JOB_CLOSE`, and POSIX process groups.
PowerShell carries arguments as UTF-8 JSON through stdin, preserving Cyrillic, embedded
quotes and newlines without storing tokens in a transport file.

The existing `AGENT_CLI_LOGS/<task>.meta`, `.log`, `.md` and `.inbox` layout remains readable
across engines. Keep the existing state directory to resume old sessions. Logs retain
`session id:` and `---------- output ----------`; exit 124/125/126 still identify deadline,
silence and provider failures. Exit 130 identifies explicit cancellation, including `wait` on
stopped tasks (`WAIT_DONE rc=130`); this deliberately replaces the Phase 1 wait exit 0. Watchdog termination
also publishes a STOPPED report with its reason while retaining those existing codes/states.
During this release the previous Bash implementation lives in `legacy/`: on POSIX use
`AGENT_LEGACY=1 ./agent.sh ...`; on PowerShell set `$env:AGENT_LEGACY='1'` and explicitly
invoke Git Bash on `agent.sh`. The native PowerShell/cmd entry always uses the Python core.
Do not replace files in a checkout whose legacy wrapper is currently running: deploy the
completed checkout after its launchers finish, or launch the new core from a separate checkout
pointed at the same state directory. Running legacy tasks keep their legacy process lifetime
until restarted by the new core.

---

## Working from another computer 🌍

This is the question everyone asks, so here is the honest answer up front. **The agent always
runs on the machine hosting the server, and every file it touches is a file on that machine.**

| What you want | Do this |
|---|---|
| An LLM endpoint for an app / benchmark / phone, backed by your CLI subscription | `agent.sh openai-server … --api-key SECRET` — the client sends messages, gets text back. It is chat-only by design: it answers, it does not edit code. |
| **Edit files that live on the server**, driven from another PC | `agent.sh gui 8765 --lan --token SECRET` — open `http://<host>:8765/?token=SECRET` on the other machine and launch tasks in the browser. |
| **Edit files that live on the other PC** | Install this repo *there* and run agents locally on it. No API can do this — an agent has no access to the caller's disk. |

Both servers refuse to be careless about it:

- **API bridge** (`openai-server`) binds `127.0.0.1` by default. LAN access is explicit via
  `--lan` and is refused without `--api-key SECRET` (or `$AGENT_OPENAI_KEY`); every caller must
  send the standard `Authorization: Bearer SECRET` header — any OpenAI client already does, it's
  just the `api_key` field. `/health` stays open for liveness probes; everything that can spend
  tokens returns 401 without the key. Codex/Gemini bridges remain loopback-only because their
  CLI restrictions still permit host reads.
- **Web panel** (`gui`) is loopback-only by default, because it *launches full-auto subagents in
  any directory a caller names* — an open LAN panel is remote code execution. `--lan` therefore
  **refuses to start** without `--token` (or `$AGENT_GUI_TOKEN`). The token is accepted as
  `?token=` once (then redirected to a clean URL and stored in an HttpOnly cookie), as an
  `X-Agent-Token` header, or from the cookie. When configured, it is required even on loopback,
  so a reverse proxy cannot bypass it; tokenless local POSTs enforce JSON and same-origin headers.
- **Over the actual internet:** technically just a port-forward, but neither server speaks TLS,
  so a bearer token would cross the network in clear text. Put it behind a VPN/overlay
  (Tailscale, WireGuard, ZeroTier) or an HTTPS reverse proxy (Caddy/nginx) that forwards to
  `127.0.0.1:<port>`, and keep the key/token on as a second lock.

---

## Features

- **Control a running agent.** `send` queues durable follow-ups, `peek` shows readable activity,
  `stop` interrupts a task by name, and `restart` continues its saved session. `reply` is an
  alias for `send`.
- **One identity per task.** Metadata, final answers, redacted activity and durable messages
  outlive the bounded raw log. `--log` retains the complete diagnostic transcript.
- **Live state, not just logs.** `running` / `running (no output for Nm)` (alive but quiet) /
  `waiting` (the agent asked a question) / `done` / `error` / `stalled` (process died — e.g. the
  machine was turned off) / `stopped` (`⏹`, stopped by the orchestrator) / `limited` (provider
  limit/auth/model failure) / `silent` (no-output watchdog). The GUI shares the owner-liveness
  rule; the CLI additionally checks engine descendants for legacy quiet tasks (see [GUI notes](docs/GUI.md)).
- **No silent forever-hangs.** Every step runs under a wall-clock deadline (`AGENT_TIMEOUT_SEC`,
  default 30 min). On expiry the whole process tree is killed — including the native Windows
  grandchildren a plain `timeout` leaves orphaned — the log gets an explicit `!! TIMEOUT …` line
  and the task ends as `error` exit 124, instead of sitting in `running` for hours unnoticed.
- **Durable checkpoints.** A markdown snapshot (`<name>.md`) after every step, plus the CLI's own
  resumable sessions: a task survives a reboot, and `agent.sh reply <name> "continue"` picks up
  where it left off. Each task also keeps its own `PROGRESS.<task>.md` checklist in the working
  directory only with `--progress` or `AGENT_PROGRESS=1`.
- **`fan` — parallel batches in one call.** `agent.sh fan -t audit -C dir "p1" "p2" "p3"` launches
  each prompt as its own background task (`audit-01`, `audit-02`, …) and returns immediately.
- **`doctor` — pre-flight before you fan out.** Which CLIs are installed, login state, and live
  rate-limit bars (window, % used, time to reset). Claude exposes no remaining-limit API, so it
  shows a clearly-labelled **local usage estimate** (5h and 7d, in+out and cache separately) from
  the CLI's own transcripts. `doctor --deep` goes further and makes an engine **actually execute a
  shell command** in a throwaway directory — the only check that catches a CLI that answers
  normally while every command it runs hangs forever.
- **Web GUI.** Project tree of subagents (activity + topic emoji), a full-dialog chat view
  (Claude-Code-style: every run/reply step in order, tool calls collapsed to one line and
  expandable, thinking blocks hidden by default, per-step and per-tool durations, basic markdown),
  a provider/model/**effort** picker with a cached refreshable limits panel, a folder browser,
  resizable panels, toasts with history, an optional "open in a real terminal" checkbox, an
  **API tab** that starts/stops bridges from the browser, and English/Russian (more via one
  dropped-in locale file).
- **Plugin providers.** A CLI provider — invocation, model/effort resolution, `doctor` info, GUI
  metadata — is one `providers/<name>/` directory. Adding one means creating that directory, with
  zero edits to `agent.sh`, `gui.py` or `gui.html`.
- **Model and effort are separate.** `-m <model> -f <effort>` (or two dropdowns), not baked into
  one alias string, for every provider that has an effort concept.
- **`test-api` mode.** `agent.sh test-api --base-url <url> --goal "<what to verify>"` points an
  agent at a local HTTP API and returns one strict JSON pass/fail report — the agent exercises it
  with real HTTP calls via its own shell/curl. The GUI's API tab wraps the same endpoint with a
  form, a results view, and ready-made curl / C# (Unity `UnityWebRequest`) snippets.

<details>
<summary><b>A real HTTP API, not just a GUI backend</b> — <code>/api/thread</code>, <code>/api/stream</code>, <code>/api/wait</code></summary>

`/api/thread?task=<name>` exposes the available raw log tail. `--log` retains the full
multi-turn transcript; with default retention old turns can be truncated or expire.
`/api/dialog` falls back to the retained final answer after raw pruning. Queue, activity,
failure reason and stop/send controls remain available.

- **`/api/stream?task=<name>`** — a Server-Sent Events (`text/event-stream`) endpoint that tails a
  task's `.log` in real time and pushes each new line as a `data: …` event, instead of making the
  client poll. It ends (an `event: done`, then closes) once the task leaves the live states
  (`running`/`idle`), or after a fixed idle timeout. Consumable from anything that can read an SSE
  or chunked HTTP stream — `EventSource` in JS, a line-buffered GET in curl/Python/C#.
- **`/api/wait?task=<name>&timeout=<sec>`** — a blocking-poll convenience: holds the response open
  (checking the task's state server-side every ~0.5 s) until the task leaves the live states or
  `timeout` seconds elapse (capped at 300), then returns one JSON object
  `{"name", "state", "model", "log"}`. Turns a kick-off call plus one wait into a synchronous
  round-trip for a test harness that doesn't want to hand-roll a polling loop.

</details>

<details>
<summary><b>Shared state, multiple installs, GUI port behaviour</b></summary>

All state (metadata, answer, digest, baseline, raw tail and inbox) lives under `AGENT_CLI_LOGS`
(`~/.claude/agent-cli-logs` by default) — shared on purpose, so one GUI shows every subagent no
matter which provider or which install launched it; concurrent writes to the same task are made
safe by a portable file lock. For strict isolation (two people on one machine, "personal" vs
"CI"), point installs at different directories: `export AGENT_CLI_LOGS="$HOME/.neoxider-ci-logs"`.

`gui.py` listens on a stable, documented port: an explicit CLI arg, else `$AGENT_GUI_PORT`, else
`8765` — so the URL stays bookmarkable across restarts. If the port is taken, the panel first
checks **who** holds it: its own running instance → it just opens the browser there; anything else
(8765 is popular — an unrelated WebSocket server squatting on it used to make `gui` claim success
while the tab failed with `invalid Connection header`) → it moves to the next free port and prints
the URL it actually bound in a banner you cannot miss.

</details>

<details>
<summary><b>Using this repo from other AI coding CLIs</b></summary>

The usage instructions are duplicated across the conventions different tools read automatically:
[`SKILL.md`](SKILL.md) (Claude Code), [`AGENTS.md`](AGENTS.md) (Codex CLI and opencode natively;
Claude Code as secondary context), and [`GEMINI.md`](GEMINI.md) (Gemini CLI, which hasn't adopted
the shared `AGENTS.md` convention). Whichever CLI you're chatting with picks up the same baseline
with no extra setup.

</details>

**Full command reference:** [`SKILL.md`](SKILL.md) — model aliases, the question-detection
heuristic, path-normalization notes, every environment knob, and the known trade-offs. It doubles
as the operating manual an AI agent reads before using this tool.

---

## Control a running agent

```text
agent.sh send NAME [--now] [-C dir] (TEXT | --prompt-file F)
agent.sh reply NAME [--now] [-C dir] (TEXT | --prompt-file F)
agent.sh send --flush NAME
agent.sh stop NAME... | --all-mine
agent.sh restart NAME [TEXT | --prompt-file F] [--fresh]
agent.sh peek NAME [-n N] [-f] [--raw]
```

`send` to a running task returns immediately with `queued (#N)`. Messages are written
atomically to `AGENT_CLI_LOGS/NAME.inbox/` in sequence order. The owning wrapper combines the
queued messages into a numbered follow-up turn in the same session after the current turn ends,
then checks for more messages before finishing. A tracked `wait NAME` returns after that drain
and prints the final answer. A send to a settled task resumes immediately; `reply` behaves
identically, including `reply NAME --prompt-file F`.

Delivery is durable and at least once: a crash after provider execution but before inbox
acknowledgement can replay the batch. Inspect partial edits before recovery. The drain is bounded
by `AGENT_INBOX_MAX_TURNS` (default 32); reaching the limit leaves the task `waiting`, retains the
inbox and requires `send --flush NAME` to continue delivery.

```bash
bash agent.sh peek fix-readme -n 25 -f
bash agent.sh send fix-readme "Add a regression test for the reported case"
bash agent.sh send fix-readme --now "Stop the current approach; inspect partial edits and use option B"
bash agent.sh stop fix-readme
bash agent.sh restart fix-readme "Keep the existing edits and finish the tests"
bash agent.sh wait fix-readme
```

Use `--now` when the current work must change immediately: it stops the current process tree
and resumes with the new message plus queued messages. Tool edits already made remain on disk;
an interrupted command may be incomplete. Plain `stop` preserves the session ID, inbox, log and
any opt-in `PROGRESS.<task>.md`; repeated stops are harmless. Stop uses process-tree termination and cannot
guarantee graceful native cancellation. `stop --all-mine` uses `AGENT_PARENT`, or
`AGENT_ORCHESTRATOR_ID` when parent ownership is absent; it refuses if no ownership is available.
Set ownership consistently when launching and controlling tasks. `restart` normally keeps the
same session; without text it asks the agent to inspect
`git status/diff` and continue unfinished work. `restart --fresh` starts a new session from the
stored original prompt, engine, model, effort and directory, retaining `previous_session` in meta.

`peek` shows recent commands and exit codes, file/tool activity, short assistant text, thinking
markers, errors, retries and turn usage where the stream provides them. It defaults to 25 entries;
`--raw` shows redacted source lines. Plain-text providers fall back to a labelled text tail.
`list` and `status` show the last activity and queued count. If a wrapper dies before delivery,
they and `pending` report `N undelivered message(s)`; use `send --flush NAME` or `restart NAME` to
recover them. `pending` also flags stopped tasks, and `wait` treats `stopped` as settled with
exit 130 and `WAIT_DONE rc=130`. `clean` protects an undelivered inbox unless `--all` or `--purge` explicitly overrides it.

Resume-capable engines are Claude, Codex, Kimi and OpenCode. Gemini's provider has
`supports_resume=false`: a running follow-up is refused; after a stop, start fresh with
`restart --fresh` or `run`. Resume preflight failures preserve the task's previous state, exit
and answer and record `last_send_error` in meta. Options work before or after positionals;
`--` ends option parsing, unknown flags fail, and `<command> --help` starts no task.

When upgrading, let old launchers settle or stop them before switching to the complete new
installation, including its helpers. Do not replace an executing `agent.sh` in place. Wrappers
already running before the upgrade cannot auto-drain newly queued messages; their inbox remains
visible, and `send --flush NAME` delivers it after the old turn settles.

---

## OpenAI-compatible bridge (`openai-server`)

```bash
agent.sh openai-server -e claude -m sonnet -f low -p 8801 --api-key "$(openssl rand -hex 16)"
# then: base_url = http://127.0.0.1:8801/v1 , api_key = <that secret>
```

A standalone, zero-dependency HTTP server exposing `POST /v1/chat/completions` (plus
`GET /health`, `GET /v1/models`, `GET /`, `POST /reset`) backed by a CLI subagent. Point any
OpenAI-compatible client at it and it drives your CLI subscription as the "model" — no separate
provider API key needed.

> ⚠️ **It is a wire-compatible shim, not a low-latency native LLM backend.** Read the trade-offs
> below before pointing production-ish things at it.

**At a glance**

| | |
|---|---|
| Session model | one process = **one ongoing conversation**, resumed via `agent.sh reply` when the `messages` array is an exact extension; anything else falls back to a fresh run |
| Concurrency | a lock serializes requests — safe, but not parallel. One port per conversation |
| Streaming | real token deltas on `claude`; word-sized replay of the finished answer elsewhere |
| Tool calling | emulated via prompting, parsing every call spelling real models emit |
| Auth | `--api-key` / `$AGENT_OPENAI_KEY` → `Authorization: Bearer …`; open if unset |
| Lockdown | every engine but `gemini` runs with real CLI flags that remove shell/file/MCP access |
| `usage` | estimated (~4 chars/token), flagged `"neoxider_estimated": true` |

<details>
<summary><b>Session continuation, reset, and idle expiry</b></summary>

The bridge remembers the exact `messages` array from the previous call. When a new call's
`messages` is that array **plus** one or more messages appended at the end (a deterministic prefix
check, not a model guessing), only the new tail goes to the *same* CLI session via `agent.sh
reply`, instead of re-serializing the whole growing history into a brand-new `agent.sh run`. That
saves the resend cost and lets the provider's own prompt caching actually apply.

API-вызовы отключают инструкции вести `PROGRESS.<task>.md` как при запуске, так и при
продолжении: текстовый endpoint не должен просить модель менять файлы. Служебные `.log`,
`.meta` и `.md` самого wrapper сохраняются. Повторное использование сессии само по себе
не доказывает попадание в кеш: это проверяется по данным провайдера, а `usage` этого API
остаётся оценкой.

Служебные поля одной фазы (`run`, выбор модели, завершение) записываются одним атомарным
обновлением metadata. Это сокращает запуск вспомогательных процессов на Windows и позволяет
панели прочитать согласованные `state`, `exit` и `reason`. Конкурентные одиночные и групповые
записи используют общую блокировку; новое состояние публикуется только после подготовки всей
группы. Многострочные значения и повторяющиеся ключи отклоняются до записи.

Any mismatch — edited/rolled-back history, a genuinely different conversation, the first call, or
a previous session that ended `error`/`stalled` — falls back safely to a fresh run with the full
history; it never resumes onto a session that might disagree with the caller. `claude`, `codex`,
`kimi`, and `opencode` support continuation (`provider.json`'s `supports_resume`); `gemini` remains
`false` and always takes the fresh-run path. opencode resumes the recorded session with `-s`
and preserves the selected model and effort via `-m` and `--variant`.
Verified live: Claude's task log showed one `[run]`
block followed by a `[reply]` block containing only the new tail, the task count stayed at 1
across 4 sequential calls, and an unrelated conversation sent next correctly started a new session.

**One bridge process serves one conversation at a time.** A lock (`SESSION_LOCK`) serializes every
request. That is safe under concurrency (verified: two genuinely concurrent unrelated requests both
got correct answers with zero cross-contamination) but not efficient for many independent parallel
conversations — start one bridge per port for those.

**`POST /reset`** clears the remembered session (drops the messages/task, wipes the scratch working
dir unless `--dir` pinned a real project). **Idle sessions expire** after `--session-ttl` seconds
(default 1800 = 30 min): an untouched session is treated exactly like a dead one, so an abandoned
conversation can't grow forever. `GET /health` reports `session_active`, `session_turns`,
`session_idle_seconds` and `session_ttl_seconds`.

</details>

<details>
<summary><b>Latency and streaming</b></summary>

**First-token latency depends on the engine.** On `claude` the bridge keeps ONE persistent
`claude -p` process (stream-json), so the ~7–11 s agent-environment boot is paid once and each
turn afterwards costs only inference (~3.5 s measured on Opus), keeping the provider prompt cache
warm across turns (`CLAUDE_NO_NATIVE=1` opts out). The other engines spawn a fresh CLI subprocess
per completion; both process startup and wrapper bookkeeping add latency before the response.
For Codex, `stream: true` replays the completed answer as SSE; it does not expose live model tokens.

**`stream: true` is REAL token streaming on `claude`:** the CLI runs with `--output-format
stream-json --include-partial-messages` piped through `stream_text_filter.py`, so the task log
grows while the model generates; the bridge tails it and forwards each new piece as an SSE
`delta.content` chunk. Tool calls in the canonical fenced `{"tool_calls":[...]}` format become
native `delta.tool_calls` chunks **as each call's JSON object closes** — a 100-call build turn
streams its calls one by one instead of dumping them at the end (verified: ~0.2 s spacing on
haiku). Safety valves: the first ~350 chars are held back so a provider limit banner can still
become an HTTP 429 (impossible once SSE headers are out); a full end-of-turn parse reconciles any
spelling the incremental scanner missed; invisible retries are only allowed while nothing has
reached the client. `--no-live-stream` is the emergency switch back to the legacy behaviour, which
non-live engines use anyway: the finished answer replayed as word-sized chunks ending in
`data: [DONE]`. The connection is explicitly closed after `[DONE]` so plain HTTP clients that
don't know that sentinel don't hang.

</details>

<details>
<summary><b>Tool calling (emulated) — every spelling it accepts</b></summary>

When a request includes an OpenAI `tools` array, the bridge instructs the agent (in the prompt) to
answer with either prose or a tool call, then parses the call into a real `tool_calls` response.
It accepts every spelling seen from real models:

- a fenced or bare JSON `{"tool_calls":[...]}` block (plus the wrapper aliases models substitute:
  `actions`, `calls`, `function_calls`, `commands`, `requests`);
- **one fenced JSON block per call** shaped like an OpenAI tool-call object (Sonnet 5's habit),
  or the flat `{"name":…, "arguments":{…}}` / `{"tool":…}` / `{"name":…, "parameters":{…}}` forms
  — only KNOWN tool names are consumed, so a plain JSON answer survives as content;
- a fenced JSON **array** of call objects (Opus 4.8) or of bare argument objects (Fable 5), and
  JSONL one call per line;
- literal call lines: `name(arg=value, …)`, `name({"arg": "value"})` with one positional JSON
  object (gpt-5.5's SDK-style habit), or `name("scalar")` mapped onto a one-parameter schema;
- the **nameless** spelling — the whole message is bare JSON argument objects, one per line, whose
  keys fit exactly one tool (spark's habit in single-tool scenarios).

Each of these, before it was supported, silently zeroed whole live benchmark groups. A malformed
element inside an array is salvaged object-by-object rather than dropping every good call with it.

**Echo protection:** after a tool-result round-trip models tend to restate the calls they already
made, in exactly the `name({…})` style the rendered history shows them. A call-syntax line that
exactly repeats an already-executed call (same name, same canonical arguments) is treated as
summary prose, not re-executed — fenced `{"tool_calls":[...]}` stays exempt as a deliberate format.
The prompt also warns that *describing* an action in prose does nothing. The instructions are
re-sent on every call that includes `tools`, including continuations — a deliberate
simplicity/robustness choice over tracking whether the schema already "stuck".

</details>

<details>
<summary><b>The chat-only lockdown (why an exposed bridge is not a shell)</b></summary>

Every subprocess the bridge launches gets `AGENT_CHAT_ONLY=1`, which the provider scripts turn
into **real CLI-level restrictions**, not a prompt request:

| Engine | What chat-only actually does |
|---|---|
| `codex` | `--sandbox read-only --ignore-user-config` — blocks writes and user MCP config, but still permits host reads; therefore Codex bridges are loopback-only |
| `claude` | `--strict-mcp-config --tools ""` — no MCP or native tools |
| `kimi` | an explicit `tools: []` agent profile |
| `opencode` | an agent profile whose tool map is `{"*": false}` (`providers/opencode/chat-only.json`, via `OPENCODE_CONFIG` + `--agent neoxider-chat-only`) — disables built-ins **and** MCP tools |
| `gemini` | `--approval-mode plan` instead of `--yolo` — no writes/exec, but read tools remain; therefore Gemini bridges are loopback-only |

This mode ignores `AGENT_CODEX_SANDBOX` and `AGENT_CLAUDE_PERMISSION` on purpose: the HTTP endpoint
turns arbitrary callers into CLI invocations, so environment overrides must never grant it write,
shell, or permission-bypass access. The restrictions apply **only** to bridge subprocesses — a
normal `agent.sh run` keeps full access for real coding work.

Why it matters: without it a model can reach for a REAL tool (an actual Unity Editor via
`unityMCP`) instead of answering in the expected `tool_calls` format — silently mutating live state
outside the calling application's control. Verified live: `-c mcp_servers={}` on the codex command
line did NOT stop a real `unityMCP.manage_tools` call from succeeding against a live Unity Editor,
but `--ignore-user-config` does.

`opencode` used to be the hole here, and it was a big one (fixed in 0.2.0): the bridge's
default `opencode serve` path had `bash`, `write`, `edit`, `webfetch` plus 40 live `unityMCP`
tools, and a plain "create notes.txt" request really created the file — in the repository's own
checkout, because that path also ignored the working directory. Since the server path does not
honour a chat-only agent (measured on opencode 1.18.16: identical profile answers `NONE` through
`opencode run --agent`, but still lists every tool over HTTP), `opencode` now goes through the CLI
path by default. `AGENT_OPENCODE_NATIVE_UNSAFE=1` opts back into the faster native path, with a
loud banner, for a loopback-only bridge.

The prompt was also reframed to avoid "You are acting as X, NOT as an autonomous agent"
identity-override phrasing — Claude Code's own prompt-injection defenses refused that framing live.
The current wording (`BASE_INSTRUCTIONS` in `openai_server.py`) states the restriction as plain
fact, which it genuinely is.

</details>

<details>
<summary><b>Reliability: retries, 429s, clean output</b></summary>

A completion whose CLI invocation came back empty or in an `error` state is re-run (`--retries`,
default 1) — a real OpenAI endpoint effectively never returns an empty 200, and one transient CLI
hiccup should not zero a whole benchmark scenario. A resume that "succeeds" but produces an empty
answer falls back to a fresh run the same way. An unexpected bridge exception returns an
OpenAI-style `{"error": {...}}` HTTP 500, never a bare connection reset. A CLI answer that IS the
provider's usage-limit banner ("You've hit your session limit · resets …") becomes an HTTP 429
`rate_limit_error`, so a rate-limited account reads as an environment problem rather than a model
that suddenly scores zero.

The bridge disables `agent.sh`'s inner retry layer (`AGENT_RETRIES=0`) for its child calls, so
`--retries N` means exactly one initial provider call plus at most N bridge retries rather than a
hidden multiplication of both retry loops.

`content` is a clean answer for every bundled engine. `codex`'s non-interactive `exec` mode
otherwise mixes its startup banner/session-id/error-log/"tokens used" chrome (and, on Windows, a
cp866-mojibake OS-notification line) into the same stream as the answer — so the codex provider
runs `codex exec --json` and extracts just the final agent message, which also cleaned up
`agent.sh last` and the GUI chat view.

**Images are attached for real on two engines.** Most engines still get an `[image omitted]` note
in place of the picture, but a bridge started with `-e codex` or with `-e opencode` (the safe CLI
path) accepts OpenAI `image_url` data URLs and hands the actual file to the model — `codex exec -i`
or `opencode run -f` — so the turn is genuinely multimodal:

```jsonc
{"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgo…"}}
```

| Rule | Behaviour |
|---|---|
| Formats | `data:image/png`, `data:image/jpeg`, `data:image/webp` — **`image/jpg` is rejected** |
| Per turn | at most **8 images**, at most **4 MiB** of decoded data per image → HTTP **413** |
| Remote URLs | unsupported on purpose (a data URL needs no outbound fetch of an untrusted link) |
| Bad input | unsupported format, `http(s)://` URL, broken base64, or bytes not matching the declared format → HTTP **400** naming the exact `messages[i].content[j].image_url` |
| Cleanup | images live in a per-turn temp dir deleted after normal completion or a handled timeout; abrupt process termination may leave a temp file |
| Transcript | the prompt keeps an `[image attached to Codex CLI]` / `[image attached to OpenCode CLI]` marker so the text record stays honest |
| Not covered | `claude`, `kimi`, `gemini`; opencode's opt-in `AGENT_OPENCODE_NATIVE_UNSAFE=1` HTTP path; the safe path stays under the same `AGENT_CHAT_ONLY=1` gate that strips tools |

Both the fresh-run and the resumed-session turn honour this (on resume only the new message tail is
validated), so an image can arrive mid-conversation.
Camera tools that return an `ok` JSON result with `camera` and `dataUrl` are also lifted into an
image part automatically. The CLI receives the pixels, while its text prompt keeps the camera
summary without the base64 payload.

GPT-6 Luna/Sol require Codex CLI 0.156.1 or newer;
select them with `-e codex -m 6-luna` or `-e codex -m 6-sol` on separate ports.


**One process = one fixed engine/model/effort** for its whole lifetime. To compare models, run the
command again with different `-e/-m/-f/-p`. `codex` resume preserves its pinned model/effort
(`provider_codex_resume_cmd` re-sends `-m` and `model_reasoning_effort`) — fixing a real drift found
live, where a session started with `-m spark` silently ran under `gpt-5.5` on resume.

</details>

<details>
<summary><b>Motivating use case: a Unity benchmark on a CLI subscription</b></summary>

CoreAI's Unity Game-Creation Benchmark can point its live PlayMode test suite at any
OpenAI-compatible provider via env vars. One benchmark scenario is one ongoing conversation, so the
session-continuation model turns into real token/cache savings over the scenario's lifetime, not
just wire compatibility:

```bash
# terminal 1, from this repo:
agent.sh openai-server -e claude -m sonnet -f low -p 8801

# env for the Unity test run:
export COREAI_TEST_BASE_URL=http://127.0.0.1:8801/v1
export COREAI_TEST_MODEL=claude-sonnet-5
export COREAI_TEST_API_KEY=
```

To compare several models in one full benchmark run each, start one bridge per model on a
different port (`-p 8801/8802/…`, different `-e/-m/-f`) and point a separate run at each `/v1`.

</details>

---

## Adding a provider

Create `providers/<name>/provider.py` and `providers/<name>/provider.json`. The core lazily
loads providers; the GUI reads their JSON metadata. Keep `provider.sh` only if that provider
must also work with the retained legacy engine.

- `provider.json` supplies `label`, `models`, `efforts`, `default_model`, `default_effort`,
  `limits` and `supports_resume`, used by both the GUI and bridge.
- `provider.py` exports a `Provider` subclass of `neoxider_agents.providers.BaseProvider`.
  Set `engine` and `supports_resume`; implement `command(model, effort, cwd, session='',
  chat_only=False)` to return argv. Optional hooks are `resolve`, `prepare_resume`, `doctor`,
  `retry_reason` and `retry_delay`.

Prompts reach providers through a UTF-8 file on stdin, never command-line arguments. The core
owns hidden process launch, output filtering, watchdogs and control polling. Every provider
must use unattended flags because interactive permission prompts cannot be answered:

| Provider | Full-auto flag | Chat-only (bridge) |
|---|---|---|
| Codex | `--sandbox danger-full-access --skip-git-repo-check` (opt down: `AGENT_CODEX_SANDBOX`) | `--sandbox read-only --ignore-user-config` |
| Claude Code | `--dangerously-skip-permissions` (opt down: `AGENT_CLAUDE_PERMISSION`) | `--strict-mcp-config --tools ""` |
| Kimi Code | `-p` uses the built-in auto policy (`--auto` must not be combined with it) | `--agent-file chat-only-agent.md` |
| opencode | `--auto` | `--agent neoxider-chat-only` + `OPENCODE_CONFIG` |
| Gemini CLI | `--yolo` | `--approval-mode plan` (read tools remain) |

Codex's full-access default is the repository owner's explicit standing decision for Windows: its
sandbox helper rejected every write with `patch rejected: writing is blocked by read-only sandbox`
(and earlier `windows sandbox: helper_unknown_error`), so the agent could run commands and report a
fix but never apply it.

See `providers/codex/`, `providers/claude/` and `providers/kimi/` for worked examples (alias
resolution, effort suffixes, codex's rate-limit JSON parsing). The opencode provider keeps its JSON
stdout clean while forwarding prefixed stderr diagnostics and throttled activity heartbeats; its
runs have a 30-minute hard timeout (`AGENT_OPENCODE_TIMEOUT_SEC`, `0` disables).

---

## Troubleshooting: codex answers but every shell command hangs

If a codex task replies normally yet never finishes — no log growth, `state=running` for hours —
the cause is almost certainly `~/.codex/config.toml`. On a machine with the ChatGPT desktop app
installed, that file is owned by the app and declares a stdio MCP "code-mode host" (`node_repl`,
under `AppData\Local\OpenAI\Codex\runtimes\cua_node\…`) plus HTTP MCP servers that only answer
while the app is running. Launched from a plain shell, codex's tool router blocks on the first exec
call forever (`ERROR codex_core::tools::router: error=code-mode host closed its stdout`).

The wrapper therefore runs codex with `--ignore-user-config` by default (auth is unaffected — that
flag skips `config.toml` only). Confirm with `agent.sh doctor --deep`. To bring one MCP server
back: `AGENT_CODEX_MCP="unityMCP=http://127.0.0.1:8040/mcp"`. To restore the old behaviour
entirely (not recommended): `AGENT_CODEX_USER_CONFIG=1`.

Unrelated but often seen on the same machines: `codex doctor` itself may die with
`memory allocation of … bytes failed` on a stale models cache, and `~/.codex/` can accumulate
multi-megabyte orphaned `..codex-global-state.json.tmp-*` files. Neither affects agent.sh; this
tool never touches your `~/.codex/`.

---

## Development

```powershell
$env:AGENT_CLI_LOGS = 'D:/Temp/agents-core/test-state'
python -m unittest discover -s tests -p 'test_*.py'
powershell -NoProfile -ExecutionPolicy Bypass -File tests/test_powershell_entry.ps1
python tests/bench_agent.py
```

The Python core, GUI, bridge and launcher suites use the standard library, fake providers
and isolated state. Legacy Bash scenarios remain under `tests/test_agent_sh.sh`; run them
with the legacy switch. Planted quote/encoding defects prove that Windows entry checks detect
PowerShell 5.1 argument corruption. Performance evidence is in [docs/PERFORMANCE.md](docs/PERFORMANCE.md).

For a real end-to-end check against a live CLI subagent (health, error codes, auth, fresh
completion, session continuation, tool round-trip, divergence, `/reset`, idle expiry, streaming,
concurrency):

```bash
python tests/live_smoke_openai_server.py --engine codex   --model spark             --api-key k1
python tests/live_smoke_openai_server.py --engine claude  --model sonnet -f low     --api-key k1
python tests/live_smoke_openai_server.py --engine opencode --model opencode/big-pickle --api-key k1
```

That one costs real time and real usage against your subscription, so it is deliberately not part
of the automatic suites. Add `--no-native` to also cover the task-count assertions for `claude`
(which otherwise runs through its persistent process and writes no `agent.sh` task).

See [`tests/README.md`](tests/README.md).

---

## Roadmap

[`TODO.md`](TODO.md) for planned work (diff rendering, macOS support…) and
[`docs/IDEAS.md`](docs/IDEAS.md) for open design questions (subagents spawning subagents as a real
tree, ideas borrowed from CliDeck/agent-of-empires). Release history: [`CHANGELOG.md`](CHANGELOG.md).

## Author

[NeoXider](https://github.com/NeoXider) · [MIT](LICENSE)
