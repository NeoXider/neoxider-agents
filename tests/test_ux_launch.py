"""Launch ergonomics contracts; deterministic providers, stdin transport and defect proofs."""
import concurrent.futures
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from neoxider_agents import brief, cli, completion, config, lifecycle, providers
from neoxider_agents.state import Store
from neoxider_agents.process import hidden_kwargs


class IsolatedLaunchTests(unittest.TestCase):
    def setUp(self):
        base = Path("D:/Temp/agents-ux/launch-tests") if os.name == "nt" else Path(tempfile.gettempdir()) / "agents-ux/launch-tests"
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=str(base))
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.work = self.base / "work"
        self.work.mkdir()
        self.config_file = self.base / "config.json"
        self.environment = dict(AGENT_CONFIG=self.config_file.as_posix(), AGENT_CLI_LOGS=(self.base / "state").as_posix(), AGENT_ENGINE="", AGENT_MODEL="", AGENT_EFFORT="", AGENT_PROGRESS="0", AGENT_KEEP_LOGS="0", AGENT_NOTIFY="0", AGENT_PROVIDER_DIR=(ROOT / "tests/fixtures/providers").as_posix(), AGENT_PARENT="ux-launch-tests")
        self.patcher = patch.dict(os.environ, self.environment)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.store = Store()


