"""Canonical usage-limit reporting: LIMIT_HIT marker, resets parsing, wait --strict."""
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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from neoxider_agents import cli, lifecycle, reporting, views
from neoxider_agents import ux_tracking as ux
from neoxider_agents.providers import format_limit_hit, limit_line, parse_limit_reset
from neoxider_agents.state import Store

CODEX_REASON = "You've hit your usage limit. Please try again at Oct 14th, 2026 8:28 AM."


class LimitReportingTests(unittest.TestCase):
    def setUp(self):
        base = Path("D:/Temp/limit-report/tests") if os.name == "nt" else Path(tempfile.gettempdir()) / "limit-report/tests"
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="limits-", dir=str(base))
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.work = self.base / "work"
        self.work.mkdir()
        self.store = Store(self.base / "state")
        self.git = mock.patch.object(reporting, "_git_status", return_value=None)
        self.git.start()
        self.addCleanup(self.git.stop)
        environment = mock.patch.dict(os.environ, {"AGENT_CLI_LOGS": self.store.root.as_posix(),
                                                   "AGENT_PROVIDER_DIR": (ROOT / "tests/fixtures/providers").as_posix(),
                                                   "FIXTURE_SCRIPT": (ROOT / "tests/fixtures/core-provider.py").as_posix()})
        environment.start()
        self.addCleanup(environment.stop)

    def seed(self, name, state="limited", reason=CODEX_REASON, engine="codex", model="gpt-5", answer=""):
        self.store.update(name, state=state, exit=126 if state == "limited" else 0, reason=reason,
                          engine=engine, model=model, dir=self.work.as_posix(), started_epoch=time.time() - 30)
        self.store.path(name, ".answer").write_text(answer, encoding="utf-8")
        reporting.begin_snapshot(self.store, name, self.work)

    def output(self, call, *args, **kwargs):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = call(*args, **kwargs)
        return code, out.getvalue(), err.getvalue()

    def test_parse_each_pattern_and_unknown(self):
        cases = (
            ("You've hit your usage limit. Please try again at Oct 14th, 2026 8:28 AM.",
             "Oct 14th, 2026 8:28 AM"),
            ("Rate limit exceeded; quota resets in 3 hours.", "3 hours"),
            ("Limit reached, retry after 60s before continuing.", "60s"),
            ("Quota exhausted, available again tomorrow at 09:00.", "tomorrow at 09:00"),
            ("Not logged in; please run login.", "unknown"),
            ("", "unknown"),
        )
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(parse_limit_reset(text), expected)

    def test_limit_line_exact_format(self):
        self.assertEqual(limit_line("job", "codex", "gpt-5", CODEX_REASON),
                         'LIMIT_HIT task=job engine=codex model=gpt-5 resets="Oct 14th, 2026 8:28 AM"')
        self.assertEqual(limit_line("job", "codex", "gpt-5", "usage limit, nothing more"),
                         'LIMIT_HIT task=job engine=codex model=gpt-5 resets="unknown"')

    def test_limit_line_redacts_secrets(self):
        line = limit_line("job", "codex", "gpt-5",
                          "usage limit; try again at soon; api_key=sk-secret-value token=abc123")
        self.assertIn("LIMIT_HIT", line)
        self.assertNotIn("sk-secret-value", line)
        self.assertNotIn("abc123", line)

    def test_run_limited_ends_with_marker(self):
        reason = CODEX_REASON

        class FakeTurn:
            def __init__(self, *args, **kwargs):
                self.by, self.reason, self.last_activity = "watchdog", reason, ""
            def run(self):
                return 126, None, "failure", reason, "watchdog"

        with mock.patch.object(lifecycle, "Turn", FakeTurn):
            code, out, err = self.output(lifecycle.run, self.store, "lim-run", "do work",
                                         dict(engine="fixture", model="spark", effort="", dir=self.work.as_posix()))
        self.assertEqual(code, 126)
        self.assertEqual(self.store.read("lim-run")["state"], "limited")
        lines = [line for line in out.splitlines() if line.strip()]
        self.assertEqual(lines[-1], format_limit_hit("lim-run", "fixture", "spark", "Oct 14th, 2026 8:28 AM"))

    def test_last_limited_prints_stop_block_and_marker(self):
        self.seed("lim-last")
        code, out, err = self.output(views.last, self.store, "lim-last")
        self.assertEqual(code, 0)
        self.assertNotEqual(out.strip(), "")
        self.assertIn("STOPPED task=lim-last", out)
        self.assertEqual(out.splitlines()[-1], limit_line("lim-last", "codex", "gpt-5", CODEX_REASON))

    def test_result_text_and_json_for_limited(self):
        self.seed("lim-result")
        code, out, err = self.output(ux.result, self.store, "lim-result", {})
        self.assertEqual(code, 0)
        self.assertIn("STOPPED task=lim-result", out)
        self.assertEqual(out.splitlines()[-1], limit_line("lim-result", "codex", "gpt-5", CODEX_REASON))
        code, out, err = self.output(ux.result, self.store, "lim-result", {"--json": True})
        payload = json.loads(out)
        self.assertEqual(payload["limit"], {"hit": True, "resets": "Oct 14th, 2026 8:28 AM"})

    def test_result_json_has_no_limit_object_for_done(self):
        self.seed("ok-result", state="done", reason="", answer="finished")
        self.store.update("ok-result", exit=0)
        code, out, err = self.output(ux.result, self.store, "ok-result", {"--json": True})
        self.assertNotIn("limit", json.loads(out))

    def test_wait_summary_and_strict(self):
        self.seed("lim-wait")
        code, out, err = self.output(views.wait, self.store, ["lim-wait"], 0, 0.01)
        self.assertEqual(code, 0)
        self.assertIn("WAIT_DONE tasks=1 rc=0 ok=0 limited=1 failed=0", out)
        self.assertIn(limit_line("lim-wait", "codex", "gpt-5", CODEX_REASON), out)
        code, out, err = self.output(views.wait, self.store, ["lim-wait"], 0, 0.01, strict=True)
        self.assertEqual(code, 3)
        self.assertIn("WAIT_DONE tasks=1 rc=3 ok=0 limited=1 failed=0", out)

    def test_wait_strict_done_stays_zero(self):
        self.seed("ok-wait", state="done", reason="", answer="finished")
        self.store.update("ok-wait", exit=0)
        code, out, err = self.output(views.wait, self.store, ["ok-wait"], 0, 0.01, strict=True)
        self.assertEqual(code, 0)
        self.assertIn("WAIT_DONE tasks=1 rc=0 ok=1 limited=0 failed=0", out)

    def test_wait_parses_strict_flag(self):
        opts, args = cli.parse("wait", ["lim-wait", "--strict"])
        self.assertTrue(opts.get("--strict"))
        self.assertEqual(args, ["lim-wait"])
        self.assertIn("--strict", cli.COMMANDS["wait"])

    def test_status_and_list_show_resets(self):
        self.seed("lim-view")
        code, out, err = self.output(views.status, self.store, "lim-view")
        self.assertIn('resets="Oct 14th, 2026 8:28 AM"', out)
        code, out, err = self.output(views.task_list, self.store, 20)
        self.assertIn('resets="Oct 14th, 2026 8:28 AM"', out)


if __name__ == "__main__":
    unittest.main()
