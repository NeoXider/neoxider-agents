"""Revert individual fixes on disposable copies; each regression must fail."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from neoxider_agents.process import hidden_kwargs


def main():
    base = Path(os.environ.get("AGENT_CLI_LOGS", "D:/Temp/prune-fix/state")).parent / "proofs"
    base.mkdir(parents=True, exist_ok=True)
    cases = {
        "logs": ["test_views_skip_locked_log_and_retry", "test_partial_migration_failure_is_retryable"],
        "views": ["test_clean_reports_undeletable_file"],
        "state": ["test_atomic_cleanup_failure_does_not_mask_publication", "test_seen_failure_does_not_fail_last"],
        "reporting": ["test_frozen_change_cache_failure_is_best_effort"],
    }
    results = []
    for module, tests in cases.items():
        with tempfile.TemporaryDirectory(dir=base) as scratch:
            copy = Path(scratch)
            shutil.copytree(ROOT / "neoxider_agents", copy / "neoxider_agents", ignore=shutil.ignore_patterns("__pycache__"))
            for filename in ("agent.py", "activity.py"):
                shutil.copy2(ROOT / filename, copy / filename)
            original = subprocess.check_output(["git", "show", "ead8158:neoxider_agents/%s.py" % module], cwd=ROOT,
                                               **hidden_kwargs(executable="git"))
            (copy / "neoxider_agents" / (module + ".py")).write_bytes(original)
            for test in tests:
                env = dict(os.environ, AGENT_PRUNE_TEST_ROOT=copy.as_posix(),
                           AGENT_CLI_LOGS=(copy / "state").as_posix(), PYTHONDONTWRITEBYTECODE="1")
                result = subprocess.run([sys.executable, "-B", str(ROOT / "tests/test_prune_housekeeping.py"),
                                         "HousekeepingTests." + test, "-q"], env=env,
                                        capture_output=True, timeout=30, **hidden_kwargs(executable=sys.executable))
                caught = result.returncode != 0 and b"FAILED" in result.stderr
                (base / (test + ".txt")).write_bytes(result.stdout + result.stderr)
                results.append(dict(reverted=module, test=test, caught=caught))
    (base / "results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    print("Planted defects caught: %s/%s" % (sum(row["caught"] for row in results), len(results)))
    return 0 if all(row["caught"] for row in results) else 1


if __name__ == "__main__":
    sys.exit(main())
