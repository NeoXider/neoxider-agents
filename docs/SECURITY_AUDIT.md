# Independent Windows enforcement audit — 2026-10-14

**Verdict: BLOCKED for exact enforced write scope, including console-only enablement.**
The audited standard-AppContainer candidate allowed an untrusted child to change
an outside file. Public enforcement remained disabled and refused launch.
Root has subsequently reported a separate native-author fix in progress. **This
report evaluates only the baseline below; no subsequent fix is verified here.**

## Baseline and provenance

Reviewed every line of `enforcement.py`, `windows_enforcement.py`,
`test_enforcement.py`, and `ENFORCED_SCOPE.md`, plus current Core integration,
runtime-profile/recovery contracts and the relevant implementation boundaries.
The observed audit launcher requested `gpt-6-astra`, effort `high`; this is route
evidence, not independent proof of service-side model identity. No subagents or
model probes were launched by this audit. The prototype author was separate.

READY v3 packet SHA256:
`7d72d85d69399e485e9f54f7f0aa542cf07bc46600e59d4196168be1a5206ddc`.

| Audited file | SHA256 |
| --- | --- |
| `neoxider_agents/enforcement.py` | `43aaa0b10c88990b0b8b0643fd07d70764541848e3adbb02c071958a034d8ca7` |
| `neoxider_agents/windows_enforcement.py` | `69d87c077bee37e26347b98d643c340966644f4c597b1e93196bee691595c9c9` |
| `tests/test_enforcement.py` | `3ea0fa1a4ab1b65bcdbfb0fae5674d00028d5310fa0cc06025455c6fbe2841cc` |
| `docs/ENFORCED_SCOPE.md` | `021d12e83ca1afbfdfb676b9e5572af15c478fe98249ed87b5ecba8be4561d67` |

All four matched the packet at initial capture and the later hash observation
before the reported fix work. These are historical audit bindings, not a claim
about files after that work. Line references below refer to these baseline bytes.
Original native fixture EXE SHA256:
`f273f6fea8f1f5f6edc01a4e74188bbf6503d6830d019b922adcfbf236a154de`;
its C source matched
`ace9a20dbd6f9d615cec5d6a4c4d92e22c598076d24f11a73c2cd5ff6760a020`.

Evidence root: `C:/AI/outputs/neoxider-parity-astra-audit-20261014/`.
`SECURITY_AUDIT.json` binds preserved artifact hashes, outcomes and exclusions.
Raw command outputs remain in that directory; hashes are tamper-evidence, not
WORM storage or protection against a hostile same-user host.

## Findings and proposed fixes

### A1 — Critical: ambient ALL APPLICATION PACKAGES write access escapes scope

Baseline `windows_enforcement.py:138`, `:448`, `:590–594` creates a standard
AppContainer with zero capabilities and applies package-specific ACLs only to
its copies. This does not remove ambient `ALL APPLICATION PACKAGES` access.

The independently compiled `audit_native.c` opened an already-existing outside
owned fixture with this pre-established descriptor:

```text
D:P(A;;GA;;;OW)(A;;GA;;;WD)(A;;GA;;;AC)S:(ML;;NW;;;LW)
```

Actual child output:

```text
appcontainer=1 win32k=1 in_job=1
outside_open=1 outside_write=1 bytes=17 error=0
```

The fixture changed from `UNCHANGED_OUTSIDE!` to `AUDIT_CHILD_WRITE!`.
Its ACL and original source ACL fingerprints stayed unchanged. The auditor
created the external fixture before launch; no host-side replacement or child
ACL modification was needed. This is an untrusted-child escape from the claimed
exact write boundary, not a Windows vulnerability claim. The separate identical
Everyone/low-label control without `AC` denied open/write with error 5.

Evidence: `independent-results.json` → `outside-aap-write`,
`extra-audit.log` → `everyone-low-control`; native source/EXE receipts retained.
The 19 existing tests passing does not cover this ambient package-group grant.

