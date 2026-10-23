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
import argparse
import base64
import contextlib
import importlib.util
import io
import json
import logging
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

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


@unittest.skipUnless(os.name == "nt", "Windows process-window contract")
class PromptArgumentTests(unittest.TestCase):
    """A prompt too long for a command line is handed to agent.sh as a file instead.

    WHY: Windows caps a whole command line at 32767 characters. Passing a long conversation as an
    argument failed the spawn itself (WinError 206, surfacing as FileNotFoundError), which the
    bridge could only report as an opaque 500 -- the model was never asked.
    """

    def test_short_prompt_is_passed_directly(self):
        with srv._prompt_argument("build a castle") as args:
            self.assertEqual(args, ["build a castle"])

    @staticmethod
    def _capture_staged_path():
        """agent.sh is handed a git-bash path, so keep the OS path the file was actually written to."""
        seen = []
        real_mkstemp = srv.tempfile.mkstemp

        def spy(*args, **kwargs):
            fd, path = real_mkstemp(*args, **kwargs)
            seen.append(path)
            return fd, path

        return seen, spy

    def test_long_prompt_is_staged_in_a_file(self):
        text = "x" * (srv.ARGV_PROMPT_MAX + 1)
        seen, spy = self._capture_staged_path()
        with mock.patch.object(srv.tempfile, "mkstemp", spy):
            with srv._prompt_argument(text) as args:
                self.assertEqual(args[0], "--prompt-file")
                self.assertEqual(args[1], srv.to_git_bash_path(seen[0]))
                self.assertTrue(os.path.exists(seen[0]), "staged file should exist while in scope")
                with open(seen[0], encoding="utf-8") as f:
                    self.assertEqual(f.read(), text)
        self.assertFalse(os.path.exists(seen[0]), "staged file should be removed on exit")

    def test_boundary_length_still_goes_on_argv(self):
        text = "x" * srv.ARGV_PROMPT_MAX
        with srv._prompt_argument(text) as args:
            self.assertEqual(args, [text])

    def test_staged_file_is_removed_even_when_the_body_raises(self):
        text = "x" * (srv.ARGV_PROMPT_MAX + 1)
        seen, spy = self._capture_staged_path()
        with mock.patch.object(srv.tempfile, "mkstemp", spy):
            with self.assertRaises(RuntimeError):
                with srv._prompt_argument(text):
                    raise RuntimeError("agent died mid-run")
        self.assertFalse(os.path.exists(seen[0]))


class ProcessVisibilityTests(unittest.TestCase):
    def test_provider_process_groups_are_hidden_without_losing_killability(self):
        kwargs = srv._process_group_kwargs()
        flags = kwargs["creationflags"]
        self.assertTrue(flags & subprocess.CREATE_NO_WINDOW)
        self.assertTrue(flags & subprocess.CREATE_NEW_PROCESS_GROUP)
        self.assertFalse(flags & subprocess.CREATE_NEW_CONSOLE)
        self.assertTrue(kwargs["startupinfo"].dwFlags & subprocess.STARTF_USESHOWWINDOW)
        self.assertEqual(kwargs["startupinfo"].wShowWindow, subprocess.SW_HIDE)


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

    # Live 2026-09-29: `Red` + ~80 opencode schema warnings shipped as one completion because the
    # provider wrote its CLI stderr AFTER the output marker and stdout+stderr share one task log.
    SCHEMA_NOISE = ('[opencode] unknown format "uint32" ignored in schema at path '
                    '"#/anyOf/0/properties/apps/items/properties/pid"')

    def test_trailing_provider_diagnostics_are_dropped(self):
        block = "Red\n" + "\n".join([self.SCHEMA_NOISE] * 80) + "\n"
        log = "========== [run] ... ==========\n---------- output ----------\n" + block
        self.assertEqual(srv.last_output(log), "Red")

    def test_diagnostics_before_the_marker_were_never_returned(self):
        log = ("[opencode] unknown format \"uint64\" ignored in schema\n"
               "---------- output ----------\nRed\n")
        self.assertEqual(srv.last_output(log), "Red\n")

    def test_answer_without_noise_is_returned_byte_for_byte(self):
        self.assertEqual(srv.last_output("answer\n"), "answer\n")
        self.assertEqual(srv.last_output("answer"), "answer")

    def test_noise_quoted_inside_the_answer_survives(self):
        block = "The CLI logged:\n%s\nend.\n" % self.SCHEMA_NOISE
        self.assertEqual(srv.last_output("---------- output ----------\n" + block), block)

    def test_agent_bookkeeping_lines_are_dropped_too(self):
        block = ("pong\n[agent-activity] tool Read\n"
                 "AGENT_PROVIDER_ERROR: Rate limit exceeded\n"
                 "!! TIMEOUT: step exceeded AGENT_TIMEOUT_SEC=1800s and was killed\n")
        self.assertEqual(srv.last_output("---------- output ----------\n" + block), "pong")


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
        self.assertEqual(srv.model_label("codex", "sol", "medium"), "codex/GPT-5.6 Sol (medium)")

    def test_codex_gpt6_labels(self):
        self.assertEqual(srv.model_label("codex", "6-luna", "medium"), "codex/GPT-6 Luna (medium)")
        self.assertEqual(srv.model_label("codex", "6-sol", "medium"), "codex/GPT-6 Sol (medium)")

    def test_kimi_default_model_shows_k3(self):
        self.assertEqual(srv.model_label("kimi", "", ""), "kimi/Kimi K3")


class ContentTextTests(unittest.TestCase):
    def test_camera_tool_json_is_lifted_to_cli_image_part(self):
        image = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Yl6hdwAAAAASUVORK5CYII=")
        url = "data:image/png;base64," + base64.b64encode(image).decode("ascii")
        camera = {"ok": True, "camera": "Preview", "summary": "Captured scene", "dataUrl": url}
        body = {"messages": [
            {"role": "user", "content": "Inspect the scene"},
            {"role": "tool", "tool_call_id": "camera_1", "content": json.dumps(camera)},
        ]}
        with mock.patch.object(srv, "CFG", argparse.Namespace(engine="codex")):
            messages, _, _, _ = srv.validate_chat_request(body)
        self.assertIsInstance(body["messages"][1]["content"], str,
                              "normalization must not mutate the caller's transcript")
        lifted = messages[1]["content"]
        self.assertEqual("image_url", lifted[1]["type"])
        self.assertEqual(url, lifted[1]["image_url"]["url"])
        self.assertNotIn("dataUrl", lifted[0]["text"])
        with mock.patch.object(srv, "CFG", argparse.Namespace(engine="codex")):
            self.assertIn("image attached to Codex CLI", srv.render_messages(messages))
            with srv._bridge_images(messages):
                self.assertEqual(1, len(srv._chatonly_env()["AGENT_CODEX_IMAGE_PATHS"].splitlines()))

        with mock.patch.object(srv, "CFG", argparse.Namespace(engine="claude")):
            plain, _, _, _ = srv.validate_chat_request(body)
        self.assertEqual(camera, json.loads(plain[1]["content"]))

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

    def test_codex_data_url_image_is_staged_only_for_the_request(self):
        image = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Yl6hdwAAAAASUVORK5CYII=")
        url = "data:image/png;base64," + base64.b64encode(image).decode("ascii")
        message = {"role": "user", "content": [
            {"type": "text", "text": "Inspect this image"},
            {"type": "image_url", "image_url": {"url": url}},
        ]}
        with mock.patch.object(srv, "CFG", argparse.Namespace(engine="codex")):
            self.assertIn("image attached to Codex CLI", srv._content_text(message["content"]))
            with srv._bridge_images([message]):
                paths = srv._chatonly_env()["AGENT_CODEX_IMAGE_PATHS"].splitlines()
                self.assertEqual(1, len(paths))
                native_path = paths[0]
                if os.name == "nt":
                    native_path = native_path[1].upper() + ":" + native_path[2:]
                with open(native_path, "rb") as image_file:
                    self.assertEqual(image, image_file.read())
            self.assertFalse(os.path.exists(native_path))

    def test_opencode_data_url_image_is_staged_with_native_path(self):
        image = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Yl6hdwAAAAASUVORK5CYII=")
        url = "data:image/png;base64," + base64.b64encode(image).decode("ascii")
        message = {"role": "user", "content": [
            {"type": "text", "text": "Inspect this image"},
            {"type": "image_url", "image_url": {"url": url}},
        ]}
        with mock.patch.object(srv, "CFG", argparse.Namespace(engine="opencode")), \
                mock.patch.object(srv, "OPENCODE_NATIVE", False):
            self.assertIn("image attached to OpenCode CLI", srv._content_text(message["content"]))
            with srv._bridge_images([message]):
                env = srv._chatonly_env()
                self.assertNotIn("AGENT_CODEX_IMAGE_PATHS", env)
                path = env["AGENT_OPENCODE_IMAGE_PATHS"]
                with open(path, "rb") as image_file:
                    self.assertEqual(image, image_file.read())
            self.assertFalse(os.path.exists(path))

    def test_codex_rejects_malformed_unsupported_and_oversized_images(self):
        urls_and_statuses = [
            ("data:image/png;base64,not-base64!", 400),
            ("https://example.com/picture.png", 400),
            ("data:image/png;base64," + base64.b64encode(b"not-a-png").decode("ascii"), 400),
            ("data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\n"
                + b"x" * (4 * 1024 * 1024)).decode("ascii"), 413),
        ]
        with mock.patch.object(srv, "CFG", argparse.Namespace(engine="codex")):
            for url, status in urls_and_statuses:
                with self.subTest(url_prefix=url[:32]):
                    message = {"role": "user", "content": [
                        {"type": "image_url", "image_url": {"url": url}}]}
                    with self.assertRaises(srv.RequestValidationError) as caught:
                        with srv._bridge_images([message]):
                            self.fail("invalid image must not reach the provider")
                    self.assertEqual(status, caught.exception.status)
                    self.assertNotIn("AGENT_CODEX_IMAGE_PATHS", srv._chatonly_env())

    def test_codex_rejects_ninth_image_and_preserves_duplicate_image_order(self):
        image = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Yl6hdwAAAAASUVORK5CYII=")
        url = "data:image/png;base64," + base64.b64encode(image).decode("ascii")
        message = {"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": url}} for _ in range(8)]}
        with mock.patch.object(srv, "CFG", argparse.Namespace(engine="codex")):
            with srv._bridge_images([message]):
                self.assertEqual(8, len(srv._chatonly_env()["AGENT_CODEX_IMAGE_PATHS"].splitlines()))
            message["content"].append({"type": "image_url", "image_url": {"url": url}})
            with self.assertRaises(srv.RequestValidationError) as caught:
                with srv._bridge_images([message]):
                    self.fail("ninth image must not reach the provider")
            self.assertEqual(413, caught.exception.status)

    def test_codex_image_validation_stops_before_provider_invocation(self):
        message = {"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,invalid!"}}]}
        cfg = argparse.Namespace(engine="codex", model="6-luna", effort="medium",
                                 dir="/tmp/pinned", retries=0, timeout=60, session_ttl=1800)
        empty = {"task_name": None, "messages": [], "dir": None, "last_activity": 0.0}
        with mock.patch.object(srv, "CFG", cfg), mock.patch.object(srv, "SESSION", empty), \
                mock.patch.object(srv, "run_agent", side_effect=AssertionError("provider ran")):
            with self.assertRaises(srv.RequestValidationError):
                srv.H._raw_completion(object(), [message], None)

    def test_codex_image_reaches_sync_and_live_provider_branches(self):
        image = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Yl6hdwAAAAASUVORK5CYII=")
        url = "data:image/png;base64," + base64.b64encode(image).decode("ascii")
        message = {"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": url}}]}
        cfg = argparse.Namespace(engine="codex", model="6-sol", effort="medium",
                                 dir="/tmp/pinned", port=8990, retries=0, timeout=60,
                                 session_ttl=1800)
        for live in (False, True):
            with self.subTest(live=live):
                seen = []

                def provider(*args):
                    path = srv._chatonly_env()["AGENT_CODEX_IMAGE_PATHS"]
                    native_path = path[1].upper() + ":" + path[2:] if os.name == "nt" else path
                    with open(native_path, "rb") as image_file:
                        self.assertEqual(image, image_file.read())
                    seen.append(native_path)
                    return "image seen"

                empty = {"task_name": None, "messages": [], "dir": None, "last_activity": 0.0}
                with mock.patch.object(srv, "CFG", cfg), mock.patch.object(srv, "SESSION", empty), \
                        mock.patch.object(srv, "read_meta", return_value={"state": "done"}), \
                        mock.patch.object(srv, "LIVE_STREAM_ENGINES", {"codex"} if live else set()), \
                        mock.patch.object(srv, "run_agent", side_effect=provider), \
                        mock.patch.object(srv, "run_agent_live", side_effect=provider):
                    answer = srv.H._raw_completion(
                        object(), [message], None, on_delta=(lambda text: None) if live else None)
                self.assertEqual("image seen", answer)
                self.assertEqual(1, len(seen))
                self.assertFalse(os.path.exists(seen[0]))

    def test_codex_image_rejection_returns_http_400_or_413(self):
        image = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Yl6hdwAAAAASUVORK5CYII=")
        url = "data:image/png;base64," + base64.b64encode(image).decode("ascii")
        cases = [("https://example.com/picture.png", 400), ([url] * 9, 413)]
        for case, expected_status in cases:
            with self.subTest(status=expected_status):
                urls = case if isinstance(case, list) else [case]
                body = json.dumps({"messages": [{"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": item}} for item in urls]}]}).encode()
                sent = []

                class Fake:
                    _reject_unauthorized = srv.H._reject_unauthorized
                    _read_content_length = srv.H._read_content_length
                    _send_json = staticmethod(lambda code, payload: sent.append((code, payload)))
                    _sync_response = staticmethod(
                        lambda messages, tools, choice: srv._validated_bridge_images(messages))

                    def __init__(self):
                        self.path = "/v1/chat/completions"
                        self.headers = {"Content-Length": str(len(body))}
                        self.rfile = io.BytesIO(body)

                with mock.patch.object(srv, "CFG", argparse.Namespace(engine="codex", api_key="")):
                    srv.H.do_POST(Fake())
                self.assertEqual(expected_status, sent[0][0])
                self.assertEqual("invalid_request_error", sent[0][1]["error"]["type"])


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
    def test_no_tools_still_gets_base_instructions_plus_messages(self):
        # BASE_INSTRUCTIONS (the "no MCP/skills/tools, chat-content-only" guard) is prepended to
        # EVERY prompt, tools or not -- this is the "chat-only" boundary the user asked for.
        messages = [{"role": "user", "content": "hi"}]
        out = srv.build_prompt(messages, None)
        self.assertIn("MCP servers", out)
        self.assertIn(srv.render_messages(messages), out)

    def test_base_instructions_do_not_use_identity_override_framing(self):
        # Regression: an earlier draft ("You are acting as X, NOT an autonomous agent") got
        # REFUSED live by claude as a prompt-injection attempt. Guard against reintroducing it.
        self.assertNotIn("You are acting as", srv.BASE_INSTRUCTIONS)
        self.assertNotIn("NOT as an autonomous", srv.BASE_INSTRUCTIONS)

    def test_tools_appends_function_calling_instructions(self):
        messages = [{"role": "user", "content": "hi"}]
        tools = [{"type": "function", "function": {"name": "f", "parameters": {}}}]
        out = srv.build_prompt(messages, tools)
        self.assertIn('"name": "f"', out)
        # the single canonical format must be advertised, and prose-only must be discouraged
        self.assertIn("tool_calls", out)          # the fenced ```json {"tool_calls":[...]} block
        self.assertIn("```json", out)
        self.assertIn("does NOT count", out)      # prose-describes-an-action warning


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

    def test_empty_tool_calls_fence_alone_falls_back_to_raw_text(self):
        raw = '```json\n{"tool_calls":[]}\n```'
        calls, cleaned = srv.extract_tool_calls(raw)
        self.assertIsNone(calls)
        self.assertEqual(cleaned, raw)

    def test_canonical_wrapper_preserves_declared_and_undeclared_tool_names(self):
        text = ('```json\n{"tool_calls":[{"name":"declared","arguments":{}},'
                '{"name":"secret_tool","arguments":{}}]}\n```')
        calls, _ = srv.extract_tool_calls(text, {"declared"})
        self.assertEqual([c["function"]["name"] for c in calls],
                         ["declared", "secret_tool"])


class ToolCallResponseTests(unittest.TestCase):
    """Exercise actual JSON/SSE response formatting without a provider or HTTP server."""

    TOOLS = [{"type": "function", "function": {
        "name": "world_command", "parameters": {"type": "object"}}}]
    MESSAGES = [{"role": "user", "content": "Scale TowerB"}]

    def setUp(self):
        self.handler = srv.H.__new__(srv.H)
        self.handler.wfile = io.BytesIO()
        self.handler._sse_headers = mock.Mock()
        self.handler._send_json = mock.Mock()
        for attr, value in (
                ("CFG", argparse.Namespace(no_live_stream=True)),
                ("_route", mock.Mock(return_value=argparse.Namespace(
                    engine="codex", label="codex/test", id="test"))),
                ("_use_opencode_native", mock.Mock(return_value=False)),
                ("_response_metadata", mock.Mock(return_value={})),
                ("_record_completion", mock.Mock())):
            patcher = mock.patch.object(srv, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _response(self, raw, mode):
        self.handler.wfile = io.BytesIO()

        def complete(*args, **kwargs):
            if "on_delta" in kwargs:
                for i in range(0, len(raw), 3):
                    kwargs["on_delta"](raw[i:i + 3])
            return raw

        with mock.patch.object(srv.H, "_raw_completion", side_effect=complete) as run:
            if mode == "sync":
                self.handler._sync_response(self.MESSAGES, self.TOOLS)
                run.assert_called_once()
                code, payload = self.handler._send_json.call_args.args
                self.assertEqual(code, 200)
                choice = payload["choices"][0]
                return choice["message"], choice["finish_reason"]
            if mode == "replay":
                self.handler._stream_response(self.MESSAGES, self.TOOLS)
            else:
                self.handler._stream_response_live(self.MESSAGES, self.TOOLS)
            run.assert_called_once()

        wire = self.handler.wfile.getvalue().decode("utf-8")
        self.assertTrue(wire.endswith("data: [DONE]\n\n"))
        choices = [json.loads(line[6:])["choices"][0] for line in wire.splitlines()
                   if line.startswith("data: ") and line != "data: [DONE]"]
        message = {"content": "".join(c["delta"].get("content", "") for c in choices),
                   "tool_calls": [call for c in choices
                                  for call in c["delta"].get("tool_calls", [])]}
        return message, choices[-1]["finish_reason"]

    def test_undeclared_name_passes_through_without_remapping(self):
        args = {"action": "change", "targetName": "TowerB", "scale": 2.0}
        raw = '```json\n' + json.dumps({"tool_calls": [
            {"name": "change", "arguments": args}]}) + '\n```'
        for mode in ("sync", "replay", "live"):
            with self.subTest(mode=mode):
                message, reason = self._response(raw, mode)
                self.assertEqual(reason, "tool_calls")
                self.assertFalse(message["content"])
                self.assertEqual(len(message["tool_calls"]), 1)
                call = message["tool_calls"][0]
                self.assertEqual(call["type"], "function")
                self.assertTrue(call["id"].startswith("call_"))
                self.assertEqual(call["function"]["name"], "change")
                self.assertEqual(json.loads(call["function"]["arguments"]), args)

    def test_declared_names_unchanged_alongside_unknown_names(self):
        names = ["world_command", "change"]
        raw = '```json\n' + json.dumps({"tool_calls": [
            {"name": name, "arguments": {"scale": 2.0}} for name in names]}) + '\n```'
        for mode in ("sync", "replay", "live"):
            with self.subTest(mode=mode):
                message, reason = self._response(raw, mode)
                self.assertEqual(reason, "tool_calls")
                self.assertEqual([c["function"]["name"] for c in message["tool_calls"]], names)

    def test_empty_after_parse_returns_raw_text(self):
        for calls in ([], [{"arguments": {}}], [{"name": "", "arguments": {}}]):
            raw = '```json\n' + json.dumps({"tool_calls": calls}) + '\n```'
            for mode in ("sync", "replay", "live"):
                with self.subTest(mode=mode, calls=calls):
                    message, reason = self._response(raw, mode)
                    self.assertEqual(reason, "stop")
                    self.assertFalse(message.get("tool_calls"))
                    self.assertEqual(message["content"], raw)


class FuncCallSyntaxTests(unittest.TestCase):
    """The `name(arg=value, ...)` fallback: codex CLI tends to emit tool calls as literal
    function-call lines (the way it would WRITE a call) instead of the prompted JSON block.
    extract_func_calls / extract_tool_calls recover those, gated on the known tool names."""

    NAMES = {"world_command", "execute_lua"}

    def test_single_func_call_line_recovered(self):
        text = 'execute_lua(code="x = 1", label="setup")'
        calls, _ = srv.extract_tool_calls(text, self.NAMES)
        self.assertIsNotNone(calls)
        self.assertEqual(calls[0]["function"]["name"], "execute_lua")
        self.assertEqual(json.loads(calls[0]["function"]["arguments"]),
                         {"code": "x = 1", "label": "setup"})

    def test_multiple_calls_all_recovered_with_typed_args(self):
        text = (
            "Here is the castle:\n"
            'world_command(action="spawn", targetName="Tower_NW", prefabKey="cylinder", x=-6, y=1.55, z=-6)\n'
            'world_command(action="spawn", targetName="Flag", prefabKey="quad", color=[1,0,0], solid=true)\n'
            "Execution succeeded."
        )
        calls, cleaned = srv.extract_tool_calls(text, self.NAMES)
        self.assertEqual(len(calls), 2)
        a0 = json.loads(calls[0]["function"]["arguments"])
        self.assertEqual(a0["x"], -6)            # negative int
        self.assertEqual(a0["y"], 1.55)          # float
        self.assertEqual(a0["prefabKey"], "cylinder")
        a1 = json.loads(calls[1]["function"]["arguments"])
        self.assertEqual(a1["color"], [1, 0, 0])  # list value
        self.assertIs(a1["solid"], True)          # boolean
        # the call lines are stripped from the display text (surrounding prose kept)
        self.assertNotIn("world_command(", cleaned)
        self.assertIn("Execution succeeded.", cleaned)

    def test_string_value_containing_commas_and_braces_stays_intact(self):
        text = 'execute_lua(code="slot.price = {10,20,30}", label="prices")'
        calls, _ = srv.extract_tool_calls(text, self.NAMES)
        self.assertEqual(json.loads(calls[0]["function"]["arguments"])["code"],
                         "slot.price = {10,20,30}")

    def test_json_block_takes_precedence_over_func_syntax(self):
        text = ('```json\n{"tool_calls":[{"name":"execute_lua","arguments":{"code":"a"}}]}\n```\n'
                'world_command(action="spawn", x=1)')
        calls, _ = srv.extract_tool_calls(text, self.NAMES)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["function"]["name"], "execute_lua")

    def test_unknown_name_is_not_treated_as_a_call(self):
        calls, _ = srv.extract_tool_calls('helper(x=1) and foo(bar=2)', self.NAMES)
        self.assertIsNone(calls)

    def test_empty_parens_mention_is_prose_not_a_call(self):
        calls, cleaned = srv.extract_tool_calls("I will use world_command() next.", self.NAMES)
        self.assertIsNone(calls)
        self.assertEqual(cleaned, "I will use world_command() next.")

    def test_no_names_means_no_func_call_fallback(self):
        # Backward compatible: called without names (the old signature), the func-call fallback
        # is disabled and plain prose stays plain prose.
        calls, text = srv.extract_tool_calls('world_command(action="spawn", x=1)')
        self.assertIsNone(calls)
        self.assertEqual(text, 'world_command(action="spawn", x=1)')


