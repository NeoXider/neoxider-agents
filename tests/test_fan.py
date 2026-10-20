"""Focused fan scheduling tests: the OpenCode startup barrier (stdlib only)."""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from neoxider_agents import fan as fan_module
from neoxider_agents.state import Store


class FakeProcess:
    """Stand-in for the detached child process: it never runs, it only reports."""

    def __init__(self, pid):
        self.pid = pid
        self.code = None
        self.killed = False

    def poll(self):
        return self.code

    def terminate(self):
        self.killed = True
        self.code = -15


class Harness:
    """Records fan launches and fakes the child's own startup bookkeeping."""

    def __init__(self, store, engine="opencode", activity_delay=0.15, fail_first=False, expect_barrier=True,
                 activity_row=None):
        self.store = store
        self.engine = engine
        self.activity_delay = activity_delay
        self.fail_first = fail_first
        self.expect_barrier = expect_barrier
        self.activity_row = activity_row
        self.launches = []
        self.launch_times = {}
        self.activity_times = {}
        self.lock = threading.Lock()

    def popen(self, argv, **kwargs):
        name = argv[argv.index("-t") + 1]
        process = FakeProcess(os.getpid())
        with self.lock:
            self.launches.append(name)
            self.launch_times[name] = time.monotonic()
        if self.expect_barrier and len(self.launches) > 1:
            previous = self.launches[-2]
            rows = self._rows(previous)
            if not any(row.get("kind") in fan_module.READY_KINDS for row in rows):
                raise AssertionError("fan launched %s before %s produced provider activity" % (name, previous))
        if self.fail_first and len(self.launches) == 1:
            self.store.update(name, state="error", generation="gen-" + name, pid=str(process.pid),
                              engine=self.engine, core_version="2", exit="126")
            process.code = 126
            return process
        self.store.update(name, state="running", generation="gen-" + name, pid=str(process.pid),
                          engine=self.engine, core_version="2")
        thread = threading.Thread(target=self._emit, args=(name,), daemon=True)
        thread.start()
        return process

    def _rows(self, name):
        try:
            text = self.store.path(name, ".activity.jsonl").read_text(encoding="utf-8")
        except OSError:
            return []
        rows = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                rows.append(row)
        return rows

    def _emit(self, name):
        time.sleep(self.activity_delay)
        row = dict(self.activity_row or {"kind": "text", "detail": "hello from " + name})
        row["recorded_at"] = time.time()
        row["engine"] = self.engine
        try:
            with self.store.path(name, ".activity.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row) + "\n")
        except OSError:
            return
        with self.lock:
            self.activity_times[name] = time.monotonic()


def fake_preflight(store, name, opts, resume):
    return (dict(opts), None, "", "", "")


class FanBarrierTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="fan-")
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "logs")
        self.env = mock.patch.dict(os.environ, {"AGENT_FAN_STARTUP_TIMEOUT_SEC": "5"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def run_fan(self, harness, engine, prompts=("a", "b", "c")):
        with mock.patch.object(fan_module, "preflight", fake_preflight), \
                mock.patch.object(fan_module.subprocess, "Popen", harness.popen):
            return fan_module.fan(self.store, "wave", list(prompts),
                                  {"engine": engine, "dir": str(self.temp.name)})

    def test_next_opencode_launch_waits_for_fresh_activity(self):
        harness = Harness(self.store, engine="opencode")
        self.run_fan(harness, "opencode")
        self.assertEqual(harness.launches, ["wave-01", "wave-02", "wave-03"])
        for index in range(len(harness.launches) - 1):
            name = harness.launches[index]
            following = harness.launches[index + 1]
            self.assertIn(name, harness.activity_times)
            self.assertGreaterEqual(harness.launch_times[following], harness.activity_times[name],
                                    "%s was launched before %s produced provider activity" % (following, name))

    def test_pre_activity_failure_stops_later_launches(self):
        harness = Harness(self.store, engine="opencode", fail_first=True)
        with mock.patch.object(fan_module, "kill_tree") as kill:
            with self.assertRaises(ValueError) as caught:
                self.run_fan(harness, "opencode")
        message = str(caught.exception)
        self.assertIn("launched: wave-01", message)
        self.assertIn("not launched: wave-02, wave-03", message)
        self.assertIn("state=error", message)
        self.assertEqual(harness.launches, ["wave-01"])
        kill.assert_not_called()

    def test_process_heartbeat_row_does_not_release_the_next_launch(self):
        heartbeat = {"kind": "tool", "detail": "[opencode] activity: tool running (4 helper processes)"}
        harness = Harness(self.store, engine="opencode", activity_delay=0.1, activity_row=heartbeat)
        with mock.patch.dict(os.environ, {"AGENT_FAN_STARTUP_TIMEOUT_SEC": "1"}):
            with self.assertRaises(ValueError) as caught:
                self.run_fan(harness, "opencode")
        self.assertIn("no actual provider activity", str(caught.exception))
        self.assertEqual(harness.launches, ["wave-01"])

    def test_real_provider_tool_row_releases_the_next_launch(self):
        tool = {"kind": "tool", "detail": "bash {\"command\":\"git status --short\"}"}
        harness = Harness(self.store, engine="opencode", activity_delay=0.1, activity_row=tool)
        self.run_fan(harness, "opencode")
        self.assertEqual(harness.launches, ["wave-01", "wave-02", "wave-03"])

    def test_stale_activity_from_an_earlier_generation_is_not_ready(self):
        stale = {"recorded_at": time.time() - 3600, "engine": "opencode", "kind": "text", "detail": "old turn"}
        with self.store.path("wave-01", ".activity.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(stale) + "\n")
        harness = Harness(self.store, engine="opencode", activity_delay=30)
        with mock.patch.dict(os.environ, {"AGENT_FAN_STARTUP_TIMEOUT_SEC": "1"}):
            with self.assertRaises(ValueError) as caught:
                self.run_fan(harness, "opencode")
        self.assertIn("no actual provider activity", str(caught.exception))
        self.assertEqual(harness.launches, ["wave-01"])

    def test_invalid_timeout_never_disables_the_barrier(self):
        for raw in ("0", "-5", "abc", "nan", "inf", "  "):
            with mock.patch.dict(os.environ, {"AGENT_FAN_STARTUP_TIMEOUT_SEC": raw}):
                self.assertEqual(fan_module.startup_timeout(), fan_module.STARTUP_TIMEOUT)
        with mock.patch.dict(os.environ, {"AGENT_FAN_STARTUP_TIMEOUT_SEC": "12"}):
            self.assertEqual(fan_module.startup_timeout(), 12.0)

    def test_non_opencode_fan_launches_immediately(self):
        harness = Harness(self.store, engine="claude", activity_delay=30, expect_barrier=False)
        self.run_fan(harness, "claude")
        self.assertEqual(harness.launches, ["wave-01", "wave-02", "wave-03"])
        self.assertEqual(harness.activity_times, {})


if __name__ == "__main__":
    unittest.main()
