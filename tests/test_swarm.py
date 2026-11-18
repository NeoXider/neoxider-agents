"""Offline swarm acceptance using the native CLI and private state roots."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from neoxider_agents import config, swarm
from neoxider_agents.process import hidden_kwargs
from neoxider_agents.state import Store, atomic_write


def row(task, **overrides):
    record = dict(task=task, generation="generation-1", role="implementer", date="2026-11-18T00:00:00+00:00",
                  engine="codex", model="gpt-5.6-sol", effort="medium", state="done", exit_code=0,
                  duration_sec=8, verdict="pass", gates=dict(review="pass", verify="pass"),
                  changed_file_count=1, baseline_known=True, baseline_partial=False, watchdog_hit=False,
                  reported=dict(tokens=100, cost_usd=0.05), overrides=[], note="")
    record.update(overrides)
    return record


CHILD = """
import runpy, sys, time
from pathlib import Path
source, root, gate, index = sys.argv[1:]
test = runpy.run_path(source)
memory = test['swarm'].Memory(Path(root))
deadline = time.monotonic() + 15
while not Path(gate).exists():
    if time.monotonic() > deadline:
        raise RuntimeError('gate timed out')
    time.sleep(.01)
for count in range(8):
    memory.append(test['row']('process-%s-%s' % (index, count)), 5)
