#!/usr/bin/env python3
"""tests/test_gui.py — zero-dependency regression tests for gui.py's pure-logic pieces.

Philosophy: Python stdlib `unittest` only — no pytest, no third-party test deps, matching this
project's "zero dependencies" philosophy.

gui.py is a single script, not an importable package, so it's loaded via importlib from its
file path with the repo root added to sys.path. Its module-level code only does read-only,
side-effect-free work (constant assignments + globbing providers/*/provider.json and computing
HERE/LOGDIR path strings) -- it does NOT bind a network port or write to LOGDIR at import time
(that only happens inside main(), guarded by `if __name__ == "__main__":`). So a plain import is
safe; we still monkeypatch LOGDIR/PROVIDERS_DIR/LOCALES_DIR to scratch temp dirs before any test
that exercises functions which touch those paths, so tests never read/write the user's real
~/.claude/agent-cli-logs. The port-selection tests (choose_port/is_our_panel) do bind an
ephemeral 127.0.0.1 port of their own — never a fixed one, and never the panel's default 8765.

Run:
    python tests/test_gui.py
    python -m unittest tests.test_gui          (from the repo root)
    python -m unittest discover tests
"""
import importlib.util
import io
import json
import logging
import os
import socket
import sys
import tempfile
import shutil
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
GUI_PATH = os.path.join(REPO_ROOT, "gui.py")


def _load_gui_module():
    """Import gui.py by file path (it's a standalone script, not a package on sys.path)."""
    spec = importlib.util.spec_from_file_location("cli_agents_gui", GUI_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Import once for the whole test module -- module-level code in gui.py is read-only (see
# docstring above), so this does not bind a port or write to the real LOGDIR.
gui = _load_gui_module()


class ToGitBashPathTests(unittest.TestCase):
    def test_windows_forward_slash_drive_path(self):
        self.assertEqual(gui.to_git_bash_path("C:/Git/CoreAI"), "/c/Git/CoreAI")

    def test_windows_backslash_drive_path(self):
        self.assertEqual(gui.to_git_bash_path("C:\\Git\\CoreAI"), "/c/Git/CoreAI")

    def test_already_unix_path_passes_through(self):
        self.assertEqual(gui.to_git_bash_path("/c/Git/CoreAI"), "/c/Git/CoreAI")

    def test_empty_string_passes_through(self):
        self.assertEqual(gui.to_git_bash_path(""), "")

    def test_none_like_falsy_input_passes_through(self):
        # to_git_bash_path does `p or ""` internally, so None is also accepted defensively.
        self.assertEqual(gui.to_git_bash_path(None), "")

    def test_lowercases_drive_letter(self):
        self.assertEqual(gui.to_git_bash_path("D:/Foo/Bar"), "/d/Foo/Bar")


class EffStateTests(unittest.TestCase):
    """eff_state is the ONE liveness truth, mirrored in agent.sh's eff_state(); these tests pin
    the contract that used to differ between the two (CLI said running, GUI said stalled)."""

    def setUp(self):
        # default: no pid recorded -> liveness unknown, i.e. the historical mtime-only behaviour
        self._orig_pid_alive = gui.pid_alive

    def tearDown(self):
        gui.pid_alive = self._orig_pid_alive

    def test_running_with_stale_mtime_and_unknown_pid_becomes_stalled(self):
        nowt = 1_000_000.0
        log_mtime = nowt - (gui.STALE_SEC + 1)  # older than STALE_SEC, liveness unknown -> stalled
        meta = {"state": "running"}
        self.assertEqual(gui.eff_state(meta, log_mtime, nowt), "stalled")

    def test_running_with_dead_pid_is_stalled_even_with_a_fresh_log(self):
        gui.pid_alive = lambda meta: False
        nowt = 1_000_000.0
        self.assertEqual(gui.eff_state({"state": "running"}, nowt - 1, nowt), "stalled")

    def test_running_with_live_pid_and_silent_log_is_idle_not_stalled(self):
        gui.pid_alive = lambda meta: True
        nowt = 1_000_000.0
        log_mtime = nowt - (gui.STALE_SEC + 1)
        self.assertEqual(gui.eff_state({"state": "running"}, log_mtime, nowt), "idle")

    def test_running_with_live_pid_and_fresh_log_is_running(self):
        gui.pid_alive = lambda meta: True
        nowt = 1_000_000.0
        self.assertEqual(gui.eff_state({"state": "running"}, nowt - 3, nowt), "running")

    def test_idle_counts_as_live(self):
        self.assertIn("idle", gui.LIVE_STATES)
        self.assertIn("running", gui.LIVE_STATES)

    def test_pid_alive_returns_none_when_no_pid_recorded(self):
        self.assertIsNone(gui.pid_alive({}))
        self.assertIsNone(gui.pid_alive({"pid": "", "winpid": ""}))
        self.assertIsNone(gui.pid_alive({"winpid": "not-a-number", "pid": "also-not"}))

    def test_pid_alive_says_this_very_process_is_alive(self):
        # os.getpid() is a Windows pid on Windows and a unix pid elsewhere -- which is exactly
        # what agent.sh records in winpid=/pid= respectively.
        meta = {"winpid": str(os.getpid())} if os.name == "nt" else {"pid": str(os.getpid())}
        self.assertTrue(gui.pid_alive(meta))

    def test_pid_alive_says_an_impossible_pid_is_dead(self):
        meta = {"winpid": "4294967294"} if os.name == "nt" else {"pid": "4194303"}
        self.assertIs(gui.pid_alive(meta), False)

    def test_running_with_fresh_mtime_stays_running(self):
        nowt = 1_000_000.0
        log_mtime = nowt - 5  # well within STALE_SEC
        meta = {"state": "running"}
        self.assertEqual(gui.eff_state(meta, log_mtime, nowt), "running")

    def test_running_with_no_log_mtime_stays_running(self):
        # log_mtime falsy (0/None) -> the "stale" branch's `log_mtime and ...` short-circuits.
        nowt = 1_000_000.0
        meta = {"state": "running"}
        self.assertEqual(gui.eff_state(meta, 0, nowt), "running")

    def test_other_states_pass_through_unchanged(self):
        nowt = 1_000_000.0
        for st in ("done", "waiting", "error", "stalled", "?"):
            with self.subTest(state=st):
                meta = {"state": st}
                # even with a very stale mtime, non-"running" states are untouched
                self.assertEqual(gui.eff_state(meta, nowt - 999999, nowt), st)

    def test_missing_state_key_defaults_to_question_mark(self):
        nowt = 1_000_000.0
        self.assertEqual(gui.eff_state({}, nowt, nowt), "?")


class ActivityEmojiTests(unittest.TestCase):
    def test_done_state_maps_to_done_emoji(self):
        self.assertEqual(gui.activity_emoji("irrelevant-task-name", "done"), gui.ACT_BY_STATE["done"])

    def test_waiting_state_maps_to_waiting_emoji(self):
        self.assertEqual(gui.activity_emoji("irrelevant-task-name", "waiting"), gui.ACT_BY_STATE["waiting"])

    def test_error_state_maps_to_error_emoji(self):
        self.assertEqual(gui.activity_emoji("irrelevant-task-name", "error"), gui.ACT_BY_STATE["error"])

    def test_unknown_non_running_state_falls_back_to_bullet(self):
        self.assertEqual(gui.activity_emoji("irrelevant-task-name", "totally-unknown-state"), "•")

    def test_running_state_reads_log_for_activity_hint(self):
        # activity_emoji("running", ...) reads the task's log via read_log(name); point LOGDIR at
        # a scratch dir with a synthetic log whose last non-empty line hints at "editing".
        scratch = tempfile.mkdtemp()
        try:
            orig_logdir = gui.LOGDIR
            gui.LOGDIR = scratch
            try:
                name = "running-task"
                with open(os.path.join(scratch, name + ".log"), "w", encoding="utf-8") as f:
                    f.write("some earlier line\nediting src/main.py now\n")
                self.assertEqual(gui.activity_emoji(name, "running"), "✏️")  # editing emoji
            finally:
                gui.LOGDIR = orig_logdir
        finally:
            shutil.rmtree(scratch, ignore_errors=True)


class TopicEmojiTests(unittest.TestCase):
    def test_title_matching_bug_topic(self):
        self.assertEqual(gui.topic_emoji("please fix bug in the parser"), "\U0001f41b")  # bug emoji

    def test_title_matching_nothing_falls_back_to_default(self):
        self.assertEqual(gui.topic_emoji("something entirely unrelated to any keyword list xyz"), "\U0001f4dd")

    def test_empty_title_falls_back_to_default(self):
        self.assertEqual(gui.topic_emoji(""), "\U0001f4dd")

    def test_none_title_falls_back_to_default(self):
        self.assertEqual(gui.topic_emoji(None), "\U0001f4dd")

    def test_russian_keyword_matches_same_topic(self):
        # TOPIC_RULES intentionally mixes Russian and English keywords in the same bucket.
        self.assertEqual(gui.topic_emoji("исправ баг в модуле"), "\U0001f41b")


class ListLocalesTests(unittest.TestCase):
    """list_locales() globs LOCALES_DIR/*.json -- easily testable by monkeypatching LOCALES_DIR
    to a scratch directory with synthetic locale files."""

    def test_lists_locales_from_scratch_dir(self):
        scratch = tempfile.mkdtemp()
        try:
            with open(os.path.join(scratch, "en.json"), "w", encoding="utf-8") as f:
                json.dump({"_label": "English"}, f)
            with open(os.path.join(scratch, "xx.json"), "w", encoding="utf-8") as f:
                json.dump({"_label": "Xylophonic"}, f)
            # non-json file should be ignored
            with open(os.path.join(scratch, "notes.txt"), "w", encoding="utf-8") as f:
                f.write("not a locale")

            orig = gui.LOCALES_DIR
            gui.LOCALES_DIR = scratch
            try:
                result = gui.list_locales()
            finally:
                gui.LOCALES_DIR = orig

            codes = sorted(r["code"] for r in result)
            self.assertEqual(codes, ["en", "xx"])
            labels = {r["code"]: r["label"] for r in result}
            self.assertEqual(labels["en"], "English")
            self.assertEqual(labels["xx"], "Xylophonic")
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def test_malformed_locale_file_is_skipped_not_raised(self):
        scratch = tempfile.mkdtemp()
        try:
            with open(os.path.join(scratch, "broken.json"), "w", encoding="utf-8") as f:
                f.write("{not valid json")
            orig = gui.LOCALES_DIR
            gui.LOCALES_DIR = scratch
            try:
                result = gui.list_locales()  # must not raise
            finally:
                gui.LOCALES_DIR = orig
            self.assertEqual(result, [])
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def test_missing_locales_dir_returns_empty_list(self):
        orig = gui.LOCALES_DIR
        gui.LOCALES_DIR = os.path.join(tempfile.gettempdir(), "definitely-does-not-exist-xyz-123")
        try:
            self.assertEqual(gui.list_locales(), [])
        finally:
            gui.LOCALES_DIR = orig


class ServeStaticTraversalGuardTests(unittest.TestCase):
    """gui.H._serve_static is a method on the BaseHTTPRequestHandler subclass `H`. It's awkward
    to unit test through a real HTTP request (that needs a live socket/server), but the method
    itself only touches `self._send` (which we can stub) and pure path logic -- it never touches
    self.rfile/self.wfile/etc. directly. So we construct a bare instance via H.__new__(H) (this
    skips BaseHTTPRequestHandler.__init__, which would otherwise try to read a real request from
    a socket) and hand it a fake `self` stand-in with a stub `_send` that records calls, then call
    the *unbound* function `gui.H._serve_static` with that stand-in as `self`. This avoids any
    real socket/server and does not require modifying gui.py.
    """

    def _make_fake_handler(self):
        calls = []

        class FakeHandler:
            def _send(fake_self, code, body, ctype="application/json"):
                calls.append({"code": code, "body": body, "ctype": ctype})

        return FakeHandler(), calls

    def setUp(self):
        self.scratch = tempfile.mkdtemp()
        # legitimate file inside the served root
        with open(os.path.join(self.scratch, "style.css"), "w", encoding="utf-8") as f:
            f.write("body { color: red; }")
        # a "secret" file OUTSIDE the served root, in the parent dir, to target via traversal
        self.outside_dir = tempfile.mkdtemp()
        with open(os.path.join(self.outside_dir, "secret.txt"), "w", encoding="utf-8") as f:
            f.write("top secret")

    def tearDown(self):
        shutil.rmtree(self.scratch, ignore_errors=True)
        shutil.rmtree(self.outside_dir, ignore_errors=True)

    def test_legitimate_path_is_served_200(self):
        fake_self, calls = self._make_fake_handler()
        gui.H._serve_static(fake_self, self.scratch, "style.css")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["code"], 200)
        self.assertIn(b"color: red", calls[0]["body"])

    def test_directory_traversal_via_dotdot_is_rejected_403(self):
        fake_self, calls = self._make_fake_handler()
        # try to escape `scratch` and read a file from its parent via ../
        traversal_rel = "../" + os.path.basename(self.outside_dir) + "/secret.txt"
        gui.H._serve_static(fake_self, self.scratch, traversal_rel)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["code"], 403)

    def test_absolute_path_traversal_is_rejected_403(self):
        fake_self, calls = self._make_fake_handler()
        secret_path = os.path.join(self.outside_dir, "secret.txt")
        gui.H._serve_static(fake_self, self.scratch, secret_path)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["code"], 403)

    def test_missing_file_within_root_is_404(self):
        fake_self, calls = self._make_fake_handler()
        gui.H._serve_static(fake_self, self.scratch, "does-not-exist.css")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["code"], 404)


