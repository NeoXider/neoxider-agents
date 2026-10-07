# Orchestrator prompt & model cheat-sheet

A ready-to-paste prompt for running a session as an **orchestrator** that delegates work to CLI
subagents via `agent.sh` (neoxider), plus a matrix of which model fits which task.

For a plugin install use `SK="$CLAUDE_PLUGIN_ROOT/agent.sh"`; for a manual skill install use
`SK="$HOME/.claude/skills/neoxider-agents/agent.sh"` (or use the `neoxider` command).

---

## Paste-ready orchestrator prompt

> Own the requested outcome. Delegate independent, bounded tasks while doing useful work
> locally; implement small or tightly coupled changes yourself. Use native workers for your own
> engine and `agent.sh` only for foreign engines. Honor explicit model/effort/endpoint choices;
> do not silently substitute them. Check foreign CLI availability only when that route is needed.
>
> Give each worker an objective, owned files/resources, relevant context, acceptance checks and
> permitted side effects. Serialize shared files, installs, configuration, ports and restarts.
> Workers return changes, verification evidence, failed/unrun checks, blockers and active work;
> they do not commit, publish or restart shared services unless assigned that responsibility.
>
> Reuse exact task IDs. Use `peek` for activity, `send` for follow-ups, `stop` for interruption
> and `restart` for recovery instead of blind waits. Read logs before retrying and inspect
> partial side effects. Silence is
> not proof of a hang. Review every diff and verify the integrated result with required checks
> and the user's actual runtime path where applicable. Do not equate configured with working.
> Complete authorized integration/restart/release steps, preserve unrelated work and report
> remaining gaps honestly. Do not repeat passing checks without a reason or manufacture commits.
>
> On Windows keep background launches hidden at every relevant child/fallback boundary. Follow
> [runtime discipline](docs/RUNTIME-DISCIPLINE.md) for launch verification and shared-state changes.

Copy the block above as the system/first message when you want a model to run an orchestration session.

---

## Model matrix — which model for what

Pick the **cheapest model that will succeed among the engines that are actually available**.
Reasoning tokens dominate cost, so effort/model choice matters more than prompt wording.
The table below is a verified snapshot, not a prescription — check the relevant catalog only when needed (`agent.sh doctor`,
`opencode models`, or the DSH model picker), and never invent model ids. A user-named model
always wins.

> Claude-model entries below are for NON-Claude orchestrators (e.g. Codex driving the wrapper);
> from Claude Code spawn those tiers via the native Agent tool instead (NATIVE-FIRST rule above).

| Task type | First choice | Notes / alternatives |
|---|---|---|
| Trivial: rename, one-line fix, text/doc tweak, run tests | `-e codex -m spark` (`gpt-5.3-codex-spark`) or `-e claude -m haiku` | Cheapest. "не жалко" for test runs. |
| Regular coding / refactor / docs | `-e codex` (default `gpt-5.6-terra`, medium) **or** `-e claude -m sonnet` | Sonnet is a fine everyday default too; use it when Codex limits are tight. |
| Harder reasoning / tricky bug / careful refactor | `-e codex -m high` (`gpt-5.6-sol`, high effort) | Bump effort, not necessarily model. |
| Long-horizon coding / multimodal agent work | `-e kimi` (default Kimi K3) | Use Kimi Code after `kimi login`; `-m highspeed` selects the managed fast coding route. |
| Deepest / architecture / security review | `-e claude -m opus5`, or keep it yourself | Reserve current Opus 5 for genuinely hard work; bare `opus` is the legacy 4.8 alias. |
| 5.6 variant A/B or if `terra` is rate-limited | `-m sol` (`gpt-5.6-sol`) / `-m luna` (`gpt-5.6-luna`) | Alternative 5.6 models. **Observed speed (n=1): luna 41s < sol 56s < terra 105s.** In that same run only `sol` produced code whose own tests passed (luna/terra picked non-palindrome examples) — `terra` is the default per user preference, so still verify its output. |
| Remote free tier | `-e opencode -m free` (Muse Spark 1.3, с 03.09.2026) | `free`/`spark`/`muse`, `ox`/`alpha`, `pickle`, `hy3`, `mimo`, `nemotron`/`ultra`, `lightning` alias the OpenCode Zen free tier. Unranked — `free` is a user preference, not a measurement. `opencode models` shows the live list. |
| Local / offline | A configured local provider/model on the requested endpoint | Verify the exact route is local; a free remote tier is not an offline substitute. Serialize access to a shared model slot. |

