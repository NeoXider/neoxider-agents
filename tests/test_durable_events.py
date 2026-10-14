"""Durable parent event backend: fault injection, multi-process writers, replay and cursors.

Every event in this file is fabricated. No test payload contains a real credential, token,
endpoint or account value; the shapes that must be refused are obviously fake and are only
written when a test deliberately checks the credential guard.
"""
import json
import multiprocessing
import hashlib
import os
import shutil
import sys
import subprocess
import tempfile
import threading
import time
import unittest
import tracemalloc
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from neoxider_agents.events import (  # noqa: E402
    EventCorruption, EventStore, WaitTimeout, idempotency_key, records_of)
from neoxider_agents.state import Lock  # noqa: E402

TASK = "events-task"
FAKE_KEY = "sk-notarealkey0000000000000000000000"


def worker(root, task, count, tag, gate):
    """A separate OS process writing the same log; ordering comes from the file lock."""
    store = EventStore(root)
    gate.wait(30)
    for index in range(count):
        store.emit(task, generation=tag, kind="worker.step", data={"index": index, "tag": tag})
    return count


def ack_worker(root, task, value, gate):
    gate.wait(30)
    EventStore(root).ack(task, "race", value)


class EventTestCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="neoxider-events-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.store = EventStore(self.root)

    def emit(self, kind="task.step", data=None, task=TASK, **options):
        return self.store.emit(task, options.pop("generation", ""), options.pop("parent", ""),
                               kind, data if data is not None else {"note": "x"}, **options)

    def log_path(self, task=TASK):
        return Path(self.root) / (task + ".events.jsonl")


class DurableWrite(EventTestCase):
    def test_record_carries_id_sequence_generation_parent_timestamp_kind_data(self):
        root = self.emit("task.start", {"argv": ["run", "-t", TASK]}, generation="3")
        child = self.emit("task.step", {"note": "child"}, generation="3", parent=root["id"])
        self.assertEqual(len(root["id"]), 32)
        self.assertEqual(root["seq"], 1)
        self.assertEqual(root["generation"], "3")
        self.assertEqual(root["parent"], "")
        self.assertEqual(child["seq"], 2)
        self.assertEqual(child["parent"], root["id"])
        self.assertGreater(root["epoch"], 0)
        self.assertEqual(root["kind"], "task.start")
        self.assertEqual(root["data"], {"argv": ["run", "-t", TASK]})
        self.assertEqual(root["task"], TASK)
        self.assertTrue(root["digest"].startswith("sha256:"))

    def test_sequence_is_monotonic_and_never_resets(self):
        for index in range(5):
            self.emit(data={"i": index})
        self.assertEqual([record["seq"] for record in self.store.read(TASK)], [1, 2, 3, 4, 5])
        self.assertEqual(self.store.head(TASK), 5)
        self.assertEqual(self.store.ack(TASK, "reader", 5)["cursor"], 5)
        for index in range(5, 8):
            self.emit(data={"i": index})
        self.assertEqual(self.store.head(TASK), 8)
        self.assertEqual(self.store.cursor(TASK, "reader"), 5)

    def test_append_is_one_fsynced_line_and_never_rewrites_the_log(self):
        self.emit(data={"i": 1})
        first = self.log_path().read_bytes()
        self.emit(data={"i": 2})
        second = self.log_path().read_bytes()
        self.assertTrue(second.startswith(first), "append-only: the earlier bytes must survive intact")
        self.assertEqual(len(second.splitlines()), 2)
        self.assertTrue(second.endswith(b"\n"))

    def test_read_window_holds_open_does_not_block_a_writer(self):
        """Windows readers share the file; nothing here replaces or truncates it."""
        handle = self.log_path().open("rb") if self.log_path().exists() else None
        for index in range(3):
            self.emit(data={"i": index})
        if handle is None:
            handle = self.log_path().open("rb")
        try:
            self.emit(data={"i": 99})
            handle.seek(0)
            self.assertEqual(len(handle.read().splitlines()), 4)
        finally:
            handle.close()


class Idempotency(EventTestCase):
    def test_same_contents_reuses_the_id_and_appends_nothing(self):
        key = idempotency_key(TASK, "", "", "task.start", {"argv": ["run"]})
        first = self.emit("task.start", {"argv": ["run"]}, event_id=key)
        again = self.emit("task.start", {"argv": ["run"]}, event_id=key)
        self.assertEqual(first["id"], key)
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(again["seq"], 1)
        self.assertFalse(first["duplicate"])
        self.assertTrue(again["duplicate"])
        self.assertEqual(self.store.head(TASK), 1)

    def test_same_key_different_content_is_rejected(self):
        key = idempotency_key(TASK, "", "", "task.start", {"argv": ["run"]})
        self.emit("task.start", {"argv": ["run"]}, event_id=key)
        with self.assertRaises(ValueError) as caught:
            self.emit("task.start", {"argv": ["run", "--fresh"]}, event_id=key)
        self.assertIn("different content", str(caught.exception))
        self.assertEqual(self.store.head(TASK), 1)

    def test_idempotency_key_is_deterministic_and_content_addressed(self):
        first = idempotency_key(TASK, "1", "", "kind", {"a": 1, "b": 2})
        second = idempotency_key(TASK, "1", "", "kind", {"b": 2, "a": 1})
        other = idempotency_key(TASK, "1", "", "kind", {"a": 2, "b": 2})
        self.assertEqual(first, second)
        self.assertNotEqual(first, other)
        self.assertEqual(len(first), 32)

    def test_omitting_the_id_never_deduplicates(self):
        self.emit("task.tick", {"n": 1})
        self.emit("task.tick", {"n": 1})
        self.assertEqual(self.store.head(TASK), 2)


