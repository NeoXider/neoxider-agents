"""Housekeeping failures must not escape state views or destroy legacy answers."""
import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(os.environ.get("AGENT_PRUNE_TEST_ROOT", Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(ROOT))
from neoxider_agents.cli import main
from neoxider_agents.logs import prune_task
from neoxider_agents.process import hidden_kwargs
from neoxider_agents.state import Store, atomic_write


def denied(path):
    error = PermissionError(13, "[WinError 5] Access denied", str(path))
    error.winerror = 5
    return error


class HousekeepingTests(unittest.TestCase):
    def setUp(self):
        base = Path(os.environ.get("AGENT_CLI_LOGS", "D:/Temp/prune-fix/state")).parent
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "state")
        env = patch.dict(os.environ, AGENT_CLI_LOGS=self.store.root.as_posix(),
                         AGENT_LOG_TTL_HOURS="0", AGENT_PARENT="", AGENT_ORCHESTRATOR_ID="")
        env.start()
        self.addCleanup(env.stop)
        self.seed("old")

    def seed(self, name):
        atomic_write(self.store.path(name, ".meta"), "state=done\nexit=0\ncore_version=1\n")
        atomic_write(self.store.path(name, ".log"), "---------- output ----------\nANSWER\n")
        os.utime(self.store.path(name, ".log"), (0, 0))

    def invoke(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(list(args))
        self.assertNotIn("Traceback", out.getvalue() + err.getvalue())
        return code, out.getvalue() + err.getvalue()

    def test_views_skip_locked_log_and_retry(self):
        original = Path.unlink

        def unlink(path, *args, **kwargs):
            if path.suffix == ".log":
                raise denied(path)
            return original(path, *args, **kwargs)

        with patch.object(Path, "unlink", unlink):
            for args in (("list",), ("status", "old"), ("pending",), ("top", "--once"),
                         ("peek", "old"), ("last", "old"), ("result", "old", "--json"),
                         ("wait", "old")):
                with self.subTest(command=args):
                    self.assertEqual(self.invoke(*args)[0], 0)
            self.assertTrue(self.store.path("old", ".log").exists())
            self.assertEqual(self.store.path("old", ".answer").read_text(), "ANSWER\n")
        self.assertEqual(self.invoke("list")[0], 0)
        self.assertFalse(self.store.path("old", ".log").exists())

    def test_failed_migration_keeps_only_copy_and_retries(self):
        original = Path.open

        def open_file(path, *args, **kwargs):
            if ".migration." in path.name:
                raise denied(path)
            return original(path, *args, **kwargs)

        with patch.object(Path, "open", open_file):
            self.assertEqual(self.invoke("list")[0], 0)
            self.assertFalse(self.store.path("old", ".answer").exists())
            self.assertTrue(self.store.path("old", ".log").exists())
            code, text = self.invoke("clean", "--purge")
            self.assertEqual(code, 1)
            self.assertIn("could not remove", text)
            self.assertTrue(self.store.path("old", ".log").exists())
            self.assertTrue(self.store.path("old", ".meta").exists())
        self.assertTrue(prune_task(self.store, "old", {"state": "done"}))
        self.assertEqual(self.store.path("old", ".answer").read_text(), "ANSWER\n")

    def test_failed_publication_does_not_publish_partial_answer(self):
        with patch("neoxider_agents.logs.os.link", side_effect=denied("answer")):
            self.assertEqual(self.invoke("status", "old")[0], 0)
            self.assertTrue(self.store.path("old", ".log").exists())
            self.assertFalse(self.store.path("old", ".answer").exists())

    def test_disappearing_migration_file_does_not_allow_purge(self):
        original = Path.open

        def open_file(path, *args, **kwargs):
            if ".migration." in path.name:
                raise FileNotFoundError(str(path))
            return original(path, *args, **kwargs)

        with patch.object(Path, "open", open_file):
            code, text = self.invoke("clean", "--purge")
            self.assertEqual(code, 1)
            self.assertIn("could not remove", text)
            self.assertTrue(self.store.path("old", ".log").exists())
            self.assertTrue(self.store.path("old", ".meta").exists())

    def test_partial_migration_failure_is_retryable(self):
        def chunks(path):
            yield "partial"
            raise denied(path)

        with patch("neoxider_agents.state.output_chunks", chunks):
            self.assertEqual(self.invoke("list")[0], 0)
            self.assertTrue(self.store.path("old", ".log").exists())
            self.assertFalse(self.store.path("old", ".answer").exists())
        self.assertEqual(self.invoke("list")[0], 0)
        self.assertEqual(self.store.path("old", ".answer").read_text(), "ANSWER\n")

    def test_frozen_change_cache_failure_is_best_effort(self):
        from neoxider_agents.reporting import file_changes
        work = Path(self.temp.name) / "work"
        work.mkdir()
        atomic_write(self.store.path("old", ".baseline.json"), '{"version":2,"files":{}}')
        with patch("neoxider_agents.reporting.atomic_write", side_effect=denied("changes")):
            self.assertEqual(file_changes(self.store, "old", str(work), freeze=True), [])

    def test_lock_release_failure_is_best_effort(self):
        from neoxider_agents.state import Lock
        lock = Lock(self.store.path("old", ".test"))
        with lock:
            with patch.object(Path, "unlink", side_effect=OSError("sharing violation")):
                # Exercise release while the owner file cannot be removed.
                lock.__exit__()
        self.assertFalse(lock.path.exists())

    def test_clean_reports_undeletable_file(self):
        path = self.store.path("old", ".md")
        path.write_text("report")
        original = Path.unlink

        def unlink(target, *args, **kwargs):
            if target == path:
                raise denied(target)
            return original(target, *args, **kwargs)

        with patch.object(Path, "unlink", unlink):
            for command in ("clean", "prune"):
                code, text = self.invoke(command)
                self.assertEqual(code, 1)
                self.assertIn("old.md", text)
                self.assertIn("1 file(s) could not be removed", text)
        self.assertTrue(path.exists())

    def test_clean_locked_raw_preserves_answer(self):
        original = Path.unlink

        def unlink(path, *args, **kwargs):
            if path.suffix == ".log":
                raise denied(path)
            return original(path, *args, **kwargs)

        with patch.object(Path, "unlink", unlink):
            code, text = self.invoke("clean", "--purge")
            self.assertEqual(code, 1)
            self.assertIn("old.log", text)
            self.assertEqual(self.store.path("old", ".answer").read_text(), "ANSWER\n")

    def test_atomic_cleanup_failure_does_not_mask_publication(self):
        with patch.object(Path, "unlink", side_effect=denied("temporary")):
            atomic_write(self.store.path("old", ".seen"), "seen")
        self.assertEqual(self.store.path("old", ".seen").read_text(), "seen")

    def test_seen_failure_does_not_fail_last(self):
        with patch.object(Path, "touch", side_effect=denied("seen")):
            self.assertEqual(self.invoke("last", "old")[0], 0)

    def test_stat_and_generic_unlink_errors_are_retryable(self):
        original_stat, original_unlink = Path.stat, Path.unlink
        log = self.store.path("old", ".log")
        for operation, original in (("stat", original_stat), ("unlink", original_unlink)):
            def fail(path, *args, **kwargs):
                if path == log:
                    raise OSError("sharing violation")
                return original(path, *args, **kwargs)

            with self.subTest(operation=operation), patch.object(Path, operation, fail):
                self.assertEqual(self.invoke("list")[0], 0)
                code, text = self.invoke("clean")
                self.assertEqual(code, 1)
                self.assertIn("old.log", text)
        self.assertEqual(self.invoke("list")[0], 0)
        self.assertFalse(log.exists())

    def test_optional_digest_and_notification_failures_are_best_effort(self):
        from neoxider_agents.lifecycle import finish_notification
        from neoxider_agents.logs import record_digest
        with patch.object(Path, "open", side_effect=denied("digest")):
            record_digest(self.store.path("old", ".activity.jsonl"), "end", "done")
        with patch("neoxider_agents.notifications.notify_task", return_value=True), \
                patch.object(self.store, "update", side_effect=denied("meta")):
            finish_notification(self.store, "old", True)

    def test_two_process_pruning_race_30_rounds(self):
        for turn in range(30):
            names = ["race-%s-%s" % (turn, index) for index in range(50)]
            for name in names:
                self.seed(name)
            processes = [subprocess.Popen([sys.executable, str(ROOT / "agent.py"), "list", "0"],
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                         **hidden_kwargs(executable=sys.executable)) for _ in range(2)]
            for process in processes:
                out, err = process.communicate(timeout=30)
                self.assertEqual(process.returncode, 0, err.decode(errors="replace"))
                self.assertNotIn(b"Traceback", out + err)
            for name in names:
                self.assertFalse(self.store.path(name, ".log").exists())
                self.assertEqual(self.store.path(name, ".answer").read_text(), "ANSWER\n")


if __name__ == "__main__":
    unittest.main()
