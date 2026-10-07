# Performance

The Python core removes Git Bash forks from state reads, polling and watchdogs. Each running task has one Python owner plus its provider tree. Windows providers start hidden, detached and suspended, join a Job Object before executing, and inherit a launcher lifetime job. Both jobs use `KILL_ON_JOB_CLOSE`; POSIX owners use a separate process group. Prompts go through a UTF-8 file, avoiding shell encoding and command-line limits.

The migration keeps the thin `agent.sh` entry point for Linux/macOS and existing scripts. PowerShell and cmd invoke Python directly. A PowerShell rewrite would add Windows-only behavior and startup overhead. Git Bash on Windows has expensive forks, has failed under memory pressure, may resolve through WSL, and makes hidden launch/tree cancellation harder to guarantee.

## Reproduce

Run from the worktree after freezing its runtime sources. The benchmark copies the core and fixture dependencies to an isolated snapshot before starting tasks; it never calls a provider service or changes the live checkout.

```powershell
python tests/bench_agent.py --out D:/Temp/agents-core/bench/core.json --check
python tests/bench_agent.py --engine legacy --root D:/Temp/agents-core/baseline `
  --baseline-timings D:/Temp/agents-core/baseline/timings.json `
  --out D:/Temp/agents-core/bench/baseline-runtime.json --repeat 1
```

The legacy root must be a separate `git archive main` snapshot. The baseline was captured from `main:agent.sh` under `D:/Temp/agents-core/baseline`; neither the live checkout nor its active scripts were used. All state and provider work directories are under `D:/Temp/agents-core/`. On other operating systems, the default scratch directory is under the system temporary directory.

Defaults are 200 completed task fixtures plus 30 real task owners/providers. Completed entries use the compatible `.meta`/`.log` layout. Each running fake provider blocks in its own directory, producing a session and partial output. The benchmark measures CLI process startup separately from warm in-process command dispatch. It records medians over five core command samples and preserves output, timings, source fingerprint and process inventory in JSON.

## Measurements

Windows, Python 3.14.5, 2026-10-07. These are observations on the development machine, not portable latency guarantees. Bash cold timings use 200 completed tasks; the final core snapshot adds 30 running tasks. Cold includes process startup; warm is direct dispatch with imports loaded. Core medians use five samples, except mutating `stop`, which uses one per path.

| Command | Bash main cold, seconds | Python core cold, seconds | Python core warm, seconds |
|---|---:|---:|---:|
| `list` | 5.400 | 0.208 | 0.048093 |
| `status finished-000` | 7.143 | 0.187 | 0.000586 |
| `pending` | 185.956 (exit 1) | 0.196 | 0.048541 |
| `last finished-000` | 0.763 | 0.155 | 0.000560 |
| `wait finished-000` | 2.850 | 0.160 | 0.001002 |
| `peek finished-000` | unavailable | 0.179 | 0.000847 |
| `result finished-000` | unavailable | 0.121 | 0.000684 |
| `send` while running | unavailable | 0.178 | 0.004816 |
| `stop` while running | unavailable | 0.253 | 0.127407 |

The `pending` exit of 1 is the archived Bash command's observed result, not a successful exit silently reclassified. Original evidence is `D:/Temp/agents-core/baseline/timings.json`, with command output alongside it. `peek`, `send`, `stop` and `result` were absent from that `main` snapshot; they cannot receive invented baseline timings.

The archived Bash run also reached 30 running fake providers. Its final native snapshot counted 200 processes (6.67 per task) and 1,816,363,008 bytes of working sets across those trees. Median owner RSS was 6,664,192 bytes. A previous sequential wave took about 150 seconds from first to last task start; the final five-task batches took 57.589 seconds. `wait --timeout 10` exceeded a bounded 100-second observation in one attempt; the final attempt hit Windows paging-file exhaustion (`WinError 1455`) before the wait process could start. Owned wrappers were stopped and no more Bash stress attempts were made. Consequently, baseline idle CPU/spawn rate is unavailable, with the failure retained in `D:/Temp/agents-core/bench/baseline-runtime.json`.

| 30 running tasks | Bash main | Final Python core |
|---|---:|---:|
| Time to start all tasks, seconds | 57.589 | 6.900 |
| Native processes including fake providers | 200 | 60 (exactly 2 per task) |
| Median owner RSS, bytes | 6,664,192 | 22,874,112 |
| Sum of tree working sets, bytes | 1,816,363,008 | 1,138,532,352 |
| Idle wait CPU, seconds / elapsed seconds | unavailable | 0.015625 / 10.03393 |
| Idle wait CPU, one core | unavailable | 0.15572% |
| Child spawns during observed wait | unavailable | 0 audited, 0 sampled (10 s; 0/min rate) |
| Empty Python startup median, seconds | unavailable | 0.048687 |
| 11 MiB log read, seconds / traced peak bytes | unavailable | 0.010521 / 198,864 |

The Python owner has higher individual RSS than Bash, while its task tree has fewer processes and a smaller summed working set. Working-set sums include shared pages and describe this fixture run, not private physical memory. The final core gate run passed all nine checks at CI margin 1, with no eager heavy imports and no surviving owned wrappers after cleanup. Its source fingerprint is `bf32c79a4331d08f5cbf212c621c0c3ceb1efb7059543f72469f954aadd67caa`; exact measurements and executable/parent/PID-generation inventory are in `D:/Temp/agents-core/bench/core-final.json`.