class IsOurPanelTests(unittest.TestCase):
    """`agent.sh gui` used to treat ANY busy port as "the panel is already running" and open a
    browser tab at it -- even when the port belonged to an unrelated server (a WebSocket one, in
    the wild), which showed up as a broken tab and no panel. is_our_panel() is the identity check
    that now decides between "reuse it" and "move to the next free port"."""

    def _free_port(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        p = s.getsockname()[1]
        s.close()          # closed again immediately: nothing is listening on p
        return p

    def test_nothing_listening_is_not_our_panel(self):
        self.assertFalse(gui.is_our_panel(self._free_port()))

    def test_port_available_agrees_that_a_free_port_is_free(self):
        self.assertTrue(gui.port_available(self._free_port()))

    def test_choose_port_uses_the_asked_port_when_free(self):
        p = self._free_port()
        self.assertEqual(gui.choose_port(p), (p, "asked"))

    def test_choose_port_moves_off_a_port_held_by_a_foreign_server(self):
        """A plain listening socket that never speaks HTTP stands in for the unrelated
        WebSocket server that was found squatting on 8765."""
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        busy = srv.getsockname()[1]
        try:
            port, how = gui.choose_port(busy)
            self.assertEqual(how, "moved")
            self.assertNotEqual(port, busy)
            self.assertGreater(port, busy)
        finally:
            srv.close()


class FmtDurTests(unittest.TestCase):
    """fmt_dur is the single human-duration formatter for the dialog view (per step and per
    tool call): 1.2s / 45s / 3m 20s / 1h 5m; no data -> empty string, never a made-up number."""

    def test_sub_ten_seconds_one_decimal(self):
        self.assertEqual(gui.fmt_dur(1.23), "1.2s")

    def test_whole_seconds_no_decimal(self):
        self.assertEqual(gui.fmt_dur(45), "45s")

    def test_minutes_and_seconds(self):
        self.assertEqual(gui.fmt_dur(200), "3m 20s")

    def test_round_minute_drops_zero_seconds(self):
        self.assertEqual(gui.fmt_dur(180), "3m")

    def test_hours(self):
        self.assertEqual(gui.fmt_dur(3900), "1h 5m")
        self.assertEqual(gui.fmt_dur(3600), "1h")

    def test_rounding_near_minute_boundary(self):
        self.assertEqual(gui.fmt_dur(59.6), "1m")

    def test_none_and_negative_are_empty(self):
        self.assertEqual(gui.fmt_dur(None), "")
        self.assertEqual(gui.fmt_dur(-3), "")
        self.assertEqual(gui.fmt_dur("junk"), "")


CURRENT_LOG = """
========== [run] 2026-08-12 10:00:00 | engine=codex model=sol dir=/c/x ==========
> PROMPT:
fix the tests
---------- output ----------
session id: 019ff
---------- output ----------
Done, all green.
"""

LEGACY_CODEX_LOG = """OpenAI Codex v0.130.0
--------
workdir: C:\\X
--------
user
fix the bug
thinking
I should look at the tests first
codex
Let me check.
exec
rg --files Assets
 succeeded in 1115ms:
Assets/a.cs
codex
Fixed it.
tokens used
12,345
"""


def _jsonl_log(objs):
    out = "\n========== [run] 2026-08-12 10:00:00 | engine=x model=y dir=/c/x ==========\n"
    out += "> PROMPT:\ndo it\n---------- output ----------\n"
    for o in objs:
        out += json.dumps(o) + "\n"
    return out


class ParseDialogTests(unittest.TestCase):
    """parse_dialog turns a raw .log into ordered steps; parse_output_blocks inside it handles
    every engine's flavour and ALWAYS degrades to raw text (never an exception, never empty)."""

    def test_current_format_steps_prompt_and_text(self):
        steps = gui.parse_dialog(CURRENT_LOG, log_mtime=0, now=0)
        self.assertEqual(len(steps), 1)
        st = steps[0]
        self.assertEqual(st["kind"], "run")
        self.assertEqual(st["ts"], "2026-08-12 10:00:00")
        self.assertEqual(st["prompt"], "fix the tests")
        self.assertEqual([b["type"] for b in st["blocks"]], ["text"])
        self.assertIn("session id: 019ff", st["blocks"][0]["text"])  # chrome kept as text
        self.assertIn("Done, all green.", st["blocks"][0]["text"])

    def test_leading_blank_line_creates_no_phantom_step(self):
        # hdr() writes a blank line before the first header; it must not become a 'log' step.
        steps = gui.parse_dialog(CURRENT_LOG, log_mtime=0, now=0)
        self.assertEqual([s["kind"] for s in steps], ["run"])

    def test_step_duration_from_next_header_and_mtime(self):
        log = CURRENT_LOG + """
========== [reply] 2026-08-12 10:05:00 | engine=codex model=sol dir=/c/x ==========
> ANSWER:
continue
---------- output ----------
More work done.
"""
        steps = gui.parse_dialog(log, log_mtime=gui._ts_epoch("2026-08-12 10:09:00"), now=0)
        self.assertEqual([s["kind"] for s in steps], ["run", "reply"])
        self.assertEqual(steps[0]["duration"], "5m")          # until the reply's header
        self.assertEqual(steps[1]["duration"], "4m")          # final step: until log mtime
        self.assertEqual(steps[1]["prompt"], "continue")
        self.assertEqual(steps[1]["prompt_label"], "ANSWER")

    def test_no_timestamps_means_no_duration(self):
        steps = gui.parse_dialog("just some raw text\n", log_mtime=0, now=0)
        self.assertEqual(steps[0]["duration"], "")
        self.assertIsNone(steps[0]["duration_s"])

    def test_legacy_codex_plaintext_blocks(self):
        steps = gui.parse_dialog(LEGACY_CODEX_LOG, log_mtime=0, now=0)
        self.assertEqual(len(steps), 1)
        st = steps[0]
        self.assertEqual(st["kind"], "log")  # no step headers in a legacy log
        self.assertEqual(st["prompt"], "fix the bug")  # the 'user' block becomes the prompt
        types = [b["type"] for b in st["blocks"]]
        self.assertEqual(types, ["thinking", "text", "tool", "text"])
        tool = st["blocks"][2]
        self.assertEqual(tool["name"], "exec")
        self.assertEqual(tool["arg"], "rg --files Assets")
        self.assertIn("Assets/a.cs", tool["result"])
        self.assertEqual(tool["dur_ms"], 1115)
        self.assertEqual(tool["duration"], "1.1s")
        # banner chrome and the 'tokens used' block are dropped
        blob = json.dumps(st, ensure_ascii=False)
        self.assertNotIn("OpenAI Codex v0.130.0", blob)
        self.assertNotIn("12,345", blob)

    def test_legacy_codex_wall_time_duration(self):
        self.assertEqual(gui._dur_ms_from_result("...output...\nWall time: 63.5 seconds"), 63500)

    def test_codex_jsonl_stream(self):
        log = _jsonl_log([
            {"type": "thread.started", "thread_id": "abc"},
            {"type": "item.completed", "item": {"type": "reasoning", "text": "thinking hard"}},
            {"type": "item.completed", "item": {"type": "command_execution", "command": "ls -la",
                                                "aggregated_output": "file1\nfile2", "exit_code": 0}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "Done."}},
        ])
        blocks = gui.parse_dialog(log, log_mtime=0, now=0)[0]["blocks"]
        self.assertEqual([b["type"] for b in blocks], ["thinking", "tool", "text"])
        self.assertEqual(blocks[1]["name"], "exec")
        self.assertEqual(blocks[1]["arg"], "ls -la")
        self.assertEqual(blocks[1]["result"], "file1\nfile2")
        blob = json.dumps(blocks)
        self.assertNotIn("abc", blob)  # thread.started is chrome, not rendered

    def test_kimi_jsonl_stream_tool_result_attached(self):
        log = _jsonl_log([
            {"role": "meta", "type": "session.resume_hint", "session_id": "ses_1"},
            {"role": "assistant", "content": "I will run it.",
             "tool_calls": [{"function": {"name": "Bash", "arguments": '{"cmd":"ls"}'}}]},
            {"role": "tool", "content": "file1"},
            {"role": "assistant", "content": "All done"},
        ])
        blocks = gui.parse_dialog(log, log_mtime=0, now=0)[0]["blocks"]
        self.assertEqual([b["type"] for b in blocks], ["text", "tool", "text"])
        self.assertEqual(blocks[1]["name"], "Bash")
        self.assertEqual(blocks[1]["result"], "file1")

    def test_claude_stream_json_envelope(self):
        log = _jsonl_log([
            {"type": "system", "subtype": "init"},
            {"type": "assistant", "message": {"content": [
                {"type": "thinking", "thinking": "hmm"},
                {"type": "text", "text": "reading file"},
                {"type": "tool_use", "name": "Read", "input": {"file_path": "/x.py"}}]}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "content": "file body"}]}},
            {"type": "result", "subtype": "success"},
        ])
        blocks = gui.parse_dialog(log, log_mtime=0, now=0)[0]["blocks"]
        self.assertEqual([b["type"] for b in blocks], ["thinking", "text", "tool"])
        self.assertEqual(blocks[2]["name"], "Read")
        self.assertEqual(blocks[2]["result"], "file body")

    def test_unrecognised_format_falls_back_to_raw_text(self):
        junk = "\x00\x01 random\nbinary junk {{{\nnot json at all"
        steps = gui.parse_dialog(junk, log_mtime=0, now=0)  # must not raise
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["kind"], "log")
        self.assertEqual([b["type"] for b in steps[0]["blocks"]], ["text"])
        self.assertIn("binary junk", steps[0]["blocks"][0]["text"])

    def test_json_looking_prose_is_not_misparsed_as_a_stream(self):
        # an answer that merely CONTAINS a json line must stay plain text
        log = "\n========== [run] 2026-08-12 10:00:00 | e ==========\n> PROMPT:\np\n---------- output ----------\n"
        log += 'Here is the config: {"foo": 1, "bar": [2]}\nDone.\n'
        blocks = gui.parse_dialog(log, log_mtime=0, now=0)[0]["blocks"]
        self.assertEqual([b["type"] for b in blocks], ["text"])
        self.assertIn('"foo": 1', blocks[0]["text"])

    def test_empty_log_gives_no_steps(self):
        self.assertEqual(gui.parse_dialog("", log_mtime=0, now=0), [])
        self.assertEqual(gui.parse_dialog("\n\n", log_mtime=0, now=0), [])

    def test_parse_output_blocks_never_raises_on_none_like_input(self):
        self.assertEqual(gui.parse_output_blocks(""), [])


