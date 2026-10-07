"""Tool keepalive proves tree scope, Windows policy and zero spawned helpers."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from neoxider_agents.heartbeat import ToolHeartbeat


class Heartbeat(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {"AGENT_OPENCODE_TOOL_KEEPALIVE": "1", "AGENT_OPENCODE_KEEPALIVE_SEC": "60"})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.time = [0]
        self.calls = []
        self.rows = [(10, 1, "opencode.exe"), (11, 10, "cmd.exe"), (12, 11, "MSBuild.exe"),
                     (13, 12, "dotnet.exe"), (20, 1, "python.exe"), (14, 10, "not-a-tool.exe")]

    def create(self, engine="opencode", windows=True):
        def snapshot():
            self.calls.append(True)
            return self.rows
        return ToolHeartbeat(engine, 10, clock=lambda: self.time[0], snapshot=snapshot, windows=windows)

    def test_counts_only_owned_whitelisted_descendants(self):
        heartbeat = self.create()
        self.time[0] = 60
        with patch("subprocess.Popen", side_effect=AssertionError("polling spawned a process")):
            self.assertEqual(heartbeat.poll(), "[opencode] activity: tool running (2 helper processes)")

    def test_provider_root_never_counts_as_a_tool(self):
        self.rows = [(10, 1, "node.exe")]
        heartbeat = self.create()
        self.time[0] = 60
        self.assertEqual(heartbeat.poll(), "")

    def test_adaptive_provider_poll_does_not_scan_between_heartbeats(self):
        heartbeat = self.create()
        for now in (0, 0.1, 1, 59.9):
            self.time[0] = now
            self.assertEqual(heartbeat.poll(), "")
        self.assertEqual(self.calls, [])
        self.time[0] = 60
        self.assertTrue(heartbeat.poll())
        self.time[0] = 61
        self.assertEqual(heartbeat.poll(), "")
        self.assertEqual(len(self.calls), 1)

    def test_environment_opt_out(self):
        os.environ["AGENT_OPENCODE_TOOL_KEEPALIVE"] = "0"
        heartbeat = self.create()
        self.time[0] = 100
        self.assertEqual(heartbeat.poll(), "")
        self.assertEqual(self.calls, [])

    def test_windows_only_and_opencode_only(self):
        for engine, windows in (("codex", True), ("opencode", False)):
            heartbeat = self.create(engine, windows)
            self.time[0] += 100
            self.assertEqual(heartbeat.poll(), "")
        self.assertEqual(self.calls, [])

    def test_interval_override(self):
        os.environ["AGENT_OPENCODE_KEEPALIVE_SEC"] = "1"
        heartbeat = self.create()
        self.time[0] = 1
        self.assertTrue(heartbeat.poll())

    def test_snapshot_failure_does_not_fake_activity(self):
        heartbeat = self.create()
        heartbeat.snapshot = lambda: (_ for _ in ()).throw(OSError("fixture"))
        self.time[0] = 60
        self.assertEqual(heartbeat.poll(), "")


def prove_defects():
    from neoxider_agents.providers import hidden_options
    base = Path("D:/Temp/agents-core/review") if os.name == "nt" else Path(tempfile.gettempdir()) / "agents-core/review"
    base.mkdir(parents=True, exist_ok=True)
    defects = [
        ('os.environ.get("AGENT_OPENCODE_TOOL_KEEPALIVE", "1") == "1"', 'True', "test_environment_opt_out"),
        ('if now - self.previous < self.period:', 'if False:', "test_adaptive_provider_poll_does_not_scan_between_heartbeats"),
        ('for pid in descendants)', 'for pid in names)', "test_counts_only_owned_whitelisted_descendants"),
    ]
    results = []
    with tempfile.TemporaryDirectory(prefix="heartbeat-mutations-", dir=base) as temporary:
        copy = Path(temporary)
        shutil.copytree(ROOT / "neoxider_agents", copy / "neoxider_agents")
        (copy / "tests").mkdir()
        shutil.copy(__file__, copy / "tests/test_heartbeat_core.py")
        target = copy / "neoxider_agents/heartbeat.py"
        original = target.read_text(encoding="utf-8")
        for before, after, selected in defects:
            if before not in original:
                raise RuntimeError("mutation no longer matches: " + before)
            target.write_text(original.replace(before, after, 1), encoding="utf-8")
            result = subprocess.run([sys.executable, str(copy / "tests/test_heartbeat_core.py"), "Heartbeat." + selected],
                                    env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"), cwd=copy,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=20, **hidden_options())
            target.write_text(original, encoding="utf-8")
            caught = result.returncode != 0 and b"FAILED" in result.stdout
            results.append(dict(test=selected, caught=caught, output=result.stdout.decode("utf-8", "replace")[-1200:]))
            print("%s %s" % ("CAUGHT" if caught else "MISSED", selected))
    (base / "heartbeat-defects.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    return 0 if all(row["caught"] for row in results) else 1


if __name__ == "__main__":
    if "--prove-defects" in sys.argv:
        sys.exit(prove_defects())
    unittest.main()
