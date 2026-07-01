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

## Concurrency safety when this tool is used from multiple places at once

The design is intentionally provider-agnostic and installation-agnostic: `agent.sh` /
`gui.py` don't care who invoked them — a human terminal, a Claude Code session, a
Codex/opencode/Gemini agent, or several of those at once, all sharing the same
`LOGDIR` (`~/.claude/agent-cli-logs` by default) so the GUI shows one unified tree
regardless of source. That's the right shared-interface design (there's no need for
per-provider silos — one dashboard is the whole point). But sharing one `LOGDIR`
across concurrent, uncoordinated processes has two real gaps, found while thinking
through "what if this skill is invoked from several agents/providers at the same
time":

1. **`meta_set`'s read-modify-write isn't atomic across processes.** It does
   `grep -v "^key=" file > file.tmp && mv file.tmp file` — if two processes update the
   *same* task's `.meta` around the same time (e.g. a GUI double-click firing two
   `reply` calls, or a background poller reading while a run is mid-write), one write
   can silently clobber the other's key (a classic read-modify-write race / lost
   update). In the common case — one task, one writer at a time, sequential steps
   within a single `run`/`reply` invocation — this never triggers. It's a real but
   narrow edge case, worth a proper fix (e.g. wrap the read-modify-write in `flock` on
   a per-task lock file) rather than a "someday" item, since it gets more likely the
   more concurrent installs/agents point at the same `LOGDIR`.
2. **Auto-generated task names can collide.** The default name is
   `task-$(date +%Y%m%d-%H%M%S)` — two processes (from the same or different
   tools/installs) starting within the same second get the *same* default name and
   then race on the same `.meta`/`.log` files. Fix: make the default name
   collision-resistant (append a short random suffix or the PID), or have `run`
   check-and-retry if the target `.meta` already exists.

Neither is implemented yet — noted here (rather than acted on immediately) because
`agent.sh` was mid-refactor (provider-plugin migration) when this was written; do it
as a small, focused follow-up once that refactor lands, to avoid two concurrent edits
to the same file colliding — which would be a delightfully on-the-nose demonstration
of exactly the problem described above.

The `AGENT_CLI_LOGS` env var already gives a clean escape hatch for anyone who *wants*
strict namespace isolation between installs (e.g. two people sharing a machine, or a
"personal" vs "CI" split) — worth calling out explicitly in the README once the
locking fix lands, rather than adding new namespacing machinery on top.