class DialogPayloadTests(unittest.TestCase):
    """dialog_payload backs GET /api/dialog: pagination over steps (default = the LAST
    _DIALOG_STEP_LIMIT), per-block size caps in the default payload, full=1 lifts both."""

    def setUp(self):
        self.scratch = tempfile.mkdtemp()
        self._orig = gui.LOGDIR
        gui.LOGDIR = self.scratch
        gui._DIALOG_CACHE.clear()

    def tearDown(self):
        gui.LOGDIR = self._orig
        gui._DIALOG_CACHE.clear()
        shutil.rmtree(self.scratch, ignore_errors=True)

    def _write_task(self, name, log):
        with open(os.path.join(self.scratch, name + ".log"), "w", encoding="utf-8") as f:
            f.write(log)
        with open(os.path.join(self.scratch, name + ".meta"), "w", encoding="utf-8") as f:
            f.write("engine=codex\nmodel=sol\nstate=done\n")

    def test_payload_fields_and_single_step(self):
        self._write_task("t1", CURRENT_LOG)
        p = gui.dialog_payload("t1")
        self.assertEqual(p["name"], "t1")
        self.assertEqual(p["engine"], "codex")
        self.assertEqual(p["state"], "done")
        self.assertEqual(p["total_steps"], 1)
        self.assertFalse(p["has_more"])
        self.assertEqual(p["steps"][0]["i"], 0)

    def test_default_payload_is_last_n_steps_with_has_more(self):
        log = ""
        for i in range(gui._DIALOG_STEP_LIMIT + 5):
            log += ("\n========== [run] 2026-08-12 10:%02d:00 | e ==========\n"
                    "> PROMPT:\nstep %d\n---------- output ----------\nout %d\n" % (i % 60, i, i))
        self._write_task("t2", log)
        p = gui.dialog_payload("t2")
        self.assertEqual(p["total_steps"], gui._DIALOG_STEP_LIMIT + 5)
        self.assertEqual(len(p["steps"]), gui._DIALOG_STEP_LIMIT)
        self.assertTrue(p["has_more"])
        self.assertEqual(p["offset"], 5)
        self.assertEqual(p["steps"][0]["prompt"], "step 5")  # the OLDEST shown is step 5
        pf = gui.dialog_payload("t2", full=True)
        self.assertEqual(len(pf["steps"]), gui._DIALOG_STEP_LIMIT + 5)
        self.assertFalse(pf["has_more"])
        self.assertEqual(pf["steps"][0]["prompt"], "step 0")

    def test_default_payload_caps_huge_blocks_full_does_not(self):
        big = "x" * (gui._DIALOG_BLOCK_CAP + 500)
        log = ("\n========== [run] 2026-08-12 10:00:00 | e ==========\n"
               "> PROMPT:\np\n---------- output ----------\n" + big + "\n")
        self._write_task("t3", log)
        p = gui.dialog_payload("t3")
        b = p["steps"][0]["blocks"][0]
        self.assertTrue(b["truncated"])
        self.assertEqual(len(b["text"]), gui._DIALOG_BLOCK_CAP)
        pf = gui.dialog_payload("t3", full=True)
        self.assertEqual(len(pf["steps"][0]["blocks"][0]["text"]), len(big))
        self.assertNotIn("truncated", pf["steps"][0]["blocks"][0])

    def test_missing_log_is_empty_payload_not_exception(self):
        p = gui.dialog_payload("no-such-task")
        self.assertEqual(p["total_steps"], 0)
        self.assertEqual(p["steps"], [])

    def test_endpoint_serves_dialog_json(self):
        """H.do_GET for /api/dialog, driven through a fake handler (no real socket)."""
        self._write_task("t4", CURRENT_LOG)
        sent = {}

        class FakeHandler:
            path = "/api/dialog?task=t4"
            client_address = ("127.0.0.1", 4242)
            headers = {}
            _reject_unauthorized = gui.H._reject_unauthorized

            def _send(self, code, body, ctype="application/json"):
                sent["code"] = code
                sent["body"] = body

        gui.H.do_GET(FakeHandler())
        self.assertEqual(sent["code"], 200)
        data = json.loads(sent["body"])
        self.assertEqual(data["name"], "t4")
        self.assertEqual(data["steps"][0]["prompt"], "fix the tests")

    def test_endpoint_requires_task_param(self):
        sent = {}

        class FakeHandler:
            path = "/api/dialog"
            client_address = ("127.0.0.1", 4242)
            headers = {}
            _reject_unauthorized = gui.H._reject_unauthorized

            def _send(self, code, body, ctype="application/json"):
                sent["code"] = code
                sent["body"] = body

        gui.H.do_GET(FakeHandler())
        self.assertEqual(sent["code"], 400)


