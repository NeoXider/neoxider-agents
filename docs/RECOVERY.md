# Recovery inspection and explicit review

`neoxider_agents/recovery.py` captures retained task evidence and records review
receipts. It never launches a provider, resumes a session, executes a model prompt,
retries a request, changes task state, or edits the source tree. Existing `restart`
remains a separate **unsafe manual legacy route**: it does not enforce these
review, coverage or replay guards. CLI/SDK opt-in reviewed resume is now integrated
and independently rechecks retained evidence before launch; see
[Core integration](CORE_INTEGRATION.md#reviewed-recovery). A review receipt is
**not** approval to replay unknown external effects.

## Public API

All operations raise `RecoveryError` for unsafe, incomplete or changed evidence;
ordinary filesystem failures can also propagate. Paths must be UTF-8 encodable,
without NUL or `..` components. Task names are canonical ASCII names of 1–128
characters, with Windows reserved names refused. No symlink, junction/reparse or
hardlink references are followed. Storage roots must be separate from provider
profiles and the original source tree; the default is inside retained task state.

```python
from neoxider_agents.recovery import RecoveryStore, inspect_recovery, resume_projection
import hashlib
import json

# task_store and runtime_profiles need only their absolute `root` attributes.
recovery = RecoveryStore(task_store.root / "recovery")
report = inspect_recovery(task_store, "task", runtime_profiles,
                          recovery_store=recovery)
# Equivalent: recovery.inspect(task_store, "task", runtime_profiles)
report_sha = hashlib.sha256(json.dumps(
    report, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    allow_nan=False).encode("utf-8")).hexdigest()

# Caller must review the captured report. This call is explicit review evidence.
# It refuses until blockers are resolved and unknown coverage is operator-reviewed.
fresh = recovery.inspect(task_store, "task", runtime_profiles)
receipt = recovery.authorize_resume(report_sha, fresh)
```

`inspect_recovery(store, task, runtime_profiles=None, *, recovery_store=None)`
returns a JSON-compatible dictionary. Without `recovery_store`, it uses
`RecoveryStore(<store.root>/recovery)`. Without runtime profiles, inspection remains
available but reviewed resume is blocked. Legacy Store methods are not called:
they may prune logs or mutate receipts. No model output is accepted as inspection.

`RecoveryStore(root)` creates an owned root if absent (including missing parents),
or validates its ownership marker. A nonempty unowned root is refused. Writes occur
only under this root. Existing records and blobs are never overwritten or erased.

`record_intent(task, payload)` persists arbitrary JSON intent data and returns
`{id, sha256, record}`. `sha256` identifies the complete canonical record, including
its sequence, UUID, task, root identity and previous record digest. Call this before
any operation whose intent must survive a process death. It does not perform it.

`capture_reference(path)` copies exact file bytes into recovery storage and returns
`{ref, sha256, size, source}`. `ref` is `blobs/<sha256>`, relative to the recovery
root. Outcome/evidence APIs accept only a nonempty list/tuple of these references;
the digest and byte length are checked against retained bytes. A bare string such
as `"success"`, an arbitrary URL or a traversal path is rejected. Capturing a
receipt proves its bytes, not the truth of what a caller wrote in it.

`record_effect(task, effectID, intentSHA, status, outcomeRefs=())` writes an immutable
effect version. Status is exactly `intended`, `unknown` or `confirmed`. The intent
digest must identify this task's retained `intent` record. A confirmed outcome
requires captured evidence references. An effect ID cannot switch intent, regress
from confirmed, or regress from unknown to intended. Each version has its own
record digest; all previous versions remain in history. This API records evidence
supplied by callers; it never detects or performs a POST.

`review_unobserved_effects(task, evidence_refs)` records **explicit operator/Root
review of unknown coverage**, with `scope="unobserved_external_effects"`,
`coverage="unknown"`, and `decision="continue_without_automatic_replay"`.
`declare_effect_inventory(task, evidence_refs)` is a compatibility name for this
same operation; it does not declare complete external inventory. Without valid
review evidence, strict reviewed resume is blocked by `external_inventory_unknown`
and `operator_review_required:unobserved_effects`, even with an empty caller journal.
An earlier bare inventory declaration without this review scope/decision cannot
authorize strict resume.

The report exposes `effects.coverage="explicit-journal-only"` when registered
effects exist, otherwise `"unknown"`; `effects.unobserved_coverage` stays `"unknown"`
even after operator review. `effects.operator_review` states whether explicit
review is retained, not whether there were no effects. Arbitrary model tools can
make unobserved requests. An empty journal, successful tool output or operator
review cannot prove those requests absent or establish a generic HTTP POST
exactly-once guarantee. Only registered effect IDs bound to retained intents are
guarded by this journal. The module cannot discover unjournaled effects.

`resolve_effect(task, effectID, effectSHA, evidence_refs, decision="do_not_replay")`
records explicit Root resolution against the current effect version. The only
decision accepted is `do_not_replay`. A later version invalidates that resolution.
This allows review of continuing the retained session without replaying that
effect; it does not change unknown into confirmed or authorize a request retry.

`replay_guard(task, effectID)` always raises. Unregistered IDs are refused because
their intent is unknown. Unknown and intended registered effects refuse
automatic replay, including when execution might have happened before a crash.
Confirmed or explicitly resolved effects also cannot be automatically replayed.
The existing catalogunknownWardrobePOST is never modified or retried. The test
uses that identifier solely in a local caller journal; no network request occurs.

`authorize_resume(reportSHA, currentfreshreport)` requires both reports to be
retained inspections in this recovery store. It independently captures current
data again while holding the recovery lock. The original, supplied fresh and
recaptured reports must all have no blockers and equal `resume_projection(report)`
values. Their full report hashes naturally differ; the original reviewed report
SHA remains pinned, and the fresh report SHA is retained separately as evidence.
Canonical task/root, generation, session, exact file bytes, source state, effect
journal, inventory, profile ownership/seal and executable SHA are revalidated.
Editing a returned dictionary cannot rewrite the durable inspection or confer
authorization. The method returns `{id, sha256, record}` for a durable `review`
record, with `scope="review_receipt_only"` and `external_effect_replay=false`, plus
the unknown-coverage operator review digest and explicit coverage fields.
The integrated optional CLI/SDK reviewed route calls this API, rechecks liveness
and evidence, and takes the launch ownership lock. It requires the retained report
SHA and explicit effect review; no implicit approval or unknown-effect replay is
added. The backend itself never executes a resume.

`history(task=None)` returns the complete ordered journal, optionally filtered by
task. Sequence numbers are global to this recovery root. Histories never use a
cached “last task” or a mutable sequence counter.

`resume_projection(report)` returns a detached stable dictionary of task/root,
metadata, captured byte references, current source, profile/root, effect and
operator-review receipts, inbox receipts, Git evidence and implementation SHA.
It excludes report UUID/capture timestamp, owner observation data, snapshot
statistics and other report-only observation fields. It does **not** recursively
strip timestamps from retained files: their exact bytes are pinned preimages,
including receipt timestamps. Changed retained evidence still requires review.

## Inspection contents and refusal rules

The report includes `schema`, UUID `id`, `captured_at_ns`, `task`, `task_root`,
`metadata`, `owner`, `captures`, `inbox_receipts`, `current_source`, `delta`, `git`,
`profile`, `profile_root`, `effects`, `snapshot`, `implementation_sha256`,
`binding_sha256`, `blockers`, and `automatic_replay=false`. Hashes are plain
lowercase SHA-256 hex. The report digest hashes the entire dictionary using the
canonical JSON recipe above; `binding_sha256` hashes `resume_projection(report)`,
excluding volatile report observations so another inspection can compare it.

Captured references preserve original metadata, original prompt, last answer,
unfiltered log bytes, every retained task sidecar, current inbox payloads and
receipt journal, baseline, available baseline preimages, current source files,
profile manifest/seals/seeds and files under its four retained runtime directories.
Changes are derived from baseline/current SHA comparisons; this cannot attribute
changes to a particular concurrent worker. Baseline fingerprints, partial
baselines, invalid/traversing references or inconsistent preimages block review.
Preimages are copied when the baseline supplies a content digest; a baseline hash
alone cannot reconstruct missing historical bytes. Changed entries contain
`path`, `before`, `after`, and `absence_proven`; partial coverage must not be read
as proof of deletion.

The module reads owner PID and creation stamp from task metadata. A live matching
owner explicitly blocks resume. A creation mismatch reports `reused_pid`, which
is not the task owner. Missing or inaccessible proof reports `unverifiable` and
blocks. Linux hashes the raw NUL-separated command-line bytes where available.
Windows captures the executable path and queries command-line information through
the same process handle, hashing its UTF-8 representation. Protected processes or
unsupported systems declare command proof unavailable. A supplied
`owner_command_sha256` mismatch is unverifiable. No
process is signalled. Generation, session and source directory are required;
legacy tasks missing these stay inspectable but cannot use strict reviewed resume.

Receipt snapshots are parsed without repairing, acknowledging or draining them.
Unknown/attempting delivery blocks because invocation may have happened. Torn,
unrecognized or contradictory outcomes, incomplete acceptance and payload digest
mismatch also block. Delivered does not mean applied. Root resolves ambiguous
delivery through the receipt/lifecycle layer before taking a new inspection.

For a Git-backed source tree, the report captures actual read-only status and diff
commands, exit codes and raw stdout/stderr references. Commands disable optional
locks, fsmonitor, the untracked cache, external diff and text conversion; global
and system Git config are disabled only in the child environment. No index,
global config, hooks, commit or source edits are performed. The diff includes
staged/unstaged changes relative to HEAD; current-source captures cover untracked
files. A Git error (including an unborn HEAD) blocks reviewed resume honestly.

## Bounds, durability and limitations

Default snapshot bounds are 20,000 captured files, 8 MiB per file, 64 MiB total
captured file bytes and a 10-second capture budget. Oversized/unreadable/linked
files, traversal failures and exhausted bounds declare `snapshot.partial` with
reasons and block review. No truncation is presented as complete evidence. Git
stdout/stderr have separate 8 MiB bounds per stream and 5-second command timeouts;
an oversized stream is marked partial rather than published as a full digest.
Executable hashing streams all bytes, with the capture-time budget enforced.
The source scan explicitly excludes `.git`, `__pycache__`, task-state and recovery
roots; these exclusions do not imply external inventory coverage. Journal history
is read in full to preserve ordering and intent identity, with an 8 MiB bound per
record. Large histories can increase operation time and lock contention.

Every operation uses the same retained OS advisory lock inode, with a two-second
acquisition timeout. The OS releases it on process death; the module never steals
a lock, unlinks its inode or guesses that a quiet owner has died. UUID records use
exclusive creation and fsync, and a contiguous sequence/previous-digest chain is
validated under the lock. Blob publication is exclusive and existing bytes must
match their digest. Directories are fsynced where supported; Windows stdlib does
not provide directory fsync. A crash during publication may leave incomplete
bytes. Such corruption fails closed and remains preserved for explicit Root
resolution; it is never silently repaired or erased. An inspection intent is
durable before capture begins, even if no completed report follows it.

Hashes detect drift/corruption, not malicious same-user rewriting of all evidence.
Path checks and repeated captures reduce ordinary races but cannot provide an
atomic snapshot of a concurrently changing source tree/provider database or close
the interval between review and future launch. Root must quiesce writers and bind
any future capability to this receipt and fresh launch-time proof. Private recovery
storage contains exact prompts, outputs and inbox bytes, potentially secrets;
it is deliberately not a redacted activity digest or public artifact.

## Verification

```powershell
python -B -m unittest discover -s tests -p test_recovery.py -v
```

Tests compile captured source bytes directly with an isolated namespace. They do
not import helper PYC files or mutate global `sys.modules`. Owned temporary
fixtures exercise actual subprocess exit/crash/restart, live-owner and reused-PID
evidence, interprocess history races, retained unfiltered output, effect intents
and unknown POST refusal, Root resolution, malformed outcomes, changed metadata,
inbox/source/session files, executable/seal/source drift, partial snapshots,
traversal/link prevention and preserved corrupt history. Child processes are hidden
on Windows, bounded by short timeouts, and killed/waited on during cleanup. These
checks establish local recovery behavior, not provider or remote-effect success.