The first core snapshot failed two checks: 120 native processes for 30 tasks, and 1.59% CPU including final answer formatting. A native image/parent inventory proved that `CREATE_NO_WINDOW` allocated an invisible `conhost.exe` for each Python owner and provider on this machine. A flag probe showed `DETACHED_PROCESS | CREATE_NO_WINDOW` removed those hosts and still produced zero visible windows. The core now uses that combination with hidden `STARTUPINFO`; jobs and control events handle cancellation. Windows ignores `CREATE_NO_WINDOW` when detached, while explicit standard handles preserve prompt/output pipes. The opt-in terminal mode retains its process group. [Microsoft process creation flags](https://learn.microsoft.com/en-us/windows/win32/procthread/process-creation-flags).

PowerShell helpers are an executable-specific exception: Windows PowerShell5.1 exited0 without executing a simple `[Console]::WriteLine('ECHO')` when detached on this host. `hidden_kwargs(..., executable=...)` and provider `spawn` therefore use `CREATE_NO_WINDOW` plus hidden `STARTUPINFO`, without `DETACHED_PROCESS`, for `powershell.exe`/`pwsh.exe`. A real helper-output regression and an argv-propagation mutation prove that it executes. Python and the measured Codex/OpenCode provider roots retain detached launch behavior; a PowerShell helper may have an invisible console host in its own tree.

The wait benchmark reports the idle phase up to the timeout notice separately from final answer formatting, retaining both total CPU and duration for comparison. The original failure is preserved in `D:/Temp/agents-core/bench/core-initial-failed.json`; native tree/flag probes are in `bench/probe/`. Budgets were not raised.

A later compatibility audit added tracked-wait ownership. Its first benchmark regressed to 1.0827% idle CPU because it opened waiter locks for all 30 ordinary foreground tasks. The core now checks eligibility before opening those per-task locks and rechecks eligible tasks under the lock. That failure remains in `core-audit-wait-failed.json`, with a separate optional diagnostic profile in `core-wait-profile.json`; the earlier passing snapshot is `core-pre-audit-pass.json`. The final run includes registration/setup in the idle measurement and takes 0.046875 CPU seconds over 10.33599 seconds including answer formatting. The preceding pass before the PowerShell helper correction is retained as `core-pre-powershell-pass.json`.

## Budgets and measurement limits

`--check` enforces the following budgets. It prints both the nominal limit and the selected CI margin; the local acceptance run uses margin 1. CI may set `--ci-margin 3` for noisy shared hosts without changing the reported measurements.

| Check | Nominal limit |
|---|---:|
| Warm `list` with 200 completed + 30 running tasks | <0.5 s |
| Warm `status` | <0.15 s |
| Warm `pending` | <0.5 s |
| Idle `wait` observing 30 running owners | <1% of one CPU core |
| Child process creation by idle `wait` | 0 |
| Python empty process startup, median | <0.25 s |
| Peak traced allocation reading an 11 MiB log tail | <1 MiB |
| Task owner processes | 1 per task, plus provider tree |

The wait worker warms imports before measuring `time.process_time()`. Its idle boundary is the timeout notice before the 30 result blocks are formatted. A Python audit hook counts subprocess/OS spawn calls throughout the entire wait, including formatting; the benchmark also samples native descendants. The audit gives an exact zero for the core's stdlib spawn paths. Legacy descendant counts are sampled every 50 ms and are a lower bound: short-lived forks between samples may be missed. Legacy owner CPU excludes missed child CPU and is not directly equivalent to the core's in-process total.

RSS comes from the Windows process memory API (Linux uses `/proc`); the benchmark records owner/tree RSS, per-task process counts, executable parents and PID-generation stamps. Provider memory belongs to the provider tree. `python -X importtime` output is retained, and importing the CLI is checked for eager `ctypes`, `subprocess`, HTTP server and runtime modules. The final import trace is `D:/Temp/agents-core/bench/core-5p_irrcn/importtime.txt`.

State listing uses one `os.scandir` pass. State updates publish a temporary file with `os.replace`; contention is per task and never a global lock. Tail reads seek from the end, and running output uses bounded chunks and incremental offsets. The 11 MiB fixture checks both the final answer and traced peak memory.

`python tests/bench_agent.py --prove-budgets --out D:/Temp/agents-core/bench/budget-defects.json` plants a violation of each of nine budget gates. All nine must fail their expected gate; this exercises latency, CPU, process creation, startup imports, log allocation and process count without changing production sources.

Window/process lifecycle verification is separate from latency acceptance. `tests/check_windows.py` provides a 100 ms sampler for fake and real engine runs; `tests/test_process_core.py` proves hard launcher death, suspended-start cancellation, descendant cleanup and closed stdout. Performance results do not prove a real engine's availability or absence of provider-created UI.

Window sampling schedules deadlines every 100 ms rather than adding a 100 ms delay after enumeration. Reports retain actual sample gaps, snapshot durations and skipped intervals when enumeration overruns the deadline; transient windows shorter than a sampling gap can still escape observation.

The final fake-provider/PowerShell window/lifecycle suite passed 27 tests and caught 27 planted defects. Its 49 samples had a median 95.066 ms gap, a maximum 110.184 ms gap and a maximum 9.180 ms snapshot duration, with no missed intervals or new visible/console windows. Evidence is `D:/Temp/agents-core/process/windows-powershell-final.json`; real-engine availability and window evidence are reported separately.

Windows Job Objects kill descendants even when the launcher is forcibly terminated. On Linux/macOS, process groups support `stop`, SIGTERM/SIGHUP and detected stdout closure, but SIGKILL or a crash cannot run Python cleanup and does not automatically kill the provider group. A later `status`/`list` detects the orphan and `stop` cancels it. Guaranteed immediate lifetime cancellation for an arbitrary POSIX provider tree would require an external supervisor; a Linux parent-death signal alone protects only the direct child, and macOS offers no equivalent Job Object. This release does not add a supervisor process, so POSIX hard-kill lifetime parity remains a limitation.
