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
~/.claude/agent-cli-logs.

Run:
    python tests/test_gui.py
    python -m unittest tests.test_gui          (from the repo root)
    python -m unittest discover tests
"""
import importlib.util
import json
import os
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
    def test_running_with_stale_mtime_becomes_stalled(self):
        nowt = 1_000_000.0
        log_mtime = nowt - (gui.STALE_SEC + 1)  # older than STALE_SEC -> stalled
        meta = {"state": "running"}
        self.assertEqual(gui.eff_state(meta, log_mtime, nowt), "stalled")

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


if __name__ == "__main__":
    unittest.main()
