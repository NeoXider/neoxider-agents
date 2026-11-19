import collections
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from neoxider_agents import disk_guard

Usage = collections.namedtuple("Usage", "total used free")
MB = 1024 * 1024


class DiskGuardTests(unittest.TestCase):
    def test_low_volume_is_reported_once_per_drive(self):
        with tempfile.TemporaryDirectory() as root:
            a, b = Path(root) / "a", Path(root) / "b"
            a.mkdir(); b.mkdir()
            with mock.patch("shutil.disk_usage", return_value=Usage(100 * MB, 91 * MB, 9 * MB)):
                low = disk_guard.low_volumes([a, b], minimum_mb=512)
        self.assertEqual(len(low), 1)
        self.assertEqual(low[0][1], 9)
        self.assertIn("9 MB free", disk_guard.describe(low))

    def test_enough_space_and_disabled_threshold_pass(self):
        with tempfile.TemporaryDirectory() as root:
            with mock.patch("shutil.disk_usage", return_value=Usage(100 * MB, 0, 2048 * MB)):
                self.assertEqual(disk_guard.low_volumes([root], minimum_mb=512), [])
            with mock.patch("shutil.disk_usage", return_value=Usage(100 * MB, 99 * MB, 1 * MB)):
                self.assertEqual(disk_guard.low_volumes([root], minimum_mb=0), [])

    def test_missing_path_probes_nearest_existing_parent(self):
        with tempfile.TemporaryDirectory() as root:
            missing = Path(root) / "not" / "yet"
            with mock.patch("shutil.disk_usage", return_value=Usage(100 * MB, 99 * MB, 1 * MB)) as usage:
                low = disk_guard.low_volumes([missing], minimum_mb=512)
            self.assertEqual(len(low), 1)
            self.assertTrue(Path(usage.call_args[0][0]).exists())

    def test_env_threshold(self):
        with mock.patch.dict(os.environ, {"AGENT_MIN_FREE_MB": "0"}):
            self.assertEqual(disk_guard.min_free_mb(), 0)
        with mock.patch.dict(os.environ, {"AGENT_MIN_FREE_MB": "bogus"}):
            self.assertEqual(disk_guard.min_free_mb(), disk_guard.DEFAULT_MIN_FREE_MB)

    def test_codex_home_respects_env(self):
        with mock.patch.dict(os.environ, {"CODEX_HOME": "X:/codexhome"}):
            self.assertEqual(disk_guard.provider_home("codex"), Path("X:/codexhome"))


if __name__ == "__main__":
    unittest.main()
