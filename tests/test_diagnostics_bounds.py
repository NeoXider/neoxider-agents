"""Offline diagnostic process deadlines and containment, not provider availability."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from neoxider_agents import diagnostics
from neoxider_agents import process, providers
from neoxider_agents.process import pid_alive, pid_stamp
from neoxider_agents.state import Store


class DiagnosticBoundsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="neoxider-health-tests-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.store = Store(self.base / "state")
        self.env = mock.patch.dict(os.environ, AGENT_DOCTOR_TIMEOUT_SEC="1.5",
                                   AGENT_DOCTOR_PROVIDER_TIMEOUT_SEC="0.65")
        self.env.start()
        self.addCleanup(self.env.stop)
        self.script = self.base / "diagnostic helper.py"
        self.script.write_text('''import json, os, subprocess, sys, time
from pathlib import Path
engine = sys.argv[1]
if engine in ("resolver", "filesystem", "network", "never"):
    while True: time.sleep(60)
if engine == "badjson":
    print("raw secret must not be returned: sk-not-a-real-secret")
elif engine == "overflow":
    print("x" * (300 * 1024))
elif engine == "failure":
    print("raw authentication error: secret fixture")
    sys.exit(7)
else:
    if engine == "orphanpipe":
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                 stdout=sys.stdout, stderr=sys.stderr)
        Path(sys.argv[2]).write_text(str(child.pid))
    print(json.dumps(dict(engine=engine, version="fixture", available=True,
                         login="ok", limits=None, note="healthy")), flush=True)
''', encoding="utf-8")

    def command(self, engine):
        return [sys.executable, str(self.script), engine, str(self.base / "child.pid")]

    def test_stuck_resolver_filesystem_network_and_provider_do_not_hide_healthy_answer(self):
        started = time.monotonic()
        rows = diagnostics.bounded_probes(["resolver", "filesystem", "network", "never", "healthy"], self.command)
        self.assertLess(time.monotonic() - started, 2.5)
        for row in rows[:-1]:
            self.assertEqual(row["state"], "timeout")
            self.assertIsNone(row["available"])
            self.assertIsNone(row["limits"])
        self.assertTrue(rows[-1]["available"])
        self.assertEqual(rows[-1]["version"], "fixture")

    def test_whole_deadline_wins_over_provider_budget(self):
        with mock.patch.dict(os.environ, AGENT_DOCTOR_TIMEOUT_SEC="0.3", AGENT_DOCTOR_PROVIDER_TIMEOUT_SEC="5"):
            started = time.monotonic()
            row = diagnostics.bounded_probes(["never"], self.command)[0]
        self.assertLess(time.monotonic() - started, 1.3)
        self.assertEqual(row["state"], "timeout")
        self.assertIn("whole", row["note"])

    def test_provider_deadline_is_independent_from_the_longer_whole_budget(self):
        with mock.patch.dict(os.environ, AGENT_DOCTOR_TIMEOUT_SEC="5", AGENT_DOCTOR_PROVIDER_TIMEOUT_SEC="0.3"):
            started = time.monotonic()
            row = diagnostics.bounded_probes(["never"], self.command)[0]
        self.assertLess(time.monotonic() - started, 1.3)
        self.assertIn("provider", row["note"])

    def test_invalid_json_nonzero_and_large_output_are_distinct_errors_without_raw_secrets(self):
        rows = diagnostics.bounded_probes(["badjson", "failure", "overflow"], self.command)
        self.assertEqual([row["state"] for row in rows], ["error"] * 3)
        for row in rows:
            self.assertIsNone(row["available"])
            self.assertNotIn("secret", json.dumps(row))
        self.assertIn("invalid JSON", rows[0]["note"])
        self.assertIn("unsuccessfully", rows[1]["note"])
        self.assertIn("output", rows[2]["note"])

    def test_successful_parent_cannot_leave_stdout_grandchild(self):
        started = time.monotonic()
        row = diagnostics.bounded_probes(["orphanpipe"], self.command)[0]
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertTrue(row["available"])
        child = int((self.base / "child.pid").read_text())
        self.assertFalse(pid_alive(child, pid_stamp(child)))

    def test_spawn_error_is_unknown_not_missing_engine(self):
        with mock.patch("neoxider_agents.process.spawn", side_effect=OSError("fixture")):
            row = diagnostics.bounded_probes(["healthy"], self.command)[0]
        self.assertEqual(diagnostics.normalize(row, "healthy")["state"], "error")
        self.assertIsNone(row["available"])

    def test_output_setup_read_failure_still_closes_and_reaps_the_owned_tree(self):
        trees = []
        original = process.spawn
        def tracked(*args, **kwargs):
            tree = original(*args, **kwargs)
            trees.append(tree)
            return tree
        with mock.patch.object(process, "spawn", side_effect=tracked), \
                mock.patch.object(providers, "diagnostic_pipe_read", side_effect=OSError("fixture read failure")):
            row = diagnostics.bounded_probes(["healthy"], self.command)[0]
        self.assertEqual(row['state'], 'error')
        self.assertIn('output read failed', row['note'])
        self.assertTrue(trees[0].process.stdout.closed)
        self.assertFalse(pid_alive(trees[0].pid, trees[0].stamp))

    def test_wait_failure_retains_unknown_reason_and_closes_stdout_before_return(self):
        import subprocess
        trees = []
        original = process.spawn
        def tracked(*args, **kwargs):
            tree = original(*args, **kwargs)
            trees.append(tree)
            wait = tree.process.wait
            calls = [0]
            def failed_once(*args, **kwargs):
                calls[0] += 1
                if calls[0] == 1:
                    raise subprocess.TimeoutExpired('fixture', .2)
                return wait(*args, **kwargs)
            tree.process.wait = failed_once
            return tree
        with mock.patch.object(process, "spawn", side_effect=tracked):
            row = diagnostics.bounded_probes(["healthy"], self.command)[0]
        self.assertEqual(row['state'], 'error')
        self.assertIn('process cleanup incomplete', row['note'])
        self.assertTrue(trees[0].process.stdout.closed)
        self.assertFalse(pid_alive(trees[0].pid, trees[0].stamp))

    def test_containment_close_failure_retries_verified_cleanup_without_losing_ownership(self):
        trees = []
        original = process.spawn
        def tracked(*args, **kwargs):
            tree = original(*args, **kwargs)
            trees.append(tree)
            close = tree.close
            calls = [0]
            def failed_once():
                calls[0] += 1
                if calls[0] == 1:
                    raise OSError('fixture containment failure')
                return close()
            tree.close = failed_once
            return tree
        with mock.patch.object(process, "spawn", side_effect=tracked):
            row = diagnostics.bounded_probes(["healthy"], self.command)[0]
        self.assertEqual(row['state'], 'error')
        self.assertIn('cleanup failed', row['note'])
        self.assertTrue(trees[0].process.stdout.closed)
        self.assertFalse(pid_alive(trees[0].pid, trees[0].stamp))

    def test_descendant_inventory_failure_does_not_bypass_tree_and_stdout_cleanup(self):
        trees = []
        original = process.spawn
        def tracked(*args, **kwargs):
            tree = original(*args, **kwargs)
            trees.append(tree)
            return tree
        target = 'neoxider_agents.windows.processes' if os.name == 'nt' else 'neoxider_agents.process._posix_rows'
        with mock.patch.object(process, "spawn", side_effect=tracked), \
                mock.patch(target, side_effect=OSError('fixture inventory failure')):
            row = diagnostics.bounded_probes(["healthy"], self.command)[0]
        self.assertEqual(row['state'], 'error')
        self.assertIn('inventory failed', row['note'])
        self.assertTrue(trees[0].process.stdout.closed)
        self.assertFalse(pid_alive(trees[0].pid, trees[0].stamp))

    def test_missing_availability_in_plugin_payload_remains_unknown(self):
        row = diagnostics.normalize(dict(engine="fixture", version="?", limits=None), "fixture")
        self.assertEqual(row["state"], "error")
        self.assertIsNone(row["available"])

    def test_render_timeout_and_missing_usage_do_not_become_false_install_or_zero_quota(self):
        unknown = diagnostics.normalize(diagnostics._unknown("slow", "timeout", "deadline"), "slow")
        healthy = diagnostics.normalize(dict(engine="codex", available=True, version="fixture", login="ok",
                                              limits={"primary": {"window_minutes": 300}}), "codex")
        text = diagnostics.render([unknown, healthy], ["codex"], False, time.time())
        self.assertIn("slow      timeout", text)
        self.assertIn("utilization unknown", text)
        self.assertNotIn("0%", text)
        self.assertIsNone(unknown["available"])

    def test_real_provider_info_entry_returns_bounded_missing_engine_result(self):
        with mock.patch.dict(os.environ, AGENT_GEMINI_BIN=str(self.base / "missing executable.exe")):
            started = time.monotonic()
            row = diagnostics.bounded_probes(["gemini"])[0]
        self.assertLess(time.monotonic() - started, 2.5)
        self.assertIsNone(row["available"])
        self.assertEqual(row["state"], "error")

    def test_real_provider_info_entry_kills_stuck_cli_version_descendant(self):
        version = self.base / "blocked version.py"
        version.write_text("import os,time\nfrom pathlib import Path\nPath(__file__).with_suffix('.pid').write_text(str(os.getpid()))\ntime.sleep(60)\n", encoding="utf-8")
        with mock.patch.dict(os.environ, AGENT_CODEX_BIN=str(version)):
            row = diagnostics.bounded_probes(["codex"])[0]
        self.assertEqual(row["state"], "timeout")
        self.assertIsNone(row["available"])
        pid = int(version.with_suffix('.pid').read_text())
        self.assertFalse(pid_alive(pid, pid_stamp(pid)))


if __name__ == "__main__":
    unittest.main()
