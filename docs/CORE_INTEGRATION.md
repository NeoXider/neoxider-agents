# Core integration contract

The CLI lifecycle and `AgentClient` now connect the existing receipt, event,
recovery, runtime-profile and diagnostic backends. Existing launch defaults,
legacy message sequence files, metadata fields, stdout blocks and provider
session storage remain supported. New features are additive and opt-in where
they change launch policy. This document supersedes the standalone-phase
integration caveats in the individual backend documents; their durability and
security limitations still apply.

The [native parity report](NATIVE_PARITY_REPORT.md) remains **DRAFT** pending
Root security, final regression, live and remote gates. Ordinary provider write-scope
enforcement is false; candidate Windows changes are not acceptance evidence.

## Receipts and safe delivery

`send` and `reply` accept `--message-id UUID` (hyphenated UUID or 32 hex digits).
`AgentClient.send(handle, text, message_id=...)` reserves a UUID before any
possible enqueue side effect. If omitted, it generates one. It enqueues directly
under the task inbox lock rather than starting a transport process to carry the
message. The owner's final queue check holds the same lock. Accepted means both
the payload and receipt journal are durable. A control or launch failure after
acceptance does not erase that fact or silently generate another ID.

```python
receipt = client.send(handle, "Continue with the reviewed change", message_id=my_uuid)
receipt = client.receipt(handle, my_uuid)  # recovery after a caller/transport timeout
all_receipts = client.receipts(handle)
client.acknowledge(handle, my_uuid, evidence="Assistant explicitly confirms receipt")
```

Typed receipts retain the legacy integer `sequence` and add the durable
`receipt_id`, receipt `state`, `delivered` and `acknowledged`. Repeating the same
UUID with different text is rejected. Delivery requires a completed successful
invocation with a nonempty answer; it does not prove semantic application.
Acknowledgment requires explicit caller-supplied assistant evidence. Tool logs,
model claims of tests passing and the final task exit do not supply that evidence.

CLI inspection and acknowledgment:

```powershell
python -B agent.py receipt TASK --list --json
python -B agent.py receipt TASK UUID --json
python -B agent.py receipt TASK UUID --ack --source assistant --evidence "Explicit assistant confirmation"
```

For a settled task, SDK send schedules a tracked `send --flush --pending-only`
owner. It drains pending messages and skips unknown/in-progress old attempts.
The ordinary automatic drain does the same. Manual legacy `send --flush` and
legacy restart retain their documented at-least-once retry behavior and can
repeat an unknown external effect. Reviewed mode blocks unknown receipt attempts;
operator effect review alone does not turn an unknown message delivery into a
known completion. A corrupt journal fails rather than claiming delivery.

## Durable parent events

Lifecycle publishes `born`, `queued`, `delivered`, `delivery_unknown`, `terminal`
and `error` events with the actual task generation and parent. The per-task
sequence continues across generations. Terminal data contains the captured
state, exit, session, answer SHA256 and file count; tests and QA default to
`not_run`. Terminal IDs are stable for the task/generation.

Each event first gets a fsynced immutable JSON intent in `TASK.event-outbox/`.
An inbox-independent locked counter retains intent creation order for crash replay.
Terminal metadata also retains the exact `terminal_intent` envelope. Publication
replays that envelope, not a fresh result assembled from a later answer or
generation. If the publisher dies after appending but before clearing the intent,
the same ID/content is appended idempotently. Successful publication removes only
its intent. Corruption retains the outbox and reports `event_pending`/`event_error`.
SDK wait refuses to accept a terminal state whose required publication is missing
or corrupt. Existing legacy task records without the integration marker keep
their older result/wait behavior.

```python
client = AgentClient(parent="my-parent", logs_root=state_dir,
                     on_event=receive_event, consumer="my-parent-inbox")
handle = client.spawn(prompt, engine="codex", cwd=project)
completed = client.wait_any([handle, another_handle], timeout=30)
page = client.events(handle, after=cursor)
```

Keep the same explicit `consumer` name and parent when reconnecting, then attach
the retained task. Named callbacks receive typed events with `event_id`,
`generation`, `parent`, `sequence` and `data`. Each subscriber has its own thread.
A callback is acknowledged only after it returns successfully; failure stops that
subscriber, records `callback_errors` and leaves the failed event for replay.
Callbacks must deduplicate their own external effects by event ID. Different
consumers and the resource reaper continue independently of a slow/failed callback.

`subscribe(callback, consumer="name")` enables the same durable path.
Omitting `consumer` preserves the existing ephemeral metadata callback API,
including `spawned`/`settled` notifications. Actual durable terminal IDs are reconciled
even when completion precedes the first monitor tick; state polling does not duplicate
the settled notification. It does not persist callback progress. A reconnect may replay
retained terminal generations. Parent-style named consumers containing `/` are mapped
to a stable UUID5-based consumer identifier rather than being dropped.
Event delivery uses bounded internal reads; no hosted-chat injection is promised.
The existing GUI event and acknowledgment endpoints read these published sidecars;
this change does not modify the GUI or certify a live GUI session.

## Reviewed recovery

Legacy `restart` remains manual and does not imply strict review. Strict resume
requires an actual retained inspection and its explicit SHA:

```python
report = client.inspect_recovery(handle)
client.resume(handle, message="Continue the reviewed work", reviewed=True,
              reviewed_report_sha=report["report_sha256"])
```

```powershell
python -B agent.py recovery TASK --json
python -B agent.py restart TASK --reviewed --reviewed-report-sha SHA "Continue the reviewed work"
```

