# Windows detached-only audit V2 — 2026-10-14

**Verdict: BLOCKED for final acceptance.** The measured detached native filesystem
controls deny the previously demonstrated outside-AppContainer write escape.
Two reproduced cleanup paths lose resource ownership after release faults;
supplemental review also establishes a termination-path cleanup contract defect.
The observed auditor launcher selected `gpt-6.1-sol`, rather than the requested
`gpt-6-astra`; this report does not satisfy the requested different-model gate.
Public/general-provider enforcement remains false and refused. No capability,
implementation, provider, SDK, UI or fallback was enabled.

## Binding and method

Reviewed all 149 lines of `enforcement.py`, 998 lines of
`windows_enforcement.py`, 826 lines of `test_enforcement.py`, and 138 lines of
`ENFORCED_SCOPE.md`; also read the baseline audit, final handoff, native controls
and related integration references. No Git calls were made.

READY SHA256: `d47e9460b0f726e7b81c1af3d37f15c6de03cce558837a6c494e2d3a7ba76182`.
Manifest SHA256: `2993adb36cda5edd9c24b79c7c19c888a66a472ebdd921f06ac7545bdc0c7b25`.
All 132 author-manifest artifacts and the four frozen source snapshots verified.
Historical evidence outside that packet was retained and not rehashed.

| Source, unchanged before/after | SHA256 |
| --- | --- |
| `neoxider_agents/enforcement.py` | `5ccd52e958ebd0bd69aaef17b36473bb59a6ef72c7ab53ae050813aa4726a566` |
| `neoxider_agents/windows_enforcement.py` | `13a042aee0ac81e9f037dfbe95572dda8662b637783aad76ce16820020215bdd` |
| `tests/test_enforcement.py` | `da33ebb7a182fb57dfe037b93632a1ebdc68520ad181bf1bca45dbe611bba6c1` |
| `docs/ENFORCED_SCOPE.md` | `a8acb17313f53286a74232e531316a634c2c650af462baaf83c3d2d068a53c1e` |

Evidence: `C:/AI/outputs/neoxider-parity-detached-audit-20261014/`.
`SECURITY_AUDIT_V2.json` binds this report, source captures, native C/EXE files,
logs, failures, cleanup observations and outcomes. SHA256 is tamper evidence,
not protection against a hostile same-user host.

Execution used Windows 11 build 26200, x64 Python 3.13.0. A source-only import
loader held no-follow/share-read ancestor and leaf handles, verified identities,
single links, stable metadata and expected hashes, then compiled those exact
captured bytes. Repository `.pyc`/`.pyo` inputs were never loaded or written.
The suite C literal was extracted from the captured test source by AST; helper C
files were hash-captured before compilation. Standard-library caches are not
repository/runtime-source acceptance evidence.

Pinned MSVC: toolset `14.44.35207`, SDK `10.0.26100.0`, absolute
`Hostx64/x64/cl.exe` and `link.exe`; build logs identify the actual compiler,
linker and libraries. Compiler SHA256:
`88c8344236a27a6e727e0a8edc49aaa2690bdc7a9464b9d18cc7abe70a9f1c0d`;
linker: `ca11e6c45debd34bf652dfe984c5360a531a005ed78bf72852330c9c2590cf0d`.
Before/after tool hashes matched. Each `*-build.json` binds its C, EXE, command,
toolchain and retained raw build log; no install or compiler fallback occurred.

## Blocking findings

### V2-1 — Medium: profile-setup failure discards failed deletion ownership

`windows_enforcement.py:612–628` creates a real OS profile, but the conversion
failure branch invokes `FreeSid` and `DeleteAppContainerProfile` directly,
ignoring their results. `prepare` has not yet assigned `self._profile` at
`:217–218`. Its later session cleanup therefore cannot recover this profile.

Independent fault injection made SID-to-string conversion fail with WinError 5
after successful profile creation and made deletion return `E_ACCESSDENIED`.
The original exception survived, but deletion was attempted only once;
`cleanup_session` and `cleanup_evidence` were absent, native pending releases
were zero, and native cleanup errors were empty. The real uniquely owned profile
directory still existed. The auditor retained its name independently and deleted
it immediately with HRESULT 0. This demonstrates lost ownership and evidence;
it does not claim that an untrusted child can manufacture this host API failure.

Evidence: `independent-results-first.json`, check
`uncaptured-profile-cleanup-fault`. Required correction: register profile/SID
ownership immediately after successful creation and route every subsequent
failure release through checked, retryable cleanup without replacing the
original exception. Also inspect the other direct cleanup calls in helper
`finally` blocks; the current tests cover only selected release paths.

### V2-2 — Medium: wait-handle release failure leaves an untracked handle

