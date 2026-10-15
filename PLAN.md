# Installed skill docs publication — 2026-10-15

The installed Unity/CLI Markdown corrections are complete and validated.
Current commit/push outcomes and exact remote SHAs are recorded by Root in
`C:/AI/outputs/skills-publication-final-20261015/PUBLISH_RECEIPT.json`;
verify that receipt against the remotes before treating publication as confirmed.

At this pass's snapshot, CLI `main` and `origin/main` both point to
`7d6e2341b1913984187a0f91ada8ec0d39a95dbd`; Unity both point to
`e93b01a176ea27cd3ed36e66303b620f8b8f4b60`. Root must freshly check remote
SHAs before publishing. No new worktree, branch or stash is needed.

The earlier parity receipt at
`C:/AI/outputs/parity-final-recovery-20261015/RELEASE_RECEIPT.json` records
`PUBLISHED_VERIFIED_CLEANED` at 2026-10-15T08:11:15Z for
`828c3b7136851bdc23ffaf8911ad992fdda896ea`. That is a prior release,
not the commit for this docs update. The source verification remains in
[release verification](docs/RELEASE_VERIFICATION_20261015.md), now reconciled
with that receipt. The historical PLAN snapshot below remains unchanged.

Completed work: synchronize stale CLI integration statements with the canonical
[SKILL.md](SKILL.md), qualify Unity lesson generalizations without dropping the
lessons, then validate the installed docs. Preserve unrelated tracked files,
`.opencode/` and `docs/ProviderProbe20261011.md`. Ordinary-provider enforcement
remains false/fail closed; no new runtime, billing or model-quota claim follows.

Evidence and milestone state:
`C:/AI/outputs/skills-publication-final-20261015/PLAN.md`, `REVIEW.md` and
`READY.json`. The packet records actual checks, final hashes and remaining
Root acceptance/publication steps. The Root publication receipt above supersedes
the worker's pre-publication status. Workers do not commit or push.

## Historical parity recovery snapshot — retained verbatim below

The following text is archival. Its pending steps, writer/process state,
worktree paths and Git restrictions are not active instructions for this pass.
The receipt above supersedes its pending publication/cleanup status; current
user instructions and the docs-publication state above govern this work.

---

# Parity final recovery — durable root PLAN — 2026-10-15

Goal: finish and verify Neoxider Agents, publish the accepted result as sole
NeoXider, verify remote SHA, preserve evidence and remove only this task's
branch/worktree; then resume the Unity catalog. Root owns Git and acceptance.
Source scope is ACCEPTED per Root `root_source_gate.json`
(`SOURCE_GATE_ACCEPTED` 2026-10-15T07:35:52Z, 35 modules / 1313 unique tests /
5 skips, file SHA256 `ba196679de21c3f67292758991ed04b7c4de1bf81e995fa7c9ffecb60b6c8c3b`).
Publication/push/cleanup outcomes belong to Root and will be recorded in
`C:/AI/outputs/parity-final-recovery-20261015/RELEASE_RECEIPT.json` — never
self-attested here; do not invent a future commit SHA.
Workers use the explicit disjoint scopes below; unrelated user files and global
configuration are preserved. Historical evidence is not a fresh acceptance gate.

Base: recovery worktree starts from remote `main 9b74239f...`.
Installed repo `main` is `2dd31e08...` (Root verified). Original task worktree is
absent; the original PLAN is retained at `C:/AI/work/parity-evidence-20261014/PLAN.md`
(prior phases DRAFT/TODO per that file) and linked here instead of restating stale prose.
Current canonical status doc is the final-for-source Russian
`docs/RELEASE_VERIFICATION_20261015.md`; `docs/NATIVE_PARITY_REPORT.md` is a
historical snapshot superseded by it for accepted source.
Root fetched and preserved installed unrelated `.opencode/` and
`docs/ProviderProbe20261011.md`.

Implied invariants: durable queue/journal persistence; delivery = invocation
completion with nonempty answer; acknowledgment = explicit assistant evidence
(none proves semantic application). `subscribe(callback)` ephemeral;
`subscribe(callback, consumer="stable-parent")` persists progress only after
callback success. `wait_any(handles, timeout=30)` returns settled results;
observation timeout leaves workers running. Reviewed resume refuses
stale/incomplete evidence, unknown attempts, unresolved external effects.
`run --isolate-runtime` optional for Codex/OpenCode only; reuse same profile on
resume. Credentials need explicit `--credential-env-keys`; no auth file copied.
Previous parity feature commit was already published; source scope is now
Root-accepted (bounded: this gate only, not a universal quality/speed/billing
ranking) while release publication/cleanup remain pending — do NOT claim push,
cleanup completion, or a future commit SHA. Windows public enforcement false
remains fail closed; do not claim production provider isolation sandbox.
Unsupported, explicitly: hosted-chat injection, hard billing caps, automatic
auth-file copy, automatic CAS writeback.

