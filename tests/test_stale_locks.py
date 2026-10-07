"""Offline lock identity/reclamation regressions; no provider processes."""
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from neoxider_agents.process import pid_stamp
from neoxider_agents.state import Lock


class StaleLockTests(unittest.TestCase):
    def setUp(self):
        root = Path("D:/Temp/baseline-merge/lock-tests")
        root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=str(root))
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def plant(self, name, owner="999999999 stamp"):
        path = self.root / name
        directory = Path(str(path) + ".lock.d")
        directory.mkdir()
        (directory / "owner.old").write_text(owner, encoding="ascii")
        return path

    def test_stamped_dead_global_and_task_owner_recover(self):
        for name in ("ownership", "task.owner"):
            with self.subTest(name=name):
                path = self.plant(name)
                with Lock(path, timeout=0):
                    self.assertEqual(len(list(Path(str(path) + ".lock.d").glob("owner*"))), 1)
                self.assertFalse(Path(str(path) + ".lock.d").exists())

    def test_live_holder_is_not_reclaimed_and_diagnostic_names_pid(self):
        path = self.plant("ownership", "%s %s" % (os.getpid(), pid_stamp(os.getpid())))
        with self.assertRaisesRegex(ValueError, r"held by pid %s \(alive\)" % os.getpid()):
            with Lock(path, timeout=0):
                self.fail("live holder evicted")
        self.assertTrue((Path(str(path) + ".lock.d") / "owner.old").exists())

    def test_legacy_bash_token_is_not_mistaken_for_start_stamp(self):
        path = self.plant("ownership", "%s %s-42-123456" % (os.getpid(), os.getpid()))
        with self.assertRaisesRegex(ValueError, r"held by pid %s \(alive\)" % os.getpid()):
            with Lock(path, timeout=0):
                self.fail("live Bash generation evicted")

    def test_pid_reuse_stamp_is_checked(self):
        path = self.plant("task.owner", "%s wrong-start-stamp" % os.getpid())
        with Lock(path, timeout=0):
            self.assertFalse((Path(str(path) + ".lock.d") / "owner.old").exists())

    def test_new_owner_records_stamp_and_legacy_native_token_recovers(self):
        path = self.plant("ownership", "999999999 999999999-" + "a" * 32)
        with Lock(path, timeout=0) as lock:
            fields = lock.owner.read_text().split()
            self.assertEqual(fields[0], str(os.getpid()))
            self.assertEqual(fields[2], pid_stamp(os.getpid()))

    def test_dead_holder_diagnostic_when_recovery_fails(self):
        path = self.plant("ownership")
        with patch.object(Lock, "_reclaim", return_value=False):
            with self.assertRaisesRegex(ValueError, r"held by pid 999999999 \(dead\)"):
                with Lock(path, timeout=0):
                    self.fail("blocked recovery succeeded")

    def test_retired_cleanup_failure_does_not_block_recovered_lock(self):
        path = self.plant("ownership")
        with patch("neoxider_agents.state.shutil.rmtree", side_effect=PermissionError("sharing violation")):
            with Lock(path, timeout=0) as lock:
                self.assertTrue(lock.owner.exists())
        self.assertFalse(Path(str(path) + ".lock.d").exists())
        self.assertEqual(len(list(self.root.glob("ownership.lock.d.retired.*"))), 1)

    def test_two_contenders_reclaim_without_overlapping_live_owners(self):
        path = self.plant("ownership")
        barrier = threading.Barrier(2)
        observed_stale = threading.Barrier(2)
        local = threading.local()
        original_holders = Lock._holders
        state_guard = threading.Lock()
        active = [0]
        errors = []
        entries = []

        def holders(lock):
            result = original_holders(lock)
            if not getattr(local, "observed", False):
                local.observed = True
                self.assertEqual(result, [("999999999", False)])
                # Both contenders have read the same old generation before either
                # can rename it; the second must recheck after winning the guard.
                observed_stale.wait(timeout=2)
            return result

        def compete():
            try:
                barrier.wait(timeout=2)
                with Lock(path, timeout=3) as lock:
                    with state_guard:
                        active[0] += 1
                        self.assertEqual(active[0], 1)
                    self.assertTrue(lock.owner.exists())
                    time.sleep(0.04)
                    self.assertTrue(lock.owner.exists(), "second reaper moved live generation")
                    with state_guard:
                        active[0] -= 1
                        entries.append(lock.token)
            except BaseException as error:
                errors.append(error)

        threads = [threading.Thread(target=compete) for _ in range(2)]
        with patch.object(Lock, "_holders", holders):
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(5)
                self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(set(entries)), 2)
        self.assertFalse(Path(str(path) + ".lock.d").exists())

    def test_reaper_rechecks_identity_after_waiting_for_reclamation_guard(self):
        path = self.plant("ownership")
        lock = Lock(path, timeout=0)
        stale = lock._holders()
        self.assertEqual(stale, [("999999999", False)])
        (lock.path / "owner.old").write_text("%s %s" % (os.getpid(), pid_stamp(os.getpid())))
        self.assertFalse(lock._reclaim())
        self.assertTrue((lock.path / "owner.old").exists())


if __name__ == "__main__":
    unittest.main()
