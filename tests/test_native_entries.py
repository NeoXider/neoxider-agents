"""Native entry contracts, with a restricted PATH and planted quote/encoding defects."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
POWERSHELL = shutil.which("powershell.exe")
PWSH = shutil.which("pwsh")


def hidden():
    if os.name != "nt":
        return {}
    info = subprocess.STARTUPINFO()
    info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    info.wShowWindow = subprocess.SW_HIDE
    return {"startupinfo": info, "creationflags": subprocess.CREATE_NO_WINDOW}


@unittest.skipUnless(os.name == "nt" and POWERSHELL, "Windows PowerShell required")
class NativeEntryTests(unittest.TestCase):
    def setUp(self):
        scratch = Path("D:/Temp/agents-core/entries")
        scratch.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=str(scratch))
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "bin").mkdir()
        for name in ("bin/neoxider.ps1", "bin/neoxider.cmd", "agent.ps1", "agent.cmd"):
            shutil.copyfile(ROOT / name, self.root / name)
        (self.root / "agent.py").write_text(
            "import json,sys\n"
            "args=json.loads(sys.stdin.read()) if sys.argv[1:]==['--argv-stdin'] else sys.argv[1:]\n"
            "print(json.dumps(args,ensure_ascii=False))\n"
            "sys.exit(37 if args==['not-a-command'] else 0)\n", encoding="utf-8")
        self.env = os.environ.copy()
        system = Path(os.environ["SystemRoot"])
        python_paths = [str(Path(sys.executable).parent)]
        for candidate in ("py", "python", "python3"):
            executable = shutil.which(candidate)
            if executable:
                python_paths.append(str(Path(executable).parent))
        self.env.update(PATH=os.pathsep.join(python_paths + [str(system / "System32")]),
                        AGENT_CLI_LOGS=(self.root / "state").as_posix(), PYTHONUTF8="1",
                        PYTHONIOENCODING="utf-8")
        self.assertNotIn("git", self.env["PATH"].lower())

    def invoke_ps(self, arguments, launcher="bin/neoxider.ps1", shell=None):
        quoted = ",".join("'" + item.replace("'", "''") + "'" for item in arguments)
        script = self.root / "invoke.ps1"
        with script.open("w", encoding="utf-8-sig", newline="") as stream:
            stream.write("$values = @(" + quoted + ")\n& '" +
                         str(self.root / launcher).replace("'", "''") +
                         "' @values\nexit $LASTEXITCODE\n")
        return subprocess.run([shell or POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass",
                               "-File", str(script)], env=self.env, capture_output=True,
                              encoding="utf-8", errors="replace", timeout=30, **hidden())

    def test_ps51_preserves_utf8_quotes_newlines_empty_arguments_without_bash(self):
        arguments = ["run", "-t", "fixture", "Привет, мир!\nстрока \"два\"", "", "C:/dir with spaces/"]
        result = self.invoke_ps(arguments)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), arguments)
        self.assertFalse((self.root / "state" / ".argv").exists(), "secrets must not be staged")

    def test_ps51_large_prompt_uses_stdin_instead_of_native_argv(self):
        arguments = ["run", "я" * 40000]
        result = self.invoke_ps(arguments)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), arguments)

    def test_ps51_bare_entry_and_root_shim(self):
        result = self.invoke_ps([], launcher="agent.ps1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [])

    def test_ps51_propagates_native_exit_code(self):
        result = self.invoke_ps(["not-a-command"])
        self.assertEqual(result.returncode, 37, result.stderr)

    @unittest.skipUnless(shutil.which("py"), "Python launcher not installed")
    def test_direct_python_parent_and_planted_persistent_py_defect(self):
        (self.root / "agent.py").write_text(
            "import json,os,sys\n"
            "sys.path.insert(0," + repr(str(ROOT)) + ")\n"
            "from neoxider_agents.windows import processes\n"
            "print(json.dumps(next(exe for pid,parent,exe in processes() if pid==os.getppid())))\n",
            encoding="utf-8")
        result = self.invoke_ps(["list"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout).lower(), "powershell.exe")
        cmd_result = subprocess.run([os.environ["COMSPEC"], "/d", "/c",
                                     str(self.root / "agent.cmd"), "list"],
                                    env=self.env, capture_output=True, encoding="utf-8",
                                    timeout=30, **hidden())
        self.assertEqual(cmd_result.returncode, 0, cmd_result.stderr)
        self.assertEqual(json.loads(cmd_result.stdout).lower(), "cmd.exe")
        launcher = self.root / "bin/neoxider.ps1"
        original = launcher.read_text(encoding="ascii")
        start = original.index("        if ($candidate -eq 'py') {")
        end = original.index("        break", start)
        launcher.write_text(original[:start] +
                            "        if ($candidate -eq 'py') { $pythonPrefix = @('-3') }\n" +
                            original[end:], encoding="ascii")
        mutant = self.invoke_ps(["list"])
        self.assertEqual(mutant.returncode, 0, mutant.stderr)
        self.assertEqual(json.loads(mutant.stdout).lower(), "py.exe")

    def test_missing_python_has_one_error_with_install_fix(self):
        self.env["PATH"] = str(self.root / "empty-path")
        result = self.invoke_ps(["list"])
        self.assertEqual(result.returncode, 127)
        self.assertEqual(result.stderr.count("Python 3.8+ is required"), 1)
        self.assertIn("python.org", result.stderr)

    def test_cmd_does_not_require_bash_and_propagates_exit(self):
        result = subprocess.run([os.environ["COMSPEC"], "/d", "/c",
                                 str(self.root / "agent.cmd"), "not-a-command"],
                                env=self.env, capture_output=True, encoding="utf-8",
                                timeout=30, **hidden())
        self.assertEqual(result.returncode, 37, result.stderr)
        self.assertEqual(json.loads(result.stdout), ["not-a-command"])

    def test_cmd_utf8_prompt_and_answer_without_bash(self):
        arguments = ["run", "Привет мир"]
        result = subprocess.run([os.environ["COMSPEC"], "/d", "/c",
                                 str(self.root / "agent.cmd"), *arguments],
                                env=self.env, capture_output=True, encoding="utf-8",
                                timeout=30, **hidden())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), arguments)

    @unittest.skipUnless(PWSH, "PowerShell 7 not installed")
    def test_ps7_same_native_contract(self):
        arguments = ["send", "fixture", "Привет \"мир\"\nследующая строка"]
        result = self.invoke_ps(arguments, shell=PWSH)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), arguments)

    def test_planted_ascii_stdin_defect_is_detected(self):
        launcher = self.root / "bin/neoxider.ps1"
        original = launcher.read_text(encoding="ascii")
        launcher.write_text(original.replace("$global:OutputEncoding = $utf8",
                                             "$global:OutputEncoding = [Text.Encoding]::ASCII"), encoding="ascii")
        result = self.invoke_ps(["run", "Привет"])
        self.assertNotEqual(json.loads(result.stdout), ["run", "Привет"])

    def test_planted_native_quote_rewriting_defect_is_detected(self):
        launcher = self.root / "bin/neoxider.ps1"
        original = launcher.read_text(encoding="ascii")
        launcher.write_text(original.replace(
            "$argumentJson | & $pythonCommand @pythonPrefix $entry --argv-stdin",
            "& $pythonCommand @pythonPrefix $entry @args"), encoding="ascii")
        result = self.invoke_ps(["run", 'embedded "quote"'])
        self.assertNotEqual(json.loads(result.stdout), ["run", 'embedded "quote"'])


@unittest.skipUnless(os.name == "nt" and POWERSHELL, "Windows PowerShell required")
class NativeCoreIntegrationTests(unittest.TestCase):
    def setUp(self):
        scratch = Path("D:/Temp/agents-core/entries")
        scratch.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="native-core-", dir=str(scratch))
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.work = self.base / "work"
        self.work.mkdir()
        self.logs = self.base / "state"
        self.env = dict(os.environ, AGENT_CLI_LOGS=self.logs.as_posix(),
                        AGENT_PROVIDER_DIR=(ROOT / "tests/fixtures/providers").as_posix(),
                        FIXTURE_SCRIPT=(ROOT / "tests/fixtures/core-provider.py").as_posix(),
                        AGENT_PARENT="entries-tests", AGENT_ORCHESTRATOR_ID="entries-tests",
                        AGENT_RETRIES="0", AGENT_TIMEOUT_SEC="30", AGENT_SILENCE_SEC="0")
        self.env["PATH"] = os.pathsep.join(part for part in self.env["PATH"].split(os.pathsep)
                                            if "git" not in part.lower() and "wsl" not in part.lower())
        self.jobs = []
        self.addCleanup(self.cleanup_jobs)

    def cleanup_jobs(self):
        for job in self.jobs:
            if job.poll() is None or self.meta().get("state") == "running":
                self.cli("stop", "native-core")
            try:
                job.communicate(timeout=8)
            except subprocess.TimeoutExpired:
                job.kill()
                job.communicate(timeout=5)

    def invocation(self, *arguments):
        quoted = ",".join("'" + item.replace("'", "''") + "'" for item in arguments)
        script = self.base / ("invoke-%s.ps1" % time.time_ns())
        with script.open("w", encoding="utf-8-sig", newline="") as stream:
            stream.write("$values = @(" + quoted + ")\n& '" +
                         str(ROOT / "bin/neoxider.ps1").replace("'", "''") +
                         "' @values\nexit $LASTEXITCODE\n")
        return [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)]

    def cli(self, *arguments):
        return subprocess.run(self.invocation(*arguments), env=self.env, capture_output=True,
                              encoding="utf-8", timeout=20, **hidden())

    def until(self, condition):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(0.05)
        self.fail("native core readiness deadline exceeded")

    def meta(self):
        path = self.logs / "native-core.meta"
        if not path.exists():
            return {}
        return dict(line.split("=", 1) for line in path.read_text(encoding="utf-8").splitlines()
                    if "=" in line)

    def launch(self):
        env = dict(self.env, FIXTURE_BLOCK_TURNS="1", FIXTURE_CHILD="1")
        job = subprocess.Popen(self.invocation("run", "-e", "fixture", "-t", "native-core",
                                             "-C", self.work.as_posix(), "--no-progress",
                                             "--no-terse", "BLOCK"), env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               encoding="utf-8", **hidden())
        self.jobs.append(job)
        self.until(lambda: (self.work / "turn.1.started").exists())
        return job

    def test_actual_core_cyrillic_prompt_is_exact_without_bash(self):
        prompt = 'Привет "мир"\nследующая строка'
        result = self.cli("run", "-e", "fixture", "-t", "native-core", "-C",
                          self.work.as_posix(), "--no-progress", "--no-terse", prompt)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.work / "turn.1.prompt").read_text(encoding="utf-8"), prompt)
        self.assertIn(prompt, result.stdout.replace("\r\n", "\n"))
        message = self.base / "message.txt"
        message.write_text('Ответ "два"', encoding="utf-8")
        reply = self.cli("reply", "native-core", "--no-progress", "--prompt-file", message.as_posix())
        self.assertEqual(reply.returncode, 0, reply.stdout + reply.stderr)
        self.assertIn('Ответ "два"', (self.work / "turn.2.prompt").read_text(encoding="utf-8"))
        self.assertEqual((self.work / "turn.1.session").read_text(),
                         (self.work / "turn.2.session").read_text())

    def test_stop_reaches_original_powershell_launcher(self):
        from neoxider_agents.process import pid_alive
        job = self.launch()
        initial = self.meta()
        child = int((self.work / "child.pid").read_text())
        stopped = self.cli("stop", "native-core")
        self.assertEqual(stopped.returncode, 0, stopped.stdout + stopped.stderr)
        output = job.communicate(timeout=10)[0]
        self.assertEqual(job.returncode, 130, output)
        self.assertIn("STOPPED task=native-core", output)
        self.assertEqual(self.meta().get("state"), "stopped")
        self.assertFalse(pid_alive(initial.get("provider_pid")))
        self.assertFalse(pid_alive(child))

    def test_killed_powershell_launcher_cannot_leave_provider_or_grandchild(self):
        from neoxider_agents.process import pid_alive
        job = self.launch()
        initial = self.meta()
        child = int((self.work / "child.pid").read_text())
        job.kill()  # Terminate only the PowerShell launcher, not its descendants.
        job.communicate(timeout=10)
        self.until(lambda: self.meta().get("state") == "stopped")
        self.assertEqual(self.meta().get("reason"), "launcher stopped")
        self.until(lambda: not pid_alive(initial.get("pid")))
        self.assertFalse(pid_alive(initial.get("provider_pid")))
        self.assertFalse(pid_alive(child))


if __name__ == "__main__":
    unittest.main()
