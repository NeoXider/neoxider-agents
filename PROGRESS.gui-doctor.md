# Summary (TL;DR)

Doctor is complete and verified: concurrent structured probes, persistent cache-first refresh, readable limits/engine rows, and manual deep check. No commit was made.

# Checklist

- [x] Measure the required pre-change `doctor` runtime.
- [x] Profile the per-engine probes.
- [x] Investigate Claude 2.1.198 limit/usage sources.
- [x] Implement faster doctor probes and verify timing under 10 seconds.
- [x] Implement persistent cache-first/background refresh and structured doctor APIs/UI.
- [x] Add both-locale i18n, documentation, and changelog updates.
- [x] Add and run doctor shell/GUI tests.
- [x] Live-verify panel on port 8793 and stop it.

# Log

- 2026-08-12: Read the canonical neoxider-agents `SKILL.md`; no previous checkpoint existed.
- 2026-08-12: Baseline `bash ./agent.sh doctor` completed successfully in 32.637s (PowerShell stopwatch); it printed all five engines, Codex 65%/7d/reset 140h38m, and Claude local usage estimate. This is worse than the previously measured exact 25s and confirms the GUI's 25s timeout cannot succeed reliably.
- 2026-08-12: Sequential `provider-info` profile: Claude 4.959s, Codex 1.569s, Gemini 2.268s, Kimi 2.998s, opencode 1.958s. Doctor currently serializes these and repeats Codex/Claude; it also spawns Python separately for every JSON field.
- 2026-08-12: Claude 2.1.198 investigation: top-level/nested help has no usage/limit/quota command; `stats-cache.json` has historical totals only. Binary inspection confirms a real authenticated `GET /api/oauth/usage` (5s timeout) with `five_hour`/`seven_day` utilization and reset fields. The persisted access token here expired and the endpoint returned HTTP 401; refreshing would rotate auth state, so implementation will use the live endpoint only with a current token and otherwise clearly fall back to local transcript usage-so-far.
- 2026-08-12: Doctor now runs its five provider probes concurrently (one file per probe, ordered render) and emits `--json`; successful measured runtime was 8.885s, down from 32.637s. GUI timeout is 60s for the compatibility path, but the HTTP endpoint starts background refresh and returns cache immediately; snapshots persist as `gui-doctor-cache.json` beside logs.
- 2026-08-12: Live panel on port 8793: cached endpoint returned five engines in 158ms; force refresh returned in 3.4ms with `refreshing=true`. Snapshot had Codex 68%/7d/reset and Claude's clearly-labelled local estimate. Panel process PID 70740 was terminated and port 8793 confirmed free.
- 2026-08-12: Regression run passed: Python `68` tests and Bash `134/134` tests. A later final doctor timing retry could not start because Windows reported `Starting the CLR failed with HRESULT 80004005`; no retry was made to avoid machine pressure.

# Conclusions / next steps

Final Python run: `Ran 69 tests ... OK`; Bash run: `134/134 passed`. `git diff --check` found no patch whitespace errors. The final extra timing retry was skipped after a Windows CLR resource-start failure; the successful post-change timing remains 8.885s.
