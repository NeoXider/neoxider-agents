#!/usr/bin/env python3
"""tests/test_openai_server.py — zero-dependency regression tests for openai_server.py's
pure-logic pieces: message rendering, prompt building, tool-call extraction, and the small
label/log helpers. Deliberately does NOT spin up the real HTTP server or shell out to agent.sh
(that's exercised live, by hand, against a real CLI subagent -- see README.md) -- this suite
only covers the parsing/formatting logic that's cheap and deterministic to test in isolation.

Run:
    python tests/test_openai_server.py
    python -m unittest tests.test_openai_server   (from the repo root)
"""
import importlib.util
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
MOD_PATH = os.path.join(REPO_ROOT, "openai_server.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("cli_agents_openai_server", MOD_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


srv = _load_module()


class ToGitBashPathTests(unittest.TestCase):
    def test_windows_forward_slash_drive_path(self):
        self.assertEqual(srv.to_git_bash_path("C:/Git/CoreAI"), "/c/Git/CoreAI")

    def test_windows_backslash_drive_path(self):
        self.assertEqual(srv.to_git_bash_path("C:\\Git\\CoreAI"), "/c/Git/CoreAI")

    def test_empty_string_passes_through(self):
        self.assertEqual(srv.to_git_bash_path(""), "")


class LastOutputTests(unittest.TestCase):
    def test_extracts_text_after_last_marker(self):
        log = (
            "========== [run] ... ==========\n> PROMPT:\nhello\n"
            "---------- output ----------\nfirst reply\n"
            "========== [reply] ... ==========\n> ANSWER:\nmore\n"
            "---------- output ----------\nsecond reply\n"
        )
        self.assertEqual(srv.last_output(log), "second reply\n")

    def test_no_marker_returns_whole_text(self):
        self.assertEqual(srv.last_output("just some raw text"), "just some raw text")

    def test_empty_text(self):
        self.assertEqual(srv.last_output(""), "")


class ModelLabelTests(unittest.TestCase):
    """model_label must show a versioned, human-readable name (e.g. "Sonnet 5", "Opus 4.8"),
    not the bare CLI alias ("sonnet", "opus") -- that was the exact bug reported live: the
    bridge's `model` field said "claude/sonnet-low"/"claude/opus" with no version number,
    giving no indication of which real model the alias points to."""

    def setUp(self):
        # isolate from the real providers/*/provider.json files for the lookup-logic tests
        self._orig_providers = srv.PROVIDERS
        srv.PROVIDERS = {
            "fakeeng": {
                "default_model": "alpha",
                "model_labels": {"alpha": "Alpha 9", "beta": "Beta 2.1"},
            }
        }

    def tearDown(self):
        srv.PROVIDERS = self._orig_providers

    def test_known_alias_uses_display_name_from_model_labels(self):
        self.assertEqual(srv.model_label("fakeeng", "beta", ""), "fakeeng/Beta 2.1")

    def test_effort_appended_in_parens_after_display_name(self):
        self.assertEqual(srv.model_label("fakeeng", "beta", "high"), "fakeeng/Beta 2.1 (high)")

    def test_unknown_alias_falls_back_to_the_raw_alias_itself(self):
        self.assertEqual(srv.model_label("fakeeng", "gamma-not-in-map", ""), "fakeeng/gamma-not-in-map")

    def test_empty_model_falls_back_to_provider_default_then_its_display_name(self):
        self.assertEqual(srv.model_label("fakeeng", "", ""), "fakeeng/Alpha 9")

    def test_unknown_engine_with_no_model_falls_back_to_default_literal(self):
        self.assertEqual(srv.model_label("totally-unknown-engine-xyz", "", ""), "totally-unknown-engine-xyz/default")


class ModelLabelRealProviderDataTests(unittest.TestCase):
    """Integration-style checks against the actual providers/*/provider.json shipped in this
    repo -- pins down the exact real-world regression: claude's "sonnet"/"opus" aliases must
    resolve to a versioned display name, not pass through unchanged."""

    def test_claude_sonnet_alias_shows_version_number(self):
        self.assertEqual(srv.model_label("claude", "sonnet", "low"), "claude/Sonnet 5 (low)")

    def test_claude_opus_alias_shows_version_number(self):
        self.assertEqual(srv.model_label("claude", "opus", ""), "claude/Opus 4.8")

    def test_claude_haiku_alias_shows_version_number(self):
        self.assertEqual(srv.model_label("claude", "haiku", ""), "claude/Haiku 4.5")

    def test_codex_spark_alias_shows_full_name(self):
        self.assertEqual(srv.model_label("codex", "spark", ""), "codex/GPT-5.3 Codex Spark")

    def test_codex_default_model_shows_version_number(self):
        self.assertEqual(srv.model_label("codex", "5.5", "medium"), "codex/GPT-5.5 (medium)")


class ContentTextTests(unittest.TestCase):
    def test_plain_string(self):
        self.assertEqual(srv._content_text("hello"), "hello")

    def test_none_becomes_empty(self):
        self.assertEqual(srv._content_text(None), "")

    def test_text_parts_list(self):
        parts = [{"type": "text", "text": "hello"}, {"type": "text", "text": "world"}]
        self.assertEqual(srv._content_text(parts), "hello\nworld")

    def test_image_part_is_noted_not_rendered(self):
        parts = [{"type": "image_url", "image_url": {"url": "http://x/y.png"}}]
        self.assertIn("image omitted", srv._content_text(parts))


class RenderMessagesTests(unittest.TestCase):
    def test_system_and_user_roles(self):
        messages = [
            {"role": "system", "content": "be terse"},
            {"role": "user", "content": "hi"},
        ]
        out = srv.render_messages(messages)
        self.assertIn("[SYSTEM]\nbe terse", out)
        self.assertIn("[USER]\nhi", out)

    def test_tool_result_message(self):
        messages = [{"role": "tool", "name": "get_weather", "content": "22C sunny"}]
        out = srv.render_messages(messages)
        self.assertIn("[TOOL RESULT for get_weather]", out)
        self.assertIn("22C sunny", out)

    def test_assistant_tool_call_message(self):
        messages = [{
            "role": "assistant",
            "tool_calls": [{"function": {"name": "get_weather", "arguments": '{"city": "Paris"}'}}],
        }]
        out = srv.render_messages(messages)
        self.assertIn("[ASSISTANT CALLED FUNCTION]", out)
        self.assertIn("get_weather", out)
        self.assertIn("Paris", out)

    def test_message_order_is_preserved(self):
        messages = [{"role": "user", "content": "first"}, {"role": "assistant", "content": "second"},
                    {"role": "user", "content": "third"}]
        out = srv.render_messages(messages)
        self.assertLess(out.index("first"), out.index("second"))
        self.assertLess(out.index("second"), out.index("third"))


class BuildPromptTests(unittest.TestCase):
    def test_no_tools_is_just_rendered_messages(self):
        messages = [{"role": "user", "content": "hi"}]
        self.assertEqual(srv.build_prompt(messages, None), srv.render_messages(messages))

    def test_tools_appends_function_calling_instructions(self):
        messages = [{"role": "user", "content": "hi"}]
        tools = [{"type": "function", "function": {"name": "f", "parameters": {}}}]
        out = srv.build_prompt(messages, tools)
        self.assertIn("Function-calling mode", out)
        self.assertIn('"name": "f"', out)
        self.assertIn("Do NOT use your own shell", out)


class ExtractToolCallsTests(unittest.TestCase):
    """extract_tool_calls returns (tool_calls_or_None, display_text)."""

    def test_no_tool_call_returns_none_and_original_text(self):
        calls, text = srv.extract_tool_calls("Just a plain prose answer.")
        self.assertIsNone(calls)
        self.assertEqual(text, "Just a plain prose answer.")

    def test_fenced_json_tool_call(self):
        text = 'Sure, calling it now.\n```json\n{"tool_calls":[{"name":"get_weather","arguments":{"city":"Paris"}}]}\n```'
        calls, _ = srv.extract_tool_calls(text)
        self.assertIsNotNone(calls)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["type"], "function")
        self.assertEqual(calls[0]["function"]["name"], "get_weather")
        self.assertEqual(json.loads(calls[0]["function"]["arguments"]), {"city": "Paris"})
        self.assertTrue(calls[0]["id"].startswith("call_"))

    def test_bare_unfenced_json_tool_call(self):
        text = '{"tool_calls":[{"name":"ping","arguments":{}}]}'
        calls, _ = srv.extract_tool_calls(text)
        self.assertIsNotNone(calls)
        self.assertEqual(calls[0]["function"]["name"], "ping")

    def test_last_fence_wins_when_multiple_present(self):
        text = (
            '```json\n{"tool_calls":[{"name":"first","arguments":{}}]}\n```\n'
            'actually let me reconsider...\n'
            '```json\n{"tool_calls":[{"name":"second","arguments":{}}]}\n```'
        )
        calls, _ = srv.extract_tool_calls(text)
        self.assertEqual(calls[0]["function"]["name"], "second")

    def test_malformed_json_in_fence_is_ignored(self):
        text = '```json\n{not valid json\n```'
        calls, cleaned = srv.extract_tool_calls(text)
        self.assertIsNone(calls)
        # unparseable fence is left as-is -- only recognized {"tool_calls":...} blocks get stripped
        self.assertEqual(cleaned, text)

    def test_multiple_tool_calls_in_one_block(self):
        text = '```json\n{"tool_calls":[{"name":"a","arguments":{}},{"name":"b","arguments":{"x":1}}]}\n```'
        calls, _ = srv.extract_tool_calls(text)
        self.assertEqual(len(calls), 2)
        self.assertEqual([c["function"]["name"] for c in calls], ["a", "b"])

    def test_string_arguments_are_passed_through_unmodified(self):
        text = '```json\n{"tool_calls":[{"name":"a","arguments":"{\\"already\\":\\"a string\\"}"}]}\n```'
        calls, _ = srv.extract_tool_calls(text)
        self.assertEqual(calls[0]["function"]["arguments"], '{"already":"a string"}')

    def test_empty_tool_calls_fence_is_stripped_from_display_text(self):
        # Regression test: observed live against Claude after a tool-result round-trip -- the
        # model gave its real prose answer but ALSO echoed a stray empty {"tool_calls":[]}
        # fence beforehand, despite being instructed not to. That noise must not leak into the
        # user-facing `content` string, and an empty list must not be treated as a real call.
        text = '```json\n{"tool_calls":[]}\n```\n\nTokyo is currently 22C, sunny, with a light breeze.'
        calls, cleaned = srv.extract_tool_calls(text)
        self.assertIsNone(calls)
        self.assertEqual(cleaned, "Tokyo is currently 22C, sunny, with a light breeze.")
        self.assertNotIn("tool_calls", cleaned)

    def test_empty_tool_calls_fence_alone_yields_empty_string(self):
        calls, cleaned = srv.extract_tool_calls('```json\n{"tool_calls":[]}\n```')
        self.assertIsNone(calls)
        self.assertEqual(cleaned, "")