**Proposal only:** retain public capability false. Require a boundary that also
denies ambient package-group writable objects outside declared scope, and a new
positive/negative ACL-matrix regression with real inherited children. Merely
adding another private ACL or disabling Win32k does not fix this result. Earlier
LPAC child-compatibility failures must be resolved and retested if LPAC is chosen;
this audit does not approve that design or the later author patch.

### A2 — High availability gap: no job quotas or bounded aggregate staging

`windows_enforcement.py:605–609` sets only `KILL_ON_JOB_CLOSE`. Independent native
`QueryInformationJobObject` returned `LimitFlags=8192`, process/job memory limits
zero and active-process limit zero. No CPU/process/memory budget is enforced by
this code. A hostile child can allocate or fork until external limits intervene;
host exhaustion was deliberately not attempted.

`read_nofollow`, lines 509–526, bounds one file to 128 MiB but buffers chunks and
joins them. `_source_snapshot`, lines 181–203, has no aggregate byte/file/time
budget; preparation copies the entire source. `prepare`, lines 86–116, does not
reject a private parent nested within that source, so staging can recursively
encounter its own output. The latter is a static caller-input/resource defect,
not an observed hostile-child escape.

**Proposal only:** explicit aggregate staging bounds, disjoint roots, streaming
capture, and accepted job resource limits before resume, with bounded failure
tests. Do not use destructive host-exhaustion tests as acceptance evidence.

### A3 — Medium: cleanup errors stop remaining cleanup; retention needs a contract

`windows_enforcement.py:277–300` does not independently finalize each resource.
An injected stream-close `OSError` at lines 281–283 left the process handle,
desktop, profile and SID retained; the job had already closed. Restoring the
stream and explicitly retrying cleanup deleted the profile with HRESULT 0.
This is fault-injection evidence, not proof the untrusted child can cause that
specific host exception. `CloseHandle`/`CloseDesktop` results are also ignored
at lines 396–397 and 548–549. Profile deletion errors are recorded and retryable,
but no standalone retry/reaper is registered.

Successful `close()` retained copied workspace/runtime directories. The SDK's
300-second policy is for completed process/pipe/launcher resources and explicitly
retains durable profiles and sessions; it does not automatically cover these
standalone console snapshots. There is no implicit five-minute disk cleanup.

**Proposal only:** independent `finally` cleanup, checked cleanup outcomes,
tracked bounded retry, and an explicit retained-artifact versus temporary-copy
policy. Retain Task/session/evidence and use owned, identity-checked cleanup.

### A4 — Medium contract mismatch: public seeds accept tool policy configuration

`runtime_profiles.py:126–142` rejects selected field-name patterns but omits
`tools` and `permission`. Actual `RuntimeProfiles.create` accepted and sealed:

```json
{"tools":{"bash":true},"permission":{"bash":"allow"}}
```

Destination was `config/opencode/opencode.json`; no provider was launched.
`RUNTIME_PROFILES.md` claims tool/plugin configuration is rejected, which this
accepted seed contradicts. It does not prove an OpenCode command was executed or
an OS sandbox escaped: runtime profiles explicitly provide storage isolation.

**Proposal only:** a documented schema/allowlist for seed contents and negative
tests for tool/permission controls, or an explicit narrower contract reviewed by
Root. A heuristic public-JSON check cannot prove strings contain no credentials.

## Verification already completed

