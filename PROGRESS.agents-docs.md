# Summary (TL;DR)

Updated documentation only for provider autonomy defaults, chat-only security boundaries, and `clean` preserving live `idle` tasks. Work is complete; scoped diff checks pass and tracked docs retain CRLF working-tree endings.

# Checklist

- [x] Read existing checkpoint (none) and canonical `SKILL.md`.
- [x] Inspect the actual provider/code diff before documentation edits.
- [x] Verify current `clean` behavior and audit requested docs for stale statements.
- [x] Patch relevant documentation only.
- [x] Review diff and verify no code/tests changed by this task.

# Log

- Read `SKILL.md` completely; `PROGRESS.agents-docs.md` did not exist.
- Ran `git diff` for the named code files. Confirmed Codex defaults to `danger-full-access` with `AGENT_CODEX_SANDBOX`, Claude defaults to `--dangerously-skip-permissions` with `AGENT_CLAUDE_PERMISSION`, and both chat-only paths ignore overrides. No unstaged `agent.sh` diff was present.
- Initial status contained pre-existing changes in both providers and `tests/test_agent_sh.sh`, plus `PROGRESS.gui-expand2.md`; these are out of scope and will not be edited.
- Verified `agent.sh` directly: `clean|prune` computes `eff_state` and skips `running|idle`, so `--purge` cannot remove a live quiet task's `.log/.meta`.
- Documentation audit found stale exact defaults in `README.md`; missing new environment variables in `SKILL.md`; and relevant unattended/bridge wording in `AGENTS.md`, `GEMINI.md`, and idle-state wording in `docs/GUI.md`. `ORCHESTRATOR.md` remains accurate and needs no edit.
- Patched the six relevant docs: added the Unreleased changelog entries; documented both overrides/defaults and closed-stdin rationale; made the chat-only boundary explicitly non-overridable; corrected README's old Codex/Claude flags and live-state wording; documented `clean` skipping `idle`.
- First review: `git diff --check` passed, but `git ls-files --eol` reported mixed working-tree EOLs in the six edited tracked docs. Normalize them back to this checkout's CRLF before final verification. Concurrent unrelated work also added a `gui.py` modification; leave it untouched.
- Normalized the six edited tracked docs back to CRLF. Final `git diff --check` passed; `git ls-files --eol` reports `w/crlf` for all six.
- Final stale scan found only intentional mentions of `workspace-write`/`acceptEdits` as opt-down overrides, historical old behavior, or the chat-only exception. No old normal defaults or running-only clean claim remains.
- This task changed only `CHANGELOG.md`, `SKILL.md`, `README.md`, `AGENTS.md`, `GEMINI.md`, `docs/GUI.md`, and this checkpoint. Concurrent/pre-existing code, test, GUI asset, provider, and `PROGRESS.gui-expand2.md` changes were not touched.

# Conclusions / next steps

Complete. No tests were run because this is documentation-only and the user reported the code suites already green. Remaining risk is limited to prose accuracy; the statements were checked against the actual provider diff and current `agent.sh clean` implementation.
