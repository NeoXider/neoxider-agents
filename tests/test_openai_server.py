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
        # both accepted call formats must be advertised, and prose-only must be discouraged
        self.assertIn("tool_calls", out)                 # Format 1 (JSON block)
        self.assertIn("one literal call per line", out)  # Format 2 (name(arg=value))
        self.assertIn("does not count", out)             # prose-describes-an-action warning


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

        def fake_run(args, **kwargs):
            captured.update(kwargs)
            class R:
                pass
            return R()

        real_run, real_read_log = srv.subprocess.run, srv.read_log
        srv.subprocess.run = fake_run
        srv.read_log = lambda name: ""
        try:
            srv.run_agent("codex", "spark", "medium", "/tmp/x", "hi", "t", 60)
        finally:
            srv.subprocess.run, srv.read_log = real_run, real_read_log
        self.assertEqual(captured.get("env", {}).get("AGENT_CHAT_ONLY"), "1")

    def test_reply_agent_passes_chatonly_env_to_subprocess(self):
        captured = {}

        def fake_run(args, **kwargs):
            captured.update(kwargs)
            class R:
                pass
            return R()

        real_run, real_read_log, real_read_meta = srv.subprocess.run, srv.read_log, srv.read_meta
        srv.subprocess.run = fake_run
        srv.read_log = lambda name: ""  # log never grows -> reply_agent returns None, that's fine
        srv.read_meta = lambda name: {"state": "done"}
        try:
            srv.reply_agent("codex", "spark", "medium", "/tmp/x", "t", "hi", 60)
        finally:
            srv.subprocess.run, srv.read_log, srv.read_meta = real_run, real_read_log, real_read_meta
        self.assertEqual(captured.get("env", {}).get("AGENT_CHAT_ONLY"), "1")


class ReplyAgentStaleGuardTests(unittest.TestCase):
    """reply_agent must NOT return the previous answer when a resume appended nothing to the log
    (e.g. `agent.sh reply` died before writing a new block). It returns None instead, so _run
    falls back to a fresh run rather than silently echoing the stale prior answer."""

    def setUp(self):
        self._real_run = srv.subprocess.run
        self._real_read_log = srv.read_log
        self._real_read_meta = srv.read_meta
        srv.subprocess.run = lambda *a, **k: None  # never actually shell out
        srv.read_meta = lambda name: {"state": "done"}  # default: a healthy finished task

    def tearDown(self):
        srv.subprocess.run = self._real_run
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
        # A provider that fails AFTER the reply header still grows the log; the meta-state guard
        # must reject it so _run falls back to a fresh run instead of returning the error block.
        self._grow()
        srv.read_meta = lambda name: {"state": "error"}
        self.assertIsNone(srv.reply_agent("codex", "spark", "medium", "/tmp/x", "t", "hi", 60))

    def test_accepts_waiting_state(self):
        self._grow()
        srv.read_meta = lambda name: {"state": "waiting"}
        self.assertEqual(srv.reply_agent("codex", "spark", "medium", "/tmp/x", "t", "hi", 60),
                         "NEW ANSWER\n")


if __name__ == "__main__":
    unittest.main()
