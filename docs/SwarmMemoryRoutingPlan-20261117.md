# Swarm mode, role routing and local memory (plan, 2026-11-17)

Status: phased design. Additive, opt-in, stdlib-only.
The P0 implementation includes the advisory `swarm plan` template and manual
route preview. Execution and native commands remain in P1.

Absorbs the contracts (not the code) of opencode-swarm, claude-flow/ruflo and
pi-subagents/dsh-subagents (all MIT): per-task dynamic planning, per-agent
effort economics, independent verification gates. This project already owns
the fleet primitives (launch/tracking, `fan`, ledger mode, `brief --owns`,
durable receipts, ownership baselines); the missing pieces are the role-graph
gates, a routing policy with auto/manual modes, and a local performance
memory. No upstream repo is merged; MIT lets us absorb the shapes cleanly.

## What ultracode-class orchestration actually adds

Plain delegation loses on four axes, and the plan keeps all four:

1. per-task dynamic planning (phases, parallel/pipeline fan-out) instead of
   one delegation call;
2. scale (dozens of fresh-context workers, not 1–5);
3. independent verification gates (fresh read-only reviewer, refutation pass,
   nothing ships until reviewer + verifier pass — coordinator runs the checks,
   worker claims are claims);
4. effort economics per role (cheap mechanical stages on the cheapest capable
   route, expensive judgment on the strongest), heterogeneous pairing by
   default: the verifier runs on a different provider than the implementer.

## 1. Role graph

Roles: planner, implementer (N), reviewer (fresh read-only context),
verifier/test (runs repository checks itself), skeptic (refutes findings).
The coordinator (the caller) remains the manager; role workers are ordinary
tracked `run`/`fan` tasks with declared ownership. One hard unsplit task stays
in ledger mode; a wave of independent pieces stays `fan`; swarm is the gates +
routing layer over both.

Commands (thin wrappers over existing mechanisms, no new launchers):

```text
swarm plan  <objective>            # role plan: tasks, owned files, routes (manual map)
swarm run   <objective>            # execute the plan via existing run/fan paths
swarm status [patterns]            # scoped table of the swarm's owned tree
swarm score                        # render the memory scoreboard
swarm mode auto|manual             # per-wave routing mode
```

Gates enforced via the existing contracts: `--owns`/`--strict-owns`
serialize shared files (disjoint source files alone do not make tasks
independent); reviewer/verifier get fresh contexts; a finding is confirmed
only by a refutation attempt that failed to refute it.

## 2. Routing (manual first, auto later)

**Manual (P0/P1).** A role→route table `swarm.map` next to the config (same
resolver chain as engine/model/effort keys). Ordered preferences per role:
`[engine/model/effort, ...]`. The catalog (`models --json`: capabilities,
limits, cost, reasoning variants) is the candidate pool, not proof — the
existing rule stays: catalogue presence proves neither capability nor quota.

**Auto (P2).** Picks only from `swarm.map ∩ memory-verified routes`: rules —
mechanical stages (search/read/format) → cheapest capable; judgment/verify →
strongest available; respect caps, rate-limit/health data from doctor, the
15-minute effort gate and the paid-agent permission rule. No silent
substitution on limits: the wave reports and waits. Auto starts advisory
(`swarm plan` shows picks + reason; execution stays human-gated) until each
candidate route has ≥5 coordinator-verified runs; then it may run autonomously
with `swarm mode auto`.

## 3. Local memory

Files in the state dir root (`AGENT_CLI_LOGS`, default `~/.claude/agent-cli-logs`),
written under the existing mkdir mutex; the `.meta`/`.jsonl` conventions apply:

- `swarm_memory.md` — append-only, human/agent-readable (the requested form);
- `swarm_memory.jsonl` — machine side for auto mode, same append-only rule.

One record per settled role worker: date, task, engine/model/effort, role,
final state/exit, file-delta verdict from the files, independent-gate outcome
(review pass/fail), reported tokens/cost (marked `reported`, since reported
usage is not confirmed billing), human overrides, short note. Only
coordinator-verified facts are appended — a worker never writes its own score.

