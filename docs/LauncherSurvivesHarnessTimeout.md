# Keep a worker launcher alive across harness timeouts

Observed 2026-11-25 during an 8-worker `fan` wave.

## Failure mode

A wave was started with `fan` and the coordinator attached one harness
background shell that ran `wait` for all eight tasks. The harness killed that
shell at its own timeout (~600 s). Two finished workers were safe, but the six
still-running tasks were marked `stopped` with:

- `cause=launcher died (no stop record)`
- `STOPPED ... reason=launcher stopped`

The provider sessions and partial file edits survived; only the wrapper
launcher process was gone. A later attempt to `restart` six tasks sequentially
inside ONE shell was killed the same way, stopping the already-restarted
tasks again.

## Rules

1. One tracked harness background job per blocking worker call (`run`,
   `restart`, `wait`). Never put several blocking `restart` calls in one shell.
2. Give that job a timeout comfortably longer than the expected worker
   lifetime (hours, not 10 minutes). The harness may SIGTERM a background job
   at its configured timeout even when `background: true`.
3. A `wait` timeout itself does not stop workers, but killing the shell that
   owns a launcher does. Reattach a new `wait` with a longer timeout; do not
   assume the dead shell means the work is lost.
4. Recovery: check `status NAME --detail` for `launcher died`, inspect partial
   edits, then `restart NAME` in the SAME saved session. `restart` continues
   from `git status/diff` instead of replaying the prompt.
5. For an 8-worker wave, monitor with `status 'wave-*' --detail` rather than a
   single long `wait` if the harness timeout is short.
