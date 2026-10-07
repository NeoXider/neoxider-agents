"""Real OS lifecycle checks; scratch stays outside the live checkout."""
import json
import os
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from neoxider_agents import process


class ProcessTests(unittest.TestCase):
    def setUp(self):
        base = Path("D:/Temp/agents-ux/process") if os.name == "nt" else Path(tempfile.gettempdir()) / "agents-core-process"
        base.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=str(base))
        self.work = Path(self.temporary.name)
        self.prompt = self.work / "prompt.txt"
        self.prompt.write_text("Привет — проверка UTF-8\n", encoding="utf-8")
        self.trees = []
        self.children = []
        self.owned_pids = {}

    def tearDown(self):
        for tree in self.trees:
            tree.close()
            # Cleanup remains independent of the functions planted mutants disable.
            if os.name == "nt" and tree.job and tree.job.handle:
                from neoxider_agents.windows import TerminateJob, CloseHandle
                TerminateJob(tree.job.handle, 130)
                CloseHandle(tree.job.handle)
                tree.job.handle = None
            if tree.process.poll() is None:
                tree.process.kill()
            try:
                tree.process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                tree.process.kill()
        for child in self.children:
            if child.poll() is None:
                child.kill()
            try:
                child.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        for pid, stamp in self.owned_pids.items():
            if process.pid_stamp(pid) != stamp:
                continue
            if os.name == "nt":
                from neoxider_agents.windows import OpenProcess, TerminateProcess, WaitForSingleObject, CloseHandle
                handle = OpenProcess(1 | 0x100000, False, pid)
                if handle:
                    TerminateProcess(handle, 130)
                    WaitForSingleObject(handle, 3000)
                    CloseHandle(handle)
            else:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
        self.temporary.cleanup()

    def script(self, name, source):
        path = self.work / name
        path.write_text(source, encoding="utf-8")
        return path

    def launch(self, source):
        path = self.script("provider-%s.py" % len(self.trees), source)
        tree = process.spawn([sys.executable, str(path)], self.prompt, str(self.work))
        self.trees.append(tree)
        self.owned_pids[tree.pid] = tree.stamp
        return tree

    def wait_file(self, path, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if path.exists() and path.stat().st_size:
                return path.read_text(encoding="utf-8")
            time.sleep(0.02)
        self.fail("fixture did not create %s" % path)

    def wait_dead(self, pid, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and process.pid_alive(pid):
            time.sleep(0.02)
        self.assertFalse(process.pid_alive(pid), "surviving process %s" % pid)

    def tree_fixture(self):
        child = self.script("child.py", "import time\ntime.sleep(90)\n")
        source = ("import os,subprocess,sys,time\n"
                  "p=subprocess.Popen([sys.executable,%r])\n"
                  "open('child.pid','w').write(str(p.pid))\n"
                  "print('partial assistant result',flush=True)\n"
                  "time.sleep(90)\n") % str(child)
        tree = self.launch(source)
        child_pid = int(self.wait_file(self.work / "child.pid"))
        self.owned_pids[child_pid] = process.pid_stamp(child_pid)
        return tree, child_pid

    def test_utf8_file_stdin_exact_and_not_argv(self):
        tree = self.launch("import sys\nsys.stdout.buffer.write(sys.stdin.buffer.read())\n")
        output, unused = tree.process.communicate(timeout=5)
        self.assertEqual(output, self.prompt.read_bytes())
        self.assertNotIn("Привет", " ".join(tree.process.args))
        self.assertEqual(tree.process.returncode, 0)

    def test_stderr_is_merged(self):
        tree = self.launch("import sys\nprint('out',flush=True)\nprint('err',file=sys.stderr,flush=True)\n")
        output, unused = tree.process.communicate(timeout=5)
        self.assertIn(b"out", output)
        self.assertIn(b"err", output)
        self.assertIsNone(tree.process.stderr)

    def test_process_stamp_rejects_other_generation(self):
        tree = self.launch("import time\ntime.sleep(90)\n")
        self.assertTrue(tree.stamp)
        self.assertTrue(process.pid_alive(tree.pid, tree.stamp))
        self.assertFalse(process.pid_alive(tree.pid, tree.stamp + "0"))
        self.assertFalse(process.kill_tree(tree.pid, stamp=tree.stamp + "0"))
        self.assertTrue(process.pid_alive(tree.pid))

    def test_kill_reaches_grandchild(self):
        tree, child_pid = self.tree_fixture()
        tree.kill()
        self.wait_dead(tree.pid)
        self.wait_dead(child_pid)

    def test_close_reaches_grandchild_and_is_idempotent(self):
        tree, child_pid = self.tree_fixture()
        tree.close()
        tree.close()
        self.wait_dead(child_pid)
        self.wait_dead(tree.pid)

    def test_external_kill_named_job_reaches_tree(self):
        tree, child_pid = self.tree_fixture()
        self.assertTrue(process.kill_tree(tree.pid, stamp=tree.stamp, job_name=tree.job_name))
        self.wait_dead(tree.pid)
        self.wait_dead(child_pid)

    def test_invalid_pid_never_targets_a_group(self):
        for pid in ("bad", "", 0, -1):
            self.assertFalse(process.pid_alive(pid))
            self.assertFalse(process.kill_tree(pid))
            self.assertEqual(process.pid_stamp(pid), "")

    def test_control_signal_fast_path(self):
        with process.ControlSignal(str(self.work)) as event:
            meta = {"control_event": event.name, "pid": str(os.getpid()), "pid_stamp": process.pid_stamp(os.getpid())}
            self.assertTrue(process.signal_task(meta))
            started = time.monotonic()
            self.assertTrue(event.wait(2))
            self.assertLess(time.monotonic() - started, 0.2)
            self.assertFalse(event.wait(0.01))
        self.assertFalse(process.signal_task({}))

    def test_control_signal_cross_process(self):
        with process.ControlSignal(str(self.work)) as event:
            meta = {"control_event": event.name, "pid": str(os.getpid()), "pid_stamp": process.pid_stamp(os.getpid())}
            sender = self.script("sender.py", "import sys\nsys.path.insert(0,%r)\nfrom neoxider_agents.process import signal_task\nassert signal_task(%r)\n" % (str(ROOT), meta))
            child = subprocess.Popen([sys.executable, str(sender)], **process.hidden_kwargs())
            self.children.append(child)
            self.assertTrue(event.wait(5))
            self.assertEqual(child.wait(timeout=5), 0)

    def test_posix_legacy_shared_group_kills_only_owned_tree(self):
        fake_os = mock.Mock(name="posix-os")
        fake_os.name = "posix"
        fake_os.getpgid.return_value = 900
        stamps = {100: "root", 101: "child", 102: "new-generation", 900: "caller"}
        rows = [(100, 900, "root"), (101, 100, "child"), (102, 101, "old-generation"), (103, 900, "other")]
        with mock.patch.object(process, "os", fake_os), mock.patch.object(process, "pid_alive", return_value=True), \
                mock.patch.object(process, "pid_stamp", side_effect=lambda pid: stamps.get(pid, "")), \
                mock.patch.object(process, "_posix_rows", return_value=rows), \
                mock.patch.object(process.signal, "SIGKILL", 9, create=True):
            self.assertTrue(process.kill_tree(100, stamp="root"))
        fake_os.killpg.assert_not_called()
        self.assertEqual(fake_os.kill.call_args_list, [mock.call(100, 9), mock.call(101, 9)])

    def test_posix_owned_group_survives_leader_exit_for_cleanup(self):
        fake_os = mock.Mock()
        fake_os.name = "posix"
        tree = process.ProcessTree(mock.Mock(pid=100))
        tree.stamp = "root"
        with mock.patch.object(process, "os", fake_os), mock.patch.object(process, "pid_stamp", return_value=""), \
                mock.patch.object(process.signal, "SIGKILL", 9, create=True):
            tree.kill()
            tree._closed = True
        fake_os.killpg.assert_called_once_with(100, 9)

    def test_posix_signal_uses_pid_start_generation(self):
        fake_os = mock.Mock()
        fake_os.name = "posix"
        meta = {"control_event": "fixture", "pid": "100", "pid_start": "old"}
        with mock.patch.object(process, "os", fake_os), \
                mock.patch.object(process, "pid_alive", side_effect=lambda pid, stamp: stamp != "old"), \
                mock.patch.object(process.signal, "SIGUSR1", 10, create=True):
            self.assertFalse(process.signal_task(meta))
            fake_os.kill.assert_not_called()
            meta["pid_start"] = "current"
            self.assertTrue(process.signal_task(meta))
        fake_os.kill.assert_called_once_with(100, 10)

    @unittest.skipUnless(os.name == "nt", "Windows hidden creation flags")
    def test_hidden_by_default_and_terminal_opt_in(self):
        options = process.hidden_kwargs()
        self.assertTrue(options["creationflags"] & subprocess.CREATE_NO_WINDOW)
        self.assertTrue(options["startupinfo"].dwFlags & subprocess.STARTF_USESHOWWINDOW)
        self.assertEqual(options["startupinfo"].wShowWindow, subprocess.SW_HIDE)
        self.assertFalse(process.hidden_kwargs(True)["creationflags"] & subprocess.CREATE_NO_WINDOW)

    @unittest.skipUnless(os.name == "nt", "Windows PowerShell hidden flags")
    def test_powershell_helpers_use_hidden_non_detached_flags(self):
        for executable in ("powershell.exe", "pwsh.exe", "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe"):
            options = process.hidden_kwargs(executable=executable)
            self.assertTrue(options["creationflags"] & subprocess.CREATE_NO_WINDOW)
            self.assertFalse(options["creationflags"] & subprocess.DETACHED_PROCESS)
            self.assertEqual(options["startupinfo"].wShowWindow, subprocess.SW_HIDE)
            visible = process.hidden_kwargs(True, executable)
            self.assertTrue(visible["creationflags"] & subprocess.CREATE_NEW_PROCESS_GROUP)
            self.assertFalse(visible["creationflags"] & subprocess.CREATE_NO_WINDOW)
        self.assertTrue(process.hidden_kwargs(executable=sys.executable)["creationflags"] & subprocess.DETACHED_PROCESS)

    @unittest.skipUnless(os.name == "nt", "Windows hidden creation flags")
    def test_hidden_launch_keeps_a_hidden_console_for_grandchildren(self):
        for executable in ("", sys.executable, "node.exe", "codex.exe"):
            flags = process.hidden_kwargs(executable=executable, console=True)["creationflags"]
            self.assertTrue(flags & subprocess.CREATE_NO_WINDOW)
            self.assertFalse(flags & subprocess.DETACHED_PROCESS)

    @unittest.skipUnless(os.name == "nt", "Actual Windows PowerShell helper")
    def test_powershell_helper_executes_and_emits_stdout(self):
        executables = [value for value in (shutil.which("powershell.exe"), shutil.which("pwsh.exe")) if value]
        self.assertTrue(executables, "Windows PowerShell is required for native entry verification")
        for executable in executables:
            with self.subTest(executable=executable):
                tree = process.spawn([executable, "-NoProfile", "-NonInteractive", "-Command",
                                      "[Console]::WriteLine('ECHO'); exit 0"], self.prompt, cwd=str(self.work))
                self.trees.append(tree)
                output, unused = tree.process.communicate(timeout=15)
                self.assertEqual(tree.process.returncode, 0, output)
                self.assertEqual(output.strip(), b"ECHO", "helper exited without executing its command")

    @unittest.skipUnless(os.name == "nt", "Windows hidden console sharing")
    def test_provider_console_grandchild_shares_the_hidden_console(self):
        grandchild = "import ctypes;print(ctypes.windll.kernel32.GetConsoleProcessList((ctypes.c_uint*8)(),8))"
        provider = self.script("console_probe.py", "import subprocess,sys\nprint(subprocess.check_output([sys.executable,'-c',%r]).decode().strip())\n" % grandchild)
        tree = process.spawn([sys.executable, str(provider)], self.prompt, cwd=str(self.work))
        self.trees.append(tree)
        output, unused = tree.process.communicate(timeout=20)
        self.assertEqual(tree.process.returncode, 0, output)
        self.assertGreaterEqual(int(output.strip()), 2, "a console child opened its own console window instead of sharing the hidden one")

    @unittest.skipUnless(os.name == "nt", "Windows job lifetime")
    def test_hard_launcher_kill_reaches_provider_and_grandchild(self):
        child = self.script("grandchild.py", "import time\ntime.sleep(90)\n")
        provider = self.script("provider.py", "import os,subprocess,sys,time\np=subprocess.Popen([sys.executable,%r])\nopen('grandchild.pid','w').write(str(p.pid))\ntime.sleep(90)\n" % str(child))
        launcher = self.script("launcher.py", "import sys,time\nsys.path.insert(0,%r)\nfrom neoxider_agents.process import spawn\nt=spawn([sys.executable,%r],%r,cwd=%r)\nopen(%r,'w').write(str(t.pid))\ntime.sleep(90)\n" % (str(ROOT), str(provider), str(self.prompt), str(self.work), str(self.work / "provider.pid")))
        wrapper = subprocess.Popen([sys.executable, str(launcher)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, **process.hidden_kwargs())
        self.children.append(wrapper)
        provider_pid = int(self.wait_file(self.work / "provider.pid"))
        grandchild_pid = int(self.wait_file(self.work / "grandchild.pid"))
        self.owned_pids[provider_pid] = process.pid_stamp(provider_pid)
        self.owned_pids[grandchild_pid] = process.pid_stamp(grandchild_pid)
        wrapper.kill()
        wrapper.wait(timeout=5)
        self.wait_dead(provider_pid)
        self.wait_dead(grandchild_pid)

    def test_launcher_signal_cancels_tree_and_raises_stopped(self):
        tree, child_pid = self.tree_fixture()
        with self.assertRaises(process.LauncherStopped):
            process._cancel(getattr(signal, "SIGTERM", 15), None)
        self.wait_dead(tree.pid)
        self.wait_dead(child_pid)

    @unittest.skipUnless(os.name == "nt", "Windows suspended launch race")
    def test_hard_launcher_kill_during_suspended_assignment(self):
        provider = self.script("suspended-provider.py", "import time\ntime.sleep(90)\n")
        pidfile = self.work / "suspended.pid"
        source = ("import sys,time\nsys.path.insert(0,%r)\n"
                  "from neoxider_agents.process import spawn\n"
                  "from neoxider_agents import windows as w\n"
                  "original=w.Job.assign\n"
                  "def paused(self,handle):\n"
                  " if int(handle)==int(w.GetCurrentProcess()):\n"
                  "  return original(self,handle)\n"
                  " open(%r,'w').write(str(w.GetProcessId(handle)))\n"
                  " time.sleep(90)\n"
                  "w.Job.assign=paused\n"
                  "spawn([sys.executable,%r],%r,cwd=%r)\n") % (
                      str(ROOT), str(pidfile), str(provider), str(self.prompt), str(self.work))
        launcher = self.script("assignment-launcher.py", source)
        wrapper = subprocess.Popen([sys.executable, str(launcher)], stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, **process.hidden_kwargs())
        self.children.append(wrapper)
        provider_pid = int(self.wait_file(pidfile))
        self.owned_pids[provider_pid] = process.pid_stamp(provider_pid)
        wrapper.kill()
        wrapper.wait(timeout=5)
        self.wait_dead(provider_pid)

    def test_missing_executable_and_missing_prompt_raise(self):
        with self.assertRaises(OSError):
            process.spawn([str(self.work / "missing.exe")], self.prompt)
        with self.assertRaises(OSError):
            process.spawn([sys.executable], self.work / "missing.txt")

    def test_closed_stdout_pipe_is_detected_without_writing(self):
        marker = self.work / "closed.txt"
        ready = self.work / "ready.txt"
        source = ("import sys,time\nsys.path.insert(0,%r)\n"
                  "from neoxider_agents.process import stdout_closed\n"
                  "open(%r,'w').write('ready')\n"
                  "deadline=time.monotonic()+5\n"
                  "while time.monotonic()<deadline:\n"
                  " if stdout_closed():\n"
                  "  open(%r,'w').write('closed')\n"
                  "  break\n"
                  " time.sleep(.02)\n") % (str(ROOT), str(ready), str(marker))
        launcher = self.script("closed-stdout.py", source)
        child = subprocess.Popen([sys.executable, str(launcher)], stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, **process.hidden_kwargs())
        self.children.append(child)
        self.wait_file(ready)
        child.stdout.close()
        child.stdout = None
        self.assertEqual(self.wait_file(marker), "closed")
        self.assertEqual(child.wait(timeout=5), 0)

    def test_window_gate_reports_existing_non_console_dialog_separately(self):
        from tests.check_windows import WindowSampler
        sampler = WindowSampler()
        sampler.baseline_pids = {100, 101}
        sampler.observations = [{"pid": 100, "executable": "Unity.exe", "title": "Reloading Domain"}]
        self.assertTrue(sampler.report()["passed"])
        self.assertEqual(len(sampler.report()["other_visible_changes"]), 1)
        sampler.observations.append({"pid": 101, "executable": "conhost.exe", "title": ""})
        self.assertFalse(sampler.report()["passed"])
        self.assertEqual(len(sampler.report()["new_visible_consoles"]), 1)

    def test_window_gate_rejects_new_provider_ui(self):
        from tests.check_windows import WindowSampler
        sampler = WindowSampler()
        sampler.baseline_pids = {100}
        sampler.observations = [{"pid": 102, "executable": "provider.exe", "title": "Unwanted UI"}]
        self.assertFalse(sampler.report()["passed"])

    def test_window_sampler_uses_deadlines_without_enumeration_drift(self):
        from tests import check_windows
        sampler = check_windows.WindowSampler()
        now = [0.0]
        sampler._stop = mock.Mock()
        sampler._stop.is_set.side_effect = [False, False, False, True]
        sampler._stop.wait.side_effect = lambda seconds: now.__setitem__(0, now[0] + seconds)
        def snapshot(*unused):
            now[0] += 0.04
            return {}
        with mock.patch.object(check_windows, "visible_windows", side_effect=snapshot), \
                mock.patch.object(check_windows.time, "monotonic", side_effect=lambda: now[0]):
            sampler._run()
        self.assertEqual(sampler.samples, 3)
        for gap in sampler.report()["sample_gaps_ms"]:
            self.assertAlmostEqual(gap, 100)
        self.assertEqual(sampler.report()["missed_intervals"], 0)

    def test_window_sampler_reports_snapshot_overruns(self):
        from tests import check_windows
        sampler = check_windows.WindowSampler()
        now = [0.0]
        sampler._stop = mock.Mock()
        sampler._stop.is_set.side_effect = [False, False, True]
        sampler._stop.wait.side_effect = lambda seconds: now.__setitem__(0, now[0] + seconds)
        def snapshot(*unused):
            now[0] += 0.16
            return {}
        with mock.patch.object(check_windows, "visible_windows", side_effect=snapshot), \
                mock.patch.object(check_windows.time, "monotonic", side_effect=lambda: now[0]):
            sampler._run()
        report = sampler.report()
        self.assertAlmostEqual(report["max_sample_gap_ms"], 200)
        self.assertEqual(report["missed_intervals"], 2)
        self.assertEqual(len(report["overruns"]), 2)
        self.assertAlmostEqual(report["max_snapshot_ms"], 160)

    def test_window_sampler_only_resolves_new_window_processes(self):
        from tests import check_windows
        sampler = check_windows.WindowSampler()
        sampler._stop = mock.Mock()
        sampler._stop.is_set.side_effect = [False, False, True]
        existing = {1: {"pid": 100, "handle": 1}}
        sampler.baseline = existing
        current = dict(existing)
        current[2] = {"pid": 200, "handle": 2}
        with mock.patch.object(check_windows, "visible_windows", side_effect=[existing, current]) as snapshot, \
                mock.patch.object(check_windows, "window_process_details", side_effect=lambda rows: rows) as details:
            sampler._run()
        self.assertEqual(snapshot.call_args_list, [mock.call(False), mock.call(False)])
        self.assertEqual(details.call_count, 1)
        self.assertEqual(set(details.call_args.args[0]), {2})

    @unittest.skipUnless(os.name == "nt", "Windows suspended assignment")
    def test_assignment_failure_does_not_execute_provider(self):
        from neoxider_agents.windows import ensure_lifetime_job
        ensure_lifetime_job()
        marker = self.work / "ran.txt"
        provider = self.script("no-execute.py", "open(%r,'w').write('ran')\n" % str(marker))
        def reject_assignment(unused_handle):
            time.sleep(0.15)
            raise OSError("planted assignment failure")
        with mock.patch("neoxider_agents.windows.Job.assign", side_effect=reject_assignment):
            with self.assertRaisesRegex(OSError, "planted"):
                process.spawn([sys.executable, str(provider)], self.prompt)
        self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
