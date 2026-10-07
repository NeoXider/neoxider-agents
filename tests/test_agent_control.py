"""Offline control acceptance: tracked fixture processes, isolated logs, no real CLIs."""
import os
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(os.environ.get("AGENT_CONTROL_TEST_ROOT", Path(__file__).resolve().parents[1]))
BASH = "C:/Program Files/Git/bin/bash.exe" if os.name == "nt" else shutil.which("bash")
DEFAULT_RESTART = ("The previous turn was interrupted. First check the working tree (git status/diff) "
                   "for partial edits, then continue the assignment from where you stopped; do not redo finished work.")


class ControlRegressions(unittest.TestCase):
    def setUp(self):
        base = Path("D:/Temp/agents-oc") if os.name == "nt" else Path(tempfile.gettempdir()) / "agents-oc"
        base.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=base, prefix="control-")
        self.base = Path(self.tmp.name)
        self.logs = self.base / "logs"
        self.logs.mkdir()
        self.work = self.base / "work"
        self.work.mkdir()
        self.env = dict(os.environ, AGENT_CLI_LOGS=self.logs.as_posix(),
                        BASH_ENV=(ROOT / "tests/fixtures/control-provider.sh").as_posix(),
                        AGENT_RETRIES="0", AGENT_SILENCE_SEC="0", AGENT_TIMEOUT_SEC="120",
                        AGENT_PARENT="control-tests", AGENT_ORCHESTRATOR_ID="control-tests")
        self.env.pop("FIXTURE_BLOCK_TURNS", None)
        self.jobs = []

    def tearDown(self):
        incomplete = []
        for job in self.jobs:
            if job.poll() is None:
                self.cli("stop", job.task_name)
                for ready in self.base.rglob("turn.*.started"):
                    ready.with_suffix(".release").touch()
            try:
                job.communicate(timeout=20)
            except subprocess.TimeoutExpired:
                job.kill()
                try:
                    job.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    # communicate's daemon reader may hold the stream lock. Closing it can hang too.
                    record = {"task": job.task_name, "launcher_pid": job.pid,
                              "returncode": job.poll(), "stage": "stdout open after kill",
                              "time": time.time(), "meta": self.meta(job.task_name)}
                    evidence = self.base.parent / "control-teardown-timeouts.jsonl"
                    with evidence.open("a", encoding="utf-8") as stream:
                        stream.write(json.dumps(record) + "\n")
                    incomplete.append(str(evidence))
        self.tmp.cleanup()
        self.assertFalse(incomplete, "fixture stdout did not close after bounded kill: " + ", ".join(incomplete))

    def cli(self, *args, env=None):
        return subprocess.run([BASH, (ROOT / "agent.sh").as_posix(), *args], env=env or self.env,
                              cwd=ROOT, text=True, encoding="utf-8", errors="replace",
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)

    def ok(self, *args, env=None):
        result = self.cli(*args, env=env)
        self.assertEqual(result.returncode, 0, result.stdout)
        return result.stdout

    def start(self, name="task", blocked="1", parent="control-tests", prompt="ORIGINAL", work=None):
        work = work or self.work
        env = dict(self.env, FIXTURE_BLOCK_TURNS=blocked, AGENT_PARENT=parent)
        job = subprocess.Popen([BASH, (ROOT / "agent.sh").as_posix(), "run", "-e", "fixture",
                                "-t", name, "-C", work.as_posix(), "--no-progress", "--no-terse", prompt],
                               env=env, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, encoding="utf-8", errors="replace")
        job.task_name = name
        self.jobs.append(job)
        self.until(lambda: (work / "turn.1.started").exists(), job)
        return job

    def until(self, predicate, job=None, seconds=100):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if predicate():
                return
            if job is not None and job.poll() is not None:
                self.fail("fixture ended before readiness: " + job.communicate()[0])
            time.sleep(0.1)
        self.fail("fixture readiness deadline exceeded")

    def finish(self, job):
        output = job.communicate(timeout=120)[0]
        self.assertEqual(job.returncode, 0, output)
        return output

    def release(self, turn, work=None):
        ((work or self.work) / f"turn.{turn}.release").touch()

    def meta(self, name="task"):
        return dict(line.split("=", 1) for line in (self.logs / (name + ".meta")).read_text(encoding="utf-8").splitlines() if "=" in line)

    def seed(self, engine="fixture", session="ses_saved", state="done", name="task"):
        (self.logs / (name + ".meta")).write_text(
            f"engine={engine}\nsession={session}\ndir={self.work.as_posix()}\nstate={state}\nexit=0\nparent=control-tests\n", encoding="utf-8")
        (self.logs / (name + ".log")).write_text("---------- output ----------\nGOOD ANSWER\n", encoding="utf-8")

    def inbox(self, messages=("ORPHAN",)):
        box = self.logs / "task.inbox"
        box.mkdir(exist_ok=True)
        for index, text in enumerate(messages, 1):
            (box / f"{index:012d}.msg").write_text(text, encoding="utf-8")
        return box

    def test_reply_prompt_file_after_name(self):
        self.seed()
        prompt = self.base / "message.txt"
        prompt.write_text("FOLLOW UP", encoding="utf-8")
        output = self.ok("reply", "task", "--no-progress", "--prompt-file", prompt.as_posix())
        self.assertIn("FOLLOW UP", output)
        self.assertEqual((self.work / "turn.1.prompt").read_text(), "Message #1:\nFOLLOW UP")
        self.assertNotIn("ANSWER: --prompt-file", output)

    def test_reply_help_starts_nothing(self):
        self.seed()
        before = (self.logs / "task.meta").read_bytes(), (self.logs / "task.log").read_bytes()
        output = self.ok("reply", "--help")
        self.assertIn("Usage:", output)
        self.assertEqual(before, ((self.logs / "task.meta").read_bytes(), (self.logs / "task.log").read_bytes()))
        self.assertFalse((self.work / "turns").exists())

    def test_running_send_is_queued(self):
        job = self.start()
        started = time.monotonic()
        output = self.ok("send", "task", "FOLLOW UP")
        self.assertIn("queued (#1)", output)
        self.assertLess(time.monotonic() - started, 30)
        self.assertIsNone(job.poll())
        self.assertEqual((self.logs / "task.inbox/000000000001.msg").read_text(), "FOLLOW UP")
        self.assertIn("command", self.ok("peek", "task", "-n", "5"))
        self.release(1)
        self.finish(job)
        self.assertIn("FOLLOW UP", (self.work / "delivered").read_text())

    def test_two_concurrent_senders_publish_complete_ordered_messages(self):
        job = self.start()
        texts = ["FIRST\n" + "a" * 4096, "SECOND\n" + "b" * 4096]
        sender_env = dict(self.env, FIXTURE_SLOW_INBOX="1")
        prompt_files = [self.base / f"sender-{index}.txt" for index in range(2)]
        for path, text in zip(prompt_files, texts):
            path.write_text(text, encoding="utf-8")
        senders = [subprocess.Popen([BASH, (ROOT / "agent.sh").as_posix(), "send", "task", "--prompt-file", path.as_posix()],
                                  env=sender_env, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  text=True, encoding="utf-8", errors="replace") for path in prompt_files]
        for sender in senders:
            sender.task_name = "task"
            self.jobs.append(sender)
        partial = []
        deadline = time.monotonic() + 120
        while any(sender.poll() is None for sender in senders) and time.monotonic() < deadline:
            for path in (self.logs / "task.inbox").glob("*.msg"):
                try:
                    complete = path.read_text() in texts
                except OSError:
                    complete = False
                if not complete:
                    partial.append(path.name)
            time.sleep(0.01)
        for sender in senders:
            output = sender.communicate(timeout=120)[0]
            self.assertEqual(sender.returncode, 0, output)
            self.assertIn("queued (#", output)
        self.assertEqual(partial, [], "a partially written inbox message was visible")
        files = sorted((self.logs / "task.inbox").glob("*.msg"))
        self.assertEqual([path.name for path in files], ["000000000001.msg", "000000000002.msg"])
        contents = [path.read_text() for path in files]
        self.assertCountEqual(contents, texts)
        self.release(1)
        self.finish(job)
        self.assertEqual((self.work / "turn.2.prompt").read_text(),
                         "Message #1:\n" + contents[0] + "\n\nMessage #2:\n" + contents[1])
        self.assertEqual(list((self.logs / "task.inbox").glob("*.msg")), [])

    def test_mid_drain_message_and_wait_finish_only_after_final_delivery(self):
        job = self.start(blocked="1 2")
        self.ok("send", "task", "FIRST FOLLOWUP")
        self.release(1)
        self.until(lambda: (self.work / "turn.2.started").exists(), job)
        self.assertEqual(self.meta()["state"], "running")
        self.assertIn("queued (#2)", self.ok("send", "task", "DURING DRAIN"))
        waiter = subprocess.Popen([BASH, (ROOT / "agent.sh").as_posix(), "wait", "task", "--poll", "1"],
                                  env=self.env, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  text=True, encoding="utf-8", errors="replace")
        waiter.task_name = "task"
        self.jobs.append(waiter)
        self.assertIsNone(waiter.poll())
        self.release(2)
        self.finish(job)
        output = waiter.communicate(timeout=120)[0]
        self.assertEqual(waiter.returncode, 0, output)
        self.assertIn("DURING DRAIN", output)
        self.assertTrue((self.work / "turn.3.finished").exists())
        self.assertEqual((self.work / "turn.3.prompt").read_text(), "Message #2:\nDURING DRAIN")
        self.assertEqual(self.meta()["state"], "done")

    def test_send_now_interrupts_and_preserves_session_and_queued_messages(self):
        job = self.start()
        self.ok("send", "task", "EARLIER QUEUE")
        sid = (self.work / "turn.1.session").read_text()
        output = self.ok("send", "task", "--now", "INTERRUPT")
        self.assertIn("queued (#2)", output)
        job.communicate(timeout=30)
        self.assertFalse((self.work / "turn.1.finished").exists())
        self.assertEqual(self.meta()["session"], sid)
        self.assertEqual((self.work / "turn.2.session").read_text(), sid)
        self.assertEqual((self.work / "turn.2.prompt").read_text(), "Message #1:\nEARLIER QUEUE\n\nMessage #2:\nINTERRUPT")
        self.assertEqual(self.meta()["state"], "done")

    def test_stop_is_idempotent_and_preserves_session_logs_inbox_and_progress(self):
        job = self.start()
        self.ok("send", "task", "KEEP QUEUE")
        sid = (self.work / "turn.1.session").read_text()
        progress = self.work / "PROGRESS.task.md"
        progress.write_text("partial progress")
        self.assertIn("stopped", self.ok("stop", "task"))
        job.communicate(timeout=30)
        self.assertEqual(self.meta()["state"], "stopped")
        self.assertEqual(self.meta()["session"], sid)
        self.assertEqual(self.meta()["reason"], "stopped by orchestrator")
        log = (self.logs / "task.log").read_bytes()
        self.assertIn("already stopped", self.ok("stop", "task"))
        self.assertEqual(log, (self.logs / "task.log").read_bytes())
        self.assertEqual(progress.read_text(), "partial progress")
        self.assertEqual((self.logs / "task.inbox/000000000001.msg").read_text(), "KEEP QUEUE")
        self.assertFalse((self.work / "turn.1.finished").exists())

    def _native_child_identity(self, pid, marker):
        if os.name == "nt":
            quoted = marker.as_posix().replace("'", "''")
            command = (f"$p=Get-CimInstance Win32_Process -Filter 'ProcessId={pid}'; "
                       f"if ($p -and $p.CommandLine.Replace('\\','/').Contains('{quoted}')) "
                       "{ $p.CreationDate.ToUniversalTime().ToString('o') }")
            result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
                                    capture_output=True, text=True, timeout=20,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout.strip()
        proc = Path(f"/proc/{pid}")
        if proc.exists():
            try:
                fields = (proc / "stat").read_text().rpartition(") ")[2].split()
                if fields[0] == "Z" or marker.as_posix().encode() not in (proc / "cmdline").read_bytes():
                    return ""
                return fields[19]
            except FileNotFoundError:
                return ""
        result = subprocess.run(["ps", "-p", str(pid), "-o", "stat=,lstart=,command="],
                                capture_output=True, text=True, timeout=20)
        line = result.stdout.strip()
        return line if line and not line.startswith("Z") and marker.as_posix() in line else ""

    def test_stop_kills_native_grandchild_and_settles_wrapper(self):
        marker = self.work / "native-child.pid"
        identity = ""
        pid = None
        try:
            job = subprocess.Popen([BASH, (ROOT / "agent.sh").as_posix(), "run", "-e", "fixture", "-t", "task",
                                    "-C", self.work.as_posix(), "--no-progress", "--no-terse", "ORIGINAL"],
                                   env=dict(self.env, FIXTURE_NATIVE_GRANDCHILD="1", FIXTURE_BLOCK_TURNS="1",
                                            FIXTURE_PYTHON=sys.executable.replace("\\", "/")), cwd=ROOT,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, encoding="utf-8", errors="replace")
            job.task_name = "task"
            self.jobs.append(job)
            self.until(lambda: (self.work / "turn.1.started").exists(), job)
            pid = int(marker.read_text())
            identity = self._native_child_identity(pid, marker)
            self.assertTrue(identity, "native fixture child was not alive before stop")
            sid = (self.work / "turn.1.session").read_text()
            self.ok("stop", "task")
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and self._native_child_identity(pid, marker) == identity:
                time.sleep(0.1)
            self.assertNotEqual(self._native_child_identity(pid, marker), identity,
                                "stop left the native grandchild alive")
            job.communicate(timeout=30)
            self.assertEqual(self.meta()["state"], "stopped")
            self.assertEqual(self.meta()["session"], sid)
            self.assertFalse((self.work / "turn.1.finished").exists())
        finally:
            if pid is not None and identity and self._native_child_identity(pid, marker) == identity:
                if os.name == "nt":
                    quoted = marker.as_posix().replace("'", "''")
                    command = (f"$p=Get-CimInstance Win32_Process -Filter 'ProcessId={pid}'; "
                               f"if ($p -and $p.CommandLine.Replace('\\','/').Contains('{quoted}') "
                               f"-and $p.CreationDate.ToUniversalTime().ToString('o') -eq '{identity}') "
                               "{ Stop-Process -Id $p.ProcessId -Force }")
                    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
                                   capture_output=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
                else:
                    import signal
                    try:
                        os.kill(pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass

    def test_stop_all_mine_leaves_foreign_task_running(self):
        mine = self.start(name="mine")
        foreign_work = self.base / "foreign-work"
        foreign_work.mkdir()
        foreign = self.start(name="foreign", parent="other-orchestrator", work=foreign_work)
        self.ok("stop", "--all-mine")
        mine.communicate(timeout=30)
        self.assertEqual(self.meta("mine")["state"], "stopped")
        self.assertEqual(self.meta("foreign")["state"], "running")
        self.assertIsNone(foreign.poll())
        self.release(1, foreign_work)
        self.finish(foreign)

    def test_stop_double_dash_does_not_reinterpret_bulk_option(self):
        self.seed()
        before = (self.logs / "task.meta").read_bytes(), (self.logs / "task.log").read_bytes()
        result = self.cli("stop", "--", "--all-mine")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("invalid task name", result.stdout)
        self.assertEqual(before, ((self.logs / "task.meta").read_bytes(), (self.logs / "task.log").read_bytes()))

    def test_restart_default_checks_partial_edits_and_keeps_session(self):
        job = self.start()
        sid = (self.work / "turn.1.session").read_text()
        self.ok("restart", "task", "--no-progress")
        job.communicate(timeout=30)
        self.assertEqual((self.work / "turn.2.prompt").read_text(), "Message #1:\n" + DEFAULT_RESTART)
        self.assertEqual(self.meta()["session"], sid)
        self.assertEqual((self.work / "partial.txt").read_text(), "partial edit survives\n")

    def test_restart_fresh_replays_original_options_and_keeps_previous_session(self):
        self.ok("run", "-e", "fixture", "-m", "model-original", "-f", "low", "-C", self.work.as_posix(),
                "-t", "task", "--no-progress", "--no-terse", "ORIGINAL PROMPT")
        old = self.meta()["session"]
        self.ok("send", "task", "LATER INSTRUCTION")
        self.ok("restart", "task", "--fresh")
        self.assertEqual((self.work / "turn.3.prompt").read_text(), "ORIGINAL PROMPT")
        current = self.meta()
        self.assertEqual(current["previous_session"], old)
        self.assertNotEqual(current["session"], old)
        self.assertEqual(current["engine"], "fixture")
        self.assertEqual(current["resolved_model"], "model-original")
        self.assertEqual(current["effort"], "low")
        self.assertEqual((self.work / "partial.txt").read_text(), "partial edit survives\n")

    def test_orphaned_inbox_visible_pending_wait_stopped_clean_and_flush(self):
        self.seed(state="stopped")
        box = self.inbox(("ONE", "TWO"))
        (self.logs / "task.md").write_text("preserve")
        self.ok("last", "task")
        pending = self.cli("pending", "--strict")
        self.assertEqual(pending.returncode, 3, pending.stdout)
        self.assertIn("2 undelivered message(s)", pending.stdout)
        self.assertIn("stopped", self.ok("wait", "task", "--timeout", "1", "--poll", "1"))
        self.assertIn("2 undelivered message(s)", self.ok("status", "task"))
        self.assertIn("2 undelivered", self.ok("list"))
        self.ok("clean")
        self.assertTrue((self.logs / "task.md").exists())
        self.assertEqual(len(list(box.glob("*.msg"))), 2)
        self.ok("send", "--flush", "task")
        self.assertEqual((self.work / "turn.1.prompt").read_text(), "Message #1:\nONE\n\nMessage #2:\nTWO")
        self.assertEqual(list(box.glob("*.msg")), [])

    def test_unknown_options_are_errors_and_double_dash_preserves_literal_text(self):
        self.seed()
        before = (self.logs / "task.log").read_bytes()
        for cmd in ("send", "reply", "restart", "stop", "peek", "list", "pending", "wait", "clean"):
            result = self.cli(cmd, "task", "--unknown")
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertIn("unknown option", result.stdout)
            self.assertIn("Usage:", result.stdout)
            self.assertEqual((self.logs / "task.log").read_bytes(), before)
        self.ok("send", "task", "--", "--help")
        self.assertEqual((self.work / "turn.1.prompt").read_text(), "Message #1:\n--help")

    def test_every_command_help_starts_nothing(self):
        self.seed()
        for cmd in ("run", "fan", "send", "reply", "stop", "restart", "peek", "log", "last", "status", "list", "pending", "wait", "clean", "prune", "doctor", "provider-info", "test-api", "gui", "openai-server"):
            output = self.ok(cmd, "--help")
            self.assertIn("usage:", output.lower())
        self.assertFalse((self.work / "turns").exists())
        self.assertEqual(self.meta()["state"], "done")

    def test_preflight_failures_preserve_previous_state_exit_and_answer(self):
        for engine, session, args in (("fixture", "", ("send", "task", "TEXT")),
                                      ("gemini", "saved", ("send", "task", "TEXT")),
                                      ("fixture", "saved", ("reply", "task", "--prompt-file", "missing-file")),
                                      ("fixture", "saved", ("send", "task", "--bad"))):
            self.seed(engine=engine, session=session)
            old = (self.logs / "task.log").read_bytes()
            result = self.cli(*args)
            self.assertNotEqual(result.returncode, 0, result.stdout)
            meta = self.meta()
            self.assertEqual((meta["state"], meta["exit"]), ("done", "0"))
            self.assertEqual((self.logs / "task.log").read_bytes(), old)
            self.assertTrue(meta.get("last_send_error"))
        self.assertFalse((self.work / "turns").exists())

    def test_failed_drain_keeps_durable_inbox_for_retry(self):
        job = self.start()
        self.ok("send", "task", "RECOVERABLE")
        (self.work / "fail-turn-2").touch()
        self.release(1)
        output = job.communicate(timeout=120)[0]
        self.assertNotEqual(job.returncode, 0, output)
        self.assertEqual(self.meta()["state"], "error")
        self.assertTrue((self.logs / "task.inbox/000000000001.msg").exists())
        self.assertEqual((self.logs / "task.inbox/000000000001.msg").read_text(), "RECOVERABLE")
        (self.work / "fail-turn-2").unlink()
        self.ok("send", "--flush", "task")
        self.assertEqual(self.meta()["state"], "done")
        self.assertEqual(list((self.logs / "task.inbox").glob("*.msg")), [])

    def test_nonresumable_engine_refuses_running_and_stopped_sends(self):
        job = self.start()
        path = self.logs / "task.meta"
        path.write_text(path.read_text().replace("engine=fixture", "engine=gemini"))
        result = self.cli("send", "task", "IMPOSSIBLE")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("supports_resume=false", result.stdout)
        self.assertEqual(self.meta()["state"], "running")
        self.assertEqual(list((self.logs / "task.inbox").glob("*.msg")), [])
        self.ok("stop", "task")
        job.communicate(timeout=30)
        result = self.cli("send", "task", "IMPOSSIBLE")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("start fresh", result.stdout)
        self.assertEqual(self.meta()["state"], "stopped")

    def test_bounded_drain_retains_remaining_messages_for_flush(self):
        self.env["AGENT_INBOX_MAX_TURNS"] = "1"
        job = self.start(blocked="1 2")
        self.ok("send", "task", "FIRST")
        self.release(1)
        self.until(lambda: (self.work / "turn.2.started").exists(), job)
        self.ok("send", "task", "SECOND")
        self.release(2)
        self.finish(job)
        self.assertEqual(self.meta()["state"], "waiting")
        self.assertIn("undelivered message", self.meta()["reason"])
        self.assertTrue((self.logs / "task.inbox/000000000002.msg").exists())
        self.ok("send", "--flush", "task")
        self.assertEqual((self.work / "turn.3.prompt").read_text(), "Message #2:\nSECOND")
        self.assertEqual(self.meta()["state"], "done")

    def test_clean_explicit_all_and_purge_allow_undelivered_cleanup(self):
        self.seed(state="stopped")
        box = self.inbox()
        (self.logs / "task.md").write_text("report")
        (self.work / "PROGRESS.task.md").write_text("progress")
        self.ok("clean", "--all")
        self.assertFalse((self.logs / "task.md").exists())
        self.assertFalse((self.work / "PROGRESS.task.md").exists())
        self.assertTrue((box / "000000000001.msg").exists())
        self.ok("clean", "--purge")
        self.assertFalse((self.logs / "task.meta").exists())
        self.assertFalse((self.logs / "task.log").exists())
        self.assertEqual(list(box.glob("*.msg")), [])

    def test_stop_refuses_mismatched_pid_generation(self):
        job = self.start()
        path = self.logs / "task.meta"
        path.write_text("\n".join("pid_start=0" if line.startswith("pid_start=") else line
                                  for line in path.read_text().splitlines()) + "\n")
        result = self.cli("stop", "task")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("refusing reused/unverifiable pid", result.stdout)
        self.assertEqual(self.meta()["state"], "running")
        self.assertIsNone(job.poll())
        self.release(1)
        self.finish(job)
        self._assert_portable_stop_verification()

    def _assert_portable_stop_verification(self):
        script = self.base / "portable-stop.sh"
        script.write_bytes(b'source "$1" list >/dev/null 2>&1\n_fixture_portability_stop "$2"\n')
        for scenario in ("stamp", "mismatch", "valid", "legacy", "unavailable", "unavailable-legacy"):
            with self.subTest(portability=scenario):
                logs = self.base / ("portable-" + scenario)
                logs.mkdir()
                result = subprocess.run([BASH, script.as_posix(), (ROOT / "agent.sh").as_posix(), scenario],
                                        env=dict(self.env, AGENT_CLI_LOGS=logs.as_posix()), cwd=ROOT,
                                        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
                meta_path = logs / "task.meta"
                meta = dict(line.split("=", 1) for line in meta_path.read_text().splitlines() if "=" in line) if meta_path.exists() else {}
                killed = (logs / "killed").read_text().splitlines() if (logs / "killed").exists() else []
                if scenario == "stamp":
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn("ps:Mon Oct", result.stdout)
                    self.assertIn("2026", result.stdout)
                elif scenario in ("valid", "legacy"):
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertEqual(meta.get("state"), "stopped")
                    self.assertEqual(meta.get("session"), "ses_portable")
                    self.assertEqual(killed, ["73199999"])
                else:
                    self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertEqual(meta.get("state"), "running")
                    self.assertEqual(meta.get("session"), "ses_portable")
                    self.assertEqual(killed, [])

    def _assert_running_continuation_can_stop(self, args):
        self.ok("run", "-e", "fixture", "-t", "task", "-C", self.work.as_posix(),
                "--no-progress", "--no-terse", "ORIGINAL")
        old = self.meta()["session"]
        job = subprocess.Popen([BASH, (ROOT / "agent.sh").as_posix(), *args],
                               env=dict(self.env, FIXTURE_BLOCK_TURNS="2"), cwd=ROOT,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, encoding="utf-8", errors="replace")
        job.task_name = "task"
        self.jobs.append(job)
        self.until(lambda: (self.work / "turn.2.started").exists(), job)
        meta = self.meta()
        self.assertEqual(meta["state"], "running")
        self.assertRegex(meta["pid"], r"^\d+$")
        probe = subprocess.run([BASH, "-c", '''IFS= read -r proc_stat < "/proc/$1/stat"
printf '%s\\n' "$proc_stat"
proc_winpid=''
if [ -r "/proc/$1/winpid" ]; then IFS= read -r proc_winpid < "/proc/$1/winpid"; fi
printf '%s\\n' "$proc_winpid"
while IFS= read -r -d '' arg; do printf '%s\\n' "$arg"; done < "/proc/$1/cmdline"''',
                                "--", meta["pid"]], env=self.env, cwd=ROOT,
                               capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
        lines = probe.stdout.splitlines()
        self.assertGreaterEqual(len(lines), 3, probe.stdout + probe.stderr)
        self.assertEqual(lines[0].rpartition(") ")[2].split()[19], meta["pid_start"])
        if os.name == "nt":
            self.assertRegex(lines[1], r"^\d+$")
            self.assertEqual(lines[1], meta["winpid"])
        else:
            self.assertEqual(int(meta["pid"]), job.pid)
        self.assertIn("agent.sh", " ".join(lines[2:]))
        self.assertIn("task", lines[2:])
        self.ok("stop", "task")
        job.communicate(timeout=30)
        self.assertEqual(self.meta()["state"], "stopped")
        self.assertFalse((self.work / "turn.2.finished").exists())
        return old

    def test_stop_a_running_resumed_turn_uses_wrapper_pid_generation(self):
        old = self._assert_running_continuation_can_stop(("send", "task", "CONTINUE"))
        self.assertEqual(self.meta()["session"], old)

    def test_stop_a_running_fresh_restart_uses_wrapper_pid_generation(self):
        old = self._assert_running_continuation_can_stop(("restart", "task", "--fresh"))
        self.assertEqual(self.meta()["previous_session"], old)

    def test_drain_stop_between_unlock_and_resume_prevents_provider_start(self):
        self.seed(state="running")
        box = self.inbox()
        script = self.base / "drain-race.sh"
        script.write_text('''source "$1" list >/dev/null
engine=fixture; tname=task; dir="$2"; log="$LOGDIR/task.log"; rc=0
# Reproduce stop precisely after the drain releases its lock, before resume publication.
resolve_session() { meta_set task state stopped; printf '%s' ses_saved; }
provider_dispatch_resume() { printf 'started' > "$dir/unexpected-provider"; rc=0; }
_finish_with_inbox task
''', encoding="utf-8")
        result = subprocess.run([BASH, script.as_posix(), (ROOT / "agent.sh").as_posix(), self.work.as_posix()],
                                env=self.env, cwd=ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.meta()["state"], "stopped")
        self.assertTrue((box / "000000000001.msg").exists())
        self.assertFalse((self.work / "unexpected-provider").exists())

    def test_interrupted_fresh_session_recovers_new_hint_and_refuses_previous_hint(self):
        self.ok("run", "-e", "fixture", "-t", "task", "-C", self.work.as_posix(),
                "--no-progress", "--no-terse", "ORIGINAL")
        old = self.meta()["session"]
        job = subprocess.Popen([BASH, (ROOT / "agent.sh").as_posix(), "restart", "task", "--fresh"],
                               env=dict(self.env, FIXTURE_BLOCK_TURNS="2"), cwd=ROOT,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, encoding="utf-8", errors="replace")
        job.task_name = "task"
        self.jobs.append(job)
        self.until(lambda: (self.work / "turn.2.started").exists(), job)
        new = (self.work / "turn.2.session").read_text()
        self.assertNotEqual(new, old)
        self.until(lambda: f"session id: {new}" in (self.logs / "task.log").read_text(), job)
        self.ok("stop", "task")
        job.communicate(timeout=30)
        self.assertEqual(self.meta()["session"], new)
        self.assertEqual(self.meta()["previous_session"], old)
        log_path = self.logs / "task.log"
        saved_log = log_path.read_text()
        log_path.write_bytes(saved_log.replace(f"session id: {new}", "fresh session hint unavailable").encode("utf-8"))
        meta_path = self.logs / "task.meta"
        meta_path.write_bytes(("\n".join("session=" if line.startswith("session=") else line
                                      for line in meta_path.read_text().splitlines()) + "\n").encode("utf-8"))
        refused = self.cli("send", "task", "DO NOT RESUME OLD SESSION")
        self.assertNotEqual(refused.returncode, 0, refused.stdout)
        self.assertIn("session id", refused.stdout)
        self.assertEqual(self.meta()["state"], "stopped")
        self.assertFalse((self.work / "turn.3.started").exists())
        log_path.write_bytes(saved_log.encode("utf-8"))
        self.ok("send", "task", "CONTINUE NEW SESSION")
        self.assertEqual((self.work / "turn.3.session").read_text(), new)

    def test_send_now_without_session_rejects_before_stop_or_queue(self):
        job = self.start()
        self.until(lambda: '"command_execution"' in (self.logs / "task.log").read_text(), job)
        meta_path = self.logs / "task.meta"
        meta_path.write_bytes(("\n".join("session=" if line.startswith("session=") else line
                                      for line in meta_path.read_text().splitlines()) + "\n").encode("utf-8"))
        log_path = self.logs / "task.log"
        log_path.write_bytes(("\n".join(line for line in log_path.read_text().splitlines()
                                     if "session id:" not in line) + "\n").encode("utf-8"))
        before, log_before = self.meta(), log_path.read_bytes()
        result = self.cli("send", "task", "--now", "INTERRUPT WITHOUT SESSION")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("session id", result.stdout)
        for key in ("state", "exit", "session", "pid", "reason"):
            self.assertEqual(self.meta().get(key), before.get(key), key)
        self.assertEqual(log_path.read_bytes(), log_before)
        self.assertEqual(list((self.logs / "task.inbox").glob("*.msg")), [])
        self.assertIsNone(job.poll())
        self.release(1)
        self.finish(job)

    def test_finished_provider_does_not_wait_for_orphan_stdout_eof(self):
        try:
            job = subprocess.Popen([BASH, (ROOT / "agent.sh").as_posix(), "run", "-e", "fixture", "-t", "task",
                                    "-C", self.work.as_posix(), "--no-progress", "--no-terse", "ORIGINAL"],
                                   env=dict(self.env, FIXTURE_ORPHAN_STDOUT="1", FIXTURE_PYTHON=sys.executable.replace("\\", "/")),
                                   cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, encoding="utf-8", errors="replace")
            job.task_name = "task"
            self.jobs.append(job)
            self.until(lambda: (self.work / "orphan.pid").exists(), job)
            started = time.monotonic()
            self.finish(job)
            self.assertLess(time.monotonic() - started, 25, "finished provider waited for inherited stdout EOF")
            self.assertEqual(self.meta()["state"], "done")
        finally:
            pid_file = self.work / "orphan.pid"
            if pid_file.exists():
                pid = int(pid_file.read_text())
                if os.name == "nt":
                    # A PID may be reused after the mutant's 30s child exits. Match its unique argv marker.
                    marker = pid_file.as_posix().replace("'", "''")
                    command = (f"$p=Get-CimInstance Win32_Process -Filter 'ProcessId={pid}'; "
                               f"if ($p -and $p.CommandLine.Replace('\\','/').Contains('{marker}')) "
                               "{ Stop-Process -Id $p.ProcessId -Force }")
                    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
                                   capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
                else:
                    import signal
                    try:
                        os.kill(pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass


if __name__ == "__main__":
    unittest.main()
