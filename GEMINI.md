# neoxider-agents — agent instructions (Gemini CLI)

This repo is `agent.sh` + `gui.py`/`gui.html`: a non-interactive wrapper for launching
and managing CLI coding subagents (Codex, Claude Code, opencode, Gemini CLI) across a
shared thread-per-task log, plus an optional zero-dependency local web GUI.

Gemini CLI doesn't read the cross-tool `AGENTS.md` convention (Codex CLI and opencode
do; Claude Code reads it as secondary context) — this file exists so Gemini picks up
the same baseline instructions. See [`SKILL.md`](SKILL.md) for the full command
reference, model-alias tables, the question-detection heuristic, and known trade-offs
— it's the canonical operating manual and takes precedence over this file if anything
here goes stale.

## Quick reference

```bash
SK=./agent.sh
bash $SK run  -t <name> -C <dir> "<prompt>"     # new task (codex/gpt-5.6-sol by default)
bash $SK run  -e claude -t <name> -C <dir> "..." # -e: codex|claude|opencode|gemini
bash $SK reply <name> "<answer>"                 # continue a task by name
bash $SK log  -f <name>                          # follow a task live
bash $SK status <name>                           # state / current step / needs a reply?
bash $SK doctor                                   # engines + codex rate limits, before a batch
bash $SK gui                                      # web GUI (stable default port 8765; or: ./bin/neoxider gui)
```

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
to a brand-new `agent.sh run` with the full history. Only `claude`/`codex` support this
resume (`opencode`/`gemini` always take the fresh-run path). Consequence: **one bridge
process serves one conversation at a time** — a lock serializes every request, so don't
point multiple unrelated tasks at the same port expecting independence (run one process
per port per conversation instead); `POST .../reset` clears the remembered session,
and `GET /health` reports `session_active`/`session_turns`. An idle session also
auto-expires after `--session-ttl` seconds (default 1800 = 30 min) — the next call
after that just starts fresh instead of resuming. Beyond that: latency is a
**full CLI subprocess invocation** (seconds to low minutes, not a token stream);
`stream: true` **replays an already-finished answer** as word-sized SSE chunks, it is
not real per-token streaming; `tools`/function-calling is **emulated via prompting**
(best-effort; the bridge accepts the call as EITHER a JSON `{"tool_calls":[...]}` block
OR literal `name(arg=value, ...)` lines — codex tends to write the latter — and the
prompt warns that prose describing an action is ignored; re-sent on every `tools` call);
`usage` token counts are **always
`0/0/0`**; and **`content` is a clean answer for every bundled engine** (`codex` would
otherwise mix its banner/session-id/error-log/"tokens used" chrome into the answer, so its
provider runs `codex exec --json` and extracts only the final agent message — this also
cleaned up `agent.sh last`/the GUI for codex). One process = one fixed engine/model/effort
— run it again on another port to compare models. **The wrapped CLI is locked to
text-only completion for bridge calls**: `AGENT_CHAT_ONLY=1` makes codex run
`--sandbox read-only --ignore-user-config` and claude run `--strict-mcp-config
--disallowedTools Bash,Edit,Write,NotebookEdit,Task,WebFetch,WebSearch`, so it can't
reach a real MCP server (verified live against a configured `unityMCP`) or write files
instead of answering in the expected format — a normal `agent.sh run` outside the
bridge is unaffected.

## Rules for using this tool as a subagent orchestrator

- Give every task a meaningful name via `-t` — the auto-generated default
  (`task-<timestamp>-<pid>`) is collision-safe but not descriptive.
- Always `reply` by task name (or session id) — never rely on "last task" when more
  than one subagent might be running, or you'll answer into the wrong session.
- Every provider runs fully unattended (no approval prompts — see `providers/*/provider.sh`
  and the "Adding a provider" section of [`README.md`](README.md) for the exact flag
  each one uses, e.g. Gemini's own `--yolo`). Do not remove those flags; a subagent's
  stdin is always closed, so a provider that blocks on a prompt hangs forever instead
  of failing loudly.
- Keep this file, `AGENTS.md`, and `SKILL.md` in sync when the tool's interface changes
  — they intentionally overlap so every CLI convention picks up the same instructions.