class Validation(EventTestCase):
    def test_rejects_paths_and_fields_that_could_escape_the_state_root(self):
        for task in ("../escape", "a/b", "..", "", "x" * 200, "with space", "back\\slash"):
            with self.assertRaises(ValueError, msg=task):
                self.store.emit(task, "", "", "kind", {})
        for consumer in ("../escape", "..", "", "a" * 100, "bad consumer"):
            with self.assertRaises(ValueError, msg=consumer):
                self.store.cursor(TASK, consumer)
        for identifier in ("not-hex", "ZZ" + "0" * 30, "../" + "0" * 32, "0" * 31):
            with self.assertRaises(ValueError, msg=identifier):
                self.store.emit(TASK, "", "", "kind", {}, event_id=identifier)

    def test_rejects_malformed_cursors_instead_of_guessing(self):
        for cursor in ("abc", "-1", 1.5, True, [2], "0x10", " "):
            with self.assertRaises(ValueError, msg=repr(cursor)):
                self.store.read(TASK, after=cursor)
        self.assertEqual(self.store.read(TASK, after="7").cursor, 7)
        self.assertEqual(self.store.read(TASK, after=0).cursor, 0)

    def test_payload_must_be_immutable_json(self):
        for payload in ([1, 2], "text", 3, None, {1: "int key"}, {"n": float("nan")},
                        {"n": float("inf")}, {"b": b"bytes"}, {"s": {1, 2}}):
            with self.assertRaises(ValueError, msg=repr(payload)):
                self.store.emit(TASK, "", "", "kind", payload)
        self.emit("kind", {"nested": {"list": [1, "two", None, True]}, "unicode": "ключ"})
        stored = self.store.read(TASK).events[0]
        self.assertEqual(stored["data"]["nested"]["list"], [1, "two", None, True])
        self.assertEqual(stored["data"]["unicode"], "ключ")

    def test_mutating_the_caller_payload_does_not_change_the_stored_event(self):
        payload = {"counter": 1, "items": ["a"]}
        record = self.store.emit(TASK, "", "", "kind", payload)
        payload["counter"] = 99
        payload["items"].append("b")
        self.assertEqual(self.store.find(TASK, record["id"])["data"], {"counter": 1, "items": ["a"]})

    def test_mutating_a_returned_event_does_not_change_the_log(self):
        record = self.store.emit(TASK, "", "", "kind", {"counter": 1})
        record["data"]["counter"] = 42
        record["seq"] = 999
        self.assertEqual(self.store.find(TASK, record["id"])["data"], {"counter": 1})
        self.assertEqual(self.store.head(TASK), 1)

    def test_oversized_payload_is_refused_rather_than_written(self):
        with self.assertRaises(ValueError) as caught:
            self.store.emit(TASK, "", "", "kind", {"blob": "x" * 300000})
        self.assertIn("per-record limit", str(caught.exception))
        self.assertEqual(self.store.head(TASK), 0)

    def test_credential_shaped_payload_is_refused_unless_explicitly_allowed(self):
        with self.assertRaises(ValueError) as caught:
            self.store.emit(TASK, "", "", "task.start", {"provider_api_key": FAKE_KEY})
        self.assertIn("credentials", str(caught.exception))
        self.assertEqual(self.store.head(TASK), 0)
        self.store.emit(TASK, "", "", "task.start", {"provider_api_key": FAKE_KEY}, allow_secrets=True)
        self.assertEqual(self.store.head(TASK), 1)

    def test_ordinary_payload_fields_are_not_mistaken_for_credentials(self):
        self.store.emit(TASK, "", "", "usage", {"tokens": 4096, "token_count": 17, "api_key_present": False})
        self.assertEqual(self.store.head(TASK), 1)


class Paging(EventTestCase):
    def fill(self, count):
        return [self.emit("task.step", {"i": index}) for index in range(count)]

    def test_pages_are_bounded_and_never_overlap(self):
        self.fill(250)
        first = self.store.read(TASK, limit=100)
        second = self.store.read(TASK, after=first.cursor, limit=100)
        third = self.store.read(TASK, after=second.cursor, limit=100)
        self.assertEqual([len(page.events) for page in (first, second, third)], [100, 100, 50])
        self.assertEqual(first.more, True)
        self.assertEqual(second.more, True)
        self.assertEqual(third.more, False)
        sequences = [record["seq"] for page in (first, second, third) for record in page.events]
        self.assertEqual(sequences, list(range(1, 251)))

    def test_cursor_after_a_page_is_exclusive_and_survives_a_reconnect(self):
        self.fill(30)
        page = self.store.read(TASK, limit=10)
        # A brand new store object stands in for a fresh process: only the file survives.
        reopened = EventStore(self.root)
        rest = reopened.read(TASK, after=page.cursor, limit=100)
        self.assertEqual([record["seq"] for record in rest.events], list(range(11, 31)))
        self.assertEqual(reopened.head(TASK), 30)

    def test_limit_is_clamped_and_rejects_junk(self):
        self.fill(3)
        self.assertEqual(len(self.store.read(TASK, limit=10 ** 9).events), 3)
        for limit in (0, -1, "many", None):
            with self.assertRaises(ValueError, msg=repr(limit)):
                self.store.read(TASK, limit=limit)

    def test_a_long_log_never_clips_an_old_terminal_event(self):
        """A terminal event is old by definition; reading it must not depend on log size."""
        terminal = self.emit("task.completed", {"exit": 0, "verdict": "ok"})
        self.fill(500)
        self.assertGreater(self.log_path().stat().st_size, 65536)
        self.assertEqual(self.log_path().stat().st_size, self.log_path().stat().st_size)
        replayed = self.store.find(TASK, terminal["id"])
        self.assertEqual(replayed["seq"], 1)
        self.assertEqual(replayed["data"]["verdict"], "ok")
        self.assertEqual(self.store.read(TASK, limit=100).events[0]["id"], terminal["id"])
        consumer = ["late-reader"]
        seen = []
        report = self.store.drain(TASK, seen.append, "late-reader")
        self.assertEqual(seen[0]["id"], terminal["id"])
        self.assertEqual(seen[-1]["seq"], 501)
        self.assertEqual(self.store.cursor(TASK, "late-reader"), 501)
        del consumer

    def test_page_memory_stays_bounded_on_a_large_log(self):
        self.fill(400)
        page = self.store.read(TASK, limit=5)
        self.assertEqual(len(page.events), 5)
        self.assertEqual(page.truncated, True)
        self.assertEqual(page.torn, False)
        self.assertEqual(self.store.head(TASK), 400)