`windows_enforcement.py:357–375` correctly duplicates the process handle for
concurrent waiting, but its `finally` directly closes that duplicate. A close
failure neither registers the duplicate nor includes it in session cleanup.

After a real native process completed, injected failure of this specific close
raised `OSError`. Subsequent ordinary `session.close()` reported empty cleanup
pending/errors and released its other resources, while the duplicate remained a
valid process handle. Native pending releases were zero. The auditor retained
and closed that exact duplicate immediately. Evidence:
`independent-results-first.json`, `uncaptured-wait-handle-cleanup-fault`.
Required correction: retain the duplicate through checked release/retry and
expose unresolved ownership in cleanup evidence. This is a failure-stability
defect, not an observed write escape.

### V2-3 — Procedural: requested auditor model was not selected

Read-only observation of this audit's process ancestry found the local Codex
launcher requesting `gpt-6.1-sol`; see `author-route-observation.json`. A launcher
argument is route evidence, not service-side identity attestation. No model
probe, model change or new worker was authorized or performed. Root needs a
separate correctly routed Astra review; this report must not be relabeled as it.

### V2-4 — Blocking cleanup-all gap: termination failure prevents job closure

Supplemental independent review of the already captured source establishes that
`windows_enforcement.py:378–382` checks `TerminateJobObject` before attempting
`CloseHandle(_job)`. On either failed termination attempt, the exception prevents
job closure. `close():422–454` retries this combined action twice, rather than
attempting termination and job-handle release independently. The process-close
action waits for exit but does not terminate the job or release its handle.

For a live owned job where termination fails permanently but handle closure
would succeed, both passes unnecessarily retain the job handle and can leave
owned descendants active. The installed `KILL_ON_JOB_CLOSE` flag supplies the
available release/kill path, provided the final job handle closes; this session
does not expose an inheritable job handle. Preserving the termination error and
attempting job closure are compatible. Independent cleanup of other resources
does not satisfy the cleanup-all contract while this release is suppressed.

This is a blocking failure-path design finding. **No controlled live-job fault
injection was run and no additional active-process/handle retention was observed
in this supplement.** Root instructed that no new probes run after a sufficient
audit gate; V2-1/V2-2 had already blocked that gate. The conclusion follows from
the reviewed control flow and installed job policy, not a new native experiment.
Generic caller thread callbacks and SDK actor behavior are outside this finding.

Required correction: attempt job termination and checked job-handle closure
independently, keep unresolved handles owned, preserve both outcomes, and still
attempt all remaining cleanup. A subsequent authorized gate should inject
permanent termination failure into an actual owned job with one benign detached
child, verify the job-close attempt and child exit using captured identities,
and release only those owned resources. That native outcome remains unverified.

## Verification results and precise limits

| Gate | Result |
| --- | --- |
| Current full enforcement suite | 30 tests attempted: 28 passed, zero assertion failures/skips, two audit-harness errors; 5.633 s |
| Corrected local controls | Both affected cases passed; zero failures/errors/skips, 0.253 s. All 30 distinct current cases have passing evidence; there was no clean single 30-test invocation |
| AC/AAR/Everyone, low label | Rebuilt native parent and real detached/forced-Win32k-off children wrote 12 bytes to allowed copy; each outside open denied error 5. Exact same single restricting SID, genuine AC, no broad SID union, Win32k and job controls retained |
| Healthy native write controls | Same executable, host before/after: allowed and all three external fixtures opened/wrote successfully. Child and host comparisons preserved original source/outside ACLs and final source bytes |
| Ordinary/NO_WINDOW children | Real child creation succeeded but startup returned `0xC0000142`, no child receipt. Unsupported compatibility negative; not successful security execution |
| Desktop/NtUser | Existing Default/WinSta0 controls passed after harness correction. Supplemental actual uniquely named private desktop opened on healthy host; child direct NtUser open failed. Its reported error was 0, so do not call this an error-5 ACL denial |
| Immutable Win32k | Direct child request to turn policy off failed error 5; policy remained 1. Actual detached forced-off descendants also retained policy 1 |
| Mapping/link/token | Protected read/write open, writable mapping, protected hardlink, original-source write and outside write after `RevertToSelf` denied error 5. Protected original/copy bytes and original ACLs unchanged. No claim of exhaustive token/API escapes |
| Owner/parent/runtime/rename/stdio | Native suite passed protected/runtime/ancestor DACL and owner attacks, cross-scope/runtime rename denial, parent mutation/token duplication denial, pipe round trip, real inheritable outside-handle exclusion, child cancellation and concurrent wait/close |
| Local named-pipe broker | Healthy host controls before/after; child error 2 namespace isolation, including GLOBALROOT spelling. Not ACL-denial evidence or exhaustive broker coverage |
| Single loopback listener | One `127.0.0.1:61626` listener, healthy same-listener host controls before/after, child denial and late-traffic grace passed. Python audit recorded one bind and two host connects; no second listener or external host. The native error was asserted as 10013 or 10060, not separately retained as raw stdout |
| Staging/job bounds | Suite passed root overlap, aggregate bytes/entries, directory/depth/deadline/runtime directories, native deadline release, queried job process/memory limits and one bounded 64 MiB allocation denial. No destructive exhaustion |
| Capture/runtime pin | Hardlinks/junctions and runtime drift refused. Independent retained leaf/ancestor handles blocked rename with WinError 32 and blocked source writes; captured bytes and their hash matched |
| Cleanup | Existing stream/desktop/profile fault continuation tests passed. V2-1/V2-2 were reproduced; V2-4 is an additional static termination-path defect. Auditor-owned leaked profile/duplicate were immediately released; final owned-process/profile/desktop observations are bound separately |
| Public boundary | Capability/probe remain false; public prepare/launch refuse without starting a fallback. No actual SDK/UI/provider execution under this native boundary was accepted |

