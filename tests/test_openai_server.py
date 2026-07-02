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

    def test_one_second_under_the_ttl_is_not_yet_expired(self):
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

    def test_retries_exhausted_returns_last_result_without_raising(self):
        attempts = []

        def fake_run(engine, model, effort, workdir, prompt, name, timeout):
            attempts.append(name)
            return ""

        srv.run_agent = fake_run
        text, calls, _usage = self._call([{"role": "user", "content": "hi"}])
        self.assertEqual(text, "")
        self.assertIsNone(calls)
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

    def test_usage_is_a_flagged_nonzero_estimate(self):
        srv.run_agent = lambda *a, **k: "four token answer here"
        _text, _calls, usage = self._call([{"role": "user", "content": "hi"}])
        self.assertTrue(usage["neoxider_estimated"])
        self.assertGreater(usage["prompt_tokens"], 0)
        self.assertGreater(usage["completion_tokens"], 0)
        self.assertEqual(usage["total_tokens"],
                         usage["prompt_tokens"] + usage["completion_tokens"])


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
        import threading
        name = "tail-test"
        path = os.path.join(self._tmp, name + ".log")
        proc = self._FakeProc()
        got = []

        def writer():
            with open(path, "w", encoding="utf-8", newline="") as f:
                f.write("header stuff\n> PROMPT: hi\n")
                f.flush()
                srv.time.sleep(0.15)
                f.write(srv.OUTPUT_MARKER + "\n")
                f.write("Hel")
                f.flush()
                srv.time.sleep(0.15)
                f.write("lo ж")  # multi-byte char near a flush boundary
                f.flush()
                srv.time.sleep(0.15)
            proc.dead = True

        t = threading.Thread(target=writer)
        t.start()
        srv._tail_task_log(name, proc, timeout=10, on_delta=got.append)
        t.join()
        self.assertEqual("".join(got), "Hello ж")
        self.assertGreaterEqual(len(got), 2)  # arrived in pieces, not one blob


if __name__ == "__main__":
    unittest.main()
