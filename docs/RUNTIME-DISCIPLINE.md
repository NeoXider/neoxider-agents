# Runtime discipline

Read when workers launch Windows processes, modify a shared running application, or verify
an integration. These are operating instructions, not proof that existing launchers obey them.
Do not add a runtime audit or restart to an unrelated documentation/code task.

## Keep background work out of the user's way

- Prefer existing native tools and background execution with captured output. Do not open a
  visible terminal, browser or app merely to run a helper. An explicitly requested interactive
  window is a separate case.
- On Windows, hide each process creation boundary you control: Node `spawn`/`execFile` with
  `windowsHide: true`; Python `subprocess` with `CREATE_NO_WINDOW`; PowerShell `Start-Process`
  with `-WindowStyle Hidden`. These are boundary-specific options, not interchangeable flags.
  Do not assume a hidden parent hides its children. Inspect intermediate launchers, native
  process APIs, driver/service factories, retry and fallback paths when those are implicated.
- Keep stdout/stderr in logs and preserve cancellation/exit status. Do not swallow startup
  failures or retry through a visible launcher to make the job appear successful.
- For a visible-window complaint, collect the executable, command line, parent chain and time
  where accessible. Distinguish a console host process from an actually visible window; inspect
  window ownership/title changes during a bounded representative run. Report the observation
  window and remaining coverage, not a universal guarantee from one successful run.
- If your test disrupts the desktop, stop that test before repeating it. Terminate only processes
  belonging to this task, checking executable path and creation time as well as PID/parent PID
  (PIDs can be reused). Do not kill all Node/Python/terminal processes or close user applications.

## One owner for shared state

- Assign one coordinator for runtime configuration, credentials, installations, ports, restarts
  and releases. Workers return proposed changes or findings instead of racing on that state.
- Inspect how the application persists settings before editing its files. A live process may
  overwrite an external edit from its in-memory cache. When a restart is authorized, establish
  whether active user work can be preserved, stop the relevant writer, apply the change, start
  dependencies in the required order, then confirm settings and health after startup.
- Keep tokens in the intended private store; do not print them in prompts, logs, reports or
  release artifacts. Redact diagnostic URLs containing credentials. Respect fresh per-process
  credentials after restart instead of reusing a stale cached launch URL.
- Local patches to installed dependencies may disappear on upgrade. Record that limitation and
  an idempotent reapply/verification procedure where maintenance is part of the requested work.

## Verify the actual claim

- For model/MCP work, distinguish registration, discovery, direct tool execution and execution
  through the requested model and host application. Test the latter when that is the requirement.
  Check requested reasoning modes and image input with small observable probes; don't infer
  support from a picker label or unrelated model. Avoid exposing hidden reasoning in reports.
- For UI bugs, exercise the reported state: loading/history size, scroll position, attachments,
  overlays or animation transitions as relevant. Isolate fixture state between cases; a stale
  test setting can mimic a production defect. Use a focused regression rather than testing
  every UI behavior for every change.
- Before expensive suites, use focused checks that can catch immediate failures. Run mandatory
  repository checks once the integration is ready. Reuse unchanged results; if CI can safely
  perform expensive platform checks, track its actual final result for the tested revision.
- If verification fails, distinguish product defects from environment/fixture mismatches using
  the failure and installed versions. Fix the cause; never weaken a meaningful assertion just
  to obtain green checks. Record unavailable live verification honestly.