class IsExtensionTests(unittest.TestCase):
    """is_extension is THE deterministic check that decides whether a call continues the
    existing CLI session (via `agent.sh reply`) or falls back to a brand-new one -- this is
    the crux of the whole session model, so it gets the most thorough coverage here."""

    def test_new_turn_appended_is_an_extension(self):
        prev = [{"role": "user", "content": "hi"}]
        new = prev + [{"role": "assistant", "content": "hello"}]
        self.assertTrue(srv.is_extension(prev, new))

    def test_identical_arrays_are_not_an_extension(self):
        # nothing NEW was appended -- must not resume on a no-op repeat
        msgs = [{"role": "user", "content": "hi"}]
        self.assertFalse(srv.is_extension(msgs, list(msgs)))

    def test_shorter_array_is_not_an_extension(self):
        prev = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]
        new = [{"role": "user", "content": "a"}]
        self.assertFalse(srv.is_extension(prev, new))

    def test_edited_earlier_message_is_not_an_extension(self):
        # same length-or-longer, but the shared prefix itself changed -- must NOT resume onto
        # a session that would then disagree with what the caller thinks the history is
        prev = [{"role": "user", "content": "hi"}]
        new = [{"role": "user", "content": "hi, EDITED"}, {"role": "assistant", "content": "hello"}]
        self.assertFalse(srv.is_extension(prev, new))

    def test_empty_previous_history_with_new_messages_is_an_extension(self):
        self.assertTrue(srv.is_extension([], [{"role": "user", "content": "first ever message"}]))

    def test_both_empty_is_not_an_extension(self):
        self.assertFalse(srv.is_extension([], []))

    def test_unrelated_conversation_of_equal_length_is_not_an_extension(self):
        prev = [{"role": "user", "content": "conversation A"}]
        new = [{"role": "user", "content": "conversation B"}]
        self.assertFalse(srv.is_extension(prev, new))


class ReadMetaTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._orig_logdir = srv.LOGDIR
        srv.LOGDIR = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(srv.LOGDIR, ignore_errors=True)
        srv.LOGDIR = self._orig_logdir

    def test_reads_key_value_pairs(self):
        with open(os.path.join(srv.LOGDIR, "sometask.meta"), "w", encoding="utf-8") as f:
            f.write("state=done\nengine=claude\nmodel=sonnet-low\n")
        meta = srv.read_meta("sometask")
        self.assertEqual(meta.get("state"), "done")
        self.assertEqual(meta.get("engine"), "claude")

    def test_missing_file_returns_empty_dict(self):
        self.assertEqual(srv.read_meta("does-not-exist-xyz"), {})


class SessionDirTests(unittest.TestCase):
    def setUp(self):
        self._orig_cfg = srv.CFG

        class FakeCfg:
            dir = ""
            port = 19191

        srv.CFG = FakeCfg()

    def tearDown(self):
        import shutil
        shutil.rmtree(srv.session_scratch_dir(), ignore_errors=True)
        srv.CFG = self._orig_cfg

    def test_fresh_session_dir_creates_and_returns_the_scratch_path(self):
        d = srv.fresh_session_dir()
        self.assertTrue(os.path.isdir(d))
        self.assertEqual(d, srv.session_scratch_dir())

    def test_fresh_session_dir_wipes_leftover_files_from_a_prior_session(self):
        d = srv.fresh_session_dir()
        stray = os.path.join(d, "leftover.txt")
        with open(stray, "w", encoding="utf-8") as f:
            f.write("stale data from a previous unrelated conversation")
        d2 = srv.fresh_session_dir()
        self.assertEqual(d, d2)
        self.assertFalse(os.path.exists(stray))

    def test_pinned_dir_is_returned_as_is_and_never_wiped(self):
        import tempfile
        pinned = tempfile.mkdtemp()
        try:
            stray = os.path.join(pinned, "keep-me.txt")
            with open(stray, "w", encoding="utf-8") as f:
                f.write("a real project file")
            srv.CFG.dir = pinned
            d = srv.fresh_session_dir()
            self.assertEqual(d, pinned)
            self.assertTrue(os.path.exists(stray))  # never touched
        finally:
            import shutil
            shutil.rmtree(pinned, ignore_errors=True)


