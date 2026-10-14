# Native parity final report — DRAFT — 2026-10-14

**DRAFT: pending Root security, final regression, live-provider/browser and remote
gates.** This records current integrated source and bounded evidence. It does not
declare the full plan accepted, Windows enforcement supported, or publication done.
The worktree remains under concurrent implementation, particularly Windows enforcement.
Historical receipts bind their tested bytes; later changes reopen affected gates.

Base: `origin/main ab3dd6cb07da101bf4f0bee2feca5e64814599b0` as recorded by Root.
This documentation task did not fetch, commit, push, change credentials/network,
launch models/subagents, or operate Unity. Root owns PLAN and release decisions;
security owners retain their [audit](SECURITY_AUDIT.md) and
[enforcement contract](ENFORCED_SCOPE.md).

## Integrated behavior and remaining limits

| Area | Source and actual contract | Evidence/limit |
|---|---|---|
| Typed SDK | [orchestrator.py](../neoxider_agents/orchestrator.py): `AgentClient`, `TaskHandle`, typed status/results; tracked hidden wrapper subprocesses, private prompt transport, ownership binding | Existing CLI lifecycle/provider path; session may publish after `spawn` returns. No silent fallback model. |
| Messages | [state.py](../neoxider_agents/state.py), [message_receipts.py](../neoxider_agents/message_receipts.py), [lifecycle.py](../neoxider_agents/lifecycle.py): stable caller UUID, durable queue/journal, receipt query after uncertain transport | `accepted` = persisted; `delivered` = invocation succeeded with nonempty answer; `acknowledged` = explicit assistant evidence. None proves application. |
| Parent events | [events.py](../neoxider_agents/events.py), lifecycle outbox, SDK named subscribers, GUI pages/cursors | Terminal publication is durable/idempotent across crash replay; named callback progress advances after success. At-least-once effects require caller deduplication. |
| Wait/callbacks | SDK `wait`, `wait_any`, default callbacks and named durable consumers | Default callbacks are ephemeral; named consumers replay across reconnect. Timeout observes without stopping a worker. No hosted-chat injection. |
| Recovery | [recovery.py](../neoxider_agents/recovery.py) plus opt-in CLI/SDK reviewed route | Retained report SHA, recapture, session/generation/source/profile/effect checks and launch ownership lock. Unknown attempts/effects and stale/incomplete evidence fail closed. Legacy manual restart/flush retains replay risks. |
| Runtime profiles | [runtime_profiles.py](../neoxider_agents/runtime_profiles.py), lifecycle/SDK launch and resume | Optional Codex/OpenCode private retained paths, executable/script pins and actual process lease. No automatic auth-file copy/global-home fallback. Other providers refuse isolation. |
| Failures/costs/QA | [diagnostics.py](../neoxider_agents/diagnostics.py), runtime classification and result collector | Quota authority differs from auth/TLS/network/OOM. Catalogue estimates, provider usage and billing are separate. Hard billing caps refused; task exit zero is not QA. |
| Fleet GUI | [gui.py](../gui.py), [fleet.js](../static/fleet.js), [Fleet contract](FLEET_UI.md) | Group/filter/conflict/status views, event rendering/read acknowledgments, exact UUID receipt polling. Launcher started means pending, `accepted=false`; browser/terminal live QA remains Root-owned. |
| Write scope | Public enforcement capabilities and SDK `scope_enforced=False` | Ordinary providers have declaration/diff reporting only. Independent outside-AAP escape baseline rejected; candidate Windows fixes remain unverified by this report. |

Use [Core integration](CORE_INTEGRATION.md) for shared invariants and
[SDK API](ORCHESTRATOR_API.md) for types/signatures. The
[original source map](NativeParitySourceMap-20261014.md) is explicitly historical;
its pre-integration gaps are preserved rather than presented as current behavior.

## Examples matching current signatures

These examples are usage instructions, syntactically checked against source.
They were not executed against a provider during this documentation task.
Choose an available explicitly authorized provider/model before running them.