**Engine quick facts (verified 2026-07-09):**
- **codex** — opt in with `-e codex`; the 5.6 family needs **codex-cli >= 0.144**. Watch usage limits (`agent.sh doctor`). Runs are launched with `--ignore-user-config`: `~/.codex/config.toml` (owned by the ChatGPT desktop app) hangs codex's tool router on the very first shell command. Need one of its MCP servers back → `AGENT_CODEX_MCP="unityMCP=http://127.0.0.1:8040/mcp"`.
- **claude** — default engine, `opus5` by default; `sonnet`, legacy `opus`, and `haiku` are explicit alternatives.
- **kimi** — `k3` by default (`kimi-code/k3`); `k3-256k`, `coding`, and `highspeed` are verified alternatives; supports named-task resume.
- **opencode** — works via `--auto`. Free-tier aliases resolve to real ids (`-m free`, `-m ox`, …); raw `provider/model` still passes through. `ollama` and `zai` are disabled in `~/.config/opencode/opencode.json` as of 2026-08-24 — Ollama's cloud DeepSeek 403s without a paid subscription, and the local one is 8B.
- **gemini** — needs `GEMINI_API_KEY` (Google sign-in is geo-blocked for some accounts); unavailable until a key is set.

**Token economy (already on by default):** `--terse` (concise output) and per-task `PROGRESS.<task>.md` are
on by default. Add `--no-terse` for exploratory work, `--no-progress` for throwaway one-shots. The
biggest lever is still model/effort — drop to `spark`/`haiku`/`-f low` for easy work.

---

## Watching a fan-out: what the states mean

A delegated step can never hang silently forever — `AGENT_TIMEOUT_SEC` (default 30 min) kills the whole
process tree and ends the task as `error`/`exit=124` with a `!! TIMEOUT …` line in the log. So when you
poll `agent.sh list` / `status <name>`:

| State | What it means | What you do |
|---|---|---|
| `running` (`▶`) | alive and producing output | `peek -f NAME`; `send NAME "…"` to queue a follow-up |
| `running (no output for Nm)` (`▷`, meta state `idle`) | alive but quiet; silence does not prove a hang | inspect `peek`/processes; keep waits bounded |
| `waiting` (`⏳`) | the agent asked a question | `agent.sh send NAME "…"` (`reply` also works) |
| `stalled` (`⚠`) | the process is gone (reboot/kill) | inspect edits, then `restart NAME` or `send --flush NAME` |
| `stopped` (`⏹`) | orchestrator stopped the process tree; session and edits remain | `agent.sh restart NAME "…"` |
| `error` + `⏱ killed by the step watchdog` | it hit the deadline | re-scope the task, or re-run with a bigger `AGENT_TIMEOUT_SEC` |

The CLI and the web panel compute this identically, so the two never contradict each other. When diagnosing foreign CLI execution on a new machine or after an upgrade, use
`agent.sh doctor --deep`: it makes each
engine actually execute a shell command, which is the only way to catch an engine that answers happily
while every command it runs hangs.

`send` to a running task returns `queued (#N)`; its wrapper resumes the same session with
ordered inbox messages after the turn and drains again before completing. Keep one tracked
`wait NAME` for completion: it includes those follow-up turns. `list`/`status` show queued
messages and last activity; `pending` also flags stopped tasks and undelivered inboxes.
If the wrapper died, `send --flush NAME` or `restart NAME` recovers queued messages.

Use `send NAME --now "…"` when the current approach must stop immediately. It interrupts the
process tree and resumes with your message plus the queue; inspect partial edits first.
`stop NAME...` is idempotent, retaining session/log/inbox/progress; process-tree termination
cannot guarantee graceful native cancellation. `stop --all-mine` uses `AGENT_PARENT`, falling
back to `AGENT_ORCHESTRATOR_ID`; missing ownership is refused. `restart NAME` defaults to an instruction to inspect
`git status/diff` and continue unfinished work; `--fresh` starts the original prompt as a new
session with the stored engine/model/effort/directory. For Gemini (`supports_resume=false`),
running follow-ups are refused; use a fresh run after stopping. Full syntax is in
[SKILL.md](SKILL.md#control-foreign-workers-like-native-subagents).

---

## Ledger mode (single hard task, not a fan-out)

`fan` is for splittable work. For ONE hard problem (tricky algorithm, multi-cycle bugfix — where a
single-shot answer drowns in reasoning), use ledger mode instead: `.ledger/<task>/` with
`task.md` (read-only) + `plan.md` + `tasks.json` + append-only `notes.md` + `solution.py`.
You are the manager: brainstorm worker → up to 10 single-task fresh-context rounds → YOU run the
public sample tests each round (`test: input/expected/got` into `notes.md`) → `verdict.md` or stop
after 2 stagnant rounds. Stubs in `ledger/`, full protocol in `SKILL.md` ("Ledger mode").