| Check | Actual result and limits |
| --- | --- |
| Existing enforcement suite, pinned real fixture | 19 tests passed, zero skips; `test_enforcement.log` |
| Independent outside ACL differential | AAP-granted outside write **succeeded**; Everyone-only control denied 5 |
| Installed MSVC ABI/constant check | x64 native and ctypes sizes/constants match; `abi-receipt.json` |
| Leader exits while inherited stdout grandchild lives | Reader remained blocked after leader exit; `close()` killed grandchild and released reader in about 0.008 s; profile deletion 0 |
| Cleanup fault | Remaining objects retained on injected stream-close failure; explicit retry succeeded |
| Runtime profiles suite | 22 run: 21 passed, one SKIPPED (symlink creation WinError 1314); real junction test passed |
| Recovery selected suite | 17 passed, zero skips in selected suite; one Git-specific test deliberately excluded |
| Independent lease cleanup | Foreign generation, missing/reused stamp and live foreign PID refused; genuine exited identities accepted; Task/session retained |
| SDK cleanup checks | Two passed; actual resources released at 299 s of injected clock, measured cleanup about 0.046 s; foreign/reused identity guards passed |
| Public enforcement | `EnforcementUnavailable`, capability false, fallback not called |
| SDK unsupported enforced-scope keyword | `TypeError` before launcher creation, zero children; ordinary provider enforcement remains unsupported |

The ABI helper compared pointer/SA/SC/SX/PI/job/file-info sizes and the security,
mitigation, handle-list attributes plus suspended/hidden/Unicode creation flags
against installed Windows SDK headers. It establishes x64 ABI consistency on
this machine, not OS security acceptance. The baseline uses a 64-bit mitigation
value, explicit two-handle stdio list, suspended creation, job assignment before
resume and a private desktop. Existing gates cover inherited mitigation and
forced-off attempts; private desktop alone remains known insufficient.

Source capture holds no-follow/share-read ancestor and leaf handles during file
capture and rechecks runtime hashes while retaining handles through launch. It
does not make a multi-file source snapshot atomic against a hostile same-user
host. Runtime-profile hashes/seals and recovery receipts likewise detect drift,
not coordinated same-user rewriting. No automatic original writeback exists;
safe CAS writeback remains unimplemented.

Reviewed recovery reinspection rejects changed evidence and unresolved registered
effects. `recovery.py:391–397` always refuses automatic effect replay, including
resolved/confirmed effects. Unknown POST and explicit-resolution tests passed
without making a POST. Unknown coverage remains unknown. Strict review is opt-in;
manual legacy restart is not a generic exactly-once or no-replay guarantee.

## Exclusions, cleanup and handoff

No new probes were performed after Root requested this freeze. No implementation
or public-capability edits were made. No global security/TLS/auth policy, user
source ACL or shared desktop ACL was modified by the audit. Native security
changes were limited to uniquely owned fixtures, temporary package profiles and
private desktops. All completed audit sessions reported profile deletion 0;
last process observation showed no helper running from the owned evidence tree.
Owned copies and logs remain deliberately retained evidence.

Procedural deviation: the initial read-only inventory included `git status
--short` despite the NoGit constraint. No Git mutation occurred; subsequent
checks excluded Git and the extra-check harness prohibited Git/model CLIs.
The original operator-reported API timeout is a transport failure, **not quota**;
existing failure output was not erased or reclassified.

Not verified: Root's new fix; hostile-host resistance; all Windows versions or
32-bit ABI; exhaustive COM/RPC/named-pipe/GUI brokers; public network, IPv6/UDP,
credential grants; real AI-provider compatibility; source-writeback; forced
kernel cleanup failures; sustained memory/disk exhaustion. Runtime symlinks remain
SKIPPED for the stated privilege reason. The five-minute SDK check used a fake
clock with real resources; it was not a five-minute wall-clock soak. Complete Core
and GUI suites were not run in this audit.

**Reflection — what did we miss?** Everyone-writable denial did not test ambient
application-package grants. Token/mitigation/job presence did not prove exact
filesystem confinement or resource budgets. Leader completion did not imply pipe
EOF while descendants retained handles. Seed pinning proved accepted bytes were
unchanged, not that their policy was safe. These distinctions must become explicit
regression gates rather than acceptance assumptions.

Root next steps: review the separately authored AAP fix against this reproducer;
bind any new audit to all changed source hashes; resolve resource/cleanup/seed
contracts; rerun the complete native matrix and independent negative gates under
one security writer. Until that evidence exists, keep ordinary providers and the
console candidate unsupported for enforced scope and never fall back to an
unrestricted launch. This report supplies no all-clear recommendation.