class DoctorTests(unittest.TestCase):
    """Doctor snapshots are structured, cache-first, and never replace good data with errors."""

    def setUp(self):
        self.scratch = tempfile.mkdtemp()
        self.orig_cache = dict(gui._CACHE)
        self.orig_path = gui.DOCTOR_CACHE_FILE
        self.orig_start = gui._start_doctor_refresh
        self.orig_run = gui.run_sync
        gui._CACHE.clear()
        gui.DOCTOR_CACHE_FILE = os.path.join(self.scratch, "doctor.json")

    def tearDown(self):
        gui._CACHE.clear(); gui._CACHE.update(self.orig_cache)
        gui.DOCTOR_CACHE_FILE = self.orig_path
        gui._start_doctor_refresh = self.orig_start
        gui.run_sync = self.orig_run
        shutil.rmtree(self.scratch, ignore_errors=True)

    def _payload(self):
        return {"generated_at": 1, "raw": "raw doctor", "deep_engines": ["codex"], "engines": [
            {"engine": "codex", "version": "1", "available": True, "login": "ok", "state": "ok",
             "limits": {"plan_type": "pro", "primary": {"used_percent": 42, "window_minutes": 300,
                        "resets_at": 2_000_000_000}}, "note": ""},
            {"engine": "claude", "version": "2", "available": True, "login": "CLI ok", "state": "ok",
             "limits": None, "usage": {"source": "local_transcripts", "estimated": True,
                       "windows": [{"label": "5h", "input_output_tokens": 1200, "cache_tokens": 3400}]},
             "note": "Local transcript estimate."}]}

    def test_parses_legacy_doctor_text_into_engine_and_limit_rows(self):
        got = gui.parse_doctor_text("=== engines (CLI) ===\n  codex     ok   1.0\n  codex     auth ok\n"
                                    "=== codex rate limits (latest session) ===\n"
                                    "  primary   [####------]   42% (window 5h) resets in 1h02m\n")
        self.assertEqual(got["engines"][0]["engine"], "codex")
        self.assertEqual(got["engines"][0]["limits"]["windows"][0]["used_percent"], 42.0)

    def test_cache_first_loads_persistent_snapshot_and_starts_refresh_without_waiting(self):
        hit = {"value": self._payload(), "at": 1}
        gui._save_doctor_cache(hit)
        calls = []
        gui._start_doctor_refresh = lambda force=False: calls.append(force) or True
        got = gui.doctor_cached_only(refresh=True)
        self.assertTrue(got["cached"])
        self.assertEqual(got["engines"][0]["engine"], "codex")
        self.assertEqual(calls, [False])

    def test_failed_refresh_keeps_last_good_raw_snapshot(self):
        gui._CACHE["doctor"] = {"value": self._payload(), "at": 0}
        gui.run_sync = lambda args, timeout=0: "error: Command timed out"
        raw, cached = gui.doctor_text(force=True)
        self.assertTrue(cached)
        self.assertEqual(raw, "raw doctor")

    def test_doctor_frontend_renders_structured_limits_and_keeps_raw_toggle(self):
        with open(os.path.join(REPO_ROOT, "static", "modals.js"), encoding="utf-8") as f:
            js = f.read()
        self.assertIn("doctor-limit", js)
        self.assertIn("doctor-engine", js)
        self.assertIn("doctor-raw", js)


class LanTokenTests(unittest.TestCase):
    """`gui [port] [--lan] [--token SECRET]`. The panel starts full-auto subagents, so binding it
    to the network without a token must be impossible, not merely discouraged."""

    def test_defaults_are_loopback_port_8765_no_token(self):
        self.assertEqual(gui.parse_argv([], {}), (8765, "127.0.0.1", ""))

    def test_positional_port_wins_over_env(self):
        self.assertEqual(gui.parse_argv(["9001"], {"AGENT_GUI_PORT": "8100"})[0], 9001)

    def test_env_port_used_when_no_argument(self):
        self.assertEqual(gui.parse_argv([], {"AGENT_GUI_PORT": "8100"})[0], 8100)

    def test_lan_without_a_token_refuses_to_start(self):
        with self.assertRaises(SystemExit) as ctx:
            gui.parse_argv(["--lan"], {})
        self.assertIn("token", str(ctx.exception).lower())

    def test_lan_with_a_token_binds_all_interfaces(self):
        self.assertEqual(gui.parse_argv(["8765", "--lan", "--token", "s3cret"], {}),
                         (8765, "0.0.0.0", "s3cret"))

    def test_token_can_come_from_the_environment(self):
        self.assertEqual(gui.parse_argv(["--lan"], {"AGENT_GUI_TOKEN": "envkey"})[2], "envkey")

    def test_env_host_also_needs_a_token(self):
        with self.assertRaises(SystemExit):
            gui.parse_argv([], {"AGENT_GUI_HOST": "0.0.0.0"})

    def test_localhost_flag_overrides_env_host_and_needs_no_token(self):
        self.assertEqual(gui.parse_argv(["--localhost"], {"AGENT_GUI_HOST": "0.0.0.0"}),
                         (8765, "127.0.0.1", ""))

    def test_unknown_argument_is_an_explicit_error(self):
        with self.assertRaises(SystemExit):
            gui.parse_argv(["--nope"], {})