```python
import uuid
from neoxider_agents.orchestrator import AgentClient

seen_events = []  # no external side effects in this callback
with AgentClient(parent="parity-parent", logs_root="C:/private/parity-state",
                 on_event=seen_events.append, consumer="parity-parent-events") as client:
    handle = client.spawn("Inspect src/parser.py and report findings",
                          engine="codex", cwd="C:/work/project",
                          name="parser-review", owns="src/**")
    message_id = uuid.uuid4().hex
    receipt = client.send(handle, "Include malformed input handling",
                          message_id=message_id)
    receipt = client.receipt(handle, message_id)
    completed = client.wait_any([handle], timeout=30)  # list of settled TaskResult
    result = handle.result()
    print(receipt.accepted, receipt.delivered, result.tests_status, result.qa_status)
```

Default context exit stops unfinished client-owned work and preserves durable state.
Use explicit detach only when the caller intentionally keeps a tracked worker alive.
`wait_any(..., want_all=True)` waits for all selected tasks or its deadline; zero
timeout polls once. `subscribe(callback)` gives ephemeral metadata notifications;
`subscribe(callback, consumer="stable-name")` uses independent durable consumer
progress. Reconnect with the same parent/consumer, then `client.attach("parser-review")`.
A callback exception leaves its event unacknowledged for replay; external callback
effects must be idempotent by event ID.

```python
with AgentClient(parent="parity-parent", logs_root="C:/private/parity-state") as client:
    handle = client.attach("parser-review")
    # message_id and assistant_evidence come from the actual retained conversation:
    client.acknowledge(handle, message_id, evidence=assistant_evidence)
    # Review the retained report and resolve blockers before executing resume:
    report = client.inspect_recovery(handle)
    client.resume(handle, message="Continue reviewed work", reviewed=True,
                  reviewed_report_sha=report["report_sha256"])
    # Optional isolation; the named environment key must be explicitly approved:
    isolated = client.spawn("Report findings", engine="codex", cwd="C:/work/project",
                            name="isolated-review", isolate_runtime=True,
                            credential_env_keys=("OPENAI_API_KEY",))
```

The backend records evidence supplied by the caller; use actual explicit assistant
confirmation rather than fabricated evidence. Reviewed recovery does not
authorize unknown external POSTs or replay unfinished message attempts. Isolated
authentication must be provisioned explicitly; the current global login is not copied.

```powershell
neoxider send parser-review --message-id 12345678-1234-4234-8234-123456789abc "Follow-up"
neoxider receipt parser-review --list --json
neoxider receipt parser-review 12345678-1234-4234-8234-123456789abc --json
neoxider recovery parser-review --json
neoxider restart parser-review --reviewed --reviewed-report-sha SHA "Continue reviewed work"
neoxider run -e codex -t isolated-review --isolate-runtime --credential-env-keys OPENAI_API_KEY "Report findings"
```

## Typed SDK, wrapper and native host tradeoffs

| Interface | Useful contract | Practical cost/limit |
|---|---|---|
| Typed Python SDK | In-process typed handles/results, `wait_any`, ownership-checked controls, callbacks and retained consumer cursors | Still launches CLI subprocesses; uses monitor/subscriber/reaper threads and filesystem journals. Python caller owns its lifecycle and callback effects. |
| CLI wrapper | PowerShell/cmd/POSIX entrypoints; cross-provider launch/control; durable task/message/event state; inspection after launcher exit | Harness must retain exact task/job/session identities and collect completion. Ordinary CLI stdout is not a hosted parent callback channel. |
| Host-native subagents | Host-provided agent handles and completion delivery when available | Host/model/session features depend on that host. This wrapper does not inject its events into arbitrary hosted chats or prove native security/latency equivalence. |

