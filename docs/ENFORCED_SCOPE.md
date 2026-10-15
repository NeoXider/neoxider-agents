# Enforced write scope — Windows candidate, not accepted

Public `enforcement.capabilities().enforced_write_scope` remains **false**.
Public `prepare` and `launch` raise `EnforcementUnavailable`; no unrestricted
fallback exists. Legacy `--owns` is a declaration/diff contract. Detached native
children are a restricted helper, **not a general sandbox** for ordinary providers.

The independent [security audit](SECURITY_AUDIT.md) rejected baseline
`windows_enforcement.py` SHA256 `69d87c077bee37e26347b98d643c340966644f4c597b1e93196bee691595c9c9`:
a genuine AppContainer changed an outside ALL APPLICATION PACKAGES writable
file. Its 19 passing tests did not cover that escape. Those historical gates
are **not acceptance of the current implementation**.

## Current experimental contract

`WindowsConsoleSession.prepare(request, runtime_files, executable,
private_parent, budgets=None)` stages pinned copies and launches an x64 Windows
**detached process with pipe stdio**, a genuine per-session AppContainer package
SID, exactly one unique normal write-restricting SID, and immutable Win32k
lockdown. The restricting SID list never includes AC/AAR/Everyone as an escape
workaround. Token/package/capability/mitigation and kernel job limits are checked
while suspended, before resume. Public integration remains disabled.

Only owned copies, a newly created private desktop and the unique OS profile
receive private ACLs. The host-user SID has explicit access on those objects:
`OWNER RIGHTS` alone fails the combined token's ordinary-user access pass.
The package and restricting SID receive read/execute rights, or modify rights
on exact writable scopes, temp and profile. Their grants omit WRITE_DAC and
WRITE_OWNER. No original source, WinSta0 or Default desktop ACL is edited.

Ordinary and `CREATE_NO_WINDOW` children currently fail before main with
`0xC0000142`. Local tracing identified KERNELBASE console initialization and
ConDrv IOCTL `0x500037` returning `STATUS_ACCESS_DENIED`. Detached children run,
including a real child requesting Win32k-off whose mitigation remains enabled.
**Runtimes must explicitly support detached subprocesses. This is not evidence
that an ordinary AI provider or its tools work under enforcement.** There is no
console-security downgrade or unrestricted retry. A process created successfully
or a parent returning zero does not establish child startup or useful work.

Network and credential grants refuse. No automatic source writeback exists;
CAS writeback remains unimplemented. Only minimal OS paths and private runtime,
profile and temp paths enter the environment. A caller-selected source can
contain sensitive content; this module does not classify it.

## Staging and availability

`ScopeRequest` accepts existing exact workspace-relative files/directories.
Traversal, wildcard/device/ADS semantics, reparse points, hardlinks and special
objects are refused. Source and private-parent roots must be disjoint, including
ancestor relations. Source validation is preflight, not an OS security boundary.

Staging streams directory enumeration and native file copying. Native no-follow
handles hold ancestors and input leaves against rename/delete/write sharing;
file identity, size, link count and hash are checked. Runtime copies are hashed
again and retained through launch. Source `.pyc`/`.pyo` files are refused.
This is not an atomic multi-file snapshot against a hostile same-user host.

Defaults for `ConsoleBudgets`:

| Limit | Default |
|---|---:|
| Aggregate staged file bytes | 256 MiB |
| Entries, including directories/infrastructure | 10,000 |
| Path depth | 64 |
| Staging deadline | 60 seconds |
| Active job processes | 8 |
| Per-process committed memory | 512 MiB |
| Aggregate job committed memory | 1 GiB |

Per-file capture also caps at 128 MiB. Limits must be positive integers with
explicit upper ceilings; kernel process/memory limits plus KILL_ON_JOB_CLOSE
are installed and queried before resume. There is no CPU-time or disk-output
quota: staging limits bound preparation, not arbitrary writes into granted
directories. Filesystem calls themselves are synchronous; the staging deadline
is checked between entries/chunks, not a kernel I/O cancellation deadline.

## Lifetime and cleanup

The session exposes `pid`, `poll`, `wait`, `terminate`, `stdin`, `stdout` and
`close`. Wait holds a duplicate process handle so cancellation cannot reuse it.
A duplicate whose checked release fails is registered in a threadsafe native
ledger and retried by later cleanup instead of being lost; unresolved entries
stay visible in native cleanup evidence. Stdio uses an explicit two-handle
inheritance list. Termination reaches the job and descendants; a leader exit
alone does not mean pipe EOF. Job termination and job-handle closure are
independent checked attempts: a termination fault never suppresses closure
(which carries KILL_ON_JOB_CLOSE), the handle clears only on closure success,
and both errors are preserved.

`close()` independently attempts every resource, then retries failed releases
once. Failed handles/streams/profile names remain owned for explicit retry;
errors and pending steps remain in `session.evidence`. A cleanup failure does
not replace the original prepare/launch exception. Profile ownership is
registered immediately after the OS creates the profile and before SID
conversion, so a conversion failure still routes the real profile through
checked retryable deletion. Only a successfully created profile is ever
claimed; creation failure claims and deletes nothing. There is no standalone
background reaper for persistent OS failures: the caller must retain the session
and retry `close()`. Native failure cleanup also retains unresolved releases.
If prepare/launch raises while cleanup remains pending, the original exception
retains `cleanup_session` for explicit retry and `cleanup_evidence` for reporting.

