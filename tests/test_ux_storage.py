"""Phase 2B default artifacts, bounded logs, retained controls and notifications."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(os.environ.get("AGENT_UX_TEST_ROOT", Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(ROOT))
from neoxider_agents.state import Store, atomic_write
from neoxider_agents.process import hidden_kwargs
from neoxider_agents.logs import TailWriter, digest_rows, prune_task
from neoxider_agents.output import OutputFilter


class StorageTests(unittest.TestCase):
    def setUp(self):
        base = Path("D:/Temp/agents-ux/storage") if os.name == "nt" else Path(tempfile.gettempdir()) / "agents-ux/storage"
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=str(base))
        self.base = Path(self.temp.name)
        self.work = self.base / "work"
        self.work.mkdir()
        self.logs = self.base / "state"
        self.capture = self.base / "capture"
        self.env = dict(os.environ, AGENT_CLI_LOGS=self.logs.as_posix(),
                        AGENT_PROVIDER_DIR=(ROOT / "tests/fixtures/providers").as_posix(),
                        FIXTURE_SCRIPT=(ROOT / "tests/fixtures/ux-provider.py").as_posix(),
                        UX_CAPTURE=self.capture.as_posix(), AGENT_PARENT="ux-storage",
                        AGENT_CONFIG=(self.base / "config.json").as_posix(),
                        AGENT_TIMEOUT_SEC="15", AGENT_RETRIES="0", AGENT_PROGRESS="0", AGENT_KEEP_LOGS="0", AGENT_NOTIFY="0")
        self.store = Store(self.logs)
        self.jobs = []

    def tearDown(self):
        for job in self.jobs:
            if job.poll() is None:
                self.cli("stop", "task")
            job.communicate(timeout=8)
        self.temp.cleanup()

    def cli(self, *args, env=None, stdin=None):
        return subprocess.run([sys.executable, str(ROOT / "agent.py"), *args],
                              cwd=str(self.work), env=env or self.env, input=stdin,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              encoding="utf-8", timeout=20, **hidden_kwargs())

    def run_task(self, *flags, env=None, prompt="HELLO"):
        result = self.cli("run", "-e", "fixture", "-t", "task", "--no-terse", *flags, prompt, env=env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def test_default_run_adds_no_project_files(self):
        (self.work / "existing.txt").write_text("unchanged")
        before = {p.name: p.read_bytes() for p in self.work.iterdir()}
        self.run_task()
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.work.iterdir()})
        self.assertNotIn("[Progress protocol]", (self.capture / "prompt-1.txt").read_text(encoding="utf-8"))

    def test_progress_opt_in(self):
        result = self.run_task("--progress")
        self.assertTrue((self.work / "PROGRESS.task.md").is_file())
        self.assertNotIn("progress/log echo off", result.stdout)

    def test_progress_environment_and_no_progress_alias(self):
        self.run_task(env=dict(self.env, AGENT_PROGRESS="1"))
        self.assertTrue((self.work / "PROGRESS.task.md").is_file())
        (self.work / "PROGRESS.task.md").unlink()
        self.run_task("--no-progress", env=dict(self.env, AGENT_PROGRESS="1"))
        self.assertFalse((self.work / "PROGRESS.task.md").exists())

    def test_quiet_default_and_verbose_stream(self):
        env = dict(self.env, UX_NOISE_LINES="4")
        quiet = self.run_task(env=env)
        self.assertNotIn("[noise]", quiet.stdout)
        self.assertIn("started task=task", quiet.stdout)
        self.assertIn("FINAL ANSWER", quiet.stdout)
        verbose = self.run_task("-v", env=env)
        self.assertIn("[noise]", verbose.stdout)
        self.assertNotIn("progress/log echo off", verbose.stdout)

    def test_ask_only_answer_and_exit_result(self):
        result = self.cli("ask", "-e", "fixture", "-t", "task", "hello")
        self.assertEqual(result.stdout, "FINAL ANSWER\n")
        self.assertEqual(result.stderr, "")
        result = self.cli("ask", "-e", "fixture", "-t", "fail", "hello", env=dict(self.env, UX_EXIT="7"))
        self.assertEqual(result.returncode, 7)
        self.assertEqual(result.stdout, "FINAL ANSWER\n")

    def test_bounded_raw_logs_full_answer(self):
        answer = "Б" * 40000
        self.run_task(env=dict(self.env, UX_NOISE_LINES="90", UX_ANSWER=answer, AGENT_LOG_MAX_BYTES="4096"))
        self.assertLessEqual(self.store.path("task", ".log").stat().st_size, 4096)
        last = self.cli("last", "task")
        self.assertEqual(last.stdout, answer + "\n")

    def test_keep_log_flag_and_environment(self):
        env = dict(self.env, UX_NOISE_LINES="40", AGENT_LOG_MAX_BYTES="4096")
        self.run_task("--log", env=env)
        self.assertGreater(self.store.path("task", ".log").stat().st_size, 4096)
        self.assertEqual(self.store.read("task")["keep_logs"], "1")
        self.run_task(env=dict(env, AGENT_KEEP_LOGS="1"))
        self.assertGreater(self.store.path("task", ".log").stat().st_size, 4096)

    def test_ttl_prunes_raw_and_all_views_survive(self):
        self.run_task()
        path = self.store.path("task", ".log")
        os.utime(path, (0, 0))
        for args in (("last", "task"), ("status", "task"), ("list",), ("wait", "task"), ("pending",), ("peek", "task"), ("result", "task", "--json")):
            result = self.cli(*args)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(path.exists())
        self.assertEqual(self.cli("last", "task").stdout, "FINAL ANSWER\n")
        self.assertTrue(self.store.path("task", ".answer").exists())
        self.assertTrue(self.store.path("task", ".activity.jsonl").exists())

    def test_clean_removes_persistent_raw_but_retains_session(self):
        self.run_task("--log")
        self.cli("clean")
        self.assertFalse(self.store.path("task", ".log").exists())
        self.assertEqual(self.cli("last", "task").stdout, "FINAL ANSWER\n")
        self.assertTrue(self.store.session("task"))
        self.assertEqual(self.cli("send", "task", "again").returncode, 0)

    def test_persistent_log_ignores_ttl(self):
        self.run_task("--log")
        path = self.store.path("task", ".log")
        os.utime(path, (0, 0))
        self.cli("list", env=dict(self.env, AGENT_LOG_TTL_HOURS="0"))
        self.assertTrue(path.exists())

    def test_resume_and_fresh_after_pruning(self):
        self.run_task("--progress")
        self.cli("clean")
        result = self.cli("restart", "task", "again")
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.cli("restart", "task", "--fresh")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_digest_has_no_secrets_or_reasoning(self):
        target = self.store.path("task", ".activity.jsonl")
        with patch.dict(os.environ, {"AGENT_ACTIVITY_FILE": str(target)}):
            output = OutputFilter("codex", "task", self.logs)
            output.feed(json.dumps(dict(type="item.completed", item=dict(type="command_execution", command="curl -H 'Authorization: Bearer abc123' password=hello"))) + "\n")
            output.feed(json.dumps(dict(type="item.completed", item=dict(type="reasoning", text="PRIVATE THOUGHTS"))) + "\n")
        text = target.read_text(encoding="utf-8")
        self.assertNotIn("abc123", text)
        self.assertNotIn("hello", text)
        self.assertNotIn("PRIVATE THOUGHTS", text)
        self.assertNotIn('"event"', text)
        self.assertIn("REDACTED", text)

    def test_reported_usage_and_cost_only(self):
        output = OutputFilter("codex", "task", self.logs)
        output.feed('{"type":"turn.completed","usage":{"input_tokens":17,"output_tokens":4,"secret":"abc"},"cost":0.02}\n')
        self.assertEqual(output.usage, {"input_tokens": 17, "output_tokens": 4})
        self.assertEqual(output.cost, 0.02)
        self.assertEqual(OutputFilter("codex").usage, {})

    def test_tail_writer_never_exceeds_cap_and_valid_utf8(self):
        with patch.dict(os.environ, {"AGENT_LOG_MAX_BYTES": "1024"}):
            path = self.logs / "test-tail.log"
            with TailWriter(path) as writer:
                for _ in range(60):
                    writer.write("Ж" * 800 + "\n")
                    self.assertLessEqual(path.stat().st_size, 1024)
                    path.read_text(encoding="utf-8")

    def test_legacy_answer_preserved_on_ttl(self):
        self.store.update("old", state="done", session="ses_old")
        atomic_write(self.store.path("old", ".log"), "---------- output ----------\nLEGACY ANSWER\n")
        os.utime(self.store.path("old", ".log"), (0, 0))
        self.assertEqual(self.cli("last", "old").stdout, "LEGACY ANSWER\n")

    def test_clean_legacy_answer_survives(self):
        self.store.update("old", state="done", session="ses_old")
        atomic_write(self.store.path("old", ".log"), "---------- output ----------\nLEGACY ANSWER\n")
        self.assertEqual(self.cli("clean").returncode, 0)
        self.assertEqual(self.cli("last", "old").stdout, "LEGACY ANSWER\n")

    def test_reused_run_resets_opt_ins(self):
        self.run_task("--progress", "--log", "--owns", "owned.txt")
        (self.work / "PROGRESS.task.md").unlink()
        self.run_task()
        self.assertFalse((self.work / "PROGRESS.task.md").exists())
        self.assertEqual(self.store.read("task")["keep_logs"], "0")
        self.assertEqual(self.store.read("task")["owns"], "")

    def test_strict_resume_refuses_running_overlap_before_queueing(self):
        self.run_task("--owns", "shared.txt")
        self.store.update("other", state="running", dir=str(self.work), owns="shared.txt", pid=os.getpid())
        result = self.cli("send", "task", "--strict-owns", "continue")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("ownership overlap", result.stderr)
        self.assertFalse(self.store.inbox("task"))
        self.assertEqual(self.store.read("task")["state"], "done")

    def test_structured_failed_ask_never_prints_raw_json(self):
        from neoxider_agents import lifecycle
        from neoxider_agents.providers import get_provider
        output = OutputFilter("codex", "task", self.logs)
        output.feed('{"type":"turn.failed","error":{"message":"quota exceeded"}}\n')
        atomic_write(self.store.path("task", ".log"), '---------- output ----------\n{"type":"turn.failed"}\n')
        class Turn:
            last_activity = "error"
            def __init__(self, *args):
                pass
            def run(self):
                with self_store.path("task", ".log").open("a", encoding="utf-8") as raw:
                    raw.write('{"type":"turn.failed","error":{"message":"quota exceeded"}}\n')
                return 1, output, None, "", ""
        self_store = self.store
        out = io.StringIO()
        from contextlib import redirect_stdout, redirect_stderr
        with patch.object(lifecycle, "Turn", Turn), redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = lifecycle.execute(self.store, "task", {"dir": str(self.work), "ask": True}, get_provider("codex"), "model", "", "", "hello", False, [])
        self.assertEqual(code, 126)
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(self.store.path("task", ".answer").read_text(encoding="utf-8"), "")

    def test_opencode_usage_accumulates_and_normalizes_counts(self):
        output = OutputFilter("opencode", "task", self.logs)
        for _ in range(2):
            output.feed('{"type":"step_finish","part":{"tokens":{"input":10,"output":3,"total":20,"reasoning":1,"cache":{"read":2,"write":4}},"cost":0.01}}\n')
        self.assertEqual(output.usage["input_tokens"], 20)
        self.assertEqual(output.usage["output_tokens"], 6)
        self.assertEqual(output.usage["total_tokens"], 40)
        self.assertEqual(output.usage["cache_read_tokens"], 4)
        self.assertEqual(output.cost, 0.02)

    def test_auto_task_name_redacts_prompt_secrets(self):
        from neoxider_agents.cli import task_name
        name, reservation = task_name(self.store, "password=LEAKED_NAME inspect file")
        reservation.unlink()
        self.assertNotIn("leaked", name)

    def test_one_line_errors_redact_credentials(self):
        result = self.cli("run", "--password=DO_NOT_LEAK_ERROR")
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("DO_NOT_LEAK_ERROR", result.stderr)
        self.assertIn("REDACTED", result.stderr)
        self.assertEqual(len(result.stderr.strip().splitlines()), 1)

    def test_git_baseline_disables_index_refresh(self):
        from neoxider_agents.reporting import _git_status
        (self.work / ".git").mkdir()
        with patch("subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = b""
            _git_status(str(self.work))
        self.assertIn("--no-optional-locks", run.call_args.args[0])

    def test_default_entry_never_creates_project_bytecode(self):
        local = self.base / "client"
        import shutil
        shutil.copytree(ROOT / "neoxider_agents", local / "neoxider_agents", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy2(ROOT / "agent.py", local / "agent.py")
        shutil.copy2(ROOT / "activity.py", local / "activity.py")
        env = dict(self.env)
        env.pop("PYTHONDONTWRITEBYTECODE", None)
        result = subprocess.run([sys.executable, str(local / "agent.py"), "run", "-e", "fixture", "-t", "bytecode", "hello"],
                                cwd=str(local), env=env, capture_output=True, encoding="utf-8", timeout=20, **hidden_kwargs())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(list(local.rglob("__pycache__")))

    def test_log_follow_resumes_after_tail_truncation(self):
        from neoxider_agents.views import log
        from threading import Thread, Event
        from contextlib import redirect_stdout
        self.store.update("task", state="running", pid=os.getpid())
        path = self.store.path("task", ".log")
        atomic_write(path, "OLD" * 2000 + "\n")
        def write_tail():
            Event().wait(.08)
            with TailWriter(path) as writer:
                writer.write("NEW_ACTIVITY" * 40 + "\n")
            Event().wait(.25)
            self.store.update("task", state="done")
        with patch.dict(os.environ, {"AGENT_LOG_MAX_BYTES": "1024"}), redirect_stdout(io.StringIO()) as out:
            thread = Thread(target=write_tail)
            thread.start()
            log(self.store, "task", {"-f": True})
            thread.join(timeout=2)
        self.assertIn("NEW_ACTIVITY", out.getvalue())

    def test_notifications_hidden_and_failure_optional(self):
        from neoxider_agents.notifications import notify_task
        with patch("neoxider_agents.notifications.shutil.which", return_value="powershell.exe"), patch("neoxider_agents.notifications.subprocess.run") as run:
            run.return_value.returncode = 0
            self.assertTrue(notify_task("task", "waiting"))
            if os.name == "nt":
                args, kwargs = run.call_args
                self.assertIn("Hidden", args[0])
                self.assertTrue(kwargs.get("creationflags", 0) & subprocess.CREATE_NO_WINDOW)
                self.assertEqual(kwargs["startupinfo"].wShowWindow, 0)
            run.side_effect = OSError("not available")
            self.assertFalse(notify_task("task", "done"))

    def test_large_cyrillic_prompt_never_in_provider_argv(self):
        prompt = "Привет мир " * 6000
        path = self.base / "prompt.txt"
        path.write_text(prompt, encoding="utf-8")
        result = self.cli("run", "-e", "fixture", "-t", "task", "--no-terse", "-p", str(path))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.capture / "prompt-1.txt").read_text(encoding="utf-8"), prompt)
        self.assertNotIn("Привет", (self.capture / "argv-1.json").read_text(encoding="utf-8"))

    def test_metadata_and_control_history_redact_secrets(self):
        self.run_task()
        result = self.cli("send", "task", "password=DO_NOT_STORE_MESSAGE")
        self.assertEqual(result.returncode, 0, result.stderr)
        text = self.store.path("task", ".history.jsonl").read_text(encoding="utf-8")
        self.assertIn('"kind": "send"', text)
        self.assertNotIn("DO_NOT_STORE_MESSAGE", text)
        from neoxider_agents.logs import record_digest
        record_digest(self.store.path("task", ".activity.jsonl"), "tool", "password=DO_NOT_STORE_DIGEST")
        self.assertNotIn("DO_NOT_STORE_DIGEST", self.store.path("task", ".activity.jsonl").read_text(encoding="utf-8"))

    def test_raw_provider_event_is_retained_separately_from_digest(self):
        from neoxider_agents.runtime import Turn
        class Provider:
            engine = "codex"
        self.store.update("task", state="running")
        turn = Turn(self.store, "task", Provider(), "model", "", str(self.work), "", "PROMPT")
        event = '{"type":"item.completed","item":{"type":"command_execution","command":"read-file.txt"}}\n'
        target = self.store.path("task", ".activity.jsonl")
        with patch.dict(os.environ, {"AGENT_ACTIVITY_FILE": str(target)}):
            with TailWriter(self.store.path("task", ".log")) as writer:
                turn._line(event, writer, OutputFilter("codex", "task", self.logs))
        self.assertIn(event, self.store.path("task", ".log").read_text(encoding="utf-8"))
        self.assertNotIn('"item"', target.read_text(encoding="utf-8"))

    def test_fan_forwards_opt_ins_and_has_no_default_launcher_transcript(self):
        args = ("fan", "-e", "fixture", "-t", "wave", "-C", str(self.work), "--no-terse", "--progress", "--owns", "owned.txt", "ONE")
        result = self.cli(*args)
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.cli("wait", "wave-01", "--poll", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.work / "PROGRESS.wave-01.md").exists())
        self.assertFalse(self.store.path("wave-01", ".launcher.log").exists())
        self.assertEqual(self.store.read("wave-01")["owns"], "owned.txt")


if __name__ == "__main__":
    unittest.main()