## Phase status (truthful, current)

| Phase | Status 2026-10-15 |
|---|---|
| 1. SDK / typed API | ACCEPTED (bounded source gate). Reviewed startup admission + retained failed-start resources + PID-reuse tests; SDK 63-test module PASS inside the full run; real SDK OpenCode Muse run `ses_ec193c869ffes41PASFzAqxeq5` done/exit 0 with result-SHA/input match and zero retained children. Interim pump-start rejection and forged-handle/RLock/prompt-filename/race history retained as history, not current state. |
| 2. Message receipts | Bounded acceptance in gate (56 receipts + 4 state fixture PASS carried in full run); reopened by any later state change. |
| 3. Parent events | Bounded acceptance in gate (71 PASS carried in full run); named-consumer replay contract stands; `wait_any` timeout leaves workers running. |
| 4. Recovery | Bounded acceptance in gate (18 PASS carried in full run); reviewed-resume SHA route opt-in; legacy manual path keeps replay risk. |
| 5. Enforced write scope | FAIL CLOSED for ordinary providers — accepted as false. Detached helper: Root gate 37 passed + 31 subtests incl. loopback control with exact-C-literal match and pinned fixture binary. Outside-AAP escape baseline rejected. |
| 6. Runtime isolation | BOUNDED component acceptance per Root `root_isolation_acceptance.json` (`BOUNDED_RUNTIME_ISOLATION_ACCEPTED`, rechecked 2026-10-15, no new provider calls): historical 2026-10-14 Codex pair — two real profiles, each initial + same-session resume done/exit 0, distinct CODEX_HOME, real file hashes verified, ack/dedupe, 14 callbacks, billing unknown. Current `runtime_profiles.py` differs ONLY by CRLF (normalized LF SHA `50db5d38…d8cc2` matches historical capture; raw current `9532b858…5c98ab`). Current 26-test fixture docs check (25 pass, 1 symlink-privilege skip) retained. NOT a fresh whole-current-SDK isolated-pair rerun, NOT a general OS sandbox; current new-SDK ordinary OpenCode live check stays separate. |
| 7. Verification/failures/costs | Bounded acceptance in gate; old 20-fail/10-error history preserved. Exit zero ≠ QA; estimates/usage ≠ billing. |
| 8. Fleet GUI | Root CLI 41 PASS, GUI 191 PASS, Node chat/tree + Fleet 12 groups PASS, fixture browser reload/reselect exactly 1 event (`fleet-replay.png`); narrow default viewport + server-disconnect traces documented, no responsive/visible-terminal-window guarantee. Fixture QA only. |
| 9. Final integration/publication | Source gate ACCEPTED; push/remote verification + own-worktree cleanup ALL pending Root. No push, no cleanup. Authoritative outcome: `C:/AI/outputs/parity-final-recovery-20261015/RELEASE_RECEIPT.json`. |

## Writer map — RETIRED 2026-10-15 (no active code writers)

Source gate accepted; no code-writing workers remain. Former owners
(`parity-sdk-admission-20261015` on `orchestrator.py` + `test_orchestrator_api.py`,
`parity-profile-release-20261015` on `windows_enforcement.py` + `test_enforcement.py`,
`parity-route-20261015` probe) are closed; their evidence dirs
(`C:/AI/outputs/parity-final-recovery-20261015/sdk`, `/security`) are retained
as history. Doc/source scope-compliance packets from workers are NOT accepted
(they violated the read-only-Git restriction); Root independently reviewed the
actual source/diffs/tests. Do NOT run source tests concurrently with any future
writer; do NOT run Git at all (including status/diff) — Root owns all Git and
reads real diffs itself.

## Acceptance (all required before release)

1. Source review: read every diff of both active workers; resolve foreign-handle,
   spawn-after-close, LocalFree-text, and enforcement fail-closed findings.
2. Meaningful tests: real fixture/API tests for changed files, post-change;
   no concurrent source-test runs with the workers.
3. Final audit: independent adversarial + Windows escape/security gates green.
4. Docs: `NATIVE_PARITY_REPORT.md` moved off DRAFT by Root with fresh evidence links.
5. Push/remote verification: sole-author commit, push, remote SHA verified.
6. Own worktree cleanup: only this recovery worktree/branch, after verified
   publication and artifact preservation.
7. Unity work resumes ONLY after wrapper accepted; no Unity publisher/worker active.

Prior receipts: `C:/AI/outputs/neoxider-parity-core-20261014/`,
`C:/AI/outputs/neoxider-parity-final-gates-20261014/`, other
`C:/AI/outputs/neoxider-parity-*-20261014/` dirs. Recovery reconciliation dir
`C:/AI/outputs/parity-final-recovery-20261015/reconciliation` was absent at PLAN
write time. Historical failures in prior PLAN + DRAFT report are preserved, not erased.

## Milestone / next action