class PositionalJsonArgTests(unittest.TestCase):
    """The `name({...})` spelling: a single positional JSON object instead of name=value pairs.
    This is gpt-5.5's DOMINANT spelling (literally how an OpenAI SDK call is written) -- before
    it was accepted, every such line was silently dropped as prose, which zeroed whole benchmark
    groups (G5 went 0/6 with tools=0 on runs whose transcripts contained perfectly good calls)."""

    NAMES = {"world_command", "execute_lua"}
    TOOLS = [
        {"type": "function", "function": {
            "name": "world_command",
            "parameters": {"type": "object", "properties": {
                "action": {"type": "string"}, "targetName": {"type": "string"},
                "prefabKey": {"type": "string"}, "x": {"type": "number"},
                "y": {"type": "number"}, "z": {"type": "number"}}}}},
        {"type": "function", "function": {
            "name": "execute_lua",
            "parameters": {"type": "object", "properties": {"code": {"type": "string"}}}}},
    ]

    def test_single_positional_json_object_recovered(self):
        text = 'world_command({"action":"spawn","prefabKey":"Cube","targetName":"Enemy1"})'
        calls, cleaned = srv.extract_tool_calls(text, self.NAMES, self.TOOLS)
        self.assertEqual(len(calls), 1)
        self.assertEqual(json.loads(calls[0]["function"]["arguments"]),
                         {"action": "spawn", "prefabKey": "Cube", "targetName": "Enemy1"})
        self.assertEqual(cleaned, "")

    def test_positional_json_with_typed_values(self):
        text = 'world_command({"action":"spawn","targetName":"Enemy1","prefabKey":"sphere","x":3,"y":0.5,"z":-3})'
        calls, _ = srv.extract_tool_calls(text, self.NAMES, self.TOOLS)
        a = json.loads(calls[0]["function"]["arguments"])
        self.assertEqual(a["x"], 3)
        self.assertEqual(a["y"], 0.5)
        self.assertEqual(a["z"], -3)

    def test_multiple_positional_json_call_lines_all_recovered(self):
        # Verbatim shape from a live gpt-5.5 G3 "Balanced enemy HP" run that scored tools=0.
        text = (
            'world_command({"action":"spawn","prefabKey":"Cube","targetName":"Enemy1"})\n'
            'world_command({"action":"spawn","prefabKey":"Cube","targetName":"Enemy2"})\n'
            'world_command({"action":"spawn","prefabKey":"Cube","targetName":"Enemy3"})\n'
            'world_command({"action":"spawn","prefabKey":"Cube","targetName":"Enemy4"})\n'
            'execute_lua({"code":"logic_define(\'enemy_hp\', function(name)\\n  local hp = '
            '{\\n    Enemy1 = 50,\\n    Enemy2 = 75,\\n    Enemy3 = 125,\\n    Enemy4 = 150\\n  }'
            '\\n  return hp[name]\\nend)"})'
        )
        calls, cleaned = srv.extract_tool_calls(text, self.NAMES, self.TOOLS)
        self.assertEqual(len(calls), 5)
        self.assertEqual([c["function"]["name"] for c in calls],
                         ["world_command"] * 4 + ["execute_lua"])
        self.assertIn("logic_define", json.loads(calls[4]["function"]["arguments"])["code"])
        self.assertEqual(cleaned, "")

    def test_positional_json_whose_string_contains_equals_and_parens(self):
        text = ('execute_lua({"code":"logic_define(\'win\', function(s) return s >= 100 end)"})')
        calls, _ = srv.extract_tool_calls(text, self.NAMES, self.TOOLS)
        self.assertEqual(json.loads(calls[0]["function"]["arguments"])["code"],
                         "logic_define('win', function(s) return s >= 100 end)")

    def test_positional_scalar_maps_onto_single_parameter_function(self):
        calls, _ = srv.extract_tool_calls('execute_lua("print(1)")', self.NAMES, self.TOOLS)
        self.assertEqual(json.loads(calls[0]["function"]["arguments"]), {"code": "print(1)"})

    def test_positional_scalar_not_mapped_for_multi_parameter_function(self):
        # world_command has many parameters -- a bare scalar has no safe mapping.
        calls, cleaned = srv.extract_tool_calls('world_command("spawn")', self.NAMES, self.TOOLS)
        self.assertIsNone(calls)
        self.assertEqual(cleaned, 'world_command("spawn")')

    def test_malformed_json_object_arg_is_not_double_wrapped_into_sole_param(self):
        # An invalid {...} blob is a FAILED shape-2 parse; stuffing it into "code" would
        # double-wrap it into nonsense Lua, so it must stay unparsed prose instead.
        text = 'execute_lua({"code": "line1\nline2"})'  # literal newline inside a JSON string
        calls, cleaned = srv.extract_tool_calls(text, self.NAMES, self.TOOLS)
        self.assertIsNone(calls)
        self.assertEqual(cleaned, text)

    def test_prose_around_positional_json_lines_is_kept_as_display_text(self):
        text = ('Spawning the gate now.\n'
                'world_command({"action":"spawn","targetName":"Gate","prefabKey":"cube"})\n'
                'Done.')
        calls, cleaned = srv.extract_tool_calls(text, self.NAMES, self.TOOLS)
        self.assertEqual(len(calls), 1)
        self.assertIn("Spawning the gate now.", cleaned)
        self.assertIn("Done.", cleaned)
        self.assertNotIn("world_command(", cleaned)

    def test_works_without_tools_schema_for_object_args(self):
        # The schema map is only needed for the scalar mapping; a positional JSON OBJECT must
        # parse with names alone (old call sites that never pass `tools`).
        text = 'world_command({"action":"spawn","targetName":"Key","prefabKey":"Cube"})'
        calls, _ = srv.extract_tool_calls(text, self.NAMES)
        self.assertEqual(len(calls), 1)


class EchoDedupTests(unittest.TestCase):
    """After a tool-result round-trip, models restate the calls they already made -- in exactly
    the `name({...})` spelling the rendered history shows them -- as part of a summary.
    Re-parsing those as NEW calls re-executed them every round (observed live: a 5-spawn
    scenario ballooned to 15 tool calls). Func-syntax calls that exactly repeat an
    already-executed call are echoes and must be dropped; genuinely new calls in the same
    message must survive."""

    NAMES = {"world_command"}

    @staticmethod
    def _history(*args_list):
        return [{"role": "assistant", "tool_calls": [
            {"id": "x", "type": "function",
             "function": {"name": "world_command", "arguments": json.dumps(a)}}
            for a in args_list]}]

    def test_exact_repeat_of_prior_call_is_dropped_as_echo(self):
        prior = srv.prior_call_keys(self._history({"action": "spawn", "targetName": "Player"}))
        text = 'world_command({"action":"spawn","targetName":"Player"})'
        calls, cleaned = srv.extract_tool_calls(text, self.NAMES, None, prior)
        self.assertIsNone(calls)
        self.assertEqual(cleaned, text)  # the echo stays in the display text as prose

    def test_key_order_does_not_defeat_the_echo_check(self):
        prior = srv.prior_call_keys(self._history({"action": "spawn", "targetName": "Player"}))
        text = 'world_command({"targetName":"Player","action":"spawn"})'
        calls, _ = srv.extract_tool_calls(text, self.NAMES, None, prior)
        self.assertIsNone(calls)

    def test_new_call_survives_next_to_an_echo(self):
        prior = srv.prior_call_keys(self._history({"action": "spawn", "targetName": "Player"}))
        text = ('world_command({"action":"spawn","targetName":"Player"})\n'
                'world_command({"action":"spawn","targetName":"Goal"})')
        calls, _ = srv.extract_tool_calls(text, self.NAMES, None, prior)
        self.assertEqual(len(calls), 1)
        self.assertEqual(json.loads(calls[0]["function"]["arguments"])["targetName"], "Goal")

    def test_no_prior_history_means_no_dedup(self):
        text = 'world_command({"action":"spawn","targetName":"Player"})'
        calls, _ = srv.extract_tool_calls(text, self.NAMES, None, set())
        self.assertEqual(len(calls), 1)

    def test_fenced_json_block_is_exempt_from_echo_dedup(self):
        # Format 1 is an explicit calling format, not a summary style -- a deliberate repeat
        # through it must still execute.
        prior = srv.prior_call_keys(self._history({"action": "spawn", "targetName": "Player"}))
        text = ('```json\n{"tool_calls":[{"name":"world_command",'
                '"arguments":{"action":"spawn","targetName":"Player"}}]}\n```')
        calls, _ = srv.extract_tool_calls(text, self.NAMES, None, prior)
        self.assertEqual(len(calls), 1)

    def test_duplicates_within_one_message_are_kept(self):
        # Only PRIOR turns dedupe; two identical calls in one fresh message could be a
        # deliberate batch and are not the echo pattern this guards against.
        text = ('world_command({"action":"spawn","targetName":"A"})\n'
                'world_command({"action":"spawn","targetName":"A"})')
        calls, _ = srv.extract_tool_calls(text, self.NAMES, None, set())
        self.assertEqual(len(calls), 2)


class PriorCallKeysTests(unittest.TestCase):
    def test_collects_names_and_canonical_args_from_assistant_messages(self):
        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "tool_calls": [
                {"id": "1", "type": "function",
                 "function": {"name": "f", "arguments": '{"b":2,"a":1}'}}]},
            {"role": "tool", "content": "ok"},
        ]
        keys = srv.prior_call_keys(msgs)
        self.assertEqual(keys, {("f", '{"a": 1, "b": 2}')})

    def test_empty_or_junk_messages_yield_empty_set(self):
        self.assertEqual(srv.prior_call_keys(None), set())
        self.assertEqual(srv.prior_call_keys([{"role": "user", "content": "x"}, "junk"]), set())


class ToolParamNamesTests(unittest.TestCase):
    def test_extracts_property_names_per_function(self):
        tools = [{"type": "function", "function": {
            "name": "f", "parameters": {"type": "object", "properties": {"a": {}, "b": {}}}}}]
        self.assertEqual(srv.tool_param_names(tools), {"f": ["a", "b"]})

    def test_bare_shape_and_missing_parameters_tolerated(self):
        tools = [{"name": "g"}]
        self.assertEqual(srv.tool_param_names(tools), {"g": []})

    def test_none_and_junk_entries_ignored(self):
        self.assertEqual(srv.tool_param_names(None), {})
        self.assertEqual(srv.tool_param_names(["junk", 42, {}]), {})


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

    def test_wipe_false_keeps_the_same_conversation_files(self):
        """A no-resume engine (gemini) takes the fresh-run path on EVERY turn of one
        conversation; wiping the scratch dir each time is both
        pointless and destabilising, so a continuation keeps the directory intact."""
        d = srv.fresh_session_dir()
        keep = os.path.join(d, "turn1.txt")
        with open(keep, "w", encoding="utf-8") as f:
            f.write("written on the previous turn of the SAME conversation")
        self.assertEqual(srv.fresh_session_dir(wipe=False), d)
        self.assertTrue(os.path.exists(keep))

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
        srv.SESSION["task_name"] = "some-task"
        srv.SESSION["last_activity"] = srv.time.time() - 3600
        self.assertTrue(srv.session_expired())

    def test_idle_seconds_reports_elapsed_time(self):
        srv.SESSION["task_name"] = "some-task"
        srv.SESSION["last_activity"] = srv.time.time() - 100
        idle = srv.session_idle_seconds()
        self.assertGreaterEqual(idle, 100)
        self.assertLess(idle, 105)

    def test_one_second_under_the_ttl_is_not_yet_expired(self):
        srv.SESSION["task_name"] = "some-task"
        srv.SESSION["last_activity"] = srv.time.time() - 1799
        self.assertFalse(srv.session_expired())


class ClaudeNativeFreshBoundaryTests(unittest.TestCase):
    def test_non_extension_recreates_unpinned_scratch_even_when_dir_was_already_set(self):
        saved = (srv.CFG, srv.SESSION, srv.claude_native_alive, srv.claude_native_kill,
                 srv.claude_native_send, srv.fresh_session_dir)
        class FakeCfg:
            engine, model, effort, dir = "claude", "sonnet", "low", ""
            timeout, retries, session_ttl = 60, 0, 1800
        wipes = []
        srv.CFG = FakeCfg()
        srv.SESSION = {"task_name": srv.CLAUDE_NATIVE_TASK,
                       "messages": [{"role": "user", "content": "old"}],
                       "dir": "/old/scratch", "last_activity": srv.time.time()}
        srv.claude_native_alive = lambda: True
        srv.claude_native_kill = lambda: None
        srv.claude_native_send = lambda *a, **k: "fresh answer"
        srv.fresh_session_dir = lambda wipe=True: wipes.append(wipe) or "/fresh/scratch"
        try:
            srv._claude_native_raw([{"role": "user", "content": "different"}], None,
                                   "none", None, None, None, False)
            self.assertEqual(wipes, [True])
            self.assertEqual(srv.SESSION["dir"], "/fresh/scratch")
        finally:
            (srv.CFG, srv.SESSION, srv.claude_native_alive, srv.claude_native_kill,
             srv.claude_native_send, srv.fresh_session_dir) = saved

class ChatOnlyEnvTests(unittest.TestCase):
    """run_agent/reply_agent must launch agent.sh with AGENT_CHAT_ONLY=1 in the subprocess env --
    that's what tells providers/{codex,claude}/provider.sh to strip MCP/shell/file access (see
    _chatonly_env). Regression guard against a future refactor silently dropping `env=`."""

    def test_chatonly_env_sets_the_flag_without_mutating_the_real_process_env(self):
        env = srv._chatonly_env()
        self.assertEqual(env.get("AGENT_CHAT_ONLY"), "1")
        self.assertNotIn("AGENT_CHAT_ONLY", os.environ)  # _chatonly_env must copy, not mutate

    def test_run_agent_passes_chatonly_env_to_subprocess(self):
        captured = {}

        def fake_run(args, timeout, env):
            captured.update({"timeout": timeout, "env": env})

        real_run, real_read_log = srv._run_agent_process, srv.read_log
        srv._run_agent_process = fake_run
        srv.read_log = lambda name: ""
        try:
            srv.run_agent("codex", "spark", "medium", "/tmp/x", "hi", "t", 60)
        finally:
            srv._run_agent_process, srv.read_log = real_run, real_read_log
        self.assertEqual(captured.get("env", {}).get("AGENT_CHAT_ONLY"), "1")

    def test_reply_agent_passes_chatonly_env_to_subprocess(self):
        captured = {}

        def fake_run(args, timeout, env):
            captured.update({"timeout": timeout, "env": env})

        real_run, real_read_log, real_read_meta = srv._run_agent_process, srv.read_log, srv.read_meta
        srv._run_agent_process = fake_run
        srv.read_log = lambda name: ""  # log never grows -> reply_agent returns None, that's fine
        srv.read_meta = lambda name: {"state": "done"}
        try:
            srv.reply_agent("codex", "spark", "medium", "/tmp/x", "t", "hi", 60)
        finally:
            srv._run_agent_process, srv.read_log, srv.read_meta = real_run, real_read_log, real_read_meta
        self.assertEqual(captured.get("env", {}).get("AGENT_CHAT_ONLY"), "1")

    def test_inherited_watchdog_cannot_exceed_outer_timeout(self):
        class FakeCfg:
            timeout = 40
        old_cfg = srv.CFG
        old_value = os.environ.get("AGENT_TIMEOUT_SEC")
        srv.CFG = FakeCfg()
        os.environ["AGENT_TIMEOUT_SEC"] = "999999"
        try:
            env = srv._chatonly_env()
            self.assertEqual(env["AGENT_TIMEOUT_SEC"], "35")
            self.assertEqual(env["AGENT_RETRIES"], "0")
        finally:
            srv.CFG = old_cfg
            if old_value is None:
                os.environ.pop("AGENT_TIMEOUT_SEC", None)
            else:
                os.environ["AGENT_TIMEOUT_SEC"] = old_value


class ReplyAgentStaleGuardTests(unittest.TestCase):
    """reply_agent must NOT return the previous answer when a resume appended nothing to the log
    (e.g. `agent.sh reply` died before writing a new block). It returns None instead, so _run
    falls back to a fresh run rather than silently echoing the stale prior answer."""

    def setUp(self):
        self._real_run = srv._run_agent_process
        self._real_read_log = srv.read_log
        self._real_read_meta = srv.read_meta
        srv._run_agent_process = lambda *a, **k: None  # never actually shell out
        srv.read_meta = lambda name: {"state": "done"}  # default: a healthy finished task

    def tearDown(self):
        srv._run_agent_process = self._real_run
        srv.read_log = self._real_read_log
        srv.read_meta = self._real_read_meta

    def _grow(self):
        """Make read_log return `before` on the 1st call and `after` (grown) on later calls."""
        state = {"n": 0}
        before = "========== [run] ...\n---------- output ----------\nOLD ANSWER\n"
        after = before + "========== [reply] ...\n---------- output ----------\nNEW ANSWER\n"
        def fake_read_log(name):
            state["n"] += 1
            return before if state["n"] == 1 else after
        srv.read_log = fake_read_log

    def test_returns_none_when_log_did_not_grow(self):
        # read_log returns the same content before and after -> nothing was appended.
        srv.read_log = lambda name: "========== [run] ...\n---------- output ----------\nOLD ANSWER\n"
        self.assertIsNone(srv.reply_agent("codex", "spark", "medium", "/tmp/x", "t", "hi", 60))

    def test_returns_new_answer_when_log_grew_and_state_done(self):
        self._grow()
        srv.read_meta = lambda name: {"state": "done"}
        self.assertEqual(srv.reply_agent("codex", "spark", "medium", "/tmp/x", "t", "hi", 60),
                         "NEW ANSWER\n")

    def test_returns_none_when_log_grew_but_state_error(self):
        self._grow()
        srv.read_meta = lambda name: {"state": "error"}
        self.assertIsNone(srv.reply_agent("codex", "spark", "medium", "/tmp/x", "t", "hi", 60))

    def test_accepts_waiting_state(self):
        self._grow()
        srv.read_meta = lambda name: {"state": "waiting"}
        self.assertEqual(srv.reply_agent("codex", "spark", "medium", "/tmp/x", "t", "hi", 60),
                         "NEW ANSWER\n")


class ChatOnlyResumePromptTests(unittest.TestCase):
    """Resuming an API conversation must not ask the provider to write task checkpoints."""

    def _assert_no_progress(self, live):
        before = "---------- output ----------\nOLD\n"
        after = before + "---------- output ----------\nNEW\n"
        with mock.patch.object(srv, "read_log", side_effect=[before, after]), \
                mock.patch.object(srv, "read_meta", return_value={"state": "done"}), \
                mock.patch.object(srv, "_log_size", return_value=len(before)), \
                mock.patch.object(srv, "_run_agent_process") as run, \
                mock.patch.object(srv, "_popen_process_tree") as launch, \
                mock.patch.object(srv, "_tail_task_log"), \
                mock.patch.object(srv, "_release_process_tree"):
            if live:
                result = srv.reply_agent_live(
                    "claude", "sonnet", "medium", "/tmp/api", "api-task", "continue", 60,
                    lambda delta: None)
                arguments = launch.call_args.args[0]
            else:
                result = srv.reply_agent(
                    "codex", "spark", "medium", "/tmp/api", "api-task", "continue", 60)
                arguments = run.call_args.args[0]
        self.assertEqual(result, "NEW\n")
        self.assertIn("--no-progress", arguments,
                      "The public agent.sh resume boundary must disable checkpoint instructions.")

    def test_buffered_api_resume_disables_checkpoint_instructions(self):
        self._assert_no_progress(live=False)

    def test_live_api_resume_disables_checkpoint_instructions(self):
        self._assert_no_progress(live=True)


class ProcessTreeTimeoutTests(unittest.TestCase):
    @staticmethod
    def _pid_alive(pid):
        if os.name == "nt":
            out = subprocess.check_output(
                ["tasklist", "/FI", "PID eq %d" % pid, "/FO", "CSV", "/NH"],
                text=True, errors="replace")
            return str(pid) in out and "No tasks" not in out
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    def test_outer_timeout_kills_and_reaps_child_sleeper_tree(self):
        d = tempfile.mkdtemp()
        pid_file = os.path.join(d, "child.pid")
        script = ("import subprocess,sys,time; "
                  "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
                  "open(sys.argv[1],'w').write(str(p.pid)); time.sleep(60)")
        try:
            # Windows process startup under a concurrent Unity benchmark can exceed two seconds;
            # ten seconds still distinguishes cleanup from the 60-second sleeper.
            timeout = 10.0 if os.name == "nt" else 0.5
            srv._run_agent_process([sys.executable, "-c", script, pid_file], timeout,
                                   dict(os.environ))
            self.assertTrue(os.path.exists(pid_file))
            with open(pid_file, encoding="ascii") as f:
                child_pid = int(f.read())
            deadline = srv.time.time() + 5
            while self._pid_alive(child_pid) and srv.time.time() < deadline:
                srv.time.sleep(0.05)
            self.assertFalse(self._pid_alive(child_pid))
        finally:
            import shutil
            shutil.rmtree(d, ignore_errors=True)

    def test_timeout_kills_orphan_grandchild_after_direct_wrapper_exits(self):
        """The direct child can exit while its grandchild keeps inherited pipes open.  The
        timeout must target the launch-time group/job, not require the dead wrapper PID."""
        d = tempfile.mkdtemp()
        pid_file = os.path.join(d, "orphan.pid")
        script = (
            "import subprocess,sys; "
            "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'], "
            "stdout=sys.stdout, stderr=sys.stderr); "
            "open(sys.argv[1],'w').write(str(p.pid))")
        orphan_pid = None
        try:
            timeout = 10.0 if os.name == "nt" else 0.5
            srv._run_agent_process([sys.executable, "-c", script, pid_file], timeout,
                                   dict(os.environ))
            self.assertTrue(os.path.exists(pid_file))
            with open(pid_file, encoding="ascii") as f:
                orphan_pid = int(f.read())
            deadline = srv.time.time() + 5
            while self._pid_alive(orphan_pid) and srv.time.time() < deadline:
                srv.time.sleep(0.05)
            self.assertFalse(self._pid_alive(orphan_pid))
        finally:
            if orphan_pid and self._pid_alive(orphan_pid):
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(orphan_pid), "/F"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   check=False)
                else:
                    try:
                        os.kill(orphan_pid, srv.signal.SIGKILL)
                    except OSError:
                        pass
            import shutil
            shutil.rmtree(d, ignore_errors=True)


class ProcessTreeLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.saved_registry = srv._PROCESS_TREES
        self.saved_shutdown = srv._PROCESS_TREES_SHUTTING_DOWN
        self.saved_sigterm_active = getattr(srv, "_SIGTERM_HANDLER_ACTIVE", False)
        srv._PROCESS_TREES = {}
        srv._PROCESS_TREES_SHUTTING_DOWN = False
        srv._SIGTERM_HANDLER_ACTIVE = False

    def tearDown(self):
        srv._PROCESS_TREES = self.saved_registry
        srv._PROCESS_TREES_SHUTTING_DOWN = self.saved_shutdown
        srv._SIGTERM_HANDLER_ACTIVE = self.saved_sigterm_active

    def test_release_claims_job_handle_once(self):
        closes = []

        class Kernel:
            def CloseHandle(self, handle):
                closes.append(handle)

        class Proc:
            _neoxider_tree_state = "active"
            _neoxider_job = 123
            _neoxider_kernel32 = Kernel()

        proc = Proc()
        srv._PROCESS_TREES[id(proc)] = proc
        real_terminate = srv._terminate_claimed_process_tree
        srv._terminate_claimed_process_tree = srv._close_process_tree_container
        try:
            srv._release_process_tree(proc)
            srv._release_process_tree(proc)
            self.assertEqual(closes, [123])
            self.assertEqual(proc._neoxider_tree_state, "closed")
            self.assertNotIn(id(proc), srv._PROCESS_TREES)
        finally:
            srv._terminate_claimed_process_tree = real_terminate

    def test_release_racing_global_shutdown_still_closes_container_once(self):
        closes = []
        barrier = threading.Barrier(3)

        class Kernel:
            def CloseHandle(self, handle):
                closes.append(handle)

        class Proc:
            _neoxider_tree_state = "active"
            _neoxider_job = 456
            _neoxider_kernel32 = Kernel()

        proc = Proc()
        srv._PROCESS_TREES[id(proc)] = proc
        real_terminate = srv._terminate_claimed_process_tree
        srv._terminate_claimed_process_tree = srv._close_process_tree_container

        def release():
            barrier.wait()
            srv._release_process_tree(proc)

        def shutdown():
            barrier.wait()
            srv._shutdown_process_trees()

        threads = [threading.Thread(target=release), threading.Thread(target=shutdown)]
        try:
            for thread in threads:
                thread.start()
            barrier.wait()
            for thread in threads:
                thread.join(timeout=5)
            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual(closes, [456])
        finally:
            srv._terminate_claimed_process_tree = real_terminate

    def test_shutdown_atomically_drains_registry_once_and_rejects_late_launch(self):
        class Proc:
            _neoxider_tree_state = "active"

        first, second = Proc(), Proc()
        srv._PROCESS_TREES = {id(first): first, id(second): second}
        terminated = []
        real_terminate = srv._terminate_claimed_process_tree
        srv._terminate_claimed_process_tree = lambda proc: terminated.append(proc)
        try:
            srv._shutdown_process_trees()
            srv._shutdown_process_trees()
            self.assertCountEqual(terminated, [first, second])
            self.assertEqual(srv._PROCESS_TREES, {})
            with self.assertRaises(RuntimeError):
                srv._popen_process_tree([sys.executable, "-c", "pass"])
        finally:
            srv._terminate_claimed_process_tree = real_terminate

    def test_sigterm_handler_cleans_before_exiting(self):
        calls = []
        real_shutdown = srv._shutdown_process_trees
        srv._shutdown_process_trees = lambda: calls.append("cleanup")
        try:
            with self.assertRaises(SystemExit) as raised:
                srv._handle_sigterm(srv.signal.SIGTERM, None)
            self.assertEqual(calls, ["cleanup"])
            self.assertEqual(raised.exception.code, 128 + srv.signal.SIGTERM)
        finally:
            srv._shutdown_process_trees = real_shutdown

    def test_reentrant_sigterm_during_first_termination_drains_both_trees_once(self):
        class Proc:
            _neoxider_tree_state = "active"

        first, second = Proc(), Proc()
        srv._PROCESS_TREES = {id(first): first, id(second): second}
        terminated = []
        real_terminate = srv._terminate_claimed_process_tree

        def terminate(proc):
            terminated.append(proc)
            if proc is first:
                srv._handle_sigterm(signal.SIGTERM, None)

        srv._terminate_claimed_process_tree = terminate
        try:
            with self.assertRaises(SystemExit) as raised:
                srv._handle_sigterm(signal.SIGTERM, None)
            self.assertEqual(raised.exception.code, 128 + signal.SIGTERM)
            self.assertEqual(terminated, [first, second])
            self.assertEqual(srv._PROCESS_TREES, {})
        finally:
            srv._terminate_claimed_process_tree = real_terminate

    @unittest.skipIf(os.name == "nt", "POSIX process-group regression")
    def test_success_release_kills_background_child_before_forgetting_group(self):
        d = tempfile.mkdtemp()
        pid_file = os.path.join(d, "background.pid")
        wrapper = (
            "import subprocess,sys; "
            "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'], "
            "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
            "open(sys.argv[1],'w').write(str(p.pid))")
        child_pid = None
        try:
            srv._run_agent_process([sys.executable, "-c", wrapper, pid_file], 10,
                                   dict(os.environ))
            with open(pid_file, encoding="ascii") as stream:
                child_pid = int(stream.read())
            deadline = time.time() + 5
            while ProcessTreeTimeoutTests._pid_alive(child_pid) and time.time() < deadline:
                stat_path = "/proc/%d/stat" % child_pid
                if os.path.exists(stat_path):
                    with open(stat_path, encoding="ascii") as stream:
                        if stream.read().split()[2] == "Z":
                            break
                time.sleep(0.05)
            else:
                self.assertFalse(ProcessTreeTimeoutTests._pid_alive(child_pid))
            self.assertEqual(srv._PROCESS_TREES, {})
        finally:
            if child_pid and ProcessTreeTimeoutTests._pid_alive(child_pid):
                try:
                    os.kill(child_pid, signal.SIGKILL)
                except OSError:
                    pass
            import shutil
            shutil.rmtree(d, ignore_errors=True)

    def test_global_cleanup_kills_registered_provider_tree(self):
        d = tempfile.mkdtemp()
        pid_file = os.path.join(d, "child.pid")
        script = (
            "import subprocess,sys,time; "
            "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
            "open(sys.argv[1],'w').write(str(p.pid)); time.sleep(60)")
        child_pid = None
        try:
            srv._popen_process_tree(
                [sys.executable, "-c", script, pid_file],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            deadline = time.time() + 5
            while not os.path.exists(pid_file) and time.time() < deadline:
                time.sleep(0.05)
            self.assertTrue(os.path.exists(pid_file))
            with open(pid_file, encoding="ascii") as stream:
                child_pid = int(stream.read())
            srv._shutdown_process_trees()
            deadline = time.time() + 5
            while ProcessTreeTimeoutTests._pid_alive(child_pid) and time.time() < deadline:
                time.sleep(0.05)
            self.assertFalse(ProcessTreeTimeoutTests._pid_alive(child_pid))
        finally:
            if child_pid and ProcessTreeTimeoutTests._pid_alive(child_pid):
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(child_pid), "/F"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   check=False)
                else:
                    try:
                        os.kill(child_pid, signal.SIGKILL)
                    except OSError:
                        pass
            import shutil
            shutil.rmtree(d, ignore_errors=True)

    @unittest.skipIf(os.name == "nt", "POSIX process-group regression")
    def test_sigterm_kills_orphan_in_registered_provider_group(self):
        d = tempfile.mkdtemp()
        pid_file = os.path.join(d, "orphan.pid")
        module_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        wrapper = (
            "import subprocess,sys; "
            "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
            "open(sys.argv[1],'w').write(str(p.pid))")
        helper = (
            "import os,signal,subprocess,sys,time; "
            "sys.path.insert(0,%s); import openai_server as srv; "
            "signal.signal(signal.SIGTERM,srv._handle_sigterm); "
            "srv._popen_process_tree([sys.executable,'-c',%s,%s],"
            "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
            "\nwhile not os.path.exists(%s): time.sleep(.01)\n"
            "print('READY',flush=True)\n"
            "while True: time.sleep(1)" % (
                repr(module_dir), repr(wrapper), repr(pid_file), repr(pid_file)))
        bridge = subprocess.Popen(
            [sys.executable, "-c", helper], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True)
        orphan_pid = None
        try:
            self.assertEqual(bridge.stdout.readline().strip(), "READY")
            with open(pid_file, encoding="ascii") as stream:
                orphan_pid = int(stream.read())
            os.kill(bridge.pid, signal.SIGTERM)
            bridge.wait(timeout=10)
            deadline = time.time() + 5
            while ProcessTreeTimeoutTests._pid_alive(orphan_pid) and time.time() < deadline:
                # A killed process may briefly remain as a zombie; it is no longer an orphaned
                # provider workload and init will reap it.
                stat_path = "/proc/%d/stat" % orphan_pid
                if os.path.exists(stat_path):
                    with open(stat_path, encoding="ascii") as stream:
                        if stream.read().split()[2] == "Z":
                            break
                time.sleep(0.05)
            else:
                self.assertFalse(ProcessTreeTimeoutTests._pid_alive(orphan_pid))
        finally:
            if bridge.poll() is None:
                bridge.kill()
                bridge.wait(timeout=5)
            if orphan_pid and ProcessTreeTimeoutTests._pid_alive(orphan_pid):
                try:
                    os.kill(orphan_pid, signal.SIGKILL)
                except OSError:
                    pass
            import shutil
            shutil.rmtree(d, ignore_errors=True)


