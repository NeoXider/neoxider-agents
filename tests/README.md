# Tests

The Python core, native entry points, GUI and bridge use stdlib unittest fixtures. Preserved
Bash suites exercise the one-release legacy implementation; live checks are separate.

## Native core and entries

```powershell
$env:AGENT_CLI_LOGS = 'D:/Temp/agents-ux/tests/state'
python -m unittest discover -s tests -p 'test_*.py' -v
python tests/bench_agent.py --out D:/Temp/agents-ux/bench/acceptance-final.json --check
powershell.exe -NoProfile -File tests/test_powershell_entry.ps1
```

`test_agent_core.py` runs deterministic real provider subprocesses with file barriers, task inboxes,
launcher cancellation and cross-compatibility fixtures. `test_providers_core.py`,
`test_process_core.py`, `test_core_contracts.py` and `test_heartbeat_core.py` prove provider,
Windows job/process, CLI and watchdog contracts. `test_native_entries.py` removes Git Bash/WSL
from PATH and checks PowerShell/cmd UTF-8 prompts. PowerShell 7 is skipped when unavailable.

`prove_core_defects.py` and the provider, contract and heartbeat suites' `--prove-defects` modes
plant production source defects on disposable copies. Process mutation results are retained in
the Phase 2A acceptance evidence under `D:/Temp/agents-core/process/proofs/`.
`bench_agent.py --prove-budgets` plants violations of the budget
gates. All test state uses isolated scratch directories; Windows defaults are under
`D:/Temp/agents-ux/`. `check_windows.py` samples visible windows on monotonic 100 ms deadlines
and records timing gaps and overruns.

Phase 2B adds `test_ux_launch.py` (29 contracts), `test_ux_storage.py` (30),
`test_ux_tracking.py` (48), and GUI TTL/retained-answer coverage. Launch and tracking suites
accept `--prove-defects`; `python tests/prove_ux_storage.py` proves storage contracts.
GUI defect proofs are embedded in `tests/test_gui.py`: run
`python -B tests/test_gui.py RetainedGuiDefectProofTests -v` for its ten source mutations.
Use `AGENT_GUI_TEST_ROOT=D:/Temp/agents-ux/gui-proof` for isolated GUI fixtures. Other
disposable source copies and failures live under `D:/Temp/agents-ux/`. The benchmark
separately proves its budget gates.

## Frontend toast suite

```bash
node tests/test_toast.js
```

Uses only Node.js built-ins and a tiny in-memory DOM/localStorage stand-in. It covers persisted
toast-history compatibility, defensive bounds, and polling-error coalescing without a browser.

## Python suite (gui.py + openai_server.py)

```bash
python -m unittest discover tests
# or individually:
python -m unittest tests.test_gui
python -m unittest tests.test_openai_server
# or directly:
python tests/test_gui.py
python tests/test_openai_server.py
```

`test_gui.py` imports `gui.py` by file path and covers GUI state, parsing, HTTP guards, bridge
lifecycle, and diagnostics. `test_openai_server.py` covers bridge parsing, sessions, routing,
streaming, tool-call extraction, and diagnostics. They start no persistent servers, use scratch
directories where needed, and rely only on stdlib `unittest`.

## Bash suite (agent.sh)

```bash
bash tests/test_agent_sh.sh
```

Sources `legacy/agent.sh` (via the harmless `list` subcommand) inside a scratch `AGENT_CLI_LOGS`
directory to exercise metadata locking, provider contracts, watchdog/liveness behavior, shared
helpers, and waiting detection—without invoking a real agent CLI or touching the real logs. It
prints per-test results and exits non-zero on failure.

## Live smoke (manual — spends provider usage)

```bash
python tests/live_smoke_openai_server.py
```

Standalone end-to-end smoke against a real CLI subagent. It covers health/errors, completion,
continuation, tool calls, reset, expiry, streaming, and concurrency. It is not part of discovery;
run it deliberately because it requires credentials and spends provider usage.

## No third-party dependencies

The offline suites use plain Bash, Python stdlib `unittest`, and Node.js built-ins — no bats-core,
pytest, pip, npm install, or other package installation is required.