Milestone: durable root PLAN restored; two scoped workers active on disjoint files;
no release claims made. Next: Root reviews worker diffs on completion, runs
sequential meaningful tests, then final audit → docs → push/remote-verify →
own-worktree cleanup.

### Root verification milestone — 2026-10-15

Root read the full profile-pointer source/test diff. Independent Root run:
`python -B -m pytest tests/test_enforcement.py -v` with the retained pinned
`suite-fixture.exe` completed exit 0: **37 passed, 31 subtests passed**, 23.54 s.
This includes native child/escape, cleanup faults and loopback controls;
logs/JUnit are in `C:/AI/outputs/parity-final-recovery-20261015/security/`.
Public provider enforcement remains false; no generic sandbox claim follows.
SDK worker is still active; no recovery commit or push has occurred.

Root reviewed the SDK interim diff and rejected the remaining pump-start gap:
`_children.append` under lock followed by `thread.start` outside it lets close
join an unstarted pump; start failure also needs explicit child/prompt cleanup.
Named worker received queued review message #1 (send exec19547 exited 0).
Require deterministic pump-start, pre-owner-publication and failed-start tests
before SDK acceptance. Do not launch a second SDK writer or accept the interim diff.
SDK exec44576 remains tracked; security86924 and PLAN82522 collected exit 0.
Automation now points to this recovery PLAN, preserving the Unity continuation goal.

Heartbeat 06:48 UTC: SDK first candidate suite ran 58 tests with one adoption
fixture turn-start deadline failure; isolated rerun passed, cause NOT established.
Parent/core dotted-module invocation failed import (tests-path harness error),
not accepted as green; corrected standalone parent suite subsequently passed 11.
The pump-start review receipt c66098e9db244a2aa877c3ec73af8499 was still queued,
not yet delivered at last inspection. Root does not accept the interim SDK patch.
Docs worker parity-release-docs-20261015 owns five documentation files only,
exec27523 failed TLS certificate verification before edits (files0, processes gone).
One informed same-session retry exec77682 uses unchanged TLS/profile settings.
No quota inference, certificate bypass, duplicate source writer or push occurred.

### Ownership CLOSED — supersedes all earlier writer maps

All SDK/profile writers stopped and collected; no resume of old writers, no
second SDK writer. The interim SDK pump-start rejection is history: the final
reviewed startup admission (deterministic pump-start, pre-owner-publication,
failed-start cleanup) is accepted in the source gate. Docs retry completed 0;
the five-doc proposal below replaces it with Root-corrected final text (this
edit). Root CLI 41 PASS 55.115 s, GUI 191 PASS, Node chat/tree/Fleet PASS;
browser replay of one synthetic event verified, screenshots/limits in
ROOT_GUI_VERIFICATION.md. Final integration/push/cleanup still pending; Unity
remains paused until Root publishes.

### SOURCE_GATE_ACCEPTED — 2026-10-15T07:35:52Z (Root authority)

Authority: `C:/AI/outputs/parity-final-recovery-20261015/root_source_gate.json`,
SHA256 `ba196679de21c3f67292758991ed04b7c4de1bf81e995fa7c9ffecb60b6c8c3b`,
status `SOURCE_GATE_ACCEPTED`. 35 modules, 1313 unique tests, 5 skips, 0
failures/errors — green after ONE original storage teardown error. Root fixed
ONLY `tests/test_ux_storage.py` (bounded exact-owner process drain, excludes
the in-process test runner); the affected 31 tests were rerun in 46.142 s
PASS. The original initial full run and the failed intermediate fixture
attempt are preserved — this is NOT claimed as one clean 1313 rerun. Production
code stayed exactly the same; the source-final manifest
(`c7488f46a24555d5942e7c6245063155e1c1c4c59e56349e6d502bd694035def`) changed
solely by that test file. SDK source
`a730dc79f75a814e17d3780cdedc95d33b6868bb937edd0032f721e6ee6df679`, test
source `6e836d18a2c072b9e97abab473ce25ffce65205c72b28c7cdec715223af3dce9`;
SDK 63 passed inside the full run, and the current real SDK OpenCode Muse run
(`ses_ec193c869ffes41PASFzAqxeq5`, done/exit 0, result SHA matches input
`c679af20556cb9cc9d0c35982d6dd27fcd982833835cfe978cf904e5a45adb4a`, zero
child resources, close failed/pending all empty, exact owner 10772 / provider
2176 gone per Root check) passed. Model/tool availability or task exit 0 alone
is not acceptance; this gate is.

Exact next Root release steps (Root-owned, in order): record
`C:/AI/outputs/parity-final-recovery-20261015/RELEASE_RECEIPT.json`
(authoritative for all Git side effects) → sole-author commit → push →
remote-SHA verification → preserve artifacts → remove only this recovery
worktree/branch → resume Unity catalog. No orphan speculative TODO remains:
unsupported caps are listed explicitly above; next actions are these.