The first two suite errors were introduced by the audit's subprocess allowlist:
Windows' Python audit event supplied `executable=None`. The harness was corrected
to extract the executable from the already supplied command; only the junction
and desktop host-control cases were rerun. The loopback case was not repeated.
Original failed suite log/JSON remain intact.

The first supplemental raw private-desktop host control returned nonzero with
empty stdout. Its exact exit code was not retained, an evidence gap. The original
C/EXE and failed record remain. One informed retry initialized USER32 before raw
NtUser calls, as the existing healthy-host fixture does; separately compiled v2
host/child controls passed and their exact commands/results were retained.
No third attempt occurred. `protected_writable_mapping=False` on the host alone
is inconclusive because its file handle was read-only; the supplemental mapping
result is not used as independent proof of token isolation.

Exact gate commands, from the repository cwd:

```powershell
python -B C:/AI/outputs/neoxider-parity-detached-audit-20261014/audit_driver.py packet
python -B C:/AI/outputs/neoxider-parity-detached-audit-20261014/audit_driver.py build
python -B C:/AI/outputs/neoxider-parity-detached-audit-20261014/audit_driver.py suite
python -B C:/AI/outputs/neoxider-parity-detached-audit-20261014/audit_driver.py retry
python -B C:/AI/outputs/neoxider-parity-detached-audit-20261014/independent_checks.py
python -B C:/AI/outputs/neoxider-parity-detached-audit-20261014/independent_checks.py retry-private
python -B C:/AI/outputs/neoxider-parity-detached-audit-20261014/finalize_audit.py
```

Exit results respectively: 0, 0, 1 (audit policy), 0, 1 (host control), 0, 0.
Build scripts use the captured pinned MSVC and record their exact invocations.
No native test was skipped or excluded. Supplemental failure/fault evidence is
retained; a reproduced cleanup defect counts as a finding, not a passing product
cleanup test.

## Handoff

**CHANGED:** Only this report and the designated audit evidence directory.
Code/tests/other docs/PLAN/Git stayed read-only. No subagents, model probes,
Unity, global ACL/config/credential changes, elevation or remote network.
Owned TEMP fixtures, profiles, desktop/job/process/pipe handles were the only
verification resources; retained copied inputs/results are audit evidence.

**VERIFY OUTPUT:** Four source hashes and READY match before/after; 132 frozen
author artifacts verified; all 30 distinct native-suite cases have passing
evidence with the disclosed harness retry; supplemental native controls passed;
two independent resource-ownership faults reproduced. Final resource observations
show no owned executable, desktop or profile directory left alive.

**NOT DONE:** Fixes for V2-1/V2-2/V2-4, live-job termination-fault verification
deferred by Root's stop-after-gate instruction, and correctly routed Astra acceptance. No public
enablement, SDK/UI/general-provider acceptance, ordinary-console compatibility,
writeback, exhaustive COM/RPC/broker coverage, network/credential grants, IPv6/UDP,
other Windows versions/ABIs or hostile-host guarantees. Historical 1800-second
failure remains unbound; no retry or success reclassification was made.

**CONCERNS:** Staging deadline checks occur between synchronous I/O operations;
there is no CPU-time or execution-output disk quota. Snapshot consistency is
per-file, not an atomic multi-file transaction against a hostile same-user host.
Persistent OS release faults need retained ownership and explicit caller retry;
the two missing ownership paths and suppressed job closure invalidate the
advertised general cleanup rule.
The first supplementary host-control exit-code gap and the auditor model mismatch
are disclosed above. Do not enable ordinary providers from these native results.

**QUESTIONS:** None. Root owns implementation fixes and the next acceptance gate.