The actual `RecoveryStore` inspects generation/session, sidecar bytes, baseline
preimages and current source, pinned runtime profile/executable and effect state.
Its `authorize_resume` independently recaptures and compares the stable projection.
An explicit operator review of unobserved effects must already be recorded through
the backend's evidence APIs. Nothing automatically declares all external effects
known. Missing SHA, stale source/session/profile/evidence, partial capture, unknown
inbox attempts and unresolved external POST effects block launch without enqueueing
the restart message. Strict fresh replay is refused.

Reinspection occurs before transient launcher prompt files and publication locks
are created. Reviewed SDK prompt transport lives outside task-named retained
sidecars. Ownership locks then prevent competing launchers; inspection is not an
atomic filesystem snapshot or a sandbox against a hostile concurrent writer.
SDK reviewed send can accept a newly reviewed message but requires another review
of the changed inbox before starting a settled owner. It cannot launch using the
pre-enqueue projection. Review receipts and preserved sessions remain durable.

## Retained isolated runtimes

```python
handle = client.spawn(prompt, engine="codex", cwd=project, isolate_runtime=True,
                      credential_env_keys=("OPENAI_API_KEY",))
```

```powershell
python -B agent.py run -e codex -t TASK --isolate-runtime --credential-env-keys OPENAI_API_KEY "Prompt"
```

Only `codex` and `opencode` support this mode. Unsupported providers fail before
task/inbox publication. The default private root is `STATE/runtime-profiles`,
or supply `runtime_profile_root` / `--runtime-profile-root`. Profiles retain their
same home/config/cache/data/state paths for send and resume. Credentials are
inherited only through explicitly approved environment keys; no authentication
files are copied. Supply approved credential values in the client's environment
for subsequent invocations. No global provider-home fallback is used.

The existing provider command resolves native executable/npm launcher unwrapping
without a shell. The primary executable is pinned by the profile; core metadata
also pins any leading script paths (e.g. Node entry scripts). Actual invocation
validates those hashes and uses the retained paths. Provider methods run against
a private environment view, without SDK `os.environ` mutation. Per-call overrides
are limited to the provider's existing command contract and wrapper activity fields.

`runtime_lease` records actual launcher/provider PIDs, creation stamps, parent and
generation after real provider creation. SDK resource reaping retains profiles,
results and native sessions. Explicit `client.cleanup_runtime(handle)` requires
the matching lease, inactive exact process identities and acquired ownership/inbox
locks; missing stamps, live/reused/unknown identities or a changed generation refuse
destruction. Backend seal/link checks still apply. Removing a profile intentionally
makes future isolated resume fail rather than falling back to global state.

Completed launcher handles, pipes and provider resources are automatically reaped;
the existing bounded 300-second ownership/stamp guard handles lingering completed
launchers. Cleanup runs independently from subscribers and never sends idle prompts.

## Failure, cost and verification evidence

Actual provider error envelopes/markers are classified at the provider stream
boundary. OOM, TLS, network and auth remain `state=error`; only authoritative quota
evidence becomes `limited`/`LIMIT_HIT`. Legacy provider-failure exit 126 and watchdog
124/125, stop 130 and existing stop-block ownership wording are preserved.
Unstructured logs remain advisory evidence; quoted/model content is not quota
authority. Results include typed failure evidence and an additive bounded
`diagnostics` object while retaining their legacy fields.

Provider-reported usage/cost, caller-supplied catalogue estimates and confirmed
billing facts remain distinct. A task's success or an advertised zero price never
establishes billing. `hard_cost_cap` / `--hard-cost-cap` is explicitly refused,
because these CLI providers do not offer an enforceable hard billing boundary.
No existing cap or quota setting is raised.

`client.result(handle, verification_receipts=..., current_source_hash=...,
cost_facts=..., cost_decision=...)` accepts the existing typed diagnostic evidence.
`VerificationReceipt` records an explicit caller/wrapper-executed command, actual
exit/output and source-final hash. No command runs automatically. A changed source
hash marks evidence stale. Model assertions and task exit zero never become tests
passing. Default results/terminal events report tests and QA `not_run`.

## Evidence and acceptance boundary

`tests/test_core_parity_integration.py` and `tests/test_parent_delivery.py` use real
local fixture processes, private directories, exact queue payloads and real
publisher process death. They cover safe send/terminal races, reconnect/callback
failure, cross-generation events, reviewed resume/refusal, actual runtime leases,
profile retention/pins, failure categories, source-bound caller checks and corruption.
Unfiltered test logs and final source SHA256 receipts are saved under
`C:/AI/outputs/neoxider-parity-core-20261014`.

The SDK suite now asserts UUID receipts, integrated capabilities and exact distinct
concurrent queue payloads delivered once in sequence order. Existing real/fake-clock
300-second cleanup gates remain. Parent gates also cover completion before the first
monitor tick, default callback delivery, resume/reattach and consumer normalization.
These fixture gates are not live network-provider,
GUI, enforcement or full-plan acceptance; Root audit/live gates remain separate.
Callback-shutdown behavior (shared `shutdown_timeout`, explicit pending callbacks,
no forced kill, no self-join deadlock) is pinned by
`outputs/neoxider-parity-close-fix-20261014` (53 SDK + 11 parent + 17 core tests PASS).
Measured live-provider evidence lives outside the repo under
`C:/AI/outputs/neoxider-live-isolation-20261014` (Codex) and
`C:/AI/outputs/neoxider-opencode-live-20261014` (OpenCode receipts/project only);
see the [draft report](NATIVE_PARITY_REPORT.md) for the exact-case scope.