class PersistentProcessReplacementTests(unittest.TestCase):
    def test_dead_opencode_wrapper_is_disposed_before_state_clear_and_replacement(self):
        events = []

        class OldProc:
            def poll(self):
                return 7

        class NewProc:
            def poll(self):
                return None

        class Response:
            status = 200
            def __enter__(self):
                return self
            def __exit__(self, *_args):
                return False

        old = OldProc()
        saved = (srv._OC, srv._free_port, srv._popen_process_tree,
                 srv._terminate_process_tree, srv._urlreq.urlopen)
        srv._OC = {"base": "http://stale", "proc": old, "lock": threading.Lock()}
        srv._free_port = lambda: 18181

        def terminate(proc):
            self.assertIs(srv._OC["proc"], old)
            events.append(("terminate", proc))

        def popen(*_args, **_kwargs):
            self.assertIsNone(srv._OC["proc"])
            events.append(("spawn", None))
            return NewProc()

        srv._terminate_process_tree = terminate
        srv._popen_process_tree = popen
        srv._urlreq.urlopen = lambda *_args, **_kwargs: Response()
        try:
            self.assertEqual(srv._ensure_opencode_server(), "http://127.0.0.1:18181")
            self.assertEqual([event[0] for event in events], ["terminate", "spawn"])
            self.assertIsInstance(srv._OC["proc"], NewProc)
        finally:
            (srv._OC, srv._free_port, srv._popen_process_tree,
             srv._terminate_process_tree, srv._urlreq.urlopen) = saved

    def test_dead_claude_wrapper_is_disposed_before_state_clear_and_replacement(self):
        events = []

        class OldProc:
            def poll(self):
                return 9

        class NewProc:
            stdout = ()
            def poll(self):
                return None

        old = OldProc()
        saved = (srv._CL, srv._popen_process_tree, srv._terminate_process_tree,
                 srv._claude_native_cmd)
        srv._CL = {"proc": old, "queue": object(), "lock": threading.RLock()}

        def terminate(proc):
            self.assertIs(srv._CL["proc"], old)
            events.append(("terminate", proc))

        def popen(*_args, **_kwargs):
            self.assertIsNone(srv._CL["proc"])
            events.append(("spawn", None))
            return NewProc()

        srv._terminate_process_tree = terminate
        srv._popen_process_tree = popen
        srv._claude_native_cmd = lambda: "claude"
        try:
            srv._claude_native_ensure()
            self.assertEqual([event[0] for event in events], ["terminate", "spawn"])
            self.assertIsInstance(srv._CL["proc"], NewProc)
        finally:
            (srv._CL, srv._popen_process_tree, srv._terminate_process_tree,
             srv._claude_native_cmd) = saved


class AdversarialParsingTests(unittest.TestCase):
    """Hostile inputs surfaced by an adversarial audit -- every case here reproduced a REAL
    parsing defect before its fix (silent call loss, phantom double-execution, display-text
    corruption, valid-fence swallowing). Each test name states the defect it locks."""

    TOOLS = [
        {"type": "function", "function": {"name": "world_command",
         "parameters": {"type": "object", "properties": {"action": {}, "text": {}, "x": {}}}}},
        {"type": "function", "function": {"name": "execute_lua",
         "parameters": {"type": "object", "properties": {"code": {}}}}},
    ]
    NAMES = {"world_command", "execute_lua"}

    def test_string_arg_ending_in_backslash_still_parses(self):
        # A JSON-escaped Windows path ending in "\" -- the old single-char lookbehind read the
        # closing quote as escaped, the parens never balanced, and the WHOLE call was dropped.
        calls, _ = srv.extract_tool_calls(
            'world_command(action="say", text="C:\\\\Games\\\\")', self.NAMES, self.TOOLS)
        self.assertEqual(len(calls), 1)
        args = json.loads(calls[0]["function"]["arguments"])
        self.assertEqual(args["action"], "say")

    def test_backslash_tail_in_positional_json_spelling(self):
        calls, _ = srv.extract_tool_calls(
            'world_command({"action": "say", "text": "C:\\\\Games\\\\"})', self.NAMES, self.TOOLS)
        self.assertEqual(len(calls), 1)
        self.assertEqual(json.loads(calls[0]["function"]["arguments"])["text"], "C:\\Games\\")

    def test_known_tool_name_inside_string_argument_is_not_a_second_call(self):
        # Lua code that MENTIONS world_command must not be extracted as a phantom second call
        # (double-execution corrupted benchmark exact-count scenarios).
        calls, _ = srv.extract_tool_calls(
            'execute_lua(code="world_command(action=1)")', self.NAMES, self.TOOLS)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["function"]["name"], "execute_lua")

    def test_malformed_fence_does_not_swallow_a_following_valid_one(self):
        text = ('```json\n{"tool_calls":[{"name":"world_command","arguments":{"x":1}}]} junk\n```\n'
                'prose\n'
                '```json\n{"tool_calls":[{"name":"world_command","arguments":{"x":2}}]}\n```')
        calls, _ = srv.extract_tool_calls(text, self.NAMES, self.TOOLS)
        self.assertIsNotNone(calls)
        self.assertEqual(json.loads(calls[0]["function"]["arguments"]), {"x": 2})

    def test_multiple_fences_with_leading_whitespace_leave_clean_display_text(self):
        text = ('  \nbefore\n'
                '```json\n{"tool_calls":[]}\n```\n'
                'middle prose\n'
                '```json\n{"tool_calls":[]}\n```\n'
                'after')
        _calls, cleaned = srv.extract_tool_calls(text, self.NAMES, self.TOOLS)
        self.assertNotIn("```", cleaned)
        self.assertIn("middle prose", cleaned)
        self.assertIn("before", cleaned)
        self.assertIn("after", cleaned)

    def test_call_syntax_inside_a_tagged_code_fence_is_example_not_a_call(self):
        text = ('Here is how you could do it:\n'
                '```lua\nworld_command(action="spawn", x=1)\n```\n'
                'But I am not calling it.')
        calls, cleaned = srv.extract_tool_calls(text, self.NAMES, self.TOOLS)
        self.assertIsNone(calls)
        self.assertIn("not calling it", cleaned)

    def test_call_lines_in_an_untagged_fence_still_execute(self):
        # Models often wrap their GENUINE Format-2 call lines in a bare ``` fence -- only
        # language-tagged fences are treated as examples.
        text = '```\nworld_command(action="spawn", x=1)\n```'
        calls, _ = srv.extract_tool_calls(text, self.NAMES, self.TOOLS)
        self.assertEqual(len(calls), 1)

    def test_uppercase_json_fence_tag_is_recognized(self):
        text = '```JSON\n{"tool_calls":[{"name":"world_command","arguments":{"x":1}}]}\n```'
        calls, _ = srv.extract_tool_calls(text, self.NAMES, self.TOOLS)
        self.assertEqual(len(calls), 1)

    def test_bare_json_with_trailing_prose_is_not_lost(self):
        text = '{"tool_calls":[{"name":"world_command","arguments":{"x":1}}]}\nDone.'
        calls, cleaned = srv.extract_tool_calls(text, self.NAMES, self.TOOLS)
        self.assertEqual(len(calls), 1)
        self.assertEqual(cleaned, "Done.")

    def test_multiline_format2_call_and_two_calls_on_one_line_still_work(self):
        # These worked before the audit -- pinned so hardening never regresses them.
        calls, _ = srv.extract_tool_calls(
            'execute_lua({"code": "line1\\nline2"})', self.NAMES, self.TOOLS)
        self.assertEqual(len(calls), 1)
        calls2, _ = srv.extract_tool_calls(
            'world_command(action="a", x=1) world_command(action="b", x=2)',
            self.NAMES, self.TOOLS)
        self.assertEqual(len(calls2), 2)


class BareObjectLinesTests(unittest.TestCase):
    """Nameless call spelling observed live from gpt-5.3-codex-spark in the single-tool G6
    scenario: the entire message is bare JSON argument objects, one per line, NO function name
    (the model 'saved' the redundant tool name -- ~400 spawns scored tools=0 before this).
    Deterministic gate: every non-blank line must be a JSON object and the keys must fit exactly
    ONE tool -- prose or ambiguity rejects the whole message."""

    TOOLS = [
        {"type": "function", "function": {"name": "world_command",
         "parameters": {"type": "object", "properties": {
             "action": {}, "targetName": {}, "prefabKey": {}, "x": {}, "y": {}, "z": {},
             "scaleX": {}, "scaleY": {}, "scaleZ": {}, "fx": {}, "fy": {}, "fz": {}}}}},
        {"type": "function", "function": {"name": "execute_lua",
         "parameters": {"type": "object", "properties": {"code": {}}}}},
    ]
    NAMES = {"world_command", "execute_lua"}

    def test_verbatim_live_failure_shape_recovers_every_line(self):
        # Two lines exactly as the failing spark run wrote them (of ~400).
        text = ('{"action":"spawn","targetName":"ground","prefabKey":"cube","x":0,"y":0,"z":0,'
                '"scaleX":18,"scaleY":0.2,"scaleZ":18,"fx":0,"fy":0,"fz":0}\n'
                '{"action":"spawn","targetName":"tower_nw","prefabKey":"cylinder","x":-6,"y":1.5,'
                '"z":-6,"scaleX":1.45,"scaleY":1.55,"scaleZ":1.45,"fx":0,"fy":0,"fz":0}')
        calls, cleaned = srv.extract_tool_calls(text, self.NAMES, self.TOOLS)
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(c["function"]["name"] == "world_command" for c in calls))
        self.assertEqual(json.loads(calls[0]["function"]["arguments"])["targetName"], "ground")
        self.assertEqual(cleaned, "")

    def test_fence_wrapped_object_lines_still_recover(self):
        text = '```\n{"action":"spawn","targetName":"a","x":1}\n{"action":"spawn","targetName":"b","x":2}\n```'
        calls, _ = srv.extract_tool_calls(text, self.NAMES, self.TOOLS)
        self.assertEqual(len(calls), 2)

    def test_any_prose_line_rejects_the_whole_message(self):
        text = 'Here are the spawns:\n{"action":"spawn","targetName":"a","x":1}'
        calls, _ = srv.extract_tool_calls(text, self.NAMES, self.TOOLS)
        self.assertIsNone(calls)

    def test_keys_fitting_more_than_one_tool_are_ambiguous_and_rejected(self):
        tools = [
            {"type": "function", "function": {"name": "a", "parameters": {"properties": {"x": {}}}}},
            {"type": "function", "function": {"name": "b", "parameters": {"properties": {"x": {}, "y": {}}}}},
        ]
        calls, _ = srv.extract_tool_calls('{"x": 1}', {"a", "b"}, tools)
        self.assertIsNone(calls)

    def test_keys_outside_every_schema_reject(self):
        calls, _ = srv.extract_tool_calls(
            '{"totally_unknown_key": 1}', self.NAMES, self.TOOLS)
        self.assertIsNone(calls)

    def test_named_format2_still_wins_over_bare_interpretation(self):
        text = 'world_command({"action": "spawn", "x": 1})'
        calls, _ = srv.extract_tool_calls(text, self.NAMES, self.TOOLS)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["function"]["name"], "world_command")


class SingleCallFenceTests(unittest.TestCase):
    """One fenced JSON block PER CALL, each an OpenAI-shaped tool-call object -- observed live
    from Sonnet 5 ({\"type\":\"function\",\"function\":{...}} per fence, no tool_calls wrapper);
    every world_command scenario scored tools=0 before this. Order across fences must be
    preserved; unknown names must NOT be eaten as calls."""

    NAMES = {"world_command", "execute_lua"}

    def test_verbatim_sonnet_shape_two_fences_two_calls_in_order(self):
        text = ('I will write the calls for the application.\n'
                '```json\n{"type": "function", "function": {"name": "world_command", '
                '"arguments": {"action": "spawn", "targetName": "Player", "x": 0}}}\n```\n'
                '```json\n{"type": "function", "function": {"name": "world_command", '
                '"arguments": {"action": "spawn", "targetName": "Gate", "x": 1}}}\n```')
        calls, cleaned = srv.extract_tool_calls(text, self.NAMES, None)
        self.assertEqual(len(calls), 2)
        self.assertEqual(json.loads(calls[0]["function"]["arguments"])["targetName"], "Player")
        self.assertEqual(json.loads(calls[1]["function"]["arguments"])["targetName"], "Gate")
        self.assertNotIn("```", cleaned)

    def test_flat_name_arguments_fence_also_accepted(self):
        text = '```json\n{"name": "execute_lua", "arguments": {"code": "print(1)"}}\n```'
        calls, _ = srv.extract_tool_calls(text, self.NAMES, None)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["function"]["name"], "execute_lua")

    def test_unknown_function_name_fence_is_left_as_content(self):
        text = '```json\n{"type": "function", "function": {"name": "not_a_tool", "arguments": {}}}\n```'
        calls, cleaned = srv.extract_tool_calls(text, self.NAMES, None)
        self.assertIsNone(calls)
        self.assertIn("not_a_tool", cleaned)

    def test_plain_json_answer_without_call_shape_is_untouched(self):
        text = '```json\n{"answer": 42, "reason": "because"}\n```'
        calls, cleaned = srv.extract_tool_calls(text, self.NAMES, None)
        self.assertIsNone(calls)
        self.assertIn("42", cleaned)

    def test_no_declared_tools_means_no_fence_is_ever_a_call(self):
        # Audit finding: with an empty `names` set (request declared no tools), a call-shaped
        # fence must stay content — nothing to call means nothing to extract.
        text = '```json\n{"type": "function", "function": {"name": "world_command", "arguments": {"x": 1}}}\n```'
        calls, cleaned = srv.extract_tool_calls(text, set(), None)
        self.assertIsNone(calls)
        self.assertIn("world_command", cleaned)

    def test_flat_shape_with_extra_keys_is_data_not_a_call(self):
        # Audit finding: a JSON data answer that HAPPENS to have name+arguments among other
        # fields must survive; only the exact two-key flat shape reads as a call.
        text = '```json\n{"name": "execute_lua", "arguments": {"code": "x"}, "confidence": 0.9}\n```'
        calls, cleaned = srv.extract_tool_calls(text, self.NAMES, None)
        self.assertIsNone(calls)
        self.assertIn("confidence", cleaned)

    def test_tool_calls_fence_with_backtick_in_string_still_parses(self):
        # Audit finding: the strict backtick-free fence body rejected valid JSON whose string
        # values contain backticks (lua/markdown code) — the fallback pass must recover it.
        text = ('```json\n{"tool_calls":[{"name":"execute_lua","arguments":'
                '{"code":"print(`hi`)"}}]}\n```')
        calls, _ = srv.extract_tool_calls(text, self.NAMES, None)
        self.assertEqual(len(calls), 1)
        self.assertIn("`hi`", json.loads(calls[0]["function"]["arguments"])["code"])

    def test_jsonl_fence_with_multiple_flat_calls_parses_all_in_order(self):
        # Verbatim Opus 4.8 shape: several flat call objects, one per line, inside ONE json
        # fence ("JSONL, one call per line") -- invalid as whole-body JSON, so the whole castle
        # used to be dropped.
        text = ('Building now.\n```json\n'
                '{"name":"world_command","arguments":{"action":"spawn","targetName":"moat","x":0}}\n'
                '{"name":"world_command","arguments":{"action":"spawn","targetName":"tower_NW","x":-6}}\n'
                '{"name":"world_command","arguments":{"action":"set_color","targetName":"moat","stringValue":"#3b6ea5"}}\n'
                '```')
        calls, cleaned = srv.extract_tool_calls(text, self.NAMES, None)
        self.assertEqual(len(calls), 3)
        self.assertEqual(json.loads(calls[0]["function"]["arguments"])["targetName"], "moat")
        self.assertEqual(json.loads(calls[2]["function"]["arguments"])["action"], "set_color")
        self.assertNotIn("```", cleaned)

    def test_json_array_of_calls_in_one_fence_parses_all(self):
        # Verbatim Opus 4.8 shape: a fenced ```json block whose body is a JSON ARRAY of call
        # objects [ {"name":...,"arguments":...}, ... ]. Not a dict, so it used to fall through
        # and score tools=0 across G1/G3/G4/G6/G7.
        text = ('I will build the arena.\n```json\n[\n'
                '  {"name": "world_command", "arguments": {"action": "spawn", "targetName": "Player", "x": 0}},\n'
                '  {"name": "world_command", "arguments": {"action": "spawn", "targetName": "Enemy1", "x": 5}},\n'
                '  {"name": "world_command", "arguments": {"action": "spawn", "targetName": "Enemy2", "x": -5}}\n'
                ']\n```')
        calls, cleaned = srv.extract_tool_calls(text, self.NAMES, None)
        self.assertEqual(len(calls), 3)
        self.assertEqual(json.loads(calls[0]["function"]["arguments"])["targetName"], "Player")
        self.assertEqual(json.loads(calls[2]["function"]["arguments"])["targetName"], "Enemy2")
        self.assertNotIn("```", cleaned)

    def test_json_array_of_plain_data_is_not_eaten_as_calls(self):
        text = '```json\n[{"id": 1, "label": "a"}, {"id": 2, "label": "b"}]\n```'
        calls, cleaned = srv.extract_tool_calls(text, self.NAMES, None)
        self.assertIsNone(calls)
        self.assertIn("label", cleaned)

    def test_jsonl_fence_with_a_non_call_line_is_left_as_content(self):
        text = ('```json\n'
                '{"name":"world_command","arguments":{"x":1}}\n'
                '{"note":"this line is data, not a call"}\n'
                '```')
        calls, cleaned = srv.extract_tool_calls(text, self.NAMES, None)
        self.assertIsNone(calls)
        self.assertIn("data, not a call", cleaned)

    def test_function_call_tagged_fence_is_a_request_not_an_example(self):
        # Verbatim Opus 4.8 shape: a Format-2 line inside a ```function_call fence -- the
        # tagged-fence example masking must NOT eat call-intent tags.
        text = ('I will spawn the objects.\n```function_call\n'
                'world_command(action="spawn", prefabKey="Cube", targetName="Key")\n```')
        calls, _ = srv.extract_tool_calls(text, self.NAMES, self.TOOLS if hasattr(self, "TOOLS") else None)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["function"]["name"], "world_command")

    def test_lua_tagged_fence_is_still_an_example(self):
        text = ('Here is how you could build a wall with the tool if you wanted to do it yourself:\n'
                '```lua\nworld_command(action="spawn", x=1)\n```\n'
                'But I am only explaining the approach, not actually calling it right now.')
        calls, _ = srv.extract_tool_calls(text, self.NAMES, None)
        self.assertIsNone(calls)

    def test_python_fence_that_IS_the_whole_answer_executes(self):
        # Verbatim spark shape: the entire message is a ```python fence whose body is the real
        # multiline world_command(...) calls, with no explaining prose around it. The tagged-fence
        # example masking must NOT eat these (spawn_arena scored tools=0 before this).
        text = ('```python\n'
                'world_command({\n  "action": "spawn",\n  "targetName": "Player",\n  "prefabKey": "Cube"\n})\n'
                'world_command({\n  "action": "spawn",\n  "targetName": "Goal",\n  "prefabKey": "Cube"\n})\n'
                '```')
        calls, _ = srv.extract_tool_calls(text, self.NAMES, None)
        self.assertEqual(len(calls), 2)
        self.assertEqual(json.loads(calls[0]["function"]["arguments"])["targetName"], "Player")

    def test_explicit_tool_calls_block_still_wins_over_singles(self):
        text = ('```json\n{"type": "function", "function": {"name": "world_command", "arguments": {"x": 1}}}\n```\n'
                '```json\n{"tool_calls":[{"name":"world_command","arguments":{"x": 9}}]}\n```')
        calls, _ = srv.extract_tool_calls(text, self.NAMES, None)
        self.assertEqual(len(calls), 1)
        self.assertEqual(json.loads(calls[0]["function"]["arguments"]), {"x": 9})


class DeliberateRepeatContractTests(unittest.TestCase):
    """The documented contract for re-running an identical call (echo-dedup's one false
    positive): a Format-2 line that exactly repeats an executed call is ALWAYS summary prose;
    a deliberate repeat must use Format 1 (fenced JSON), which is exempt from dedup. The prompt
    states this escape hatch explicitly -- both halves locked here."""

    PRIOR = [{"role": "assistant", "tool_calls": [
        {"function": {"name": "execute_lua", "arguments": '{"code": "score = score + 1"}'}}]}]

    def test_format2_identical_repeat_is_dropped_as_echo(self):
        prior = srv.prior_call_keys(self.PRIOR)
        calls, _ = srv.extract_tool_calls(
            'execute_lua({"code": "score = score + 1"})',
            {"execute_lua"}, None, prior)
        self.assertIsNone(calls)

    def test_format1_identical_repeat_is_executed(self):
        prior = srv.prior_call_keys(self.PRIOR)
        text = '```json\n{"tool_calls":[{"name":"execute_lua","arguments":{"code":"score = score + 1"}}]}\n```'
        calls, _ = srv.extract_tool_calls(text, {"execute_lua"}, None, prior)
        self.assertEqual(len(calls), 1)

    def test_prompt_prescribes_the_single_canonical_tool_calls_format(self):
        # The prompt now steers every model to ONE format: a fenced ```json {"tool_calls":[...]}
        # block. Fenced JSON is dedup-exempt, so a deliberate identical repeat still executes.
        self.assertIn("tool_calls", srv.TOOLCALL_INSTRUCTIONS)
        self.assertIn("```json", srv.TOOLCALL_INSTRUCTIONS)


class LimitBannerTests(unittest.TestCase):
    """A CLI answer that IS the provider's usage-limit banner must become an OpenAI-style 429
    (ProviderLimitError), never a normal 200 completion — a live Sonnet 5 benchmark run scored
    every scenario ~0 because 'You've hit your session limit · resets 7:40am' was returned as
    the model's answer. Narrow gate: short text that matches the banner wording; an answer that
    merely DISCUSSES rate limits stays a normal completion."""

    def test_claude_session_limit_banner_detected(self):
        self.assertTrue(srv.looks_like_limit_banner(
            "You've hit your session limit · resets 7:40am (Asia/Yekaterinburg)"))

    def test_usage_and_rate_limit_wordings_detected(self):
        self.assertTrue(srv.looks_like_limit_banner("Usage limit reached. Try again later."))
        self.assertTrue(srv.looks_like_limit_banner("Rate limit exceeded"))

    def test_long_prose_mentioning_limits_is_not_a_banner(self):
        prose = ("Rate limiting is a technique servers use. " * 12 +
                 "If you hit your session limit, the API returns 429.")
        self.assertFalse(srv.looks_like_limit_banner(prose))

    def test_normal_answer_is_not_a_banner(self):
        self.assertFalse(srv.looks_like_limit_banner("The castle has four towers."))
        self.assertFalse(srv.looks_like_limit_banner(""))

    def test_short_answer_quoting_a_limit_phrase_mid_sentence_is_not_a_banner(self):
        # Audit finding: the gate must be anchored at the START — a short answer that merely
        # RELAYS a limit phrase is a normal completion, not a 429.
        self.assertFalse(srv.looks_like_limit_banner("The API returned: Rate limit exceeded."))
        self.assertFalse(srv.looks_like_limit_banner(
            "Set retries because sometimes you see 'usage limit reached' errors."))


