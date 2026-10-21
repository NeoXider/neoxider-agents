"""revive: settled failed tasks resume in their SAVED session through the ordinary send path, behind safety gates."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(os.environ.get("AGENT_REVIVE_TEST_ROOT", Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(ROOT))
from neoxider_agents import cli, reporting, revive
from neoxider_agents.process import hidden_kwargs, pid_stamp
from neoxider_agents.state import Store
from neoxider_agents.ux_tracking import record_history

PARENT = "revive-tests"


class ReviveTests(unittest.TestCase):
    def setUp(self):
        base = Path("D:/Temp/agents-ux/revive/tests") if os.name == "nt" else Path(tempfile.gettempdir()) / "agents-ux/revive/tests"
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="revive-", dir=str(base))
        self.base = Path(self.temp.name)
        self.work = self.base / "work"
        self.work.mkdir()
        self.store = Store(self.base / "state")
        self.env = dict(os.environ, AGENT_CLI_LOGS=self.store.root.as_posix(),
                        AGENT_PROVIDER_DIR=(ROOT / "tests/fixtures/providers").as_posix(),
                        FIXTURE_SCRIPT=(ROOT / "tests/fixtures/core-provider.py").as_posix(),
                        AGENT_PARENT=PARENT, AGENT_ORCHESTRATOR_ID=PARENT, AGENT_KEEP_LOGS="0",
                        AGENT_RETRIES="0", AGENT_TIMEOUT_SEC="60", AGENT_SILENCE_SEC="0", PYTHONUTF8="1")
        self.patch = mock.patch.dict(os.environ, {key: self.env[key] for key in (
            "AGENT_CLI_LOGS", "AGENT_PROVIDER_DIR", "FIXTURE_SCRIPT", "AGENT_PARENT", "AGENT_ORCHESTRATOR_ID")})
        self.patch.start()
        self.git = mock.patch.object(reporting, "_git_status", return_value=None)
        self.git.start()

    def tearDown(self):
        self.git.stop()
        self.patch.stop()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and any(meta.get("state") == "running" for _, meta, _ in self.store.scan()):
            time.sleep(0.05)
        deadline = time.monotonic() + 5
        while True:
            try:
                self.temp.cleanup()
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.1)

    # helpers ---------------------------------------------------------------
    def cli(self, *argv):
        result = subprocess.run([sys.executable, str(ROOT / "agent.py"), *argv], cwd=str(ROOT), env=self.env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8", errors="replace",
                                timeout=60, **hidden_kwargs())
        return result.returncode, result.stdout, result.stderr

    def inproc(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def failed_task(self, name="crash"):
        """A REAL fixture task whose first turn fails (exit 42 -> state=error) with a saved session."""
        (self.work / "fail-turn-1").touch()
        code, out, err = self.cli("run", "-e", "fixture", "-t", name, "-C", self.work.as_posix(), "--no-progress", "--no-terse", "ASSIGNMENT")
        self.assertNotEqual(code, 0, out + err)
        meta = self.store.read(name)
        self.assertEqual(meta.get("state"), "error", meta)
        self.assertTrue(meta.get("session"), meta)
        self.age(name)
        return meta["session"]

    def age(self, name, seconds=3600):
        old = time.time() - seconds
        self.store.update(name, activity_epoch=old, last_activity_epoch=old, finished_epoch=old)

    def settle(self, name, seconds=30):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            meta = self.store.read(name)
            if meta.get("state") != "running":
                return meta
            time.sleep(0.05)
        self.fail("task %s did not settle" % name)

    def seed(self, name, state, **fields):
        values = dict(state=state, exit=fields.pop("exit", 1), dir=self.work.as_posix(), parent=PARENT, engine="fixture",
                      core_version="2", session=fields.pop("session", "ses_" + name), model="fixture")
        values.update(fields)
        self.store.update(name, **values)
        self.age(name)

    def json_revive(self, *argv):
        code, out, err = self.inproc("revive", "--json", *argv)
        return code, json.loads(out), err

    # integration -----------------------------------------------------------
    def test_error_task_with_stale_activity_is_revived_in_the_same_session(self):
        session = self.failed_task()
        (self.work / "fail-turn-1").unlink()
        code, out, err = self.cli("revive", "--json")
        self.assertEqual(code, 0, out + err)
        data = json.loads(out)
        self.assertEqual([row["name"] for row in data["resumed"]], ["crash"])
        row = data["resumed"][0]
        self.assertTrue(row["resumed"])
        self.assertEqual(row["state_before"], "error")
        self.assertIn(row["state_after"], ("running", "done"))
        self.assertEqual(row["session_id"], session)
        self.assertEqual(row["session_after"], session)
        self.assertIsNone(row["error"])
        self.assertEqual(data["summary"]["resumed"], 1)
        meta = self.settle("crash")
        self.assertEqual(meta.get("state"), "done", meta)
        # The provider itself received the SAME session on the resumed turn.
        self.assertEqual((self.work / "turn.1.session").read_text(), session)
        self.assertEqual((self.work / "turn.2.session").read_text(), session)
        self.assertEqual(self.store.session("crash"), session)
        self.assertIn("[neoxider revive]", (self.work / "turn.2.prompt").read_text(encoding="utf-8"))
        kinds = [entry["kind"] for entry in revive.history(self.store, "crash")]
        self.assertEqual(kinds, ["run", "revive", "send"])

    def test_second_revive_within_cooldown_does_not_resume_twice(self):
        self.failed_task()
        (self.work / "fail-turn-2").touch()
        code, out, err = self.cli("revive", "--json")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(json.loads(out)["summary"]["resumed"], 1)
        meta = self.settle("crash")
        self.assertEqual(meta.get("state"), "error")
        self.age("crash")
        code, data, _ = self.json_revive()
        self.assertEqual(code, 0)
        self.assertEqual(data["resumed"], [])
        self.assertIn("--cooldown", data["skipped"][0]["action"])
        self.assertFalse((self.work / "turn.3.started").exists())
        self.assertEqual(self.store.read("crash").get("revive_count"), "1")

    def test_dry_run_changes_nothing(self):
        self.failed_task()
        before = {suffix: self.store.path("crash", suffix).read_bytes() for suffix in (".meta", ".history.jsonl")}
        code, out, err = self.cli("revive", "--dry-run")
        self.assertEqual(code, 0, out + err)
        self.assertIn("would revive", out)
        self.assertIn("dry run, nothing changed", out)
        code, data, _ = self.json_revive("--dry-run")
        self.assertEqual([row["name"] for row in data["candidates"]], ["crash"])
        self.assertEqual(data["resumed"], [])
        time.sleep(0.3)
        self.assertEqual(before, {suffix: self.store.path("crash", suffix).read_bytes() for suffix in (".meta", ".history.jsonl")})
        self.assertFalse((self.work / "turn.2.started").exists())
        self.assertFalse(self.store.path("crash", ".revive.prompt").exists())

    # gates -----------------------------------------------------------------
    def test_running_and_idle_tasks_are_never_revived(self):
        stamp = pid_stamp(os.getpid())
        self.seed("live", "running", pid=os.getpid(), winpid=os.getpid() if os.name == "nt" else "", pid_start=stamp, exit="")
        self.store.update("live", activity_epoch=time.time())
        self.seed("quiet", "running", pid=os.getpid(), winpid=os.getpid() if os.name == "nt" else "", pid_start=stamp, exit="")
        code, data, _ = self.json_revive()
        self.assertEqual(code, 0)
        self.assertEqual(data["resumed"] + data["candidates"] + data["skipped"], [])
        code, data, _ = self.json_revive("live", "quiet", "--force", "--stale", "0")
        actions = {row["name"]: row["action"] for row in data["skipped"]}
        self.assertEqual(actions, {"live": "skip: running is never revived", "quiet": "skip: idle is never revived"})
        self.assertEqual(data["resumed"], [])
        self.assertNotIn("revive_epoch", self.store.read("live"))

    def test_done_and_waiting_tasks_are_never_revived(self):
        self.seed("ok", "done", exit=0)
        self.seed("asks", "waiting", exit=0)
        code, data, _ = self.json_revive()
        self.assertEqual(data["skipped"] + data["resumed"] + data["candidates"], [])
        code, data, _ = self.json_revive("ok", "asks", "--force")
        self.assertEqual(sorted(row["action"] for row in data["skipped"]),
                         ["skip: done is never revived", "skip: waiting is never revived"])
        with self.assertRaises(ValueError):
            revive.parse_options({"state": "error,done"})
        code, _, err = self.inproc("revive", "--state", "running")
        self.assertEqual(code, 1)
        self.assertIn("--state accepts only", err)

    def test_recent_activity_and_missing_session_are_skipped(self):
        self.seed("fresh", "error")
        self.store.update("fresh", activity_epoch=time.time(), finished_epoch=time.time())
        self.seed("nosession", "error", session="")
        _, data, _ = self.json_revive("--dry-run")
        actions = {row["name"]: row["action"] for row in data["skipped"]}
        self.assertIn("--stale 600s", actions["fresh"])
        self.assertIn("no saved session", actions["nosession"])
        _, data, _ = self.json_revive("--dry-run", "--stale", "0")
        self.assertEqual([row["name"] for row in data["candidates"]], ["fresh"])

    def test_deliberate_stop_is_not_revived_and_ambiguous_stop_needs_force(self):
        self.seed("halted", "stopped", exit=130)
        record_history(self.store, "halted", "run")
        reporting.record_stop(self.store, "halted", by="orchestrator", reason="stopped by orchestrator")
        self.seed("parentgone", "stopped", exit=130)
        record_history(self.store, "parentgone", "run")
        reporting.record_stop(self.store, "parentgone", by="user", reason="launcher stopped")
        self.seed("ownerdied", "stopped", exit="")
        record_history(self.store, "ownerdied", "run")
        for name in ("halted", "parentgone"):
            self.age(name)
        _, data, _ = self.json_revive("--dry-run")
        actions = {row["name"]: row["action"] for row in data["skipped"] + data["candidates"]}
        self.assertIn("deliberate stop", actions["halted"])
        self.assertIn("confirm with --force", actions["parentgone"])
        self.assertEqual(actions["ownerdied"], "would revive")
        _, data, _ = self.json_revive("--dry-run", "--force")
        actions = {row["name"]: row["action"] for row in data["skipped"] + data["candidates"]}
        self.assertIn("deliberate stop", actions["halted"], "a glob/filter --force never overrides a deliberate stop")
        self.assertEqual(actions["parentgone"], "would revive")
        _, data, _ = self.json_revive("halted", "--dry-run", "--force")
        self.assertEqual([row["name"] for row in data["candidates"]], ["halted"])

    def test_stop_intent_and_streak_rules(self):
        self.assertEqual(revive.stop_intent(self.store, "x", {}, [dict(kind="run")])[0], "crash")
        self.assertEqual(revive.stop_intent(self.store, "x", {}, [dict(kind="run"), dict(kind="stop", by="watchdog", reason="t")])[0], "crash")
        self.assertEqual(revive.stop_intent(self.store, "x", {}, [dict(kind="stop", by="gui", reason="r")])[0], "deliberate")
        self.assertEqual(revive.stop_intent(self.store, "x", {}, [])[0], "ambiguous")
        queued = [dict(kind="run"), dict(kind="stop", by="orchestrator"), dict(kind="send", queued=True)]
        self.assertEqual(revive.stop_intent(self.store, "x", {}, queued)[0], "deliberate")
        rows = [dict(kind="run"), dict(kind="revive"), dict(kind="send"), dict(kind="stop"), dict(kind="revive"), dict(kind="send")]
        self.assertEqual(revive.streak(rows), 2)
        self.assertEqual(revive.streak(rows + [dict(kind="send")]), 0, "a manual send resets the streak")
        self.assertEqual(revive.streak(rows + [dict(kind="run")]), 0)

    def test_max_attempts_and_limits_and_human_failures_gate_revive(self):
        self.seed("loop", "error")
        for kind in ("run", "revive", "send", "revive", "send", "revive", "send"):
            record_history(self.store, "loop", kind)
        self.seed("auth", "error", failure_json=json.dumps(dict(category="auth")))
        _, data, _ = self.json_revive("--dry-run")
        actions = {row["name"]: row["action"] for row in data["skipped"]}
        self.assertIn("--max-attempts 3", actions["loop"])
        self.assertIn("auth failure needs a human", actions["auth"])
        _, data, _ = self.json_revive("--dry-run", "--max-attempts", "0")
        self.assertIn("loop", [row["name"] for row in data["candidates"]])
        from neoxider_agents.providers import record_limit
        record_limit(self.store.root, "fixture", "fixture", "usage limit; try again at Dec 31st, 2099 11:00 AM")
        _, data, _ = self.json_revive("--dry-run", "--max-attempts", "0")
        actions = {row["name"]: row["action"] for row in data["skipped"]}
        self.assertIn("usage limit not reset", actions["loop"])

    def test_failed_launch_is_collected_and_the_sweep_continues(self):
        self.seed("ghost", "error", dir=(self.base / "missing-dir").as_posix())
        self.seed("ghost2", "error", dir=(self.base / "missing-dir").as_posix())
        code, data, _ = self.json_revive()
        self.assertEqual(code, 3, data)
        self.assertEqual(sorted(row["name"] for row in data["skipped"] if row["action"] == "failed"), ["ghost", "ghost2"])
        self.assertTrue(all(row["error"] for row in data["skipped"]))
        self.assertEqual(data["summary"]["failed"], 2)
        self.assertTrue(self.store.read("ghost").get("revive_epoch"), "a failed attempt still starts the cooldown")

    def test_old_tasks_need_a_name_bulk_sweeps_are_capped_and_scoped(self):
        self.seed("ancient", "error")
        self.age("ancient", 3 * 86400)
        for index in range(3):
            self.seed("bulk-%d" % index, "error")
        _, data, _ = self.json_revive("--dry-run", "--limit", "2")
        rows = data["candidates"] + data["skipped"]
        self.assertNotIn("ancient", [row["name"] for row in rows])
        self.assertEqual(data["summary"]["ignored_older"], 1)
        self.assertEqual(len(data["candidates"]), 2)
        self.assertEqual([row["action"] for row in data["skipped"]], ["skip: deferred by --limit 2"])
        _, data, _ = self.json_revive("ancient", "--dry-run")
        self.assertEqual([row["name"] for row in data["candidates"]], ["ancient"])
        _, data, _ = self.json_revive("--dry-run", "--within", "0", "--limit", "0")
        self.assertEqual(len(data["candidates"]), 4)
        with mock.patch.dict(os.environ, {"AGENT_PARENT": "", "AGENT_ORCHESTRATOR_ID": ""}):
            code, _, err = self.inproc("revive")
            self.assertEqual(code, 1)
            self.assertIn("no task scope", err)
            self.assertEqual(self.inproc("revive", "--dry-run")[0], 0)
        self.assertFalse(any(self.store.read(name).get("revive_epoch") for name in ("ancient", "bulk-0", "bulk-1", "bulk-2")))

    def test_table_and_watch_mode_stop_on_ctrl_c(self):
        self.seed("crashy", "error", dir=(self.base / "missing-dir").as_posix())
        code, out, _ = self.inproc("revive", "--dry-run")
        self.assertIn("TASK", out.splitlines()[0])
        self.assertIn("ses_crashy"[:12], out)
        with mock.patch.object(revive.time, "sleep", side_effect=KeyboardInterrupt):
            code, out, err = self.inproc("revive", "--watch", "--interval", "1")
        self.assertIn("revive --watch stopped", err)
        self.assertIn("crashy", out)
        code, _, err = self.inproc("revive", "--watch", "--dry-run")
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
