"""Independent migration/diagnostics/output contracts; no real providers or services."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from neoxider_agents.state import MARK, Store, atomic_write
from neoxider_agents import cli, diagnostics, lifecycle, views
from neoxider_agents.output import OutputFilter
from neoxider_agents.providers import get_provider


class Contracts(unittest.TestCase):
    def setUp(self):
        base = Path(tempfile.gettempdir()) / "agents-core/review"
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="contracts-", dir=base)
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.store = Store(self.folder / "logs")
        self.work = self.folder / "work"
        self.work.mkdir()
        environment = patch.dict(os.environ, {"AGENT_CLI_LOGS": self.store.root.as_posix()})
        environment.start()
        self.addCleanup(environment.stop)

    def doctor_json(self):
        rows = {
            "claude": {"engine": "claude", "version": "fixture", "available": True, "login": "not logged in",
                       "limits": {"windows": [{"label": "5h", "used_percent": 37, "window_minutes": 300,
                                                "resets_at": "2026-10-07T09:00:00Z"}]}, "note": "fixture"},
            "codex": {"engine": "codex", "version": "fixture", "available": True, "login": "CLI ok",
                      "limits": {"plan_type": "fixture", "primary": {"used_percent": 81, "window_minutes": 300,
                                                                   "resets_at": "2026-10-07T09:00:00"}}, "note": ""},
            "gemini": {"engine": "gemini", "version": "NOT_FOUND", "available": False, "login": "", "limits": None, "note": ""}}
        class Provider:
            def __init__(self, engine):
                self.engine = engine
            def doctor(self):
                return copy.deepcopy(rows[self.engine])
        output = io.StringIO()
        with patch.object(diagnostics, "provider_info", return_value={key: {} for key in rows}), patch.object(diagnostics, "bounded_probes", return_value=[copy.deepcopy(row) for row in rows.values()]), contextlib.redirect_stdout(output):
            code = diagnostics.doctor(self.store, {"--json": True}, [])
        self.assertEqual(code, 0)
        return json.loads(output.getvalue())

    def test_doctor_legacy_json_shape(self):
        payload = self.doctor_json()
        self.assertEqual(set(payload), {"generated_at", "engines", "raw", "deep_engines"})
        self.assertIsInstance(payload["generated_at"], (int, float))
        self.assertEqual([row["engine"] for row in payload["engines"]], ["claude", "codex", "gemini"])
        self.assertEqual(payload["deep_engines"], ["codex"])

    def test_doctor_states_and_reset_epochs(self):
        payload = self.doctor_json()
        self.assertEqual([row["state"] for row in payload["engines"]], ["not_logged_in", "ok", "not_installed"])
        first, second = payload["engines"][:2]
        self.assertEqual(first["limits"]["windows"][0]["resets_at"], second["limits"]["primary"]["resets_at"])
        self.assertIsInstance(second["limits"]["primary"]["resets_at"], float)

    def test_doctor_raw_includes_limits_and_deep_hint(self):
        raw = self.doctor_json()["raw"]
        self.assertIn("=== engines (CLI) ===", raw)
        self.assertIn("81%", raw)
        self.assertIn("window 5h", raw)
        self.assertIn("doctor --deep", raw)

    def test_doctor_unexpected_operand_rejected(self):
        with self.assertRaises(ValueError):
            diagnostics.doctor(self.store, {}, ["surprise"])

    def test_doctor_deep_captures_run_and_restores_policy(self):
        class Provider:
            def doctor(self):
                return dict(engine="codex", version="fixture", available=True, login="ok", limits=None)
        calls = []
        def run(store, name, prompt, opts):
            calls.append((opts, os.environ["AGENT_TIMEOUT_SEC"], os.environ["AGENT_CHAT_ONLY"]))
            print("unfiltered provider output")
            print("unfiltered provider error", file=sys.stderr)
            return 124
        output = io.StringIO()
        with patch.dict(os.environ, {"AGENT_TIMEOUT_SEC": "900", "AGENT_CHAT_ONLY": "0"}), patch.object(diagnostics, "provider_info", return_value={"codex": {}}), patch.object(diagnostics, "bounded_probes", return_value=[Provider().doctor()]), patch.object(lifecycle, "run", side_effect=run), contextlib.redirect_stdout(output):
            code = diagnostics.doctor(self.store, {"--deep": True}, [])
            self.assertEqual(os.environ["AGENT_TIMEOUT_SEC"], "900")
            self.assertEqual(os.environ["AGENT_CHAT_ONLY"], "0")
        self.assertEqual(code, 0)
        self.assertIn("codex shell: BROKEN", output.getvalue())
        self.assertNotIn("unfiltered provider", output.getvalue())
        self.assertEqual(calls[0][1:], ("60", "1"))
        self.assertEqual(calls[0][0]["model"], "spark")

    def test_legacy_model_resume_prefers_resolved_model(self):
        self.store.update("task", engine="codex", dir=self.work.as_posix(), state="done", session="ses_saved",
                          model="gpt-6.1-sol-medium", resolved_model="gpt-6.1-sol", effort="medium")
        selected = lifecycle.select_options(self.store, "task", {})
        self.assertEqual(selected["model"], "gpt-6.1-sol")
        self.assertEqual(lifecycle.select_options(self.store, "task", {"model": "gpt-explicit"})["model"], "gpt-explicit")

    def test_restart_stops_before_resume_writer_preparation(self):
        self.store.update("task", engine="codex", dir=self.work.as_posix(), state="running", session="ses_saved")
        stopped = []
        class Provider:
            supports_resume = True
            def resolve(self, model, effort):
                return model, effort
            def command(self, *args, **kwargs):
                return [sys.executable], {}
            def prepare_resume(self, session, cwd):
                if not stopped:
                    raise AssertionError("resume writer preparation ran before stop")
        with patch("neoxider_agents.providers.get_provider", return_value=Provider()), patch.object(lifecycle, "stop", side_effect=lambda *args: stopped.append(True)), patch.object(lifecycle, "send", return_value=0):
            self.assertEqual(lifecycle.restart(self.store, "task", "continue", {}), 0)
        self.assertTrue(stopped)

    def test_new_run_reused_name_uses_fresh_defaults(self):
        self.store.update("task", engine="codex", model="gpt-6.1-sol-medium", resolved_model="gpt-6.1-sol",
                          effort="medium", dir=self.work.as_posix(), state="done", session="ses_saved")
        captured = []
        def preflight(store, name, opts, resume):
            captured.append(opts)
            return opts, object(), "fixture", "", ""
        with patch.object(lifecycle, "preflight", side_effect=preflight), patch.object(lifecycle, "execute", return_value=0):
            self.assertEqual(lifecycle.run(self.store, "task", "new prompt", {}), 0)
        self.assertEqual(captured[0]["engine"], "claude")
        self.assertEqual(captured[0]["model"], "")
        self.assertEqual(captured[0]["effort"], "")
        self.assertEqual(captured[0]["dir"], os.getcwd())

    def execute_retry(self, engine, failure, code, action):
        first = OutputFilter(engine)
        first.session = "ses_saved"
        first.last_assistant = "partial response"
        first.raw.append(failure)
        first.provider_error = failure if code == 126 else ""
        final = OutputFilter(engine)
        final.session = "ses_saved"
        final.last_assistant = "complete response"
        final.had_answer = True
        outcomes = [(code, first, action, failure if action == "failure" else "", "watchdog"),
                    (0, final, "", "", "")]
        class Turn:
            last_activity = "fixture"
            def __init__(self, *args):
                pass
            def run(self):
                return outcomes.pop(0)
        provider = get_provider(engine)
        with patch.object(lifecycle, "Turn", Turn), patch.object(lifecycle, "retry_wait", return_value=True), patch.object(lifecycle, "begin_snapshot"), patch.object(lifecycle, "changed_files", return_value=[]), patch.object(lifecycle, "render_md"), patch.object(provider, "prepare_resume"), patch.dict(os.environ, {"AGENT_RETRIES": "1", "AGENT_RETRY_DELAY": "0"}), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            result = lifecycle.execute(self.store, "task", {"dir": self.work.as_posix(), "--no-progress": True},
                                       provider, "fixture", "", "ses_saved", "prompt", True, [])
        self.assertEqual(result, 0)
        self.assertEqual(outcomes, [])
        self.assertEqual(self.store.read("task")["state"], "done")

    def test_codex_writer_conflict_retried_before_limited(self):
        self.execute_retry("codex", "thread-store conflict: already has an active writer", 126, "failure")

    def test_opencode_retry_reads_error_despite_partial_answer(self):
        self.execute_retry("opencode", "fetch failed: connection reset", 1, "")

    def run_turns(self, engine, outcomes, env=None, delivered=None, patch_retry_wait=True):
        """Drive lifecycle.execute over scripted turn outcomes; returns (exit code, notified states)."""
        class Turn:
            last_activity = "fixture"
            def __init__(self, *args):
                pass
            def run(self):
                return outcomes.pop(0)
        provider = get_provider(engine)
        notified = []
        settings = {"AGENT_RETRY_DELAY": "0"}
        settings.update(env or {})
        waiter = patch.object(lifecycle, "retry_wait", return_value=True) if patch_retry_wait else contextlib.nullcontext()
        with patch.object(lifecycle, "Turn", Turn), waiter, \
                patch.object(lifecycle, "begin_snapshot"), patch.object(lifecycle, "changed_files", return_value=[]), \
                patch.object(lifecycle, "render_md"), patch.object(provider, "prepare_resume"), \
                patch("neoxider_agents.notifications.notify_task", side_effect=lambda name, state: notified.append(state) or True), \
                patch.dict(os.environ, settings), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            result = lifecycle.execute(self.store, "task", {"dir": self.work.as_posix(), "--no-progress": True},
                                       provider, "fixture", "", "ses_saved", "prompt", True, delivered or [])
        return result, notified

    @staticmethod
    def transport_failure(engine, message):
        failed = OutputFilter(engine)
        failed.session = "ses_saved"
        failed.provider_error = message
        failed.raw.append(message)
        return (126, failed, "failure", message, "watchdog")

    @staticmethod
    def finished(engine):
        final = OutputFilter(engine)
        final.session = "ses_saved"
        final.last_assistant = "complete response"
        final.had_answer = True
        return (0, final, "", "", "")

    def test_transport_fault_is_retried_and_the_task_finishes(self):
        blip = '{"type": "provider.transport", "message": "getaddrinfo ENOTFOUND opencode.ai"}'
        outcomes = [self.transport_failure("opencode", blip), self.transport_failure("opencode", "ECONNRESET"),
                    self.finished("opencode")]
        result, notified = self.run_turns("opencode", outcomes)
        self.assertEqual(result, 0)
        self.assertEqual(outcomes, [])
        self.assertEqual(self.store.read("task")["state"], "done")
        self.assertEqual(notified, [])
        self.assertIn("RETRY 2/5 after transient network transport failure",
                      self.store.path("task", ".log").read_text(encoding="utf-8"))

    def test_transport_retries_run_out_then_the_task_fails_and_notifies(self):
        outcomes = [self.transport_failure("opencode", "ECONNRESET: The socket connection was closed unexpectedly")
                    for _ in range(4)]
        result, notified = self.run_turns("opencode", outcomes, {"AGENT_TRANSPORT_RETRIES": "2"})
        self.assertEqual(result, 126)
        self.assertEqual(len(outcomes), 1, "two retries after the first failure, then no further turn")
        meta = self.store.read("task")
        self.assertEqual(meta["state"], "error")
        self.assertEqual(notified, ["error"])
        log = self.store.path("task", ".log").read_text(encoding="utf-8")
        self.assertIn("RETRIES EXHAUSTED 2/2", log)
        self.assertIn("gave up after 2 automatic retries", json.dumps(meta))

    def test_transport_retry_can_be_muted_and_switched_off(self):
        outcomes = [self.transport_failure("opencode", "ECONNRESET") for _ in range(2)]
        result, notified = self.run_turns("opencode", outcomes, {"AGENT_TRANSPORT_RETRIES": "0"})
        self.assertEqual((result, len(outcomes), notified), (126, 1, []))
        outcomes = [self.transport_failure("opencode", "ECONNRESET") for _ in range(3)]
        result, notified = self.run_turns("opencode", outcomes,
                                          {"AGENT_TRANSPORT_RETRIES": "1", "AGENT_NOTIFY_RETRY_EXHAUSTED": "0"})
        self.assertEqual((result, len(outcomes), notified), (126, 1, []))

    def test_free_tier_limit_is_waited_out_with_growing_pauses_then_gives_up_as_limited(self):
        limit = ('{"type":"provider.free-limit","message":"Free usage limit reached. Connect a provider or '
                 'subscribe to OpenCode Go to keep going, or switch models.","status":429}')
        pauses = []
        outcomes = [self.transport_failure("opencode", limit), self.transport_failure("opencode", limit),
                    self.finished("opencode")]
        with patch.object(lifecycle, "retry_wait", side_effect=lambda turn, seconds: pauses.append(seconds) or True):
            result, notified = self.run_turns("opencode", outcomes, patch_retry_wait=False)
        self.assertEqual((result, outcomes, notified), (0, [], []))
        self.assertEqual(pauses, [300, 600])
        outcomes = [self.transport_failure("opencode", limit) for _ in range(3)]
        result, notified = self.run_turns("opencode", outcomes, {"AGENT_FREE_LIMIT_RETRIES": "1"})
        self.assertEqual((result, len(outcomes), notified), (126, 1, ["limited"]))
        self.assertEqual(self.store.read("task")["state"], "limited")

    FREE_LIMIT_EVENT = ('{"type":"error","timestamp":1795604916927,"sessionID":"ses_saved","error":{"type":'
                        '"provider.free-limit","message":"Free usage limit reached. Connect a provider or subscribe '
                        'to OpenCode Go to keep going, or switch models.","status":429,"retryAfterMs":%s}}')

    def free_limit_turn(self, retry_after_ms):
        """A failed turn built by feeding the live 2026-11-25 OpenCode error line through the real filter."""
        failed = OutputFilter("opencode")
        failed.session = "ses_saved"
        failed.feed(self.FREE_LIMIT_EVENT % retry_after_ms)
        return (126, failed, "failure", failed.provider_error, "watchdog")

    def test_free_limit_with_a_far_provider_reset_ends_limited_at_once_with_the_reset_time(self):
        pauses = []
        outcomes = [self.free_limit_turn(46283000), self.finished("opencode")]
        self.assertEqual(outcomes[0][1].provider_retry_after, 46283.0)
        with patch.object(lifecycle, "retry_wait", side_effect=lambda turn, seconds: pauses.append(seconds) or True):
            result, _ = self.run_turns("opencode", outcomes, patch_retry_wait=False)
        meta = self.store.read("task")
        self.assertEqual((result, len(outcomes), pauses, meta["state"]), (126, 1, [], "limited"))
        self.assertIn("provider reports reset in 12.9 h", meta.get("reason", ""))
        self.assertGreater(int(meta["limit_resets_at"]), 0)

    def test_free_limit_with_a_near_provider_reset_waits_exactly_that_long(self):
        pauses = []
        outcomes = [self.free_limit_turn(90000), self.finished("opencode")]
        with patch.object(lifecycle, "retry_wait", side_effect=lambda turn, seconds: pauses.append(seconds) or True):
            result, _ = self.run_turns("opencode", outcomes, patch_retry_wait=False)
        self.assertEqual((result, outcomes, pauses), (0, [], [95.0]))

    def test_free_limit_hint_is_ignored_when_not_a_positive_number(self):
        for hint in ('"soon"', "0", "-5", "true"):
            failed = OutputFilter("opencode")
            failed.feed(self.FREE_LIMIT_EVENT % hint)
            self.assertIsNone(failed.provider_retry_after, hint)
            self.assertIn("Free usage limit reached", failed.provider_error)

    def test_a_paid_usage_limit_is_not_waited_out(self):
        outcomes = [self.transport_failure("codex", "You've hit your usage limit. Try again at Nov 25th, 2026 5:17 PM."),
                    self.finished("codex")]
        result, _ = self.run_turns("codex", outcomes)
        self.assertNotEqual(result, 0)
        self.assertEqual(len(outcomes), 1)

    def test_an_outer_owner_of_retries_switches_the_transport_retry_off(self):
        outcomes = [self.transport_failure("opencode", "ECONNRESET") for _ in range(2)]
        result, notified = self.run_turns("opencode", outcomes, {"AGENT_RETRIES": "0"})
        self.assertEqual((result, len(outcomes), notified), (126, 1, []))
        outcomes = [self.transport_failure("opencode", "ECONNRESET"), self.finished("opencode")]
        result, _ = self.run_turns("opencode", outcomes, {"AGENT_RETRIES": "0", "AGENT_TRANSPORT_RETRIES": "1"})
        self.assertEqual((result, outcomes), (0, []))

    def test_quota_and_auth_provider_errors_are_never_retried_as_transport(self):
        for message in ("You've hit your usage limit. Try again later.", "401 unauthorized: invalid api key",
                        "rate limit exceeded (ECONNRESET while reading the body)"):
            outcomes = [self.transport_failure("opencode", message), self.finished("opencode")]
            result, _ = self.run_turns("opencode", outcomes)
            self.assertNotEqual(result, 0, message)
            self.assertEqual(len(outcomes), 1, message)

    def test_transport_fault_with_another_exit_code_is_retried(self):
        failed = OutputFilter("opencode")
        failed.session = "ses_saved"
        outcomes = [(1, failed, "failure", "Transport: ECONNRESET: The socket connection was closed unexpectedly",
                     "watchdog"), self.finished("opencode")]
        result, _ = self.run_turns("opencode", outcomes)
        self.assertEqual((result, outcomes), (0, []))

    def test_transport_fault_of_a_turn_that_carries_a_queued_message_is_retried(self):
        self.store.update("task", state="running", dir=self.work.as_posix(), engine="opencode", session="ses_saved")
        outcomes = [self.transport_failure("opencode", "ECONNRESET"), self.finished("opencode")]
        calls = []
        with patch.object(lifecycle, "release_delivery", side_effect=lambda *args: calls.append(args[-1])):
            result, _ = self.run_turns("opencode", outcomes, delivered=["message-1"])
        self.assertEqual(outcomes, [], "the retried turn ran")
        self.assertEqual([call for call in calls if "failed" in call], [], "no receipt was failed before the retry")

    def test_a_turn_that_worked_before_the_fault_starts_a_fresh_retry_budget(self):
        outcomes = [self.transport_failure("opencode", "ECONNRESET") for _ in range(4)] + [self.finished("opencode")]
        result, notified = self.run_turns("opencode", outcomes,
                                          {"AGENT_TRANSPORT_RETRIES": "1", "AGENT_TRANSPORT_RESET_SEC": "0"})
        self.assertEqual((result, outcomes, notified), (0, [], []))

    def test_test_api_legacy_prompt_and_out_schema(self):
        body = {"base_url": "http://127.0.0.1:12345", "goal": "fixture", "overall": "pass", "endpoints": [],
                "summary": {"total": 0, "passed": 0, "failed": 0}}
        captured = []
        def run(store, name, prompt, opts):
            captured.append(prompt)
            store.update(name, state="done", dir=self.work.as_posix())
            atomic_write(store.path(name, ".log"), MARK + "\n```json\n" + json.dumps(body) + "\n```\n")
            return 0
        path = self.folder / "api.json"
        with patch.object(lifecycle, "run", side_effect=run), contextlib.redirect_stdout(io.StringIO()):
            code = cli.dispatch("test-api", dict(base_url=body["base_url"], goal="fixture", name="task",
                                                out=str(path), engine="codex"), [], self.store)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), body)
        self.assertIn('"overall"', captured[0])
        self.assertIn('"endpoints"', captured[0])


def prove_defects():
    base = Path(tempfile.gettempdir()) / "agents-core/review"
    base.mkdir(parents=True, exist_ok=True)
    defects = [
        ("diagnostics.py", "generated_at=generated, engines=results, raw=raw, deep_engines=deep_engines", "generated_at=generated, engines=results", "test_doctor_legacy_json_shape"),
        ("diagnostics.py", 'info["state"] = ("not_installed"', 'info["state"] = ("missing"', "test_doctor_states_and_reset_epochs"),
        ("diagnostics.py", 'row["resets_at"] = epoch(row.get("resets_at"))', 'row["resets_at"] = row.get("resets_at")', "test_doctor_states_and_reset_epochs"),
        ("diagnostics.py", 'os.environ["AGENT_CHAT_ONLY"] = "1"', 'os.environ["AGENT_CHAT_ONLY"] = "0"', "test_doctor_deep_captures_run_and_restores_policy"),
        ("lifecycle.py", '(meta.get("resolved_model") if key == "model" else "") or ', '', "test_legacy_model_resume_prefers_resolved_model"),
        ("cli.py", '"overall":"pass|fail|partial"', '"ok":true', "test_test_api_legacy_prompt_and_out_schema"),
        ("lifecycle.py", 'preflight(store, name, opts, False)\n    if not provider.supports_resume', 'preflight(store, name, opts, True)\n    if not provider.supports_resume', "test_restart_stops_before_resume_writer_preparation"),
        ("lifecycle.py", '"".join(output_filter.raw) if output_filter else answer', 'answer', "test_opencode_retry_reads_error_despite_partial_answer"),
        ("lifecycle.py", 'elif code and provider.retry_reason(retry_output, code, session)', 'elif code != 126 and code and provider.retry_reason(retry_output, code, session)', "test_codex_writer_conflict_retried_before_limited"),
        ("lifecycle.py", 'defaults = dict(engine="claude", model="", effort="", dir=os.getcwd())', 'defaults = dict()', "test_new_run_reused_name_uses_fresh_defaults"),
    ]
    records = []
    from neoxider_agents.providers import hidden_options
    with tempfile.TemporaryDirectory(prefix="mutations-", dir=base) as temporary:
        copy_root = Path(temporary)
        shutil.copytree(ROOT / "neoxider_agents", copy_root / "neoxider_agents")
        shutil.copytree(ROOT / "providers", copy_root / "providers")
        shutil.copy(ROOT / "activity.py", copy_root / "activity.py")
        (copy_root / "tests").mkdir()
        shutil.copy(__file__, copy_root / "tests/test_core_contracts.py")
        for filename, before, after, selected in defects:
            target = copy_root / "neoxider_agents" / filename
            original = target.read_text(encoding="utf-8")
            if before not in original:
                raise RuntimeError("mutation no longer matches: " + filename + " " + before)
            target.write_text(original.replace(before, after, 1), encoding="utf-8")
            result = subprocess.run([sys.executable, str(copy_root / "tests/test_core_contracts.py"), "Contracts." + selected],
                                    cwd=copy_root, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30, **hidden_options())
            target.write_text(original, encoding="utf-8")
            caught = result.returncode != 0 and b"FAILED" in result.stdout
            records.append(dict(file=filename, test=selected, caught=caught, output=result.stdout.decode("utf-8", "replace")[-1200:]))
            print("%s %s" % ("CAUGHT" if caught else "MISSED", selected))
    (base / "contracts-defects.json").write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if all(record["caught"] for record in records) else 1


if __name__ == "__main__":
    if "--prove-defects" in sys.argv:
        sys.exit(prove_defects())
    unittest.main()
