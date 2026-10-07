"""Offline UX tracking acceptance and disposable production-source defect proofs."""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(os.environ.get("AGENT_UX_TEST_ROOT", Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(ROOT))
from neoxider_agents import reporting, ux_tracking as ux
from neoxider_agents.state import Store, MARK, atomic_write
from neoxider_agents.process import hidden_kwargs


class TrackingTests(unittest.TestCase):
    def setUp(self):
        base = Path("D:/Temp/agents-ux/tracking/tests") if os.name == "nt" else Path(tempfile.gettempdir()) / "agents-ux/tracking/tests"
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="tracking-", dir=str(base))
        self.base = Path(self.temp.name)
        self.work = self.base / "work"
        self.work.mkdir()
        self.store = Store(self.base / "state")
        self.git = mock.patch.object(reporting, "_git_status", return_value=None)
        self.git.start()
        self.env = mock.patch.dict(os.environ, {"AGENT_CLI_LOGS": self.store.root.as_posix(),
                                               "AGENT_PARENT": "tracking-tests", "AGENT_ORCHESTRATOR_ID": "tracking-tests"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.git.stop()
        self.temp.cleanup()

    def put(self, name, value):
        path = self.work / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")
        return path

    def seed(self, name="task", state="running", owns="", parent="tracking-tests", **fields):
        self.store.update(name, state=state, exit=0, dir=self.work.as_posix(), owns=owns, parent=parent,
                          pid=os.getpid(), started_epoch=time.time() - 30, engine="fixture", model="fake", **fields)
        reporting.begin_snapshot(self.store, name, self.work)

    def output(self, call, *args, **kwargs):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = call(*args, **kwargs)
        return code, out.getvalue(), err.getvalue()

    def test_hash_detects_same_size_same_timestamp_write(self):
        path = self.put("dirty.py", "old\n")
        self.seed()
        stamp = path.stat()
        self.put("dirty.py", "new\n")
        os.utime(str(path), ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        self.assertEqual(reporting.changed_files(self.store, "task", self.work), ["dirty.py"])

    def test_metadata_only_change_is_not_content_change(self):
        path = self.put("stable.txt", "same")
        self.seed()
        os.utime(str(path), ns=(1, 1))
        self.assertEqual(reporting.changed_files(self.store, "task", self.work), [])

    def test_baseline_preserved_across_followups(self):
        self.put("a.py", "old\n")
        self.seed()
        before = self.store.path("task", ".baseline.json").read_bytes()
        self.put("a.py", "new\n")
        reporting.begin_snapshot(self.store, "task", self.work)
        self.assertEqual(self.store.path("task", ".baseline.json").read_bytes(), before)
        self.assertEqual(reporting.changed_files(self.store, "task", self.work), ["a.py"])

    def test_new_run_force_replaces_baseline(self):
        self.put("a.py", "old\n")
        self.seed()
        self.put("a.py", "new\n")
        reporting.file_changes(self.store, "task", self.work, freeze=True)
        reporting.begin_snapshot(self.store, "task", self.work, force=True)
        self.assertEqual(reporting.changed_files(self.store, "task", self.work), [])
        self.assertFalse(self.store.path("task", ".changes.json").exists())
        self.assertEqual(len(list(self.store.path("task", ".baseline.files").iterdir())), 1)

    def test_dirty_git_start_recorded_and_diffed_against_dirty_content(self):
        self.put("dirty.py", "already dirty\n")
        with mock.patch.object(reporting, "_git_status", return_value=[" M dirty.py", "?? untracked.txt"]):
            self.seed()
        baseline = reporting.read_baseline(self.store, "task")
        self.assertEqual(baseline["git_status"], [" M dirty.py", "?? untracked.txt"])
        self.assertEqual(reporting.changed_files(self.store, "task", self.work), [])
        self.put("dirty.py", "agent dirty\nsecond line\n")
        changes = reporting.file_changes(self.store, "task", self.work)
        self.assertEqual((changes[0]["insertions"], changes[0]["deletions"]), (2, 1))

    def test_git_status_hidden_and_nul_safe(self):
        self.git.stop()
        (self.work / ".git").mkdir()
        with mock.patch.object(subprocess, "run", return_value=mock.Mock(returncode=0, stdout=b" M a b\0?? c\0")) as run:
            self.assertEqual(reporting._git_status(self.work), [" M a b", "?? c"])
        arguments, options = run.call_args
        self.assertEqual(arguments[0][-3:], ["--porcelain=v1", "-z", "--untracked-files=all"])
        self.assertEqual(options["stderr"], subprocess.DEVNULL)
        self.assertIn("creationflags", options) if os.name == "nt" else None
        self.git.start()

    def test_git_missing_is_nongit_not_error(self):
        self.git.stop()
        (self.work / ".git").mkdir()
        with mock.patch.object(subprocess, "run", side_effect=FileNotFoundError("git")):
            self.assertIsNone(reporting._git_status(self.work))
        self.git.start()

    def test_legacy_stat_baseline_remains_readable(self):
        path = self.put("old.txt", "before")
        info = path.stat()
        atomic_write(self.store.path("task", ".baseline.json"), json.dumps({"old.txt": [info.st_size, info.st_mtime_ns]}))
        self.assertEqual(reporting.changed_files(self.store, "task", self.work), [])
        self.put("old.txt", "changed")
        self.assertEqual(reporting.changed_files(self.store, "task", self.work), ["old.txt"])

    def test_missing_baseline_unknown_not_invented(self):
        self.put("a.txt", "a")
        self.assertEqual(reporting.changed_files(self.store, "legacy", self.work), [])
        self.store.update("legacy", state="done", dir=self.work.as_posix(), delta_known=0)
        data = ux.collect_result(self.store, "legacy")
        self.assertFalse(data["baseline_known"])
        self.assertEqual(data["changed_files"], [])

    def test_runtime_dirs_and_progress_are_ignored(self):
        self.seed()
        for name in (".git/index", "node_modules/x/a", ".venv/lib/a", "__pycache__/a.pyc", ".cache/a", "dist/a", "PROGRESS.task.md", ".agent-token"):
            self.put(name, "ignored")
        self.put("source.py", "yes")
        self.assertEqual(reporting.changed_files(self.store, "task", self.work), ["source.py"])

    def test_state_directory_inside_work_is_excluded(self):
        self.store = Store(self.work / "private-state")
        self.put("source.py", "before")
        self.seed()
        atomic_write(self.store.path("task", ".log"), "private")
        self.assertEqual(reporting.changed_files(self.store, "task", self.work), [])

    def test_snapshot_writes_nothing_to_work(self):
        self.put("source.py", "source")
        before = sorted(path.relative_to(self.work).as_posix() for path in self.work.rglob("*"))
        self.seed()
        after = sorted(path.relative_to(self.work).as_posix() for path in self.work.rglob("*"))
        self.assertEqual(after, before)
        self.assertFalse((self.work / "PROGRESS.task.md").exists())

    def test_baseline_text_is_bounded(self):
        self.put("small.txt", "small")
        self.put("large.txt", "a" * (2 * 1024 * 1024 + 1))
        self.seed()
        files = reporting.read_baseline(self.store, "task")["files"]
        self.assertIn("content", files["small.txt"])
        self.assertNotIn("content", files["large.txt"])

    def test_baseline_total_text_budget(self):
        self.put("a.txt", "a" * 10)
        self.put("b.txt", "b" * 10)
        with mock.patch.object(reporting, "BASELINE_TEXT_LIMIT", 15):
            self.seed()
        folder = self.store.path("task", ".baseline.files")
        self.assertLessEqual(sum(path.stat().st_size for path in folder.iterdir()), 15)

    def test_binary_files_have_unknown_line_counts(self):
        (self.work / "binary.dat").write_bytes(b"\0before")
        self.seed()
        (self.work / "binary.dat").write_bytes(b"\0after")
        data = reporting.file_changes(self.store, "task", self.work)[0]
        self.assertTrue(data["binary"])
        self.assertIsNone(data["insertions"])

    def test_symlink_hashes_link_never_target_content(self):
        import hashlib
        import stat
        from types import SimpleNamespace
        path = self.put("link.txt", "must not capture")
        original = Path.lstat
        def link_stat(item):
            info = original(item)
            return SimpleNamespace(st_mode=stat.S_IFLNK, st_size=info.st_size, st_mtime_ns=info.st_mtime_ns) if item == path else info
        with mock.patch.object(Path, "lstat", link_stat), \
                mock.patch.object(os, "readlink", return_value="../private/secret"):
            self.seed()
        entry = reporting.read_baseline(self.store, "task")["files"]["link.txt"]
        self.assertEqual(entry["type"], "symlink")
        self.assertEqual(entry["sha256"], hashlib.sha256(b"../private/secret").hexdigest())
        self.assertNotIn("content", entry)

    def test_added_deleted_modified_line_counts(self):
        self.put("delete.txt", "a\nb\n")
        self.put("modify.txt", "a\nb\n")
        self.seed()
        (self.work / "delete.txt").unlink()
        self.put("modify.txt", "a\nc\nd\n")
        self.put("add.txt", "one\ntwo\n")
        rows = {item["path"]: item for item in reporting.file_changes(self.store, "task", self.work)}
        self.assertEqual((rows["add.txt"]["status"], rows["add.txt"]["insertions"], rows["add.txt"]["deletions"]), ("added", 2, 0))
        self.assertEqual((rows["delete.txt"]["status"], rows["delete.txt"]["insertions"], rows["delete.txt"]["deletions"]), ("deleted", 0, 2))
        self.assertEqual((rows["modify.txt"]["insertions"], rows["modify.txt"]["deletions"]), (2, 1))

    def test_frozen_result_excludes_later_writer(self):
        self.put("a.py", "old\n")
        self.seed(state="done")
        self.put("a.py", "new\n")
        reporting.file_changes(self.store, "task", self.work, freeze=True)
        self.put("foreign.py", "later writer\n")
        self.assertEqual([entry["path"] for entry in ux.collect_result(self.store, "task")["changed_files"]], ["a.py"])

    def test_diff_names_is_machine_readable_while_running(self):
        self.seed()
        self.put("new.py", "a\n")
        code, out, err = self.output(ux.diff, self.store, "task", {"--names": True})
        self.assertEqual((code, out, err), (0, "new.py\n", ""))

    def test_diff_stat_contains_line_counts(self):
        self.seed()
        self.put("new.py", "a\nb\n")
        _, out, _ = self.output(ux.diff, self.store, "task", {"--stat": True})
        self.assertIn("+2 -0", out)
        self.assertIn("1 file(s) changed", out)

    def test_diff_patch_uses_start_content(self):
        self.put("dirty.py", "dirty start\n")
        self.seed()
        self.put("dirty.py", "new finish\n")
        _, out, _ = self.output(ux.diff, self.store, "task")
        self.assertIn("--- a/dirty.py", out)
        self.assertIn("-dirty start\n+new finish", out.replace("\r\n", "\n"))
        self.assertIn("concurrent writers cannot be distinguished", out)

    def test_diff_unknown_task_or_baseline_has_fix(self):
        with self.assertRaisesRegex(ValueError, "use neoxider list"):
            ux.diff(self.store, "missing")
        self.store.update("legacy", state="done")
        with self.assertRaisesRegex(ValueError, "start a new task"):
            ux.diff(self.store, "legacy")

    def test_finished_patch_refuses_later_writer_content(self):
        self.put("dirty.py", "start\n")
        self.seed(state="done")
        self.put("dirty.py", "agent end\n")
        reporting.file_changes(self.store, "task", self.work, freeze=True)
        self.put("dirty.py", "another writer\n")
        _, out, _ = self.output(ux.diff, self.store, "task")
        self.assertNotIn("+another writer", out)
        self.assertIn("text patch unavailable", out)

    def test_glob_intersection_patterns(self):
        for first, second, answer in (("src/*.py", "src/a.py", True), ("src/*.py", "src/*.cs", False),
                                      ("src/*", "docs/*", False), ("src/**", "src/tests/*.py", True),
                                      ("a[0-9].py", "a[5-7].py", True), ("a[!0-9]", "a[1-3]", False),
                                      ("a?.py", "aa.py", True), ("a?.py", "abc.py", False),
                                      ("x*", "*x", True), ("[]a]", "a", True), ("[", "[", True)):
            with self.subTest(first=first, second=second):
                self.assertEqual(ux.globs_overlap(first, second), answer)

    def test_ownership_warns_and_strict_refuses_running(self):
        self.seed("other", owns="src/*.py")
        _, _, err = self.output(ux.guard_ownership, self.store, "new", self.work, "src/tests/*.py")
        self.assertIn("ownership overlap with running task 'other'", err)
        with self.assertRaisesRegex(ValueError, "--strict-owns refused"):
            ux.guard_ownership(self.store, "new", self.work, "src/a.py", strict=True)

    def test_ownership_disjoint_patterns_are_allowed(self):
        self.seed("other", owns="src/*.py")
        self.assertEqual(ux.guard_ownership(self.store, "new", self.work, "src/*.cs,docs/**", strict=True), [])

    def test_ownership_finished_task_does_not_block(self):
        self.seed("other", state="done", owns="src/**")
        try:
            warnings = ux.guard_ownership(self.store, "new", self.work, "src/**", strict=True)
        except ValueError as error:
            self.fail("finished owner must not block: " + str(error))
        self.assertEqual(warnings, [])

    def test_ownership_nested_working_directories(self):
        nested = self.work / "src"
        nested.mkdir()
        self.seed("other", owns="src/**")
        with self.assertRaisesRegex(ValueError, "overlap"):
            ux.guard_ownership(self.store, "new", nested, "*.py", strict=True)

    def test_ownership_same_glob_different_dirs_is_disjoint(self):
        self.seed("other", owns="*.py")
        foreign = self.base / "foreign"
        foreign.mkdir()
        try:
            warnings = ux.guard_ownership(self.store, "new", foreign, "*.py", strict=True)
        except ValueError as error:
            self.fail("disjoint directories must not overlap: " + str(error))
        self.assertEqual(warnings, [])

    def test_overlap_warning_for_concurrent_shared_file(self):
        self.put("shared.py", "old\n")
        self.seed("one", owns="one.py,shared.py")
        self.seed("two", owns="two.py,shared.py")
        self.put("one.py", "one\n")
        self.put("shared.py", "new\n")
        _, _, err = self.output(ux.diff, self.store, "one", {"--names": True})
        self.assertIn("task 'two' also changed", err)
        self.assertIn("shared.py", err)
        self.assertIn("authorship is ambiguous", err)

    def test_overlap_excludes_disjoint_task_lifetimes(self):
        self.put("shared.py", "old\n")
        self.seed("one", state="done", task_started_epoch=100, finished_epoch=200)
        self.seed("two", state="done", task_started_epoch=300, finished_epoch=400)
        self.put("shared.py", "new\n")
        self.assertEqual(ux.overlaps(self.store, "one", ["shared.py"]), [])

    def test_declared_scope_excludes_disjoint_workers_changes(self):
        self.put("one.py", "old one\n")
        self.put("two.py", "old two\n")
        self.seed("one", owns="one.py")
        self.seed("two", owns="two.py")
        self.put("one.py", "new one\n")
        self.put("two.py", "new two\n")
        self.assertEqual(reporting.changed_files(self.store, "one", self.work), ["one.py"])
        self.assertEqual([entry["path"] for entry in reporting.file_changes(self.store, "two", self.work)], ["two.py"])
        _, out, err = self.output(ux.diff, self.store, "one", {"--names": True})
        self.assertEqual((out, err), ("one.py\n", ""))
        rows = {entry["name"]: entry for entry in ux.dashboard_data(self.store)}
        self.assertEqual((rows["one"]["files_changed"], rows["two"]["files_changed"]), (1, 1))

    def test_dashboard_filters_owner_and_shows_queue_usage(self):
        self.seed("mine", state="done", usage=json.dumps({"input_tokens": 7, "output_tokens": 3}), cost="0.04")
        self.seed("foreign", state="done", parent="somebody-else")
        self.store.enqueue_locked("mine", "message")
        rows = ux.dashboard_data(self.store)
        self.assertEqual([row["name"] for row in rows], ["mine"])
        self.assertEqual((rows[0]["queued"], rows[0]["tokens"], rows[0]["cost_usd"]), (1, 10, 0.04))

    def test_dashboard_shared_directory_scanned_once(self):
        self.seed("one")
        self.seed("two")
        self.put("changed.py", "a\n")
        with mock.patch.object(ux, "snapshot", wraps=reporting.snapshot) as scan:
            rows = ux.dashboard_data(self.store)
        self.assertEqual(scan.call_count, 1)
        self.assertTrue(all(row["files_changed"] == 1 for row in rows))

    def test_dashboard_unknown_usage_not_estimated(self):
        self.seed(state="done")
        row = ux.dashboard_data(self.store)[0]
        self.assertIsNone(row["tokens"])
        self.assertIsNone(row["cost_usd"])

    def test_top_json_no_subprocess_polling(self):
        self.seed(state="done")
        with mock.patch.object(subprocess, "Popen", side_effect=AssertionError("spawned poll")):
            code, out, _ = self.output(ux.top, self.store, {"--json": True})
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("["), out)
        self.assertEqual(json.loads(out)[0]["name"], "task")

    def test_top_once_contains_compact_columns(self):
        self.seed(state="done")
        code, out, _ = self.output(ux.top, self.store, {"--once": True})
        self.assertEqual(code, 0)
        for column in ("STATE", "ACTIVITY", "FILES", "QUEUE", "TOKENS", "COST USD"):
            self.assertIn(column, out)

    def test_top_non_tty_settles_without_loop(self):
        self.seed(state="done")
        with mock.patch.object(ux.Event, "wait", side_effect=AssertionError("looped non-TTY")):
            self.assertEqual(self.output(ux.top, self.store)[0], 0)

    def test_top_interactive_uses_event_wait_and_ctrl_c(self):
        self.seed(state="done")
        out = io.StringIO()
        out.isatty = lambda: True
        with contextlib.redirect_stdout(out), mock.patch.object(ux.Event, "wait", side_effect=KeyboardInterrupt) as wait:
            self.assertEqual(ux.top(self.store, {"interval": "0.1"}), 0)
        wait.assert_called_once_with(0.1)
        self.assertIn("Refreshing every 0.1s", out.getvalue())

    def test_top_invalid_interval_one_line_error(self):
        for value in ("oops", "0", "-1"):
            with self.assertRaisesRegex(ValueError, "positive number"):
                ux.top(self.store, {"interval": value})

    def test_result_survives_pruned_logs(self):
        self.seed(state="done", task_started_epoch=100, finished_epoch=120)
        atomic_write(self.store.path("task", ".answer"), "Ответ без логов\n")
        self.put("changed.py", "a\n")
        reporting.file_changes(self.store, "task", self.work, freeze=True)
        code, out, _ = self.output(ux.result, self.store, "task", {"--json": True})
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("{"), out)
        data = json.loads(out)
        self.assertEqual(data["final_answer"], "Ответ без логов\n")
        self.assertEqual(data["duration_sec"], 20)
        self.assertEqual(data["changed_files"][0]["path"], "changed.py")
        self.assertTrue(self.store.path("task", ".seen").exists())

    def test_result_legacy_answer_fallback(self):
        self.seed(state="done")
        atomic_write(self.store.path("task", ".log"), "prompt\n" + MARK + "\nFINAL\n")
        self.assertEqual(ux.collect_result(self.store, "task")["final_answer"], "FINAL\n")

    def test_result_history_redacted_and_text_not_recorded(self):
        self.seed(state="done")
        ux.record_history(self.store, "task", "send", sequence=1, queued=True, now=False, message="DO NOT KEEP", prompt="DO NOT KEEP", reason="api_key=abcdef")
        data = ux.collect_result(self.store, "task")
        self.assertEqual(data["history"][0]["sequence"], 1)
        text = self.store.path("task", ".history.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("DO NOT KEEP", text)
        self.assertNotIn("abcdef", text)
        self.assertIn("[REDACTED]", text)

    def test_result_skips_torn_history_line(self):
        self.seed(state="done")
        ux.record_history(self.store, "task", "run")
        with self.store.path("task", ".history.jsonl").open("a", encoding="utf-8") as out:
            out.write('{"incomplete":')
        self.assertEqual(len(ux.collect_result(self.store, "task")["history"]), 1)

    def test_stop_records_finished_history_and_redacts_digest(self):
        self.seed()
        self.put("partial.py", "partial\n")
        reporting.record_stop(self.store, "task", reason="secret=hiddenvalue", partial="password=abc", activity="token=abc")
        meta = self.store.read("task")
        self.assertGreater(float(meta["finished_epoch"]), 0)
        history = ux.collect_result(self.store, "task")["history"]
        self.assertTrue(history)
        self.assertEqual(history[-1]["kind"], "stop")
        self.assertNotIn("hiddenvalue", meta["reason"])
        self.assertNotIn("abc", meta["partial_result"])
        self.assertNotIn("abc", meta["last_activity"])
        self.assertTrue(self.store.path("task", ".changes.json").is_file())

    def test_stop_result_survives_log_pruning(self):
        self.seed()
        atomic_write(self.store.path("task", ".answer"), "provider partial answer\n")
        reporting.record_stop(self.store, "task", partial="partial answer")
        data = ux.collect_result(self.store, "task")
        self.assertIn("STOPPED", data["stop_report"])
        self.assertEqual(data["final_answer"], "provider partial answer\n")
        _, out, _ = self.output(ux.result, self.store, "task")
        self.assertIn("STOPPED", out)

    def test_render_md_contains_answer_not_raw_prompt(self):
        self.seed(state="done")
        atomic_write(self.store.path("task", ".log"), "PRIVATE RAW PROMPT\n" + MARK + "\nFINAL\n")
        atomic_write(self.store.path("task", ".answer"), "FINAL\n")
        reporting.render_md(self.store, "task")
        text = self.store.path("task", ".md").read_text(encoding="utf-8")
        self.assertIn("FINAL", text)
        self.assertNotIn("PRIVATE RAW PROMPT", text)

    def test_dashboard_activity_secret_redaction(self):
        self.seed(state="done", last_activity="tool api_key=abcdef")
        self.assertNotIn("abcdef", ux.dashboard_data(self.store)[0]["last_activity"])


DEFECTS = [
    ("hash-stat-regression", "reporting.py", 'return (initial.get("sha256"), initial.get("type")) != (current.get("sha256"), current.get("type"))', 'return initial.get("size") != current.get("size")', "test_hash_detects_same_size_same_timestamp_write"),
    ("mtime-false-positive", "reporting.py", 'return (initial.get("sha256"), initial.get("type")) != (current.get("sha256"), current.get("type"))', 'return initial != current', "test_metadata_only_change_is_not_content_change"),
    ("send-baseline-reset", "reporting.py", 'if baseline.is_file() and not force:', 'if False:', "test_baseline_preserved_across_followups"),
    ("fresh-keeps-baseline", "reporting.py", 'if baseline.is_file() and not force:', 'if baseline.is_file():', "test_new_run_force_replaces_baseline"),
    ("dirty-git-status-lost", "reporting.py", 'git_status=_git_status(directory)', 'git_status=None', "test_dirty_git_start_recorded_and_diffed_against_dirty_content"),
    ("legacy-baseline-dropped", "reporting.py", 'return initial != ([current["size"], current["mtime_ns"]] if current else None)', 'return True', "test_legacy_stat_baseline_remains_readable"),
    ("progress-counted", "reporting.py", 'if name.startswith("PROGRESS.") and name.endswith(".md") or name.startswith(".agent") or ignored(path):', 'if name.startswith(".agent") or ignored(path):', "test_runtime_dirs_and_progress_are_ignored"),
    ("state-self-counted", "reporting.py", 'files=snapshot(directory, (store.root,), capture)', 'files=snapshot(directory, (), capture)', "test_state_directory_inside_work_is_excluded"),
    ("baseline-text-unbounded", "reporting.py", 'TEXT_LIMIT = 2 * 1024 * 1024', 'TEXT_LIMIT = 8 * 1024 * 1024', "test_baseline_text_is_bounded"),
    ("baseline-total-unbounded", "reporting.py", 'or len(data) > remaining[0]', '', "test_baseline_total_text_budget"),
    ("line-counts-zero", "reporting.py", 'entry.update(insertions=additions, deletions=removals)', 'entry.update(insertions=0, deletions=0)', "test_added_deleted_modified_line_counts"),
    ("symlink-content-followed", "reporting.py", 'info = path.lstat()', 'info = path.stat()', "test_symlink_hashes_link_never_target_content"),
    ("final-delta-not-frozen", "reporting.py", 'if frozen and not freeze:', 'if False:', "test_frozen_result_excludes_later_writer"),
    ("names-polluted", "ux_tracking.py", 'if opts.get("--names"):', 'if False:', "test_diff_names_is_machine_readable_while_running"),
    ("patch-start-content-lost", "ux_tracking.py", 'fromfile="a/" + entry["path"]', 'fromfile="wrong/" + entry["path"]', "test_diff_patch_uses_start_content"),
    ("patch-later-writer-leaked", "ux_tracking.py", 'and hashlib.sha256(raw).hexdigest() == entry.get("after_sha256")', '', "test_finished_patch_refuses_later_writer_content"),
    ("glob-overlap-missed", "ux_tracking.py", 'if i == len(left) and j == len(right):\n            return True', 'if i == len(left) and j == len(right):\n            return False', "test_glob_intersection_patterns"),
    ("strict-owns-ignored", "ux_tracking.py", 'if warnings and strict:', 'if False:', "test_ownership_warns_and_strict_refuses_running"),
    ("ownership-no-warnings", "ux_tracking.py", 'print("[neoxider] warning: " + warning, file=sys.stderr)', 'pass', "test_ownership_warns_and_strict_refuses_running"),
    ("finished-blocks-ownership", "ux_tracking.py", 'or effective(store, other, meta) not in ACTIVE', '', "test_ownership_finished_task_does_not_block"),
    ("nested-ownership-lost", "ux_tracking.py", 'os.path.join(str(Path(directory).resolve()), pattern)', 'os.path.join("/ignored", pattern)', "test_ownership_same_glob_different_dirs_is_disjoint"),
    ("shared-file-warning-lost", "ux_tracking.py", 'for conflict in overlaps(store, name, names):', 'for conflict in []:', "test_overlap_warning_for_concurrent_shared_file"),
    ("ownership-scope-lost", "reporting.py", 'return owned_paths(store, name, directory, names) if scope else names', 'return names', "test_declared_scope_excludes_disjoint_workers_changes"),
    ("lifetimes-ignored", "ux_tracking.py", 'if ours_end < other_start or other_end < ours_start:', 'if False:', "test_overlap_excludes_disjoint_task_lifetimes"),
    ("owner-filter-lost", "ux_tracking.py", 'if not mine(meta):', 'if False:', "test_dashboard_filters_owner_and_shows_queue_usage"),
    ("queue-count-lost", "ux_tracking.py", 'queued=len(store.inbox(name))', 'queued=0', "test_dashboard_filters_owner_and_shows_queue_usage"),
    ("tokens-cost-lost", "ux_tracking.py", 'return data or None, total, cost', 'return None, None, None', "test_dashboard_filters_owner_and_shows_queue_usage"),
    ("unknown-usage-invented", "ux_tracking.py", 'return data or None, total, cost', 'return data or None, total or 0, cost or 0', "test_dashboard_unknown_usage_not_estimated"),
    ("scan-per-agent", "ux_tracking.py", 'if normalized not in scans:', 'if True:', "test_dashboard_shared_directory_scanned_once"),
    ("top-json-broken", "ux_tracking.py", 'print(json.dumps(rows, ensure_ascii=False))', 'print("BROKEN")', "test_top_json_no_subprocess_polling"),
    ("top-untracked-spawn", "ux_tracking.py", 'rows = dashboard_data(store)', 'import subprocess; subprocess.Popen(["git", "status"]); rows = dashboard_data(store)', "test_top_json_no_subprocess_polling"),
    ("top-nontty-hang", "ux_tracking.py", 'or not sys.stdout.isatty()', '', "test_top_non_tty_settles_without_loop"),
    ("top-wait-interval-lost", "ux_tracking.py", 'sleeper.wait(interval)', 'sleeper.wait(5)', "test_top_interactive_uses_event_wait_and_ctrl_c"),
    ("result-loses-answer", "ux_tracking.py", 'answer = store.path(name, ".answer").read_text(encoding="utf-8")', 'answer = ""', "test_result_survives_pruned_logs"),
    ("result-duration-wrong", "ux_tracking.py", 'round((end or time.time()) - start, 3)', '0', "test_result_survives_pruned_logs"),
    ("result-seen-lost", "ux_tracking.py", 'store.seen(name)', 'pass', "test_result_survives_pruned_logs"),
    ("history-secret-leak", "ux_tracking.py", 'return redact(str(text))', 'return str(text)', "test_result_history_redacted_and_text_not_recorded"),
    ("history-prompt-persisted", "ux_tracking.py", '"sequence", "queued", "now", "fresh", "session")', '"sequence", "queued", "now", "fresh", "session", "message", "prompt")', "test_result_history_redacted_and_text_not_recorded"),
    ("stop-digest-secret-leak", "reporting.py", 'reason=redact(reason)', 'reason=reason', "test_stop_records_finished_history_and_redacts_digest"),
    ("stop-history-lost", "reporting.py", 'record_history(store, name, "stop", by=by, reason=reason, exit=code, state=state)', 'pass', "test_stop_records_finished_history_and_redacts_digest"),
    ("md-retains-raw-transcript", "reporting.py", 'store.path(name, ".answer").open("r"', 'store.path(name, ".log").open("r"', "test_render_md_contains_answer_not_raw_prompt"),
]


def prove_defects():
    base = Path("D:/Temp/agents-ux/tracking/proofs") if os.name == "nt" else Path(tempfile.gettempdir()) / "agents-ux/tracking/proofs"
    base.mkdir(parents=True, exist_ok=True)
    records = []
    for label, module, original, replacement, test in DEFECTS:
        copy = base / label
        copy.mkdir(exist_ok=True)
        shutil.copytree(ROOT / "neoxider_agents", copy / "neoxider_agents", dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__"))
        (copy / "tests").mkdir(exist_ok=True)
        shutil.copy2(ROOT / "activity.py", copy / "activity.py")
        shutil.copy2(ROOT / "tests/test_ux_tracking.py", copy / "tests/test_ux_tracking.py")
        path = copy / "neoxider_agents" / module
        source = path.read_text(encoding="utf-8")
        if original not in source:
            records.append(dict(defect=label, caught=False, error="mutation did not match"))
            continue
        path.write_text(source.replace(original, replacement, 1), encoding="utf-8")
        env = dict(os.environ, AGENT_UX_TEST_ROOT=copy.as_posix(), PYTHONPATH=copy.as_posix(),
                   AGENT_CLI_LOGS=(base / "state").as_posix(), PYTHONDONTWRITEBYTECODE="1")
        try:
            run = subprocess.run([sys.executable, "-m", "unittest", "test_ux_tracking.TrackingTests." + test],
                                 cwd=str(copy / "tests"), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 timeout=30, **hidden_kwargs())
            output = run.stdout.decode("utf-8", "replace")
            caught = run.returncode != 0 and "FAIL:" in output
            (copy / "result.txt").write_text(output, encoding="utf-8")
            records.append(dict(defect=label, test=test, caught=caught, exit=run.returncode, evidence=(copy / "result.txt").as_posix()))
        except subprocess.TimeoutExpired:
            records.append(dict(defect=label, test=test, caught=False, error="proof timeout"))
        print(label, "CAUGHT" if records[-1]["caught"] else "NOT CAUGHT", flush=True)
    (base / "manifest.json").write_text(json.dumps(records, indent=2), encoding="utf-8")
    return 0 if all(record["caught"] for record in records) else 1


if __name__ == "__main__":
    if "--prove-defects" in sys.argv:
        sys.exit(prove_defects())
    unittest.main()