class Acknowledgment(EventTestCase):
    def fill(self, count):
        for index in range(count):
            self.emit("task.step", {"i": index})

    def test_cursor_starts_at_zero_and_advances_monotonically(self):
        self.fill(10)
        self.assertEqual(self.store.cursor(TASK, "gui"), 0)
        self.assertTrue(self.store.ack(TASK, "gui", 4)["advanced"])
        self.assertEqual(self.store.cursor(TASK, "gui"), 4)
        repeat = self.store.ack(TASK, "gui", 4)
        self.assertFalse(repeat["advanced"])
        self.assertEqual(repeat["cursor"], 4)
        self.assertEqual(self.store.ack(TASK, "gui", 2)["cursor"], 4)
        self.assertEqual(self.store.cursor(TASK, "gui"), 4)
        self.assertTrue(self.store.ack(TASK, "gui", 9)["advanced"])
        self.assertEqual(self.store.cursor(TASK, "gui"), 9)

    def test_each_consumer_has_an_independent_cursor(self):
        self.fill(6)
        self.store.ack(TASK, "gui", 2)
        self.store.ack(TASK, "orchestrator", 5)
        self.assertEqual(self.store.cursor(TASK, "gui"), 2)
        self.assertEqual(self.store.cursor(TASK, "orchestrator"), 5)
        self.assertEqual(self.store.cursors(TASK), {"gui": 2, "orchestrator": 5})
        self.store.ack(TASK, "gui", 1)
        self.assertEqual(self.store.cursor(TASK, "orchestrator"), 5)
        self.assertEqual(self.store.cursor(TASK, "unknown-consumer"), 0)

    def test_acknowledging_past_the_head_is_refused(self):
        self.fill(3)
        with self.assertRaises(ValueError) as caught:
            self.store.ack(TASK, "gui", 4)
        self.assertIn("log head is 3", str(caught.exception))
        self.assertEqual(self.store.cursor(TASK, "gui"), 0)

    def test_racing_acknowledgments_keep_the_highest_value(self):
        self.fill(20)
        results = []
        errors = []

        def race(value):
            try:
                results.append(self.store.ack(TASK, "racer", value))
            except Exception as error:  # pragma: no cover - surfaced by the assertions below
                errors.append(error)

        threads = [threading.Thread(target=race, args=(value,)) for value in (7, 19, 3, 12, 20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        self.assertEqual(errors, [])
        self.assertEqual(self.store.cursor(TASK, "racer"), 20)
        self.assertTrue(all(result["cursor"] <= 20 for result in results))

    def test_racing_consumers_never_see_each_others_progress(self):
        self.fill(12)
        self.store.ack(TASK, "fast", 12)
        self.assertEqual(self.store.cursor(TASK, "slow"), 0)
        self.assertEqual(len(self.store.read(TASK, after=self.store.cursor(TASK, "slow")).events), 12)


class Delivery(EventTestCase):
    def fill(self, count):
        return [self.emit("task.step", {"i": index}) for index in range(count)]

    def test_drain_delivers_once_and_persists_the_cursor(self):
        self.fill(5)
        seen = []
        report = self.store.drain(TASK, seen.append, "worker")
        self.assertEqual([record["seq"] for record in seen], [1, 2, 3, 4, 5])
        self.assertEqual(report.delivered, 5)
        self.assertEqual(self.store.cursor(TASK, "worker"), 5)
        self.assertEqual(self.store.drain(TASK, seen.append, "worker").delivered, 0)
        self.assertEqual(len(seen), 5)

    def test_callback_failure_acknowledges_the_prefix_and_replays_the_rest(self):
        self.fill(6)
        seen = []

        def flaky(record):
            if record["seq"] == 3:
                raise RuntimeError("callback crashed")
            seen.append(record["seq"])

        with self.assertRaises(RuntimeError):
            self.store.drain(TASK, flaky, "flaky")
        self.assertEqual(seen, [1, 2])
        self.assertEqual(self.store.cursor(TASK, "flaky"), 2)
        # A restart resumes at the failed event: nothing is skipped, nothing before is re-sent.
        seen = []
        report = self.store.drain(TASK, lambda record: seen.append(record["seq"]), "flaky")
        self.assertEqual(seen, [3, 4, 5, 6])
        self.assertEqual(report.delivered, 4)
        self.assertEqual(self.store.cursor(TASK, "flaky"), 6)

    def test_a_process_dying_between_callback_and_ack_replays_that_event(self):
        """The honest at-least-once boundary: acting without acking repeats the event."""
        self.fill(4)
        acted_path = Path(self.root) / "acted.txt"
        script = (
            "import os, sys\n"
            "sys.path.insert(0, %r)\n"
            "from neoxider_agents.events import EventStore\n"
            "store = EventStore(%r)\n"
            "def cb(record):\n"
            "    with open(%r, 'a') as f:\n"
            "        f.write(str(record['seq']) + '\\n')\n"
            "        f.flush()\n"
            "        os.fsync(f.fileno())\n"
            "    if record['seq'] == 2:\n"
            "        os._exit(3)\n"
            "store.drain(%r, cb, 'child')"
        ) % (str(ROOT), self.root, str(acted_path), TASK)
        finished = subprocess.run([sys.executable, "-B", "-c", script], capture_output=True, timeout=30)
        self.assertEqual(finished.returncode, 3, finished.stderr.decode("utf-8", "replace"))
        self.assertEqual(self.store.cursor(TASK, "child"), 1)
        replay = []
        self.store.drain(TASK, replay.append, "child")
        self.assertEqual([record["seq"] for record in replay], [2, 3, 4])
        self.assertEqual(acted_path.read_text().splitlines(), ["1", "2"])

    def test_no_exactly_once_claim_for_an_in_memory_subscriber(self):
        self.fill(3)
        first = []
        self.store.drain(TASK, first.append, None)
        second = []
        self.store.drain(TASK, second.append, None)
        self.assertEqual(len(first), 3)
        self.assertEqual(len(second), 3, "without a durable cursor a re-run replays everything")

    def test_subscribe_until_predicate_and_durable_ack(self):
        self.fill(4)
        seen = []
        report = self.store.subscribe(TASK, seen.append, consumer="watcher",
                                      until=lambda record: record["kind"] == "task.step" and record["seq"] == 2)
        self.assertEqual([record["seq"] for record in seen], [1, 2])
        self.assertTrue(report.stopped)
        self.assertEqual(report.cursor, 2)
        self.assertEqual(self.store.cursor(TASK, "watcher"), 2)

    def test_subscribe_waits_for_a_later_event_from_another_thread(self):
        self.fill(1)
        seen = []

        def producer():
            time.sleep(0.15)
            self.emit("task.completed", {"exit": 0})

        thread = threading.Thread(target=producer)
        thread.start()
        try:
            report = self.store.subscribe(TASK, seen.append, consumer="live", poll_interval=0.02,
                                          until=lambda record: record["kind"] == "task.completed", timeout=30)
        finally:
            thread.join(30)
        self.assertEqual([record["kind"] for record in seen], ["task.step", "task.completed"])
        self.assertEqual(report.cursor, 2)
        self.assertEqual(self.store.cursor(TASK, "live"), 2)

    def test_wait_timeout_is_explicit_and_acknowledges_nothing(self):
        self.fill(2)
        with self.assertRaises(WaitTimeout):
            self.store.wait(TASK, after=2, timeout=0.2, poll_interval=0.02)
        self.assertEqual(self.store.cursor(TASK, "reader"), 0)
        page = self.store.wait(TASK, after=0, timeout=1)
        self.assertEqual(len(page.events), 2)

    def test_wait_and_subscribe_honour_cancellation(self):
        self.fill(1)
        token = threading.Event()
        token.set()
        with self.assertRaises(WaitTimeout) as caught:
            self.store.wait(TASK, after=1, timeout=30, poll_interval=0.02, stop=token.is_set)
        self.assertIn("cancelled", str(caught.exception))
        self.assertEqual(self.store.drain(TASK, lambda record: None, "stopper", acknowledge=True).delivered, 1)

    def test_subscribe_stops_when_the_token_fires(self):
        self.fill(2)
        flag = []
        report = self.store.subscribe(TASK, lambda record: flag.append(record["seq"]), consumer="stopper",
                                     poll_interval=0.02, timeout=30,
                                     stop=lambda: len(flag) >= 2 and True)
        self.assertEqual(flag, [1, 2])
        self.assertTrue(report.stopped)
        self.assertEqual(self.store.cursor(TASK, "stopper"), 2)


class Corruption(EventTestCase):
    def append_raw(self, text, task=TASK):
        with self.log_path(task).open("a", encoding="ascii", newline="") as stream:
            stream.write(text)

    def emit_two(self):
        self.emit("task.start", {"i": 1})
        self.emit("task.completed", {"i": 2})

    def test_torn_trailing_write_is_recovered_honestly(self):
        self.emit_two()
        intact = self.log_path().read_bytes()
        fragment = '{"schema":1,"id":"abc","seq":3,"task":"events-t'
        self.append_raw(fragment)
        page = self.store.read(TASK)
        self.assertEqual(len(page.events), 2)
        self.assertEqual(page.torn, True)
        self.assertEqual(page.cursor, 2)
        self.assertEqual(self.log_path().read_bytes(), intact + fragment.encode("ascii"))
        report = self.store.inspect(TASK)
        self.assertEqual((report["records"], report["head"], report["torn"]), (2, 2, True))
        # Preserve the damaged original before repairing; reuse the incomplete sequence.
        self.emit("task.next", {"i": 3})
        self.assertEqual(self.store.head(TASK), 3)
        self.assertEqual(self.store.read(TASK).torn, False)
        backups = list(Path(self.root).glob("*.torn.*.bak"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), intact + fragment.encode("ascii"))
        self.assertTrue(self.log_path().read_bytes().startswith(intact))

    def test_interior_damage_fails_instead_of_skipping_data(self):
        self.emit_two()
        path = self.log_path()
        lines = path.read_text(encoding="ascii").splitlines(True)
        path.write_text(lines[0] + "{not json}\n" + lines[1], encoding="ascii")
        with self.assertRaises(EventCorruption) as caught:
            self.store.read(TASK)
        self.assertIn("interior", str(caught.exception).lower())

    def test_interior_damage_after_a_torn_line_is_not_mistaken_for_a_tail(self):
        self.emit_two()
        path = self.log_path()
        lines = path.read_text(encoding="ascii").splitlines(True)
        path.write_text(lines[0] + '{"schema":1,"id":"tor' + lines[1], encoding="ascii")
        with self.assertRaises(EventCorruption) as caught:
            self.store.read(TASK)
        self.assertIn("interior corruption", str(caught.exception))

    def test_a_mutated_payload_is_detected_instead_of_served_as_fact(self):
        self.emit("task.start", {"exit": 0})
        path = self.log_path()
        record = json.loads(path.read_text(encoding="ascii"))
        record["data"] = {"exit": 1}
        path.write_text(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n", encoding="ascii")
        with self.assertRaises(EventCorruption) as caught:
            self.store.read(TASK)
        self.assertIn("digest mismatch", str(caught.exception))

    def test_a_foreign_task_record_is_corruption_not_a_cross_task_hit(self):
        self.emit("task.start", {"i": 1})
        borrowed = json.loads(self.log_path().read_text(encoding="ascii"))
        borrowed["task"] = "other-task"
        borrowed["seq"] = 2
        self.append_raw(json.dumps(borrowed, sort_keys=True, separators=(",", ":")) + "\n")
        with self.assertRaises(EventCorruption) as caught:
            self.store.read(TASK)
        self.assertIn("belongs to task 'other-task'", str(caught.exception))

    def test_a_sequence_reset_is_detected(self):
        self.emit_two()
        lines = self.log_path().read_text(encoding="ascii").splitlines(True)
        rewound = json.loads(lines[1])
        rewound["seq"] = 1
        self.log_path().write_text(lines[0] + json.dumps(rewound, sort_keys=True, separators=(",", ":")) + "\n",
                                   encoding="ascii")
        with self.assertRaises(EventCorruption) as caught:
            self.store.read(TASK)
        self.assertIn("sequence was reset", str(caught.exception))

    def test_a_damaged_acknowledgment_ledger_fails_honestly(self):
        self.emit("task.step", {"i": 1})
        self.store.ack(TASK, "gui", 1)
        with self.ack_path_open(TASK) as stream:
            stream.write("not json\n")
        with self.assertRaises(EventCorruption) as caught:
            self.store.cursor(TASK, "gui")
        self.assertIn("complete JSON acknowledgment", str(caught.exception))

    def ack_path_open(self, task):
        return (Path(self.root) / (task + ".events.acks.jsonl")).open("a", encoding="ascii", newline="")

    def test_a_torn_acknowledgment_tail_is_recovered(self):
        self.emit("task.step", {"i": 1})
        self.store.ack(TASK, "gui", 1)
        with self.ack_path_open(TASK) as stream:
            stream.write('{"schema":1,"task":"events-task","cons')
        self.assertEqual(self.store.cursor(TASK, "gui"), 1)
        self.assertTrue(self.store.ack(TASK, "gui", 1)["advanced"] is False)
        self.assertTrue(self.store.ack_path(TASK).read_bytes().endswith(b"\n"))
        self.assertEqual(len(list(Path(self.root).glob("*.acks.jsonl.torn.*.bak"))), 1)


class Concurrency(EventTestCase):
    def test_separate_processes_produce_one_gapless_sequence(self):
        context = multiprocessing.get_context("spawn")
        gate = context.Event()
        procs = [context.Process(target=worker, args=(self.root, TASK, 12, "p%d" % index, gate))
                 for index in range(4)]
        try:
            for proc in procs:
                proc.start()
            gate.set()
            for proc in procs:
                proc.join(30)
                self.assertEqual(proc.exitcode, 0, "writer process failed")
        finally:
            for proc in procs:
                if proc.is_alive():
                    proc.terminate()
                if proc.pid is not None:
                    proc.join(10)
                    proc.close()
        page = self.store.read(TASK, limit=1000)
        self.assertEqual([record["seq"] for record in page.events], list(range(1, 49)))
        self.assertEqual(page.head, 48)
        self.assertEqual(page.more, False)
        tags = sorted(record["data"]["tag"] for record in page.events)
        self.assertEqual(tags, sorted(["p%d" % index for index in range(4)] * 12))
        self.assertEqual(self.store.inspect(TASK)["records"], 48)

    def test_a_writer_and_a_long_lived_reader_do_not_deadlock_or_tear(self):
        stop = threading.Event()
        seen = []
        errors = []

        def reader():
            try:
                while not stop.is_set():
                    page = self.store.read(TASK, after=len(seen))
                    seen.extend(record["seq"] for record in page.events)
                    time.sleep(0.001)
            except Exception as error:  # pragma: no cover - surfaced by the assertions below
                errors.append(error)

        thread = threading.Thread(target=reader)
        thread.start()
        try:
            for index in range(60):
                self.emit("task.step", {"i": index})
        finally:
            stop.set()
            thread.join(60)
        self.assertEqual(errors, [])
        self.assertEqual(self.store.head(TASK), 60)
        again = []
        self.store.drain(TASK, again.append, "late")
        self.assertEqual([record["seq"] for record in again], list(range(1, 61)))
        self.assertEqual(seen, list(range(1, len(seen) + 1)))


class TaskIsolation(EventTestCase):
    def test_an_id_from_one_task_never_resolves_in_another(self):
        first = self.emit("task.start", {"i": 1}, task="alpha")
        self.emit("task.start", {"i": 2}, task="beta")
        self.assertIsNone(self.store.find("beta", first["id"]))
        self.assertEqual(self.store.find("alpha", first["id"])["id"], first["id"])
        self.assertEqual([record["seq"] for record in self.store.read("beta")], [1])
        self.assertEqual(self.store.head("alpha"), 1)
        self.assertEqual(self.store.head("beta"), 1)

    def test_cursors_are_per_task(self):
        self.emit("task.step", {"i": 1}, task="alpha")
        self.emit("task.step", {"i": 1}, task="beta")
        self.store.ack("alpha", "gui", 1)
        self.assertEqual(self.store.cursor("alpha", "gui"), 1)
        self.assertEqual(self.store.cursor("beta", "gui"), 0)

    def test_events_files_stay_out_of_the_legacy_globs(self):
        self.emit("task.step", {"i": 1})
        names = sorted(path.name for path in Path(self.root).iterdir())
        self.assertIn("events-task.events.jsonl", names)
        self.assertFalse(any(name.endswith(".msg") for name in names))
        self.assertFalse(any(name.endswith(".lock.d") for name in names))


class Locking(EventTestCase):
    def test_emit_holds_the_existing_state_lock_for_the_log_file(self):
        store = EventStore(self.root)
        with Lock(self.log_path()), mock.patch("neoxider_agents.events.Lock",
                                              side_effect=lambda path: Lock(path, timeout=0.1)):
            with self.assertRaises(ValueError) as caught:
                store.emit(TASK, "", "", "kind", {"i": 1}, allow_secrets=True)
        self.assertIn("lock timed out", str(caught.exception))
        store.emit(TASK, "", "", "kind", {"i": 1})
        self.assertEqual(self.store.head(TASK), 1)

    def test_ack_holds_the_lock_for_the_acknowledgment_ledger(self):
        self.emit("task.step", {"i": 1})
        store = EventStore(self.root)
        with Lock(Path(self.root) / (TASK + ".events.acks.jsonl")), mock.patch(
                "neoxider_agents.events.Lock", side_effect=lambda path: Lock(path, timeout=0.1)):
            with self.assertRaises(ValueError):
                store.ack(TASK, "gui", 1)
        self.assertTrue(store.ack(TASK, "gui", 1)["advanced"])


class RecordsApi(EventTestCase):
    def test_records_of_streams_records_and_a_missing_log_is_empty(self):
        self.emit("task.step", {"i": 1})
        self.emit("task.step", {"i": 2})
        self.assertEqual([record["seq"] for record in records_of(self.log_path(), TASK)], [1, 2])
        self.assertEqual(list(records_of(self.log_path("absent-task"), "absent-task")), [])
        with self.assertRaises(ValueError):
            list(records_of(self.log_path(), "absent-task"))

    def test_emit_returned_records_are_validated_round_trip(self):
        record = self.emit("task.step", {"i": 1})
        self.assertEqual(self.store.find(TASK, record["id"]), record)


class RecoveryAndBoundaries(EventTestCase):
    def test_real_parent_labels_and_sdk_ids_survive_restart(self):
        for parent in ("root-orchestrator", "sdk-1234abcd", "/root/events", "agent:user@host"):
            record = self.emit(parent=parent, generation="gen:2")
            self.assertEqual(EventStore(self.root).find(TASK, record["id"])["parent"], parent)
        for parent in ("x" * 129, "bad parent", "bad\nparent", 17, None):
            with self.assertRaises(ValueError):
                self.emit(parent=parent)

    def test_returned_and_serialized_cursors_refuse_cross_task_injection(self):
        self.emit(task="alpha")
        self.emit(task="beta")
        page = self.store.read("alpha")
        serialized = json.loads(json.dumps(page.to_dict()))["cursor"]
        for cursor in (page.cursor, serialized, self.store.cursor("alpha", "gui")):
            with self.assertRaises(ValueError):
                self.store.read("beta", after=cursor)
            with self.assertRaises(ValueError):
                self.store.ack("beta", "gui", cursor)
            with self.assertRaises(ValueError):
                self.store.subscribe("beta", lambda event: None, after=cursor, timeout=0)
        self.assertEqual(len(self.store.read("alpha", after=serialized)), 0)

    def test_earliest_uuid_retry_and_terminal_replay_over_four_megabytes(self):
        terminal = self.emit("task.completed", {"exit": 0}, generation="1", parent="root")
        for index in range(42):
            self.emit(data={"index": index, "blob": "x" * 110000}, generation="2", parent="root")
        self.assertGreater(self.log_path().stat().st_size, 4 * 1024 * 1024)
        reopened = EventStore(self.root)
        again = reopened.emit(TASK, "1", "root", "task.completed", {"exit": 0}, event_id=terminal["id"])
        self.assertTrue(again["duplicate"])
        self.assertEqual(again["seq"], 1)
        with self.assertRaises(ValueError):
            reopened.emit(TASK, "1", "root", "task.completed", {"exit": 1}, event_id=terminal["id"])
        tracemalloc.start()
        try:
            page = reopened.read(TASK, limit=1)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertEqual(page.events[0]["id"], terminal["id"])
        self.assertLess(peak, 2 * 1024 * 1024, "one-row page must not retain the full log")
        seen = []
        reopened.drain(TASK, lambda event: seen.append(event["id"]), limit=3)
        self.assertEqual(seen[0], terminal["id"])
        self.assertEqual(len(seen), 43)

    def test_busy_subscription_checks_cancel_inside_a_page(self):
        for index in range(12):
            self.emit(data={"i": index})
        seen = []
        def callback(event):
            seen.append(event["seq"])
            self.emit(data={"more": len(seen)})
        report = self.store.subscribe(TASK, callback, "busy", limit=12,
                                      stop=lambda: len(seen) >= 2, timeout=2)
        self.assertEqual(seen, [1, 2])
        self.assertTrue(report.stopped)
        self.assertEqual(self.store.cursor(TASK, "busy"), 2)

    def test_busy_subscription_checks_timeout_even_with_continuous_events(self):
        self.emit()
        seen = []
        def callback(event):
            seen.append(event["seq"])
            self.emit(data={"more": len(seen)})
            time.sleep(0.005)
        started = time.monotonic()
        with self.assertRaises(WaitTimeout):
            self.store.subscribe(TASK, callback, "busy", timeout=0.04, limit=1)
        self.assertLess(time.monotonic() - started, 1)
        self.assertTrue(seen)
        self.assertEqual(self.store.cursor(TASK, "busy"), len(seen))
        self.assertEqual(self.store.head(TASK), len(seen) + 1)

    def test_precancelled_wait_does_not_return_available_events(self):
        self.emit()
        with self.assertRaises(WaitTimeout):
            self.store.wait(TASK, stop=lambda: True)
        self.assertEqual(self.store.subscribe(TASK, lambda event: self.fail("cancelled"),
                                              stop=lambda: True).delivered, 0)

    def test_callback_mutation_cannot_acknowledge_unseen_events(self):
        for index in range(3):
            self.emit(data={"i": index})
        def mutate(event):
            event["seq"] = 3
        report = self.store.subscribe(TASK, mutate, "mutation", until=lambda event: True)
        self.assertEqual(report.cursor, 1)
        self.assertEqual(self.store.cursor(TASK, "mutation"), 1)

    def test_full_validation_prevents_tail_and_id_lookup_bypasses(self):
        first = self.emit()
        last = self.emit(data={"i": 2})
        lines = self.log_path().read_bytes().splitlines(True)
        bad = lines[0] + b"not-json\n" + lines[1]
        self.log_path().write_bytes(bad)
        for operation in (lambda: self.store.head(TASK),
                          lambda: self.store.find(TASK, first["id"]),
                          lambda: self.store.read(TASK, limit=1),
                          lambda: self.emit(event_id=last["id"]),
                          lambda: self.emit(),
                          lambda: self.store.ack(TASK, "gui", 1)):
            with self.assertRaises(EventCorruption):
                operation()
            self.assertEqual(self.log_path().read_bytes(), bad)

    def test_child_crash_leaves_torn_tail_and_dead_lock_repaired_before_append(self):
        self.emit()
        intact = self.log_path().read_bytes()
        fragment = b'{"schema":1,"seq":2'
        script = (
            "import os, sys\n"
            "sys.path.insert(0, %r)\n"
            "from pathlib import Path\n"
            "from neoxider_agents.state import Lock\n"
            "path = Path(%r)\n"
            "with Lock(path):\n"
            "    with path.open('ab') as f:\n"
            "        f.write(%r)\n"
            "        f.flush()\n"
            "        os.fsync(f.fileno())\n"
            "    os._exit(7)\n"
        ) % (str(ROOT), str(self.log_path()), fragment)
        result = subprocess.run([sys.executable, "-B", "-c", script], capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertTrue(self.store.read(TASK).torn)
        self.assertEqual(self.emit()["seq"], 2)
        self.assertFalse(self.store.read(TASK).torn)
        saved = list(Path(self.root).glob("*.torn.*.bak"))
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0].read_bytes(), intact + fragment)

    def test_ack_torn_tail_is_backed_up_and_repaired_before_advancing(self):
        for index in range(3):
            self.emit(data={"i": index})
        self.store.ack(TASK, "gui", 1)
        path = self.store.ack_path(TASK)
        intact = path.read_bytes()
        fragment = b'{"schema":1,"consumer":"gui"'
        with path.open("ab") as f:
            f.write(fragment)
        self.store.ack(TASK, "gui", 2)
        self.assertEqual(self.store.cursor(TASK, "gui"), 2)
        self.assertEqual([json.loads(row)["seq"] for row in path.read_bytes().splitlines()], [1, 2])
        backups = list(Path(self.root).glob("*.acks.jsonl.torn.*.bak"))
        self.assertEqual(backups[0].read_bytes(), intact + fragment)
        self.assertTrue(path.read_bytes().startswith(intact))

    def test_ack_regression_invalid_types_and_journal_sequence_fail_closed(self):
        for index in range(3):
            self.emit(data={"i": index})
        self.store.ack(TASK, "gui", 2)
        path = self.store.ack_path(TASK)
        intact = path.read_bytes()
        original = json.loads(intact)
        for changes in ({"seq": 2, "cursor": 1}, {"seq": 2, "cursor": True},
                        {"seq": 2, "cursor": 4}, {"seq": 1, "cursor": 3},
                        {"seq": 2, "cursor": 3, "epoch": "yesterday"},
                        {"seq": 2, "cursor": 3, "consumer": "../escape"}):
            row = dict(original, **changes)
            bad = intact + (json.dumps(row) + "\n").encode("ascii")
            path.write_bytes(bad)
            with self.assertRaises(EventCorruption):
                self.store.cursor(TASK, "gui")
            with self.assertRaises(EventCorruption):
                self.store.ack(TASK, "gui", 3)
            self.assertEqual(path.read_bytes(), bad)

    def test_multiprocess_ack_races_preserve_highest_value_and_ordered_journal(self):
        for index in range(8):
            self.emit(data={"i": index})
        context = multiprocessing.get_context("spawn")
        gate = context.Event()
        procs = [context.Process(target=ack_worker, args=(self.root, TASK, value, gate))
                 for value in (3, 8, 1, 6)]
        try:
            for proc in procs:
                proc.start()
            gate.set()
            for proc in procs:
                proc.join(30)
                self.assertEqual(proc.exitcode, 0)
        finally:
            for proc in procs:
                if proc.is_alive():
                    proc.terminate()
                if proc.pid is not None:
                    proc.join(10)
                    proc.close()
        self.assertEqual(self.store.cursor(TASK, "race"), 8)
        rows = [json.loads(row) for row in self.store.ack_path(TASK).read_text().splitlines()]
        self.assertEqual([row["seq"] for row in rows], list(range(1, len(rows) + 1)))
        values = [row["cursor"] for row in rows]
        self.assertEqual(values, sorted(set(values)))

    def test_linked_sidecars_and_path_suffix_injection_are_rejected(self):
        for task in ("CON", "nul.json", "COM1", "task."):
            with self.assertRaises(ValueError):
                self.emit(task=task)
        with self.assertRaises(ValueError):
            self.store.path(TASK, "/../../escape")
        other = Path(self.root) / "other.txt"
        other.write_bytes(b"unchanged")
        os.link(str(other), str(self.log_path()))
        with self.assertRaises(ValueError):
            self.emit()
        self.assertEqual(other.read_bytes(), b"unchanged")

    def test_bad_complete_record_fields_are_corruption(self):
        self.emit()
        original = json.loads(self.log_path().read_text())
        for changes in ({"epoch": float("inf")}, {"seq": True}, {"schema": True},
                        {"generation": []}, {"parent": "bad parent"}, {"data": []}):
            row = dict(original, **changes)
            self.log_path().write_text(json.dumps(row) + "\n")
            with self.assertRaises(EventCorruption):
                self.store.head(TASK)

    def test_secret_rejection_does_not_write_logs_or_echo_values(self):
        for data in ({"nested": {"password": FAKE_KEY}}, {"text": FAKE_KEY}):
            with self.assertRaises(ValueError) as caught:
                self.emit(data=data)
            self.assertNotIn(FAKE_KEY, str(caught.exception))
            self.assertEqual(list(Path(self.root).iterdir()), [])
        with self.assertRaises(ValueError):
            self.emit(data={"nested": {1: "value"}})

    def test_oversize_unterminated_line_fails_closed_without_unbounded_read(self):
        self.log_path().write_bytes(b"x" * 300000)
        before = hashlib.sha256(self.log_path().read_bytes()).hexdigest()
        with self.assertRaises(EventCorruption):
            self.emit()
        self.assertEqual(hashlib.sha256(self.log_path().read_bytes()).hexdigest(), before)

    def test_consumer_fold_has_a_hard_memory_bound(self):
        self.emit()
        self.store.ack(TASK, "c0", 1)
        path = self.store.ack_path(TASK)
        row = json.loads(path.read_text())
        with path.open("w") as f:
            for index in range(1024):
                f.write(json.dumps(dict(row, seq=index + 1, consumer="c%d" % index)) + "\n")
        self.assertEqual(len(self.store.cursors(TASK)), 1024)
        before = path.read_bytes()
        with self.assertRaises(ValueError):
            self.store.ack(TASK, "overflow", 1)
        self.assertEqual(path.read_bytes(), before)
        with path.open("a") as f:
            f.write(json.dumps(dict(row, seq=1025, consumer="overflow")) + "\n")
        with self.assertRaises(EventCorruption):
            self.store.cursors(TASK)

    def test_backup_failure_never_truncates_live_history(self):
        self.emit()
        with self.log_path().open("ab") as f:
            f.write(b'{"torn":')
        before = self.log_path().read_bytes()
        with mock.patch("neoxider_agents.events._sync_directory", side_effect=OSError("backup sync failed")):
            with self.assertRaises(OSError):
                self.emit()
        self.assertEqual(self.log_path().read_bytes(), before)
        self.assertEqual(self.emit()["seq"], 2)

    def test_short_writes_are_completed_and_zero_progress_raises(self):
        write = os.write
        def short_write(fd, data):
            return write(fd, data[:17])
        with mock.patch("neoxider_agents.events.os.write", side_effect=short_write):
            event = self.emit()
        self.assertEqual(self.store.find(TASK, event["id"])["seq"], 1)
        with mock.patch("neoxider_agents.events.os.write", return_value=0):
            with self.assertRaises(OSError):
                self.emit()
        self.assertEqual(self.store.head(TASK), 1)


if __name__ == "__main__":
    unittest.main()