Aggregation block at the top of the md: a scoreboard per route
(role × engine/model: successes/failures/watchdog-hits, typical duration,
"good for / bad for" lines, "never use for X" lines), plus a Trusted pairs
section that auto mode consumes. Manual mode shows the scoreboard as hints
only. Entries age out by evidence, not by time: a route drops from Trusted
after two coordinator-confirmed failures on its role, with the failure event
cited.

## 4. Trigger surface (`/ultra`)

One canonical verb, `ultra` (alias `swarm`), same name in every harness.

- **In-skill keyword (P0, prompt-text only):** a user message starting with
  `/ultra` or `ultra:` means "swarm mode on the rest as the objective". It
  works anywhere the agent reads this skill at session start — the SKILL gains
  a trigger section: load this skill, consult `swarm score` (memory), build
  `swarm plan`, present plan + routes + token estimate, execute after
  confirmation (advisory until the P2 autonomy rule allows it). Counts only
  in prompts the user types, same boundary as the existing keyword rules.
- **Native commands (P1)** where a command surface exists:
  - Claude Code plugin: `commands/ultra.md` (the plugin already ships the
    skill); body = "load skill neoxider-agents, follow its swarm protocol,
    the argument is the objective".
  - opencode: global command file `command/ultra.md` in the config dir, same
    three-line body, created by the same install step that writes the
    completion file.
  - Codex/Gemini custom prompts: feature-gated per installed version;
    otherwise the keyword fallback above is the entry point.
- Invocation: `/ultra <objective>`, optional `--route engine/model` and
  `--auto`; default routing comes from the manual map, auto only per section 2.

## 5. Reuse and boundaries

Reuse unchanged: tracked launches, durable messages/receipts, ownership
baselines, `fan` waves, ledger mode, `AGENT_PARENT` tree (swarm children
appear in the GUI via existing receipts), `result --json` as the memory feed,
provider-argv and prompt-file rules, Windows PowerShell entry / POSIX shim,
zero-dependency core. Memory lives in the state dir, not the project
(no new project files by default). Attribution limit stays honest: baseline
diffs cannot attribute concurrent authors, so swarm keeps single-owner tasks
and serialized shared files.

## 6. Phases

- **P0 — memory + manual map + trigger text.** Append pipeline from
  `result --json` and coordinator gate events; `swarm score`; `swarm.map`
  file; SKILL trigger section making `/ultra` / `ultra:` the canonical verb.
  No new execution semantics. Tests extend the existing suite patterns,
  including the concurrent-writer/mutex case.
- **P1 — plan/gates + native commands.** `swarm plan/run/status` over existing
  brief/fan/run; gates as contracts (fresh-context reviewer, refutation pass,
  reviewer+verifier before acceptance); manual routing only; `commands/ultra.md`
  in the plugin, `command/ultra.md` for opencode.
- **P2 — auto.** Auto routing from map ∩ memory + cost/capability fields,
  advisory by default, opt-in autonomous; per-wave budget warning before
  launch (multi-provider means per-provider prompt caches = more tokens for
  the same work — estimate shown, not silent).
- **P3 — optional.** Role-pack templates importing the proven shapes from
  opencode-swarm/pi-subagents (cheap-finder loop-until-dry + expensive
  skeptic; gated implement/review/test), memory scoreboard in the GUI panel.

## 7. Risks / open questions

- Auto mode anchoring on one lucky run — hence the whitelist-∩-memory rule
  and the ≥5-runs bar before autonomy.
- Cost columns stay `reported`; rankings remain quality-first because
  reported usage is not confirmed billing.
- Multi-provider cache fragmentation raises token spend; the budget warning
  must land before auto autonomy.
- Synthetic per-session swarm root: adopt the `-P`/parent-slug convention
  already left open in IDEAS.md; swarm waves should not invent a second scheme.
- The 15-minute effort gate applies per role worker; swarm mode is only for
  objectives that genuinely split — everything else stays a single worker or
  stays with the coordinator.

## Acceptance for P0

Memory files survive restart and concurrent appends (mutex-proven);
`swarm score` renders the scoreboard from the jsonl; SKILL.md + README
sections updated; tests green including the concurrent-writer case;
the `/ultra` keyword verified end-to-end on one harness (skill loaded, plan
shown before any worker launch).