class SessionExpiryTests(unittest.TestCase):
    """An idle session must expire (session_expired() -> True) after --session-ttl seconds of
    no activity, so an abandoned conversation can't be resumed forever or grow unbounded --
    this is the 30-minute idle-timeout behavior."""

    def setUp(self):
        import time
        self._orig_session = srv.SESSION
        self._orig_cfg = srv.CFG
        self._now = time.time()

        class FakeCfg:
            session_ttl = 1800

        srv.CFG = FakeCfg()
        srv.SESSION = {"task_name": None, "messages": [], "dir": None, "last_activity": 0.0}

    def tearDown(self):
        srv.SESSION = self._orig_session
        srv.CFG = self._orig_cfg

    def test_no_session_yet_counts_as_expired(self):
        self.assertIsNone(srv.session_idle_seconds())
        self.assertTrue(srv.session_expired())

    def test_recently_active_session_is_not_expired(self):
        import time
        srv.SESSION["task_name"] = "some-task"
        srv.SESSION["last_activity"] = time.time() - 5  # 5s ago, well under the 1800s ttl
        self.assertFalse(srv.session_expired())

    def test_idle_longer_than_ttl_is_expired(self):
        import time
        srv.SESSION["task_name"] = "some-task"
        srv.SESSION["last_activity"] = time.time() - 3600  # 1h ago, past the 1800s ttl
        self.assertTrue(srv.session_expired())

    def test_idle_seconds_reports_elapsed_time(self):
        import time
        srv.SESSION["task_name"] = "some-task"
        srv.SESSION["last_activity"] = time.time() - 100
        idle = srv.session_idle_seconds()
        self.assertGreaterEqual(idle, 100)
        self.assertLess(idle, 105)  # generous slack for test execution time

    def test_exactly_at_ttl_boundary_is_not_yet_expired(self):
        import time
        srv.SESSION["task_name"] = "some-task"
        srv.SESSION["last_activity"] = time.time() - 1799  # 1s under the 1800s ttl
        self.assertFalse(srv.session_expired())


class ReplyAgentStaleGuardTests(unittest.TestCase):
    """reply_agent must NOT return the previous answer when a resume appended nothing to the log
    (e.g. `agent.sh reply` died before writing a new block). It returns None instead, so _run
    falls back to a fresh run rather than silently echoing the stale prior answer."""

    def setUp(self):
        self._real_run = srv.subprocess.run
        self._real_read_log = srv.read_log
        srv.subprocess.run = lambda *a, **k: None  # never actually shell out

    def tearDown(self):
        srv.subprocess.run = self._real_run
        srv.read_log = self._real_read_log

    def test_returns_none_when_log_did_not_grow(self):
        # read_log returns the same content before and after -> nothing was appended.
        srv.read_log = lambda name: "========== [run] ...\n---------- output ----------\nOLD ANSWER\n"
        self.assertIsNone(srv.reply_agent("codex", "spark", "medium", "/tmp/x", "t", "hi", 60))

    def test_returns_new_answer_when_log_grew(self):
        state = {"n": 0}
        before = "========== [run] ...\n---------- output ----------\nOLD ANSWER\n"
        after = before + "========== [reply] ...\n---------- output ----------\nNEW ANSWER\n"

        def fake_read_log(name):
            state["n"] += 1
            return before if state["n"] == 1 else after  # 1st call = before, later = after

        srv.read_log = fake_read_log
        self.assertEqual(srv.reply_agent("codex", "spark", "medium", "/tmp/x", "t", "hi", 60),
                         "NEW ANSWER\n")


if __name__ == "__main__":
    unittest.main()
