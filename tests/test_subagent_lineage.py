"""End-to-end subagent lineage: an agent that runs `agent.py run` (without -P) from inside its
own process creates a child whose meta parent= names it, at every depth, so the GUI tree shows
orchestrator -> subagent -> sub-subagent without the agent having to remember anything.

Run: python -m unittest tests.test_subagent_lineage
"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from neoxider_agents.state import Store
from neoxider_agents.process import hidden_kwargs


class NestedRunLineageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="lineage-")
        self.base = Path(self.temp.name)
        (self.base / "work").mkdir()
        self.store = Store(self.base / "logs")
        self.env = dict(os.environ, AGENT_CLI_LOGS=self.store.root.as_posix(),
                        AGENT_CONFIG=(self.base / "config.json").as_posix(),
                        AGENT_PROVIDER_DIR=(ROOT / "tests/fixtures/providers").as_posix(),
                        FIXTURE_SCRIPT=(ROOT / "tests/fixtures/nesting-provider.py").as_posix(),
                        NEST_AGENT=(ROOT / "agent.py").as_posix(), NEST_PREFIX="lvl", NEST_DEPTH="3",
                        AGENT_PARENT="lineage-tests", AGENT_ORCHESTRATOR_ID="lineage-tests",
                        AGENT_RETRIES="0", AGENT_TIMEOUT_SEC="120", AGENT_SILENCE_SEC="0")
        self.env.pop("AGENT_TASK", None)

    def tearDown(self):
        self.temp.cleanup()

    def test_nested_runs_record_their_launching_task_as_parent(self):
        result = subprocess.run([sys.executable, str(ROOT / "agent.py"), "run", "-e", "fixture", "-t", "orch",
                                 "-C", str(self.base / "work"), "top task"], cwd=str(ROOT), env=self.env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, encoding="utf-8",
                                errors="replace", timeout=240, **hidden_kwargs())
        self.assertEqual(result.returncode, 0, result.stdout)
        parent = {name: self.store.read(name).get("parent") for name in ("orch", "lvl-2", "lvl-1", "lvl-0")}
        self.assertEqual(parent, {"orch": "lineage-tests", "lvl-2": "orch", "lvl-1": "lvl-2", "lvl-0": "lvl-1"})
        for name in ("orch", "lvl-2", "lvl-1", "lvl-0"):
            self.assertEqual(self.store.read(name).get("state"), "done", name)


if __name__ == "__main__":
    unittest.main()