class RoughTokensTests(unittest.TestCase):
    """usage counts are ESTIMATES (~4 chars/token) -- non-zero for any real text, 0 only for
    empty/None, so cost panels see something useful instead of the old hardcoded 0/0/0."""

    def test_empty_and_none_are_zero(self):
        self.assertEqual(srv._rough_tokens(""), 0)
        self.assertEqual(srv._rough_tokens(None), 0)

    def test_four_chars_is_one_token(self):
        self.assertEqual(srv._rough_tokens("abcd"), 1)

    def test_rounds_up(self):
        self.assertEqual(srv._rough_tokens("abcde"), 2)


class PromptAntiEchoTests(unittest.TestCase):
    """After a tool result the prompt must ask for a NEW block only for new actions (the
    prompt-side half of the echo defense; the parser-side half is prior_call_keys dedup, see
    EchoDedupTests). Guard against the guidance being lost in a future prompt rewrite."""

    def test_instructions_scope_new_blocks_to_new_actions(self):
        self.assertIn("TOOL RESULT", srv.TOOLCALL_INSTRUCTIONS)
        self.assertIn("NEW actions", srv.TOOLCALL_INSTRUCTIONS)


class RunRetryAndFallbackTests(unittest.TestCase):
    """_run must behave like a real API endpoint about empty answers:
      - a fresh run whose CLI came back empty (or state=error) is retried up to CFG.retries times;
      - a resume that 'succeeded' but produced an EMPTY answer falls back to a fresh run;
      - the returned usage is a non-zero estimate flagged neoxider_estimated.
    H._run never touches `self`, so it is called unbound with a dummy object -- no HTTP server
    needed."""

    def setUp(self):
        self._saved = (srv.run_agent, srv.reply_agent, srv.read_meta, srv.SESSION, srv.CFG,
                       srv.PROVIDERS, srv.time.sleep)

        class FakeCfg:
            engine = "fakeeng"
            model = "m"
            effort = ""
            dir = "/pinned/project"  # pinned -> fresh_session_dir returns it, no rmtree
            port = 8999
            timeout = 60
            session_ttl = 1800
            retries = 1

        srv.CFG = FakeCfg()
        srv.PROVIDERS = {"fakeeng": {"supports_resume": True}}
        srv.SESSION = {"task_name": None, "messages": [], "dir": None, "last_activity": 0.0}
        srv.read_meta = lambda name: {"state": "done"}
        srv.time.sleep = lambda s: None  # retries must not actually wait in tests

    def tearDown(self):
        (srv.run_agent, srv.reply_agent, srv.read_meta, srv.SESSION, srv.CFG,
         srv.PROVIDERS, srv.time.sleep) = self._saved

    def _call(self, messages, tools=None):
        return srv.H._run(object(), messages, tools)

    def test_empty_fresh_run_is_retried_once_and_recovers(self):
        attempts = []

        def fake_run(engine, model, effort, workdir, prompt, name, timeout):
            attempts.append(name)
            return "" if len(attempts) == 1 else "REAL ANSWER"

        srv.run_agent = fake_run
        text, calls, usage = self._call([{"role": "user", "content": "hi"}])
        self.assertEqual(text, "REAL ANSWER")
        self.assertIsNone(calls)
        self.assertEqual(len(attempts), 2)

    def test_error_state_run_is_retried_even_with_nonempty_text(self):
        attempts = []
        states = iter(["error", "done"])
        srv.read_meta = lambda name: {"state": next(states, "done")}

        def fake_run(engine, model, effort, workdir, prompt, name, timeout):
            attempts.append(name)
            return "provider error banner" if len(attempts) == 1 else "REAL ANSWER"

        srv.run_agent = fake_run
        text, _calls, _usage = self._call([{"role": "user", "content": "hi"}])
        self.assertEqual(text, "REAL ANSWER")
        self.assertEqual(len(attempts), 2)

    def test_retries_exhausted_fails_instead_of_returning_empty_success(self):
        attempts = []

        def fake_run(engine, model, effort, workdir, prompt, name, timeout):
            attempts.append(name)
            return ""

        srv.run_agent = fake_run
        with self.assertRaises(srv.CompletionFailed):
            self._call([{"role": "user", "content": "hi"}])
        self.assertEqual(len(attempts), 2)  # retries=1 -> 2 total attempts, then give up

    def test_empty_resume_answer_falls_back_to_fresh_run(self):
        import time
        first = [{"role": "user", "content": "hi"}]
        srv.SESSION = {"task_name": "prior-task", "messages": list(first),
                       "dir": "/pinned/project", "last_activity": time.time()}
        srv.reply_agent = lambda *a, **k: ""  # resume "succeeds" but says nothing
        srv.run_agent = lambda *a, **k: "FRESH ANSWER"
        text, _calls, _usage = self._call(first + [{"role": "user", "content": "and?"}])
        self.assertEqual(text, "FRESH ANSWER")

    def test_no_tool_call_but_tool_named_in_prose_is_retried_and_recovers(self):
        attempts = []
        fence = '```json\n{"tool_calls":[{"name":"world_command","arguments":{"action":"spawn"}}]}\n```'

        def fake_run(engine, model, effort, workdir, prompt, name, timeout):
            attempts.append(name)
            # 1st turn: prose that NAMES the tool but emits no block; 2nd (nudged) turn: the real block.
            return "I'll call world_command to spawn the player." if len(attempts) == 1 else fence

        srv.run_agent = fake_run
        tools = [{"type": "function", "function": {
            "name": "world_command", "parameters": {"type": "object", "properties": {}}}}]
        _text, calls, _usage = self._call([{"role": "user", "content": "spawn a player"}], tools)
        self.assertIsNotNone(calls)
        self.assertEqual(calls[0]["function"]["name"], "world_command")
        self.assertEqual(len(attempts), 2)  # one recovery retry

    def test_prose_answer_not_naming_a_tool_is_not_retried(self):
        attempts = []
        srv.run_agent = lambda *a, **k: attempts.append("x") or "The capital of France is Paris."
        tools = [{"type": "function", "function": {
            "name": "world_command", "parameters": {"type": "object", "properties": {}}}}]
        text, calls, _usage = self._call([{"role": "user", "content": "capital?"}], tools)
        self.assertIsNone(calls)
        self.assertEqual(text, "The capital of France is Paris.")
        self.assertEqual(len(attempts), 1)  # no retry: a legitimate prose answer never named a tool

    def test_usage_is_a_flagged_nonzero_estimate(self):
        srv.run_agent = lambda *a, **k: "four token answer here"
        _text, _calls, usage = self._call([{"role": "user", "content": "hi"}])
        self.assertTrue(usage["neoxider_estimated"])
        self.assertGreater(usage["prompt_tokens"], 0)
        self.assertGreater(usage["completion_tokens"], 0)
        self.assertEqual(usage["total_tokens"],
                         usage["prompt_tokens"] + usage["completion_tokens"])


class ToolChoiceContractTests(unittest.TestCase):
    TOOLS = [
        {"type": "function", "function": {"name": "alpha", "parameters": {"type": "object"}}},
        {"type": "function", "function": {"name": "beta", "parameters": {"type": "object"}}},
    ]
    MESSAGES = [{"role": "user", "content": "act"}]

    def setUp(self):
        self.saved = (srv.run_agent, srv.read_meta, srv.SESSION, srv.CFG,
                      srv.PROVIDERS, srv.time.sleep, srv.claude_native_kill)

        class FakeCfg:
            engine, model, effort, dir = "fakeeng", "m", "", "/pinned/project"
            port, timeout, session_ttl, retries = 8996, 60, 1800, 0

        srv.CFG = FakeCfg()
        srv.PROVIDERS = {"fakeeng": {"supports_resume": False}}
        srv.SESSION = {"task_name": None, "messages": [], "dir": None, "last_activity": 0.0}
        srv.read_meta = lambda name: {"state": "done"}
        srv.time.sleep = lambda seconds: None
        srv.claude_native_kill = lambda: None
        self.prompts = []

    def tearDown(self):
        (srv.run_agent, srv.read_meta, srv.SESSION, srv.CFG,
         srv.PROVIDERS, srv.time.sleep, srv.claude_native_kill) = self.saved

    def _answers(self, *answers):
        values = iter(answers)

        def fake_run(engine, model, effort, workdir, prompt, name, timeout):
            self.prompts.append(prompt)
            return next(values)

        srv.run_agent = fake_run

    @staticmethod
    def _call(name):
        return ('```json\n{"tool_calls":[{"name":"%s","arguments":{"x":1}}]}\n```'
                % name)

    def test_none_suppresses_calls_and_retries_to_plain_text(self):
        self._answers(self._call("alpha"), "plain answer")
        text, calls, _usage = srv.H._run(
            object(), self.MESSAGES, self.TOOLS, "none")
        self.assertEqual(text, "plain answer")
        self.assertIsNone(calls)
        self.assertIn("Tool use is disabled", self.prompts[0])
        self.assertNotIn('"name": "alpha"', self.prompts[0])

    def test_required_fails_closed_when_provider_never_calls(self):
        self._answers("prose only", "still prose")
        with self.assertRaises(srv.CompletionFailed):
            srv.H._run(object(), self.MESSAGES, self.TOOLS, "required")
        self.assertIsNone(srv.SESSION["task_name"])
        self.assertEqual(srv.SESSION["messages"], self.MESSAGES)

    def test_specific_function_rejects_other_function_and_returns_only_selected(self):
        choice = {"type": "function", "function": {"name": "beta"}}
        self._answers(self._call("alpha"), self._call("beta"))
        _text, calls, _usage = srv.H._run(
            object(), self.MESSAGES, self.TOOLS, choice)
        self.assertEqual([c["function"]["name"] for c in calls], ["beta"])
        self.assertIn("MUST call", self.prompts[0])
        self.assertIn('"name": "beta"', self.prompts[0])
        self.assertNotIn('"name": "alpha"', self.prompts[0])

    def test_validation_propagates_normalized_choice_and_rejects_impossible_required(self):
        body = {"messages": self.MESSAGES, "tools": self.TOOLS, "tool_choice": "required"}
        _messages, _tools, choice, stream = srv.validate_chat_request(body)
        self.assertEqual(choice, "required")
        self.assertFalse(stream)
        with self.assertRaises(srv.RequestValidationError):
            srv.validate_chat_request({"messages": self.MESSAGES, "tool_choice": "required"})


class ToolCallNudgeSessionTests(unittest.TestCase):
    """Synthetic recovery turns must not become client-visible session history."""

    TOOLS = [{"type": "function", "function": {
        "name": "world_command", "parameters": {"type": "object", "properties": {}}}}]
    FENCE = '```json\n{"tool_calls":[{"name":"world_command","arguments":{"action":"spawn"}}]}\n```'
    FIRST_MSGS = [{"role": "user", "content": "spawn a player"}]

    @classmethod
    def _followup_msgs(cls, calls):
        return list(cls.FIRST_MSGS) + [
            {"role": "assistant", "content": None, "tool_calls": calls},
            {"role": "tool", "tool_call_id": calls[0]["id"], "content": "spawned"},
            {"role": "user", "content": "now add a gate"},
        ]

    def setUp(self):
        self._saved = (srv.run_agent, srv.reply_agent, srv.read_meta, srv.SESSION, srv.CFG,
                       srv.PROVIDERS, srv.time.sleep)

        class FakeCfg:
            engine = "fakeeng"
            model = "m"
            effort = ""
            dir = "/pinned/project"
            port = 8998
            timeout = 60
            session_ttl = 1800
            retries = 1

        srv.CFG = FakeCfg()
        srv.PROVIDERS = {"fakeeng": {"supports_resume": True}}
        srv.SESSION = {"task_name": None, "messages": [], "dir": None, "last_activity": 0.0}
        srv.read_meta = lambda name: {"state": "done"}
        srv.time.sleep = lambda s: None
        self.run_prompts, self.run_names, self.reply_answers = [], [], []

    def tearDown(self):
        (srv.run_agent, srv.reply_agent, srv.read_meta, srv.SESSION, srv.CFG,
         srv.PROVIDERS, srv.time.sleep) = self._saved

    def _install_fakes(self, reply_answers_in_order, run_answers=None):
        def fake_run(engine, model, effort, workdir, prompt, name, timeout):
            self.run_prompts.append(prompt)
            self.run_names.append(name)
            answers = run_answers or ["I'll call world_command to spawn the player."]
            return answers[min(len(self.run_names) - 1, len(answers) - 1)]

        def fake_reply(engine, model, effort, workdir, name, answer, timeout):
            self.reply_answers.append(answer)
            return reply_answers_in_order[len(self.reply_answers) - 1]

        srv.run_agent, srv.reply_agent = fake_run, fake_reply

    def test_synthetic_nudge_never_lands_in_session_messages(self):
        self._install_fakes([self.FENCE])
        text, calls, _usage = srv.H._run(object(), list(self.FIRST_MSGS), self.TOOLS)
        self.assertEqual(calls[0]["function"]["name"], "world_command")
        # the nudge DID reach the underlying CLI session (as the resume prompt) ...
        self.assertIn("did not emit a tool call", self.reply_answers[0])
        # ... and SESSION records the exact client-visible assistant response, never the nudge.
        self.assertEqual(srv.SESSION["messages"][-1]["tool_calls"], calls)
        self.assertNotIn("did not emit a tool call", json.dumps(srv.SESSION["messages"]))
        self.assertIsNone(srv.SESSION["task_name"])

    def test_follow_up_turn_after_nudge_retry_starts_fresh(self):
        self._install_fakes([self.FENCE], ["I'll call world_command to spawn the player.", "Gate added."])
        _text, first_calls, _usage = srv.H._run(object(), list(self.FIRST_MSGS), self.TOOLS)
        followup = self._followup_msgs(first_calls)
        text, calls, _usage = srv.H._run(object(), followup, self.TOOLS)
        self.assertIsNone(calls)
        self.assertEqual(text, "Gate added.")
        self.assertEqual(srv.SESSION["messages"], followup + [
            {"role": "assistant", "content": "Gate added."}])
        self.assertEqual(len(self.run_names), 2)
        self.assertEqual(len(self.reply_answers), 1)
        self.assertEqual(srv.SESSION["dir"], "/pinned/project")

    def test_unsuccessful_nudge_invalidates_the_diverged_cli_session(self):
        self._install_fakes(["Still no tool call."])
        text, calls, _usage = srv.H._run(object(), list(self.FIRST_MSGS), self.TOOLS)
        self.assertIsNone(calls)
        self.assertIn("world_command", text)
        self.assertIsNone(srv.SESSION["task_name"])
        self.assertEqual(srv.SESSION["messages"], self.FIRST_MSGS + [
            {"role": "assistant", "content": "I'll call world_command to spawn the player."}])


class CanonicalSessionContinuityTests(unittest.TestCase):
    def setUp(self):
        self.saved = (srv.run_agent, srv.reply_agent, srv.read_meta, srv.SESSION, srv.CFG,
                      srv.PROVIDERS)
        class FakeCfg:
            engine, model, effort, dir = "fakeeng", "m", "", "/pinned/project"
            port, timeout, session_ttl, retries = 8997, 60, 1800, 0
        srv.CFG = FakeCfg()
        srv.PROVIDERS = {"fakeeng": {"supports_resume": True}}
        srv.SESSION = {"task_name": None, "messages": [], "dir": None, "last_activity": 0.0}
        srv.read_meta = lambda name: {"state": "done"}
        self.runs, self.replies = [], []
        srv.run_agent = lambda *a: self.runs.append(a[4]) or "CANONICAL"
        srv.reply_agent = lambda *a: self.replies.append(a[5]) or "CONTINUED"

    def tearDown(self):
        (srv.run_agent, srv.reply_agent, srv.read_meta, srv.SESSION, srv.CFG,
         srv.PROVIDERS) = self.saved

    def test_exact_returned_assistant_turn_resumes_without_resending_it(self):
        first = [{"role": "user", "content": "one"}]
        srv.H._run(object(), first, None)
        self.assertEqual(srv.SESSION["messages"], first + [
            {"role": "assistant", "content": "CANONICAL"}])
        next_messages = list(srv.SESSION["messages"]) + [{"role": "user", "content": "two"}]
        srv.H._run(object(), next_messages, None)
        self.assertEqual(len(self.runs), 1)
        self.assertEqual(len(self.replies), 1)
        self.assertIn("[USER]\ntwo", self.replies[0])
        self.assertNotIn("CANONICAL", self.replies[0])

    def test_tampered_assistant_turn_starts_fresh(self):
        first = [{"role": "user", "content": "one"}]
        srv.H._run(object(), first, None)
        tampered = first + [{"role": "assistant", "content": "EDITED"},
                            {"role": "user", "content": "two"}]
        srv.H._run(object(), tampered, None)
        self.assertEqual(len(self.runs), 2)
        self.assertEqual(self.replies, [])
        self.assertIn("EDITED", self.runs[-1])


class ParametersKeyAliasTests(unittest.TestCase):
    """Haiku 4.5 live spelling #13: "parameters" instead of "arguments" inside the function
    object -- _to_calls silently took EMPTY args and every call failed schema validation."""

    NAMES = {"world_command"}

    def test_openai_shape_with_parameters_key(self):
        text = ('```json\n[{"type": "function", "function": {"name": "world_command",'
                ' "parameters": {"action": "spawn", "targetName": "Player"}}}]\n```')
        calls, _ = srv.extract_tool_calls(text, self.NAMES)
        self.assertEqual(len(calls), 1)
        self.assertEqual(json.loads(calls[0]["function"]["arguments"])["targetName"], "Player")

    def test_flat_name_parameters_exact_keys(self):
        shaped = srv._call_shaped(
            {"name": "world_command", "parameters": {"action": "spawn"}}, self.NAMES)
        self.assertEqual(shaped, {"name": "world_command", "arguments": {"action": "spawn"}})

    def test_tool_calls_wrapper_with_parameters_key(self):
        text = ('```json\n{"tool_calls": [{"name": "world_command",'
                ' "parameters": {"action": "spawn", "targetName": "Tree"}}]}\n```')
        calls, _ = srv.extract_tool_calls(text, self.NAMES)
        self.assertEqual(len(calls), 1)
        self.assertEqual(json.loads(calls[0]["function"]["arguments"])["targetName"], "Tree")

    def test_extra_keys_still_content(self):
        self.assertIsNone(srv._call_shaped(
            {"name": "world_command", "parameters": {}, "note": "x"}, self.NAMES))


class WrapperKeyAliasTests(unittest.TestCase):
    """Fable 5 live spelling #12: the canonical fence but with {"actions": [...]} instead of
    {"tool_calls": [...]}, elements shaped as OpenAI call objects with dict arguments -- the
    whole Dungeon-win-logic scenario scored tools=0 before this."""

    NAMES = {"world_command", "execute_lua"}
    FENCE = ('```json\n{\n  "actions": [\n'
             '    {"type": "function", "function": {"name": "world_command",'
             ' "arguments": {"action": "spawn", "prefabKey": "Cube", "targetName": "Player"}}},\n'
             '    {"type": "function", "function": {"name": "execute_lua",'
             ' "arguments": {"code": "logic_define(1)"}}}\n'
             '  ]\n}\n```')

    def test_actions_wrapper_extracts_calls(self):
        calls, cleaned = srv.extract_tool_calls(self.FENCE, self.NAMES)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["function"]["name"], "world_command")
        self.assertEqual(calls[1]["function"]["name"], "execute_lua")
        self.assertEqual(json.loads(calls[1]["function"]["arguments"])["code"], "logic_define(1)")
        self.assertEqual(cleaned, "")

    def test_actions_wrapper_with_non_call_elements_stays_content(self):
        text = '```json\n{"actions": [{"step": 1, "note": "walk"}, {"step": 2}]}\n```'
        calls, cleaned = srv.extract_tool_calls(text, self.NAMES)
        self.assertIsNone(calls)
        self.assertIn("walk", cleaned)

    def test_actions_wrapper_with_extra_keys_stays_content(self):
        text = ('```json\n{"actions": [{"type": "function", "function": {"name": "world_command",'
                ' "arguments": {}}}], "note": "plan"}\n```')
        calls, _ = srv.extract_tool_calls(text, self.NAMES)
        self.assertIsNone(calls, "A dict with extra keys besides the alias wrapper is a data answer.")

    def test_commands_wrapper_with_bare_args_extracts_calls(self):
        # Haiku 4.5 live G6 spelling: {"commands": [...]} wrapper whose elements are BARE
        # argument objects (no function name) -- the whole castle scored tools=0 before.
        tools = [{"type": "function", "function": {
            "name": "world_command",
            "parameters": {"type": "object", "properties": {
                "action": {"type": "string"}, "targetName": {"type": "string"},
                "prefabKey": {"type": "string"}, "stringValue": {"type": "string"},
                "x": {"type": "number"}, "y": {"type": "number"}, "z": {"type": "number"},
                "scaleX": {"type": "number"}, "scaleY": {"type": "number"},
                "scaleZ": {"type": "number"}}, "required": ["action"]}}}]
        text = ('```json\n{"commands": [\n'
                '  {"action": "spawn", "targetName": "ground", "prefabKey": "cube",'
                ' "x": 0, "y": 0, "z": 0, "scaleX": 18, "scaleY": 0.2, "scaleZ": 18},\n'
                '  {"action": "set_color", "targetName": "ground", "stringValue": "#8b7355"}\n'
                ']}\n```')
        calls, cleaned = srv.extract_tool_calls(text, {"world_command"}, tools=tools)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["function"]["name"], "world_command")
        self.assertEqual(json.loads(calls[0]["function"]["arguments"])["targetName"], "ground")
        self.assertEqual(json.loads(calls[1]["function"]["arguments"])["action"], "set_color")
        self.assertEqual(cleaned, "")

    def test_actions_wrapper_streams_incrementally(self):
        h = _EmitterHarness(names=self.NAMES)
        cut = self.FENCE.index('  ]')
        h.feed_chunked(self.FENCE[:cut])
        self.assertEqual(len(h.calls), 2, "Both calls must stream before the array closes.")
        h.emitter.feed(self.FENCE[cut:])
        h.emitter.finish()
        self.assertEqual(len(h.calls), 2, "No double emit after reconciliation.")


class UnfencedJsonCallTests(unittest.TestCase):
    """GPT-5.5 live spellings: real calls as PLAIN TEXT with no fence at all -- an unfenced
    {"requests":[{"tool":...}]} wrapper and unfenced bare arrays. Whole scenarios scored
    tools=0 while the calls sat in plain sight as text."""

    NAMES = {"world_command"}
    TOOLS = [{"type": "function", "function": {
        "name": "world_command",
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string"}, "targetName": {"type": "string"},
            "prefabKey": {"type": "string"}, "x": {"type": "number"},
            "y": {"type": "number"}, "z": {"type": "number"}}, "required": ["action"]}}}]

    def test_unfenced_requests_wrapper_with_tool_name_key(self):
        text = ('{"requests":[{"tool":"world_command","arguments":{"action":"spawn",'
                '"prefabKey":"Cube","targetName":"Player","x":-2,"y":0,"z":0}},'
                '{"tool":"world_command","arguments":{"action":"spawn","prefabKey":"Cube",'
                '"targetName":"Goal","x":2,"y":0,"z":0}}]}')
        calls, cleaned = srv.extract_tool_calls(text, self.NAMES, tools=self.TOOLS)
        self.assertEqual(len(calls), 2)
        self.assertEqual(json.loads(calls[0]["function"]["arguments"])["targetName"], "Player")
        self.assertEqual(cleaned, "")

    def test_unfenced_bare_array_of_call_objects(self):
        text = ('[{"name":"world_command","arguments":{"action":"spawn","targetName":"A"}},'
                '{"name":"world_command","arguments":{"action":"spawn","targetName":"B"}}]')
        calls, cleaned = srv.extract_tool_calls(text, self.NAMES, tools=self.TOOLS)
        self.assertEqual(len(calls), 2)
        self.assertEqual(cleaned, "")

    def test_unfenced_bare_array_of_arg_objects(self):
        text = ('[\n  {"action": "spawn", "targetName": "Player", "prefabKey": "capsule",'
                ' "x": 0, "y": 1, "z": 0},\n'
                '  {"action": "spawn", "targetName": "Goal", "prefabKey": "cube",'
                ' "x": 3, "y": 0.5, "z": 2}\n]')
        calls, _ = srv.extract_tool_calls(text, self.NAMES, tools=self.TOOLS)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["function"]["name"], "world_command")
        self.assertEqual(json.loads(calls[1]["function"]["arguments"])["x"], 3)

    def test_unfenced_array_with_trailing_prose_keeps_prose(self):
        text = '[{"name":"world_command","arguments":{"action":"spawn","targetName":"A"}}]\nDone.'
        calls, cleaned = srv.extract_tool_calls(text, self.NAMES, tools=self.TOOLS)
        self.assertEqual(len(calls), 1)
        self.assertEqual(cleaned.strip(), "Done.")

    def test_unfenced_data_array_stays_content(self):
        calls, cleaned = srv.extract_tool_calls('["red", "green", "blue"]',
                                                self.NAMES, tools=self.TOOLS)
        self.assertIsNone(calls)
        self.assertIn("green", cleaned)

    def test_unfenced_data_object_stays_content(self):
        calls, cleaned = srv.extract_tool_calls('{"answer": 42, "reason": "math"}',
                                                self.NAMES, tools=self.TOOLS)
        self.assertIsNone(calls)
        self.assertIn("42", cleaned)


class SalvageArrayTests(unittest.TestCase):
    """Spark live G6: an unfenced 75-call array with ONE malformed element (a duplicated
    '"function",' line) -- the whole castle scored tools=0. Salvage keeps the good calls."""

    NAMES = {"world_command"}
    TOOLS = [{"type": "function", "function": {
        "name": "world_command",
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string"}, "targetName": {"type": "string"}},
            "required": ["action"]}}}]

    GOOD = ('{"type":"function","function":{"name":"world_command",'
            '"parameters":{"action":"spawn","targetName":"%s"}}}')
    BAD = '{"type":"function","function","function":{"name":"world_command","parameters":{"action":"spawn","targetName":"broken"}}}'

    def test_malformed_element_skipped_good_calls_survive(self):
        text = "[\n" + self.GOOD % "a" + ",\n" + self.BAD + ",\n" + self.GOOD % "b" + "\n]"
        calls, cleaned = srv.extract_tool_calls(text, self.NAMES, tools=self.TOOLS)
        self.assertEqual(len(calls), 2)
        self.assertEqual(json.loads(calls[0]["function"]["arguments"])["targetName"], "a")
        self.assertEqual(json.loads(calls[1]["function"]["arguments"])["targetName"], "b")

    def test_fenced_malformed_array_salvaged(self):
        text = "```json\n[\n" + self.GOOD % "a" + ",\n" + self.BAD + "\n]\n```"
        calls, _ = srv.extract_tool_calls(text, self.NAMES, tools=self.TOOLS)
        self.assertEqual(len(calls), 1)

    def test_malformed_data_array_stays_content(self):
        text = '[{"step": 1, "note": "walk"}, {broken]'
        calls, cleaned = srv.extract_tool_calls(text, self.NAMES, tools=self.TOOLS)
        self.assertIsNone(calls)
        self.assertIn("walk", cleaned)


