# Orchestrator API

`neoxider_agents/orchestrator.py` is the typed, in-process facade over the **existing** CLI.
One stable client spawns, messages, inspects, stops and resumes real tracked tasks.
The existing lifecycle now integrates durable receipts/events, optional reviewed recovery,
retained isolated runtimes and typed diagnostics. See [Core integration](CORE_INTEGRATION.md)
for the shared CLI/SDK contract and its fixture/live acceptance boundary.

```python
from neoxider_agents.orchestrator import AgentClient

with AgentClient(logs_root=None) as client:            # logs_root=None -> AGENT_CLI_LOGS, else ~/.claude/agent-cli-logs
    handle = client.spawn("Fix the parser in src/",
                          engine="codex", model="", effort="medium",
                          cwd=r"C:\work\app", owns="src/**",
                          name="fix-parser")
    handle.send("Also add the regression test")
    result = handle.wait(timeout=1800)
    print(result.state, result.files_changed, result.answer[:200])
```

Python 3.8+, standard library only.

## What actually happens

| Step | Reality |
|---|---|
| `spawn` | Writes a unique `<state>/<name>.sdk-<uuid>.msg` and starts `[python, agent.py, run -t NAME -C DIR -P PARENT --prompt-file FILE]` as a **managed hidden subprocess** (no shell, `CREATE_NO_WINDOW`/hidden startup info on Windows). Controls use the same tracked launcher. |
| Returns when | The wrapper publishes its exact PID/start stamp, requested parent and core owner metadata, or `publish_timeout` (default 30 s) expires. Provider session publication can follow later. |
| Ownership | `-P <parent>` is passed to run/send/restart. Binding lives in private client state plus an additive `<name>.<client-id>.json` sidecar. The SDK never writes binding fields to core `.meta`. `parent` defaults to `AGENT_PARENT`, then `AGENT_ORCHESTRATOR_ID`, then the client id. |
| Generation | `handle.generation` is the live owner generation. Every resume publishes a new one; `status()` refreshes it. |
| Live status | `status()` / `result()` / `wait_any()` read task metadata under a bounded retry, so a concurrent metadata rewrite is never reported as "no such task". |
| Results | `result()` is built from the **same collector as `neoxider result --json`** (`ux_tracking.collect_result`), compacted: per-file `path/status/insertions/deletions` only, no hashes, no control history, no overlap scan. |

The provider is launched by the existing `Turn`/`provider.command` path, so per-invocation
`-e/-m/-f/-C/--owns/--strict-owns/--log/--notify` and the **existing CLI precedence**
(explicit flag → `AGENT_ENGINE`/`AGENT_MODEL`/`AGENT_EFFORT` → config file → detection)
are reused unchanged. An explicitly requested model is never silently swapped: the handle
reports both `requested_model` and `resolved_model`, and `handle.model_substituted` is
`True` only when an explicit id resolved to a different provider id (alias expansion).

## Types

`TaskHandle` — `task`, `client`, `parent`, `directory`, `engine`/`model`/`effort`,
`requested_*` vs `resolved_model`, `generation`, `session`, `state`, `queued`, `exit_code`,
`owns`, `detached`, `spawn_id`, `spawned_at`, `settled`, `error`, plus
`status()/result()/wait()/stop()/send()/resume()/detach()/to_dict()` and
`model_substituted`.

`to_dict()` returns a JSON-compatible public snapshot with `client_id`; it never copies
the client, locks or wait event. Public `owned`, `adopted`, `adopted_parent` and `parent`
fields are informational; they cannot grant control permissions.

`TaskStatus` — `state` (the same `effective()` state the CLI and GUI show), `live`,
`owner_alive`, `session`, `generation`, `exit_code`, `reason`, `files`, `queued`,
`idle_sec`, `error`, `last_send_error`.

`TaskResult` — `state`, `settled`, `exit_code`, `session`, `duration_sec`, `answer`,
`stop_report`, `changed_files`, `owns`, `scope_declared`, **`scope_enforced=False`**,
`scope_note`, `baseline_known`, `baseline_partial`, `baseline_warning`, `attribution`,
`usage`, `tokens`, `cost_usd`, `limit`, `queued`.

