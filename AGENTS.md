# neoxider-agents — agent instructions

This repo is `agent.sh` + `gui.py`/`gui.html`: a non-interactive wrapper for launching
and managing CLI coding subagents (Codex, Claude Code, opencode, Gemini CLI) across a
shared thread-per-task log, plus an optional zero-dependency local web GUI.

If you (an AI agent) are working *in* this repo, see [`SKILL.md`](SKILL.md) for the
full command reference, model-alias tables, the question-detection heuristic, and
known trade-offs — it's the canonical operating manual and takes precedence over this
file if anything here goes stale. This file exists so Codex CLI and opencode (which
read `AGENTS.md` natively) and Claude Code (which reads it as secondary context) all
pick up the same baseline instructions without extra setup.

## Quick reference

```bash
SK=./agent.sh
bash $SK run  -t <name> -C <dir> "<prompt>"     # new task (codex/gpt-5.5 by default)
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
before relying on it: every call is **stateless** (the caller's `messages` array is
serialized into one prompt and sent to a brand-new `agent.sh run` each time, nothing
resumed server-side); latency is a **full CLI subprocess invocation** (seconds to low
minutes, not a token stream); `stream: true` **replays an already-finished answer** as
word-sized SSE chunks, it is not real per-token streaming; `tools`/function-calling is
**emulated via prompting** (best-effort, can misformat); and `usage` token counts are
**always `0/0/0`**. One process = one fixed engine/model/effort — run it again on
another port to compare models.

## Rules for using this tool as a subagent orchestrator

- Give every task a meaningful name via `-t` — the auto-generated default
  (`task-<timestamp>-<pid>`) is collision-safe but not descriptive.
- Always `reply` by task name (or session id) — never rely on "last task" when more
  than one subagent might be running, or you'll answer into the wrong session.
- Every provider runs fully unattended (no approval prompts — see `providers/*/provider.sh`
  and the "Adding a provider" section of [`README.md`](README.md) for the exact flag
  each one uses). Do not remove those flags; a subagent's stdin is always closed, so a
  provider that blocks on a prompt hangs forever instead of failing loudly.
- Keep this file, `GEMINI.md`, and `SKILL.md` in sync when the tool's interface changes
  — they intentionally overlap so every CLI convention picks up the same instructions.