class BareArgsArrayTests(unittest.TestCase):
    """Fable 5 live G6 spelling: ONE fenced JSON ARRAY whose elements are bare argument objects
    of a single tool (no function name anywhere) -- a 75-object castle scored tools=0 before
    this. Gate mirrors extract_bare_object_lines: keys must fit EXACTLY ONE declared tool."""

    TOOLS = [
        {"type": "function", "function": {"name": "world_command",
         "parameters": {"type": "object", "properties": {
             "action": {}, "targetName": {}, "prefabKey": {}, "stringValue": {},
             "x": {}, "y": {}, "z": {}}}}},
        {"type": "function", "function": {"name": "execute_lua",
         "parameters": {"type": "object", "properties": {"code": {}}}}},
    ]
    NAMES = {"world_command", "execute_lua"}

    FABLE_FENCE = ('```json\n[\n'
                   '  {"action":"spawn","prefabKey":"Cube","targetName":"Wall1","x":0,"y":0,"z":0},\n'
                   '  {"action":"set_color","targetName":"TreeTop4","stringValue":"#2f6d33"}\n'
                   ']\n```')

    def test_fenced_bare_args_array_extracts_calls(self):
        calls, cleaned = srv.extract_tool_calls(self.FABLE_FENCE, self.NAMES, self.TOOLS)
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(c["function"]["name"] == "world_command" for c in calls))
        self.assertEqual(json.loads(calls[1]["function"]["arguments"])["stringValue"], "#2f6d33")
        self.assertEqual(cleaned, "")

    def test_plain_data_array_survives_as_content(self):
        text = '```json\n[{"score": 10, "player": "a"}, {"score": 20, "player": "b"}]\n```'
        calls, cleaned = srv.extract_tool_calls(text, self.NAMES, self.TOOLS)
        self.assertIsNone(calls)
        self.assertIn("score", cleaned)

    def test_ambiguous_two_tools_rejected(self):
        tools = self.TOOLS + [{"type": "function", "function": {"name": "other_tool",
            "parameters": {"type": "object", "properties": {"action": {}, "targetName": {}}}}}]
        text = '```json\n[{"action":"spawn","targetName":"a"}]\n```'
        calls, _ = srv.extract_tool_calls(text, {"world_command", "execute_lua", "other_tool"}, tools)
        self.assertIsNone(calls)

    def test_live_emitter_streams_bare_args_objects_before_fence_closes(self):
        h = _EmitterHarness(names=self.NAMES, tools=self.TOOLS)
        cut = self.FABLE_FENCE.index('\n]')  # everything except the closing bracket + fence
        h.feed_chunked(self.FABLE_FENCE[:cut])
        self.assertEqual(len(h.calls), 2, "Both bare-args objects must stream before the fence closes.")
        self.assertEqual(h.calls[0][1], "world_command")
        self.assertEqual(h.calls[0][2]["targetName"], "Wall1")
        h.emitter.feed(self.FABLE_FENCE[cut:])
        h.emitter.finish()
        self.assertEqual(len(h.calls), 2, "Reconciliation must not double-emit.")

    def test_live_emitter_releases_data_array_as_content(self):
        text = '```json\n[{"score": 10}, {"score": 20}]\n```\n' + "x" * 400
        h = _EmitterHarness(names=self.NAMES, tools=self.TOOLS)
        h.feed_chunked(text)
        h.emitter.finish()
        self.assertEqual(h.calls, [])
        self.assertIn('"score"', h.content.replace(" ", "") or h.content)


class ActionAliasSpellingTests(unittest.TestCase):
    """Opus 4.8 live spelling: {"action": "<tool>", "arguments": {...}} instead of "name" --
    G5 Ordered spawn scored tools=0 on a perfectly-shaped 3-call array before this."""

    NAMES = {"world_command"}

    def test_action_alias_is_call_shaped_and_normalized(self):
        shaped = srv._call_shaped(
            {"action": "world_command", "arguments": {"action": "spawn", "targetName": "Gate"}},
            self.NAMES)
        self.assertEqual(shaped, {"name": "world_command",
                                  "arguments": {"action": "spawn", "targetName": "Gate"}})

    def test_unknown_action_value_stays_content(self):
        self.assertIsNone(srv._call_shaped({"action": "fly", "arguments": {}}, self.NAMES))

    def test_extra_keys_stay_content(self):
        self.assertIsNone(srv._call_shaped(
            {"action": "world_command", "arguments": {}, "note": "x"}, self.NAMES))

    def test_fenced_array_of_action_alias_calls_extracts(self):
        text = ('```json\n[\n'
                '{"action": "world_command", "arguments": {"action": "spawn", "targetName": "Gate"}},\n'
                '{"action": "world_command", "arguments": {"action": "spawn", "targetName": "Flag"}}\n'
                ']\n```')
        calls, _ = srv.extract_tool_calls(text, self.NAMES)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["function"]["name"], "world_command")
        self.assertEqual(json.loads(calls[0]["function"]["arguments"])["targetName"], "Gate")

    def test_live_emitter_streams_action_alias_calls(self):
        text = ('```json\n{"tool_calls": [\n'
                '{"action": "world_command", "arguments": {"action": "spawn", "targetName": "Gate"}}\n'
                ']}\n```')
        h = _EmitterHarness(names=("world_command",))
        h.feed_chunked(text)
        h.emitter.finish()
        self.assertEqual(len(h.calls), 1)
        self.assertEqual(h.calls[0][1], "world_command")


class MatchCanonicalPrefixTests(unittest.TestCase):
    def test_complete_prefix_yes_with_end_index(self):
        state, end = srv._match_canonical_prefix('{"tool_calls": [{"name":')
        self.assertEqual(state, "yes")
        self.assertEqual('{"tool_calls": [{"name":'[end:], '{"name":')

    def test_truncated_prefix_is_maybe(self):
        for body in ("", "{", '{"tool', '{ "tool_calls"', '{"tool_calls":'):
            self.assertEqual(srv._match_canonical_prefix(body)[0], "maybe", body)

    def test_whitespace_between_tokens_allowed(self):
        state, _ = srv._match_canonical_prefix('  {\n  "tool_calls" : [\n')
        self.assertEqual(state, "yes")

    def test_other_json_is_no(self):
        self.assertEqual(srv._match_canonical_prefix('{"answer": 42}')[0], "no")
        self.assertEqual(srv._match_canonical_prefix('def foo():')[0], "no")


class ObjectEndTests(unittest.TestCase):
    def test_simple_object(self):
        s = '{"a": 1} tail'
        self.assertEqual(s[:srv._object_end(s, 0)], '{"a": 1}')

    def test_nested_and_strings_with_braces(self):
        s = '{"a": {"b": "}"}, "c": "{"} rest'
        self.assertEqual(s[:srv._object_end(s, 0)], '{"a": {"b": "}"}, "c": "{"}')

    def test_escaped_quote_inside_string(self):
        s = '{"a": "x\\"}{"} tail'
        self.assertEqual(s[:srv._object_end(s, 0)], '{"a": "x\\"}{"}')

    def test_incomplete_returns_minus_one(self):
        self.assertEqual(srv._object_end('{"a": {"b": 1}', 0), -1)


class _EmitterHarness:
    """Collects everything a LiveToolCallEmitter sends to the wire, in order."""

    def __init__(self, names=("spawn_object", "set_color"), tools=None, prior=None):
        self.events = []  # ("content", str) / ("call", name, args_dict)
        self.emitter = srv.LiveToolCallEmitter(
            set(names), tools, prior,
            on_content=lambda s: self.events.append(("content", s)),
            on_call=lambda call, idx: self.events.append(
                ("call", call["function"]["name"],
                 json.loads(call["function"]["arguments"]))),
        )

    def feed_chunked(self, text, size=3):
        for i in range(0, len(text), size):
            self.emitter.feed(text[i:i + size])

    @property
    def calls(self):
        return [e for e in self.events if e[0] == "call"]

    @property
    def content(self):
        return "".join(e[1] for e in self.events if e[0] == "content")


class LiveEmitterCanonicalTests(unittest.TestCase):
    CANONICAL = ('Building it now.\n```json\n{"tool_calls": [\n'
                 '{"name": "spawn_object", "arguments": {"prefab": "cube", "name": "t1"}},\n'
                 '{"name": "set_color", "arguments": {"name": "t1", "color": "red"}}\n'
                 ']}\n```\nDone.')

    def test_calls_emitted_before_fence_closes(self):
        h = _EmitterHarness()
        # Feed everything UP TO (not including) the closing fence: both calls must already be out.
        cut = self.CANONICAL.index("]}")
        h.feed_chunked(self.CANONICAL[:cut])
        self.assertEqual([c[1] for c in h.calls], ["spawn_object", "set_color"])

    def test_undeclared_call_emitted_before_fence_closes(self):
        h = _EmitterHarness(names=("world_command",))
        h.feed_chunked('```json\n{"tool_calls":[{"name":"change",'
                       '"arguments":{"targetName":"TowerB","scale":2.0}}')
        self.assertEqual(h.calls, [("call", "change", {"targetName": "TowerB", "scale": 2.0})])
        h.emitter.finish()
        self.assertEqual(len(h.calls), 1)

    def test_finish_does_not_double_emit(self):
        h = _EmitterHarness()
        h.feed_chunked(self.CANONICAL)
        h.emitter.finish()
        self.assertEqual(len(h.calls), 2)
        self.assertEqual(h.calls[0][2], {"prefab": "cube", "name": "t1"})

    def test_fence_text_never_leaks_into_content(self):
        h = _EmitterHarness()
        h.feed_chunked(self.CANONICAL)
        h.emitter.finish()
        self.assertNotIn("tool_calls", h.content)
        self.assertNotIn("```", h.content)

    def test_single_char_feeding(self):
        h = _EmitterHarness()
        h.feed_chunked(self.CANONICAL, size=1)
        h.emitter.finish()
        self.assertEqual(len(h.calls), 2)

    def test_unterminated_canonical_fence_still_emits_closed_calls(self):
        h = _EmitterHarness()
        cut = self.CANONICAL.index("]}")  # stream dies mid-turn
        h.feed_chunked(self.CANONICAL[:cut])
        h.emitter.finish()
        self.assertEqual(len(h.calls), 2)


class LiveEmitterNonCanonicalTests(unittest.TestCase):
    def test_per_call_fenced_object_emitted_at_fence_close(self):
        text = ('```json\n{"name": "spawn_object", "arguments": {"prefab": "cube"}}\n```')
        h = _EmitterHarness()
        h.feed_chunked(text)
        self.assertEqual(len(h.calls), 1)
        self.assertEqual(h.calls[0][1], "spawn_object")

    def test_jsonl_fence_emitted_at_fence_close(self):
        text = ('```json\n'
                '{"name": "spawn_object", "arguments": {"prefab": "cube"}}\n'
                '{"name": "set_color", "arguments": {"color": "red"}}\n'
                '```')
        h = _EmitterHarness()
        h.feed_chunked(text)
        self.assertEqual([c[1] for c in h.calls], ["spawn_object", "set_color"])

    def test_code_fence_released_as_content(self):
        text = "Here is code:\n```python\nprint('hi')\n```\nthat's it. " + "x" * 400
        h = _EmitterHarness()
        h.feed_chunked(text)
        h.emitter.finish()
        self.assertEqual(h.calls, [])
        self.assertIn("print('hi')", h.content)
        self.assertIn("```python", h.content)

    def test_func_syntax_reconciled_at_finish(self):
        # The incremental scanner doesn't know codex's name(arg=value) spelling -- the
        # end-of-turn full parse must still produce the call.
        text = 'spawn_object(prefab="cube", name="t1")\n' + "padding " * 60
        h = _EmitterHarness()
        h.feed_chunked(text)
        h.emitter.finish()
        self.assertEqual(len(h.calls), 1)
        self.assertEqual(h.calls[0][2]["prefab"], "cube")


class LiveEmitterHoldbackTests(unittest.TestCase):
    def test_short_answer_held_until_finish_fallback(self):
        h = _EmitterHarness()
        h.emitter.feed("Short answer.")
        self.assertEqual(h.events, [])  # under holdback: nothing on the wire yet
        self.assertFalse(h.emitter.wire_started)
        fallback = h.emitter.finish()
        self.assertEqual(fallback, "Short answer.")
        self.assertFalse(h.emitter.any_calls)

    def test_long_answer_streams_after_holdback(self):
        h = _EmitterHarness()
        h.feed_chunked("word " * 200)
        self.assertTrue(h.emitter.wire_started)
        self.assertTrue(len(h.content) > 0)
        self.assertIsNone(h.emitter.finish())

    def test_limit_banner_never_reaches_wire(self):
        banner = "You've hit your session limit · resets 5:40pm"
        h = _EmitterHarness()
        h.emitter.feed(banner)
        self.assertFalse(h.emitter.wire_started)
        self.assertEqual(h.emitter.raw_text, banner)  # caller checks looks_like_limit_banner

    def test_call_flushes_preceding_prose_first(self):
        text = ('placing now\n```json\n{"tool_calls": ['
                '{"name": "spawn_object", "arguments": {"prefab": "cube"}}]}\n```')
        h = _EmitterHarness()
        h.feed_chunked(text)
        self.assertEqual(h.events[0][0], "content")
        self.assertIn("placing now", h.events[0][1])
        self.assertEqual(h.events[1][0], "call")

    def test_reset_wipes_unflushed_state(self):
        h = _EmitterHarness()
        h.emitter.feed("attempt one partial ```json\n{\"tool_")
        h.emitter.reset()
        h.emitter.feed("clean second answer")
        self.assertEqual(h.emitter.raw_text, "clean second answer")
        self.assertEqual(h.emitter.finish(), "clean second answer")


class StreamTextFilterTests(unittest.TestCase):
    @staticmethod
    def _run_filter(lines):
        import io
        spec = importlib.util.spec_from_file_location(
            "stream_text_filter", os.path.join(REPO_ROOT, "stream_text_filter.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        out = io.StringIO()
        mod.main(stdin=io.StringIO("\n".join(lines) + "\n"), stdout=out)
        return out.getvalue()

    @staticmethod
    def _delta(text):
        return json.dumps({"type": "stream_event",
                           "event": {"type": "content_block_delta",
                                     "delta": {"type": "text_delta", "text": text}}})

    def test_deltas_concatenate_with_trailing_newline(self):
        out = self._run_filter([self._delta("Hel"), self._delta("lo")])
        self.assertEqual(out, "Hello\n")

    def test_assistant_event_not_duplicated_after_deltas(self):
        assistant = json.dumps({"type": "assistant", "message": {
            "content": [{"type": "text", "text": "Hello"}]}})
        out = self._run_filter([self._delta("Hel"), self._delta("lo"), assistant])
        self.assertEqual(out, "Hello\n")

    def test_assistant_event_covers_missing_deltas(self):
        assistant = json.dumps({"type": "assistant", "message": {
            "content": [{"type": "text", "text": "Full answer"}]}})
        out = self._run_filter([assistant])
        self.assertEqual(out, "Full answer\n")

    def test_result_only_printed_when_nothing_else_was(self):
        result = json.dumps({"type": "result", "result": "limit banner text"})
        self.assertEqual(self._run_filter([result]), "limit banner text\n")
        out = self._run_filter([self._delta("real answer"), result])
        self.assertEqual(out, "real answer\n")

    def test_non_json_lines_pass_through(self):
        out = self._run_filter(["some CLI error line", self._delta("ok")])
        self.assertEqual(out, "some CLI error line\nok\n")

    def test_unknown_events_ignored(self):
        out = self._run_filter([json.dumps({"type": "system", "subtype": "init"}),
                                self._delta("hi")])
        self.assertEqual(out, "hi\n")


class TailTaskLogTests(unittest.TestCase):
    """_tail_task_log must forward exactly the text after this run's own output marker, as it
    is appended, surviving multi-byte characters split across reads."""

    def setUp(self):
        import tempfile
        self._saved_logdir = srv.LOGDIR
        self._tmp = tempfile.mkdtemp()
        srv.LOGDIR = self._tmp

    def tearDown(self):
        import shutil
        srv.LOGDIR = self._saved_logdir
        shutil.rmtree(self._tmp, ignore_errors=True)

    class _FakeProc:
        def __init__(self):
            self.dead = False

        def poll(self):
            return 0 if self.dead else None

        def kill(self):
            self.dead = True

    def test_forwards_only_after_marker_and_in_pieces(self):
        name = "tail-test"
        path = os.path.join(self._tmp, name + ".log")
        got = []
        # Each poll precedes a drain: publish one chunk per read, including a UTF-8
        # character split across reads, regardless of the reader's scheduling.
        chunks = iter([b"header stuff\n> PROMPT: hi\n",
                       (srv.OUTPUT_MARKER + "\nHel").encode("utf-8"),
                       b"lo \xd0", b"\xb6"])

        class WritingProc(self._FakeProc):
            def poll(self):
                chunk = next(chunks, None)
                if chunk is None:
                    self.dead = True
                else:
                    with open(path, "ab") as f:
                        f.write(chunk)
                return super().poll()

        proc = WritingProc()
        srv._tail_task_log(name, proc, timeout=10, on_delta=got.append)
        self.assertEqual("".join(got), "Hello ж")
        self.assertEqual(got, ["Hel", "lo ", "ж"])


class ProviderNoiseStreamFilterTests(unittest.TestCase):
    def test_filters_split_diagnostic_lines_without_delaying_tool_json(self):
        chunks = []
        filtered = srv.ProviderNoiseStreamFilter(chunks.append)
        source = ('```json\n{"tool_calls":[{"name":"spawn_object","arguments":{}}]}\n```\n'
                  'session id: ses_example123\n'
                  '[codex] activity: item.completed\n'
                  '[opencode] unknown format "uint32"\n'
                  'Done')
        for i in range(0, len(source), 3):
            filtered.feed(source[i:i + 3])
        filtered.finish()
        self.assertEqual('```json\n{"tool_calls":[{"name":"spawn_object","arguments":{}}]}\n```\nDone',
                         ''.join(chunks))
        self.assertGreater(len(chunks), 1)

    def test_session_id_before_first_tool_call_is_not_streamed(self):
        chunks = []
        filtered = srv.ProviderNoiseStreamFilter(chunks.append)
        source = ('session id: ses_example123\n'
                  '[opencode] activity: step_start\n'
                  '```json\n{"tool_calls":[{"name":"execute_lua","arguments":{"code":"return 1"}}]}\n```')
        for i in range(0, len(source), 2):
            filtered.feed(source[i:i + 2])
        filtered.finish()
        self.assertEqual('```json\n{"tool_calls":[{"name":"execute_lua","arguments":{"code":"return 1"}}]}\n```',
                         ''.join(chunks))

    def test_reset_discards_partial_prefix_from_failed_retry(self):
        chunks = []
        filtered = srv.ProviderNoiseStreamFilter(chunks.append)
        filtered.feed('[cod')
        filtered.reset()
        filtered.feed('answer')
        filtered.finish()
        self.assertEqual('answer', ''.join(chunks))


class ApiKeyAuthTests(unittest.TestCase):
    """`--api-key` / $AGENT_OPENAI_KEY -- the gate that makes a LAN/public bridge safe to expose.
    An open bridge (no key) must keep behaving exactly as it always did on loopback."""

    def setUp(self):
        self._orig_cfg = srv.CFG

        class FakeCfg:
            api_key = ""

        self.cfg = FakeCfg()
        srv.CFG = self.cfg

    def tearDown(self):
        srv.CFG = self._orig_cfg

    def test_no_key_configured_lets_everything_through(self):
        self.assertTrue(srv.request_authorized({}, "/v1/chat/completions"))
        self.assertTrue(srv.request_authorized({}, "/reset"))

    def test_bearer_token_accepted(self):
        self.cfg.api_key = "s3cret"
        self.assertTrue(srv.request_authorized(
            {"Authorization": "Bearer s3cret"}, "/v1/chat/completions"))

    def test_bearer_scheme_is_case_insensitive(self):
        self.cfg.api_key = "s3cret"
        self.assertTrue(srv.request_authorized(
            {"Authorization": "bearer s3cret"}, "/v1/chat/completions"))

    def test_x_api_key_header_accepted(self):
        self.cfg.api_key = "s3cret"
        self.assertTrue(srv.request_authorized({"X-Api-Key": "s3cret"}, "/v1/chat/completions"))

    def test_wrong_and_missing_keys_rejected(self):
        self.cfg.api_key = "s3cret"
        self.assertFalse(srv.request_authorized({}, "/v1/chat/completions"))
        self.assertFalse(srv.request_authorized(
            {"Authorization": "Bearer nope"}, "/v1/chat/completions"))
        self.assertFalse(srv.request_authorized({"X-Api-Key": "nope"}, "/reset"))

    def test_prefix_of_the_key_is_not_accepted(self):
        self.cfg.api_key = "s3cret"
        self.assertFalse(srv.request_authorized({"X-Api-Key": "s3c"}, "/v1/chat/completions"))

    def test_health_and_root_stay_open_for_probes(self):
        self.cfg.api_key = "s3cret"
        self.assertTrue(srv.request_authorized({}, "/health"))
        self.assertTrue(srv.request_authorized({}, "/"))

    def test_models_endpoint_requires_the_key(self):
        self.cfg.api_key = "s3cret"
        self.assertFalse(srv.request_authorized({}, "/v1/models"))

    def test_query_string_does_not_defeat_the_path_check(self):
        self.cfg.api_key = "s3cret"
        self.assertTrue(srv.request_authorized({}, "/health?x=1"))
        self.assertFalse(srv.request_authorized({}, "/v1/chat/completions?health=1"))


class OpencodeSessionBodyTests(unittest.TestCase):
    """The native opencode session payload. Both extra fields are load-bearing: `agent` is the
    tool lockdown (without it a LAN caller had bash/write/MCP), `location` is the working dir
    (without it the agent wrote files into the bridge process's own cwd)."""

    def test_chat_only_agent_is_always_requested(self):
        body = srv.opencode_session_body("opencode/big-pickle", "/tmp/x")
        self.assertEqual(body["agent"], srv.OPENCODE_CHATONLY_AGENT)

    def test_provider_and_model_are_split_on_the_first_slash(self):
        body = srv.opencode_session_body("zai/glm-4.6", "/tmp/x")
        self.assertEqual(body["model"], {"providerID": "zai", "id": "glm-4.6"})

    def test_bare_model_leaves_opencode_its_own_default(self):
        self.assertNotIn("model", srv.opencode_session_body("", "/tmp/x"))
        self.assertNotIn("model", srv.opencode_session_body("big-pickle", "/tmp/x"))

    def test_workdir_becomes_the_session_location(self):
        body = srv.opencode_session_body("opencode/big-pickle", "C:/Git/Proj")
        self.assertEqual(body["location"], {"directory": "C:/Git/Proj"})

    def test_no_workdir_omits_location_rather_than_sending_null(self):
        self.assertNotIn("location", srv.opencode_session_body("opencode/big-pickle", None))

    def test_chat_only_config_points_at_a_file_that_disables_every_tool(self):
        with open(srv.OPENCODE_CHATONLY_CONFIG, encoding="utf-8") as f:
            cfg = json.load(f)
        agent = cfg["agent"][srv.OPENCODE_CHATONLY_AGENT]
        self.assertEqual(agent["tools"], {"*": False})


class _DoPostHarness(unittest.TestCase):
    """Drives srv.H.do_POST against a bare fake receiver (no socket), stubbing the three
    terminal branches so routing/body validation is observable in isolation."""

    def setUp(self):
        self._orig_cfg = srv.CFG
        self.sent = []   # (code, payload) of every _send_json
        self.hit = []    # which terminal branch ran ("reset" / "sync" / "stream")

        class FakeCfg:
            api_key = ""
        srv.CFG = FakeCfg()

    def tearDown(self):
        srv.CFG = self._orig_cfg

    def _call(self, path, body=b"", headers=None):
        h = srv.H
        sent, hit = self.sent, self.hit

        class Fake:
            _reject_unauthorized = h._reject_unauthorized
            _read_content_length = h._read_content_length
            _send_json = staticmethod(lambda code, obj: sent.append((code, obj)))
            _reset_session = staticmethod(lambda: hit.append("reset"))
            _sync_response = staticmethod(
                lambda messages, tools, tool_choice: hit.append(("sync", messages, tool_choice)))
            _stream_response = staticmethod(
                lambda messages, tools, tool_choice: hit.append(("stream", tool_choice)))

            def __init__(self):
                self.path = path
                self.headers = dict(headers) if headers else {}
                if body and "Content-Length" not in self.headers:
                    self.headers["Content-Length"] = str(len(body))
                self.rfile = io.BytesIO(body)

        srv.H.do_POST(Fake())


class TrailingSlashRouteTests(_DoPostHarness):
    """Documented POST routes accept one cosmetic trailing slash, and nothing broader."""

    def test_one_trailing_slash_accepted_for_all_documented_post_paths(self):
        for p in ("/chat/completions/", "/v1/chat/completions/", "/reset/", "/v1/reset/"):
            with self.subTest(path=p):
                self.hit.clear()
                self.sent.clear()
                self._call(p, b'{"messages":[{"role":"user","content":"hi"}]}')
                self.assertEqual(self.sent, [])
                self.assertTrue(self.hit)

    def test_trailing_slash_reset_actually_resets(self):
        self._call("/v1/reset/")
        self.assertEqual(self.hit, ["reset"])

    def test_doubled_trailing_slash_is_still_a_404(self):
        self._call("/v1/chat/completions//")
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][0], 404)
        self.assertFalse(self.hit)


