# Ultra workflow specification (P1)

Status: proposed · 2026-11-18

Ultra runs a saved orchestration script over tracked neoxider CLI workers: Codex,
Claude Code, OpenCode, Kimi Code and Gemini. The coordinator owns the plan,
dispatch, evidence and final decision. The script owns branching and intermediate
results. Each worker starts with a bounded brief and a fresh CLI session.
Workflow replay therefore also works for Gemini, whose CLI session cannot resume.

## Baseline and scope

- Claude Code Workflow is the behavioral reference. A saved JavaScript program
  uses `agent()` for one schema-constrained result, `parallel()` for a join,
  `pipeline()` for per-item stages and `phase()` for progress. Independent agents
  can try to refute each candidate. Restarting can replay saved results, with
  suffix invalidation after a changed prompt or failed call. Human prompt
  opt-in and session-level Ultracode are distinct. The advisory sizes are
  fewer than 5/10/50 agents, or unrestricted. Concurrency and large-run warnings
  are separate controls. [Claude Code Workflow](https://code.claude.com/docs/en/workflows)
- Reuse `AgentClient.spawn()`, `wait_any()`, `attach()`, typed results and
  `subscribe(callback, consumer=...)`. The CLI retains task names, generation
  identities, receipts, recovery reports and resource ownership.
  [SDK](ORCHESTRATOR_API.md) · [Core integration](CORE_INTEGRATION.md) · [Events](DURABLE_EVENTS.md)
- `fan` supplies the independent-wave and OpenCode startup-barrier patterns.
  The manual ledger supplies fresh contexts, bounded rounds and coordinator-run
  tests for a single evolving problem. Ultra exposes these patterns as script
  control flow with per-call routes and persisted results. Ordinary skill
  routing stays in place outside Ultra. Within Ultra every worker uses the CLI,
  including workers with the coordinator's own engine. [Orchestrator skill](../SKILL.md)
- P0 coupling is through its public routing and memory interfaces. The requested
  [P0 plan](SwarmMemoryRoutingPlan-20261117.md) was absent from the committed
  target and P0 refs during design. The adapter below assumes the requested roles
  and `swarm record` behavior. Confirm its exact interface against landed P0.

## Script and commands

Choose **Python 3.8+**, the same runtime as the CLI and SDK. A single trusted
UTF-8 `.py` file exports a literal `PLAN` and `async def main(ctx)`. Python
loops, conditionals and JSON data express dynamic fan-out directly. One language
lets helpers use the typed SDK and standard library. A YAML graph would require
a second expression language for dynamic verification. Prompt-only plans would
leave dispatch and replay to model interpretation.

```text
neoxider ultra plan SCRIPT.py [--max-workers N] [--concurrency N]
neoxider ultra run SCRIPT.py [--max-workers N] [--concurrency N]
neoxider ultra run SCRIPT.py --resume RUN_ID
neoxider ultra status [RUN_ID] [--json]
```

`plan` parses the literal with `ast` and bounded `ast.literal_eval`, validates
the declared envelope, resolves routes and prints the plan without importing the
script or invoking a provider. `run` always shows that plan first. Interactive
runs confirm it. Headless runs require a plan digest from `plan` and an explicit
`--approve-plan SHA256`. The digest covers script bytes, workspace, declared
inputs, planned routes and limits. If the digest changes, show the updated plan.
Run the approved bytes. Scripts are local executable code with coordinator
permissions. Use reviewed local scripts and ordinary Python imports. Generated
scripts should put effects behind managed calls, and preview only reads metadata.
The workflow API controls its own dispatch. It offers no sandbox for Python or
ordinary CLI workers.

The plan declares a maximum call count and a route envelope for each role.
Resolve configuration and admissible alternates ahead of approval. Every
dispatch must fit its declared role, ownership, effort and route envelope.
Hold out-of-envelope calls until the coordinator presents a revised plan.
Save the approved plan digest and a redacted route snapshot in the manifest.

| Interface | Semantics |
| --- | --- |
| `await ctx.agent(prompt, **options)` | Schedule one logical call. Return a typed outcome with `ok`, `value`, `task`, resolved `route`, `failure` and cache provenance. With a schema, `value` is parsed JSON. Otherwise it is final text. |
| `await ctx.parallel(thunks)` | Schedule no-argument async callables, wait for all and return positional results in input order. Provider failures occupy their own slots. |
| `await ctx.pipeline(items, *stages)` | Each async stage receives `(previous, original_item, index)`. The first receives the item as `previous`. An item advances as soon as its prior stage finishes. Return the final value per item in input order. A typed failure stops that item's downstream stages. |
| `with ctx.phase(name)` | Set the default progress group for calls created in the block. Capture at submission using `contextvars`; an explicit call phase overrides it. |
| `await ctx.verify(candidate, reviewer=..., prompt=..., schema=..., check=..., **options)` | Use a fresh verifier, preferring an eligible different provider. Run the coordinator callback against the candidate and verdict. Return a typed finding with `candidate`, `status`, and evidence references. |

Use one scheduler with a bounded ready queue and one SDK coordinator thread that
owns launch and observation. Implement combinators with `asyncio`, using one
worker-slot pool for actual provider attempts. Holding a pipeline or parallel
group never consumes a worker slot. Maintain input order on return, independent
of settlement order. Preserve siblings on a typed call failure. On a script
exception, stop scheduling, settle or safely stop owned tasks and retain the
failure and partial results. SDK timeouts used for observation leave workers
running. Preserve the existing OpenCode first-real-activity startup barrier for
concurrent launches into one shared store. If startup fails or times out,
hold queued launches for that store and retain already running tasks.
[Fan implementation](../neoxider_agents/fan.py)

### Call options

| Option | Contract |
| --- | --- |
| `role`, `engine`, `model`, `effort` | Role references a plan entry. Resolve explicit call overrides first, then compatible `swarm.map` role fields and ranked routes, then the existing CLI resolver. When the engine changes, discard model/effort defaults tied to the old engine. Freeze provider identity, endpoint identity, canonical model, requested effort and applicable runtime profile before launch. Record the observed route too. |
| `label`, `phase` | Stable, unique label per logical call, including indexed dynamic items. Phase defaults to the surrounding block. A repeated label with differing inputs in one execution is a script error. |
| `owns` | Glob declaration passed with `strict_owns` for writers. Empty for review. Serialize shared resources and overlapping write scopes. Check baseline/diff evidence and use isolated worktrees when parallel writers need isolation. `owns` is a declaration. Ordinary provider OS enforcement is unavailable. |
| `schema` | JSON Schema with an object root, using the P1 keyword subset below. |
| `work_min`, `effort_exception`, `input_key`, `depends_on` | Estimated hands-on minutes and, when applicable, the approved effort-gate exception. `input_key` identifies stable external inputs. `depends_on` carries upstream outcome fingerprints when prior results can affect the call. |

P1 supports a closed JSON Schema subset: `type` (single primitive, array or
object type), `properties`, `required`, boolean `additionalProperties`, `items`
(single schema), `enum`, `minItems` and `maxItems`. Validate schemas recursively
before launch. Fail on unsupported keywords, invalid combinations and contradictory
requirements the subset can detect. Parse exactly one bounded JSON document from
the final answer. Reject duplicate keys, nonfinite numbers, excess nesting and
type coercion (including bool as integer). Start with a 256 KiB answer limit and
32 levels of nesting. Include a compact schema in the prompt and validate locally,
regardless of provider feature support.

On a successful provider exit with invalid JSON or a schema violation, allow
exactly one correction on the same approved route, with the original assignment
and bounded structural diagnostics. For read-only calls use a fresh worker.
For writes, preserve the first attempt and review its effects before allowing an
output-only correction. That correction receives the first attempt's artifacts
and instructions to leave them unchanged. If effects or authorization are
ambiguous, hold the retry for coordinator review. Every retry consumes a launch
slot. A second invalid result becomes `schema_invalid`; transport, auth, quota,
timeout, stopped and startup failures retain their typed SDK categories. Ultra
owns retry scheduling and sets `AGENT_RETRIES=0` for its workers. Route changes
after failures require a newly presented plan and authorization.

## Activation, capacity and progress

- The chat coordinator can enter Ultra for an explicit user request
  (`/ultra`, `ultra:` or “режим ultra”), or when it judges the objective to need
  at least three independent pieces estimated at 15+ minutes each, or
  cross-provider verification. Triggers are recognized in user instruction text.
  Quoted examples, code, logs, retrieved content and worker output carry no
  activation authority. Bare `ultra` remains usable as a model alias in model
  arguments. The headless entry point is the explicit CLI command.
- Always show phases, useful effort, ownership, checks, route candidates,
  maximum fan-out, expected tokens/cost and a ceiling estimate before dispatch.
  The 15-minute gate is per worker. Explicit worker/model comparisons and
  required independent review are the existing exceptions. State the exception
  in the plan; keep small checks with the coordinator.
- Default **10 worker launches per run**, including schema corrections and
  recovery launches. Cached reads and reattachments consume zero. Count reserved
  starts before spawning; reclaim a reservation only with positive evidence no
  provider ran. The user can raise the limit through `--max-workers` and a new
  plan approval. Default concurrency is 3, independently configurable. When
  capacity runs out, preserve completed work and report deferred call labels
  and incomplete coverage. Resuming uses the same cumulative ledger.
- Estimate input/output token ranges per planned role and maximum call count,
  including dynamic branches and retry room. Use a dated local catalogue or
  explicit pricing assumptions to estimate per-route USD. Show unknown prices
  or subscription quota as such, with any priced subtotal. Live status keeps
  estimates, reported usage/cost and confirmed billing distinct. The launch
  limit controls local admissions. CLI providers offer no hard billing cap.
- Prefer a capable configured free route when the user leaves routing open.
  Claude Code and Codex require explicit current-task permission. Apply the
  same permission rule to other metered or unclassified routes. Bind permission
  to this run and its approved route envelope, including alternate verifier
  routes and retry allowance. Activation, saved plans, installed credentials
  and old approvals do not confer paid permission. A noninteractive invocation
  supplies approved paid routes explicitly with `--allow-paid-route ROUTE`.
- Give each run a stable parent `ultra-<id>` and pass it as SDK parent
  (`AGENT_PARENT` for launches). P1a workers are queryable by parent in Fleet.
  P1b adds a run node with phase groups and joins by exact parent ID. Status
  reports queued/running/completed/cached/failed/deferred counts, attempt names,
  live routes, budgets, and verification coverage. Use a named event consumer
  with the same run parent on reconnect. Deduplicate by event ID and publish
  projections before acknowledging. Read results as evidence, with event replay
  serving progress. [Fleet UI](FLEET_UI.md)

## Durable workflow resume

Keep a versioned run manifest and append-only attempt/checkpoint records in
`<AGENT_CLI_LOGS>/ultra/<run-id>/`. Reuse the state lock and durable journal
conventions. Derive an attempt task name from run ID, call key and attempt
number. One coordinator holds the run writer lock with checked stale-owner
recovery. Reserve the attempt, task name and launch slot durably before dispatch.
Commit validated results and evidence references before returning them to the
script. Use bounded JSON, private state, fsync and atomic manifest replacement.
Keep raw prompts and transcripts in the existing task store. Read-only status
can inspect a stale run without starting work.

```text
call_key = SHA256(canonical_json({
  version, complete_dispatch_prompt, normalized_call_options, resolved_route,
  workspace_identity, input_key, dependency_fingerprints
}))
```

Canonical options include label, phase, schema, ownership, runtime behavior and
effort. Hash exact final prompt bytes with deterministic JSON encoding. Pin
workspace identity and a source/artifact fingerprint so an unchanged prompt on
changed inputs cannot reuse stale evidence. For repository review pass immutable
Git object IDs and scope. Other scripts declare a manifest of inputs; when the
runtime cannot establish their identity, hold reuse for review. Fingerprint each
upstream outcome over its call key, validated value and relevant artifacts.
Keep cache entries run-local. A script edit needs fresh plan approval, but an
independent call whose key remains unchanged returns the recorded value.
Changed dependencies invalidate dependent calls regardless of dispatch order.

`--resume` displays the remaining plan and estimated spend before any new
launch. A resume may update limits through the same worker/concurrency flags.
Replay main with the approved inputs and consult the journal at
each call. Completed schema-valid calls with matching inputs return cached
results even after a coordinator crash. For a writer, also verify recorded
postconditions and artifact hashes before returning the cached result. Never
reapply changes to restore a cache hit. Changed or ambiguous workspace/effect
state pauses for review. Use deterministic labels and explicitly include
time-varying or external inputs in `input_key`; keep external effects outside
replayed orchestration code unless managed through an audited checkpoint.

| When the previous coordinator exited | On resume |
| --- | --- |
| Result checkpoint committed | Reuse the result and mark a cache hit. Rerun read-only local checks when their verification evidence is missing or stale. |
| Known live worker or retained terminal result | `attach()` using the same parent. Check the recorded task identity and generation. Observe the live attempt, or reconstruct and validate its terminal result before committing the checkpoint. |
| Launch reserved, publication ambiguous | Retain the slot and reconcile owner, task/event evidence and effects. Hold a fresh launch until the previous launch is positively resolved. |
| Failed, stopped or unknown effects | Surface the failure and partial work. Resume a native session only through reviewed core recovery. Schedule a fresh attempt only after the coordinator confirms safety and authorization and the budget allows it. |
| Corrupt/incompatible workflow record | Fail closed, preserve the journal and report the affected run. |

Worker result reuse is separate from task event ACK, provider-session resume and
effect reconciliation. Cache replay alone supplies no exactly-once external
effects. On normal cancellation stop current owned workers via the SDK, wait for
settlement, retain results and identify any surviving workers. Crash recovery
only reattaches workers that still exist. [Core recovery](RECOVERY.md) · [Event contract](DURABLE_EVENTS.md)

## Verification and refutation

A producer, reviewer and verifier use separate fresh contexts. Pass the
reviewed source identity, candidate claim and source locations to the verifier,
without the reviewer's conversation or confidence. Instruct it to actively
try to refute the claim with counterevidence or an alternate explanation, and
require affirmative evidence for confirmation. Choose an approved different
underlying provider when available, using P0 or explicitly configured route
metadata. A different CLI alone is insufficient. If only an eligible same
provider is available, disclose that and keep the session fresh. If diversity
was required, mark the finding unverified.

`verify()` accepts only a schema-valid `confirmed`, `refuted` or `unverified`
verdict with evidence. The coordinator uses independent source inspection and
owns the relevant tests, lint/build checks, diff checks or read-only evidence
checks, selected before dispatch. Attach typed verification receipts with
command, exit status and final source hash where supported. A positive or
negative verdict needs corroboration from the applicable coordinator check.
A failed/missing check, model disagreement without decisive evidence, quota
failure or skipped verification leaves the candidate unverified. Preserve those
slots and review failures in coverage. For edits, integrate only when the
review and final-source checks pass. Provider exit zero and schema validity
only establish result transport and shape. A clean summary requires full
planned coverage.

## Worked example

Save as `review_diff.py` and preview/run against a clean Git checkout. The
three reviewers examine distinct aspects of `HEAD^..HEAD`; each returns its
highest-impact candidate, if any. Verification starts per finding as soon as
its review finishes. The verifier investigates the contract and cites decisive
source evidence. The coordinator independently checks the candidate's added
line and the verifier's evidence line at pinned revisions. Claims
requiring runtime proof remain unverified in this script.

```python
from functools import partial
import json

PLAN = {
    "title": "Review the latest Git commit",
    "inputs": ["git:HEAD", "git:HEAD^"],
    "phases": ["Review", "Refute"],
    "roles": {
        "reviewer": {"max_calls": 3, "work_min": 20},
        "verifier": {"max_calls": 3, "work_min": 15, "different_provider": "prefer"},
    },
    "max_corrections": 4,
    "checks": ["Match candidate and verifier evidence against pinned Git objects"],
    "owns": [],
}

TEXT = {"type": "string"}


def object_schema(properties):
    return {
        "type": "object", "properties": properties,
        "required": list(properties), "additionalProperties": False,
    }


FINDING = object_schema({
    "path": TEXT, "line": {"type": "integer"}, "source_line": TEXT,
    "claim": TEXT,
})
REVIEW = object_schema({"findings": {
    "type": "array", "items": FINDING, "maxItems": 1,
}})
VERDICT = object_schema({
    "verdict": {"type": "string", "enum": ["confirmed", "refuted", "unverified"]},
    "evidence": TEXT, "anchor": object_schema({
        "revision": {"type": "string", "enum": ["base", "head"]},
        "path": TEXT, "line": {"type": "integer"}, "source_line": TEXT,
    }),
})


async def main(ctx):
    head = ctx.inputs["git:HEAD"]
    base = ctx.inputs["git:HEAD^"]
    scope = f"Inspect Git objects in {base}..{head}, using those exact revisions."
    dimensions = ["state transitions", "resource lifecycle", "CLI compatibility"]

    async def review(dimension, item, index):
        with ctx.phase("Review"):
            return await ctx.agent(
                f"{scope} Review {dimension}. Do not write files. "
                "Return at most one concrete regression supported by an added "
                "line. Include its repo-relative path, 1-based line number, "
                "exact source_line without newline, and the violated contract.",
                role="reviewer", label=f"review-{index}", owns=(),
                work_min=20, schema=REVIEW, input_key=[base, head],
            )

    async def refute(reviewed, dimension, index):
        def source_check(candidate, verdict):
            added = ctx.git_added_line(base, head, candidate["path"], candidate["line"])
            anchor = verdict["anchor"]
            revision = {"base": base, "head": head}[anchor["revision"]]
            evidence = ctx.git_line(revision, anchor["path"], anchor["line"])
            return {
                "ok": added == candidate["source_line"]
                and evidence == anchor["source_line"],
                "revision": head, "check": "pinned-source-lines",
            }

        with ctx.phase("Refute"):
            jobs = [
                partial(
                    ctx.verify, candidate, reviewer=reviewed,
                    prompt=f"{scope} Try to disprove this static regression: "
                    f"{json.dumps(candidate, sort_keys=True)}. Cite the governing "
                    "contract and counterevidence. Provide an exact source anchor "
                    "at base or head that establishes the verdict. If it requires "
                    "runtime proof or evidence is insufficient, return unverified.",
                    schema=VERDICT, check=source_check, role="verifier",
                    owns=(), label=f"refute-{index}-{number}", work_min=15,
                    input_key=[base, head], depends_on=(reviewed,),
                )
                for number, candidate in enumerate(reviewed.value["findings"])
            ]
            return await ctx.parallel(jobs)

    rows = await ctx.pipeline(dimensions, review, refute)
    findings = [finding for row in rows if isinstance(row, list) for finding in row]
    return {
        "confirmed": [item.candidate for item in findings if item.status == "confirmed"],
        "refuted": [item.candidate for item in findings if item.status == "refuted"],
        "unverified": [item.candidate for item in findings if item.status == "unverified"],
        "incomplete_dimensions": [
            dimensions[index] for index, row in enumerate(rows)
            if not isinstance(row, list)
            or any(item.status == "unverified" for item in row)
        ],
    }
```

For this script, the planner resolves the two supported `git:<ref>` inputs to
full commit IDs before approval. `ctx.git_line()` reads a pin's exact blob line.
`ctx.git_added_line()` additionally requires the line to be in a new-side diff
hunk. Validate paths and 1-based line numbers. Read Git objects and a
zero-context diff with fixed argv and literal pathspecs.
Missing files and invalid coordinates cannot pass. Record the checked revision
and provenance with the verdict. The callback checks source evidence for both
dispositions. Verifier failure, refused launch or unverifiable evidence yields
`unverified`; the report keeps the corresponding coverage gap. Real test-
backed workflows use a purpose-built coordinator check at that same seam.

## P0 integration and delivery

Resolve P0 roles through a thin adapter. Freeze selected routes and memory
references for the approved run. P1a can run with explicit fully resolved
routes and report that P0 is unavailable. In P1b invoke the public `swarm record`
path after the coordinator verifies the final outcome. Send minimal
task/role/route, evidence reference and source identity, plus measured results
where the landed interface accepts them. Record no transient, unverified or
schema-invalid outcome as a success. Key submissions by run, outcome and
evidence digest. If P0 accepts an idempotency ID, reuse it on retry. Otherwise
persist intent and reconcile an ambiguous submission before a manual retry.
A recording failure is visible in status and leaves the verified result
available. Raw prompts, transcripts and credentials stay out of routing memory.

| Delivery | Source gate |
| --- | --- |
| **P1a** | Ship trusted Python runner, plan/approval, explicit or CLI-default routes with paid admission, read-only declarative briefs, scheduler, schema correction, bounded launches, durable replay/reconciliation, CLI status and SDK parent grouping. Ship the example and coordinator verification. Allow a configured alternate with explicit provider identity. |
| **P1b** | Integrate landed P0 mapping/recording and use it to select eligible alternate providers. Add write workflows with artifact postconditions and reviewed recovery, GUI run/phase tree, skill activation and operator docs. Validate the actual P0 contract before wiring to it. |

Use `unittest`, temporary directories and the existing offline fake-provider
fixture path, exercising normal CLI/SDK launch and result collection. Run on
Windows and POSIX. Required checks:

1. Plan preview never executes top-level script code or launches providers.
   Exercise exact triggers, plan digest mismatch, effort exceptions,
   unapproved paid routes, worker-cap accounting and budget reporting.
2. Resume unchanged calls with zero new invocations; invalidate changed prompts,
   options, declared inputs and dependencies while reusing independent siblings.
   Cover crash before launch, after spawn publication and after settlement before
   checkpoint, conflicting coordinators, corrupt journal and live reattach.
3. Validate one malformed result followed by success, two malformed results,
   unsupported schema, invalid JSON bounds and no correction after transport
   failure. Verify fresh contexts and paid/launch accounting on retries.
4. Use fixture barriers to prove concurrency and input ordering, early per-item
   pipeline advancement, no nested-group deadlock, the OpenCode startup barrier,
   partial failure, cancellation and observation timeouts.
5. Exercise cross-provider selection and same-provider disclosure, refutation,
   missing or stale verification evidence and final report coverage. Check CLI
   and GUI parent/generation binding, resume event deduplication and P0 receipt
   behavior, including an interrupted memory submission.

These tests establish local orchestration behavior. Live route validation is a
separate, specifically authorized check per CLI route.

## Comparison

| Project | Plan and orchestration | Gates and routing/cost | State/failure and P1 lesson |
| --- | --- | --- | --- |
| Claude Code Workflow (commercial) | Saved JavaScript, native agents, joins and per-item stages | Schemas, independent adversarial review, per-stage model; advisory size and token visibility | Same-session result replay and conditional quota pause; adopt script flow and refutation, use dependency-keyed replay over CLI workers. [Docs](https://code.claude.com/docs/en/workflows) |
| opencode-swarm (MIT) | Architect-led hub with persisted phase plans and isolated waves | Critic, reviewer and tests; role mappings and route receipts | Disk evidence and bounded retries/escalation; adopt review receipts and planning gates, leave the OpenCode plugin stack out. [Architecture](https://github.com/ZaxbyHub/opencode-swarm/blob/main/docs/architecture.md) |
| Claude Flow / Ruflo (MIT) | Prompt-driven queen/swarm, declarative MCP workflows and native Claude JavaScript | Approval gates and verified-outcome ingestion; adaptive routing and cost telemetry | Persisted workflow lifecycle and task retry; adopt verified-only feedback and explicit state, leave consensus, neural routing and MCP dependencies out. [Workflow plugin](https://github.com/ruvnet/ruflo/tree/main/plugins/ruflo-workflows) · [Releases](https://github.com/ruvnet/ruflo/releases) |
| Pi Subagents, tintinweb (MIT) | Claude-shaped JavaScript; subprocess-based Pi workers | Schema/shell gates, per-call model and effort, usage per attempt | Journal prefix replay, typed/displayed failure and resume controls; adopt provenance and stable labels, leave the JS host and session continuation out. [Workflows](https://github.com/tintinweb/pi-subagents/blob/master/docs/workflows.md) |
| DSH Subagents, geekyfoxlab (MIT) | Prompt/Markdown personas and parent-led loops over native DSH tools; a port of nicobailon/pi-subagents | Fresh parallel reviewers, merge verdict, parent checks; role/tool guidance, host model selection | Three-round review cap by default; plugin docs establish no call-key checkpoint or cost ledger. Adopt stop conditions and bounded handoffs, leave forked context and host coupling out. [Repo](https://github.com/geekyfoxlab/dsh-subagents) |
| LangGraph (MIT) | Python/JS state graphs and conditional edges; applicable to coding harnesses | Checks and human interrupts are graph steps; model/cost policy supplied by the application | Persistent checkpointers, retry policies and recovery; adopt explicit replay boundaries, leave graph engine and dependencies out. [Docs](https://docs.langchain.com/oss/python/langgraph/persistence) |
| OpenHands Software Agent SDK (MIT) | Python agent/conversation composition with subagent delegation; Markdown role definitions | Tool confirmation and caller-driven tests; LLM configuration and token/cost metrics | Persisted conversation/event state, pause/resume and surfaced runtime errors; adopt source-bound evidence and usage reporting, leave the separate agent runtime out. [SDK](https://github.com/OpenHands/software-agent-sdk) · [Paper](https://arxiv.org/abs/2511.03690) |

See [research notes](ULTRA_RESEARCH_NOTES.md) for source and license details.

## Open questions

- Which public P0 fields identify the underlying provider, a route's price
  class and an idempotent memory receipt? Confirm against landed P0.
- Which existing state-retention command should own removal of Ultra journals
  and results? P1 preserves active, unresolved and resumable runs.
- Is three the right default for concurrent CLI processes across typical Windows
  workstations? Keep the launch count independent and measure with the fixture.