These are interface comparisons, not benchmarks or a model-quality ranking.
Provider aliases/catalogues do not prove available quota or effective reasoning.
Free OpenCode Muse/SpaceBunny routes were rate limited in this effort (historical).
On 2026-10-14 OpenCode 1.18.27 ran two measured file tasks to completion: SpaceBunny
(same session `ses_ec769f524ffensNfPjy5pEJlbx`: message UUID
`41b5106e2ef24e1ea3c2e23462d01301` delivered with explicit assistant ack, controlled
marker-plus-sleep stop `stop 0` / launcher `130` with observed identities gone, then a
pending-only same-session recovery under new UUID `c8dc54c852e442f98cc2adbc910e34c0`
ending `waiting` with no owned process) and Muse Spark 1.3 (`done`, exit 0). Input/result
bytes were SHA256-checked by Root; the final SpaceBunny answer hash is
`cbc30a10fe88d68647a4ed1f1b75f3a5e73ba41d144a469fbacf07d62b78ac3f`. That exact case
only: no claim about all models, stable free quota, tests, QA or billing. The human
authorized Codex CLI fallback. A successful model turn only proves that turn's route;
it does not prove tests, QA or billing. Separately, two concurrent private-`CODEX_HOME`
Codex CLI launches with same-session SDK attach/send/ack/dedupe and 14 callbacks passed;
Root source-hash, actual file-hash and PID-creation rechecks passed, billing unknown,
auth was manually Root-provisioned (no automatic SDK auth copy, no global change).

## Before versus current SDK/CLI (wrapper ergonomics, not model quality)

Earlier one-native-versus-one-CLI comparison artifacts
(`C:/AI/outputs/neoxider-native-comparison-20261014`, Russian source report) covered a
single static README-audit task per side with matching results (1/6/2, one shared
medium finding). The table below contrasts the pre-integration baseline against the
current wrapper. It says nothing about which model is better: both sides of that test
produced the same verified result, and speed/cost were not rankable there.

| Area | Before | Current SDK/CLI |
|---|---|---|
| Launch | Bare tracked run; caller-owned prompt argv | Typed `spawn` with private prompt-file transport, ownership binding, optional retained Codex/OpenCode runtime profile (`isolate_runtime`) with explicit `--credential-env-keys` only |
| Messages | Integer inbox sequence, at-least-once redelivery | Stable caller UUIDs with durable receipt journal (`accepted`/`delivered`/`acknowledged`); same-UUID redelivery dedupes, unknown attempts fail closed under reviewed resume |
| Control | `send`/`stop`/`restart` on live wrappers | Same verbs plus generation tracking, `--pending-only` safe drain, `send --flush` recovery, reviewed `restart --reviewed` with report-SHA recheck |
| Recovery | Manual legacy restart with replay risk | Opt-in reviewed resume: retained report SHA, recaptured projection, session/generation/source/profile/effect checks; legacy path keeps its documented replay risk |
| Resources | Launcher/process cleanup best effort | Exited resources reaped promptly; lingering settled owners identity-checked and released within 300 s of observed settlement; sessions/profiles/results retained for resume |
| Context | Caller-held task names | Durable parent events with named consumer replay (`subscribe(callback, consumer=...)` persists progress only after callback success); observation timeouts leave workers running |
| Limits | Exit codes only | Typed failure/cost/verification evidence; quota authority separated from auth/TLS/network/OOM; hard billing caps refused; exit zero is not QA |
| Evidence | Raw log tail | Retained receipts/event journals readable by CLI, SDK and Fleet GUI; unfiltered logs frozen per evidence root |

## Five-minute completed-resource contract

The SDK reaps exited launcher/provider resources promptly: waits, output pumps,
pipes, owned Windows handles and private prompt files. A lingering settled owner
is eligible at 295 seconds, reserving polling/reaping time for the 300-second bound
measured from observed settlement. It revalidates captured PID creation stamp,
generation, parent and private ownership before process-tree action. Active/stale,
reused-PID, foreign and adopted owners are spared. The separate cleanup thread
continues independently of slow callbacks; no idle turns/restart loop is sent.

Reaping retains the same task/session/profile/result for resume. Explicit
`client.cleanup_runtime(handle)` is destructive profile removal and separately
requires an inactive exact lease and locks; it invalidates isolated resume.
Backend `cleanup(..., live=False)` alone is only a caller assertion. GUI-owned
children exit naturally and are reaped; visible Windows Terminal/tab lifetime is
host-owned, with no claim of closing arbitrary user terminals. Never kill external
user terminals: only identity-checked owned launchers are acted upon.
`close(shutdown_timeout=2.0)` stops owned live workers first, then joins
subscriber/monitor/reaper threads under one shared budget. A blocking Python callback
is never forcibly killed: overdue callbacks stay explicitly `pending` and may still
persist their in-flight ACK after close returns, so keep the state directory until
pending threads finish. A callback calling close itself gets an `in_progress`
snapshot instead of a self-join deadlock.