class ResetBodyGuardTests(_DoPostHarness):
    """Reset validates its body before changing session state."""

    def test_invalid_json_body_on_reset_is_400_and_does_not_reset(self):
        raw = b"{not json"
        self._call("/v1/reset", body=raw, headers={"Content-Length": str(len(raw))})
        self.assertEqual(self.sent[0][0], 400)
        self.assertFalse(self.hit)

    def test_non_object_json_body_on_reset_is_400_and_does_not_reset(self):
        raw = b"[1,2]"
        self._call("/reset", body=raw, headers={"Content-Length": str(len(raw))})
        self.assertEqual(self.sent[0][0], 400)
        self.assertFalse(self.hit)

    def test_absent_body_still_resets(self):
        self._call("/v1/reset")
        self.assertFalse(self.sent)
        self.assertEqual(self.hit, ["reset"])

    def test_valid_json_object_body_resets(self):
        self._call("/v1/reset", b'{"why": "not"}')
        self.assertFalse(self.sent)
        self.assertEqual(self.hit, ["reset"])


class PostRoutingTests(_DoPostHarness):
    """POST accepts only the four documented exact paths (plus one harmless trailing slash);
    everything else is a 404."""

    def test_documented_paths_are_accepted(self):
        for p in ("/chat/completions", "/v1/chat/completions", "/reset", "/v1/reset"):
            with self.subTest(path=p):
                self.hit.clear()
                self.sent.clear()
                self._call(p, b'{"messages":[{"role":"user","content":"hi"}]}')
                self.assertEqual(self.sent, [])
                self.assertTrue(self.hit)

    def test_lookalike_and_unknown_paths_are_404_without_touching_the_session(self):
        for p in ("/x/chat/completions", "/chat/completions/extra", "/API/reset",
                  "/v2/reset", "/completions", "/foo", "/health/reset"):
            with self.subTest(path=p):
                self.hit.clear()
                self.sent.clear()
                self._call(p)
                self.assertEqual(len(self.sent), 1)
                self.assertEqual(self.sent[0][0], 404)
                self.assertFalse(self.hit)

    def test_network_and_absolute_route_forms_are_rejected(self):
        for p in ("//v1/reset", "//host/v1/reset", "http://host/v1/reset"):
            with self.subTest(path=p):
                self.sent.clear()
                self.hit.clear()
                self._call(p)
                self.assertEqual(self.sent[0][0], 404)
                self.assertFalse(self.hit)


class PostBodyGuardTests(_DoPostHarness):
    """Malformed/non-integer/oversized Content-Length and invalid or non-object JSON are
    controlled 400/413 responses, not crashes or unbounded reads."""

    def test_non_integer_content_length_is_400(self):
        for v in ("abc", "12.5", "1e3", " ²"):
            with self.subTest(value=v):
                self.sent.clear()
                self.hit.clear()
                self._call("/v1/chat/completions", headers={"Content-Length": v})
                self.assertEqual(self.sent[0][0], 400)
                self.assertFalse(self.hit)

    def test_negative_content_length_is_400_not_a_crash(self):
        self._call("/v1/chat/completions", headers={"Content-Length": "-5"})
        self.assertEqual(self.sent[0][0], 400)

    def test_oversized_content_length_is_413_before_any_body_is_read(self):
        self._call("/v1/chat/completions",
                   headers={"Content-Length": str(srv.MAX_BODY_BYTES + 1)})
        self.assertEqual(self.sent[0][0], 413)

    def test_invalid_json_body_is_400(self):
        raw = b"{not json"
        self._call("/v1/chat/completions", body=raw,
                   headers={"Content-Length": str(len(raw))})
        self.assertEqual(self.sent[0][0], 400)

    def test_json_array_body_is_400_because_it_must_be_an_object(self):
        raw = b"[1,2]"
        self._call("/v1/chat/completions", body=raw,
                   headers={"Content-Length": str(len(raw))})
        self.assertEqual(self.sent[0][0], 400)

    def test_missing_header_and_empty_body_still_reach_the_handler(self):
        self._call("/v1/chat/completions")
        self.assertFalse(self.hit)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][0], 400)
        self.assertIn("messages", self.sent[0][1]["error"]["message"])

    def test_malformed_chat_schema_table_never_reaches_provider(self):
        valid = {"messages": [{"role": "user", "content": "hi"}]}
        cases = [
            ({"messages": "bad"}, "messages"),
            ({"messages": [7]}, "messages[0]"),
            ({"messages": [{"role": 7, "content": "x"}]}, "messages[0].role"),
            ({"messages": [{"role": "user", "content": {}}]}, "messages[0].content"),
            ({"messages": [{"role": "assistant", "content": None, "tool_calls": {}}]},
             "messages[0].tool_calls"),
            ({"messages": [{"role": "assistant", "content": None, "tool_calls": [
                {"id": "x", "type": "function", "function": {"name": "f", "arguments": {}}}]}]},
             "messages[0].tool_calls[0].function.arguments"),
            (dict(valid, tools={}), "tools"),
            (dict(valid, tools=[{"type": "function", "function": {"name": "bad name"}}]),
             "tools[0].function.name"),
            (dict(valid, tools=[{"type": "function", "function": {
                "name": "ok", "parameters": []}}]), "tools[0].function.parameters"),
            (dict(valid, tool_choice=7), "tool_choice"),
            (dict(valid, stream="true"), "stream"),
        ]
        for body, param in cases:
            with self.subTest(param=param):
                self.sent.clear(); self.hit.clear()
                self._call("/v1/chat/completions", json.dumps(body).encode("utf-8"))
                self.assertEqual(self.sent[0][0], 400)
                self.assertEqual(self.sent[0][1]["error"]["param"], param)
                self.assertEqual(self.sent[0][1]["error"]["code"], "invalid_parameter")
                self.assertFalse(self.hit)

    def test_handler_auth_gate_runs_before_body_or_provider(self):
        srv.CFG.api_key = "secret"
        self._call("/v1/chat/completions",
                   b'{"messages":[{"role":"user","content":"hi"}]}')
        self.assertEqual(self.sent[0][0], 401)
        self.assertFalse(self.hit)