class StartBridgeTests(unittest.TestCase):
    """The API tab must be able to protect a bridge it exposes, and must say so when it doesn't."""

    def setUp(self):
        self.spawned = []
        self._orig_spawn, self._orig_free = gui.spawn, gui.port_available
        gui.spawn = lambda args, terminal=False: self.spawned.append(list(args))
        gui.port_available = lambda port, host="0.0.0.0": True

    def tearDown(self):
        gui.spawn, gui.port_available = self._orig_spawn, self._orig_free

    def test_api_key_is_forwarded_to_the_bridge(self):
        r = gui.start_bridge({"engine": "claude", "port": 8801, "api_key": " s3cret "})
        self.assertIn("--api-key", self.spawned[0])
        self.assertEqual(self.spawned[0][self.spawned[0].index("--api-key") + 1], "s3cret")
        self.assertTrue(r["auth"])
        self.assertFalse(r["unprotected_lan"])

    def test_no_key_means_no_flag_and_stays_the_open_bridge(self):
        r = gui.start_bridge({"engine": "claude", "port": 8801, "localhost": True})
        self.assertNotIn("--api-key", self.spawned[0])
        self.assertFalse(r["auth"])
        self.assertFalse(r["unprotected_lan"])  # loopback without a key is the historic default

    def test_lan_without_a_key_is_flagged_back_to_the_panel(self):
        r = gui.start_bridge({"engine": "claude", "port": 8801, "localhost": False})
        self.assertIn("--lan", self.spawned[0])
        self.assertTrue(r["unprotected_lan"])


class GuiAuthTests(unittest.TestCase):
    def setUp(self):
        self._orig = gui.GUI_TOKEN

    def tearDown(self):
        gui.GUI_TOKEN = self._orig

    def test_loopback_forms_recognised(self):
        for a in ("127.0.0.1", "127.1.2.3", "::1", "::ffff:127.0.0.1"):
            self.assertTrue(gui.is_loopback(a), a)
        for a in ("192.168.1.5", "10.0.0.2", "", "::ffff:10.0.0.2"):
            self.assertFalse(gui.is_loopback(a), a)

    def test_no_token_configured_means_open_panel(self):
        gui.GUI_TOKEN = ""
        self.assertTrue(gui.gui_authorized("192.168.1.5", {}, "/api/tasks"))

    def test_loopback_never_needs_the_token(self):
        gui.GUI_TOKEN = "s3cret"
        self.assertTrue(gui.gui_authorized("127.0.0.1", {}, "/api/tasks"))

    def test_network_client_without_the_token_is_rejected(self):
        gui.GUI_TOKEN = "s3cret"
        self.assertFalse(gui.gui_authorized("192.168.1.5", {}, "/api/tasks"))
        self.assertFalse(gui.gui_authorized("192.168.1.5", {}, "/api/tasks?token=nope"))

    def test_token_accepted_from_query_header_or_cookie(self):
        gui.GUI_TOKEN = "s3cret"
        self.assertTrue(gui.gui_authorized("192.168.1.5", {}, "/?token=s3cret"))
        self.assertTrue(gui.gui_authorized("192.168.1.5", {"X-Agent-Token": "s3cret"}, "/api/tasks"))
        self.assertTrue(gui.gui_authorized(
            "192.168.1.5", {"Cookie": "theme=dark; agent_gui_token=s3cret"}, "/api/tasks"))

    def test_run_endpoint_is_gated_too(self):
        """/api/run spawns a real subagent -- the whole reason the token exists."""
        gui.GUI_TOKEN = "s3cret"
        self.assertFalse(gui.gui_authorized("192.168.1.5", {}, "/api/run"))


class WaitTimeoutAndPortTests(unittest.TestCase):
    """Untrusted /api/wait timeouts stay finite in [0, cap]; bridge ports stay real ports."""

    def test_wait_timeout_garbage_falls_back_to_default(self):
        for v in (None, "", "abc", [], object()):
            self.assertEqual(gui.clamp_wait_timeout(v), gui.DEFAULT_WAIT_TIMEOUT, repr(v))

    def test_wait_timeout_is_clamped_to_finite_0_300(self):
        self.assertEqual(gui.WAIT_TIMEOUT_CAP, 300.0)
        self.assertEqual(gui.clamp_wait_timeout("-1"), 0.0)
        self.assertEqual(gui.clamp_wait_timeout("12.5"), 12.5)
        self.assertEqual(gui.clamp_wait_timeout("10e9"), 300.0)
        self.assertEqual(gui.clamp_wait_timeout("inf"), 300.0)
        self.assertEqual(gui.clamp_wait_timeout("-inf"), 0.0)
        self.assertEqual(gui.clamp_wait_timeout("nan"), gui.DEFAULT_WAIT_TIMEOUT)

    def test_coerce_port_accepts_only_1_65535(self):
        self.assertEqual(gui.coerce_port("8801"), 8801)
        for bad in (None, "", 0, -1, 65536, "abc"):
            self.assertIsNone(gui.coerce_port(bad), repr(bad))
        self.assertEqual(gui.coerce_port("junk", 99), 99)

    def test_start_bridge_rejects_a_bad_port_without_spawning(self):
        spawned = []
        orig = gui.spawn
        gui.spawn = lambda args, terminal=False: spawned.append(args)
        try:
            r = gui.start_bridge({"engine": "codex", "port": "70000"})
        finally:
            gui.spawn = orig
        self.assertIn("error", r)
        self.assertFalse(spawned)


