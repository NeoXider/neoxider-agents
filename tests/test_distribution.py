#!/usr/bin/env python3
"""Release-metadata invariants that do not require external package managers."""

import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


class DistributionMetadataTests(unittest.TestCase):
    def test_plugin_and_marketplace_describe_every_bundled_provider(self):
        plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
        marketplace = json.loads(
            (ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8")
        )
        descriptions = " ".join(
            [plugin["description"], marketplace["description"]]
            + [entry["description"] for entry in marketplace["plugins"]]
        ).lower()
        for provider in ("codex", "claude", "kimi", "opencode", "gemini"):
            self.assertIn(provider, descriptions)
        self.assertRegex(plugin["version"], r"^\d+\.\d+\.\d+$")

    def test_skill_resolves_plugin_install_from_plugin_root(self):
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("CLAUDE_PLUGIN_ROOT", skill)
        self.assertIn("$CLAUDE_PLUGIN_ROOT/agent.sh", skill)

    def test_readme_does_not_publish_a_stale_exact_test_count(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"tests-\d+%20offline", readme))
        self.assertIsNone(re.search(r"^\d+ offline checks", readme, re.MULTILINE))

    def test_todo_contains_only_open_work(self):
        todo = (ROOT / "TODO.md").read_text(encoding="utf-8")
        self.assertNotIn("- [x]", todo.lower())


if __name__ == "__main__":
    unittest.main()
