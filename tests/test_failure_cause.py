"""Failure causes: 124/125/126 and provider failures are explained in list, status, the table and doctor."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(os.environ.get("AGENT_CAUSE_TEST_ROOT", Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(ROOT))
from neoxider_agents import cli, diagnostics, reporting
from neoxider_agents.diagnostics import explain_failure
from neoxider_agents.state import MARK, Store

RETRY = '{"type":"system","subtype":"api_retry","attempt":%d,"max_retries":10,"error_status":529,"error":"overloaded_error","session_id":"s"}'
EXPLICIT = dict(source="provider_stream", confidence="explicit", blocking=True)


class ExplainTests(unittest.TestCase):
    def test_exit_codes_and_categories_map_to_operator_causes(self):
        self.assertEqual(explain_failure(dict(state="error", exit="124")), "task deadline (watchdog, AGENT_TIMEOUT_SEC)")
        self.assertEqual(explain_failure(dict(state="silent", exit="125")), "silence watchdog (AGENT_SILENCE_SEC)")
        self.assertEqual(explain_failure(dict(state="error", exit="126", reason="unable to get local issuer certificate")),
                         "provider: certificate error (VPN?)")
        self.assertEqual(explain_failure(dict(state="limited", exit="126", reason="You've hit your usage limit")),
                         "provider: failure/limit (usage limit)")
        self.assertEqual(explain_failure(dict(state="error", exit="126", reason="Reconnecting... 2/5 (stream disconnected before completion)")),
                         "provider: failure/limit (stream dropped)")
        self.assertEqual(explain_failure(dict(state="error", exit="126", reason="Cannot connect to API: Unable to connect")),
                         "provider: network error (VPN/proxy?)")
        self.assertEqual(explain_failure(dict(state="error", exit="126", reason="unknown")),
                         "provider: failure (unclassified; see neoxider log)")
        self.assertEqual(explain_failure(dict(state="stopped", exit="130", stopped_by="user", reason="launcher stopped")),
                         "stopped by user (launcher stopped)")
        self.assertEqual(explain_failure(dict(state="done", exit="0")), "")

    def test_claude_api_retry_events_explain_an_unknown_126(self):
        log = "\n".join(RETRY % attempt for attempt in range(1, 4))
        meta = dict(state="error", exit="126", reason="unknown", failure_json=json.dumps(dict(EXPLICIT, category="unknown", reason="unknown")))
        self.assertEqual(explain_failure(meta, log), "provider: failure/limit (overloaded, api_retry x3, status 529)")

    def test_heuristic_categories_and_assistant_text_are_not_causes(self):
        noise = json.dumps(dict(category="tls", reason='"node:tls" "node:tty" module list', source="raw", confidence="heuristic", blocking=False))
        self.assertEqual(explain_failure(dict(state="error", exit="3", reason="provider exited 3", failure_json=noise)), "exit 3")
        chatter = json.dumps(dict(type="assistant", message=dict(content=[dict(type="text", text="the TLS certificate expired")])))
        self.assertEqual(explain_failure(dict(state="error", exit="1", reason="provider exited 1"), chatter), "exit 1")


class CauseViewTests(unittest.TestCase):
    def setUp(self):
        base = Path("D:/Temp/agents-ux/cause/tests") if os.name == "nt" else Path(tempfile.gettempdir()) / "agents-ux/cause/tests"
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="cause-", dir=str(base))
        self.base = Path(self.temp.name)
        self.store = Store(self.base / "state")
        self.env = mock.patch.dict(os.environ, {"AGENT_CLI_LOGS": self.store.root.as_posix(), "AGENT_PARENT": "cause-tests",
                                                "AGENT_ORCHESTRATOR_ID": "cause-tests"})
        self.env.start()
        self.git = mock.patch.object(reporting, "_git_status", return_value=None)
        self.git.start()
        now = time.time()
        self.store.update("overload", state="error", exit=126, reason="unknown", engine="claude", model="opus", parent="cause-tests",
                          dir=self.base.as_posix(), core_version="2", session="ses_a", started_epoch=now - 60,
                          failure_json=json.dumps(dict(EXPLICIT, category="unknown", reason="unknown")))
        self.store.path("overload", ".log").write_text(
            "> PROMPT:\nplease discuss certificate rotation\n" + MARK + "\n" + "\n".join(RETRY % i for i in (1, 2)) + "\n", encoding="utf-8")
        self.store.update("slow", state="error", exit=124, reason="step watchdog timeout", engine="codex", model="sol",
                          parent="cause-tests", dir=self.base.as_posix(), core_version="2", session="ses_b", started_epoch=now - 30)

    def tearDown(self):
        self.git.stop()
        self.env.stop()
        self.temp.cleanup()

    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = cli.main(list(argv))
        return code, out.getvalue()

    def test_list_status_and_table_show_the_cause(self):
        _, out = self.run_cli("list")
        self.assertIn("cause=provider: failure/limit (overloaded, api_retry x2, status 529)", out)
        self.assertIn("cause=task deadline (watchdog, AGENT_TIMEOUT_SEC)", out)
        self.assertNotIn("certificate", out, "the echoed prompt above the output marker is never a cause")
        _, out = self.run_cli("status", "overload")
        self.assertIn("   cause=provider: failure/limit (overloaded", out)
        _, out = self.run_cli("status", "overload", "slow", "--detail")
        self.assertIn("needs attention: ", out)
        self.assertIn("overload (error: provider: failure/limit", out)
        self.assertIn("cause=task deadline", out)
        _, out = self.run_cli("status", "--json")
        causes = {row["name"]: row["cause"] for row in json.loads(out)}
        self.assertEqual(causes["slow"], "task deadline (watchdog, AGENT_TIMEOUT_SEC)")

    def test_doctor_lists_failed_tasks_and_revive_feasibility(self):
        text = diagnostics.failed_tasks(self.store)
        self.assertIn("overload", text)
        self.assertIn("provider: failure/limit", text)
        self.assertIn("neoxider revive --dry-run", text)
        now = time.time()
        claude = dict(engine="claude", state="ok", available=True, version="fixture",
                      limits=dict(windows=[dict(label="5h", used_percent=100, window_minutes=300, resets_at=now + 3600),
                                           dict(label="7d", used_percent=40, window_minutes=10080)]))
        raw = diagnostics.render([claude], [], False, now)
        self.assertIn("5h", raw)
        self.assertIn("revive    BLOCKED: 5h window exhausted (resets in 1h00m)", raw)
        self.assertIn("126 provider failure/limit", raw)
        claude["limits"]["windows"][0]["used_percent"] = 50
        self.assertIn("revive    possible: highest window 5h at 50%", diagnostics.render([claude], [], False, now))
        from neoxider_agents.providers import record_limit
        record_limit(None, "claude", "opus", "usage limit; try again at Dec 31st, 2099 11:00 AM")
        self.assertIn("revive    BLOCKED until the reset", diagnostics.render([claude], [], False, now))


if __name__ == "__main__":
    unittest.main()
