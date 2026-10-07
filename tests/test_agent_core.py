"""Phase 1 contracts and native lifecycle acceptance with real deterministic providers."""
import concurrent.futures
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(os.environ.get("AGENT_CORE_TEST_ROOT", Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(ROOT))
from neoxider_agents.state import Store, Lock, MARK, atomic_write, last_output
from neoxider_agents.process import hidden_kwargs, pid_alive, pid_stamp
from neoxider_agents.lifecycle import looks_waiting


class CoreTests(unittest.TestCase):
    def setUp(self):
        base = Path("D:/Temp/agents-core/tests") if os.name == "nt" else Path(tempfile.gettempdir()) / "agents-core"
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="core-", dir=str(base))
        self.base = Path(self.temp.name)
        self.work = self.base / "work"
        self.work.mkdir()
        self.store = Store(self.base / "logs")
        self.env = dict(os.environ, AGENT_CLI_LOGS=self.store.root.as_posix(),
                        AGENT_PROVIDER_DIR=(ROOT / "tests/fixtures/providers").as_posix(),
                        FIXTURE_SCRIPT=(ROOT / "tests/fixtures/core-provider.py").as_posix(),
                        AGENT_PARENT="core-tests", AGENT_ORCHESTRATOR_ID="core-tests",
                        AGENT_RETRIES="0", AGENT_TIMEOUT_SEC="90", AGENT_SILENCE_SEC="0")
        self.jobs = []

    def tearDown(self):
        for job, name in self.jobs:
            if job.poll() is None:
                self.cli("stop", name)
            try:
                if job.stdout is not None and job.stdout.closed:
                    job.stdout = None
                job.communicate(timeout=8)
            except subprocess.TimeoutExpired:
                job.kill()
                job.communicate(timeout=5)
        deadline = time.monotonic() + 3
        while True:
            try:
                self.temp.cleanup()
                break
            except PermissionError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)

    def cli(self, *argv, env=None):
        try:
            return subprocess.run([sys.executable, str(ROOT / "agent.py"), *argv], cwd=str(ROOT), env=env or self.env,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, encoding="utf-8", errors="replace", timeout=20, **hidden_kwargs())
        except subprocess.TimeoutExpired:
            self.fail("CLI did not return within 20s: " + " ".join(argv[:2]))

    def okay(self, *argv, env=None):
        result = self.cli(*argv, env=env)
        self.assertEqual(result.returncode, 0, result.stdout)
        return result.stdout

    def until(self, predicate, seconds=10):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.025)
        self.fail("readiness deadline exceeded")

    def start(self, name="task", blocked="1", **extra):
        env = dict(self.env, FIXTURE_BLOCK_TURNS=blocked, **extra)
        job = subprocess.Popen([sys.executable, str(ROOT / "agent.py"), "run", "-e", "fixture", "-t", name,
                                "-C", self.work.as_posix(), "--no-progress", "--no-terse", "ORIGINAL"], cwd=str(ROOT), env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, encoding="utf-8", errors="replace", **hidden_kwargs())
        self.jobs.append((job, name))
        self.until(lambda: (self.work / "turn.1.started").exists())
        return job

    def release(self, turn):
        (self.work / ("turn.%s.release" % turn)).touch()

    def finish(self, job, code=0):
        try:
            answer = job.communicate(timeout=12)[0]
        except subprocess.TimeoutExpired:
            self.fail("launcher did not settle within 12s after control/delivery")
        self.assertEqual(job.returncode, code, answer)
        return answer

    def seed(self, state="done", session="ses_saved", engine="fixture"):
        self.store.update("task", engine=engine, session=session, state=state, exit=0, dir=self.work.as_posix(), parent="core-tests")
        atomic_write(self.store.path("task", ".log"), MARK + "\nGOOD ANSWER\n")

    def test_prompt_file_suffix_exact_utf8(self):
        self.seed()
        prompt = self.base / "message.txt"
        text = 'Привет "мир"\nline two 🐍'
        prompt.write_text(text, encoding="utf-8")
        self.okay("reply", "task", "--no-progress", "--prompt-file", str(prompt))
        self.assertEqual((self.work / "turn.1.prompt").read_text(encoding="utf-8"), "Message #1:\n" + text)

    def test_native_cwd_and_logical_pwd_agree(self):
        env = dict(self.env, PWD=ROOT.as_posix(), FIXTURE_CHECK_PWD="1")
        self.okay("run", "-e", "fixture", "-t", "task", "-C", self.work.as_posix(), "--no-progress", "--no-terse", "CHECK CWD", env=env)
        self.assertTrue((self.work / "turn.1.finished").exists())

    def test_help_is_read_only_for_every_command(self):
        self.seed()
        before = self.store.path("task", ".meta").read_bytes(), self.store.path("task", ".log").read_bytes()
        from neoxider_agents.cli import COMMANDS
        for command in COMMANDS:
            self.assertEqual(self.cli(command, "--help").returncode, 0, command)
        self.assertEqual(before, (self.store.path("task", ".meta").read_bytes(), self.store.path("task", ".log").read_bytes()))
        self.assertFalse((self.work / "turns").exists())

    def test_unknown_flags_and_literal_double_dash(self):
        self.seed()
        for command in ("send", "stop", "peek", "wait", "list", "clean", "pending"):
            self.assertEqual(self.cli(command, "--bogus").returncode, 1)
        self.okay("send", "task", "--no-progress", "--", "--literal")
        self.assertIn("--literal", (self.work / "turn.1.prompt").read_text())
        self.assertEqual(self.cli("stop", "--", "--all-mine").returncode, 1)

    def test_running_send_queued_and_drained_before_return(self):
        job = self.start()
        self.assertIn("queued (#1)", self.okay("send", "task", "FOLLOW"))
        self.assertIsNone(job.poll())
        self.assertEqual(self.store.inbox("task")[0].read_text(), "FOLLOW")
        self.release(1)
        self.assertIn("FOLLOW", self.finish(job))
        self.assertFalse(self.store.inbox("task"))

    def test_atomic_concurrent_ordered_inbox(self):
        job = self.start()
        texts = ["FIRST\n" + "a" * 8192, "SECOND\n" + "b" * 8192]
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda text: self.cli("send", "task", text), texts))
        self.assertTrue(all(result.returncode == 0 for result in results))
        files = self.store.inbox("task")
        self.assertEqual([p.name for p in files], ["000000000001.msg", "000000000002.msg"])
        self.assertCountEqual([p.read_text() for p in files], texts)
        self.release(1)
        self.finish(job)

    def test_wait_includes_mid_drain_delivery(self):
        job = self.start(blocked="1 2")
        self.okay("send", "task", "ONE")
        self.release(1)
        self.until(lambda: (self.work / "turn.2.started").exists())
        self.okay("send", "task", "TWO")
        self.assertEqual(self.cli("wait", "task", "--timeout", "1", "--poll", "1").returncode, 2)
        self.release(2)
        self.assertIn("TWO", self.finish(job))
        wait = self.okay("wait", "task")
        self.assertIn("WAIT_DONE tasks=1 rc=0", wait)
        self.assertIn("TWO", wait)

    def test_interrupt_resumes_same_launcher_and_session(self):
        job = self.start()
        sid = self.store.session("task")
        self.okay("send", "task", "EARLIER")
        self.okay("send", "task", "--now", "INTERRUPT")
        self.assertIn("↻ INTERRUPTED+RESUMED", self.finish(job))
        self.assertFalse((self.work / "turn.1.finished").exists())
        self.assertEqual(self.store.session("task"), sid)
        self.assertEqual((self.work / "turn.2.prompt").read_text(), "Message #1:\nEARLIER\n\nMessage #2:\nINTERRUPT")

    def test_stop_reaches_launcher_and_preserves_inbox_progress(self):
        job = self.start()
        self.okay("send", "task", "KEEP")
        (self.work / "PROGRESS.task.md").write_text("partial progress")
        self.assertIn("■ STOPPED task=task", self.okay("stop", "task"))
        block = self.finish(job, 130)
        self.assertIn("■ STOPPED task=task by=orchestrator", block)
        self.assertIn("partial.txt", block)
        self.assertIn("partial result:", block)
        self.assertEqual(self.store.read("task")["state"], "stopped")
        self.assertEqual(self.store.inbox("task")[0].read_text(), "KEEP")
        self.assertEqual((self.work / "PROGRESS.task.md").read_text(), "partial progress")
        log = self.store.path("task", ".log").read_bytes()
        self.assertIn("STOPPED", self.okay("stop", "task"))
        self.assertEqual(log, self.store.path("task", ".log").read_bytes())

    def test_stop_native_grandchild_no_leftovers(self):
        job = self.start(FIXTURE_CHILD="1")
        child = int((self.work / "child.pid").read_text())
        self.assertTrue(pid_alive(child))
        self.okay("stop", "task")
        self.finish(job, 130)
        self.until(lambda: not pid_alive(child))

    def test_hard_launcher_kill_reconciles_meta_and_resume(self):
        job = self.start(FIXTURE_CHILD="1")
        child = int((self.work / "child.pid").read_text())
        sid = self.store.session("task")
        job.kill()
        job.communicate(timeout=8)
        self.until(lambda: not pid_alive(child))
        self.assertIn("launcher stopped", self.okay("status", "task"))
        self.assertIn("■ STOPPED", self.okay("result", "task"))
        self.okay("send", "task", "--no-progress", "RECOVER")
        self.assertEqual(self.store.session("task"), sid)

    def test_closed_launcher_stdout_stops_provider(self):
        job = self.start(FIXTURE_CHILD="1")
        child = int((self.work / "child.pid").read_text())
        provider = self.store.read("task")["provider_pid"]
        job.stdout.close()
        self.until(lambda: not pid_alive(provider), seconds=8)
        self.until(lambda: not pid_alive(child), seconds=8)
        job.wait(timeout=8)
        self.assertEqual(self.store.read("task")["state"], "stopped")

    def test_timeout_and_silence_print_stop_block_with_original_codes(self):
        for field, code, state in (("AGENT_TIMEOUT_SEC", 124, "error"), ("AGENT_SILENCE_SEC", 125, "silent")):
            with self.subTest(field=field):
                for p in self.work.glob("turn.*"):
                    p.unlink()
                (self.work / "turns").unlink(missing_ok=True)
                name = "timeout" if code == 124 else "silence"
                job = self.start(name=name, **{field: "1"})
                self.assertIn("■ STOPPED task=" + name, self.finish(job, code))
                self.assertEqual(self.store.read(name)["state"], state)

    def test_provider_failure_watchdog_block_and_prompt_false_positive(self):
        job = self.start(FIXTURE_PROVIDER_ERROR="1")
        block = self.finish(job, 126)
        self.assertIn("■ STOPPED task=task by=watchdog", block)
        self.assertEqual(self.store.read("task")["state"], "limited")
        self.assertIn("usage limit until tomorrow", self.store.read("task")["reason"])
        self.okay("run", "-e", "fixture", "-t", "discussion", "-C", self.work.as_posix(), "--no-progress", "--no-terse", "Discuss usage limit and rate limits")
        self.assertEqual(self.store.read("discussion")["state"], "done")

    def test_restart_same_session_and_fresh_original(self):
        job = self.start()
        sid = self.store.session("task")
        self.okay("stop", "task")
        self.finish(job, 130)
        self.okay("restart", "task", "--no-progress")
        self.assertEqual(self.store.session("task"), sid)
        self.assertIn("git status/diff", (self.work / "turn.2.prompt").read_text())
        self.okay("restart", "task", "--fresh", "--no-progress", "--no-terse")
        self.assertEqual(self.store.read("task")["previous_session"], sid)
        self.assertNotEqual(self.store.session("task"), sid)
        self.assertEqual((self.work / "turn.3.prompt").read_text(), "ORIGINAL")

    def test_preflight_failure_preserves_state_exit_answer(self):
        self.seed()
        before = self.store.read("task"), self.store.path("task", ".log").read_bytes()
        self.assertEqual(self.cli("send", "task", "-C", "missing-dir", "FAIL").returncode, 1)
        after = self.store.read("task")
        for key in ("state", "exit", "session"):
            self.assertEqual(before[0][key], after[key])
        self.assertTrue(after.get("last_send_error"))
        self.assertEqual(before[1], self.store.path("task", ".log").read_bytes())

    def test_failed_drain_retains_durable_batch(self):
        job = self.start()
        (self.work / "fail-turn-2").touch()
        self.okay("send", "task", "RETAIN")
        self.release(1)
        self.finish(job, 42)
        self.assertEqual(len(self.store.inbox("task")), 1, "failed delivery must retain its durable message")
        self.assertEqual(self.store.inbox("task")[0].read_text(), "RETAIN")
        self.assertEqual(self.store.read("task")["state"], "error")

    def test_nonresumable_refuses_without_queue(self):
        self.seed(engine="noresume")
        self.assertEqual(self.cli("send", "task", "FAIL").returncode, 1)
        self.assertFalse(self.store.inbox("task"))

    def test_inbox_drain_bound_and_flush(self):
        job = self.start(blocked="1 2", AGENT_INBOX_MAX_TURNS="1")
        self.okay("send", "task", "ONE")
        self.release(1)
        self.until(lambda: (self.work / "turn.2.started").exists())
        self.okay("send", "task", "TWO")
        self.release(2)
        self.finish(job)
        self.assertEqual(self.store.read("task")["state"], "waiting")
        self.assertTrue(self.store.inbox("task"))
        self.okay("send", "--flush", "task", "--no-progress")
        self.assertFalse(self.store.inbox("task"))

    def test_pending_seen_stopped_and_clean_protection(self):
        self.seed(state="stopped")
        with Lock(self.store.path("task", ".inbox")):
            self.store.enqueue_locked("task", "ORPHAN")
        self.store.path("task", ".md").write_text("keep")
        self.assertEqual(self.cli("pending", "--strict").returncode, 3)
        self.assertIn("1 undelivered message(s)", self.okay("status", "task"))
        self.okay("clean")
        self.assertTrue(self.store.path("task", ".md").exists())
        self.okay("clean", "--all")
        self.assertFalse(self.store.path("task", ".md").exists())
        self.okay("clean", "--purge")
        self.assertFalse(self.store.path("task", ".meta").exists())

    def test_stop_owned_only_and_requires_owner(self):
        job = self.start()
        foreign = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **hidden_kwargs())
        try:
            self.store.update("foreign", state="running", pid=foreign.pid, dir=self.work.as_posix(), parent="someone-else")
            noowner = {k: v for k, v in self.env.items() if k not in ("AGENT_PARENT", "AGENT_ORCHESTRATOR_ID")}
            self.assertEqual(self.cli("stop", "--all-mine", env=noowner).returncode, 1)
            self.okay("stop", "--all-mine")
            self.finish(job, 130)
            self.assertEqual(self.store.read("foreign")["state"], "running")
            self.assertIsNone(foreign.poll(), "bulk stop must preserve foreign ownership")
        finally:
            foreign.kill()
            foreign.wait(timeout=5)

    def test_last_reads_final_marker_and_defuses_prompt_marker(self):
        self.seed()
        text = "first\n" + MARK + "\nLAST\n[agent-activity] tool x\n"
        atomic_write(self.store.path("task", ".log"), text)
        self.assertEqual(self.okay("last", "task"), "LAST\n")
        self.assertTrue(self.store.path("task", ".seen").exists())

    def test_long_log_memory_and_tail(self):
        self.seed()
        with self.store.path("task", ".log").open("wb") as log:
            log.write(b"x" * (11 * 1024 * 1024))
            log.write(("\n" + MARK + "\nFINAL\n").encode())
        import tracemalloc
        tracemalloc.start()
        self.assertEqual(last_output(self.store.path("task", ".log")), "FINAL\n")
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        self.assertLess(peak, 1048576)

    def test_question_detector_closing_window(self):
        for text in ("Which option?\nPROGRESS.task.md updated.", "Уточни имя файла", "Please confirm the directory."):
            self.assertTrue(looks_waiting(text))
        for text in ("???", "Done. Let me know if you need anything else.", "Documents which options the CLI supports."):
            self.assertFalse(looks_waiting(text))

    def test_metadata_validation_and_concurrent_atomic_updates(self):
        self.seed()
        with self.assertRaises(ValueError):
            self.store.update("task", bad="line\ninjected=oops")
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda n: self.store.update("task", **{"field%s" % n: str(n)}), range(12)))
        self.assertEqual(sum(key.startswith("field") for key in self.store.read("task")), 12)

    def test_legacy_reads_core_and_core_reads_legacy_layout(self):
        bash = shutil.which("bash")
        if os.name == "nt":
            bash = "C:/Program Files/Git/bin/bash.exe"
        if not bash or not Path(bash).exists():
            self.skipTest("Bash unavailable for migration fixture")
        self.seed()
        old = subprocess.run([bash, (ROOT / "legacy/agent.sh").as_posix(), "last", "task"], env=self.env, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, encoding="utf-8", errors="replace", timeout=20, **hidden_kwargs())
        self.assertEqual(old.returncode, 0, old.stdout)
        self.assertEqual(old.stdout, "GOOD ANSWER\n")
        self.assertIn("session=ses_saved", self.store.path("task", ".meta").read_text())
        self.assertIn("ses_saved", self.okay("status", "task"))

    def test_live_legacy_run_is_stoppable_resumable_by_core(self):
        bash = "C:/Program Files/Git/bin/bash.exe" if os.name == "nt" else shutil.which("bash")
        if not bash:
            self.skipTest("Bash migration fixture unavailable")
        env = dict(self.env, BASH_ENV=(ROOT / "tests/fixtures/control-provider.sh").as_posix(), FIXTURE_BLOCK_TURNS="1")
        job = subprocess.Popen([bash, (ROOT / "legacy/agent.sh").as_posix(), "run", "-e", "fixture", "-t", "task",
                                "-C", self.work.as_posix(), "--no-progress", "--no-terse", "LEGACY ORIGINAL"],
                               env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, encoding="utf-8", errors="replace", **hidden_kwargs())
        self.jobs.append((job, "task"))
        self.until(lambda: (self.work / "turn.1.started").exists(), seconds=30)
        self.until(lambda: bool(self.store.session("task")), seconds=30)
        sid = self.store.session("task")
        self.assertIn("state=running", self.okay("status", "task"))
        block = self.okay("stop", "task")
        self.assertIn("STOPPED", block)
        self.assertIn("unknown (legacy task; no start snapshot)", block)
        job.communicate(timeout=10)
        self.okay("send", "task", "--no-progress", "CONTINUE")
        self.assertEqual(self.store.session("task"), sid)
        self.assertIn("CONTINUE", (self.work / "turn.2.prompt").read_text())

    def test_legacy_stop_notifies_native_owner(self):
        bash = "C:/Program Files/Git/bin/bash.exe" if os.name == "nt" else shutil.which("bash")
        if not bash:
            self.skipTest("Bash migration fixture unavailable")
        job = self.start()
        old = subprocess.run([bash, (ROOT / "legacy/agent.sh").as_posix(), "stop", "task"], env=self.env, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, encoding="utf-8", errors="replace", timeout=20, **hidden_kwargs())
        self.assertEqual(old.returncode, 0, old.stdout)
        self.assertIn("STOPPED", self.finish(job, 130))

    def test_legacy_send_queues_to_native_owner_without_replacing_it(self):
        bash = "C:/Program Files/Git/bin/bash.exe" if os.name == "nt" else shutil.which("bash")
        if not bash:
            self.skipTest("Bash migration fixture unavailable")
        job = self.start()
        owner = self.store.read("task")["pid"]
        old = subprocess.run([bash, (ROOT / "legacy/agent.sh").as_posix(), "send", "task", "LEGACY QUEUE"],
                             env=self.env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, encoding="utf-8", errors="replace",
                             timeout=20, **hidden_kwargs())
        self.assertEqual(old.returncode, 0, old.stdout)
        self.assertIn("queued", old.stdout)
        self.assertEqual(self.store.read("task")["pid"], owner)
        self.release(1)
        self.finish(job)
        self.assertIn("LEGACY QUEUE", (self.work / "turn.2.prompt").read_text())

    def test_orphan_live_provider_detected_and_stopped(self):
        from neoxider_agents.process import spawn
        prompt = self.base / "prompt.txt"
        prompt.write_text("", encoding="utf-8")
        tree = spawn([sys.executable, "-c", "import time; time.sleep(60)"], prompt, str(self.work), self.env)
        try:
            self.seed(state="running")
            self.store.update("task", core_version=2, pid=99999999, pid_start="missing", provider_pid=tree.pid,
                              provider_start=tree.stamp, job_name=tree.job_name)
            self.assertIn("orphaned provider alive", self.okay("status", "task"))
            self.okay("stop", "task")
            self.until(lambda: not pid_alive(tree.pid))
            self.assertIn("STOPPED", self.okay("result", "task"))
        finally:
            tree.close()

    def test_large_answer_last_streams_without_truncation(self):
        self.seed()
        answer = "привет" * 250000 + "\n"
        atomic_write(self.store.path("task", ".log"), MARK + "\n" + answer)
        self.assertEqual(self.okay("last", "task"), answer)

    def test_log_streams_last_step_long_lines_and_zero(self):
        self.seed()
        part = "========== [reply] fixture\n" + "я" * 600000 + "\n" + "z" * 150000 + "\nFINAL\n"
        full = "========== [run] old\nOLD\n" + part
        atomic_write(self.store.path("task", ".log"), full)
        self.assertEqual(self.okay("log", "task", "-l"), part)
        self.assertEqual(self.okay("log", "task", "-n", "3"), part.split("\n", 1)[1])
        self.assertEqual(self.okay("log", "task", "-n", "0"), full)

    def test_owner_lock_zero_fails_without_waiting(self):
        self.seed()
        path = self.store.path("task", ".owner")
        with Lock(path):
            started = time.monotonic()
            result = self.cli("send", "task", "BUSY")
            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertIn("lock timed out", result.stdout)
            self.assertLess(time.monotonic() - started, 1)

    def test_wait_honors_environment_defaults(self):
        from unittest.mock import patch
        from neoxider_agents.cli import dispatch
        env = dict(AGENT_WAIT_TIMEOUT="7", AGENT_WAIT_POLL="2")
        with patch.dict(os.environ, env), patch("neoxider_agents.views.wait", return_value=0) as waiting:
            self.assertEqual(dispatch("wait", {}, ["task"], self.store), 0)
            self.assertEqual(waiting.call_args.args[2:], (7, 2))

    def test_missing_cli_preflight_with_option_before_name_preserves_state(self):
        self.seed()
        before = self.store.read("task"), self.store.path("task", ".log").read_bytes()
        self.store.update("fixture", state="done", engine="fixture", dir=self.work.as_posix())
        result = self.cli("send", "-e", "fixture", "task", "FAIL", env=dict(self.env, FIXTURE_MISSING_CLI="1"))
        self.assertEqual(result.returncode, 1, result.stdout)
        after = self.store.read("task")
        self.assertIn("last_send_error", after)
        error = after.pop("last_send_error")
        self.assertIn("CLI not found", error)
        self.assertEqual(after, before[0])
        self.assertEqual(self.store.path("task", ".log").read_bytes(), before[1])
        self.assertFalse(self.store.inbox("task"))
        self.assertNotIn("last_send_error", self.store.read("fixture"))

    def test_fan_returns_only_after_owner_publication(self):
        env = dict(self.env, FIXTURE_BLOCK_TURNS="1", FIXTURE_START_DELAY="0.6")
        try:
            self.okay("fan", "-e", "fixture", "-t", "wave", "-C", self.work.as_posix(), "--no-progress", "ONE", env=env)
            self.assertEqual(self.store.read("wave-01").get("state"), "running")
            self.assertTrue(pid_alive(self.store.read("wave-01")["pid"]))
            self.assertEqual(self.cli("wait", "wave-01", "--timeout", "1", "--poll", "1").returncode, 2)
            self.until(lambda: (self.work / "turn.1.started").exists())
            self.release(1)
            self.okay("wait", "wave-01", "--poll", "1")
        finally:
            if self.store.read("wave-01"):
                self.cli("stop", "wave-01")

    def test_stop_during_retry_delay_reaches_launcher(self):
        (self.work / "fail-turn-1").touch()
        job = self.start(blocked="", FIXTURE_RETRY="1", AGENT_RETRIES="1", AGENT_RETRY_DELAY="30")
        self.until(lambda: "RETRY" in self.store.path("task", ".log").read_text(encoding="utf-8"))
        started = time.monotonic()
        self.okay("stop", "task")
        self.assertIn("STOPPED", self.finish(job, 130))
        self.assertLess(time.monotonic() - started, 2)
        self.assertFalse((self.work / "turn.2.started").exists())

    def test_killed_tracked_fan_wait_stops_owned_tree(self):
        env = dict(self.env, FIXTURE_BLOCK_TURNS="1")
        self.okay("fan", "-e", "fixture", "-t", "wave", "-C", self.work.as_posix(), "--no-progress", "ONE", env=env)
        observer = None
        try:
            self.until(lambda: (self.work / "turn.1.started").exists())
            provider = self.store.read("wave-01")["provider_pid"]
            observer = subprocess.Popen([sys.executable, str(ROOT / "agent.py"), "wait", "wave-01", "--poll", "1"],
                                        env=self.env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **hidden_kwargs())
            self.until(lambda: self.store.read("wave-01").get("wait_pid") == str(observer.pid))
            observer.kill()
            observer.communicate(timeout=5)
            self.until(lambda: self.store.read("wave-01").get("state") == "stopped")
            self.assertFalse(pid_alive(provider))
            self.assertIn("launcher stopped", self.okay("result", "wave-01"))
        finally:
            if observer and observer.poll() is None:
                observer.kill()
                observer.communicate(timeout=5)
            self.cli("stop", "wave-01")

    def test_wait_on_stopped_task_reports_block_and_exit_130(self):
        job = self.start()
        self.okay("stop", "task")
        self.finish(job, 130)
        result = self.cli("wait", "task")
        self.assertEqual(result.returncode, 130, result.stdout)
        self.assertIn("STOPPED", result.stdout)
        self.assertIn("WAIT_DONE tasks=1 rc=130", result.stdout)

    def test_run_stdout_keeps_session_and_output_marker(self):
        output = self.okay("run", "-e", "fixture", "-t", "task", "-C", self.work.as_posix(), "--no-progress", "--no-terse", "ROUNDTRIP")
        self.assertIn("session id: ", output)
        self.assertIn(MARK + "\nANSWER: ROUNDTRIP\n", output)
        self.assertEqual(self.okay("last", "task"), "ANSWER: ROUNDTRIP\n")

    def test_provider_error_tag_on_fast_zero_exit_still_limited(self):
        env = dict(self.env, FIXTURE_PROVIDER_ERROR="1")
        result = self.cli("run", "-e", "fixture", "-t", "task", "-C", self.work.as_posix(), "--no-progress", "ORIGINAL", env=env)
        self.assertEqual(result.returncode, 126, result.stdout)
        self.assertIn("STOPPED", result.stdout)
        self.assertEqual(self.store.read("task")["state"], "limited")


if __name__ == "__main__":
    unittest.main()
