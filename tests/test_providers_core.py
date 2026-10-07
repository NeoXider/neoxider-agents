"""Provider contract/UTF-8/filter regressions; no provider network access."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from neoxider_agents.output import LIMIT, MARK, OutputFilter
from neoxider_agents.providers import CONFLICT, executable, failure_reason, get_provider, hidden_options, is_transient_failure, provider_info


class Aliases(unittest.TestCase):
    def test_codex_default(self):
        self.assertEqual(get_provider("codex").resolve(), ("gpt-5.6-terra", "medium"))

    def test_codex_aliases(self):
        provider = get_provider("codex")
        for alias, value in (("sol", "gpt-5.6-sol"), ("terra", "gpt-5.6-terra"), ("6-sol", "gpt-6-sol"),
                             ("sol6", "gpt-6-sol"), ("luna6", "gpt-6-luna"), ("spark", "gpt-5.3-codex-spark")):
            with self.subTest(alias=alias):
                self.assertEqual(provider.resolve(alias)[0], value)

    def test_codex_effort(self):
        self.assertEqual(get_provider("codex").resolve("high"), ("gpt-5.6-sol", "high"))
        self.assertEqual(get_provider("codex").resolve("high", "low"), ("gpt-5.6-sol", "low"))

    def test_codex_raw(self):
        self.assertEqual(get_provider("codex").resolve("gpt-6.1-sol")[0], "gpt-6.1-sol")

    def test_claude_default(self):
        self.assertEqual(get_provider("claude").resolve(), ("claude-opus-5", ""))

    def test_claude_current_aliases(self):
        provider = get_provider("claude")
        for alias, value in (("opus5", "claude-opus-5"), ("sonnet", "claude-sonnet-5"), ("opus55", "claude-opus-5-5"),
                             ("sonnet55", "claude-sonnet-5-5"), ("opus", "opus"), ("haiku", "haiku")):
            self.assertEqual(provider.resolve(alias)[0], value)

    def test_claude_effort_suffix(self):
        self.assertEqual(get_provider("claude").resolve("sonnet-low"), ("claude-sonnet-5", "low"))
        self.assertEqual(get_provider("claude").resolve("sonnet-low", "max"), ("claude-sonnet-5", "max"))
        self.assertEqual(get_provider("claude").resolve("sonnet"), ("claude-sonnet-5", "high"))

    def test_kimi_aliases(self):
        provider = get_provider("kimi")
        for alias, value in (("k3", "kimi-code/k3"), ("k3-256k", "kimi-code/k3-256k"), ("coding", "kimi-code/kimi-for-coding"),
                             ("highspeed", "kimi-code/kimi-for-coding-highspeed"), ("custom", "custom")):
            self.assertEqual(provider.resolve(alias, "high"), (value, ""))

    def test_opencode_aliases(self):
        provider = get_provider("opencode")
        for alias, value in (("free", "opencode/muse-spark-1.3-contributor-free"), ("ox", "opencode/x-preview-f-free"),
                             ("pickle", "opencode/big-pickle"), ("mimo", "opencode/mimo-v2.5-free"), ("new/free-model", "new/free-model")):
            self.assertEqual(provider.resolve(alias, "high"), (value, "high"))

    def test_gemini_raw(self):
        self.assertEqual(get_provider("gemini").resolve("model-id", "high"), ("model-id", ""))

    def test_provider_info_shape(self):
        self.assertEqual(set(provider_info()), {"codex", "claude", "kimi", "opencode", "gemini"})

    def test_invalid_plugin_name(self):
        with self.assertRaises(ValueError):
            get_provider("../codex")

    def test_custom_plugin(self):
        base = Path("D:/Temp/agents-core/providers") if os.name == "nt" else Path(tempfile.gettempdir()) / "agents-core/providers"
        base.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=base) as temp:
            folder = Path(temp) / "fixture"
            folder.mkdir()
            (folder / "provider.py").write_text("from neoxider_agents.providers import BaseProvider\nclass Provider(BaseProvider):\n def resolve(self,model='',effort=''): return 'fixture',effort\n", encoding="utf-8")
            with patch.dict(os.environ, {"AGENT_PROVIDER_DIR": temp}):
                self.assertEqual(get_provider("fixture").resolve(), ("fixture", ""))


class Commands(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {"CODEX_HOME": "D:/Temp/agents-core/providers/codex-home"}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def command(self, engine, session="", chat_only=False):
        with patch("neoxider_agents.providers.executable", return_value=[engine]):
            # Plugin binds executable at import, so load inside the patch.
            return get_provider(engine).command("model", "high", "D:/Temp/agents-core/providers", session, chat_only)

    def test_codex_prompt_from_stdin(self):
        args, _ = self.command("codex")
        self.assertEqual(args[-1], "-")
        self.assertIn("--ignore-user-config", args)
        self.assertIn("danger-full-access", args)

    def test_codex_resume_model_effort(self):
        args, _ = self.command("codex", "ses_old")
        self.assertEqual(args[-2:], ["ses_old", "-"])
        self.assertIn("resume", args)
        self.assertIn("-m", args)
        self.assertEqual(args[args.index("-m") + 1], "model")
        self.assertIn('model_reasoning_effort="high"', args)
        self.assertIn('sandbox_mode="danger-full-access"', args)

    def test_codex_chat_only_overrides_env(self):
        with patch.dict(os.environ, {"AGENT_CODEX_SANDBOX": "danger-full-access", "AGENT_CODEX_USER_CONFIG": "1", "AGENT_CODEX_MCP": "private=http://localhost"}):
            args, _ = self.command("codex", chat_only=True)
        self.assertIn("read-only", args)
        self.assertIn("--ignore-user-config", args)
        self.assertFalse(any("mcp_servers" in arg for arg in args))

    def test_codex_mcp_literal_url(self):
        with patch.dict(os.environ, {"AGENT_CODEX_MCP": "unity=http://localhost:123/path?x=1, bad.name=http://bad"}):
            args, _ = self.command("codex")
        self.assertIn('mcp_servers.unity.url="http://localhost:123/path?x=1"', args)
        self.assertFalse(any("bad.name" in arg for arg in args))

    def test_claude_unattended_streaming(self):
        args, _ = self.command("claude")
        self.assertIn("--dangerously-skip-permissions", args)
        self.assertIn("stream-json", args)
        self.assertIn("-p", args)

    def test_claude_resume(self):
        args, _ = self.command("claude", "ses_old")
        self.assertIn("--resume", args)
        self.assertIn("ses_old", args)

    def test_claude_chat_only_zero_tools(self):
        with patch.dict(os.environ, {"AGENT_CLAUDE_PERMISSION": "--dangerously-skip-permissions"}):
            args, _ = self.command("claude", chat_only=True)
        self.assertNotIn("--dangerously-skip-permissions", args)
        self.assertIn("--strict-mcp-config", args)
        self.assertEqual(args[args.index("--tools") + 1], "")

    def test_claude_stream_opt_out(self):
        with patch.dict(os.environ, {"AGENT_STREAM_TEXT": "0"}):
            args, _ = self.command("claude")
        self.assertNotIn("stream-json", args)

    def test_opencode_flags_and_small_model(self):
        args, env = self.command("opencode", "ses_old")
        self.assertIn("--auto", args)
        self.assertEqual(args[-2:], ["-s", "ses_old"])
        self.assertEqual(json.loads(env["OPENCODE_CONFIG_CONTENT"]), {"small_model": "model"})

    def test_opencode_directory_explicit(self):
        args, _ = self.command("opencode", "ses_old")
        self.assertEqual(args[args.index("--dir") + 1], "D:/Temp/agents-core/providers")

    def test_opencode_respects_config(self):
        with patch.dict(os.environ, {"OPENCODE_CONFIG_CONTENT": '{"small_model":"custom"}'}):
            _, env = self.command("opencode")
        self.assertNotIn("OPENCODE_CONFIG_CONTENT", env)

    def test_opencode_chat_only_config(self):
        args, env = self.command("opencode", chat_only=True)
        self.assertIn("neoxider-chat-only", args)
        data = json.loads(Path(env["OPENCODE_CONFIG"]).read_text(encoding="utf-8"))
        self.assertEqual(data["agent"]["neoxider-chat-only"]["tools"], {"*": False})

    def test_kimi_uses_acp_prompt_transport(self):
        args, _ = self.command("kimi")
        self.assertTrue(any("acp_adapter.py" in arg for arg in args))
        self.assertNotIn("--prompt", args)

    def test_kimi_missing_cli_preflight(self):
        with patch("neoxider_agents.providers.executable", side_effect=FileNotFoundError("fixture Kimi CLI missing")):
            provider = get_provider("kimi")
            with self.assertRaisesRegex(FileNotFoundError, "Kimi CLI missing"):
                provider.command("kimi-code/k3", "", "D:/Temp/agents-core/providers")

    def test_gemini_safe_chat_mode(self):
        args, env = self.command("gemini", chat_only=True)
        self.assertIn("plan", args)
        self.assertNotIn("--yolo", args)
        self.assertEqual(env["GEMINI_CLI_NO_RELAUNCH"], "1")

    def test_gemini_refuses_resume(self):
        with self.assertRaises(ValueError):
            self.command("gemini", "ses_old")

    def test_codex_lock_free(self):
        self.assertEqual(get_provider("codex").prepare_resume("not-created", ""), "")

    def test_codex_lock_held_timeout(self):
        provider = get_provider("codex")
        with patch.object(provider, "writer_held", return_value=True), patch.dict(os.environ, {"AGENT_CODEX_WRITER_WAIT_SEC": "0"}):
            with self.assertRaises(ValueError) as caught:
                provider.prepare_resume("ses_locked", "")
            self.assertEqual(caught.exception.code, 75)


class NativeLaunchers(unittest.TestCase):
    def setUp(self):
        base = Path("D:/Temp/agents-core/providers") if os.name == "nt" else Path(tempfile.gettempdir()) / "agents-core/providers"
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)

    @unittest.skipUnless(os.name == "nt", "Windows npm launcher format")
    def test_native_exe_unwrapped(self):
        (self.folder / "engine.exe").touch()
        launcher = self.folder / "engine.cmd"
        launcher.write_text('"%dp0%\\engine.exe" %*', encoding="utf-8")
        with patch.dict(os.environ, {"AGENT_TEST_BIN": str(launcher)}):
            self.assertEqual(executable("test"), [str(self.folder / "engine.exe")])

    @unittest.skipUnless(os.name == "nt", "Windows npm launcher format")
    def test_node_js_unwrapped(self):
        (self.folder / "engine.js").touch()
        (self.folder / "node.exe").touch()
        launcher = self.folder / "engine.cmd"
        launcher.write_text('"%dp0%\\node.exe" "%dp0%\\engine.js" %*', encoding="utf-8")
        with patch.dict(os.environ, {"AGENT_TEST_BIN": str(launcher)}):
            self.assertEqual(executable("test"), [str(self.folder / "node.exe"), str(self.folder / "engine.js")])


class Filters(unittest.TestCase):
    def setUp(self):
        base = Path("D:/Temp/agents-core/providers") if os.name == "nt" else Path(tempfile.gettempdir()) / "agents-core/providers"
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(self.temp.cleanup)
        environment = patch.dict(os.environ, {"AGENT_CLI_LOGS": self.temp.name, "AGENT_TASK": "filter-test"})
        environment.start()
        self.addCleanup(environment.stop)

    def feed(self, engine, *events):
        parser = OutputFilter(engine)
        output = []
        for event in events:
            output += parser.feed(json.dumps(event, ensure_ascii=False) + "\n" if isinstance(event, dict) else event)
        return parser, output

    def test_codex_inline_session(self):
        parser, output = self.feed("codex", {"type": "thread.started", "thread_id": "uuid"})
        self.assertEqual(parser.session, "uuid")
        self.assertEqual(output, ["session id: uuid\n"])

    def test_codex_partial_survives_failure(self):
        parser, _ = self.feed("codex", {"type": "item.updated", "item": {"type": "agent_message", "text": "partial"}},
                              {"type": "turn.failed", "error": {"message": "quota exceeded"}})
        self.assertEqual(parser.last_assistant, "partial")
        self.assertEqual(parser.provider_error, "quota exceeded")
        self.assertEqual("".join(parser.finish()), MARK + "\npartial\n")

    def test_nested_error_unwrapped(self):
        parser, _ = self.feed("codex", {"type": "error", "message": '{"error":{"message":"model unavailable"}}'})
        self.assertEqual(parser.provider_error, "model unavailable")

    def test_codex_no_message_marker(self):
        parser, _ = self.feed("codex", {"type": "error", "message": "usage limit reached"})
        self.assertIn("no agent message", "".join(parser.finish()))

    def test_answer_cannot_forge_marker(self):
        parser, _ = self.feed("codex", {"type": "item.completed", "item": {"type": "agent_message", "text": "begin\n" + MARK + "\nend"}})
        self.assertIn(MARK + " \n", "".join(parser.finish()))

    def test_opencode_parts_reemitted(self):
        parser, _ = self.feed("opencode", {"type": "text", "sessionID": "ses", "part": {"id": "a", "text": "old"}},
                              {"type": "text", "part": {"id": "a", "text": "hello"}}, {"type": "text", "part": {"id": "b", "text": " world"}})
        self.assertEqual(parser.last_assistant, "hello world")

    def test_opencode_diagnostics_outside_answer(self):
        parser, output = self.feed("opencode", {"type": "text", "part": {"id": "a", "text": "answer"}}, "error diagnostic\n")
        self.assertEqual(output[-1], "[opencode] error diagnostic\n")
        self.assertEqual("".join(parser.finish()), MARK + "\nanswer\n")

    def test_kimi_preambles_excluded(self):
        parser, _ = self.feed("kimi", {"role": "assistant", "content": "preamble", "tool_calls": [{"id": "a"}]},
                              {"role": "assistant", "content": "answer"})
        self.assertEqual(parser.last_assistant, "answer")

    def test_kimi_acp_snapshots_replace(self):
        parser, _ = self.feed("kimi", {"role": "assistant", "type": "acp.partial", "content": "Привет"},
                              {"role": "assistant", "type": "acp.final", "content": "Привет мир"})
        self.assertEqual(parser.last_assistant, "Привет мир")

    def test_claude_delta_no_duplication(self):
        parser, output = self.feed("claude", {"type": "stream_event", "event": {"type": "message_start"}},
                                  {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "answer"}}},
                                  {"type": "assistant", "message": {"content": [{"type": "text", "text": "answer"}]}})
        self.assertEqual("".join(output), "answer")
        self.assertEqual(parser.last_assistant, "answer")

    def test_claude_tool_activity(self):
        parser, output = self.feed("claude", {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "echo test"}}]}})
        self.assertEqual(output, ["[agent-activity] tool Bash\n"])
        self.assertEqual(parser.events[0], ("command", "echo test"))

    def test_claude_result_only(self):
        parser, _ = self.feed("claude", {"type": "result", "result": "clean answer"})
        self.assertEqual(parser.last_assistant, "clean answer")

    def test_claude_error_result(self):
        parser, output = self.feed("claude", {"type": "result", "is_error": True, "result": "Not logged in"})
        self.assertEqual(parser.provider_error, "Not logged in")
        self.assertIn("AGENT_PROVIDER_ERROR:", "".join(output))

    def test_claude_inline_session(self):
        parser, output = self.feed("claude", {"type": "system", "subtype": "init", "session_id": "uuid"})
        self.assertEqual(output, ["session id: uuid\n"])

    def test_live_generic_limit_words_ignored(self):
        parser, _ = self.feed("codex", {"type": "item.completed", "item": {"type": "agent_message", "text": "Fix rate limit handling"}})
        self.assertEqual(parser.provider_error, "")
        self.assertEqual(parser.final_failure(0), "")

    def test_finish_idempotent(self):
        parser, _ = self.feed("codex", {"type": "item.completed", "item": {"type": "agent_message", "text": "answer"}})
        self.assertTrue(parser.finish())
        self.assertEqual(parser.finish(), [])

    def test_raw_memory_bounded(self):
        parser = OutputFilter("codex")
        for _ in range(1024):
            parser.feed("x" * 10000 + "\n")
        self.assertLessEqual(parser.raw_size, LIMIT)

    def test_partial_memory_bounded(self):
        parser, _ = self.feed("codex", {"type": "item.updated", "item": {"type": "agent_message", "text": "x" * (LIMIT * 5)}})
        self.assertLessEqual(len(parser.last_assistant), LIMIT)

    def test_large_codex_answer_replayed_exactly(self):
        text = "Привет世界" * 50000 + "\n" + MARK + "\nlast"
        parser, _ = self.feed("codex", {"type": "item.completed", "item": {"type": "agent_message", "text": text}})
        chunks = list(parser.finish())
        self.assertEqual("".join(chunks), MARK + "\n" + text.replace(MARK, MARK + " ") + "\n")
        self.assertLessEqual(max(map(len, chunks)), 65536)
        self.assertLessEqual(len(parser.last_assistant), LIMIT)
        self.assertTrue(str(parser.answer_path).startswith(self.temp.name))

    def test_large_opencode_part_replacement(self):
        parser, _ = self.feed("opencode", {"type": "text", "part": {"id": "first", "text": "old" * 150000}},
                             {"type": "text", "part": {"id": "second", "text": " middle"}},
                             {"type": "text", "part": {"id": "first", "text": "Ю" * 300000}})
        self.assertEqual("".join(parser.finish()), MARK + "\n" + "Ю" * 300000 + " middle\n")
        self.assertLessEqual(len(parser.last_assistant), LIMIT)

    def test_large_claude_delta_replay(self):
        parser = OutputFilter("claude")
        for _ in range(100):
            parser.feed(json.dumps({"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Ю" * 4000}}}) + "\n")
        self.assertEqual("".join(parser.finish()).lstrip("\n"), MARK + "\n" + "Ю" * 400000 + "\n")
        self.assertLessEqual(len(parser.last_assistant), LIMIT)

    def test_large_opencode_unicode_whitespace(self):
        text = "\u3000" * 150000 + "answer" + "\u3000" * 150000
        parser, _ = self.feed("opencode", {"type": "text", "part": {"id": "first", "text": text}})
        self.assertEqual("".join(parser.finish()), MARK + "\nanswer\n")

    def test_thinking_not_in_digest(self):
        parser, _ = self.feed("codex", {"type": "item.started", "item": {"type": "reasoning", "text": "private reasoning"}})
        self.assertEqual(parser.events, [("thinking", "thinking")])


class Classification(unittest.TestCase):
    def test_prompt_echo_ignored(self):
        self.assertEqual(failure_reason("rate limit in prompt\n" + MARK + "\nhealthy\n"), "")

    def test_live_only_explicit(self):
        self.assertEqual(failure_reason("rate limit\n", live=True), "")
        self.assertEqual(failure_reason("AGENT_PROVIDER_ERROR: quota exceeded\n", live=True), "quota exceeded")

    def test_postmortem_model_error(self):
        self.assertIn("newer version", failure_reason("Model requires a newer version of Codex"))

    def test_transient_auth_not_retryable(self):
        self.assertFalse(is_transient_failure("unauthorized HTTP 503"))
        self.assertTrue(is_transient_failure("ECONNRESET"))

    def test_opencode_retry_policy(self):
        provider = get_provider("opencode")
        self.assertTrue(provider.retry_reason("fetch failed", 1))
        for code in (0, 124, 125, 126):
            self.assertEqual(provider.retry_reason("fetch failed", code), "")
        self.assertEqual(provider.retry_reason("rate limit fetch failed", 1), "")

    def test_opencode_backoff(self):
        provider = get_provider("opencode")
        self.assertEqual([provider.retry_delay(n) for n in (1, 2, 3, 8)], [15, 30, 60, 120])

    def test_codex_conflict_retry_only_resume(self):
        provider = get_provider("codex")
        self.assertTrue(provider.retry_reason("already has an active writer", 1, "ses"))
        self.assertEqual(provider.retry_reason("already has an active writer", 1), "")


class ACPTransport(unittest.TestCase):
    def setUp(self):
        base = Path("D:/Temp/agents-core/providers") if os.name == "nt" else Path(tempfile.gettempdir()) / "agents-core/providers"
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.server = self.folder / "fake-kimi.py"
        self.server.write_text('''import json,sys
from pathlib import Path
target=Path(__file__).parent
for line in sys.stdin:
 message=json.loads(line)
 method=message.get("method")
 if not method:
  (target/"permission.json").write_text(json.dumps(message),encoding="utf-8")
  continue
 identity=message["id"]
 params=message.get("params") or {}
 with (target/"requests.jsonl").open("a",encoding="utf-8") as capture:
  capture.write(json.dumps(message,ensure_ascii=False)+"\\n")
 result={}
 if method=="initialize": result={"protocolVersion":1,"agentCapabilities":{"loadSession":True}}
 elif method=="session/new": result={"sessionId":"ses_fake"}
 elif method=="session/load":
  print(json.dumps({"jsonrpc":"2.0","method":"session/update","params":{"update":{"sessionUpdate":"agent_message_chunk","content":{"type":"text","text":"OLD REPLAY"}}}}),flush=True)
 elif method=="session/prompt":
  prompt=params["prompt"][0]["text"]
  (target/"prompt.txt").write_text(prompt,encoding="utf-8")
  for text in ["Привет", " мир"]:
   print(json.dumps({"jsonrpc":"2.0","method":"session/update","params":{"update":{"sessionUpdate":"agent_message_chunk","messageId":"new","content":{"type":"text","text":text}}}},ensure_ascii=False),flush=True)
  print(json.dumps({"jsonrpc":"2.0","id":99,"method":"session/request_permission","params":{"options":[{"kind":"allow_once","optionId":"yes"}]}}),flush=True)
  response=json.loads(sys.stdin.readline())
  (target/"permission.json").write_text(json.dumps(response),encoding="utf-8")
  result={"stopReason":"end_turn"}
 print(json.dumps({"jsonrpc":"2.0","id":identity,"result":result}),flush=True)
''', encoding="utf-8")

    def invoke(self, prompt="Точное сообщение\nВторая строка", session="", chat_only=False):
        args = [sys.executable, str(ROOT / "providers/kimi/acp_adapter.py"), "--cwd", str(self.folder), "--model", "kimi-code/k3"]
        if session:
            args += ["--session", session]
        if chat_only:
            args += ["--chat-only"]
        prompt_path = self.folder / "input.txt"
        prompt_path.write_bytes(prompt.encode("utf-8"))
        env = dict(os.environ, AGENT_KIMI_BIN=str(self.server), PYTHONPATH=str(ROOT), PYTHONIOENCODING="utf-8")
        with prompt_path.open("rb") as source:
            result = subprocess.run(args, stdin=source, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    env=env, timeout=15, **hidden_options())
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace") + result.stdout.decode("utf-8", "replace"))
        parser = OutputFilter("kimi")
        for line in result.stdout.decode("utf-8").splitlines(True):
            parser.feed(line)
        return parser

    def test_utf8_file_prompt_round_trip(self):
        parser = self.invoke()
        self.assertEqual((self.folder / "prompt.txt").read_text(encoding="utf-8"), "Точное сообщение\nВторая строка")
        self.assertEqual(parser.last_assistant, "Привет мир")
        self.assertEqual(parser.session, "ses_fake")

    def test_long_prompt_never_argv(self):
        text = "Юникод " * 8000
        parser = self.invoke(text)
        self.assertEqual((self.folder / "prompt.txt").read_text(encoding="utf-8"), text)
        self.assertEqual(parser.last_assistant, "Привет мир")

    def test_resume_replay_excluded(self):
        parser = self.invoke(session="ses_original")
        self.assertEqual(parser.session, "ses_original")
        self.assertEqual(parser.last_assistant, "Привет мир")
        self.assertNotIn("OLD REPLAY", parser.last_assistant)

    def test_normal_permission_auto_approved(self):
        self.invoke()
        result = json.loads((self.folder / "permission.json").read_text())
        self.assertEqual(result["result"]["outcome"], {"outcome": "selected", "optionId": "yes"})

    def test_chat_permission_denied(self):
        self.invoke(chat_only=True)
        result = json.loads((self.folder / "permission.json").read_text())
        self.assertEqual(result["result"]["outcome"], {"outcome": "cancelled"})


def prove_defects():
    """Plant source defects in isolated copies and demand the targeted test fail."""
    import shutil
    base = Path("D:/Temp/agents-core/providers") if os.name == "nt" else Path(tempfile.gettempdir()) / "agents-core/providers"
    base.mkdir(parents=True, exist_ok=True)
    defects = [
        ("providers/codex/provider.py", '"": "gpt-5.6-terra"', '"": "wrong-default"', "Aliases.test_codex_default"),
        ("providers/codex/provider.py", 'sandbox = "read-only" if chat_only', 'sandbox = "danger-full-access" if chat_only', "Commands.test_codex_chat_only_overrides_env"),
        ("providers/codex/provider.py", 'args += ["-m", model]', 'args += []', "Commands.test_codex_resume_model_effort"),
        ("providers/claude/provider.py", '"--tools", ""', '"--tools", "Bash"', "Commands.test_claude_chat_only_zero_tools"),
        ("providers/claude/provider.py", 'or "--dangerously-skip-permissions"', 'or "--permission-mode acceptEdits"', "Commands.test_claude_unattended_streaming"),
        ("providers/opencode/provider.py", ' or FAILURE.search(text)', '', "Classification.test_opencode_retry_policy"),
        ("providers/opencode/provider.py", 'and not os.environ.get("OPENCODE_CONFIG_CONTENT")', 'and True', "Commands.test_opencode_respects_config"),
        ("neoxider_agents/output.py", 'elif tag in ("item.started", "item.updated", "item.completed"):', 'elif tag in ("item.started", "item.completed"):', "Filters.test_codex_partial_survives_failure"),
        ("neoxider_agents/output.py", 'value = nested or inner.get("message") or value', 'value = value', "Filters.test_nested_error_unwrapped"),
        ("neoxider_agents/output.py", 'line + " " if line == MARK else line', 'line', "Filters.test_answer_cannot_forge_marker"),
        ("neoxider_agents/output.py", 'if not self.claude_streamed:', 'if True:', "Filters.test_claude_delta_no_duplication"),
        ("neoxider_agents/output.py", 'self._set_answer(text)\n                    else:', 'self._append_answer(text)\n                    else:', "Filters.test_kimi_acp_snapshots_replace"),
        ("neoxider_agents/output.py", 'while self.raw_size > LIMIT and self.raw:', 'while False:', "Filters.test_raw_memory_bounded"),
        ("providers/kimi/acp_adapter.py", 'if allows and not self.chat_only else', 'if allows else', "ACPTransport.test_chat_permission_denied"),
        ("neoxider_agents/providers.py", ' and target.name.lower() != "node.exe"', '', "NativeLaunchers.test_node_js_unwrapped"),
        ("neoxider_agents/output.py", 'stream.write(text)\n            self.answer_path = path', 'stream.write(text[-LIMIT:])\n            self.answer_path = path', "Filters.test_large_codex_answer_replayed_exactly"),
        ("providers/opencode/provider.py", ', "--dir", str(cwd)', '', "Commands.test_opencode_directory_explicit"),
        ("providers/kimi/provider.py", '        executable(self.engine)\n        args = [sys.executable', '        args = [sys.executable', "Commands.test_kimi_missing_cli_preflight"),
    ]
    results = []
    with tempfile.TemporaryDirectory(dir=base, prefix="mutation-") as temp:
        copy = Path(temp)
        assert str(copy.resolve()).startswith(str(base.resolve()))
        for directory in ("providers", "neoxider_agents"):
            shutil.copytree(ROOT / directory, copy / directory, ignore=shutil.ignore_patterns("__pycache__"))
        (copy / "tests").mkdir()
        shutil.copy2(__file__, copy / "tests/test_providers_core.py")
        shutil.copy2(ROOT / "activity.py", copy / "activity.py")
        for number, (file, before, after, selected) in enumerate(defects, 1):
            target = copy / file
            original = target.read_text(encoding="utf-8")
            if before not in original:
                raise AssertionError("mutation no longer matches source: " + file + " " + before)
            target.write_text(original.replace(before, after, 1), encoding="utf-8")
            env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
            result = subprocess.run([sys.executable, str(copy / "tests/test_providers_core.py"), selected],
                                    cwd=copy, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    timeout=30, **hidden_options())
            target.write_text(original, encoding="utf-8")
            killed = result.returncode != 0 and b"FAILED" in result.stdout
            record = {"number": number, "file": file, "test": selected, "caught": killed,
                      "output": result.stdout.decode("utf-8", "replace")[-1500:]}
            results.append(record)
            print("%02d %s %s" % (number, "CAUGHT" if killed else "MISSED", selected))
    path = base / "planted-defects.json"
    path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print("%s/%s planted provider defects caught; evidence: %s" % (sum(item["caught"] for item in results), len(results), path))
    return 0 if all(item["caught"] for item in results) else 1


if __name__ == "__main__":
    if "--prove-defects" in sys.argv:
        sys.exit(prove_defects())
    unittest.main()
