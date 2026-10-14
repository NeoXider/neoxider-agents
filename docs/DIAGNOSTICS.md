# Typed diagnostics (Phase 7)

`neoxider_agents.diagnostics` adds data APIs to the existing doctor module.
Lifecycle/provider failure classification and SDK/CLI result diagnostics now use
these APIs; the GUI Fleet reads retained failure/cost facts. See
[Core integration](CORE_INTEGRATION.md) for the integration boundaries. The data
APIs use Python 3.8 stdlib only and do not
launch providers, modify environment/stdout, write billing, change keys, or
raise caps. Existing doctor helpers retain their existing behavior.

## Failure evidence

`classify_failure(text, exit_code=None, watchdog=None, source="raw", trusted=False)`
returns frozen `FailureReport(category, reason, source, confidence, blocking,
exit_code, reason_truncated)`.

Categories: `transport`, `tls`, `network`, `auth`, `quota`, `model_unavailable`,
`provider_oom`, `watchdog_timeout`, `watchdog_silence`, `cancelled`, `unknown`.
Concrete OOM/TLS/network errors precede incidental auth/quota wording.
OpenCode `RangeError: Out of memory` remains `provider_oom` even with exit 126;
Muse/Space `RateLimitError` or HTTP/statusCode 429 is `quota`. A RangeError
without memory evidence stays unknown. TLS and auth are never quota.

Trust is an ingestion contract, not inferred from a model's text:

| Source | Authority |
| --- | --- |
| `provider_error` | Caller attests an already extracted provider error payload. |
| `provider_stream`, `trusted=True` | Requires a whole recognized error envelope or leading `AGENT_PROVIDER_ERROR:` marker. |
| `raw`, `model`, `tool` | Advisory `confidence="heuristic"`, `blocking=False`, including fake markers/JSON. |
| `wrapper` | Exit 130/-2 authorizes cancellation only. |

Recognized envelopes include `type=error`, `turn.failed`, `result/is_error=true`,
and top-level API `error` responses. Assistant/user/tool roles and nested
tool/item content do not authorize provider failures. Pass one isolated event,
not a concatenated mixed log. A provider stream must be trusted by the caller;
merely supplying JSON does not grant authority. Raw advisory categories are
not instructions to transition legacy task state. `blocking` means evidence
of failure, not permission to map every failure to legacy `limited`.

Caller-owned watchdog values are `timeout`, `silence`, `cancelled`; they take
precedence. Exit 124/126 alone proves no timeout or quota. Reasons preserve
wording, redacted through `activity.redact` with additional URL credential
masking; inspection is limited to 64 KiB and stored reasons to 4096 characters.
Truncation is explicit. Hashes remain full SHA256, not credential-redacted.

## Cost facts and caller policy

Frozen `CostFact` separates `catalog_estimate`, `provider_reported` (usage and/or
cost), and `confirmed_billing`. Fields: `amount` (Decimal or None), `currency`,
`provenance`, `measurement_window=(start,end)`, `input_tokens`, `output_tokens`.
Currency is a three-letter uppercase code; known amounts require it. Money
representations are limited to 128 characters and usage counts to signed-64-bit
nonnegative values. Missing
amount/usage is None; zero is a known value. Provenance is mandatory/redacted.
Window endpoints are caller-defined bounded identifiers, not inferred dates.
Facts are cumulative task/window totals, not increments. The caller must
associate observations with the correct task; these APIs cannot authenticate
a provider bill or determine task attribution from a string.

Advertised free-model cost 0 remains `catalog_estimate`; it never establishes
actual billing or known measured cost. Token counts do not synthesize cost.
Provider-reported cost remains distinct from confirmed billing.

`CostGuard(CostPolicy(caller, task, cap, currency="USD", measurement_window=...,
hard_billing_cap=False, uncertainty="fail_closed"))` stores the explicit policy.
`evaluate(facts, hard_cap_enforceable=False)` returns frozen `CostDecision`;
it does not terminate anything. The root owner later enforces stop decisions.

Only matching currency and exact nonempty policy windows count. Estimates do
not satisfy the cap. Repeated cumulative observations are not summed. Confirmed
billing is preferred over reports, with the largest amount at the same evidence
level; **any** matching reported/confirmed observation at or above the cap stops.
`evaluate` is a conservative prelaunch guard, with no postcheck mode: equality
leaves no spending headroom, including cap 0/cost 0. Decimal preserves exact edges.

