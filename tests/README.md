# Tests

Two independent, self-contained suites — one per language in this repo.

## Bash suite (agent.sh)

```bash
bash tests/test_agent_sh.sh
```

Sources `agent.sh` (via the harmless `list` subcommand) inside a scratch `AGENT_CLI_LOGS`
directory to exercise `meta_set`/`meta_get`/`_meta_lock`/`_meta_unlock`, the codex/claude
provider `_resolve` functions, and the default task-name shape — without invoking any real CLI
(codex/claude/etc.) and without touching the real `~/.claude/agent-cli-logs`. Prints per-test
pass/fail lines and a final `N/N passed` summary; exits non-zero if anything failed.

## Python suite (gui.py)

```bash
python tests/test_gui.py
# or:
python -m unittest tests.test_gui
# or, to run every test module under tests/:
python -m unittest discover tests
```

Imports `gui.py` by file path via `importlib` (it's a standalone script, not an importable
package) and covers `to_git_bash_path`, `eff_state`, `activity_emoji`/`topic_emoji`,
`list_locales`, and the `_serve_static` directory-traversal guard — monkeypatching
`LOGDIR`/`LOCALES_DIR` to scratch temp directories where needed so nothing reads or writes the
real `~/.claude/agent-cli-logs`, and no network port is ever bound.

## Zero dependencies

This project's "zero dependencies" philosophy extends to its tests: plain bash + Python's
built-in `unittest` only — no bats-core, no pytest, no pip/npm installs required.