class InstanceIdTests(unittest.TestCase):
    """Each bridge process publishes a random instance_id in its registry record and in
    /health -- the identity gui.stop_bridge checks before trusting a recorded pid."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig_dir, self._orig_cfg = srv.BRIDGES_DIR, srv.CFG
        srv.BRIDGES_DIR = self.tmp

        class FakeCfg:
            port = 18901
            host = "127.0.0.1"      # loopback -> register_bridge does no network probing
            engine, model, effort, dir = "codex", "spark", "", ""
            timeout, retries, session_ttl, api_key = 60, 1, 1800, ""
        self.cfg = FakeCfg()
        srv.CFG = self.cfg

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)
        srv.BRIDGES_DIR, srv.CFG = self._orig_dir, self._orig_cfg

    def test_registry_record_carries_the_process_instance_id(self):
        srv.register_bridge(self.cfg)
        with open(os.path.join(self.tmp, "bridge-18901.json"), encoding="utf-8") as f:
            rec = json.load(f)
        self.assertEqual(rec["instance_id"], srv.INSTANCE_ID)
        self.assertTrue(rec["instance_id"])

    def test_health_reports_instance_id(self):
        got = []

        class Fake:
            path = "/health"
            headers = {}
            _reject_unauthorized = srv.H._reject_unauthorized
            _send_json = staticmethod(lambda code, obj: got.append((code, obj)))

        srv.H.do_GET(Fake())
        self.assertEqual(got[0][0], 200)
        self.assertEqual(got[0][1].get("instance_id"), srv.INSTANCE_ID)


class LoggingTests(unittest.TestCase):
    """Diagnostics are opt-in (quiet WARNING default; AGENT_LOG_LEVEL raises the gate) and even
    then never carry sensitive payloads: no request bodies, prompts, auth headers or API keys."""

    def setUp(self):
        self._level = srv.LOG.level
        srv.LOG.setLevel(logging.WARNING)  # pin the documented default for every test here

    def tearDown(self):
        srv.LOG.setLevel(self._level)

    class _FakeSelf:
        @staticmethod
        def address_string():
            return "127.0.0.1"

    def test_http_request_lines_route_to_debug_only(self):
        with self.assertLogs(srv.LOG, level=logging.DEBUG) as cm:
            srv.H.log_message(self._FakeSelf(), '"GET /health HTTP/1.1" %s %s', 200, "-")
        self.assertTrue(cm.records)
        self.assertTrue(all(r.levelno == logging.DEBUG for r in cm.records))
        # at the default gate the same call is fully silent
        with self.assertRaises(AssertionError):
            with self.assertLogs(srv.LOG, level=logging.WARNING):
                srv.H.log_message(self._FakeSelf(), '"GET /health HTTP/1.1" %s %s', 200, "-")

    def test_expected_registry_removal_failure_is_swallowed_and_debug_only(self):
        # no such registry file -- the routine double-exit case: must not raise and must stay
        # below the default WARNING gate
        with self.assertLogs(srv.LOG, level=logging.DEBUG):
            srv.unregister_bridge(1)
        with self.assertRaises(AssertionError):
            with self.assertLogs(srv.LOG, level=logging.WARNING):
                srv.unregister_bridge(1)

    def test_registry_write_failure_warns_without_exposing_the_api_key(self):
        d = tempfile.mkdtemp()
        blocker = os.path.join(d, "not-a-dir")
        open(blocker, "w").close()  # an existing FILE -> makedirs(exist_ok=True) fails
        orig_dir = srv.BRIDGES_DIR
        srv.BRIDGES_DIR = blocker

        class FakeCfg:
            port = 18999
            host = "127.0.0.1"  # loopback -> no network probing in register_bridge
            engine, model, effort, dir_ = "codex", "spark", "", ""
            timeout, retries, session_ttl, api_key = 60, 1, 1800, ""

        cfg = FakeCfg()
        cfg.dir = ""
        secret = "TOPSECRET-bridge-key"
        cfg.api_key = secret
        try:
            with self.assertLogs(srv.LOG, level=logging.WARNING) as cm:
                srv.register_bridge(cfg)
        finally:
            srv.BRIDGES_DIR = orig_dir
            import shutil
            shutil.rmtree(d, ignore_errors=True)
        joined = "\n".join(cm.output)
        self.assertNotIn(secret, joined)
        self.assertNotIn("Authorization", joined)


class LiveDisconnectSessionTests(unittest.TestCase):
    def test_broken_pipe_invalidates_resume_without_recording_partial_assistant(self):
        messages = [{"role": "user", "content": "stream this"}]
        saved = (srv.CFG, srv.SESSION, srv.claude_native_kill)

        class FakeCfg:
            engine, model, effort = "claude", "sonnet", "low"

        class BreakOnFinish:
            def __init__(self):
                self.writes = 0

            def write(self, data):
                self.writes += 1
                if self.writes >= 2:
                    raise BrokenPipeError("client disconnected")
                return len(data)

            def flush(self):
                pass

        class Fake:
            def __init__(self):
                self.wfile = BreakOnFinish()

            def _sse_headers(self):
                self._sse_started = True

            def _raw_completion(self, request_messages, tools, tool_choice="auto", **kwargs):
                srv.SESSION["task_name"] = "live-task"
                srv.SESSION["messages"] = list(request_messages)
                kwargs["on_delta"]("partial answer")
                return "partial answer"

        srv.CFG = FakeCfg()
        srv.SESSION = {"task_name": None, "messages": [], "dir": "/tmp/live",
                       "last_activity": 0.0}
        srv.claude_native_kill = lambda: None
        try:
            fake = Fake()
            srv.H._stream_response_live(fake, messages, None, "none")
            self.assertIsNone(srv.SESSION["task_name"])
            self.assertEqual(srv.SESSION["messages"], messages)
            self.assertNotIn("assistant", json.dumps(srv.SESSION["messages"]))
        finally:
            srv.CFG, srv.SESSION, srv.claude_native_kill = saved


class StreamErrorSafetyTests(unittest.TestCase):
    """Late failures close SSE without appending a second HTTP response."""

    BODY = b'{"messages":[{"role":"user","content":"hi"}],"stream":true}'

    def _drive(self, sse_started, exc):
        """Drives do_POST against a fake receiver whose _stream_response raises `exc`;
        returns (json responses sent, raw bytes written to the stream)."""
        sent = []

        class FakeCfg:
            api_key = ""

        orig_cfg = srv.CFG
        srv.CFG = FakeCfg()
        holder = {}

        class Fake:
            _reject_unauthorized = srv.H._reject_unauthorized
            _read_content_length = srv.H._read_content_length
            _abort_stream_safely = srv.H._abort_stream_safely
            _send_json = staticmethod(lambda code, obj: sent.append((code, obj)))

            def __init__(self):
                self.path = "/v1/chat/completions"
                self.headers = {"Content-Length": str(len(StreamErrorSafetyTests.BODY))}
                self.rfile = io.BytesIO(StreamErrorSafetyTests.BODY)
                self.wfile = io.BytesIO()
                self._sse_started = sse_started
                holder["w"] = self.wfile

            def _stream_response(self, messages, tools, tool_choice):
                raise exc

        try:
            srv.H.do_POST(Fake())
        finally:
            srv.CFG = orig_cfg
        return sent, holder["w"].getvalue()

    def test_exception_after_sse_started_never_sends_a_second_status_line(self):
        sent, wfile = self._drive(True, RuntimeError("boom after headers"))
        self.assertEqual(sent, [], "a 500 JSON response must NOT follow SSE bytes")
        self.assertIn(b"event: error", wfile)
        self.assertNotIn(b"[DONE]", wfile)

    def test_provider_limit_after_sse_started_also_closes_safely(self):
        exc = srv.ProviderLimitError("You've hit your session limit")
        sent, wfile = self._drive(True, exc)
        self.assertEqual(sent, [])
        self.assertIn(b"event: error", wfile)
        self.assertNotIn(b"[DONE]", wfile)

    def test_provider_stream_dying_mid_answer_ends_with_a_mapped_error_event(self):
        sent, wfile = self._drive(True, srv.LiveStreamDied())
        self.assertEqual(sent, [])
        self.assertIn(b"event: error", wfile)
        self.assertIn(b"stream_interrupted", wfile)
        self.assertNotIn(b"[DONE]", wfile)

    def test_image_validation_after_sse_started_closes_without_second_status(self):
        exc = srv.RequestValidationError("messages[1].content[0].image_url", "too many images", 413)
        sent, wfile = self._drive(True, exc)
        self.assertEqual(sent, [])
        self.assertIn(b"event: error", wfile)
        self.assertNotIn(b"[DONE]", wfile)

    def test_image_validation_before_stream_returns_http_413(self):
        exc = srv.RequestValidationError("messages[1].content[0].image_url", "too many images", 413)
        sent, wfile = self._drive(False, exc)
        self.assertEqual(sent[0][0], 413)
        self.assertEqual(wfile, b"")

    def test_exception_before_any_byte_still_gets_openai_style_500(self):
        sent, wfile = self._drive(False, RuntimeError("boom"))
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], 500)
        error = sent[0][1]["error"]
        # Never a bare 500: the message names the failure class and the incident to look up,
        # while the exception text itself (which may carry request data) stays in the log.
        self.assertTrue(error["message"].startswith("bridge request failed"))
        self.assertIn("RuntimeError", error["message"])
        self.assertIn(error["incident_id"], error["message"])
        self.assertIsNone(error["param"])
        self.assertIn("server_error", error["type"])
        self.assertNotIn("boom", json.dumps(sent[0][1]))
        self.assertEqual(wfile, b"", "no stream bytes may be written on the sync error path")

    def test_real_sse_headers_mark_the_response_as_started(self):
        class Fake:
            _sse_headers = srv.H._sse_headers

            def send_response(self, code):
                pass

            def send_header(self, k, v):
                pass

            def end_headers(self):
                pass

        f = Fake()
        f._sse_headers()
        self.assertTrue(getattr(f, "_sse_started", False))
        self.assertTrue(f.close_connection)

    def test_abort_is_best_effort_when_stream_is_already_closed(self):
        class Closed:
            def write(self, _data):
                raise ValueError("closed")

        class Fake:
            wfile = Closed()

        srv.H._abort_stream_safely(Fake())


class PortArgTests(unittest.TestCase):
    def test_valid_ports_pass_through_as_ints(self):
        self.assertEqual(srv._port_arg("8801"), 8801)
        self.assertEqual(srv._port_arg(65535), 65535)

    def test_zero_means_pick_a_free_port(self):
        self.assertEqual(srv._port_arg("0"), 0)

    def test_garbage_out_of_range_and_negative_are_clean_errors(self):
        for v in ("abc", "", None, "-1", "65536", "70000"):
            with self.subTest(port=v), self.assertRaises(argparse.ArgumentTypeError):
                srv._port_arg(v)


class GetRouteAndHealthStateTests(unittest.TestCase):
    def setUp(self):
        self.old = (srv.CFG, srv.SESSION, srv.ACTIVE_REQUESTS)
        class C:
            api_key = ""
            engine, model, effort = "codex", "spark", ""
            timeout, retries, session_ttl = 60, 1, 1800
        srv.CFG = C()
        srv.SESSION = {"task_name": "reusable", "messages": [], "dir": None,
                       "last_activity": srv.time.time()}
        srv.ACTIVE_REQUESTS = 0

    def tearDown(self):
        srv.CFG, srv.SESSION, srv.ACTIVE_REQUESTS = self.old

    def call(self, path):
        sent = []
        class F:
            headers = {}
            _reject_unauthorized = srv.H._reject_unauthorized
            _send_json = staticmethod(lambda code, body: sent.append((code, body)))
        f = F(); f.path = path
        srv.H.do_GET(f)
        return sent[0]

    def test_models_routes_are_exact_not_suffix_matches(self):
        self.assertEqual(self.call("/models")[0], 200)
        self.assertEqual(self.call("/v1/models")[0], 200)
        self.assertEqual(self.call("/attacker/models")[0], 404)

    def test_busy_tracks_inflight_work_not_reusable_session(self):
        code, body = self.call("/health")
        self.assertEqual(code, 200)
        self.assertTrue(body["session_active"])
        self.assertFalse(body["busy"])
        srv.ACTIVE_REQUESTS = 1
        self.assertTrue(self.call("/health")[1]["busy"])


class NetworkSafetyTests(unittest.TestCase):
    @staticmethod
    def cfg(engine="claude", host="127.0.0.1", key=""):
        class C:
            lan = localhost = False
            model, effort = "sonnet", "low"
        c = C()
        c.engine, c.host, c.api_key = engine, host, key
        return c

    def test_loopback_is_safe_without_key(self):
        self.assertIsNone(srv.validate_network_config(self.cfg()))

    def test_default_bind_is_loopback(self):
        old = os.environ.pop("AGENT_OPENAI_HOST", None)
        try:
            self.assertEqual(srv.default_bind_host(), "127.0.0.1")
        finally:
            if old is not None:
                os.environ["AGENT_OPENAI_HOST"] = old

    def test_nonloopback_always_requires_key(self):
        self.assertIn("requires --api-key",
                      srv.validate_network_config(self.cfg(host="0.0.0.0")))
        self.assertIsNone(srv.validate_network_config(
            self.cfg(host="0.0.0.0", key="secret")))

    def test_read_capable_codex_and_gemini_are_loopback_only_even_with_key(self):
        for engine in ("codex", "gemini"):
            with self.subTest(engine=engine):
                self.assertIn("loopback-only", srv.validate_network_config(
                    self.cfg(engine=engine, host="0.0.0.0", key="secret")))

    def test_unsafe_native_opencode_is_loopback_only(self):
        old = srv.OPENCODE_NATIVE
        srv.OPENCODE_NATIVE = True
        try:
            self.assertIn("loopback-only", srv.validate_network_config(
                self.cfg(engine="opencode", host="0.0.0.0", key="secret")))
        finally:
            srv.OPENCODE_NATIVE = old

    def test_native_claude_resolves_alias_effort_and_advertised_label_together(self):
        model, effort, label = srv.resolve_claude_model("sonnet", "")
        self.assertEqual((model, effort, label),
                         ("claude-sonnet-5", "high", "claude/Sonnet 5 (high)"))
        old_cfg = srv.CFG
        srv.CFG = self.cfg()
        try:
            command = srv._claude_native_cmd()
            self.assertIn("--tools ''", command)
            self.assertNotIn("--disallowedTools", command)
        finally:
            srv.CFG = old_cfg
        with self.assertRaises(ValueError):
            srv.resolve_claude_model("arbitrary-shell-alias", "")


# ==============================================================================================
# Multi-model routing, the conversation pool, error mapping, watchdogs and the registry tools.
# Stub providers only: no CLI is launched and no provider is contacted.
# ==============================================================================================
import socket
import urllib.error
import urllib.request

_REAL_SLEEP = time.sleep  # captured before any test stubs srv.time.sleep (the same module)

# The exact `reason` the core recorded on 2026-10-15 for an OpenCode free model driven through
# the bridge (trimmed headers): it must come back as a clear 403, not a bare 500 after a retry.
FREE_TIER_REASON = json.dumps({
    "name": "APIError",
    "data": {
        "message": "OpenCode's free tier can only be used from within OpenCode. "
                   "Download it at https://opencode.ai and keep building.",
        "statusCode": 403, "isRetryable": False,
        "responseHeaders": {"content-type": "application/json", "server": "cloudflare"},
        "responseBody": json.dumps({"type": "error", "error": {
            "type": "FreeTierError",
            "message": "OpenCode's free tier can only be used from within OpenCode. "
                       "Download it at https://opencode.ai and keep building."}}),
        "metadata": {"url": "https://opencode.[REDACTED]"}}})


def _meta(state="error", exit_code="126", category="", reason=""):
    meta = {"state": state, "exit": exit_code, "reason": reason}
    if category or reason:
        meta["failure_json"] = json.dumps({"category": category, "reason": reason,
                                           "source": "provider_stream", "confidence": "explicit",
                                           "blocking": True, "exit_code": 126})
    return meta


FAKE_PROVIDERS = {
    "fakeeng": {"supports_resume": True, "efforts": ["low", "high"], "models": []},
    "fakealt": {"supports_resume": True, "efforts": [], "models": []},
    "noresume": {"supports_resume": False, "efforts": [], "models": []},
}


def _cfg(**overrides):
    values = dict(engine="fakeeng", model="m", effort="", dir="", port=8990, host="127.0.0.1",
                  timeout=60, retries=0, session_ttl=600, max_sessions=16, max_concurrency=8,
                  min_free_memory_mb=2048,
                  engine_limits={}, max_queue=64, queue_timeout=10, stall_timeout=0, api_key="",
                  no_live_stream=True, strict_models=None, allow_any=False, check=False,
                  json=False, name="", lan=False, localhost=False, routes=None,
                  max_request_mb=64)
    values.update(overrides)
    return argparse.Namespace(**values)


class _FakeProviderMixin:
    """Swaps in stub engines and a fresh pool; records every run/reply the bridge would launch."""

    def setUp(self):
        self._saved_bridge = (srv.CFG, srv.ROUTES, srv.POOL, srv.PROVIDERS, srv.run_agent,
                              srv.reply_agent, srv.read_meta, srv.time.sleep, srv.SESSION,
                              dict(srv._DYNAMIC_ROUTES))
        srv.PROVIDERS = dict(FAKE_PROVIDERS)
        srv.POOL = srv.SessionPool(memory_probe=lambda: 8192)
        srv.SESSION = {"task_name": None, "messages": [], "dir": None, "last_activity": 0.0}
        srv._DYNAMIC_ROUTES.clear()
        srv.time.sleep = lambda seconds: None
        self.calls = []
        self.metas = {}
        self.calls_lock = threading.Lock()
        srv.read_meta = lambda name: dict(self.metas.get(name, {"state": "done"}))
        srv.run_agent = self._fake_run
        srv.reply_agent = self._fake_reply
        self.answer = lambda kind, engine, model, prompt, name: "%s answer" % kind
        self.configure()

    def tearDown(self):
        srv.POOL.close_all()
        (srv.CFG, srv.ROUTES, srv.POOL, srv.PROVIDERS, srv.run_agent, srv.reply_agent,
         srv.read_meta, srv.time.sleep, srv.SESSION, dynamic) = self._saved_bridge
        srv._DYNAMIC_ROUTES.clear()
        srv._DYNAMIC_ROUTES.update(dynamic)

    def configure(self, *specs, **overrides):
        srv.CFG = _cfg(**overrides)
        routes = srv.build_routes(srv.CFG.engine, list(specs) or ["m"], srv.CFG.effort,
                                  engine_explicit=not specs)
        srv.CFG.routes = routes
        srv.ROUTES = list(routes)
        return routes

    def _record(self, kind, engine, model, workdir, name, prompt, effort=None):
        with self.calls_lock:
            self.calls.append({"kind": kind, "engine": engine, "model": model, "dir": workdir,
                               "task": name, "prompt": prompt, "effort": effort,
                               "thread": threading.current_thread().name,
                               "session": getattr(srv._CTX.session, "id", None)})

    def _fake_run(self, engine, model, effort, workdir, prompt, name, timeout):
        self._record("run", engine, model, workdir, name, prompt, effort)
        return self.answer("run", engine, model, prompt, name)

    def _fake_reply(self, engine, model, effort, workdir, name, answer, timeout):
        self._record("reply", engine, model, workdir, name, answer, effort)
        return self.answer("reply", engine, model, answer, name)


def _turn(messages, user, model=None):
    body = {"messages": list(messages) + [{"role": "user", "content": user}]}
    if model:
        body["model"] = model
    return body


class _LiveHTTPMixin(_FakeProviderMixin):
    """A real ThreadingHTTPServer on a free loopback port, backed by the stub providers."""

    def setUp(self):
        super().setUp()
        request_done = self.request_done = {}

        class Handler(srv.H):
            def do_POST(self):
                try:
                    super().do_POST()
                finally:
                    done = request_done.get(self.headers.get("X-Test-Request-ID"))
                    if done is not None:
                        done.set()

        self.server = srv._BridgeHTTPServer(("127.0.0.1", 0), Handler)
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.1},
                                       daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def post(self, body, path="/v1/chat/completions", timeout=20):
        done = threading.Event()
        request_id = str(id(done))
        self.request_done[request_id] = done
        request = urllib.request.Request(self.base + path, data=json.dumps(body).encode("utf-8"),
                                         headers={"Content-Type": "application/json",
                                                  "X-Test-Request-ID": request_id}, method="POST")
        try:
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    result = response.status, json.loads(response.read().decode("utf-8")), dict(response.headers)
            except urllib.error.HTTPError as error:
                with error:
                    result = error.code, json.loads(error.read().decode("utf-8")), dict(error.headers)
            # Receiving the body can precede checkin(), readiness updates and logging.
            # Tests inspecting that state must wait for the handler's finally blocks.
            self.assertTrue(done.wait(timeout), "HTTP handler did not finish after its response")
            return result
        finally:
            self.request_done.pop(request_id, None)

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))

    def chat(self, history, user, model=None):
        """One OpenAI-style turn; returns the client-side history including the answer."""
        status, body, _headers = self.post(_turn(history, user, model))
        self.assertEqual(status, 200, body)
        return list(history) + [{"role": "user", "content": user},
                                {"role": "assistant", "content": body["choices"][0]["message"]["content"]}]


class RouteParsingTests(unittest.TestCase):
    def test_engine_model_effort_specs(self):
        route = srv.parse_model_spec("codex/6-luna")
        self.assertEqual((route.engine, route.model, route.id), ("codex", "6-luna", "codex/6-luna"))
        route = srv.parse_model_spec("claude/sonnet@low")
        self.assertEqual((route.engine, route.model, route.effort, route.id),
                         ("claude", "sonnet", "low", "claude/sonnet@low"))
        self.assertEqual(route.label, "claude/Sonnet 5 (low)")
        self.assertIsNone(srv.parse_model_spec("6-luna"))
        self.assertIsNone(srv.parse_model_spec("unknown-engine/x"))

    def test_opencode_zen_ids_keep_their_provider_segment_and_aliases_stay_aliases(self):
        route = srv.parse_model_spec("opencode/space-bunny-free")
        self.assertEqual((route.engine, route.model, route.id),
                         ("opencode", "opencode/space-bunny-free", "opencode/space-bunny-free"))
        route = srv.parse_model_spec("opencode/free")
        self.assertEqual((route.model, route.id), ("free", "opencode/free"))
        route = srv.parse_model_spec("opencode/anthropic/claude-x")
        self.assertEqual(route.model, "anthropic/claude-x")

    def test_single_model_with_explicit_engine_keeps_the_historic_verbatim_meaning(self):
        (route,) = srv.build_routes("codex", ["6-luna"], "", engine_explicit=True)
        self.assertEqual((route.engine, route.model), ("codex", "6-luna"))
        (route,) = srv.build_routes("opencode", ["opencode/big-pickle"], "", engine_explicit=True)
        self.assertEqual((route.engine, route.model), ("opencode", "opencode/big-pickle"))
        (route,) = srv.build_routes("codex", [], "medium", engine_explicit=False)
        self.assertEqual((route.engine, route.model, route.effort), ("codex", "", "medium"))
        (route,) = srv.build_routes("codex", ["claude/sonnet"], "", engine_explicit=False)
        self.assertEqual(route.engine, "claude")

    def test_several_specs_become_several_routes_default_first_without_duplicates(self):
        routes = srv.build_routes("codex", ["codex/6-luna", "6-sol", "claude/sonnet", "codex/6-luna"],
                                  "", engine_explicit=False)
        self.assertEqual([r.id for r in routes], ["codex/6-luna", "codex/6-sol", "claude/sonnet"])

    def test_parse_config_accepts_repeated_m_and_models_lists(self):
        cfg = srv.parse_config(["-m", "codex/6-luna", "--models", "codex/6-sol,claude/sonnet",
                                "-p", "0", "--engine-limit", "claude=1", "--max-concurrency", "3"])
        self.assertEqual([r.id for r in cfg.routes], ["codex/6-luna", "codex/6-sol", "claude/sonnet"])
        self.assertEqual((cfg.engine, cfg.model, cfg.port), ("codex", "6-luna", 0))
        self.assertEqual(cfg.engine_limits, {"claude": 1})
        self.assertEqual(cfg.max_concurrency, 3)
        legacy = srv.parse_config(["-e", "claude", "-m", "sonnet", "-f", "low", "-p", "8801"])
        self.assertEqual([r.id for r in legacy.routes], ["claude/sonnet@low"])
        self.assertEqual((legacy.engine, legacy.model, legacy.effort), ("claude", "sonnet", "low"))

    def test_network_safety_checks_every_route_and_allow_any(self):
        cfg = _cfg(host="0.0.0.0", api_key="secret",
                   routes=[srv.Route("claude", "sonnet", ""), srv.Route("codex", "6-luna", "")])
        self.assertIn("loopback-only", srv.validate_network_config(cfg))
        cfg = _cfg(host="0.0.0.0", api_key="secret", allow_any=True,
                   routes=[srv.Route("claude", "sonnet", "")])
        self.assertIn("--allow-any", srv.validate_network_config(cfg))
        cfg = _cfg(routes=[srv.Route("claude", "arbitrary-shell-alias", "")])
        self.assertIn("unsupported Claude", srv.validate_network_config(cfg))


class RouteResolutionTests(_FakeProviderMixin, unittest.TestCase):
    def test_request_model_selects_route_by_id_label_or_unique_alias(self):
        routes = self.configure("fakeeng/alpha", "fakeeng/beta@high", "fakealt/alpha")
        self.assertIs(srv.resolve_route("fakeeng/beta@high"), routes[1])
        self.assertIs(srv.resolve_route("FAKEENG/BETA (HIGH)"), routes[1])   # label, any case
        self.assertIs(srv.resolve_route("fakeeng/beta"), routes[1])           # id without effort
        self.assertIs(srv.resolve_route("beta"), routes[1])                   # unique bare alias
        self.assertIs(srv.resolve_route(None), routes[0])
        self.assertIs(srv.resolve_route(""), routes[0])

    def test_multi_route_bridges_are_strict_unless_told_otherwise(self):
        self.configure("fakeeng/alpha", "fakealt/alpha")
        with self.assertRaises(srv.BridgeError) as raised:
            srv.resolve_route("gpt-4o")
        self.assertEqual((raised.exception.status, raised.exception.code), (404, "model_not_found"))
        self.assertEqual(raised.exception.param, "model")
        self.assertIn("fakeeng/alpha", str(raised.exception))
        with self.assertRaises(srv.BridgeError):
            srv.resolve_route("alpha")  # ambiguous bare alias is not guessed
        srv.CFG.strict_models = False
        self.assertIs(srv.resolve_route("gpt-4o"), srv.ROUTES[0])

    def test_single_route_keeps_the_lenient_historic_behaviour(self):
        (route,) = self.configure()
        self.assertIs(srv.resolve_route("anything-the-client-says"), route)
        srv.CFG.strict_models = True
        with self.assertRaises(srv.BridgeError):
            srv.resolve_route("anything-the-client-says")
        with self.assertRaises(srv.RequestValidationError):
            srv.resolve_route(42)

    def test_allow_any_adds_validated_bounded_on_demand_routes(self):
        self.configure("fakeeng/alpha", allow_any=True)
        route = srv.resolve_route("fakealt/zeta")
        self.assertEqual((route.engine, route.model, route.dynamic), ("fakealt", "zeta", True))
        self.assertIs(srv.resolve_route("fakealt/zeta"), route)
        self.assertIn("fakealt/zeta", [r.id for r in srv._all_routes()])
        for bad in ("fakealt/-rf", "fakealt/a b", "nope/x"):
            with self.subTest(model=bad), self.assertRaises(srv.BridgeError):
                srv.resolve_route(bad)

    def test_models_endpoint_lists_every_route_with_label_and_readiness(self):
        self.configure("fakeeng/alpha", "fakealt/beta")
        sent = []

        class Fake:
            headers = {}
            path = "/v1/models"
            _reject_unauthorized = srv.H._reject_unauthorized
            _send_json = staticmethod(lambda code, body: sent.append((code, body)))

        srv.H.do_GET(Fake())
        code, body = sent[0]
        self.assertEqual(code, 200)
        self.assertEqual([m["id"] for m in body["data"]], ["fakeeng/alpha", "fakealt/beta"])
        self.assertEqual([m["default"] for m in body["data"]], [True, False])
        self.assertTrue(all(m["label"] and m["state"] == "unknown" for m in body["data"]))
        sent.clear()
        Fake.path = "/v1/models/fakealt/beta"
        srv.H.do_GET(Fake())
        self.assertEqual((sent[0][0], sent[0][1]["id"]), (200, "fakealt/beta"))
        sent.clear()
        Fake.path = "/v1/models/nope"
        srv.H.do_GET(Fake())
        self.assertEqual(sent[0][0], 404)


class ErrorMappingTests(unittest.TestCase):
    def classify(self, meta=None, outcome=None, raw_text="", engine="opencode"):
        return srv.classify_failure_outcome(engine, meta, outcome, raw_text)

    def test_opencode_free_tier_refusal_is_a_clear_non_retryable_403(self):
        error = self.classify(_meta(category="auth", reason=FREE_TIER_REASON))
        self.assertIsInstance(error, srv.CompletionFailed)
        self.assertEqual((error.status, error.code, error.error_type), (403, "provider_forbidden", "permission_error"))
        self.assertFalse(error.retryable)
        self.assertFalse(error.internal_retry)
        self.assertIn("free tier can only be used from within OpenCode", str(error))
        self.assertIn("FreeTierError", str(error))
        self.assertIn("provider HTTP 403", str(error))
        payload = error.payload("abc")["error"]
        self.assertEqual(set(payload), {"message", "type", "code", "param", "retryable", "incident_id"})
        self.assertIsNone(payload["param"])

    def test_status_and_category_table(self):
        cases = [
            (_meta(category="auth", reason="Invalid API key · Please run /login"), 401, "provider_auth_failed", False),
            (_meta(reason=json.dumps({"error": {"message": "Payment required: insufficient credits", "status": 402}})),
             402, "provider_billing", False),
            (_meta(state="limited", category="quota", reason="Rate limit reached. Please try again in 20s"),
             429, "rate_limit_exceeded", True),
            (_meta(category="model_unavailable", reason="model gpt-x not found"), 404, "model_not_found", False),
            (_meta(reason=json.dumps({"error": {"message": "prompt is too long", "code": 400}})),
             400, "provider_bad_request", False),
            (_meta(category="network", reason="ECONNRESET socket hang up"), 503, "provider_unavailable", True),
            (_meta(category="transport", reason="503 Service Unavailable: overloaded"), 503, "provider_unavailable", True),
            (_meta(category="provider_oom", reason="JavaScript heap out of memory"), 502, "provider_crashed", True),
            (_meta(state="silent", exit_code="125", category="watchdog_silence"), 504, "provider_stalled", True),
            (_meta(state="error", exit_code="124", category="watchdog_timeout"), 504, "provider_timeout", True),
            (_meta(reason="something odd happened"), 502, "provider_failed", True),
        ]
        for meta, status, code, retryable in cases:
            with self.subTest(code=code, reason=meta.get("reason", "")[:40]):
                error = self.classify(meta)
                self.assertEqual((error.status, error.code, error.retryable), (status, code, retryable))
                self.assertTrue(str(error))

    def test_429_carries_retry_after_from_the_provider_text(self):
        error = self.classify(_meta(state="limited", category="quota",
                                    reason="usage limit reached, resets in 2h 5m"))
        self.assertEqual(error.retry_after, 2 * 3600 + 5 * 60)
        self.assertEqual(error.headers(), {"Retry-After": "7500"})
        self.assertEqual(srv._retry_after_seconds('"retry-after": "30"'), 30)
        self.assertIsNone(srv._retry_after_seconds("try again at 7:40am"))

    def test_watchdog_outcomes_and_wrapper_failures(self):
        self.assertEqual(self.classify(outcome={"stalled": True}).code, "provider_stalled")
        self.assertEqual(self.classify(outcome={"timed_out": True}).status, 504)
        self.assertIsInstance(self.classify(outcome={"cancelled": True}), srv.ClientDisconnected)
        missing = self.classify(meta={}, outcome={"stderr": "neoxider: opencode CLI not found; install it"})
        self.assertEqual((missing.status, missing.code, missing.retryable), (503, "provider_unavailable", False))
        empty = self.classify(meta={"state": "done"}, raw_text="")
        self.assertEqual((empty.status, empty.code, empty.internal_retry), (502, "empty_completion", True))

    def test_internal_retry_is_reserved_for_transient_failures(self):
        self.assertTrue(self.classify(_meta(category="network", reason="fetch failed")).internal_retry)
        provider_says_no = json.dumps({"data": {"message": "upstream 500", "statusCode": 500,
                                                "isRetryable": False}})
        self.assertFalse(self.classify(_meta(reason=provider_says_no)).internal_retry)
        for meta in (_meta(category="auth", reason=FREE_TIER_REASON),
                     _meta(state="limited", category="quota", reason="rate limit"),
                     _meta(state="silent", exit_code="125")):
            self.assertFalse(self.classify(meta).internal_retry)

    def test_model_answer_text_is_never_pattern_matched_as_an_error(self):
        # A successful-looking state with an answer that DISCUSSES limits/auth is not classified
        # by its words; only the exact limit banner is.
        error = self.classify({"state": "done"}, raw_text="Here is how a 403 forbidden works ...")
        self.assertEqual(error.code, "provider_failed")
        banner = self.classify({"state": "done"}, raw_text="You've hit your session limit · resets in 30m")
        self.assertEqual((banner.status, banner.retry_after), (429, 1800))


class NoRetryOnRefusalTests(_FakeProviderMixin, unittest.TestCase):
    def test_403_is_raised_after_one_attempt_even_with_retries_left(self):
        self.configure(retries=2)
        self.answer = lambda *a: ""
        self.metas = {}
        srv.read_meta = lambda name: _meta(category="auth", reason=FREE_TIER_REASON)
        with self.assertRaises(srv.CompletionFailed) as raised:
            srv.H._run(object(), [{"role": "user", "content": "hi"}], None)
        self.assertEqual(raised.exception.status, 403)
        self.assertEqual(len([c for c in self.calls if c["kind"] == "run"]), 1)

    def test_transient_failure_is_still_retried(self):
        self.configure(retries=1)
        answers = iter(["", "RECOVERED"])
        self.answer = lambda *a: next(answers)
        states = iter([_meta(category="network", reason="ECONNRESET"), {"state": "done"}])
        srv.read_meta = lambda name: next(states, {"state": "done"})
        text, _calls, _usage = srv.H._run(object(), [{"role": "user", "content": "hi"}], None)
        self.assertEqual(text, "RECOVERED")
        self.assertEqual(len(self.calls), 2)

    def test_refused_resume_is_not_replayed_as_a_fresh_full_history_run(self):
        self.configure(retries=1)
        first = [{"role": "user", "content": "one"}]
        srv.SESSION.update(task_name="prior", messages=first + [{"role": "assistant", "content": "a"}],
                           dir="/tmp/x", last_activity=time.time())
        self.answer = lambda kind, *a: None if kind == "reply" else "FRESH"
        metas = {"prior": {"state": "done"}}
        srv.read_meta = lambda name: metas.get(name, {"state": "done"})

        def refused_reply(engine, model, effort, workdir, name, answer, timeout):
            self._record("reply", engine, model, workdir, name, answer)
            metas["prior"] = _meta(state="limited", category="quota", reason="usage limit, retry after 60s")
            return None

        srv.reply_agent = refused_reply
        with self.assertRaises(srv.CompletionFailed) as raised:
            srv.H._run(object(), srv.SESSION["messages"] + [{"role": "user", "content": "two"}], None)
        self.assertEqual((raised.exception.status, raised.exception.retry_after), (429, 60))
        self.assertEqual([c["kind"] for c in self.calls], ["reply"])
        self.assertIsNone(srv.SESSION["task_name"])


class MemoryProbeTests(unittest.TestCase):
    def test_windows_uses_the_smaller_of_physical_and_commit_headroom(self):
        def probe(pointer):
            status = pointer._obj
            self.assertEqual(status.dwLength, 64)
            status.ullAvailPhys = physical * 1024 * 1024
            status.ullAvailPageFile = commit * 1024 * 1024
            return 1

        import ctypes
        api = mock.Mock(side_effect=probe)
        kernel = mock.Mock(GlobalMemoryStatusEx=api)
        with mock.patch.object(srv.sys, "platform", "win32"), \
                mock.patch.object(ctypes, "WinDLL", return_value=kernel, create=True):
            for physical, commit in [(8192, 1024), (512, 8192)]:
                self.assertEqual(srv._memory_free_mb(), min(physical, commit))
            api.side_effect = None
            api.return_value = 0
            self.assertIsNone(srv._memory_free_mb())

    def test_linux_reads_memavailable(self):
        info = "MemFree: 1024 kB\nMemAvailable: 3145728 kB\n"
        with mock.patch.object(srv.sys, "platform", "linux"), \
                mock.patch("builtins.open", mock.mock_open(read_data=info)) as opened:
            self.assertEqual(srv._memory_free_mb(), 3072)
            opened.assert_called_once_with("/proc/meminfo", encoding="ascii")

    def test_linux_probe_failure_or_missing_field_is_unknown(self):
        with mock.patch.object(srv.sys, "platform", "linux"):
            for info in ["MemFree: 1024 kB\n", "MemAvailable: invalid kB\n"]:
                with mock.patch("builtins.open", mock.mock_open(read_data=info)):
                    self.assertIsNone(srv._memory_free_mb())
            with mock.patch("builtins.open", side_effect=OSError("unavailable")):
                self.assertIsNone(srv._memory_free_mb())

    def test_macos_psutil_is_optional(self):
        psutil = mock.Mock()
        psutil.virtual_memory.return_value.available = 4096 * 1024 * 1024
        with mock.patch.object(srv.sys, "platform", "darwin"):
            with mock.patch.dict(sys.modules, {"psutil": psutil}):
                self.assertEqual(srv._memory_free_mb(), 4096)
            with mock.patch.dict(sys.modules, {"psutil": None}):
                self.assertIsNone(srv._memory_free_mb())


class MemoryPressureTests(_FakeProviderMixin, unittest.TestCase):
    def idle(self, label, last_used=None):
        session = srv.POOL.checkout(srv.ROUTES[0], [{"role": "user", "content": label}])
        session["messages"] = [{"role": "user", "content": label},
                               {"role": "assistant", "content": "ok"}]
        srv.POOL.checkin(session)
        if last_used is not None:
            session.last_used = last_used
        return session

    def test_parser_defaults_and_explicit_values(self):
        parser = srv.build_parser()
        cfg = parser.parse_args([])
        self.assertEqual((cfg.session_ttl, cfg.max_sessions, cfg.min_free_memory_mb),
                         (600, 16, 2048))
        cfg = parser.parse_args(["--session-ttl", "1800", "--max-sessions", "64",
                                 "--min-free-memory-mb", "0"])
        self.assertEqual((cfg.session_ttl, cfg.max_sessions, cfg.min_free_memory_mb),
                         (1800, 64, 0))
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            parser.parse_args(["--min-free-memory-mb", "-1"])

    def test_default_ttl_expires_after_600_seconds(self):
        with mock.patch.object(srv.time, "time", return_value=1000.0):
            session = self.idle("A")
        with mock.patch.object(srv.time, "time", return_value=1600.0):
            self.assertEqual(srv.POOL.evict(), 0)
        with mock.patch.object(srv.time, "time", return_value=1601.0):
            self.assertEqual(srv.POOL.evict(), 1)
        self.assertTrue(session.closed)

    def test_default_capacity_evicts_the_lru_above_16_sessions(self):
        self.configure(session_ttl=0)
        first = self.idle("0", last_used=0)
        for index in range(1, 17):
            self.idle(str(index))
        self.assertTrue(first.closed)
        self.assertEqual(len(srv.POOL.list_sessions()), 16)

    def test_pressure_evicts_lru_idle_one_at_a_time_until_headroom_recovers(self):
        self.configure(session_ttl=0)
        newest = self.idle("newest", last_used=30)
        oldest = self.idle("oldest", last_used=10)
        middle = self.idle("middle", last_used=20)
        expected = iter([(512, []), (1024, [oldest]), (2048, [oldest, middle])])

        def probe():
            free, closed = next(expected)
            for session in closed:
                self.assertTrue(session.closed)
            return free

        srv.POOL.memory_probe = mock.Mock(side_effect=probe)
        with mock.patch.object(srv, "_close_session", wraps=srv._close_session) as close, \
                self.assertLogs(srv.LOG, level=logging.WARNING) as logs:
            self.assertEqual(srv.POOL.evict(), 2)  # same path as a reaper tick
        self.assertEqual([call.args[0] for call in close.call_args_list], [oldest, middle])
        self.assertEqual(srv.POOL.list_sessions(), [newest])
        self.assertEqual(srv.POOL.snapshot()["evicted"], 2)
        self.assertIn("memory pressure: 512 MB free < 2048 MB", logs.output[0])
        self.assertIn("memory pressure: 1024 MB free < 2048 MB", logs.output[1])
        self.assertEqual(srv.POOL.memory_probe.call_count, 3)

    def test_busy_and_protected_sessions_are_never_evicted(self):
        self.configure(session_ttl=0)
        busy = srv.POOL.checkout(srv.ROUTES[0], [{"role": "user", "content": "busy"}])
        keep = self.idle("keep", last_used=1)
        idle = self.idle("idle", last_used=2)
        srv.POOL.memory_probe = lambda: 512
        try:
            self.assertEqual(srv.POOL.evict_memory(keep=keep), 1)
            self.assertEqual(srv.POOL.list_sessions(), [busy, keep])
            self.assertFalse(busy.closed)
            self.assertFalse(keep.closed)
            self.assertTrue(idle.closed)
        finally:
            srv.POOL.checkin(busy)

    def test_pressure_is_checked_before_the_cli_turn(self):
        idle = self.idle("old")
        srv.POOL.memory_probe = mock.Mock(side_effect=[512, 4096])
        with srv._conversation(srv.ROUTES[0], [{"role": "user", "content": "new"}]) as chosen:
            self.assertTrue(idle.closed)
            self.assertTrue(chosen.busy)
            self.assertFalse(chosen.closed)
            self.assertEqual(srv.POOL.list_sessions(), [chosen])

    def test_low_memory_without_idle_sessions_continues_and_warns_once_a_minute(self):
        srv.POOL.memory_probe = lambda: 512
        with mock.patch.object(srv.time, "monotonic", return_value=100.0) as clock, \
                mock.patch.object(srv.LOG, "warning") as warning:
            for now in [100.0, 120.0, 160.0]:
                clock.return_value = now
                with srv._conversation(srv.ROUTES[0], [{"role": "user", "content": "new"}]) as chosen:
                    self.assertFalse(chosen.closed)
                    self.assertEqual(srv.POOL.evict_memory(), 0)
            pressure = [call for call in warning.call_args_list if "continuing" in call.args[0]]
            self.assertEqual(len(pressure), 2)

    def test_unknown_or_failed_probe_does_not_evict(self):
        idle = self.idle("A")
        for probe in [mock.Mock(return_value=None), mock.Mock(side_effect=OSError("unavailable"))]:
            srv.POOL.memory_probe = probe
            self.assertEqual(srv.POOL.evict_memory(), 0)
            self.assertIsNone(srv.health_payload()["memory_free_mb"])
        self.assertFalse(idle.closed)

    def test_zero_disables_pressure_probing_and_eviction(self):
        self.configure(min_free_memory_mb=0)
        idle = self.idle("A")
        srv.POOL.memory_probe = mock.Mock(return_value=0)
        self.assertEqual(srv.POOL.evict(), 0)
        with srv._conversation(srv.ROUTES[0], [{"role": "user", "content": "new"}]):
            self.assertFalse(idle.closed)
        srv.POOL.memory_probe.assert_not_called()


class ConversationPoolHTTPTests(_LiveHTTPMixin, unittest.TestCase):
    def test_health_reports_memory_headroom_and_threshold(self):
        self.configure(min_free_memory_mb=3072)
        srv.POOL.memory_probe = lambda: 4096
        health = self.get("/health")
        self.assertEqual((health["memory_free_mb"], health["min_free_memory_mb"]), (4096, 3072))
        srv.POOL.memory_probe = lambda: None
        self.assertIsNone(self.get("/health")["memory_free_mb"])

    def test_interleaved_conversations_each_resume_their_own_session(self):
        """A1, B1, A2, B2, A3: A and B never overwrite each other -- A2/A3 resume A's CLI task with
        only the new tail, B2 resumes B's, each with its own working directory."""
        self.configure()  # stub engine, per-conversation scratch dirs (no --dir pin)
        a = self.chat([], "A1 question")
        b = self.chat([], "B1 question")
        a = self.chat(a, "A2 follow-up")
        b = self.chat(b, "B2 follow-up")
        a = self.chat(a, "A3 follow-up")
        runs = [c for c in self.calls if c["kind"] == "run"]
        replies = [c for c in self.calls if c["kind"] == "reply"]
        self.assertEqual(len(runs), 2, "only the two first turns may start fresh runs")
        self.assertEqual(len(replies), 3)
        task_a, task_b = runs[0]["task"], runs[1]["task"]
        self.assertNotEqual(task_a, task_b)
        self.assertEqual([r["task"] for r in replies], [task_a, task_b, task_a])
        # each resume carries ONLY the new tail of its own conversation
        self.assertIn("A2 follow-up", replies[0]["prompt"])
        self.assertNotIn("A1 question", replies[0]["prompt"])
        self.assertNotIn("B1 question", replies[0]["prompt"])
        self.assertIn("B2 follow-up", replies[1]["prompt"])
        self.assertNotIn("A2", replies[1]["prompt"])
        self.assertIn("A3 follow-up", replies[2]["prompt"])
        # own working directory per conversation, stable across that conversation's turns
        dir_a, dir_b = runs[0]["dir"], runs[1]["dir"]
        self.assertNotEqual(dir_a, dir_b)
        self.assertEqual({replies[0]["dir"], replies[2]["dir"]}, {dir_a})
        self.assertEqual(replies[1]["dir"], dir_b)
        for directory, session in ((dir_a, runs[0]["session"]), (dir_b, runs[1]["session"])):
            self.assertTrue(os.path.basename(directory).endswith(session))
        health = self.get("/health")
        self.assertEqual(health["session_count"], 2)
        turns = sorted(s["turns"] for s in health["sessions"])
        self.assertEqual(turns, [2, 3])
        self.assertTrue(all("idle_seconds" in s and "id" in s for s in health["sessions"]))
        dumped = json.dumps(health)
        for secret in ("A1 question", "B2 follow-up", "answer"):
            self.assertNotIn(secret, dumped, "health must never expose message content")
        detail = self.get("/v1/sessions")["data"]
        self.assertEqual(sorted(s["task"] for s in detail), sorted([task_a, task_b]))

    def test_cross_engine_conversations_stay_isolated_in_one_process(self):
        self.configure("fakeeng/alpha", "fakealt/beta")
        a = self.chat([], "A1", model="fakeeng/alpha")
        b = self.chat([], "B1", model="fakealt/beta")
        a = self.chat(a, "A2", model="fakeeng/alpha")
        b = self.chat(b, "B2", model="fakealt/beta")
        replies = [c for c in self.calls if c["kind"] == "reply"]
        self.assertEqual([(r["engine"], r["model"]) for r in replies],
                         [("fakeeng", "alpha"), ("fakealt", "beta")])
        runs = [c for c in self.calls if c["kind"] == "run"]
        self.assertEqual([r["task"] for r in runs], [replies[0]["task"], replies[1]["task"]])
        # the same transcript sent to the OTHER model is a different conversation, never a resume
        self.chat(a[:2], "A2", model="fakealt/beta")
        self.assertEqual(self.calls[-1]["kind"], "run")
        self.assertEqual(self.calls[-1]["engine"], "fakealt")

    def test_response_reports_the_route_label_and_id(self):
        self.configure("fakeeng/alpha", "fakealt/beta")
        status, body, _headers = self.post(_turn([], "hi", "fakealt/beta"))
        self.assertEqual(status, 200)
        self.assertEqual(body["model"], "fakealt/beta")
        self.assertEqual(body["neoxider_route"], "fakealt/beta")
        self.assertEqual((self.calls[0]["engine"], self.calls[0]["model"]), ("fakealt", "beta"))
        status, body, _headers = self.post(_turn([], "hi", "gpt-4o"))
        self.assertEqual((status, body["error"]["code"]), (404, "model_not_found"))

    def test_independent_conversations_actually_overlap(self):
        self.configure(max_concurrency=4)
        barrier = threading.Barrier(2, timeout=10)
        active, peak, lock = [0], [0], threading.Lock()

        def answer(kind, engine, model, prompt, name):
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            try:
                barrier.wait()  # only returns if BOTH conversations are inside the CLI at once
                return "parallel ok"
            finally:
                with lock:
                    active[0] -= 1

        self.answer = answer
        results = []
        threads = [threading.Thread(target=lambda u=u: results.append(self.post(_turn([], u))))
                   for u in ("conversation one", "conversation two")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(20)
        self.assertEqual(sorted(r[0] for r in results), [200, 200])
        self.assertEqual(peak[0], 2)
        self.assertEqual(len({c["session"] for c in self.calls}), 2)

    def test_max_concurrency_one_restores_one_turn_at_a_time(self):
        self.configure(max_concurrency=1)
        active, peak, lock = [0], [0], threading.Lock()
        real_sleep = _REAL_SLEEP

        def answer(*_args):
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            real_sleep(0.3)
            with lock:
                active[0] -= 1
            return "serial ok"

        self.answer = answer
        results = []
        threads = [threading.Thread(target=lambda u=u: results.append(self.post(_turn([], u))))
                   for u in ("one", "two", "three")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(20)
        self.assertEqual(sorted(r[0] for r in results), [200, 200, 200])
        self.assertEqual(peak[0], 1)

    def test_same_conversation_turns_never_overlap(self):
        self.configure(max_concurrency=4)
        history = self.chat([], "first")
        gate = threading.Event()
        inside, overlaps, lock = {}, [], threading.Lock()
        real_sleep = _REAL_SLEEP

        def answer(kind, engine, model, prompt, name):
            with lock:
                inside[name] = inside.get(name, 0) + 1
                if inside[name] > 1:
                    overlaps.append(name)
            gate.wait(5)
            real_sleep(0.2)
            with lock:
                inside[name] -= 1
            return "turn ok"

        self.answer = answer
        results = []
        body = _turn(history, "second")  # the SAME continuation sent twice concurrently
        threads = [threading.Thread(target=lambda: results.append(self.post(body))) for _ in range(2)]
        for thread in threads:
            thread.start()
        real_sleep(0.5)
        gate.set()
        for thread in threads:
            thread.join(20)
        self.assertEqual(sorted(r[0] for r in results), [200, 200])
        self.assertEqual(overlaps, [])
        kinds = [c["kind"] for c in self.calls[1:]]
        # one request resumed the conversation; the other waited for it and then (its history no
        # longer being an extension) got its own fresh session instead of racing the first
        self.assertEqual(sorted(kinds), ["reply", "run"])

    def test_capacity_eviction_takes_the_lru_idle_session_and_is_logged(self):
        self.configure(max_sessions=2)
        with mock.patch.object(srv.time, "time", return_value=1000.0) as clock:
            a = self.chat([], "A1")
            session_a = self.calls[-1]["session"]
            clock.return_value += 1
            self.chat([], "B1")
            clock.return_value += 1
            with self.assertLogs(srv.LOG, level=logging.WARNING) as logs:
                self.chat([], "C1")
            self.assertTrue(any("evicted session " + session_a in line and "capacity" in line
                                for line in logs.output))
            self.assertEqual(self.get("/health")["session_count"], 2)
            self.chat(a, "A2")  # A was evicted -> its next turn starts fresh (it was never resumed)
            self.assertEqual(self.calls[-1]["kind"], "run")

    def test_an_active_session_is_never_evicted(self):
        self.configure(max_sessions=1, max_concurrency=4)
        release = threading.Event()
        entered = threading.Event()

        def answer(kind, engine, model, prompt, name):
            if "slow" in prompt:
                entered.set()
                release.wait(10)
            return "ok"

        self.answer = answer
        slow = []
        thread = threading.Thread(target=lambda: slow.append(self.post(_turn([], "slow A1"))))
        thread.start()
        self.assertTrue(entered.wait(10))
        self.chat([], "B1")  # over capacity while A is busy: A must survive
        release.set()
        thread.join(10)
        self.assertEqual(slow[0][0], 200)
        a = [{"role": "user", "content": "slow A1"}, {"role": "assistant", "content": "ok"}]
        self.chat(a, "A2")
        self.assertEqual(self.calls[-1]["kind"], "reply", "A kept its session while it was busy")

    def test_idle_ttl_eviction_is_logged(self):
        self.configure(session_ttl=1)
        with mock.patch.object(srv.time, "time", return_value=1000.0) as clock:
            self.chat([], "A1")
            session, = srv.POOL.list_sessions()
            self.assertFalse(session.busy)
            clock.return_value += 1
            self.assertEqual(srv.POOL.evict(), 0)  # TTL expires strictly after the boundary.
            with self.assertLogs(srv.LOG, level=logging.WARNING) as logs:
                clock.return_value += 1
                self.assertEqual(srv.POOL.evict(), 1)
            self.assertTrue(any("evicted session " + session.id in line and "session-ttl" in line
                                for line in logs.output))
            self.assertTrue(session.closed)
            self.assertEqual(srv.POOL.list_sessions(), [])
            self.assertEqual(srv.POOL.evict(), 0)

    def test_reset_can_target_one_route(self):
        self.configure("fakeeng/alpha", "fakealt/beta")
        self.chat([], "A1", model="fakeeng/alpha")
        self.chat([], "B1", model="fakealt/beta")
        status, body, _headers = self.post({"model": "fakealt/beta"}, path="/v1/reset")
        self.assertEqual((status, body["sessions"]), (200, 1))
        self.assertEqual([s["model"] for s in self.get("/health")["sessions"]], ["fakeeng/alpha"])
        status, body, _headers = self.post({}, path="/v1/reset")
        self.assertEqual(self.get("/health")["session_count"], 0)

    def test_provider_refusal_is_openai_json_with_mapped_status_and_health(self):
        self.configure(retries=1)
        self.answer = lambda *a: ""
        srv.read_meta = lambda name: _meta(category="auth", reason=FREE_TIER_REASON)
        started = time.monotonic()
        status, body, _headers = self.post(_turn([], "hi"))
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(status, 403)
        error = body["error"]
        self.assertEqual((error["type"], error["code"], error["param"], error["retryable"]),
                         ("permission_error", "provider_forbidden", None, False))
        self.assertIn("FreeTierError", error["message"])
        self.assertTrue(error["incident_id"])
        self.assertEqual(len(self.calls), 1, "a 403 must not be retried")
        model = self.get("/health")["models"][0]
        self.assertEqual((model["state"], model["ready"], model["last_error"]["status"]),
                         ("error", False, 403))

    def test_rate_limit_sets_retry_after_header(self):
        self.answer = lambda *a: ""
        srv.read_meta = lambda name: _meta(state="limited", category="quota",
                                           reason="rate limit exceeded, retry after 42s")
        status, body, headers = self.post(_turn([], "hi"))
        self.assertEqual((status, body["error"]["code"]), (429, "rate_limit_exceeded"))
        self.assertEqual(headers.get("Retry-After"), "42")
        self.assertEqual(body["error"]["retry_after_seconds"], 42)

    def test_full_queue_answers_503_with_retry_after(self):
        self.configure(max_concurrency=1, max_queue=0)
        release, entered = threading.Event(), threading.Event()

        def answer(kind, engine, model, prompt, name):
            entered.set()
            release.wait(10)
            return "ok"

        self.answer = answer
        first = []
        thread = threading.Thread(target=lambda: first.append(self.post(_turn([], "one"))))
        thread.start()
        self.assertTrue(entered.wait(10))
        status, body, headers = self.post(_turn([], "two"))
        release.set()
        thread.join(10)
        self.assertEqual((status, body["error"]["code"], body["error"]["retryable"]),
                         (503, "bridge_busy", True))
        self.assertEqual(headers.get("Retry-After"), "5")
        self.assertEqual(first[0][0], 200)

    def test_client_disconnect_cancels_the_cli_run(self):
        observed = threading.Event()
        real_sleep = _REAL_SLEEP

        def answer(kind, engine, model, prompt, name):
            cancel = srv._CTX.cancel
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if cancel is not None and cancel.is_set():
                    observed.set()
                    return ""
                real_sleep(0.05)
            return "never cancelled"

        self.answer = answer
        body = json.dumps(_turn([], "abandon me")).encode("utf-8")
        port = self.server.server_address[1]
        client = socket.create_connection(("127.0.0.1", port), timeout=5)
        client.sendall(b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\n"
                       b"Content-Type: application/json\r\nContent-Length: %d\r\n\r\n" % len(body) + body)
        real_sleep(0.5)
        client.close()
        self.assertTrue(observed.wait(8), "the CLI run must be cancelled when the client leaves")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and srv.POOL.snapshot()["active_slots"]:
            real_sleep(0.05)
        self.assertEqual(srv.POOL.snapshot()["active_slots"], 0)
        health = self.get("/health")
        self.assertTrue(all(not s["resumable"] for s in health["sessions"]))
        # a client that left is not a provider failure: the model's readiness is untouched
        self.assertEqual(health["models"][0]["state"], "unknown")


class ReasoningEffortUnitTests(unittest.TestCase):
    def test_nearest_supported_level_prefers_the_cheaper_on_ties(self):
        self.assertEqual(srv._nearest_effort("high", ["medium", "high"]), "high")
        self.assertEqual(srv._nearest_effort("xhigh", ["medium", "high"]), "high")
        self.assertEqual(srv._nearest_effort("none", ["medium", "high"]), "medium")
        self.assertEqual(srv._nearest_effort("low", ["medium", "high"]), "medium")
        self.assertEqual(srv._nearest_effort("xhigh", ["minimal", "low", "medium", "high", "max"]), "high")
        self.assertEqual(srv._nearest_effort("none", ["low", "medium", "high", "xhigh", "max"]), "low")

    def test_request_field_validation(self):
        self.assertIsNone(srv.requested_effort({}))
        self.assertEqual(srv.requested_effort({"reasoning_effort": "HIGH"}), "high")
        self.assertEqual(srv.requested_effort({"reasoning": {"effort": "low"}}), "low")
        self.assertEqual(srv.requested_effort({"reasoning_effort": "high", "reasoning": {"effort": "low"}}), "high")
        for bad in ("turbo", 3, ""):
            with self.subTest(value=bad), self.assertRaises(srv.RequestValidationError) as raised:
                srv.requested_effort({"reasoning_effort": bad})
            self.assertEqual(raised.exception.param, "reasoning_effort")

    def test_applied_effort_reports_the_provider_default_when_none_is_set(self):
        self.assertEqual(srv.Route("codex", "6-luna", "").applied_effort, "medium")
        self.assertEqual(srv.Route("codex", "6-luna", "high").applied_effort, "high")
        self.assertEqual(srv.Route("claude", "sonnet", "").applied_effort, "high")
        self.assertEqual(srv.Route("claude", "opus5", "").applied_effort, None)


class ReasoningEffortHTTPTests(_LiveHTTPMixin, unittest.TestCase):
    def ask(self, history, user, effort=None, model=None, **extra):
        body = _turn(history, user, model)
        if effort is not None:
            body["reasoning_effort"] = effort
        body.update(extra)
        status, response, _headers = self.post(body)
        return status, response

    def test_request_effort_reaches_the_cli_and_the_response_proves_it(self):
        self.configure("fakeeng/alpha")
        status, body = self.ask([], "hi", effort="high")
        self.assertEqual(status, 200)
        self.assertEqual(self.calls[-1]["effort"], "high")
        meta = body["neoxider"]
        self.assertEqual((meta["reasoning_effort"], meta["reasoning_effort_requested"],
                          meta["reasoning_effort_source"], meta["reasoning_effort_clamped"]),
                         ("high", "high", "request", False))
        self.assertEqual((meta["route"], meta["base_route"]), ("fakeeng/alpha@high", "fakeeng/alpha"))
        self.assertEqual(body["model"], "fakeeng/alpha (high)")
        self.assertEqual(body["neoxider_route"], "fakeeng/alpha@high")
        self.assertEqual(meta["session"], self.calls[-1]["session"])

    def test_configured_effort_is_the_default_when_the_request_omits_it(self):
        self.configure("fakeeng/alpha@low")
        status, body = self.ask([], "hi")
        self.assertEqual(status, 200)
        self.assertEqual(self.calls[-1]["effort"], "low")
        self.assertEqual((body["neoxider"]["reasoning_effort"], body["neoxider"]["reasoning_effort_source"]),
                         ("low", "default"))
        self.configure("fakeeng/alpha", effort="high")
        self.ask([], "again")
        self.assertEqual(self.calls[-1]["effort"], "high")

    def test_a_different_effort_never_resumes_another_efforts_session(self):
        self.configure("fakeeng/alpha")
        history = [{"role": "user", "content": "A1"}]
        status, body = self.ask([], "A1", effort="high")
        history.append({"role": "assistant", "content": body["choices"][0]["message"]["content"]})
        status, _body = self.ask(history, "A2", effort="low")
        self.assertEqual((status, self.calls[-1]["kind"], self.calls[-1]["effort"]), (200, "run", "low"))
        status, _body = self.ask(history, "A2", effort="high")
        self.assertEqual((self.calls[-1]["kind"], self.calls[-1]["effort"]), ("reply", "high"))
        self.assertEqual(self.calls[-1]["task"], self.calls[0]["task"])
        sessions = {s["model"] for s in self.get("/health")["sessions"]}
        self.assertEqual(sessions, {"fakeeng/alpha@high", "fakeeng/alpha@low"})

    def test_unsupported_level_is_clamped_and_reported(self):
        self.configure("fakeeng/alpha")  # stub engine advertises low/high only
        status, body = self.ask([], "hi", effort="xhigh")
        self.assertEqual(status, 200)
        meta = body["neoxider"]
        self.assertEqual((self.calls[-1]["effort"], meta["reasoning_effort"], meta["reasoning_effort_requested"],
                          meta["reasoning_effort_clamped"]), ("high", "high", "xhigh", True))
        self.assertIn("clamped", meta["reasoning_effort_note"])
        status, body = self.ask([], "hi", effort="none")
        self.assertEqual(body["neoxider"]["reasoning_effort"], "low")

    def test_engine_without_effort_support_ignores_it_and_says_so(self):
        self.configure("fakealt/beta")
        history = [{"role": "user", "content": "B1"}]
        status, body = self.ask([], "B1", effort="high")
        self.assertEqual(status, 200)
        self.assertEqual(self.calls[-1]["effort"], "")
        meta = body["neoxider"]
        self.assertEqual((meta["reasoning_effort"], meta["reasoning_effort_supported"]), (None, False))
        self.assertIn("ignored", meta["reasoning_effort_note"])
        self.assertEqual(body["neoxider_route"], "fakealt/beta")
        history.append({"role": "assistant", "content": body["choices"][0]["message"]["content"]})
        self.ask(history, "B2", effort="low")  # nothing differs, so the session is reused safely
        self.assertEqual(self.calls[-1]["kind"], "reply")
        model = self.get("/health")["models"][0]
        self.assertEqual((model["effort_supported"], model["applied_effort"]), (False, None))

    def test_invalid_effort_is_a_400_and_reaches_no_cli(self):
        status, body = self.ask([], "hi", effort="turbo")
        self.assertEqual((status, body["error"]["param"], body["error"]["code"]),
                         (400, "reasoning_effort", "invalid_parameter"))
        self.assertEqual(self.calls, [])

    def test_reasoning_object_and_model_id_suffix_select_the_effort_too(self):
        self.configure("fakeeng/alpha", "fakealt/beta")
        status, body = self.ask([], "hi", reasoning={"effort": "high"}, model="fakeeng/alpha")
        self.assertEqual((status, self.calls[-1]["effort"]), (200, "high"))
        status, body = self.ask([], "hi", model="fakeeng/alpha@low")
        self.assertEqual((status, self.calls[-1]["effort"]), (200, "low"))
        self.assertEqual(body["neoxider"]["reasoning_effort_source"], "model")
        health = {m["id"]: m for m in self.get("/health")["models"]}
        self.assertEqual(health["fakeeng/alpha@high"]["variant_of"], "fakeeng/alpha")
        self.assertEqual(health["fakeeng/alpha@low"]["state"], "ok")
        self.assertNotIn("fakeeng/alpha@high", [m["id"] for m in self.get("/v1/models")["data"]])

    def test_streaming_final_chunk_carries_the_effective_effort(self):
        self.configure("fakeeng/alpha")
        request = urllib.request.Request(
            self.base + "/v1/chat/completions",
            data=json.dumps(dict(_turn([], "stream it"), stream=True, reasoning_effort="high")).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(request, timeout=20) as response:
            lines = response.read().decode("utf-8").splitlines()
        chunks = [json.loads(line[6:]) for line in lines if line.startswith("data: {")]
        final = [c for c in chunks if c["choices"][0]["finish_reason"]]
        self.assertEqual(final[-1]["neoxider"]["reasoning_effort"], "high")
        self.assertEqual(final[-1]["model"], "fakeeng/alpha (high)")
        self.assertIn("data: [DONE]", lines)


class StreamWatchdogTests(unittest.TestCase):
    def setUp(self):
        self.saved = (srv.CFG, srv.LOGDIR, srv.STALL_GRACE_SECONDS, srv._CTX.task,
                      srv._CTX.cancel, srv._CTX.last_run)
        self.logs = tempfile.mkdtemp()
        srv.LOGDIR = self.logs
        srv.STALL_GRACE_SECONDS = 0
        srv._CTX.cancel = None

    def tearDown(self):
        (srv.CFG, srv.LOGDIR, srv.STALL_GRACE_SECONDS, srv._CTX.task,
         srv._CTX.cancel, srv._CTX.last_run) = self.saved
        import shutil
        shutil.rmtree(self.logs, ignore_errors=True)

    SLEEPER = [sys.executable, "-c", "import time; time.sleep(60)"]

    def test_silent_step_is_aborted_by_the_stall_watchdog(self):
        srv.CFG = _cfg(stall_timeout=1)
        srv._CTX.task = "stalled-task"
        with open(os.path.join(self.logs, "stalled-task.log"), "w", encoding="utf-8") as f:
            f.write("header\n")
        started = time.monotonic()
        outcome = srv._run_agent_process(self.SLEEPER, 60, dict(os.environ))
        self.assertTrue(outcome["stalled"])
        self.assertLess(time.monotonic() - started, 15)
        self.assertIsNotNone(outcome["returncode"])
        error = srv.classify_failure_outcome("opencode", {"state": "running"}, outcome)
        self.assertEqual((error.status, error.code), (504, "provider_stalled"))

    def test_progress_keeps_a_slow_step_alive(self):
        srv.CFG = _cfg(stall_timeout=1)
        srv._CTX.task = "busy-task"
        path = os.path.join(self.logs, "busy-task.log")
        writer = ("import sys,time\nfor i in range(12):\n"
                  "    open(sys.argv[1],'a').write('x%d\\n' % i); time.sleep(0.25)\n")
        outcome = srv._run_agent_process([sys.executable, "-c", writer, path], 60, dict(os.environ))
        self.assertFalse(outcome["stalled"])
        self.assertEqual(outcome["returncode"], 0)

    def test_cancel_event_stops_the_tree(self):
        srv.CFG = _cfg(stall_timeout=0)
        srv._CTX.task = None
        cancel = threading.Event()
        srv._CTX.cancel = cancel
        threading.Timer(0.5, cancel.set).start()
        started = time.monotonic()
        outcome = srv._run_agent_process(self.SLEEPER, 60, dict(os.environ))
        self.assertTrue(outcome["cancelled"])
        self.assertLess(time.monotonic() - started, 15)

    def test_stall_timeout_reaches_the_core_silence_watchdog(self):
        srv.CFG = _cfg(stall_timeout=120, timeout=240)
        self.assertEqual(srv._chatonly_env()["AGENT_SILENCE_SEC"], "120")
        srv.CFG = _cfg(stall_timeout=0, timeout=240)
        old = os.environ.pop("AGENT_SILENCE_SEC", None)
        try:
            self.assertNotIn("AGENT_SILENCE_SEC", srv._chatonly_env())
        finally:
            if old is not None:
                os.environ["AGENT_SILENCE_SEC"] = old


class RegistryCommandTests(unittest.TestCase):
    """start --detach on port 0, list, stop: real background processes, no provider calls."""

    def setUp(self):
        self.saved = (srv.LOGDIR, srv.BRIDGES_DIR)
        self.logs = tempfile.mkdtemp()
        srv.LOGDIR = self.logs
        srv.BRIDGES_DIR = os.path.join(self.logs, "bridges")

    def tearDown(self):
        srv.LOGDIR, srv.BRIDGES_DIR = self.saved
        import shutil
        shutil.rmtree(self.logs, ignore_errors=True)

    def run_main(self, *argv):
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            code = srv.main(list(argv))
        return code, out.getvalue()

    def test_detached_start_on_port_zero_is_listed_and_stopped_with_its_tree(self):
        code, out = self.run_main("start", "--detach", "-e", "codex", "-m", "spark", "-p", "0",
                                  "--name", "unit-detach", "--json")
        self.assertEqual(code, 0, out)
        started = json.loads(out)
        port, pid = started["port"], started["pid"]
        try:
            self.assertNotEqual(port, 0)
            self.assertTrue(os.path.exists(os.path.join(srv.BRIDGES_DIR, "bridge-%d.json" % port)))
            self.assertTrue(os.path.exists(started["log"]))
            with urllib.request.urlopen("http://127.0.0.1:%d/health" % port, timeout=5) as response:
                health = json.loads(response.read().decode("utf-8"))
            self.assertEqual(health["instance_id"], started["instance_id"])
            self.assertEqual(health["default_model"], "codex/spark")
            code, out = self.run_main("list", "--json")
            rows = json.loads(out)
            self.assertEqual([(r["port"], r["status"], r["name"]) for r in rows],
                             [(port, "live", "unit-detach")])
            code, out = self.run_main("stop", "unit-detach", "--json")
            result = json.loads(out)["results"][0]
            self.assertTrue(result["stopped"], result)
            self.assertTrue(result["graceful"])
            self.assertFalse(ProcessTreeTimeoutTests._pid_alive(pid))
            self.assertFalse(os.path.exists(os.path.join(srv.BRIDGES_DIR, "bridge-%d.json" % port)))
            code, out = self.run_main("list", "--json")
            self.assertEqual(json.loads(out), [])
        finally:
            if ProcessTreeTimeoutTests._pid_alive(pid):
                from neoxider_agents.process import kill_tree
                kill_tree(pid)

    def test_list_prunes_a_dead_registry_entry_and_stop_reports_unknown_targets(self):
        os.makedirs(srv.BRIDGES_DIR)
        finished = subprocess.Popen([sys.executable, "-c", "pass"])
        finished.wait()
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            free_port = probe.getsockname()[1]
        path = os.path.join(srv.BRIDGES_DIR, "bridge-%d.json" % free_port)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"port": free_port, "pid": finished.pid, "pid_stamp": "1",
                       "instance_id": "deadbeef0000", "started": time.time() - 60,
                       "base_url": "http://127.0.0.1:%d" % free_port}, f)
        code, out = self.run_main("list", "--json")
        self.assertEqual((code, json.loads(out)), (0, []))
        self.assertFalse(os.path.exists(path))
        code, out = self.run_main("stop", "no-such-bridge")
        self.assertEqual(code, 1)
        self.assertIn("no registered bridge matches", out)


class LauncherWiringTests(unittest.TestCase):
    def test_service_launcher_accepts_the_new_subcommands_and_flags(self):
        from neoxider_agents import services
        launched = []

        class FakeProcess:
            pid = 0

            def __init__(self, args, **kwargs):
                launched.append(args)

            def wait(self):
                return 0

            def poll(self):
                return 0

        argvs = (["start", "--detach", "-m", "codex/6-luna", "-m", "opencode/x", "-p", "0", "--check",
                  "--name", "bench", "--max-concurrency", "4", "--engine-limit", "claude=1",
                  "--stall-timeout", "120", "--queue-timeout", "60", "--max-sessions", "16",
                  "--min-free-memory-mb", "2048",
                  "--strict-models", "--json"],
                 ["list", "--json"], ["stop", "all", "--force", "--wait", "5"],
                 ["check", "--models", "codex/6-luna,claude/sonnet", "--allow-any"])
        with mock.patch.object(services.subprocess, "Popen", FakeProcess):
            for argv in argvs:
                self.assertEqual(services.launch_service("openai-server", list(argv)), 0)
            with self.assertRaises(ValueError):
                services.launch_service("openai-server", ["--definitely-unknown"])
        self.assertEqual([args[2:] for args in launched], [list(a) for a in argvs])
        self.assertTrue(all(args[1].endswith("openai_server.py") for args in launched))


if __name__ == "__main__":
    unittest.main()
