# Durable parent event backend

`neoxider_agents.events.EventStore` is integrated with lifecycle publication,
SDK wait-any and named callbacks, and GUI event pages/read acknowledgments.
See [Core integration](CORE_INTEGRATION.md) for durable publication/replay and
the default ephemeral callback contract. Hosted-chat injection remains unsupported;
Root security, final regression, live and remote gates remain pending in the
[draft report](NATIVE_PARITY_REPORT.md).

## Identity and persistence

Each immutable event contains `task`, `generation`, `parent`, an opaque
32-character lowercase hex `id`, per-task integer `seq`, `epoch`, `kind`,
JSON object `data`, content `digest`, and deterministic content `key`.
Parent labels are separate bounded identifiers: `root-orchestrator`, SDK IDs,
and `/root/events` are valid; they need not be UUIDs. Generation and parent
participate in the content digest. Sequence spans generations and restarts.

`emit(task, generation, parent, kind, data, event_id=None)` assigns a new
UUID when no ID is supplied. A retry with the same ID and identical canonical
content returns the original record with `duplicate=True`, without another
append. Different content for that ID raises `ValueError`. Identical content
with a new ID is a new event. Timestamp and sequence belong to the original
append and are retained on retry. The digest detects content mutation; it is
not an authentication signature for a hostile state-directory writer.

Sidecars are `<task>.events.jsonl` and `<task>.events.acks.jsonl` under the
chosen state root (`AGENT_CLI_LOGS` by default). The module does not prune,
compact, or remove these files. Old terminal events remain replayable after
new generations start. Ordinary complete rows are never rewritten.

All store reads and event appends hold the existing `state.Lock` for the
event sidecar. Acknowledgment writers hold the acknowledgment sidecar lock
and acquire the event lock only to validate its head. Event writers never
acquire the acknowledgment lock. Appends loop over short writes and fsync
before returning. Directory fsync is attempted on POSIX; Windows does not
provide it through this implementation. Persistence depends on filesystem
and device fsync guarantees.

## Corruption and crash repair

Every complete row is validated, including rows preceding a requested cursor
or ID and rows following a match. There is no tail fast path for `head` or
append. Event sequence must advance by exactly one. Complete malformed rows,
foreign tasks, invalid fields, sequence resets/gaps, digest mismatches, and
oversized lines fail closed with `EventCorruption`.

A final fragment without a newline is reported as `torn` and withheld from
readers. Before the next append (including an idempotent retry), the writer
validates the full file, copies its original bytes to a uniquely named
`<sidecar>.torn.<uuid>.bak`, fsyncs that backup, then truncates only the fragment
under the sidecar lock and fsyncs the live file. A repair interrupted before
truncation can be retried; backups remain preserved. An incomplete sequence
was never committed and is reused: two complete rows plus a torn fragment
are followed by sequence 3. A complete JSON row lacking its newline is also
treated as uncommitted. Backups can contain explicitly allowed sensitive data
and have the same retention responsibility as the sidecars.

The acknowledgment journal follows the same backup/repair procedure, even
when an acknowledgment is a no-op. Its own `seq` must advance by one; each
consumer's integer cursor must strictly advance and stay within the event
head. Invalid timestamps, row types, or cursor regressions fail closed.
The earlier rejected draft journal without row `seq` is not accepted as valid
history; no silent migration or reset is performed.

## Paging and acknowledgment

```python
from neoxider_agents.events import EventStore

store = EventStore("private-state-directory")
store.emit("build", "gen-1", "sdk-parent-7", "task.completed", {"exit": 0})
page = store.read("build", limit=100)
for event in page:
    handle_event(event)
store.ack("build", "parent-ui", page.cursor)
saved_cursor = page.cursor.to_dict()  # {"task": "build", "seq": 1}
next_page = store.read("build", after=saved_cursor)
```

Returned `Cursor` values retain integer comparison behavior and carry their
task. Passing a cursor from another task to `read`, `wait`, `subscribe`, or
`ack` raises `ValueError`. `Cursor.to_dict()`, `Page.to_dict()`, and
`WaitReport.to_dict()` preserve binding when JSON encoded. Direct JSON encoding
of an integer subclass loses that binding; use these explicit serializers.
Bare integer/numeric-string sequences remain available for local compatibility
and are interpreted in the explicitly supplied task. Cursors are positioning
evidence, not authorization tokens; state-directory access is trusted.