## Receipts, negative history and pending gates

Evidence root: `C:/AI/outputs/neoxider-parity-core-20261014/`.
The following are preserved receipts, not a new full regression run:

| Receipt | Result and scope |
|---|---|
| `outputs/neoxider-parity-close-fix-20261014` | **53 SDK + 11 parent + 17 core-integration tests PASS** after the callback-shutdown fix (shared `shutdown_timeout` budget, explicit pending callbacks, no forced kill, no self-join deadlock). Owned change limited to `orchestrator.py`, its two test files and `ORCHESTRATOR_API.md`. |
| Full legacy regression (Root) | **1235 tests: 3 failures, 1 error, 5 skips (initial)** with 2 runner-correction cases plus an SDK race fix; Root is reconciling reports against a clean baseline exit. Do NOT claim one clean 1235 rerun. |
| `root-completion-check.log` | **69 tests PASS**, 106.336 s: SDK/core/parent fixture gates. No live network-provider, OS enforcement or browser QA claim. |
| `core-integration.log`, `parent-delivery.log` | 15 and 6 fixture tests PASS; publication/recovery/leases and parent delivery. |
| `test_message_receipts.log`, `test_state_concurrency.log`, `test_durable_events.log` | 56, 4 and 71 tests PASS (fresh Root PASS); durable-state fixtures and failure paths. |
| `test_recovery.log`, `test_diagnostics.log`, `test_diagnostics_bounds.log` | 18 PASS; 28 run with one skip; 14 PASS. |
| Current runtime-profile docs check | **26 tests run, 25 passed, one Windows symlink-privilege skip**, 11.830 s, exit 0. Local fixture/version-probe tests only; no provider launched. Earlier retained profile log has 22 tests/one skip. |
| Historical `regression-results.json` | Records earlier core/contracts failures. Do not reinterpret as all green because later logs reuse filenames. |
| Historical `sdk-regression.log` / `final-integration-results.json` | Earlier SDK suite had three failures in 46 tests. Later 69-test Root completion receipt supersedes those narrow contracts; it does not erase that history or close all final gates. |
| [Fleet follow-up](FLEET_UI.md#final-follow-up-evidence-supersedes-the-previous-wave) | 172 legacy GUI and 19 HTTP/resource tests PASS; 12 Node fleet checks and model/toast/syntax checks. Initial GUI assertion failure remains archived. |
| [Independent enforcement audit](SECURITY_AUDIT.md) | Outside file write via ambient AAP access succeeded in the audited baseline despite the candidate's 19 passing tests. **Rejected**, public enforcement false. Later fixes need fresh independent reproducer/security gates. |
| Independent security V3 | **Pending.** Three Muse fixes verified at 32 passed / 1 network-excluded: detached-only genuine access-control plus unique `WriteRestrictSID` source. Normal CLI providers remain unsupported, public ordinary-provider caps are false, and ordinary/`noWindow` native children (`0xc0000142`) are unsupported. No billing guarantees, no automatic CAS source writeback, no hosted-chat injection. |
| Live Codex isolation freeze | `C:/AI/outputs/neoxider-live-isolation-20261014`: actor watchdog 124 fired only after the successful gates (preserved); the prelaunch green gate was worker-derived, not Root preapproval. Isolation configs are launch-scoped; no global change. |

Runtime-profile verification command for the current docs check:
`python -B -m unittest discover -s tests -p test_runtime_profiles.py -v`.
Documentation Python blocks were parsed and API call signatures checked without
launching providers; local links and CLI parser examples were checked, and
`git diff --check` was required for handoff. No source-final security acceptance
is inferred from these documentation checks.

Root still owns final source hashes/full regression and distribution checks,
independent Windows escape/security gates, real authorized provider launch/message/
control/recovery and isolated-runtime evidence, live browser/visible-terminal QA,
fresh remote check, sole-author release decision and remote SHA verification.
This report remains **DRAFT** until those gates are evidenced and Root updates it.