`SendReceipt` — `accepted`, legacy `sequence`, `sequence_source`, `queued`, `resumed`,
durable UUID `receipt_id`, receipt `state`, `delivered`, `acknowledged`, `detail`.
`send(..., message_id=uuid)` enqueues directly under the inbox lock; acceptance requires
the durable payload and journal. Query with `client.receipt(handle, uuid)` or
`client.receipts(handle)`. Only `client.acknowledge(handle, uuid, evidence=...)` with
explicit assistant evidence establishes acknowledgment. Transport errors after acceptance
preserve the ID; unfinished acceptance errors expose `OrchestratorError.receipt_id`.

`TaskEvent` — `task`, `kind` (`spawned`/`state`/`settled`/`queued`/`callback_error`/
`monitor_error`), `state`, `detail`, `at`, plus durable `event_id`, `generation`, `parent`,
`sequence` and `data` when delivered from the event stream. Named consumers receive the
native lifecycle kinds (`born`, `queued`, `delivered`, `terminal`, `error`, etc.).
Results additionally expose `failure`, `verification`, `tests_status`, `qa_status`,
`cost_facts` and `event_pending`; task exit zero never establishes tests passing.

`CloseReport` — `stopped`, `retained`, `detached`, `failed`.

Errors: `OrchestratorError` (base) → `SpawnError`, `NotOwnedError`, `ResumeError` →
`ResumeNotSupported`, `WaitTimeout`, `ClientClosed`.

## Live-handle contract

A handle is a **snapshot that `status()`/`result()`/`wait()`/the monitor refresh**, not a
push channel. In particular `session` is empty until the provider reports one (the CLI
writes it from the provider stream), and `generation` changes on every resume:

```python
handle.session                 # "" right after spawn is normal, not an error
status = handle.status()       # refreshes state, session, generation, model, queue
assert handle.session == status.session
```

`wait()`/`wait_any()` combine bounded polling (the configured `poll`,
default 0.2 s) with an event wake-up set by the monitor thread. `wait(handle, timeout=N)`
raises `WaitTimeout` when the deadline passes and **leaves the task running**;
`wait_any(refs, timeout=N, want_all=False)` returns the subset that settled (all of them
with `want_all=True`). `timeout=0` polls once; negatives, booleans, strings and non-finite
numbers are rejected. `timeout=None` waits without a deadline. A resume clears the settled
event and refreshes the same handle; resource reaping does not prevent further resumes.

## Current capabilities and limits

| Area | State |
|---|---|
| Receipt ids | Durable UUIDs, queryable across generations and transport failures; the legacy integer `sequence` remains. |
| Delivery / acknowledgement | Delivery records successful invocation completion with a nonempty answer. Acknowledgment requires explicit assistant evidence; neither proves semantic application. |
| Events | Durable lifecycle events and named consumer replay; default callbacks remain ephemeral. GUI pages and read acknowledgments use the same journals. |
| Write scope | `owns` is a declaration plus delta filtering. **No OS enforcement, no sandbox** (Phase 5). `scope_enforced` is always `False` and `scope_note` says so. |
| Hosted chat | **Not supported.** No message injection into a hosted Codex/Claude chat. |
| Baselines | Measured against task start in `cwd`; concurrent writers cannot be distinguished; `baseline_partial`/`baseline_warning` are passed through verbatim. |
| Usage/cost | Provider-reported usage and cost estimates only. Model-catalogue estimates and confirmed billing remain separate facts. |

`AgentClient.capabilities()` (or `orchestrator.capabilities()`) describes these contracts
at runtime. The integrated fixture suite asserts UUID receipts and durable events;
`os_sandbox` and `hosted_chat_injection` remain `False`. Optional reviewed resume and
retained runtime isolation are integrated; hard billing caps remain unsupported.
Windows enforcement changes are under security review; the independently audited
outside-AAP escape baseline was rejected. See the [draft report](NATIVE_PARITY_REPORT.md).

## send / resume / stop semantics

* `send(handle, text)` on a **running** task enqueues and returns quickly:
  `accepted=True`, `queued=True`, `sequence=<legacy inbox sequence>`.
* `send(handle, text)` on a **settled** task starts a new owner (tracked child), so the
  receipt is `resumed=True` with `sequence` read from `inbox/sequence` at publication and
  the note *"delivery is pending and unknown"*.
