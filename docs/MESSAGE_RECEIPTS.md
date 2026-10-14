# Durable message receipts

Receipts are integrated additive state sidecars. The CLI exposes `receipt` and
`send --message-id`; the SDK exposes `send`, `receipt`, `receipts` and `acknowledge`.
Both use `Store.receipt(task, UUID_or_sequence)` and `Store.receipts(task)`;
see [Core integration](CORE_INTEGRATION.md). Existing `enqueue_locked` still returns
an integer and writes `%012d.msg` inbox files. UUIDs identify messages independently
of task generation. The same UUID and content reuse the original reservation; different
content is rejected, even after a failed enqueue.

| State | Durable fact |
|---|---|
| `intent` | UUID, sequence and payload digest reserved; acceptance unfinished |
| `rolled_back` | Queue write failed or recovery found no payload; UUID remains bound |
| `accepted` | Queue payload and acceptance snapshot were flushed and fsynced |
| `delivered` | An invocation containing this message completed with code zero and a nonempty answer |
| `acknowledged` | Caller supplied explicit assistant acknowledgment evidence |

`delivered` says nothing about the model reading, following or applying the instruction.
`applied` requires an acknowledged receipt and an explicit `mark_message_applied` call.
No answer/tool-log heuristic creates either assertion. `acknowledge_message` requires
nonempty evidence and an `assistant` or `assistant:...` source. Receipts store evidence
digests and compact notes rather than raw assistant transcripts.

## Acceptance and recovery

Enqueue publishes an intent, atomically writes and fsyncs the queue payload, writes
the high-water counters, then fsyncs the accepted journal snapshot. The journal and
`<task>.sequence` survive removal of `<task>.inbox`. Allocation includes both validated
counters, all journal reservations (including intents), and existing inbox sequences.
A missing or reset counter cannot cause sequence reuse while journal history survives;
a malformed counter fails closed. Deleting all counters, queue files and journal history
removes the evidence needed for monotonicity and UUID deduplication.

A crash after queue publication leaves an intent and the original payload. A direct
retry with the caller's UUID validates that payload and promotes the original sequence;
it needs no separate reconcile call. An intent with no payload is rolled back by
`reconcile_receipts`, and the same UUID may retry its original reservation. Reconciliation
adopts real legacy inbox messages as accepted and never fabricates missing empty payloads.
Adoption and recovered-intent promotion fsync the existing payload before acceptance.
Public queries report a missing accepted payload as `ReceiptError`.

Delivery attempts retain their generation, turn, count and outcome. An old or empty
generation cannot complete another generation's attempt. Completion is appended and
fsynced **before** removing the matching queue payload. If the owner crashes between
those operations, batch reconciliation removes the matching completed payload without
another provider invocation. A changed payload fails closed and is retained.

Nonzero, interrupted and incomplete invocations retain the queue and an unknown outcome.
Automatic draining skips these messages. Explicit `send --flush`/resume can retry them;
delivery is therefore at least once across an unknown crash boundary, never exactly once.
The wrapper cannot determine whether an external side effect occurred. Failed message
invocations do not automatically enter provider retry loops. An acknowledgment racing
with completion retains both facts without reducing receipt rank.

## Locks and metadata

Public enqueue, begin/finish delivery, acknowledge, applied, lookup/list, reconcile and
repair operations acquire the task `.inbox` publication lock once. Their `*_locked`
helpers assume the caller holds that exact `state.Lock`; lifecycle uses these helpers.
There is no thread-local/reentrant lock bypass. `batch_locked` reconciles receipts before
forming a batch; `include_unknown=False` selects only safe automatic follow-ups.

`Store.update` serializes metadata read/merge/publication with a separate `.meta` lock.
An existing caller holding the `.inbox` or `.owner` lock may call `update`. A caller
holding the `.meta` lock must use `update_locked`. Independent metadata keys survive
concurrent process updates; replacement of the same key follows lock publication order.
When both locks are needed, acquire `.inbox` before `.meta`.

## Journal integrity and durability limits

Snapshots are bounded JSONL records with schema, task, UUID, digest, sequence, revision,
state and attempt validation. Complete interior corruption or rank/binding regression
fails closed. An incomplete final line, even syntactically valid JSON, is uncommitted.
Repair runs under the inbox lock, preserves exact pre-repair bytes in `.receipts.jsonl.bak`
(subsequent repairs use unique `.bak.*` files), fsyncs the backup and replacement, then
publishes only complete validated lines. Full-history UUID lookup streams the journal
with bounded line buffers; it does not depend on a 4 MiB tail. Listing/reconciliation
hold one latest record per UUID; `journal` explicitly materializes history for diagnostics.

Atomic file publication flushes and fsyncs bytes before replacement. A failed journal
append rolls back to the previous complete boundary and reports the storage error;
it cannot leave a complete acceptance snapshot after a failed fsync. POSIX additionally
fsyncs directories after publication/removal. Windows stdlib does not expose directory
fsync, so sudden power-loss guarantees remain limited by the filesystem and device;
process-crash ordering and file fsync are enforced. Backups and task/session/result
sidecars remain available for recovery; this feature introduces no background worker.

## Verification

Run with `PYTHONDONTWRITEBYTECODE=1` so fixture subprocesses also avoid PYC writes:

```powershell
python -B -m unittest discover -s tests -p test_message_receipts.py -v
python -B -m unittest discover -s tests -p test_state_concurrency.py -v
```

Coverage includes real child crashes before/after queue publication and after completion,
direct UUID retry, counter reset/removal/corruption, old/new IDs beyond 4 MiB, bounded
lookup memory, exact torn-tail backup, interior corruption/rank validation, process
writers, external caller locks, acknowledgment/completion races, stale generations,
real provider success/failure/stop, preflight preservation and no implicit message retry.
