# Ideas (open questions, not decided yet)

## Subagents spawning subagents as a real tree

`agent.sh` already supports `-P <parent>` (or `AGENT_PARENT` env var) to record a
task's parent for the GUI tree, and the GUI already renders nesting based on it. But
right now nothing actually *uses* it end-to-end, for two separate reasons:

1. **Subagent-calls-subagent.** A subagent (say, a codex worker) could itself have
   access to this skill (the path to `agent.sh`) and spawn its *own* subagents via
   `-P <its-own-task-name>`, forming a real multi-level tree instead of a flat list.
   This is genuinely useful for a worker that wants to fan out sub-tasks of its own.

2. **Chat-calls-subagent.** When *this conversation* (a regular Claude Code / Codex
   chat session, not a task launched by `agent.sh`) spawns a subagent, that subagent
   should show up in the tree as a child of "this session" — not just of another
   task. That needs a stable identifier for "the current chat session" to pass as
   `-P`, which doesn't exist today (there's no natural `$SESSION_ID` env var the shell
   can read). One option: have the *skill instructions* tell the agent to invent and
   remember one short slug per conversation (e.g. derived from the first task name it
   creates) and pass it via `-P`/`AGENT_PARENT` for every subsequent `run` in that
   conversation, so all of a session's subagents nest under one synthetic root.

Both are appealing but under-specified — in particular, whether a synthetic
per-session root should itself appear as a (fake) task in the tree, or just be a
grouping key with no task file of its own. Left as an open design question rather
than implemented; revisit once there's a concrete use case (e.g. a worker that
genuinely needs to fan out).

## macOS terminal launch

If/when `TODO.md`'s macOS item is picked up, the terminal checkbox's `spawn(...,
terminal=True)` needs a real "open a visible terminal window running this command"
branch for Mac — likely `open -a Terminal.app <script>` or similar — mirroring what
`CREATE_NEW_CONSOLE` does on Windows.