* `send(..., now=True)` is the interrupt-and-resume variant; it needs a session id and the
  CLI refuses otherwise.
* `resume(handle, message=None, fresh=False, model=None, effort=None)` — `fresh=False` resumes the saved session in a
  new owner (new generation, same session, original files kept); `fresh=True` replays the
  original prompt as a new session. Both return the **same handle**, refreshed.
  `model`/`effort` switch the model or thinking mode for this and later turns of the same session;
  they are passed to the provider as given and never substituted.
* Resume is refused up front, with nothing started, when the provider reports
  `supports_resume=false` (e.g. `gemini`) or when no session id was saved —
  `ResumeNotSupported` / `ResumeError`.
* `stop(handle)` inspects the live state first, delegates to `neoxider stop`, waits for the
  wrapper to exit and returns the retained `TaskResult` (`state='stopped'`, `exit_code=130`,
  populated `stop_report`). Stopping an already settled task is a no-op read.
* A supplied handle must be the exact object registered in `client._handles[name]` and
  bound to that client. Constructing a handle, changing its `client` pointer or setting
  `adopted=True` never authorizes it.
* `attach(name, adopt=False)` binds an existing task with the client's parent.
  `adopt=True` records explicit private adoption provenance; adopted tasks are reported
  under `CloseReport.retained` and never automatically stopped, including when parents
  match. Explicit controls remain available to the adopter.
* Current metadata must still carry the originally bound parent before every inspection,
  wait and control, including close. Reparented tasks raise `NotOwnedError`; close retains
  them and spares their old launcher. Mutating public adoption fields cannot override this.
  Owned tasks also require the privately recorded PID/start stamp/generation, refreshed
  only by an SDK-launched owner. An unrelated replacement owner with the same parent is
  spared. Explicit adoption follows the task's parent rather than granting automatic stop.
* Each prompt file is unique. Owner publication is not proof of prompt consumption;
  owner prompt files remain until the child exits. Synchronous control files are removed
  after that command exits. Failed launches clean their own files.
* Provider resume checks execute captured UTF-8 source in a private namespace, ignoring
  matching timestamp/size bytecode caches and existing module-registry entries. The SDK
  does not register or replace global modules.

## Completed-resource cleanup and explicit close

```python
with AgentClient() as client:                 # default: stop client-owned live workers on close
    a = client.spawn(...)                     # owned  -> stopped on close
    b = client.spawn(..., detach=True)        # opt-in -> kept running, still tracked
    a.detach()                                # same opt-in, after the fact
report = client.close(detach=True)            # keep everything running
```

* Finished tracked launchers are reaped automatically: process wait, bounded output-pump
  shutdown, stdio closure, native Windows process-handle closure, prompt-file deletion and
  removal from the resource registry. This normally happens immediately after exit.
* A completed owner still holding resources is released within **300 seconds** of observing
  its settled state. Cleanup starts at 295 seconds, reserving time for polling and reaping.
  It requires the originally captured PID/start stamp, owner generation, parent and private
  ownership binding to match current metadata. Active owners, changed generations, reused
  PIDs and foreign/adopted owners are spared. The owned process tree includes descendants;
  the SDK never kills an arbitrary `provider_pid` copied from metadata.
* A separate cleanup thread polls at most once per second. Subscriber callbacks cannot
  delay cleanup. No idle model turn or automatic provider restart is generated.
* Default `close()` stops **only currently owned live workers** and keeps every accepted
  result (`.meta`, `.answer`, `.stop`, `.md`,
  inbox, baseline). It never touches tasks owned by someone else and never deletes state.
  It also releases lingering settled owned launchers before returning, using the same
  current-identity checks. Concurrent close calls return one idempotent report.
* `detach=True` (per spawn, per handle, or at `close`) keeps workers running; the handles stay
  tracked and inspectable, and `close()` reports them under `detached`.
* `CloseReport.stopped/retained/detached/failed` is the audit trail; `close()` is idempotent.
* Close cancels every named consumer before resource cleanup, then joins subscriber,
  monitor and cleanup threads with one shared `shutdown_timeout` budget (default **2 seconds**).
  This budget covers thread shutdown after owned-process cleanup; process controls keep
  their existing timeouts. It accepts finite non-negative seconds, including zero.
  Cooperative callbacks, their durable event ACK and thread teardown must finish within
  the budget; close waits for all three. New subscriptions
  after close are refused with `ClientClosed`.
