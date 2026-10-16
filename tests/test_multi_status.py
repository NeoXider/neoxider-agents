"""Several tasks at once: selection by names, globs and filters; one status table; glob stop/wait."""
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

ROOT = Path(os.environ.get("AGENT_UX_TEST_ROOT", Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(ROOT))
from neoxider_agents import cli, reporting, selection, ux_tracking as ux
from neoxider_agents.state import Store


class MultiStatusTests(unittest.TestCase):
    def setUp(self):
        base = Path("D:/Temp/agents-ux/multi/tests") if os.name == "nt" else Path(tempfile.gettempdir()) / "agents-ux/multi/tests"
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="multi-", dir=str(base))
        self.base = Path(self.temp.name)
        self.work = self.base / "work"
        self.work.mkdir()
        self.other = self.base / "other"
        self.other.mkdir()
        self.store = Store(self.base / "state")
        self.git = mock.patch.object(reporting, "_git_status", return_value=None)
        self.git.start()
        self.env = mock.patch.dict(os.environ, {"AGENT_CLI_LOGS": self.store.root.as_posix(),
                                               "AGENT_PARENT": "multi-tests", "AGENT_ORCHESTRATOR_ID": "multi-tests"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.git.stop()
        self.temp.cleanup()

    def seed(self, name, state="running", parent="multi-tests", work=None, age=30, **fields):
        directory = work or self.work
        self.store.update(name, state=state, exit=0, dir=directory.as_posix(), owns="", parent=parent,
                          pid=os.getpid(), started_epoch=time.time() - age, engine="fixture",
                          model=fields.pop("model", "vendor/fake-model:free-max"), **fields)
        reporting.begin_snapshot(self.store, name, directory)

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = cli.main(list(argv))
            except ValueError as error:
                return 1, out.getvalue(), str(error)
        return code, out.getvalue(), err.getvalue()

    def fleet(self):
        self.seed("pl-a", "running")
        self.seed("pl-b", "done")
        self.seed("pl-c", "error", reason="provider blocked")
        self.seed("qa-x", "stopped")
        self.seed("foreign-1", "running", parent="somebody-else")

    def test_names_and_globs_select_in_one_table(self):
        self.fleet()
        code, out, err = self.run_cli("status", "pl-a", "pl-b")
        self.assertEqual(code, 0, err)
        self.assertIn("pl-a", out)
        self.assertIn("pl-b", out)
        self.assertNotIn("pl-c", out)
        code, out, _ = self.run_cli("status", "pl-*")
        self.assertEqual([name for name in ("pl-a", "pl-b", "pl-c") if name in out], ["pl-a", "pl-b", "pl-c"])
        self.assertNotIn("qa-x", out)

    def test_single_plain_name_keeps_the_detail_view(self):
        self.fleet()
        _, out, _ = self.run_cli("status", "pl-a")
        self.assertIn("task=pl-a", out)
        self.assertIn("--- current step", out)

    def test_glob_ignores_other_orchestrators_but_exact_name_does_not(self):
        self.fleet()
        code, _, err = self.run_cli("status", "foreign-*")
        self.assertEqual(code, 1)
        self.assertIn("no task matches 'foreign-*'", err)
        _, out, _ = self.run_cli("status", "--state", "failed", "--since", "1s")
        self.assertEqual(out.strip(), "No tasks match.")
        _, out, _ = self.run_cli("status", "foreign-1", "pl-a")
        self.assertIn("foreign-1", out)

    def test_state_filters_and_groups(self):
        self.fleet()
        _, out, _ = self.run_cli("status", "--active")
        self.assertIn("pl-a", out)
        self.assertNotIn("pl-b", out)
        _, out, _ = self.run_cli("status", "--state", "failed")
        self.assertIn("pl-c", out)
        self.assertNotIn("pl-a", out)
        _, out, _ = self.run_cli("status", "--state", "done,stopped")
        self.assertEqual([name for name in ("pl-a", "pl-b", "pl-c", "qa-x") if name in out], ["pl-b", "qa-x"])

    def test_unknown_state_and_bad_duration_are_one_line_errors(self):
        self.fleet()
        code, _, err = self.run_cli("status", "--state", "sleeping")
        self.assertEqual(code, 1)
        self.assertIn("unknown state 'sleeping'", err)
        for value in ("90", "abc", "0m", "5x"):
            code, _, err = self.run_cli("status", "--since", value)
            self.assertEqual(code, 1, value)
            self.assertIn("--since must", err)

    def test_since_filters_by_start_time(self):
        self.seed("recent", "done", age=60)
        self.seed("old", "done", age=3 * 3600)
        _, out, _ = self.run_cli("status", "--since", "30m")
        self.assertIn("recent", out)
        self.assertNotIn("old", out)
        _, out, _ = self.run_cli("status", "--since", "1h30m")
        self.assertNotIn("old", out)
        _, out, _ = self.run_cli("status", "--since", "4h")
        self.assertIn("old", out)

    def test_here_keeps_only_tasks_under_the_current_directory(self):
        self.seed("inside", "done")
        self.seed("outside", "done", work=self.other)
        previous = os.getcwd()
        os.chdir(self.work)
        try:
            _, out, _ = self.run_cli("status", "--here")
        finally:
            os.chdir(previous)
        self.assertIn("inside", out)
        self.assertNotIn("outside", out)

    def test_unmatched_names_fail_and_unmatched_globs_warn(self):
        self.fleet()
        code, _, err = self.run_cli("status", "pl-a", "missing")
        self.assertEqual(code, 1)
        self.assertIn("no such task 'missing'", err)
        code, out, err = self.run_cli("status", "pl-*", "zz-*")
        self.assertEqual(code, 0, err)
        self.assertIn("pl-a", out)
        self.assertIn("no task matches 'zz-*'", err)
        code, _, err = self.run_cli("status", "zz-*")
        self.assertEqual(code, 1)
        self.assertIn("no task matches 'zz-*'", err)

    def test_footer_counts_states_attention_and_partial_baseline_once(self):
        self.fleet()
        baseline = reporting.read_baseline(self.store, "pl-a")
        baseline["partial"] = True
        self.store.update("pl-a", baseline_warning="partial baseline: 5 files not tracked")
        with mock.patch.object(ux, "read_baseline", return_value=baseline):
            _, out, _ = self.run_cli("status", "pl-*")
        self.assertIn("3 task(s):", out)
        self.assertIn("1 running", out)
        self.assertIn("needs attention: pl-c (error)", out)
        self.assertEqual(out.count("partial baseline"), 1)
        self.assertNotIn("[neoxider]", out)
        self.assertIn("*", out)

    def test_undelivered_messages_of_a_finished_task_need_attention(self):
        self.seed("done-with-mail", "done")
        self.store.enqueue_locked("done-with-mail", "message")
        _, out, _ = self.run_cli("status", "done-with-mail", "--json")
        rows = json.loads(out)
        self.assertEqual(ux.attention(rows), [("done-with-mail", "1 undelivered")])

    def test_strict_exit_code_follows_attention(self):
        self.fleet()
        self.assertEqual(self.run_cli("status", "pl-a", "pl-b", "--strict")[0], 0)
        self.assertEqual(self.run_cli("status", "pl-*", "--strict")[0], 3)

    def test_json_rows_match_top_and_sort_active_first(self):
        self.fleet()
        _, out, _ = self.run_cli("status", "pl-*", "--json")
        rows = json.loads(out)
        self.assertEqual(rows[0]["name"], "pl-a")
        self.assertEqual(sorted(row["name"] for row in rows), ["pl-a", "pl-b", "pl-c"])
        self.assertEqual(set(rows[0]), set(ux.dashboard_data(self.store, ["pl-a"])[0]))

    def test_detail_appends_the_full_status_blocks(self):
        self.fleet()
        _, out, _ = self.run_cli("status", "pl-a", "pl-b", "--detail")
        self.assertIn("task=pl-a", out)
        self.assertIn("task=pl-b", out)
        self.assertLess(out.index("TASK"), out.index("task=pl-a"))

    def test_top_accepts_patterns_and_filters_without_extra_arguments_error(self):
        self.fleet()
        code, out, err = self.run_cli("top", "--once", "pl-*", "--state", "done")
        self.assertEqual(code, 0, err)
        self.assertIn("pl-b", out)
        self.assertNotIn("pl-a", out)
        code, out, _ = self.run_cli("top", "--once", "--active")
        self.assertIn("pl-a", out)
        self.assertNotIn("pl-b", out)

    def test_top_filter_is_evaluated_on_every_refresh(self):
        self.fleet()
        out = io.StringIO()
        out.isatty = lambda: True
        calls = []

        def stop_second(_):
            calls.append(1)
            if len(calls) == 1:
                self.store.update("pl-b", state="running")
                return
            raise KeyboardInterrupt

        with contextlib.redirect_stdout(out), mock.patch.object(ux.Event, "wait", side_effect=stop_second):
            cli.main(["top", "--active"])
        frames = out.getvalue().split("\033[2J\033[H")
        self.assertNotIn("pl-b", frames[1])
        self.assertIn("pl-b", frames[2])

    def test_stop_and_wait_expand_globs_inside_the_owner_only(self):
        self.fleet()
        stopped = []
        with mock.patch("neoxider_agents.lifecycle.stop", side_effect=lambda store, name, by: stopped.append(name)):
            self.assertEqual(self.run_cli("stop", "pl-*", "qa-x")[0], 0)
        self.assertEqual(sorted(stopped), ["pl-a", "pl-b", "pl-c", "qa-x"])
        self.assertEqual(selection.expand(self.store, ["foreign-1", "pl-a"]), ["foreign-1", "pl-a"])
        with self.assertRaisesRegex(ValueError, "no task matches 'foreign-\\*'"):
            selection.expand(self.store, ["foreign-*"])
        waited = []
        with mock.patch("neoxider_agents.views.wait", side_effect=lambda store, names, *a, **k: waited.append(names) or 0):
            self.run_cli("wait", "pl-*")
        self.assertEqual(waited, [["pl-a", "pl-b", "pl-c"]])

    def test_activity_text_is_condensed(self):
        self.assertEqual(ux.brief_activity('end turn ended usage={"total":1,"input":2}'), "turn ended")
        self.assertEqual(ux.brief_activity('error {"name":"ContentFilterError","data":{"message":"blocked"}}'),
                         "error ContentFilterError: blocked")
        self.assertEqual(ux.brief_activity("[opencode] activity: tool running (3 helper processes)"),
                         "tool running (3 helper processes)")
        long = ux.brief_activity("x" * 200)
        self.assertEqual(len(long), 60)
        self.assertTrue(long.endswith("…"))
        self.assertEqual(ux.brief_activity(None), "")

    def test_counts_and_model_columns_are_compact(self):
        self.assertEqual((ux._count(None), ux._count(999), ux._count(15300), ux._count(2500000)), ("-", "999", "15.3k", "2.5M"))
        self.assertEqual(ux._short_model("openrouter/dots-studio/dots-3-note-preview:free"), "dots-3-note-preview:free")
        self.assertEqual(ux._short_model(""), "?")

    def test_selection_parsers(self):
        self.assertEqual(selection.parse_duration("1h30m", "x"), 5400)
        self.assertEqual(selection.parse_states("failed,done"), ("error", "limited", "silent", "stalled", "done"))
        self.assertTrue(selection.wants_selection(["a", "b"], {}))
        self.assertTrue(selection.wants_selection(["a*"], {}))
        self.assertTrue(selection.wants_selection([], {"--json": True}))
        self.assertFalse(selection.wants_selection(["a"], {}))
        self.assertFalse(selection.wants_selection([], {}))


if __name__ == "__main__":
    unittest.main()
