# Tests

Three suites: two offline zero-dependency suites that need no provider credentials, plus one manual
live smoke.

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

Sources `agent.sh` (via the harmless `list` subcommand) inside a scratch `AGENT_CLI_LOGS`
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

## Zero dependencies

Plain bash + Python stdlib `unittest` only — no bats-core, no pytest, no pip/npm installs required.