`read` is exclusive of `after`. `Page.head` reflects the validated snapshot,
`more` means a later sequence exists, `truncated` means matching records exceed
the page limit, and `torn` describes an incomplete final fragment. Acknowledging
a cursor explicitly asserts completion of its prefix; this backend cannot
prove application-side effects. Each consumer retains independent progress.
Acknowledgment races retain the highest cursor; equal/lower requests are
no-ops. Acknowledging beyond the head is refused.

## Callback delivery

`drain(task, callback, consumer)` replays available events and returns a
`WaitReport`. `subscribe` polls for later events, with optional `until`, `stop`,
and `timeout`. For a named consumer, acknowledgment happens only after each
callback returns successfully. A callback failure propagates, its successful
prefix remains acknowledged, and the failed event is replayed on reconnect.
The backend captures each delivered sequence before invoking user code, so
mutating the callback's record cannot acknowledge unseen records.

Delivery is **at least once**, never exactly once: a process that performs an
effect and dies before acknowledgment repeats that event. Callback code must
use the event ID to make its own effects idempotent. Concurrent subscriptions
using the same consumer can repeat callbacks. No claim of exclusive callback
execution or semantic/provider acknowledgment is made. Without a consumer,
or with `acknowledge=False`, no durable progress is written.

`wait` reads without acknowledgment and raises `WaitTimeout` on cancellation
or timeout; it permits an immediate read of available events at zero timeout.
`subscribe` returns a stopped report on cancellation and raises `WaitTimeout`
on timeout. It checks cancellation/deadline before polling and before every
callback, including during continuous ready pages. Callback execution, fsync,
log validation, and lock acquisition are synchronous and cannot be interrupted
by these checks; deadlines are cooperative, not hard real-time limits.
Exceptions from cancellation predicates propagate. Neither API launches
threads, providers, terminal windows, or background processes.

Callbacks acknowledge only delivery evidence supplied by the caller. There
is no mechanism or promise to inject arbitrary events into a hosted chat.

## Resource and secret policy

Logs are read incrementally using bounded `readline`, never a whole-file
`read` or an unbounded in-memory ID index. Payloads are limited to 256 KiB of
canonical ASCII JSON; lines are limited to that plus 4 KiB. Pages contain at
most 1,000 records (default 100); memory scales with that configured page,
not log length. Consumer folding is capped at 1,024 distinct consumers per
task. Repair copies in 64 KiB chunks. Validation scans the full log on each
operation, so write/replay throughput decreases as history grows; this backend
does not yet offer indexed or compacted history.

Task/consumer identifiers and sidecar suffixes are validated. Windows device
names, trailing-dot task names, symlink sidecars, and multiply linked sidecars
are refused. The state directory must be private/trusted: this is not a
sandbox against an attacker racing path replacements or editing state files.

Payloads are copied through canonical JSON, reject non-string object keys,
nonfinite numbers, non-JSON types, and excessive nesting. Known credential
field names with nonempty values and selected whole-string token shapes are
refused by default without echoing credential values. Boolean absence fields
such as `api_key_present=False` are accepted. This guard is deliberately
heuristic, not comprehensive redaction of arbitrary text. `allow_secrets=True`
explicitly permits storage. Avoid raw provider logs, prompt transcripts, and
credentials in event payloads; store compact facts or references instead.
The backend emits no raw/activity/provider logs and mutates neither environment
nor module registry. It cannot control external cleanup policies for its root.

## Verification

Run `python -B -m unittest discover -s tests -p test_durable_events.py -v`.
Tests exercise real multiprocess event appends and acknowledgment races;
child death after a callback and while holding an event lock with a torn write;
backup repair; earliest UUID retry and terminal replay over 4 MiB; bounded
page allocations; reconnect and callback failure; continuous-stream cancellation
and timeout; cross-task cursors; interior corruption; path validation; and
credential rejection. They use synthetic payloads and private temporary roots.
Child processes are bounded, joined, closed, and terminated on test failure.
These are backend tests, not the remaining parent/SDK/GUI integration gates.