For a soft cap, unknown/mismatched cost uses `uncertainty`: `fail_closed` stops,
`allow` continues with `uncertain=True`. A hard billing cap always stops on
unknown cost or unavailable enforcement, even when `uncertainty="allow"`.
A hard billing cap also requires the caller to attest
an independently enforceable boundary via `hard_cap_enforceable=True`.
Polling even a confirmed bill does not prove a hard cap can be enforced before
spending. `allow` accepts uncertainty only for soft caps.
No cap means no cost-policy stop. There is no API to implicitly raise a cap.

## Verification and bounded results

`source_hash(root, relative_paths, max_files=10000, max_bytes=536870912,
max_file_bytes=67108864)` SHA256-binds sorted relative names and exact streamed
content hashes. Limits are explicit caller parameters; exceeding any limit
raises without returning a partial hash. File-count limits include duplicate
input paths; relative names are limited to 4096 characters. It accepts regular files with exactly
one hardlink, rejects traversal/ADS paths, directories/devices/FIFOs, and rejects
observed symlinks or Windows reparse points in files and every parent component.
Path `lstat` and descriptor `fstat` bind identity, size, write/change times before
and after streaming; actual bytes read must match the initial size. Observable
replacement, mutation, or parent identity changes invalidate the hash. Every
opened descriptor closes on success and failure.
Windows path/descriptor `ctime` may have different meanings: each is compared
against its own before/after snapshot; cross-comparison uses identity, size,
link count, attributes and write time.

Where available, `O_NOFOLLOW` protects the final open component and `O_NONBLOCK`
avoids blocking on a raced FIFO. Windows has no atomic no-follow guarantee in
this stdlib implementation: it uses pre/post reparse and identity checks.
This is verification binding, not a sandbox or an atomic multi-file snapshot;
hostile change-and-restore races or edits after a file's final check cannot be
excluded. Hardlink/reparse rejection is an observed metadata policy, not proof
that an attacker cannot introduce aliases later.
The caller supplies the complete source set, including tests/config needed for
the gate, and keeps it quiescent while executing checks. Added/deleted files
require updating that set. The helper does not discover files or prevent edits.

`VerificationReceipt(command=argv_tuple, exit_code=..., result=...,
source_final_hash=..., executed_by="wrapper"|"caller"|"model_assertion",
evidence_source="external_command"|"model_assertion", check_kind="command"|"tests"|"qa")`
records the exact bounded argv, exact exit, redacted output prefix, full output
SHA256, executor/evidence and source-final hash. Oversized or credential-bearing
argv is rejected rather than silently altered. Output is bounded to 4096 chars.
Receipt creation records caller-provided evidence; it does not run a command
or authenticate fabricated evidence. External receipts must come from real
completed commands in a trusted caller/wrapper. Model assertions cannot be
upgraded by exit 0 or prose saying PASS.

`receipt.status(current_source_hash)` returns `passed`, `failed`, `not_run`,
`stale`, or `model_assertion`. Source drift invalidates an external receipt;
no exit result means not run. A command with exit 0 passes that command only.

`compact_result(existing_result, receipts=(), current_source_hash=None,
max_files=64, max_receipts=16, failure=None, cost_facts=(), cost_decision=None,
max_cost_facts=16)` returns a new bounded mapping. It retains task
state/exit, answer prefix, file delta paths/status/before+after SHA, full ordered
delta SHA, omitted counts, baseline known/partial/warning, declared scope and
explicit `scope_enforced` (default false), verification, `tests_status`,
`tests_not_run`, and `qa_status` (default `not_run`). It does not infer tests or
QA from task success. Declared ownership does not establish write enforcement.
This is a whitelist summary, not a lossless replacement for persisted results.
Explicit typed failure/cost evidence is included when supplied; Decimal cost
amounts serialize as exact strings, None remains null, and evidence kinds stay
distinct. Up to 32 cost facts may be retained with explicit omitted counts.

Delta/receipt iterables are streamed; only bounded prefixes are retained.
Status aggregation includes failures beyond the retained prefix. Per-record
memory still depends on the supplied path/output size; upstream callers should
bound individual records. Hashes cover omitted deltas; omission counts are
explicit. Hard maxima are 256 files, 64 receipts, 64 scope entries. Unknown
arbitrary result fields/history/raw logs are ignored. No source-final hash
means external receipts cannot be accepted as current.

## Offline verification

```powershell
python -B -m unittest discover -s tests -p test_diagnostics.py -v
python -B -m unittest discover -s tests -p test_diagnostics_bounds.py -v
```

Fixtures cover real subprocess exits and source drift, quoted errors, OpenCode
OOM versus quota, TLS/auth precedence, credential redaction, unknown versus zero,
estimates versus billing, immutable cap edges, hard-cap uncertainty, late failed
checks and bounded reports. No provider/network calls are needed.