Copied workspace/runtime/temp directories are deliberately retained as results
and evidence; close does not delete them. The caller owns their retention/removal.
This is separate from the SDK's 300-second process-resource lifecycle policy.
Temporary OS profiles, desktop, process/job handles and streams are closed.

## Root gate 2026-10-15 (reviewed source + real fixture run)

Root read the full profile-text source/test diff and ran
`python -B -m pytest tests/test_enforcement.py -v` with the retained pinned
fixture `C:/AI/outputs/neoxider-parity-v3-astra-20261014/suite-fixture.exe`:
**exit 0, 37 passed + 31 subtests passed, 23.54 s**, covering native
child/escape, cleanup faults and the **loopback control** (exact-C-literal
match; pinned fixture binary per `security/ROOT_VERIFICATION.md` in the
recovery evidence root)
(`C:/AI/outputs/parity-final-recovery-20261015/security/root-enforcement.log`,
JUnit `root-enforcement-junit.xml`). This supersedes the paragraph below that
excluded the loopback test — **for this loopback-control case only**; no broader
network claim follows, and public ordinary-provider enforcement stays false.

Profile-text resource ownership correction (active code, [release
verification](RELEASE_VERIFICATION_20261015.md), diff `security/fix.diff`):
`_Native.create_profile` rejects `owner=None` with `profile-owner-required`
before any OS call; profile+SID are tracked immediately after creation, before
conversion; success + `LocalFree` fault retains `profile-sid-text-free` for
exactly-once retry (original error authoritative, no double-free); conversion
failure releases non-null text via checked `release_resources(...,
raise_errors=False)`. Four `ProfileTextReleaseTests` cover success, fault
retain+retry-once, conversion-failure ownership and absent-owner rejection
(`verify-text-release.log`: 4 passed). Pre-fix repro `repro-old-bug.log` →
`BUG_REPRODUCED`; post-fix `verify-postfix-repro.log` → `FIXED`. This helper
coverage is part of the ACCEPTED 2026-10-15 Root source gate (35 modules /
1313 unique tests / 5 skips; authority `root_source_gate.json`); public
ordinary-provider enforcement stays false, and worker scope-compliance packets
remain not accepted — Root independently reviewed the actual source/diffs/tests.

## Verification and limits

Preserved original failures live in
`C:/AI/outputs/neoxider-parity-enforcement-20261014/`. Current source snapshots,
loader traces, independent native C controls, fixture binaries, failed and
passing logs, and hash manifests live in
`C:/AI/outputs/neoxider-parity-enforcement-final-20261014/`.

The current offline gates include:

- Actual positive allowed writes and outside AC/AAR/Everyone low-label denials,
  including detached children and a forced mitigation-downgrade attempt.
- Exact child restricting SID, package identity, no capability grants, inherited
  Win32k policy and installed job limits.
- Protected/runtime/ancestor ACL and owner mutation, cross-scope rename and
  original content/ACL preservation; hardlinks, junctions and runtime pin drift.
- Direct native window-station/desktop open attempts with successful host
  controls and unchanged host object owner/DACL fingerprints. `win32u.dll` can
  load; its presence is not an escape or proof that the syscalls work.
- Parent process/token mutation denial, stdio round trip and exclusion of a real
  inheritable outside file, child cancellation and concurrent wait/close.
- A uniquely owned local pipe broker with successful host controls. The child
  receives error 2 through its namespace even with GLOBALROOT spelling. This is
  namespace inaccessibility, **not an error-5 ACL result or exhaustive broker
  proof**. No network listener is involved in that test.
- Tiny job process/memory limits, one bounded 64 MiB allocation denial, staging
  byte/entry/depth/deadline/root failures and cleanup fault continuation/retry.
- Deterministic fault injection for the three former ownership gaps: real
  profile creation with controlled conversion/delete faults (original error
  preserved, profile retained then really deleted on retry), faulted
  wait-duplicate releases (exact duplicates retained, then released on retry),
  and permanent job-termination failure on a real owned job with a benign
  detached child (job still closes, child exits, termination error preserved).

The loopback network test below was excluded from the earlier effort at the user's
no-network instruction; it is not counted as passed or silently skipped there.
The 2026-10-15 Root gate above ran the loopback control with the retained
fixture — that case only, not a general network grant.
Compile the embedded C fixture with installed MSVC using
`tests.test_enforcement.build_windows_fixture` (import from the `tests` path),
set `AGENT_ENFORCEMENT_FIXTURE_BIN`, and run the selected unittest cases. Without
a real configured fixture native tests skip and are not security acceptance.

Root approved the detached-process/pipe-stdio-only contract at final freeze.
That design approval does not constitute independent security acceptance.
Independent review of this changed implementation, general AI-provider runtime
compatibility, exhaustive COM/RPC/GUI broker coverage, network/auth grants,
other Windows versions/ABIs and safe writeback remain unverified. No claim of
whole-provider enforcement, publication readiness or independent acceptance is
made by these helper tests.