* Additive `CloseReport.pending_consumers` contains plain consumer names whose threads
  are still alive at return; `pending_threads` names any surviving monitor/cleanup threads.
  Timed-out callbacks and the callback calling close itself cannot be forcibly stopped.
  They may still perform side effects or persist their in-flight ACK after close returns.
  Keep their state directory available until those threads finish. An empty pending list
  is required before assuming that subscriber writes have ceased.
* Calling close inside a subscriber skips self-join. If another thread is already closing,
  that callback returns a provisional report with `in_progress=True` and pending resources
  instead of waiting for the closer's lock. Its task-action lists are incomplete; an ordinary
  concurrent caller waits for the final report. Final reports have `in_progress=False` and
  remain idempotent snapshots, including any pending resources observed at return.
* Detached or reparented live processes keep their resource tracking until natural exit;
  their exited resources are then reaped. Typed handles, saved sessions and retained results
  survive reaping. Use explicit `close()` to stop unfinished owned work; GC is not a stop API.
* A failing callback is recorded in the bounded error queue and unsubscribed. Healthy
  subscribers receive one `callback_error` notice for that event; failed callbacks are not
  invoked recursively to report their own failures.

## Environment safety

The SDK never mutates process state: no `os.environ` writes, no `sys.stdout`/`sys.stderr`
replacement, no shell. Every launch gets its **own env copy** built from
`os.environ` + client `env=` + per-call `env=` plus
`AGENT_CLI_LOGS`, `PYTHONUTF8=1`, `PYTHONIOENCODING=utf-8`, `PYTHONDONTWRITEBYTECODE=1` and, for background owners,
`AGENT_DETACHED=1`, `AGENT_LAUNCHER_PID=`. Provider prompts always travel by file, never by
argv and never by shell interpolation. `tests/test_orchestrator_api.py` asserts the module
source contains no env/stdout mutation and that the shared tracked `subprocess.Popen`
launcher passes `launch_kwargs()` for both owners and controls.

## Example: bounded fan-out with callbacks

```python
events = []
with AgentClient(poll=0.25, on_event=lambda e: events.append(e)) as client:
    handles = [client.spawn(prompt, engine="opencode", cwd=work, name="task-%02d" % index)
               for index, prompt in enumerate(prompts)]
    done = client.wait_any(handles, timeout=600)          # returns the settled subset
    while len(done) < len(handles):
        done += client.wait_any([h for h in handles if h.task not in {r.task for r in done}],
                                timeout=600)
    for handle in handles:
        result = handle.result()
        print(handle.task, result.state, result.files_changed, result.baseline_known)
```

## Tests

```powershell
python -B -m unittest discover -s tests -p test_orchestrator_api.py -v
```

53 offline tests (46 original scenarios plus 7 callback-shutdown gates: durable ACK before close, subscriber exit, reaping under blocked callbacks, callback-origin/concurrent close, shared budget, failed-callback no-ACK, self-close/timeout validation), with **real** deterministic provider subprocesses
(`tests/fixtures/core-provider.py` via the `fixture`/`noresume` provider fixtures): typed
spawn/owner publication, live status and session refresh, startup failure with no orphaned
task, dead owner reported honestly, queued-message receipt, concurrent sends/status,
stop/cleanup, resume (session kept, generation advanced, unsupported provider refused),
compact honest result, multiple handles with `wait_any`, bounded wait timeout, callback
error isolation, thread safety, foreign-parent refusal, and the source/env/stdout contract.
Additional gates cover forged/adopted/other-client handles, reparenting before close,
exact distinct concurrent payloads with both writes completed before consumption,
two failing callbacks, JSON handle serialization, zero/invalid timeouts, matching stale
PYC exploitation by an ordinary loader followed by safe captured-source loading,
sidecar writes alongside one core writer (no claim about core-writer serialization),
normal immediate resource cleanup, a real lingering owner/descendant with a fake-clock
300-second gate, active/reused/foreign identities, blocked subscribers and three resumes
through the same reaped handle. Fixtures use hidden processes and inspect native handles;
there is no visible terminal launch to clean up. No provider quota or network is used.
