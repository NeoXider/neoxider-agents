# Tests

Four suites: three offline suites with no third-party dependencies or provider credentials, plus
one manual live smoke.

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

## No third-party dependencies

The offline suites use plain Bash, Python stdlib `unittest`, and Node.js built-ins — no bats-core,
pytest, pip, npm install, or other package installation is required.
