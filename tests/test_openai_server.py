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
    def test_explicit_model_and_effort(self):
        self.assertEqual(srv.model_label("claude", "sonnet", "high"), "claude/sonnet-high")

    def test_explicit_model_no_effort(self):
        self.assertEqual(srv.model_label("codex", "spark", ""), "codex/spark")

    def test_falls_back_to_provider_default_model(self):
        # empty model -> PROVIDERS[engine]["default_model"] when known, else "default"
        engine = next(iter(srv.PROVIDERS), None)
        if engine:
            expected_model = srv.PROVIDERS[engine].get("default_model") or "default"
            self.assertEqual(srv.model_label(engine, "", ""), "%s/%s" % (engine, expected_model))

    def test_unknown_engine_with_no_model_falls_back_to_default_literal(self):
        self.assertEqual(srv.model_label("totally-unknown-engine-xyz", "", ""), "totally-unknown-engine-xyz/default")


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


class ScratchDirTests(unittest.TestCase):
    def test_creates_and_returns_a_directory(self):
        import shutil
        d = srv.scratch_dir("test-scratch-dir-xyz")
        try:
            self.assertTrue(os.path.isdir(d))
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
