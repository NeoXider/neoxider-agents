import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from neoxider_agents import views


class _Store:
    def __init__(self, root, metas):
        self.root = Path(root)
        self.metas = metas

    def path(self, name, suffix):
        return self.root / (name + suffix)

    def read(self, name):
        return dict(self.metas.get(name, {}))


def _shell_event(pid, stamp, workdir="D:/Git/Work-wt/task-a"):
    return json.dumps({"type": "tool_use", "timestamp": int(stamp * 1000), "part": {
        "tool": "shell", "state": {"status": "completed", "input": {"command": "& build.ps1", "workdir": workdir},
                                   "metadata": {"metadata": {"status": "running", "pid": pid}}}}}, separators=(",", ":"))


class WaitJobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = time.time()
        self.meta = {"dir": "D:/Git/Work", "pid": "100", "started_epoch": str(self.now - 600),
                     "task_started_epoch": str(self.now - 900)}
        self.store = _Store(self.temp.name, {"task-a": self.meta})

    def write_log(self, *lines):
        self.store.path("task-a", ".log").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_logged_background_pid_that_is_alive_is_a_running_job(self):
        self.write_log(_shell_event(4242, self.now - 300), "[agent-activity] command & build.ps1")
        rows = [{"p": 4242, "pp": 9, "n": "powershell.exe", "c": "powershell -Command build.ps1", "t": int(self.now - 300)}]
        self.assertIn("powershell.exe", views._running_job(self.store, "task-a", self.meta, rows))

    def test_reused_pid_created_after_the_log_event_is_not_the_job(self):
        self.write_log(_shell_event(4242, self.now - 300))
        rows = [{"p": 4242, "pp": 9, "n": "powershell.exe", "c": "other", "t": int(self.now - 10)}]
        self.assertEqual("", views._running_job(self.store, "task-a", self.meta, rows))

    def test_task_directory_matches_only_as_a_whole_path(self):
        rows = [{"p": 7, "pp": 1, "n": "powershell.exe", "c": "& D:/Git/Work-other/build.ps1", "t": int(self.now - 60)}]
        self.assertEqual("", views._dir_job(self.meta, rows, self.now - 600))
        rows[0]["c"] = "Unity.exe -projectPath D:\\Git\\Work -quit"
        self.assertIn("Unity.exe", views._dir_job(self.meta, rows, self.now - 600))

    def test_logged_workdir_links_a_foreground_command_in_a_worktree(self):
        self.write_log(_shell_event(5, self.now - 800))
        rows = [{"p": 8, "pp": 1, "n": "powershell.exe", "t": int(self.now - 60),
                 "c": "& buildslot.ps1 Unity.exe -projectPath D:/Git/Work-wt/task-a -logFile x.log"}]
        self.assertIn("buildslot", views._running_job(self.store, "task-a", self.meta, rows))

    def test_shallow_workdirs_are_ignored(self):
        self.write_log(_shell_event(5, self.now, workdir="D:/Git"))
        self.assertEqual(set(), views._logged_workdirs(self.store, "task-a"))

    def test_quiet_task_with_a_job_is_not_stalled_until_the_job_limit(self):
        self.write_log("quiet")
        old = self.now - 900
        os.utime(self.store.path("task-a", ".log"), (old, old))
        with mock.patch.object(views, "_process_rows", return_value=[{"p": 1}]), \
                mock.patch.object(views, "_running_job", return_value="Unity.exe build"), \
                mock.patch.dict(os.environ, {"AGENT_STALL_ALERT_SEC": "300", "AGENT_STALL_JOB_ALERT_SEC": "3600"}):
            self.assertEqual([], views._stalled(self.store, ["task-a"]))
        with mock.patch.object(views, "_process_rows", return_value=[{"p": 1}]), \
                mock.patch.object(views, "_running_job", return_value="Unity.exe build"), \
                mock.patch.dict(os.environ, {"AGENT_STALL_ALERT_SEC": "300", "AGENT_STALL_JOB_ALERT_SEC": "600"}):
            found = views._stalled(self.store, ["task-a"])
        self.assertEqual([("task-a", found[0][1], "Unity.exe build")], found)

    def test_quiet_task_without_a_job_is_stalled(self):
        self.write_log("quiet")
        old = self.now - 900
        os.utime(self.store.path("task-a", ".log"), (old, old))
        idle = [{"p": 1, "pp": 0, "n": "System", "c": "", "t": 0}]
        with mock.patch.object(views, "_process_rows", return_value=idle), \
                mock.patch.dict(os.environ, {"AGENT_STALL_ALERT_SEC": "300"}):
            found = views._stalled(self.store, ["task-a"])
        self.assertEqual(["task-a"], [name for name, _, job in found if not job])

    def test_unknown_process_table_never_counts_as_a_stall_on_windows(self):
        self.write_log("quiet")
        old = self.now - 900
        os.utime(self.store.path("task-a", ".log"), (old, old))
        with mock.patch.object(views, "_process_rows", return_value=[]), mock.patch.object(views.os, "name", "nt"),                 mock.patch.dict(os.environ, {"AGENT_STALL_ALERT_SEC": "300"}):
            self.assertEqual([], views._stalled(self.store, ["task-a"]))

    def test_resumed_turn_is_detached_from_the_wait(self):
        with mock.patch("subprocess.Popen") as popen:
            views._send_continue("task-a", "continue")
        env = popen.call_args.kwargs["env"]
        self.assertEqual("1", env["AGENT_DETACHED"])
        self.assertEqual("", env["AGENT_LAUNCHER_PID"])

    def test_foreground_command_under_the_shell_daemon_after_the_last_log_write_is_a_job(self):
        self.write_log("text Starting the release build")
        quiet = self.now - 400
        os.utime(self.store.path("task-a", ".log"), (quiet, quiet))
        rows = [{"p": 50, "pp": 1, "n": "opencode-pty.exe", "c": "opencode-pty.exe daemon", "t": int(self.now - 9000)},
                {"p": 51, "pp": 50, "n": "powershell.exe", "c": "& D:/Git/Other/release/build.ps1", "t": int(quiet + 2)}]
        self.assertIn("build.ps1", views._running_job(self.store, "task-a", self.meta, rows))
        rows[1]["t"] = int(quiet - 600)
        self.assertEqual("", views._running_job(self.store, "task-a", self.meta, rows))
        rows[1]["t"] = int(quiet + 200)
        self.assertEqual("", views._running_job(self.store, "task-a", self.meta, rows))


if __name__ == "__main__":
    unittest.main()