class LaunchTests(IsolatedLaunchTests):
    def mutant(self, module, old, new):
        source = Path(module.__file__).read_text(encoding="utf-8-sig")
        self.assertIn(old, source, "source mutation anchor disappeared")
        changed = types.ModuleType("neoxider_agents._ux_mutant_" + module.__name__.rsplit(".", 1)[-1])
        changed.__file__ = module.__file__
        changed.__package__ = "neoxider_agents"
        exec(compile(source.replace(old, new, 1), module.__file__, "exec"), changed.__dict__)
        return changed

    def main(self, *args, stdin=""):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), patch.object(sys, "stdin", io.StringIO(stdin)):
            code = cli.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def capture_launch(self, *args, stdin=""):
        with patch.object(lifecycle, "run", return_value=42) as run:
            code, out, err = self.main(*args, stdin=stdin)
        self.assertEqual(code, 42, err)
        return run.call_args.args

    def test_implicit_run_and_prompt_slug(self):
        _, name, prompt, opts = self.capture_launch("Fix the parser", "-e", "fixture")
        self.assertRegex(name, r"^fix-the-parser-[0-9a-f]{4}$")
        self.assertEqual(prompt, "Fix the parser")
        self.assertFalse(opts["ask"])
        self.assertEqual(opts["dir"], os.getcwd())
        self.assertFalse(list(self.store.root.glob("*.reserve")))

    def test_implicit_file_prompt_and_cyrillic(self):
        path = self.base / "prompt.txt"
        text = 'Привет "мир"\nследующая строка'
        path.write_text(text, encoding="utf-8-sig")
        _, name, prompt, _ = self.capture_launch("-p", path.as_posix(), "-e", "fixture")
        self.assertEqual(prompt, text)
        self.assertRegex(name, r"^privet-mir-sleduyushchaya-stroka-[0-9a-f]{4}$")

    def test_implicit_stdin_and_explicit_stdin(self):
        for args in (("-", "-e", "fixture"), ("run", "-e", "fixture", "-")):
            with self.subTest(args=args):
                self.assertEqual(self.capture_launch(*args, stdin="stdin text")[2], "stdin text")

    def test_ask_forwards_answer_only_contract_and_exit(self):
        _, name, prompt, opts = self.capture_launch("ask", "-e", "fixture", "-t", "question", "What changed?")
        self.assertEqual(name, "question")
        self.assertEqual(prompt, "What changed?")
        self.assertTrue(opts["ask"])

    def test_contract_flags_and_progress_alias(self):
        opts, args = cli.parse("run", ["--progress", "--no-progress", "-v", "--log", "--owns", "src/*,tests/*", "--strict-owns", "--notify", "prompt"])
        self.assertEqual(opts["owns"], "src/*,tests/*")
        for key in ("--progress", "--no-progress", "-v", "--log", "--strict-owns", "--notify"):
            self.assertTrue(opts[key])
        self.assertEqual(args, ["prompt"])

    def test_unambiguous_legacy_bare_progress_flag(self):
        for arguments in (["prompt", "-p"], ["-p", "-e", "fixture", "prompt"], ["-p", "--no-progress", "prompt"]):
            opts, args = cli.parse("run", arguments)
            self.assertTrue(opts["--progress"])
            self.assertEqual(args, ["prompt"])
        opts, args = cli.parse("run", ["-p", "contract.txt"])
        self.assertEqual(opts["prompt_file"], "contract.txt")

    def test_explicit_options_win_config_and_cross_engine_model_is_not_reused(self):
        self.config_file.write_text(json.dumps(dict(engine="codex", model="gpt-configured", effort="high")))
        options = config.launch_options({"engine": "opencode", "model": "free", "effort": "low", "dir": self.work.as_posix()})
        self.assertEqual(options, dict(engine="opencode", model="free", effort="low", dir=self.work.as_posix()))
        different = config.launch_options({"engine": "opencode"})
        self.assertNotIn("model", different)
        self.assertNotIn("effort", different)
        configured = config.launch_options({})
        self.assertEqual((configured["engine"], configured["model"], configured["effort"]), ("codex", "gpt-configured", "high"))

    def test_only_installed_engine_detection_never_executes_doctor(self):
        def detect(engine):
            if engine != "opencode":
                raise FileNotFoundError(engine)
            return ["opencode"]
        with patch.object(providers, "executable", side_effect=detect), patch.object(providers, "capture", side_effect=AssertionError("must not spawn doctor probes")):
            self.assertEqual(config.launch_options({})["engine"], "opencode")
        self.assertFalse(self.config_file.exists())

    def test_config_get_set_list_are_atomic_and_do_not_create_project_files(self):
        before = set(self.work.iterdir())
        self.assertEqual(self.main("config", "set", "engine", "fixture")[0], 0)
        self.assertEqual(self.main("config", "set", "model", "small")[0], 0)
        self.assertEqual(self.main("config", "get", "model")[1], "small\n")
        self.assertEqual(json.loads(self.main("config", "list")[1]), dict(engine="fixture", model="small"))
        self.assertEqual(set(self.work.iterdir()), before)
        self.assertFalse(list(self.base.glob("*.tmp.*")))

    def test_config_path_override_and_platform_location(self):
        self.assertEqual(config.config_path(), self.config_file)
        with patch.dict(os.environ, {"AGENT_CONFIG": "", "APPDATA": self.base.as_posix(), "XDG_CONFIG_HOME": self.base.as_posix()}):
            self.assertEqual(config.config_path(), self.base / "neoxider-agents/config.json")

    def test_missing_engine_has_actionable_error_without_config_write(self):
        with patch.object(providers, "executable", side_effect=FileNotFoundError("missing")):
            code, out, err = self.main("run", "question")
        self.assertEqual(code, 1)
        self.assertIn("install", err)
        self.assertEqual(len(err.splitlines()), 1)
        self.assertFalse(self.config_file.exists())

    def test_config_invalid_json_is_one_line(self):
        self.config_file.write_text("{\ninvalid")
        code, out, err = self.main("config", "list")
        self.assertEqual(code, 1)
        self.assertIn("fix its JSON", err)
        self.assertEqual(len(err.splitlines()), 1)
        self.assertNotIn("Traceback", err)

    def test_auto_names_survive_collisions_and_concurrent_launches(self):
        import uuid
        values = iter([uuid.UUID(hex="aaaa" + "0" * 28), uuid.UUID(hex="bbbb" + "0" * 28)])
        self.store.update("fix-aaaa", state="done")
        with patch.object(uuid, "uuid4", side_effect=lambda: next(values)):
            name, reservation = cli.task_name(self.store, "Fix")
        self.assertEqual(name, "fix-bbbb")
        reservation.unlink()
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(lambda _: cli.task_name(self.store, "Concurrent task"), range(80)))
        self.assertEqual(len(set(name for name, _ in results)), 80)
        for _, reservation in results:
            reservation.unlink()

    def test_brief_exact_skeleton_and_context(self):
        context = self.base / "context.txt"
        context.write_text("Existing contract", encoding="utf-8")
        code, text, err = self.main("brief", "--outcome", "Tests pass", "--owns", "src/a.py", "--not-touch", "src/b.py", "--return", "Evidence", "--context-file", context.as_posix())
        self.assertEqual(code, 0, err)
        self.assertEqual(text, "Outcome and acceptance check:\nTests pass\n\nOwned files:\nsrc/a.py\n\nConstraints:\nDo not touch: src/b.py\nDo not commit, publish or expand scope.\n\nReturn:\nEvidence\n\nContext:\nExisting contract\n")

    def test_brief_pipes_to_run_unchanged(self):
        code, text, err = self.main("brief", "--outcome", "Fix parser")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.capture_launch("run", "-e", "fixture", "-t", "contract", "-", stdin=text)[2], text)

    def test_completion_install_idempotent_and_preserves_profile(self):
        for shell in ("powershell", "bash", "zsh"):
            with self.subTest(shell=shell):
                profile = self.base / ("profile." + shell)
                profile.write_text("# user settings\n", encoding="utf-8")
                for _ in range(2):
                    code, text, err = self.main("completion", shell, "--install", "--profile", profile.as_posix())
                    self.assertEqual(code, 0, err)
                content = profile.read_text(encoding="utf-8-sig")
                self.assertEqual(content.count(completion.START), 1)
                self.assertIn("# user settings", content)
                self.assertTrue((self.base / ("completion." + completion.SUFFIXES[shell])).exists())

    def test_completion_scripts_have_new_commands_flags_engines_models_and_tasks(self):
        for shell in completion.SUFFIXES:
            script = self.main("completion", shell)[1]
            for token in ("ask", "brief", "top", "watch", "result", "--progress", "--owns", "--notify", "gpt-6.1-sol", "opencode", ".meta"):
                self.assertIn(token, script, shell + " missing " + token)

    @unittest.skipUnless(os.name == "nt" and shutil.which("powershell"), "Windows PowerShell required")
    def test_powershell_completion_execution_and_source_planted_model_defect(self):
        self.store.update("task-active", state="done")
        scenarios = ("neoxider ", "neoxider run --pro", "neoxider run -e co", "neoxider run -e codex -m gpt-", "neoxider stop task-", "neoxider config set ")
        def expand(path):
            invoker = self.base / "complete.ps1"
            expressions = ",".join("'" + item.replace("'", "''") + "'" for item in scenarios)
            invoker.write_text(". '" + path.as_posix().replace("'", "''") + "'\n$results = @{}\nforeach ($value in @(" + expressions + ")) { $results[$value] = @( (TabExpansion2 -InputScript $value -CursorColumn $value.Length).CompletionMatches | ForEach-Object { $_.CompletionText } ) }\n$results | ConvertTo-Json -Depth 4\n", encoding="utf-8-sig")
            result = subprocess.run([shutil.which("powershell"), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(invoker)], env=dict(os.environ), capture_output=True, encoding="utf-8", timeout=15, **hidden_kwargs(executable=shutil.which("powershell")))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return json.loads(result.stdout)
        actual = expand(ROOT / "completions/neoxider.ps1")
        for scenario, expected in zip(scenarios, ("ask", "--progress", "codex", "gpt-6.1-sol", "task-active", "engine")):
            self.assertIn(expected, actual[scenario], scenario)
        altered = self.base / "broken-completion.ps1"
        altered.write_text(completion.script("powershell").replace("'gpt-6.1-sol'", "'REMOVED_MODEL'"), encoding="ascii")
        self.assertNotIn("gpt-6.1-sol", expand(altered)[scenarios[3]])

    def test_errors_suggest_fixes_and_debug_controls_traceback(self):
        code, text, err = self.main("run", "--progres", "prompt")
        self.assertEqual(code, 1)
        self.assertIn("did you mean '--progress'", err)
        self.assertEqual(len(err.splitlines()), 1)
        self.assertNotIn("Traceback", err)
        self.assertIn("did you mean 'status'", self.main("stauts")[2])
        self.assertIn("Traceback", self.main("--debug", "run", "--progres", "prompt")[2])

    def test_missing_prompt_file_and_empty_stdin_have_fixes(self):
        for arguments in (("run", "-p", "missing.txt"), ("run", "-"), ("run",)):
            code, text, err = self.main(*arguments)
            self.assertEqual(code, 1)
            self.assertEqual(len(err.splitlines()), 1)
            self.assertNotIn("Traceback", err)

    def test_literal_option_like_prompt_and_debug_word_are_preserved(self):
        _, _, text, _ = self.capture_launch("run", "-e", "fixture", "--", "--debug")
        self.assertEqual(text, "--debug")

    def test_new_commands_help_starts_no_task(self):
        for command in ("run", "ask", "brief", "config", "completion", "top", "dashboard", "diff", "result", "watch"):
            code, text, err = self.main(command, "--help")
            self.assertEqual(code, 0, err)
            self.assertIn("Usage: neoxider " + command, text)
        self.assertFalse(list(self.store.root.glob("*.meta")))

    def test_thirteen_source_planted_defects_are_detected(self):
        # Compile altered production source in separate modules, never editing the checkout.
        defects = []
        broken = self.mutant(cli, 'else "run"\n    if command', 'else "help"\n    if command')
        def check_implicit(module):
            with patch.object(lifecycle, "run", return_value=0) as run, contextlib.redirect_stdout(io.StringIO()):
                module.main(["Fix parser", "-e", "fixture"])
            self.assertTrue(run.called)
        defects.append(("implicit-run", cli, broken, check_implicit))
        broken = self.mutant(cli, 'opts["ask"] = command == "ask"', 'opts["ask"] = False')
        def check_ask(module):
            with patch.object(lifecycle, "run", return_value=0) as run:
                module.main(["ask", "-e", "fixture", "question"])
            self.assertTrue(run.call_args.args[3]["ask"])
        defects.append(("ask-contract", cli, broken, check_ask))
        broken = self.mutant(cli, 'if text == "-":', 'if text == "disabled-stdin":')
        def check_stdin(module):
            with patch.object(sys, "stdin", io.StringIO("exact")):
                self.assertEqual(module.text_prompt({}, ["-"]), "exact")
        defects.append(("stdin", cli, broken, check_stdin))
        broken = self.mutant(cli, '"-p": "prompt_file"', '"-p": "broken"')
        defects.append(("prompt-file", cli, broken, lambda module: self.assertEqual(module.parse("run", ["-p", "prompt.txt"])[0].get("prompt_file"), "prompt.txt")))
        broken = self.mutant(cli, 'if argument == "-p" and value.startswith("-"):', 'if False:')
        def check_bare_progress(module):
            try:
                options, _ = module.parse("run", ["-p", "-e", "fixture", "prompt"])
            except ValueError:
                self.fail("bare legacy -p must parse as progress")
            self.assertTrue(options.get("--progress"))
        defects.append(("legacy-progress-flag", cli, broken, check_bare_progress))
        broken = self.mutant(cli, 'uuid.uuid4().hex[:4]', 'uuid.uuid4().hex[:8]')
        def check_name(module):
            name, reserved = module.task_name(self.store, "Fix parser")
            reserved.unlink()
            self.assertRegex(name, r"^fix-parser-[0-9a-f]{4}$")
        defects.append(("task-name", cli, broken, check_name))
        broken = self.mutant(config, 'return found[0]', 'return "claude"')
        def check_detect(module):
            def detect(engine):
                if engine != "opencode":
                    raise FileNotFoundError(engine)
                return [engine]
            with patch.object(providers, "executable", side_effect=detect):
                self.assertEqual(module.installed_engine(), "opencode")
        defects.append(("engine-detection", config, broken, check_detect))
        broken = self.mutant(config, 'return Path(os.environ["AGENT_CONFIG"]).expanduser()', 'return Path("wrong-config.json")')
        defects.append(("config-location", config, broken, lambda module: self.assertEqual(module.config_path(), self.config_file)))
        broken = self.mutant(config, 'use_defaults = not configured_engine or configured_engine == engine', 'use_defaults = True')
        def check_precedence(module):
            self.config_file.write_text(json.dumps(dict(engine="codex", model="wrong-for-opencode")))
            try:
                self.assertNotIn("model", module.launch_options(dict(engine="opencode")))
            finally:
                self.config_file.unlink()
        defects.append(("config-precedence", config, broken, check_precedence))
        broken = self.mutant(brief, '"Owned files:\\n"', '"Removed ownership:\\n"')
        defects.append(("brief-contract", brief, broken, lambda module: self.assertIn("Owned files:", module.build(dict(outcome="done")))))
        broken = self.mutant(completion, 'if START in text and END in text[text.index(START):]:', 'if False:')
        def check_install(module):
            rc = self.base / "mutation-profile"
            rc.write_text("# user\n")
            with contextlib.redirect_stdout(io.StringIO()):
                module.install("bash", rc)
                module.install("bash", rc)
            self.assertEqual(rc.read_text().count(completion.START), 1)
        defects.append(("completion-idempotence", completion, broken, check_install))
        broken = self.mutant(cli, 'n=1, cutoff=0.65', 'n=1, cutoff=1.0')
        def check_errors(module):
            error = io.StringIO()
            with contextlib.redirect_stderr(error):
                module.main(["run", "--progres", "question"])
            self.assertIn("did you mean '--progress'", error.getvalue())
        defects.append(("error-suggestion", cli, broken, check_errors))
        broken = self.mutant(cli, 'if debug:\n            import traceback', 'if False:\n            import traceback')
        def check_debug(module):
            error = io.StringIO()
            with contextlib.redirect_stderr(error):
                module.main(["--debug", "run", "--progres", "question"])
            self.assertIn("Traceback", error.getvalue())
        defects.append(("debug-errors", cli, broken, check_debug))
        for label, original, mutant, check in defects:
            with self.subTest(defect=label):
                check(original)
                with self.assertRaises(AssertionError):
                    check(mutant)


