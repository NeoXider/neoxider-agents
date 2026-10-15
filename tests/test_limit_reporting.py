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

    def test_legacy_provider_error_boundary_keeps_nonquota_categories_and_quiet_labels(self):
        cases = [("AGENT_PROVIDER_ERROR: " + CODEX_REASON, "quota"),
                 ("RangeError: Out of memory while checking rate limit", "provider_oom"),
                 ("TLS certificate verify failed while checking quota exceeded", "tls"),
                 ("HTTP 401 authentication failed while checking usage limit", "auth")]
        for reason, category in cases:
            with self.subTest(category=category):
                class LegacyTurn:
                    last_activity = "provider error"
                    def __init__(self, *args, **kwargs):
                        pass
                    def run(self):
                        return 126, None, "failure", reason, "watchdog"
                name = "legacy-" + category
                with mock.patch.object(lifecycle, "Turn", LegacyTurn):
                    code, out, err = self.output(lifecycle.run, self.store, name, "do work",
                        dict(engine="fixture", model="spark", effort="", dir=str(self.work), ask=True))
                state = "limited" if category == "quota" else "error"
                self.assertEqual(code, 126)
                self.assertEqual(out, "")
                self.assertIn("neoxider: task=%s %s;" % (name, state), err)
                self.assertEqual("LIMIT_HIT" in err, category == "quota")
                meta = self.store.read(name)
                self.assertEqual(meta["state"], state)
                failure = json.loads(meta["failure_json"])
                self.assertEqual(failure["category"], category)
                self.assertTrue(failure["blocking"])

    def test_native_exit_126_alone_is_not_a_quota_boundary(self):
        class NativeTurn:
            last_activity = ""
            def __init__(self, *args, **kwargs):
                pass
            def run(self):
                return 126, None, None, "", ""
        with mock.patch.object(lifecycle, "Turn", NativeTurn):
            code, out, err = self.output(lifecycle.run, self.store, "native-126", "do work",
                dict(engine="fixture", model="spark", effort="", dir=str(self.work)))
        self.assertEqual(code, 126)
        self.assertEqual(self.store.read("native-126")["state"], "error")
        self.assertNotIn("LIMIT_HIT", out + err)

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

    def test_parse_reset_epoch_only_for_absolute_times(self):
        from neoxider_agents.providers import parse_reset_epoch
        stamp = parse_reset_epoch("Oct 22nd, 2026 11:27 AM")
        self.assertEqual(time.strftime("%Y-%m-%d %H:%M", time.localtime(stamp)), "2026-10-22 11:27")
        self.assertEqual(time.strftime("%H:%M", time.localtime(parse_reset_epoch("Oct 14th, 2026 8:28 PM"))), "20:28")
        self.assertIsNone(parse_reset_epoch("3 hours"))
        self.assertIsNone(parse_reset_epoch("unknown"))

    def test_recorded_limit_expires_clears_and_survives_bad_file(self):
        from neoxider_agents.providers import clear_limit, known_limits, limits_path, record_limit
        root = self.store.root
        future = "try again at Oct 22nd, 2026 11:27 AM"
        record_limit(root, "codex", "gpt-6.1-sol-high", "You've hit your usage limit; " + future)
        now = time.mktime((2026, 10, 16, 3, 0, 0, 0, 0, -1))
        self.assertEqual(known_limits(root, now=now)["codex"]["resets"], "Oct 22nd, 2026 11:27 AM")
        self.assertEqual(known_limits(root, now=now + 7 * 86400), {})
        record_limit(root, "claude", "m", "rate limit, retry after 60s")
        self.assertIn("claude", known_limits(root, now=time.time()))  # relative reset: kept for 6 hours
        clear_limit(root, "codex")
        self.assertNotIn("codex", known_limits(root, now=now))
        limits_path(root).write_text("{not json", encoding="utf-8")
        self.assertEqual(known_limits(root), {})
        record_limit(root, "codex", "m", future)  # a corrupt file is replaced, not fatal
        self.assertIn("codex", known_limits(root, now=now))

    def test_provider_limit_seen_wins_over_a_later_stop(self):
        class Filter:
            provider_error = CODEX_REASON
        self.assertEqual(lifecycle.provider_limit_seen(None, Filter()), CODEX_REASON)
        Filter.provider_error = "Not logged in; please run login."
        self.assertEqual(lifecycle.provider_limit_seen(None, Filter()), "")
        self.assertEqual(lifecycle.provider_limit_seen(None, None), "")

    def test_stop_record_is_bounded_when_the_snapshot_is_slow(self):
        calls = []

        def slow(*args, **kwargs):
            calls.append(1)
            time.sleep(5)
            return [{"path": "late.txt"}]

        self.seed("slow", state="running")
        with mock.patch.object(reporting, "file_changes", side_effect=slow),                 mock.patch.dict(os.environ, {"AGENT_STOP_SNAPSHOT_SEC": "0.2"}):
            started = time.monotonic()
            reporting.record_stop(self.store, "slow", by="watchdog", reason=CODEX_REASON, code=126, state="limited")
            self.assertLess(time.monotonic() - started, 3)
        meta = self.store.read("slow")
        self.assertEqual(meta["state"], "limited")
        self.assertEqual(str(meta["delta_known"]), "0")
        self.assertIn("file delta not computed in time", meta["baseline_warning"])

    def test_status_and_list_show_resets(self):
        self.seed("lim-view")
        code, out, err = self.output(views.status, self.store, "lim-view")
        self.assertIn('resets="Oct 14th, 2026 8:28 AM"', out)
        code, out, err = self.output(views.task_list, self.store, 20)
        self.assertIn('resets="Oct 14th, 2026 8:28 AM"', out)


if __name__ == "__main__":
    unittest.main()
