#!/usr/bin/env python3
"""Behavioral checks for the cross-shell launchers and installer."""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def find_bash():
    found = shutil.which("bash")
    if found:
        return found
    for base in (
        os.environ.get("ProgramFiles"),
        os.environ.get("ProgramFiles(x86)"),
        os.environ.get("LOCALAPPDATA"),
    ):
        if not base:
            continue
        suffix = (
            Path("Programs/Git/bin/bash.exe")
            if base == os.environ.get("LOCALAPPDATA")
            else Path("Git/bin/bash.exe")
        )
        candidate = Path(base) / suffix
        if candidate.is_file():
            return str(candidate)
    return None


BASH = find_bash()


class WindowsLauncherTests(unittest.TestCase):
    def test_powershell_launcher_is_windows_powershell_safe_ascii(self):
        data = (ROOT / "bin" / "neoxider.ps1").read_bytes()
        data.decode("ascii")
        self.assertNotIn(b"Git\\usr\\bin\\bash.exe", data)

    @unittest.skipUnless(os.name == "nt" and shutil.which("cmd"), "requires cmd.exe")
    def test_cmd_launcher_propagates_failure(self):
        result = subprocess.run(
            ["cmd", "/d", "/c", str(ROOT / "bin" / "neoxider.cmd"), "not-a-command"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("unknown command", (result.stdout + result.stderr).lower())

    @unittest.skipUnless(os.name == "nt" and shutil.which("powershell"), "requires Windows PowerShell")
    def test_windows_powershell_launcher_parses_and_propagates_failure(self):
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(ROOT / "bin" / "neoxider.ps1"),
                "not-a-command",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("unknown command", (result.stdout + result.stderr).lower())

    @unittest.skipUnless(os.name == "nt" and shutil.which("pwsh"), "requires PowerShell 7")
    def test_pwsh_launcher_propagates_failure(self):
        result = subprocess.run(
            [
                "pwsh",
                "-NoProfile",
                "-File",
                str(ROOT / "bin" / "neoxider.ps1"),
                "not-a-command",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("unknown command", (result.stdout + result.stderr).lower())


class PosixInstallerTests(unittest.TestCase):
    @unittest.skipUnless(BASH, "requires bash")
    def test_installer_uses_configured_login_shell(self):
        with tempfile.TemporaryDirectory() as temp_home:
            env = os.environ.copy()
            env["HOME"] = temp_home
            env["SHELL"] = "/bin/zsh"
            result = subprocess.run(
                [BASH, str(ROOT / "bin" / "install.sh")],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue((Path(temp_home) / ".zshrc").is_file())
            self.assertFalse((Path(temp_home) / ".bashrc").exists())


if __name__ == "__main__":
    unittest.main()
