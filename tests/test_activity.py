"""Offline activity capture/digest regression checks (stdlib only)."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import activity
import stream_text_filter


class ActivityTests(unittest.TestCase):
    def setUp(self):
        scratch = Path("D:/Temp/agents-ux") if os.name == "nt" else Path(tempfile.gettempdir()) / "agents-oc"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch, prefix="activity-")
        self.addCleanup(self.temp.cleanup)
        self.log = Path(self.temp.name) / "test.log"

    def invoke(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            activity.main([str(self.log), *args])
        return out.getvalue()

    def fixture(self, engine):
        self.log.write_bytes((ROOT / "tests" / "fixtures" / ("activity-%s.jsonl" % engine)).read_bytes())

    def test_codex_digest(self):
        self.fixture("codex")
        result = self.invoke()
        for text in ("command", "git status --short", "exit 0", "created", "src/new.py", "edited", "thinking", "Finished", "usage=", "42"):
            self.assertIn(text, result)
        self.assertNotIn("private reasoning", result)

    def test_opencode_digest(self):
        self.fixture("opencode")
        result = self.invoke()
        for text in ("sleep 30", "exit 0", "read", "edited", "src/main.py", "Follow-up", "usage=", "50"):
            self.assertIn(text, result)

    def test_claude_digest(self):
        self.fixture("claude")
        result = self.invoke()
        for text in ("thinking", "python -m unittest", "read", "created", "src/new.py", "Verified", "usage="):
            self.assertIn(text, result)
        self.assertNotIn("private reasoning", result)

    def test_kimi_digest(self):
        self.log.write_text(json.dumps({"role": "assistant", "tool_calls": [{"function": {"name": "Shell", "arguments": '{"command":"echo kimi"}'}}]}) + "\n", encoding="utf-8")
        self.assertIn("echo kimi", self.invoke())
        self.assertIn("command", self.invoke())

    def test_redaction_in_raw_and_plain(self):
        secret = "tok_" + "abcDEF123" * 6
        self.log.write_text("Authorization: Bearer " + secret + "\npassword=short-secret\n", encoding="utf-8")
        for args in ((), ("--raw",)):
            result = self.invoke(*args)
            self.assertNotIn(secret, result)
            self.assertNotIn("short-secret", result)
            self.assertIn("REDACTED", result)
        self.log.write_text(json.dumps({"type": "text", "part": {"text": "Authorization: Bearer " + secret}}) + "\n", encoding="utf-8")
        self.assertNotIn(secret, self.invoke("--raw"))

    def test_reasoning_not_exposed_in_raw(self):
        self.fixture("codex")
        self.assertNotIn("private reasoning", self.invoke("--raw"))

    def test_sidecar_capture_and_summary(self):
        self.log.write_text("plain text output", encoding="utf-8")
        sidecar = self.log.with_suffix(".activity.jsonl")
        with mock.patch.dict(os.environ, {"AGENT_ACTIVITY_FILE": str(sidecar)}):
            activity.record_event({"type": "item.started", "item": {"type": "command_execution", "command": "echo hello", "authorization": "secret"}}, "codex")
        captured = json.loads(sidecar.read_text(encoding="utf-8"))
        self.assertEqual(captured["engine"], "codex")
        self.assertEqual(captured["event"]["item"]["authorization"], "[REDACTED]")
        self.assertIn("echo hello", self.invoke())
        self.assertEqual(activity.summary(self.log)["kind"], "command")
        self.assertRegex(self.invoke("--summary"), r"^command\|\d+\n$")
        if os.name != "nt":
            self.assertEqual(sidecar.stat().st_mode & 0o777, 0o600)

    def test_summary_reads_only_tail(self):
        self.log.write_text("ignored\n" * 20000 + '{"type":"text","part":{"text":"latest"}}\n', encoding="utf-8")
        lines, _ = activity.read_tail(self.log)
        self.assertLessEqual(sum(len(line) + 1 for line in lines), activity.TAIL_BYTES)
        self.assertEqual(activity.summary(self.log)["kind"], "text")

    def test_follow_waits_for_settlement_and_final_append(self):
        self.log.write_text('{"type":"text","part":{"text":"first"}}\n', encoding="utf-8")
        meta = self.log.with_suffix(".meta")
        meta.write_text("state=running\n", encoding="utf-8")

        def writer():
            time.sleep(0.12)
            with self.log.open("a", encoding="utf-8") as handle:
                handle.write('{"type":"text","part":{"text":"second')
                handle.flush()
                time.sleep(0.12)
                handle.write('"}}\n')
            meta.write_text("state=stopped\n", encoding="utf-8")

        thread = threading.Thread(target=writer)
        thread.start()
        result = self.invoke("-f")
        thread.join(2)
        self.assertEqual(result.count("first"), 1)
        self.assertEqual(result.count("second"), 1)

    def test_default_last_25_and_plain_note(self):
        self.log.write_text("\n".join("line-%03d" % i for i in range(40)) + "\n", encoding="utf-8")
        result = self.invoke()
        self.assertIn("structured activity unavailable", result)
        self.assertNotIn("line-014", result)
        self.assertIn("line-015", result)
        self.assertEqual(len(result.splitlines()), 26)

    def test_errors_retries_and_other_tools(self):
        self.log.write_text('\n'.join(json.dumps(event) for event in [
            {"type": "retry", "message": "retry after timeout"},
            {"type": "error", "message": "provider failed"},
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Glob", "input": {"pattern": "*.py"}}]}},
        ]) + '\n', encoding="utf-8")
        result = self.invoke()
        for text in ("retry", "provider failed", "Glob", "*.py"):
            self.assertIn(text, result)

    def test_capture_optional_failure_preserves_filter(self):
        fixture = (ROOT / "tests" / "fixtures" / "activity-claude.jsonl").read_text(encoding="utf-8")
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"AGENT_ACTIVITY_FILE": str(Path(self.temp.name) / "absent" / "broken.jsonl")}):
            stream_text_filter.main(io.StringIO(fixture), out, activity=False)
        self.assertIn("Verified successfully.", out.getvalue())

    def test_provider_emitters_capture_without_changing_answer(self):
        if os.name == "nt":
            bash = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git/bin/bash.exe"
        else:
            bash = Path("/bin/bash")
        if not bash.exists():
            self.skipTest("Git Bash unavailable")
        sidecar = self.log.with_suffix(".activity.jsonl")
        env = dict(os.environ, AGENT_ACTIVITY_FILE=str(sidecar), AGENT_CLI_LOGS=self.temp.name)
        for engine in ("codex", "opencode", "kimi"):
            if engine == "kimi":
                payload = '{"role":"assistant","content":"Finished."}\n'
            else:
                payload = (ROOT / "tests" / "fixtures" / ("activity-%s.jsonl" % engine)).read_text(encoding="utf-8")
            fixture = Path(self.temp.name) / (engine + ".jsonl")
            fixture.write_text(payload, encoding="utf-8")
            script = 'HERE="$PWD"; _agent_python() { _AGENT_PY=python; }; source "providers/' + engine + '/provider.sh"; _provider_' + engine + '_emit < "$ACTIVITY_FIXTURE"'
            current_env = dict(env, ACTIVITY_FIXTURE=str(fixture))
            result = subprocess.run([str(bash), "-c", script], cwd=ROOT, env=current_env, capture_output=True, text=True, encoding="utf-8", timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("---------- output ----------", result.stdout)
            self.assertTrue(sidecar.exists())
            self.assertIn('"engine": "' + engine + '"', sidecar.read_text(encoding="utf-8"))

    def test_follow_keeps_append_between_snapshot_and_print(self):
        self.log.write_text('{"type":"text","part":{"text":"first"}}\n', encoding="utf-8")
        self.log.with_suffix(".meta").write_text("state=done\n", encoding="utf-8")
        original = activity.read_tail_snapshot

        def snapshot_then_append(*args, **kwargs):
            snapshot = original(*args, **kwargs)
            with self.log.open("a", encoding="utf-8") as handle:
                handle.write('{"type":"text","part":{"text":"race preserved"}}\n')
            return snapshot

        with mock.patch.object(activity, "read_tail_snapshot", snapshot_then_append):
            result = self.invoke("-f")
        self.assertIn("race preserved", result)

    def test_summary_of_event_larger_than_tail_is_still_activity(self):
        sidecar = self.log.with_suffix(".activity.jsonl")
        sidecar.write_text(json.dumps({"type": "text", "part": {"text": "x " * 100000}}) + "\n", encoding="utf-8")
        self.assertEqual(activity.summary(self.log)["kind"], "activity")
        self.assertRegex(self.invoke("--summary"), r"^activity\|\d+\n$")


if __name__ == "__main__":
    unittest.main()