"""


class SwarmTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.store = Store(self.base / "state")
        self.memory = swarm.Memory(self.store.root)
        self.config_path = self.base / "config" / "custom.json"
        self.env = {key: value for key, value in os.environ.items() if not key.startswith("AGENT_")}
        self.env.update(AGENT_CLI_LOGS=str(self.store.root), AGENT_CONFIG=str(self.config_path),
                        AGENT_PROVIDER_DIR=str(ROOT / "providers"), PYTHONDONTWRITEBYTECODE="1")
        atomic_write(self.config_path, '{"engine": "codex", "model": "sol"}')

    def cli(self, *arguments, env=None, code=0):
        result = subprocess.run([sys.executable, "-B", str(ROOT / "agent.py"), *arguments], cwd=ROOT,
                                env=env or self.env, capture_output=True, encoding="utf-8", timeout=20,
                                **hidden_kwargs())
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return result.stdout if code == 0 else result.stderr

    def read_rows(self):
        return [json.loads(line) for line in self.memory.path.read_bytes().splitlines()]

    def test_record_result_round_trip_and_idempotent_projection_retry(self):
        self.store.update("build", state="done", exit=0, generation="g1", engine="codex", model="legacy-display",
                          resolved_model="gpt-5.6-sol", effort="medium", cost_usd=0.125, total_tokens=87,
                          task_started_epoch=1000, started_epoch=1000, finished_epoch=1012)
        atomic_write(self.store.path("build", ".baseline.json"), '{"partial": true}')
        atomic_write(self.store.path("build", ".changes.json"), '[{"path": "readme.md"}]')
        atomic_write(self.store.path("build", ".answer"), "worker claims the review passed")
        args = ("swarm", "record", "build", "--role", "implementer", "--verdict", "pass",
                "--gate", "review:pass", "--gate", "verify:n/a", "--override", "Used the approved route",
                "--override", "Ran checks locally", "--note", "Good for: сборка\nChecked independently")
        self.assertIn("Recorded build", self.cli(*args))
        [record] = self.read_rows()
        self.assertEqual((record["task"], record["generation"], record["model"], record["effort"]),
                         ("build", "g1", "gpt-5.6-sol", "medium"))
        self.assertEqual((record["state"], record["exit_code"], record["duration_sec"]), ("done", 0, 12))
        self.assertEqual((record["changed_file_count"], record["baseline_partial"]), (1, True))
        self.assertEqual(record["reported"], {"tokens": 87, "cost_usd": 0.125})
        self.assertEqual(record["gates"], {"review": "pass", "verify": "n/a"})
        self.assertEqual(record["overrides"], ["Used the approved route", "Ran checks locally"])
        self.assertIn("сборка\n", record["note"])
        self.assertNotIn("worker claims", self.memory.path.read_text(encoding="utf-8"))
        original = self.memory.path.read_bytes()
        markdown = self.memory.markdown.read_text(encoding="utf-8")
        self.assertTrue(markdown.startswith("# Swarm scoreboard"))
        self.assertIn("reported tokens 87, reported cost USD 0.125", markdown)
        self.memory.markdown.unlink()
        self.cli(*args)
        self.assertEqual(self.memory.path.read_bytes(), original)
        self.assertEqual(self.memory.markdown.read_text(encoding="utf-8"), markdown)
        self.assertIn("different evidence", self.cli(*args, "--note", "Changed attestation", code=1))

    def test_nonfinal_and_self_score_are_refused(self):
        args = ("swarm", "record", "worker", "--role", "implementer", "--verdict", "pass", "--gate", "review:n/a")
        self.store.update("worker", state="running", exit=0)
        self.assertIn("settled task", self.cli(*args, code=1))
        self.store.update("worker", state="done")
        self.assertIn("cannot record its own task", self.cli(*args, env=dict(self.env, AGENT_TASK="worker"), code=1))
        if os.name == "nt":
            self.assertIn("cannot record its own task",
                          self.cli(*args, env=dict(self.env, AGENT_TASK="WORKER"), code=1))
        self.assertFalse(self.memory.path.exists())

    def test_mixed_thread_and_process_writers_preserve_all_rows_and_markdown(self):
        gate = self.base / "go"
        ready = threading.Barrier(5, timeout=15)

        def thread_writer(index):
            ready.wait()
            for count in range(8):
                self.memory.append(row("thread-%s-%s" % (index, count)), 5)

        workers = []
        try:
            for index in range(4):
                workers.append(subprocess.Popen([sys.executable, "-B", "-c", CHILD, __file__, str(self.store.root),
                                                 str(gate), str(index)], cwd=ROOT, env=self.env, stdout=subprocess.PIPE,
                                                stderr=subprocess.STDOUT, **hidden_kwargs()))
            with ThreadPoolExecutor(max_workers=4) as pool:
                futures = [pool.submit(thread_writer, index) for index in range(4)]
                gate.touch()
                ready.wait()
                for future in futures:
                    future.result(timeout=35)
            for child in workers:
                output, _ = child.communicate(timeout=35)
                self.assertEqual(child.returncode, 0, output.decode("utf-8", "replace"))
        finally:
            for child in workers:
                if child.poll() is None:
                    child.kill()
                child.communicate(timeout=5)
        expected = {"%s-%s-%s" % (kind, worker, count) for kind in ("thread", "process")
                    for worker in range(4) for count in range(8)}
        rows = self.read_rows()
        self.assertEqual(len(rows), len(expected))
        self.assertEqual({item["task"] for item in rows}, expected)
        markdown_log = self.memory.markdown.read_text(encoding="utf-8").split("## Record log\n\n")[1]
        self.assertEqual([line.split("`")[1] for line in markdown_log.splitlines()], [item["task"] for item in rows])

    def test_score_threshold_hints_duration_and_two_failure_demotion(self):
        for n in range(5):
            self.memory.append(row("pass-%s" % n, duration_sec=n, note="Good for: isolated fixes"), 5)
        self.memory.append(row("other-effort", effort="high", duration_sec=None), 5)
        self.memory.append(row("other-role", role="reviewer"), 5)
        self.memory.append(row("first-fail", gates=dict(review="fail", verify="pass"), duration_sec=10,
                               note="Bad for: cross-module changes"), 5)
        before = json.loads(self.cli("swarm", "score", "--json"))
        self.assertEqual(before["trust_min_passes"], 5)
        [trusted] = before["trusted_pairs"]
        self.assertEqual(trusted["role"], "implementer")
        self.assertEqual((trusted["successes"], trusted["failures"], trusted["median_duration_sec"]), (5, 1, 2.5))
        self.assertEqual(trusted["good_for"], ["isolated fixes"])
        self.assertEqual(trusted["bad_for"], ["cross-module changes"])
        self.memory.append(row("second-fail", verdict="fail", state="silent", watchdog_hit=True,
                               duration_sec=14, note="Never use for: unsupervised migrations"), 5)
        score = json.loads(self.cli("swarm", "score", "--json", env=dict(self.env, AGENT_SWARM_TRUST_MIN_PASSES="3")))
        self.assertEqual(score["trust_min_passes"], 3)
        self.assertEqual(score["trusted_pairs"], [])
        medium = next(item for item in score["routes"] if item["role"] == "implementer" and item["effort"] == "medium")
        self.assertEqual((medium["successes"], medium["failures"], medium["watchdog_hits"]), (5, 2, 1))
        self.assertEqual(medium["never_use"], ["unsupervised migrations"])
        self.assertEqual([item["task"] for item in medium["failure_evidence"]], ["first-fail", "second-fail"])
        small = json.loads(self.cli("swarm", "score", "--json", env=dict(self.env, AGENT_SWARM_TRUST_MIN_PASSES="1")))
        self.assertEqual(len(small["trusted_pairs"]), 2)

    def test_torn_tail_is_withheld_and_repaired_on_append(self):
        self.memory.append(row("prior"), 5)
        committed = self.memory.path.read_bytes()
        with self.memory.path.open("ab") as stream:
            stream.write(b'{"unfinished":')
        self.assertEqual(self.memory.score(5)["routes"][0]["successes"], 1)
        self.memory.append(row("next"), 5)
        [backup] = self.memory.path.parent.glob("swarm_memory.jsonl.torn.*.bak")
        self.assertEqual(backup.read_bytes(), committed + b'{"unfinished":')
        self.assertTrue(self.memory.path.read_bytes().startswith(committed))
        self.assertEqual([item["task"] for item in self.read_rows()], ["prior", "next"])

    def test_map_uses_full_config_resolver_chain(self):
        appdata = self.base / "appdata"
        xdg = self.base / "xdg"
        env = {"AGENT_CONFIG": str(self.config_path), "APPDATA": str(appdata), "XDG_CONFIG_HOME": str(xdg)}
        with patch.object(config, "os", SimpleNamespace(name="nt", environ=env)), patch.object(Path, "home", return_value=self.base):
            self.assertEqual(swarm.map_path(), self.config_path.with_name("swarm.map"))
            del env["AGENT_CONFIG"]
            self.assertEqual(swarm.map_path(), appdata / "neoxider-agents/swarm.map")
            del env["APPDATA"]
            self.assertEqual(swarm.map_path(), xdg / "neoxider-agents/swarm.map")
            del env["XDG_CONFIG_HOME"]
            self.assertEqual(swarm.map_path(), self.base / ".config/neoxider-agents/swarm.map")
        with patch.object(config, "os", SimpleNamespace(name="posix", environ={"APPDATA": str(appdata)})), \
                patch.object(Path, "home", return_value=self.base):
            self.assertEqual(swarm.map_path(), self.base / ".config/neoxider-agents/swarm.map")

    def test_native_cli_map_and_plan_create_no_task_files(self):
        self.assertIn("advisory", self.cli("swarm", "--help"))
        self.assertIn("Usage: neoxider ultra", self.cli("ultra", "--help"))
        self.cli("swarm", "map", "set", "reviewer", "opencode/opencode/big-pickle/default,codex/spark/medium")
        choices = json.loads(self.cli("swarm", "map", "show"))["reviewer"]
        self.assertEqual(choices, ["opencode/opencode/big-pickle/default", "codex/spark/medium"])
        self.assertEqual(json.loads(self.config_path.with_name("swarm.map").read_text(encoding="utf-8"))["reviewer"], choices)
        empty_plan = json.loads(self.cli("swarm", "plan", "Isolate release checks", "--json"))
        self.assertEqual(json.loads(self.cli("ultra", "plan", "Isolate release checks", "--json")), empty_plan)
        self.assertTrue(empty_plan["advisory"])
        self.assertEqual(len(empty_plan["roles"]), 5)
        self.assertEqual(self.cli("ultra", "score", "--json"), self.cli("swarm", "score", "--json"))
        self.assertIn("use record, score, map, or plan", self.cli("ultra", "run", "workflow.json", code=1))
        self.assertFalse(list(self.store.root.iterdir()))

        self.memory.append(row("memory", role="reviewer", engine="opencode", model="opencode/big-pickle", effort="",
                               note="Good for: finding regressions"), 5)
        before = {path.name: path.read_bytes() for path in self.store.root.iterdir()}
        payload = json.loads(self.cli("swarm", "plan", "Isolate release checks", "--json"))
        planner, implementer, reviewer, verifier, skeptic = payload["roles"]
        self.assertEqual(implementer["count"], "N")
        self.assertEqual(reviewer["preferences"], choices)
        self.assertEqual(reviewer["route"]["model"], "opencode/big-pickle")
        self.assertEqual(reviewer["memory"]["good_for"], ["finding regressions"])
        self.assertEqual([item["route"]["model"] for item in (planner, verifier, skeptic)], ["gpt-5.6-sol"] * 3)
        self.assertEqual(payload["estimate"]["kind"], "estimate")
        plain = self.cli("swarm", "plan", "Isolate release checks", "--route", "kimi/kimi-code/k3")
        self.assertIn("implementer × N: kimi/kimi-code/k3/default [override]", plain)
        self.assertIn("15 minutes", plain)
        self.assertIn("paid agent", plain)
        self.assertIn("Cost estimate", plain)
        self.assertEqual({path.name: path.read_bytes() for path in self.store.root.iterdir()}, before)
        self.cli("swarm", "map", "unset", "reviewer")
        self.assertEqual(json.loads(self.cli("swarm", "map", "show")), {})


if __name__ == "__main__":
    unittest.main()