class StopBridgeTests(unittest.TestCase):
    """Fail-closed stopping: kill_pid runs only when the registry record and the live /health
    carry the same non-empty instance_id. Unreachable /health never kills: a held port means
    alive-but-unverifiable -> error + registry kept; a free port proves dead -> stale prune."""

    PID = 4321

    def setUp(self):
        self.scratch = tempfile.mkdtemp()
        self.orig = (gui.BRIDGES_DIR, gui._bridge_health, gui.kill_pid)
        gui.BRIDGES_DIR = self.scratch
        self.killed = []
        gui.kill_pid = lambda pid: self.killed.append(pid) or True

    def tearDown(self):
        (gui.BRIDGES_DIR, gui._bridge_health, gui.kill_pid) = self.orig
        shutil.rmtree(self.scratch, ignore_errors=True)

    def _write_rec(self, port, **kw):
        rec = {"pid": self.PID, "base_url": "http://127.0.0.1:%d" % port,
               "instance_id": "IID-%d" % port}
        rec.update(kw)
        with open(os.path.join(self.scratch, "bridge-%d.json" % port), "w",
                  encoding="utf-8") as f:
            json.dump(rec, f)

    def _rec_path(self, port):
        return os.path.join(self.scratch, "bridge-%d.json" % port)

    def _set(self, health):
        gui._bridge_health = lambda url: health

    def _free_port(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        p = s.getsockname()[1]
        s.close()
        return p

    def _hold(self, port):
        """Hold <port> the way a live bridge would (any interface)."""
        s = socket.socket()
        s.bind(("0.0.0.0", int(port)))
        s.listen(1)
        self.addCleanup(s.close)
        return s

    def test_free_port_with_unreachable_health_prunes_the_stale_registry(self):
        port = self._free_port()
        self._write_rec(port)
        self._set(None)   # nothing listening -> the recorded process is provably gone
        r = gui.stop_bridge(port)
        self.assertTrue(r["ok"])
        self.assertFalse(r["killed"])
        self.assertTrue(r["stale_registry"])
        self.assertEqual(self.killed, [])
        self.assertFalse(os.path.exists(self._rec_path(port)))

    def test_busy_bridge_with_unreachable_health_preserves_file_and_errors(self):
        port = self._free_port()
        self._write_rec(port)
        self._hold(port)
        self._set(None)   # wedged bridge: port held but /health does not answer
        r = gui.stop_bridge(port)
        self.assertFalse(r["ok"])
        self.assertFalse(r["killed"])
        self.assertFalse(r.get("stale_registry"))
        self.assertIn("retry", r["error"])
        self.assertEqual(self.killed, [])
        self.assertTrue(os.path.exists(self._rec_path(port)))

    def test_live_bridge_without_publishable_identity_fails_closed_and_keeps_file(self):
        port = self._free_port()
        self._write_rec(port, instance_id="")
        self._set({"instance_id": ""})
        r = gui.stop_bridge(port)
        self.assertFalse(r["ok"])
        self.assertFalse(r["killed"])
        self.assertEqual(self.killed, [])
        self.assertTrue(os.path.exists(self._rec_path(port)))

    def test_live_bridge_with_different_identity_never_kills(self):
        port = self._free_port()
        self._write_rec(port)
        self._set({"instance_id": "someone-else"})
        r = gui.stop_bridge(port)
        self.assertTrue(r["ok"])
        self.assertFalse(r["killed"])
        self.assertTrue(r.get("stale_registry"))
        self.assertEqual(self.killed, [])
        self.assertFalse(os.path.exists(self._rec_path(port)))

    def test_live_bridge_without_health_identity_fails_closed_and_keeps_file(self):
        port = self._free_port()
        self._write_rec(port)
        self._set({})   # live /health carries no instance_id
        r = gui.stop_bridge(port)
        self.assertFalse(r["ok"])
        self.assertFalse(r["killed"])
        self.assertEqual(self.killed, [])
        self.assertTrue(os.path.exists(self._rec_path(port)))

    def test_matching_identity_kills_and_removes_the_registry_file(self):
        port = self._free_port()
        self._write_rec(port)
        self._set({"instance_id": "IID-%d" % port})
        r = gui.stop_bridge(port)
        self.assertTrue(r["ok"])
        self.assertTrue(r["killed"])
        self.assertEqual(self.killed, [self.PID])
        self.assertFalse(os.path.exists(self._rec_path(port)))

    def test_kill_pid_failure_keeps_registry_and_fails_closed(self):
        port = self._free_port()
        self._write_rec(port)
        self._set({"instance_id": "IID-%d" % port})
        gui.kill_pid = lambda pid: self.killed.append(pid) or False
        r = gui.stop_bridge(port)
        self.assertFalse(r["ok"])
        self.assertFalse(r["killed"])
        self.assertFalse(r.get("stale_registry"))
        self.assertIn("did not terminate", r["error"])
        self.assertEqual(self.killed, [self.PID])
        self.assertTrue(os.path.exists(self._rec_path(port)))

    def test_missing_pid_keeps_registry_and_fails_closed(self):
        port = self._free_port()
        self._write_rec(port, pid=None)
        self._set({"instance_id": "IID-%d" % port})
        r = gui.stop_bridge(port)
        self.assertFalse(r["ok"])
        self.assertFalse(r["killed"])
        self.assertFalse(r.get("stale_registry"))
        self.assertIn("no usable process id", r["error"])
        self.assertEqual(self.killed, [])
        self.assertTrue(os.path.exists(self._rec_path(port)))

    def test_stale_expected_instance_id_refuses_without_touching_anything(self):
        port = self._free_port()
        self._write_rec(port)
        self._set({"instance_id": "IID-%d" % port})
        r = gui.stop_bridge(port, expected_instance_id="outdated")
        self.assertFalse(r["ok"])
        self.assertIn("mismatch", r["error"])
        self.assertEqual(self.killed, [])
        self.assertTrue(os.path.exists(self._rec_path(port)))

    def test_expected_instance_id_against_a_record_missing_one_is_a_mismatch(self):
        port = self._free_port()
        self._write_rec(port, instance_id=None)
        self._set({"instance_id": "whatever"})
        r = gui.stop_bridge(port, expected_instance_id="seen-in-the-panel")
        self.assertFalse(r["ok"])
        self.assertIn("mismatch", r["error"])
        self.assertEqual(self.killed, [])

    def test_expected_instance_id_equal_to_the_registry_kills_normally(self):
        port = self._free_port()
        self._write_rec(port)
        self._set({"instance_id": "IID-%d" % port})
        r = gui.stop_bridge(port, expected_instance_id="IID-%d" % port)
        self.assertTrue(r["killed"])
        self.assertEqual(self.killed, [self.PID])

    def test_bad_port_is_refused(self):
        self._set(None)
        self.assertFalse(gui.stop_bridge("not-a-port")["ok"])

    def test_nonpositive_pid_fails_closed_and_keeps_registry(self):
        """Identity matches, but the recorded pid is unusable (0): the real kill_pid must
        refuse it, stop_bridge must fail closed, and the registry must survive for a retry."""
        port = self._free_port()
        self._write_rec(port, pid=0)
        self._set({"instance_id": "IID-%d" % port})
        real_kill_pid = self.orig[2]
        gui.kill_pid = real_kill_pid
        calls = []

        def no_run(*a, **k):
            calls.append("subprocess.run")
            raise AssertionError("backend primitive must not be called")

        orig_run = gui.subprocess.run
        gui.subprocess.run = no_run
        try:
            r = gui.stop_bridge(port, expected_instance_id="IID-%d" % port)
        finally:
            gui.subprocess.run = orig_run
        self.assertFalse(r["ok"])
        self.assertFalse(r["killed"])
        self.assertEqual(calls, [])
        self.assertTrue(os.path.exists(self._rec_path(port)))

    def test_bool_or_float_recorded_pid_fails_closed_and_keeps_registry(self):
        """Matching identity, but the recorded pid is a bool/float: kill_pid must refuse the
        type confusion before any backend call, stop_bridge must fail closed, and the registry
        must survive for a retry."""
        for bad_pid in (True, 1.5):
            port = self._free_port()
            self._write_rec(port, pid=bad_pid)
            self._set({"instance_id": "IID-%d" % port})
            gui.kill_pid = self.orig[2]  # the REAL kill_pid
            calls = []

            def no_run(*a, **k):
                calls.append("subprocess.run")
                raise AssertionError("backend primitive must not be called")

            orig_run = gui.subprocess.run
            gui.subprocess.run = no_run
            try:
                r = gui.stop_bridge(port, expected_instance_id="IID-%d" % port)
            finally:
                gui.subprocess.run = orig_run
            self.assertFalse(r["ok"])
            self.assertFalse(r["killed"])
            self.assertEqual(calls, [])
            self.assertTrue(os.path.exists(self._rec_path(port)))


class KillPidNonPositiveTests(unittest.TestCase):
    """pid <= 0 -- numeric or string -- is a process-group / broadcast signal target on POSIX,
    so kill_pid must reject it before subprocess.run or os.kill is ever reached."""

    def _assert_rejected_before_backend(self, bad_pid):
        calls = []

        def no_run(*a, **k):
            calls.append("subprocess.run")
            return types.SimpleNamespace(returncode=1)

        orig_run, orig_kill = gui.subprocess.run, os.kill

        def no_kill(pid, sig=None):
            calls.append("os.kill")
            return None

        gui.subprocess.run = no_run
        try:
            os.kill = no_kill  # patch the primitive itself, not os.name routing
            self.assertFalse(gui.kill_pid(bad_pid))
        finally:
            gui.subprocess.run = orig_run
            os.kill = orig_kill
        self.assertEqual(calls, [])

    def test_numeric_zero_and_minus_one_are_refused(self):
        for bad in (0, -1):
            self._assert_rejected_before_backend(bad)

    def test_string_zero_and_minus_one_are_refused(self):
        for bad in ("0", "-1"):
            self._assert_rejected_before_backend(bad)

    def test_malformed_values_are_refused_without_touching_a_backend(self):
        for bad in (None, "", "   ", "abc", "12x", [], {}):
            self._assert_rejected_before_backend(bad)

    def test_refusal_of_nonpositive_pid_logs_a_sanitized_warning(self):
        with self.assertLogs(gui.LOG, level=logging.WARNING) as cm:
            self.assertFalse(gui.kill_pid("-1"))
        msg = cm.records[0].getMessage()
        self.assertIn("non-positive", msg)
        self.assertIn("-1", msg)


class KillPidTypeStrictnessTests(unittest.TestCase):
    """A pid must be a positive int or an integer-form string. bool and float are rejected
    BEFORE int coercion: JSON true would coerce to pid 1 and 1.9/1.0 would truncate to 1,
    so kill_pid must refuse them before subprocess.run or os.kill is ever reached."""

    def _assert_rejected_before_backend(self, bad_pid):
        calls = []

        def no_run(*a, **k):
            calls.append("subprocess.run")
            return types.SimpleNamespace(returncode=1)

        orig_run, orig_kill = gui.subprocess.run, os.kill

        def no_kill(pid, sig=None):
            calls.append("os.kill")
            return None

        gui.subprocess.run = no_run
        try:
            os.kill = no_kill  # patch the primitive itself, not os.name routing
            self.assertFalse(gui.kill_pid(bad_pid))
        finally:
            gui.subprocess.run = orig_run
            os.kill = orig_kill
        self.assertEqual(calls, [])

    def test_bool_pids_are_refused_without_touching_a_backend(self):
        for bad in (True, False):
            self._assert_rejected_before_backend(bad)

    def test_float_pids_are_refused_without_touching_a_backend(self):
        for bad in (1.0, 1.9, -2.0, -2.5, 0.5, 4242.0, 4242.999, float("inf"), float("nan")):
            self._assert_rejected_before_backend(bad)

    def test_refusal_of_bool_or_float_pid_logs_a_sanitized_warning(self):
        for bad in (True, 1.5):
            with self.assertLogs(gui.LOG, level=logging.WARNING) as cm:
                self.assertFalse(gui.kill_pid(bad))
            msg = cm.records[0].getMessage()
            self.assertIn("non-integer", msg)

    def test_positive_int_pid_still_reaches_the_backend_once(self):
        calls = []
        orig_run = gui.subprocess.run
        orig_os_name = os.name
        if orig_os_name != "nt":
            os.name = "nt"

        def fake_run(args, **kw):
            calls.append(list(args))
            return types.SimpleNamespace(returncode=0, stdout=b"", stderr=b"", args=args)

        gui.subprocess.run = fake_run
        try:
            self.assertTrue(gui.kill_pid(4242))
        finally:
            gui.subprocess.run = orig_run
            if orig_os_name != "nt":
                os.name = orig_os_name
        self.assertEqual(calls, [["taskkill", "/F", "/T", "/PID", "4242"]])

    def test_positive_string_pid_is_still_accepted(self):
        # integer-form strings remain compatible (registry JSON round-trips pids as text)
        calls = []
        orig_run = gui.subprocess.run
        orig_os_name = os.name
        if orig_os_name != "nt":
            os.name = "nt"

        def fake_run(args, **kw):
            calls.append(list(args))
            return types.SimpleNamespace(returncode=0, stdout=b"", stderr=b"", args=args)

        gui.subprocess.run = fake_run
        try:
            self.assertTrue(gui.kill_pid("4242"))
            self.assertTrue(gui.kill_pid(" 4243 "))
        finally:
            gui.subprocess.run = orig_run
            if orig_os_name != "nt":
                os.name = orig_os_name
        self.assertEqual(calls, [["taskkill", "/F", "/T", "/PID", "4242"],
                                 ["taskkill", "/F", "/T", "/PID", "4243"]])


class RestartBridgeTests(unittest.TestCase):
    """restart must abort with the stop error when stopping failed -- never silently launch on
    another port -- and may relaunch only when the stale registry was pruned and the port is
    free (or the old process was killed and released it)."""

    PID = 4321

    def setUp(self):
        self.scratch = tempfile.mkdtemp()
        self.orig = (gui.BRIDGES_DIR, gui._bridge_health, gui.kill_pid, gui.spawn)
        gui.BRIDGES_DIR = self.scratch
        self.spawned = []
        gui.spawn = lambda args, terminal=False: self.spawned.append(args)
        gui.kill_pid = lambda pid: True

    def tearDown(self):
        (gui.BRIDGES_DIR, gui._bridge_health, gui.kill_pid, gui.spawn) = self.orig
        shutil.rmtree(self.scratch, ignore_errors=True)

    def _free_port(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        p = s.getsockname()[1]
        s.close()
        return p

    def _hold(self, port):
        s = socket.socket()
        s.bind(("0.0.0.0", int(port)))
        s.listen(1)
        self.addCleanup(s.close)
        return s

    def _write_rec(self, port, **kw):
        rec = {"pid": self.PID, "base_url": "http://127.0.0.1:%d" % port,
               "instance_id": "IID-%d" % port}
        rec.update(kw)
        with open(os.path.join(self.scratch, "bridge-%d.json" % port), "w",
                  encoding="utf-8") as f:
            json.dump(rec, f)

    def _rec_path(self, port):
        return os.path.join(self.scratch, "bridge-%d.json" % port)

    def test_restart_aborts_on_held_unverifiable_bridge_without_spawning(self):
        port = self._free_port()
        self._write_rec(port)
        self._hold(port)
        gui._bridge_health = lambda url: None
        r = gui.restart_bridge({"port": port, "engine": "codex"})
        self.assertIn("error", r)
        self.assertNotIn("base_url", r)
        self.assertEqual(self.spawned, [])
        self.assertTrue(os.path.exists(self._rec_path(port)))

    def test_restart_aborts_on_expected_id_mismatch_without_spawning(self):
        port = self._free_port()
        self._write_rec(port)
        gui._bridge_health = lambda url: {"instance_id": "IID-%d" % port}
        r = gui.restart_bridge({"port": port, "engine": "codex", "instance_id": "outdated"})
        self.assertIn("error", r)
        self.assertIn("mismatch", r["error"])
        self.assertEqual(self.spawned, [])
        self.assertTrue(os.path.exists(self._rec_path(port)))

    def test_restart_legacy_record_without_instance_id_fails_closed_honestly(self):
        """An id-less legacy record cannot authorize a kill or restart."""
        port = self._free_port()
        self._write_rec(port, instance_id=None)
        gui._bridge_health = lambda url: {"instance_id": "some-live-id"}
        r = gui.restart_bridge({"port": port, "engine": "codex"})  # no instance_id, like the fixed JS
        self.assertIn("error", r)
        self.assertIn("could not verify", r["error"])
        self.assertNotIn("mismatch", r["error"])
        self.assertEqual(self.spawned, [])
        self.assertTrue(os.path.exists(self._rec_path(port)))

    def test_restart_aborts_on_kill_failure_without_spawning(self):
        port = self._free_port()
        self._write_rec(port)
        gui._bridge_health = lambda url: {"instance_id": "IID-%d" % port}
        gui.kill_pid = lambda pid: False
        r = gui.restart_bridge({"port": port, "engine": "codex"})
        self.assertIn("error", r)
        self.assertNotIn("base_url", r)
        self.assertEqual(self.spawned, [])
        self.assertTrue(os.path.exists(self._rec_path(port)))

    def test_restart_proceeds_after_stale_registry_prune_on_same_port(self):
        port = self._free_port()   # nothing holds it: an already-dead stale registry
        self._write_rec(port)
        gui._bridge_health = lambda url: None
        r = gui.restart_bridge({"port": port, "engine": "codex"})
        self.assertTrue(r.get("ok"))
        self.assertEqual(r["port"], port)
        self.assertFalse(r.get("reassigned"))
        self.assertEqual(self.spawned, [[u"openai-server", u"-e", u"codex",
                                         u"-p", str(port), u"--localhost"]])
        self.assertFalse(os.path.exists(self._rec_path(port)))


class BridgeStopRouteTests(unittest.TestCase):
    """The existing /api/bridge/stop route must carry the row's registered instance_id through
    to gui.stop_bridge so a stale panel can't stop a newer bridge."""

    def test_route_forwards_instance_id_to_stop_bridge(self):
        seen = []

        def fake_stop(port, expected_instance_id=None):
            seen.append((port, expected_instance_id))
            return {"ok": True, "killed": True}

        orig = gui.stop_bridge
        gui.stop_bridge = fake_stop
        try:
            sent = []

            class Fake:
                client_address = ("127.0.0.1", 4242)
                _reject_unauthorized = gui.H._reject_unauthorized
                _read_json_object_body = gui.H._read_json_object_body

                def __init__(self):
                    body = json.dumps({"port": 8899, "instance_id": "IID-8899"}).encode()
                    self.path = "/api/bridge/stop"
                    self.headers = {"Content-Length": str(len(body))}
                    self.rfile = io.BytesIO(body)

                def _send(self, code, body_, ctype="application/json"):
                    sent.append((code, body_))

            gui.H.do_POST(Fake())
        finally:
            gui.stop_bridge = orig
        self.assertEqual(seen, [(8899, "IID-8899")])
        self.assertEqual(json.loads(sent[0][1])["killed"], True)


class BridgeRestartRouteTests(unittest.TestCase):
    """The restart endpoint forwards the validated body unchanged."""

    def test_route_forwards_body_to_restart_bridge(self):
        seen = []

        def fake_restart(data):
            seen.append(dict(data))
            return {"ok": True, "port": data["port"]}

        orig = gui.restart_bridge
        gui.restart_bridge = fake_restart
        try:
            sent = []

            class Fake:
                client_address = ("127.0.0.1", 4242)
                _reject_unauthorized = gui.H._reject_unauthorized
                _read_json_object_body = gui.H._read_json_object_body

                def __init__(self):
                    body = json.dumps({"port": 8899, "engine": "codex",
                                       "instance_id": "IID-8899"}).encode()
                    self.path = "/api/bridge/restart"
                    self.headers = {"Content-Length": str(len(body))}
                    self.rfile = io.BytesIO(body)

                def _send(self, code, body_, ctype="application/json"):
                    sent.append((code, body_))

            gui.H.do_POST(Fake())
        finally:
            gui.restart_bridge = orig
        self.assertEqual(seen, [{"port": 8899, "engine": "codex", "instance_id": "IID-8899"}])
        self.assertEqual(json.loads(sent[0][1])["ok"], True)


class ApiTestRouteRemovedTests(unittest.TestCase):
    """The dead GUI endpoint stays gone while CLI-created task rows remain visible."""

    def _post(self, path, body=b"{}"):
        sent = []

        class Fake:
            client_address = ("127.0.0.1", 4242)
            _reject_unauthorized = gui.H._reject_unauthorized
            _read_json_object_body = gui.H._read_json_object_body

            def __init__(self):
                self.path = path
                self.headers = {"Content-Length": str(len(body))}
                self.rfile = io.BytesIO(body)

            def _send(self, code, body_, ctype="application/json"):
                sent.append((code, body_))

        gui.H.do_POST(Fake())
        return sent

    def test_post_test_api_is_404_not_found(self):
        body = json.dumps({"base_url": "http://127.0.0.1:9", "goal": "probe it"}).encode()
        self.assertEqual(self._post("/api/test-api", body)[0][0], 404)

    def test_get_test_api_is_404_too(self):
        sent = []

        class Fake:
            path = "/api/test-api"
            client_address = ("127.0.0.1", 4242)
            headers = {}
            _reject_unauthorized = gui.H._reject_unauthorized

            def _send(self, code, body_, ctype="application/json"):
                sent.append((code, body_))

        gui.H.do_GET(Fake())
        self.assertEqual(sent[0][0], 404)

    def test_kind_api_test_rows_still_display_from_cli_meta(self):
        scratch = tempfile.mkdtemp()
        orig = gui.LOGDIR
        gui.LOGDIR = scratch
        try:
            with open(os.path.join(scratch, "api-test-123.meta"), "w", encoding="utf-8") as f:
                f.write("engine=codex\nmodel=sol\nstate=done\nkind=api-test\n")
            tasks = [t for t in gui.list_tasks() if t["name"] == "api-test-123"]
            self.assertEqual(len(tasks), 1)
            self.assertEqual(tasks[0]["kind"], "api-test")
        finally:
            gui.LOGDIR = orig
            shutil.rmtree(scratch, ignore_errors=True)


class GuiPostBodyGuardTests(unittest.TestCase):
    """gui.do_POST answers garbled/oversized bodies with controlled 400/413 instead of
    crashing on int()/rfile.read or reading an unbounded body into memory."""

    def _call(self, body=b"", headers=None):
        sent = []

        class Fake:
            client_address = ("127.0.0.1", 4242)
            _reject_unauthorized = gui.H._reject_unauthorized
            _read_json_object_body = gui.H._read_json_object_body

            def __init__(self):
                self.path = "/api/no-such-route"
                self.headers = headers if headers is not None else {}
                if body and "Content-Length" not in self.headers:
                    self.headers["Content-Length"] = str(len(body))
                self.rfile = io.BytesIO(body)

            def _send(self, code, body_, ctype="application/json"):
                sent.append((code, body_))

        gui.H.do_POST(Fake())
        return sent

    def test_malformed_and_negative_content_length_are_400(self):
        for v in ("abc", "-5", "1e3"):
            with self.subTest(value=v):
                sent = self._call(headers={"Content-Length": v})
                self.assertEqual(sent[0][0], 400)

    def test_oversized_content_length_is_413_before_any_read(self):
        sent = self._call(headers={"Content-Length": str(gui.MAX_BODY_BYTES + 1)})
        self.assertEqual(sent[0][0], 413)

    def test_invalid_or_non_object_json_is_400(self):
        for raw in (b"{nope", b"[1]"):
            with self.subTest(body=raw):
                sent = self._call(body=raw, headers={"Content-Length": str(len(raw))})
                self.assertEqual(sent[0][0], 400)

    def test_valid_empty_body_reaches_routing(self):
        sent = self._call()   # no header -> {} -> unknown route -> plain 404
        self.assertEqual(sent[0][0], 404)


class GuiLoggingTests(unittest.TestCase):
    """Diagnostics mirror openai_server.py: opt-in (quiet WARNING default, AGENT_LOG_LEVEL raises
    the gate) and never carrying secrets -- no request bodies/prompts/answers/auth headers, and a
    `?token=` URL is redacted before it can reach the log."""

    def setUp(self):
        self._level = gui.LOG.level
        gui.LOG.setLevel(logging.WARNING)  # pin the documented default for every test here

    def tearDown(self):
        gui.LOG.setLevel(self._level)

    class _FakeSelf:
        @staticmethod
        def address_string():
            return "127.0.0.1"

    def test_request_lines_route_to_debug_only(self):
        with self.assertLogs(gui.LOG, level=logging.DEBUG) as cm:
            gui.H.log_message(self._FakeSelf(), '"GET / HTTP/1.1" %s %s', 200, "-")
        self.assertTrue(all(r.levelno == logging.DEBUG for r in cm.records))
        with self.assertRaises(AssertionError):  # fully silent at the default gate
            with self.assertLogs(gui.LOG, level=logging.WARNING):
                gui.H.log_message(self._FakeSelf(), '"GET / HTTP/1.1" 200 -')

    def test_token_in_request_line_is_redacted(self):
        secret = "sekrit-token-9f2"
        with self.assertLogs(gui.LOG, level=logging.DEBUG) as cm:
            gui.H.log_message(self._FakeSelf(), '"GET /?token=%s HTTP/1.1" 200 -' % secret)
        line = cm.records[0].getMessage()
        self.assertNotIn(secret, line)
        self.assertIn("token=<redacted>", line)
        self.assertIn("api_key=<redacted>", self._fmt("api_key=k3"))
        self.assertNotIn("k3", self._fmt("api_key=k3"))

    def _fmt(self, requestline):
        with self.assertLogs(gui.LOG, level=logging.DEBUG) as cm:
            gui.H.log_message(self._FakeSelf(), '"%s" 200 -' % requestline)
        return cm.records[0].getMessage()

    def test_failed_health_probe_is_debug_gated_and_leaks_no_url(self):
        orig = gui.urllib.request.urlopen
        secret = "sekrit-health-9f2"
        secret_url = "http://127.0.0.1:9999/health?token=%s" % secret

        class SyntheticProbeError(RuntimeError):
            pass

        def fake_urlopen(*a, **kw):
            raise SyntheticProbeError("should-not-be-logged")

        gui.urllib.request.urlopen = fake_urlopen
        try:
            with self.assertLogs(gui.LOG, level=logging.DEBUG) as cm:
                self.assertIsNone(gui._bridge_health(secret_url))
            msg = cm.records[0].getMessage()
            self.assertIn("SyntheticProbeError", msg)
            self.assertNotIn(secret, msg)
            self.assertNotIn(secret_url, msg)
            self.assertNotIn("should-not-be-logged", msg)
            with self.assertRaises(AssertionError):
                with self.assertLogs(gui.LOG, level=logging.WARNING):
                    gui._bridge_health(secret_url)
        finally:
            gui.urllib.request.urlopen = orig

    def test_kill_pid_failure_warns_with_exception_type_only(self):
        orig_run = gui.subprocess.run

        class Boom(RuntimeError):
            pass

        def raising_run(*a, **k):
            raise Boom("taskkill internal detail should-not-be-logged")
        gui.subprocess.run = raising_run
        try:
            with self.assertLogs(gui.LOG, level=logging.WARNING) as cm:
                self.assertFalse(gui.kill_pid(424242))
        finally:
            gui.subprocess.run = orig_run
        msg = cm.records[0].getMessage()
        self.assertIn("Boom", msg)          # exception type is fine
        self.assertIn("424242", msg)        # pid is fine
        self.assertNotIn("should-not-be-logged", msg)

    def test_kill_pid_nonzero_returncode_warns_with_status_only(self):
        orig_run = gui.subprocess.run
        orig_os_name = os.name
        if orig_os_name != "nt":
            os.name = "nt"
        secret = "sekrit-cmd-should-not-appear"

        def fake_run(args, **kw):
            return types.SimpleNamespace(returncode=2, stdout=secret, stderr=secret, args=args)

        gui.subprocess.run = fake_run
        try:
            with self.assertLogs(gui.LOG, level=logging.WARNING) as cm:
                self.assertFalse(gui.kill_pid(424242))
            msg = cm.records[0].getMessage()
            self.assertIn("2", msg)
            self.assertIn("424242", msg)
            self.assertNotIn(secret, msg)
        finally:
            gui.subprocess.run = orig_run
            if orig_os_name != "nt":
                os.name = orig_os_name


if __name__ == "__main__":
    unittest.main()
