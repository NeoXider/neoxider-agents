# Swarm memory and manual routing

P0 adds local memory and an advisory plan to the existing tracked CLI. The
[skill's ultra trigger](../SKILL.md#ultra--swarm-mode) applies to an explicit user
prompt or to objectives with three independent pieces of at least 15 minutes each,
or a need for independent review or verification across providers. Consult memory,
show the plan, routes and estimate before any launch, and confirm the plan and any
paid routes with the user. Launch approved work with existing `run`/`fan`, review
and verify in fresh contexts, and record the actual outcomes. Use a different
verifier provider from the implementer when available.

## Plan and routes

```text
neoxider swarm map show
neoxider swarm map set implementer codex/sol/medium,opencode/opencode/big-pickle/default
neoxider swarm map unset implementer
neoxider swarm plan "Split the release checks by subsystem"
neoxider swarm plan "Split the release checks by subsystem" --route codex/spark --json
```

`swarm.map` is a JSON object beside the file selected by the existing config
resolver. `AGENT_CONFIG` wins, followed by `%APPDATA%/neoxider-agents` on Windows,
then `$XDG_CONFIG_HOME/neoxider-agents` or `~/.config/neoxider-agents`. Values are
ordered lists of `ENGINE/MODEL/EFFORT`. The first component is the engine, the
last is effort, and everything between is the model ID. `default` in the effort
position delegates effort to the provider resolver. `map show` prints JSON.

The plan selects the first preference for each mapped role. Other entries stay
visible in JSON for coordinator decisions. Unmapped roles use the CLI default
resolver, including config and environment precedence. `--route ENGINE/MODEL`
previews an explicit route for all roles and follows normal CLI effort resolution.
Provider alias resolution is local. Availability, quota and pricing require
separate evidence. The template contains planner, implementer × N, reviewer,
verifier and skeptic. The coordinator supplies N, the task split and any provider
changes needed for independent gates. Estimate assumptions are printed with the
token total formula. Dollar cost is unknown until route rates and scope are known.
Plan and score launch no tasks or provider probes.

## Record and score

```text
neoxider swarm record release-build --role implementer --verdict pass --gate review:pass --gate verify:pass --note "Good for: isolated build fixes"
neoxider swarm record release-verify --role verifier --verdict fail --gate verify:fail --override "Used the second approved route" --note "Bad for: cold Windows builds"
neoxider swarm score
neoxider swarm score --json
```

The coordinator supplies the file-delta verdict and at least one independent
gate outcome after inspecting the retained task result. Gates accept `pass`,
`fail` and `n/a`. The omitted gate defaults to `n/a`. A verdict or gate failure
counts as one confirmed failure. A passing verdict with no failed gate counts as
one verified pass. The scoreboard counts watchdog hits separately and takes the
median of known durations. Task exit and provider output never imply a gate
pass. Reported token and dollar values are provider evidence, separate from
billing. Put `Good for: …`, `Bad for: …` or `Never use for: …` on separate note
lines for scoreboard hints. These annotations have advisory value only.

Trusted pairs are role × engine/model/effort with at least five verified passes
and fewer than two confirmed failures over retained history. Set
`AGENT_SWARM_TRUST_MIN_PASSES` to a positive integer to change the pass threshold.
The scoreboard cites the task, date and generation for each confirmed failure.
Memory is local to the state directory across projects that share it.

The default state root is `~/.claude/agent-cli-logs`, overridable with
`AGENT_CLI_LOGS`. `swarm_memory.jsonl` is the append-only source, and
`swarm_memory.md` is regenerated atomically after each record with the scoreboard
above the chronological log. Treat both files as generated evidence. Identical
retries for a task generation and role keep one record and can refresh Markdown
after a failed projection write. A different attestation for that same identity
is rejected. A subsequent append backs up and repairs any incomplete final JSONL
fragment. Keep notes compact and omit secrets. The CLI checks `AGENT_TASK` to
reject a worker's own record within the trusted local state model.

See the [phased design](SwarmMemoryRoutingPlan-20261117.md) for native trigger
commands, execution and routing work beyond P0.
