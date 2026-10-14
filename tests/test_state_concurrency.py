"""Real publication-lock contention: independent updates must all survive."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from neoxider_agents import state
from neoxider_agents.state import Lock, Store

CHILD = r'''
import sys, time
from pathlib import Path
from neoxider_agents.state import Store
store = Store(sys.argv[1])
gate = Path(sys.argv[2])
deadline = time.monotonic() + 10
while not gate.exists():
    if time.monotonic() > deadline: raise RuntimeError("gate timeout")
    time.sleep(.01)
for n in range(12):
    key = "worker_%s_%s" % (sys.argv[3], n)
    store.update("task", **{key: "unique-" + key})
'''


class StateConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.store = Store(self.base / "state")

    def test_multiprocess_unique_metadata_updates_preserve_every_key(self):
        self.store.update("task", original="preserved", state="running")
        gate = self.base / "go"
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        workers = [subprocess.Popen([sys.executable, "-B", "-c", CHILD, str(self.store.root),
                                     str(gate), str(index)], cwd=ROOT, env=env,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                   for index in range(4)]
        try:
            gate.touch()
            for child in workers:
                output = child.communicate(timeout=20)[0]
                self.assertEqual(child.returncode, 0, output.decode("utf-8", "replace"))
        finally:
            for child in workers:
                if child.poll() is None:
                    child.kill()
                child.communicate(timeout=5)
        data = self.store.read("task")
        self.assertEqual(data["original"], "preserved")
        for index in range(4):
            for n in range(12):
                key = "worker_%s_%s" % (index, n)
                self.assertEqual(data[key], "unique-" + key)

    def test_explicit_meta_lock_helper_and_existing_inbox_caller_lock(self):
        with Lock(self.store.path("task", ".meta")):
            start = time.monotonic()
            self.store.update_locked("task", first="1")
            self.assertLess(time.monotonic() - start, 1)
        with Lock(self.store.path("task", ".inbox")):
            self.store.update("task", second="2")
            paths = [self.store.path("task", ".inbox") / "000000000001.msg"]
            start = time.monotonic()
            self.assertEqual(self.store.enqueue_locked("task", "PAYLOAD"), 1)
            self.store.begin_delivery_locked("task", paths, "gen")
            self.store.finish_delivery_locked("task", paths, "gen", delivered=True)
            self.assertLess(time.monotonic() - start, 1)
        self.assertEqual((self.store.read("task")["first"], self.store.read("task")["second"]), ("1", "2"))

    def test_atomic_write_fsyncs_payload_before_publication(self):
        path = self.base / "queue.msg"
        events = []
        real_fsync, real_replace = os.fsync, os.replace
        def synced(fd):
            events.append("fsync")
            return real_fsync(fd)
        def replaced(source, destination):
            events.append("replace")
            return real_replace(source, destination)
        with mock.patch.object(state.os, "fsync", side_effect=synced), \
                mock.patch.object(state.os, "replace", side_effect=replaced):
            state.atomic_write(path, "DURABLE")
        self.assertLess(events.index("fsync"), events.index("replace"))
        self.assertEqual(path.read_text(), "DURABLE")

    def test_fsync_failure_does_not_publish_or_erase_prior_metadata(self):
        self.store.update("task", original="safe")
        before = self.store.path("task", ".meta").read_bytes()
        with mock.patch.object(state.os, "fsync", side_effect=OSError("injected disk failure")):
            with self.assertRaises(OSError):
                self.store.update("task", original="lost", new="never")
        self.assertEqual(self.store.path("task", ".meta").read_bytes(), before)
        self.assertEqual(list(self.store.root.glob("*.tmp.*")), [])


if __name__ == "__main__":
    unittest.main()
