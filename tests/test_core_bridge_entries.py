"""Python-core streaming boundary: incremental deltas and final-answer replay."""
import inspect
import contextlib
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import openai_server as bridge


class CoreBridgeTailTests(unittest.TestCase):
    def setUp(self):
        base = Path("D:/Temp/agents-ux/entries") if os.name == "nt" else Path(tempfile.gettempdir()) / "agents-core"
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=str(base))
        self.addCleanup(self.temp.cleanup)

    def forward(self, chunks, function=None):
        path = Path(self.temp.name) / "stream.log"
        path.write_bytes(b"")
        pieces = iter(chunks)

        class FakeProcess:
            def poll(self):
                try:
                    chunk = next(pieces)
                except StopIteration:
                    return 0
                with path.open("ab") as file:
                    file.write(chunk)
                return None

        emitted = []
        with mock.patch.object(bridge, "LOGDIR", self.temp.name):
            (function or bridge._tail_task_log)("stream", FakeProcess(), 10, emitted.append)
        return "".join(emitted)

    def test_final_replay_is_excluded_when_every_utf8_byte_and_marker_are_split(self):
        answer = 'Привет "мир"\nnext line\n'
        text = "header\n" + bridge.OUTPUT_MARKER + "\n" + answer + bridge.OUTPUT_MARKER + "\n" + answer
        self.assertEqual(self.forward([bytes([byte]) for byte in text.encode("utf-8")]), answer)

    def test_marker_must_be_a_complete_line(self):
        answer = "prefix " + bridge.OUTPUT_MARKER + "\n" + bridge.OUTPUT_MARKER + " trailing\n"
        text = "header\n" + bridge.OUTPUT_MARKER + "\n" + answer + bridge.OUTPUT_MARKER + "\nreplay"
        self.assertEqual(self.forward([text.encode("utf-8")]), answer)

    def test_incomplete_marker_prefix_is_not_lost_at_eof(self):
        text = bridge.OUTPUT_MARKER + "\ntext\n-----"
        self.assertEqual(self.forward([text.encode("utf-8")]), "text\n-----")

    def test_planted_replay_detection_defect_is_detected(self):
        source = inspect.getsource(bridge._tail_task_log)
        self.assertIn("replayed = True", source)
        namespace = dict(vars(bridge))
        exec(source.replace("replayed = True", "replayed = False"), namespace)
        text = bridge.OUTPUT_MARKER + "\nanswer\n" + bridge.OUTPUT_MARKER + "\nanswer\n"
        result = self.forward([text.encode("utf-8")], namespace["_tail_task_log"])
        self.assertNotEqual(result, "answer\n")
        self.assertEqual(result, "answer\nanswer\n")


class CoreBridgeLivePromptTests(unittest.TestCase):
    def setUp(self):
        base = Path("D:/Temp/agents-ux/entries") if os.name == "nt" else Path(tempfile.gettempdir()) / "agents-core"
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=str(base))
        self.addCleanup(self.temp.cleanup)

    def verify_prompt(self, reply=False):
        prompt = '\u041f\u0440\u0438\u0432\u0435\u0442 "quotes"\n' * 3000
        paths = []

        def spawn(args, **kwargs):
            self.assertEqual(args[-2], "--prompt-file")
            path = Path(args[-1])
            self.assertEqual(path.parent, Path(self.temp.name) / ".prompts")
            self.assertEqual(path.read_bytes(), prompt.encode("utf-8"))
            self.assertLess(len(subprocess_list(args)), 1000)
            paths.append(path)
            return mock.Mock()

        def tail(*args, **kwargs):
            self.assertTrue(paths[-1].is_file())

        with mock.patch.object(bridge, "LOGDIR", self.temp.name), \
                mock.patch.object(bridge, "to_git_bash_path", side_effect=lambda value: value), \
                mock.patch.object(bridge, "_stream_env", return_value={}), \
                mock.patch.object(bridge, "_popen_process_tree", side_effect=spawn), \
                mock.patch.object(bridge, "_tail_task_log", side_effect=tail), \
                mock.patch.object(bridge, "_release_process_tree"), \
                mock.patch.object(bridge, "_log_size", return_value=0), \
                mock.patch.object(bridge, "read_meta", return_value={"state": "done"}), \
                mock.patch.object(bridge, "read_log", side_effect=[""] if not reply else ["", "answer"]), \
                mock.patch.object(bridge, "last_output", return_value="answer"):
            if reply:
                answer = bridge.reply_agent_live("claude", None, None, self.temp.name,
                                                 "live", prompt, 10, lambda value: None)
            else:
                answer = bridge.run_agent_live("claude", None, None, self.temp.name,
                                               prompt, "live", 10, lambda value: None)
        self.assertEqual(answer, "answer")
        self.assertFalse(paths[-1].exists())

    def test_live_run_long_utf8_prompt_is_staged_and_removed(self):
        self.verify_prompt()

    def test_live_reply_long_utf8_prompt_is_staged_and_removed(self):
        self.verify_prompt(reply=True)

    def test_planted_argv_bypass_defect_is_detected(self):
        @contextlib.contextmanager
        def unsafe_argument(text):
            yield [text]

        with mock.patch.object(bridge, "_prompt_argument", unsafe_argument):
            with self.assertRaises(AssertionError):
                self.verify_prompt()


def subprocess_list(args):
    import subprocess
    return subprocess.list2cmdline(args)


if __name__ == "__main__":
    unittest.main()