class FakeLaunchIntegrationTests(IsolatedLaunchTests):
    def setUp(self):
        super().setUp()
        self.provider_dir = self.base / "providers/fixture"
        self.provider_dir.mkdir(parents=True)
        (self.provider_dir / "provider.py").write_text("import os,sys\nfrom neoxider_agents.providers import BaseProvider\nclass Provider(BaseProvider):\n def command(self,model,effort,cwd,session='',chat_only=False):\n  return [sys.executable,os.environ['UX_FAKE_SCRIPT']],{}\n", encoding="utf-8")
        self.script = self.base / "fake.py"
        self.script.write_text("import json,os,re,sys\nfrom pathlib import Path\nprompt=sys.stdin.buffer.read().decode('utf-8')\nPath(os.environ['UX_CAPTURE']).write_text(json.dumps({'prompt':prompt,'argv':sys.argv[1:]},ensure_ascii=False),encoding='utf-8')\nif '[Progress protocol]' in prompt:\n match=re.search(r'named EXACTLY (PROGRESS\\.[^ ]+\\.md)',prompt)\n Path(match.group(1)).write_text('progress',encoding='utf-8')\nprint('session id: ses_ux_fake')\nprint(os.environ.get('UX_ANSWER','ANSWER'))\nsys.exit(int(os.environ.get('UX_EXIT','0')))\n", encoding="utf-8")
        self.env = dict(os.environ, AGENT_PROVIDER_DIR=self.provider_dir.parent.as_posix(), UX_FAKE_SCRIPT=self.script.as_posix(), UX_CAPTURE=(self.base / "capture.json").as_posix(), AGENT_TIMEOUT_SEC="15", AGENT_SILENCE_SEC="0", AGENT_RETRIES="0", PYTHONUTF8="1", PYTHONIOENCODING="utf-8")

    def invoke(self, *args, stdin=None, env=None):
        return subprocess.run([sys.executable, str(ROOT / "agent.py"), *args], cwd=str(self.work), env=env or self.env, input=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8", timeout=20, **hidden_kwargs())

    def test_fake_ask_stdout_and_result_exit_code(self):
        result = self.invoke("ask", "-e", "fixture", "-t", "answer", "--no-terse", "Question", env=dict(self.env, UX_EXIT="7"))
        self.assertEqual(result.returncode, 7, result.stdout + result.stderr)
        self.assertEqual(result.stdout, "ANSWER\n")

    def test_fake_default_run_creates_no_project_files_and_progress_opt_in(self):
        before = set(self.work.iterdir())
        result = self.invoke("run", "-e", "fixture", "-t", "no-progress", "--no-terse", "question")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(set(self.work.iterdir()), before)
        result = self.invoke("run", "-e", "fixture", "-t", "with-progress", "--progress", "--no-terse", "question")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(set(p.name for p in self.work.iterdir()), {"PROGRESS.with-progress.md"})

    def test_fake_provider_long_cyrillic_prompt_uses_only_stdin(self):
        text = 'Привет "мир"\n' + "я" * 40000
        result = self.invoke("run", "-e", "fixture", "-t", "long", "--no-terse", "-", stdin=text)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        capture = json.loads((self.base / "capture.json").read_text(encoding="utf-8"))
        self.assertEqual(capture, dict(prompt=text, argv=[]))

    @unittest.skipUnless(os.name == "nt" and shutil.which("powershell"), "Windows PowerShell required")
    def test_powershell_brief_pipeline_and_long_stdin_transport(self):
        prompt = self.base / "prompt.txt"
        text = 'Кириллица "точно"\n' + "я" * 40000
        prompt.write_bytes(text.encode("utf-8-sig"))
        command = "Get-Content -Raw -LiteralPath '%s' | & '%s' ask -e fixture -t ps-long --no-terse -; exit $LASTEXITCODE" % (prompt.as_posix(), (ROOT / "agent.ps1").as_posix())
        result = subprocess.run([shutil.which("powershell"), "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command], cwd=self.work, env=self.env, capture_output=True, encoding="utf-8", timeout=25, **hidden_kwargs(executable=shutil.which("powershell")))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout, "ANSWER\n")
        self.assertEqual(json.loads((self.base / "capture.json").read_text(encoding="utf-8"))["prompt"], text)
        command = "& '%s' brief --outcome 'Tests pass' --owns 'src/*' | & '%s' run -e fixture -t ps-brief --no-terse -; exit $LASTEXITCODE" % ((ROOT / "agent.ps1").as_posix(), (ROOT / "agent.ps1").as_posix())
        result = subprocess.run([shutil.which("powershell"), "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command], cwd=self.work, env=self.env, capture_output=True, encoding="utf-8", timeout=25, **hidden_kwargs(executable=shutil.which("powershell")))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        received = json.loads((self.base / "capture.json").read_text(encoding="utf-8"))["prompt"]
        self.assertIn("Outcome and acceptance check:\nTests pass", received)
        self.assertIn("Owned files:\nsrc/*", received)

    @unittest.skipUnless(os.name == "nt" and shutil.which("powershell"), "Windows PowerShell required")
    def test_source_planted_powershell_utf8_pipeline_defect_is_detected(self):
        local = self.base / "mutant-launcher"
        (local / "bin").mkdir(parents=True)
        for name in ("agent.py", "agent.ps1", "bin/neoxider.ps1"):
            shutil.copyfile(ROOT / name, local / name)
        launcher = local / "bin/neoxider.ps1"
        source = launcher.read_text(encoding="ascii")
        self.assertIn("$global:OutputEncoding = $utf8", source)
        launcher.write_text(source.replace("$global:OutputEncoding = $utf8", "$global:OutputEncoding = [Text.Encoding]::ASCII"), encoding="ascii")
        env = dict(self.env, PYTHONPATH=str(ROOT))
        text = "Кириллица survives"
        prompt = self.base / "mutant-prompt.txt"
        prompt.write_bytes(text.encode("utf-8-sig"))
        command = "Get-Content -Raw -LiteralPath '%s' | & '%s' run -e fixture -t mutant --no-terse -; exit $LASTEXITCODE" % (prompt.as_posix(), (local / "agent.ps1").as_posix())
        result = subprocess.run([shutil.which("powershell"), "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command], cwd=self.work, env=env, capture_output=True, encoding="utf-8", timeout=25, **hidden_kwargs(executable=shutil.which("powershell")))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        received = json.loads((self.base / "capture.json").read_text(encoding="utf-8"))["prompt"]
        self.assertNotEqual(received, text, "UTF8 acceptance must catch the planted ASCII transport defect")

    @unittest.skipUnless(os.name == "nt" and shutil.which("powershell"), "Windows PowerShell required")
    def test_powershell_usage_error_is_one_line_with_fix(self):
        result = subprocess.run([shutil.which("powershell"), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "bin/neoxider.ps1"), "run", "--progres", "prompt"], cwd=self.work, env=self.env, capture_output=True, encoding="utf-8", timeout=15, **hidden_kwargs(executable=shutil.which("powershell")))
        self.assertEqual(result.returncode, 1)
        self.assertEqual(len(result.stderr.splitlines()), 1, result.stderr)
        self.assertIn("did you mean '--progress'", result.stderr)


if __name__ == "__main__":
    unittest.main()
