"""Durable message receipts: crash, concurrency and honesty acceptance.

Real temp Store, real locks, real child processes, real provider fixture child.
No AI, network, GUI or SDK surface is touched.
"""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from neoxider_agents import lifecycle, message_receipts as mr, views
from neoxider_agents.logs import prune_task
from neoxider_agents.providers import BaseProvider
from neoxider_agents.state import Lock, Store

FIXTURE = ROOT / "tests/fixtures/core-provider.py"

CRASH_CHILD = r'''
import os, sys
sys.path.insert(0, os.environ["RECEIPT_ROOT"])
from neoxider_agents import message_receipts as mr
from neoxider_agents.state import Store
store = Store(os.environ["RECEIPT_LOGS"])
if sys.argv[1] == "before-queue":
    mr.atomic_write = lambda *a, **k: os._exit(9)
elif sys.argv[1] == "after-queue":
    mr.persist_sequence = lambda *a, **k: os._exit(9)
record = store.enqueue_receipt("task", "CRASHED PAYLOAD", sys.argv[2])
print("accepted " + record["id"])
'''

ENQUEUE_CHILD = r'''
import os, sys
sys.path.insert(0, os.environ["RECEIPT_ROOT"])
from neoxider_agents.state import Store
store = Store(os.environ["RECEIPT_LOGS"])
print(store.enqueue("task", sys.argv[1] * 32768))
'''


class ReceiptStore(unittest.TestCase):
    def setUp(self):
        base = Path("D:/Temp/agents-ux/receipts") if os.name == "nt" else Path(tempfile.gettempdir()) / "agents-receipts"
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="receipts-", dir=base)
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.store = Store(self.folder / "logs")
        self.work = self.folder / "work"
        self.work.mkdir()
        self.store.update("task", engine="fixture", dir=str(self.work), state="running", generation="gen-1")

    def enqueue(self, text="PING", message_id=None):
        return self.store.enqueue_receipt("task", text, message_id)

    def receipt(self, ref):
        return self.store.receipt("task", ref)

    def crash(self, mode, message_id):
        environment = dict(os.environ, RECEIPT_ROOT=ROOT.as_posix(), RECEIPT_LOGS=str(self.store.root))
        result = subprocess.run([sys.executable, "-c", CRASH_CHILD, mode, message_id], env=environment,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=15)
        self.assertEqual(result.returncode, 9, result.stdout.decode("utf-8", "replace"))
        return result.stdout.decode("utf-8", "replace")

    def spawn_enqueues(self, count):
        environment = dict(os.environ, RECEIPT_ROOT=ROOT.as_posix(), RECEIPT_LOGS=str(self.store.root))
        workers = [subprocess.Popen([sys.executable, "-c", ENQUEUE_CHILD, chr(65 + index)],
                                    env=environment, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                   for index in range(count)]
        results = []
        for worker in workers:
            out, _ = worker.communicate(timeout=20)
            self.assertEqual(worker.returncode, 0, out.decode("utf-8", "replace"))
            results.append(out.decode("utf-8", "replace").strip())
        return results


class LegacyContractTests(ReceiptStore):
    def test_enqueue_locked_keeps_legacy_sequence_and_message_format(self):
        with Lock(self.store.path("task", ".inbox")):
            first = self.store.enqueue_locked("task", "ONE")
        with Lock(self.store.path("task", ".inbox")):
            second = self.store.enqueue_locked("task", "TWO")
        self.assertEqual((first, second), (1, 2))
        self.assertIsInstance(first, int)
        box = self.store.path("task", ".inbox")
        self.assertEqual((box / "000000000001.msg").read_text(encoding="utf-8"), "ONE")
        self.assertEqual((box / "000000000002.msg").read_text(encoding="utf-8"), "TWO")
        self.assertEqual((box / "sequence").read_text(encoding="utf-8").strip(), "2")
        self.assertEqual([path.name for path in self.store.inbox("task")],
                         ["000000000001.msg", "000000000002.msg"])

    def test_receipts_are_additive_sidecars_outside_the_inbox_glob(self):
        self.enqueue("SIDECAR")
        journal = self.store.path("task", mr.SUFFIX)
        self.assertEqual(journal.parent, self.store.root)
        self.assertFalse([path for path in self.store.path("task", ".inbox").iterdir()
                          if path.name.endswith(mr.SUFFIX)])
        self.assertNotIn("receipts", [path.name for path in self.store.path("task", ".inbox").iterdir()])
        self.store.update("task", state="done")
        prune_task(self.store, "task", force=True)  # raw log prune must not touch receipts
        with contextlib.redirect_stdout(io.StringIO()):
            views.clean(self.store, {})  # ordinary clean, no --purge
        self.assertTrue(journal.is_file())
        self.assertIsNotNone(self.receipt(1))

    def test_sequence_stays_monotonic_after_drain_and_inbox_removal(self):
        for text in ("ONE", "TWO", "THREE"):
            self.enqueue(text)
        box = self.store.path("task", ".inbox")
        files = self.store.inbox("task")
        self.store.begin_delivery("task", files, "gen-1")
        self.store.finish_delivery("task", files, "gen-1", delivered=True)
        for path in self.store.inbox("task"):  # drain: successful delivery removes payloads
            path.unlink()
        self.assertEqual(self.enqueue("FOUR")["sequence"], 4)
        files = self.store.inbox("task")
        self.store.begin_delivery("task", files, "gen-1")
        self.store.finish_delivery("task", files, "gen-1", delivered=True)
        shutil.rmtree(box)  # legacy inbox reset (clean --purge / manual removal)
        self.assertEqual(self.enqueue("FIVE")["sequence"], 5)
        self.store.path("task", mr.COUNTER).unlink()  # counter lost: receipts still bind sequences
        self.assertEqual(self.enqueue("SIX")["sequence"], 6)
        self.assertEqual(sorted(record["sequence"] for record in self.store.receipts("task")),
                         [1, 2, 3, 4, 5, 6])

    def test_sequence_never_reuses_after_counter_reset(self):
        first = self.enqueue("BEFORE")
        files = self.store.inbox("task")
        self.store.begin_delivery("task", files, "gen-1")
        self.store.finish_delivery("task", files, "gen-1", delivered=True)
        shutil.rmtree(self.store.path("task", ".inbox"))
        self.store.path("task", mr.COUNTER).unlink()
        second = self.enqueue("AFTER")
        self.assertEqual((first["sequence"], second["sequence"]), (1, 2))
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(self.receipt(first["id"])["digest"], mr.digest("BEFORE"))
        self.assertEqual(self.receipt(second["id"])["digest"], mr.digest("AFTER"))
        self.assertEqual(self.receipt(2)["id"], second["id"])  # newest wins on a reused sequence


class AcceptanceTransactionTests(ReceiptStore):
    def test_receipt_fsync_failure_does_not_publish_false_acceptance(self):
        identifier = mr.new_id()
        real_write = mr._write_line
        def failing_write(path, line):
            if json.loads(line)["state"] == mr.ACCEPTED:
                with mock.patch.object(mr.os, "fsync", side_effect=OSError("injected receipt fsync failure")):
                    return real_write(path, line)
            return real_write(path, line)
        with mock.patch.object(mr, "_write_line", side_effect=failing_write):
            with self.assertRaises(OSError):
                self.enqueue("DURABLE QUEUE", identifier)
        self.assertEqual(self.receipt(identifier)["state"], mr.INTENT)
        self.assertEqual(len(self.store.inbox("task")), 1)
        retry = self.enqueue("DURABLE QUEUE", identifier)
        self.assertEqual(retry["sequence"], 1)
        self.assertEqual(retry["state"], mr.ACCEPTED)

    def test_oversized_snapshot_cannot_poison_the_journal(self):
        before = self.store.path("task", ".meta").read_bytes()
        with self.assertRaises(mr.ReceiptError):
            mr.enqueue(self.store, "task", "PAYLOAD", generation="x" * mr.MAX_LINE_BYTES)
        self.assertEqual(self.store.receipts("task"), [])
        self.assertEqual(self.store.inbox("task"), [])
        self.assertEqual(self.store.path("task", ".meta").read_bytes(), before)

    def test_valid_counter_is_used_without_inbox_or_receipt_history(self):
        self.store.path("task", mr.COUNTER).write_text("100\n")
        self.assertEqual(self.enqueue("HIGH WATER")["sequence"], 101)

    def test_counter_reset_cannot_erase_an_intent_reservation(self):
        identifier = mr.new_id()
        self.crash("before-queue", identifier)
        self.store.path("task", mr.COUNTER).write_text("0\n")
        self.assertEqual(self.enqueue("NEXT")["sequence"], 2)
        retry = self.enqueue("CRASHED PAYLOAD", identifier)
        self.assertEqual(retry["sequence"], 1)
        self.assertEqual(self.enqueue("AFTER RETRY")["sequence"], 3)

    def test_corrupt_counters_fail_closed_without_mutating_the_queue(self):
        first = self.enqueue("PRESERVE")
        for path in (mr.counter_path(self.store, "task"), self.store.path("task", ".inbox") / "sequence"):
            original = path.read_bytes()
            for value in ("bad", "-1", "", "1.5", "9" * 19):
                path.write_text(value)
                before = mr.path_for(self.store, "task").read_bytes()
                with self.assertRaises(mr.ReceiptError):
                    self.enqueue("REJECT")
                self.assertEqual(mr.path_for(self.store, "task").read_bytes(), before)
                self.assertEqual(len(self.store.inbox("task")), 1)
            path.write_bytes(original)
        self.assertEqual(self.receipt(first["id"])["state"], mr.ACCEPTED)

    def test_public_lookup_reports_missing_accepted_payload_as_corruption(self):
        record = self.enqueue("MISSING")
        mr.inbox_file(self.store, "task", record["sequence"]).unlink()
        for action in (lambda: self.receipt(record["id"]), lambda: self.store.receipts("task"),
                       lambda: self.enqueue("MISSING", record["id"]),
                       lambda: self.store.reconcile_receipts("task")):
            with self.assertRaises(mr.ReceiptError):
                action()

    def test_torn_valid_json_is_ignored_and_exact_backup_survives_repair(self):
        record = self.enqueue("ORIGINAL")
        path = mr.path_for(self.store, "task")
        with path.open("ab") as stream:
            stream.write(json.dumps(dict(record, state=mr.DELIVERED)).encode("ascii"))
        before = path.read_bytes()
        self.assertEqual(self.receipt(record["id"])["state"], mr.ACCEPTED)
        self.enqueue("REPAIR")
        self.assertEqual(path.with_name(path.name + mr.BACKUP).read_bytes(), before)
        self.assertFalse(self.store.torn_receipt_tail("task"))

    def test_interior_corruption_and_rank_regression_refuse_repair(self):
        record = self.enqueue("EVIDENCE")
        path = mr.path_for(self.store, "task")
        original = path.read_bytes()
        with path.open("ab") as stream:
            stream.write(b"not json\n" + original.splitlines()[-1] + b"\n")
        before = path.read_bytes()
        for action in (lambda: self.receipt(record["id"]), lambda: self.store.repair_receipts("task"),
                       lambda: self.enqueue("CANNOT ACCEPT")):
            with self.assertRaises(mr.ReceiptError):
                action()
        self.assertEqual(path.read_bytes(), before)
        path.write_bytes(original)
        self.store.acknowledge_message("task", record["id"], text="explicit assistant evidence")
        previous = self.receipt(record["id"])
        with path.open("ab") as stream:
            stream.write((json.dumps(dict(record, rev=previous["rev"] + 1)) + "\n").encode("ascii"))
        with self.assertRaises(mr.ReceiptError):
            self.receipt(record["id"])
        with self.assertRaises(mr.ReceiptError):
            self.store.repair_receipts("task")

    def test_accepted_receipt_is_opaque_durable_and_not_a_sequence(self):
        record = self.enqueue("HELLO")
        self.assertEqual(record["state"], mr.ACCEPTED)
        self.assertRegex(record["id"], mr.ID)
        self.assertNotEqual(record["id"], str(record["sequence"]))
        self.assertEqual(record["digest"], mr.digest("HELLO"))
        self.assertEqual((record["delivery_status"], record["attempts"], record["applied"]), ("pending", 0, 0))
        self.assertEqual(record["acknowledged_epoch"], 0.0)
        self.assertEqual(record["queued_generation"], "gen-1")  # stored, not read back from .meta
        self.assertEqual(self.receipt(record["id"])["sequence"], 1)
        self.assertEqual(self.receipt(1)["id"], record["id"])
        self.assertEqual([row["id"] for row in self.store.receipts("task")], [record["id"]])

    def test_same_id_same_payload_reuses_and_different_payload_is_rejected(self):
        message_id = mr.new_id()
        first = self.enqueue("SAME", message_id)
        again = self.enqueue("SAME", message_id)
        self.assertEqual(first["id"], again["id"])
        self.assertEqual(again["sequence"], first["sequence"])
        self.assertEqual([path.name for path in self.store.inbox("task")], ["000000000001.msg"])
        self.assertEqual(len([row for row in mr.journal(self.store, "task")[0] if row["id"] == message_id]), 2)
        with self.assertRaises(ValueError) as caught:
            self.enqueue("OTHER", message_id)
        self.assertIn("already bound to different content", str(caught.exception))
        self.assertEqual([path.name for path in self.store.inbox("task")], ["000000000001.msg"])
        self.assertEqual(self.receipt(message_id)["digest"], mr.digest("SAME"))
        self.assertEqual(len(self.store.receipts("task")), 1)

    def test_crash_between_intent_and_queue_rolls_back_without_fabricating_a_message(self):
        message_id = mr.new_id()
        self.crash("before-queue", message_id)
        self.assertEqual(self.store.inbox("task"), [])
        self.assertEqual(self.receipt(message_id)["state"], mr.INTENT)
        self.store.reconcile_receipts("task")
        records = self.store.receipts("task")
        self.assertEqual([row["state"] for row in records], [mr.ROLLED_BACK])
        self.assertEqual(records[0]["id"], message_id)
        self.assertIn("no durable queue file", records[0]["attempt_outcome"])
        self.assertEqual(self.store.reconcile_receipts("task"), [])  # nothing to resurrect
        retry = self.enqueue("CRASHED PAYLOAD", message_id)  # the id is free again, same payload
        self.assertEqual(retry["id"], message_id)
        self.assertEqual(retry["state"], mr.ACCEPTED)
        with self.assertRaises(ValueError):
            self.enqueue("DIFFERENT NOW", message_id)  # a burned id stays bound

    def test_crash_after_queue_before_receipt_keeps_the_caller_id_and_payload(self):
        message_id = mr.new_id()
        self.crash("after-queue", message_id)
        journal = self.store.receipts("task")
        self.assertEqual([row["state"] for row in journal], [mr.INTENT])  # never claimed as accepted
        self.assertEqual((self.store.inbox("task")[0]).read_text(encoding="utf-8"), "CRASHED PAYLOAD")
        retry = self.enqueue("CRASHED PAYLOAD", message_id)
        self.assertEqual(retry["sequence"], journal[0]["sequence"])
        self.assertEqual(len(self.store.inbox("task")), 1)
        adopted = [retry]
        self.assertEqual([row["id"] for row in adopted], [message_id])  # caller UUID survives
        self.assertEqual(adopted[0]["state"], mr.ACCEPTED)
        self.assertEqual(adopted[0]["digest"], mr.digest("CRASHED PAYLOAD"))
        self.assertEqual(self.receipt(message_id)["state"], mr.ACCEPTED)
        self.assertEqual(self.store.reconcile_receipts("task"), [])  # idempotent

    def test_missing_payload_never_becomes_an_accepted_empty_message(self):
        message_id = mr.new_id()
        self.enqueue("REAL", message_id)
        self.store.path("task", mr.SUFFIX).write_bytes(b'{"garbage": true}\n')  # corrupt journal
        with self.assertRaises(mr.ReceiptError):
            self.store.receipts("task")
        with self.assertRaises(mr.ReceiptError):
            self.store.repair_receipts("task")  # refuses to erase a complete corrupt line
        (self.store.path("task", mr.SUFFIX)).write_text("", encoding="ascii")
        for path in self.store.inbox("task"):
            path.unlink()  # payload gone, receipt journal gone too
        self.assertIsNone(mr.ensure_accepted(self.store, "task", 1, origin="reconcile"))
        self.assertEqual(self.store.receipts("task"), [])
        self.assertEqual(self.store.reconcile_receipts("task"), [])

    def test_intent_without_a_durable_payload_is_closed_as_rolled_back(self):
        message_id = mr.new_id()
        self.enqueue("GONE SOON", message_id)
        journal = self.store.path("task", mr.SUFFIX)
        intent = json.loads(journal.read_text(encoding="ascii").splitlines()[-1])
        journal.write_text(json.dumps(dict(intent, state=mr.INTENT), ensure_ascii=True, sort_keys=True) + "\n",
                           encoding="ascii")
        for path in self.store.inbox("task"):
            path.unlink()
        self.assertEqual(self.store.reconcile_receipts("task"), [])
        closed = self.receipt(message_id)
        self.assertEqual(closed["state"], mr.ROLLED_BACK)
        self.assertIn("no durable queue file", closed["attempt_outcome"])

    def test_torn_journal_tail_is_not_concatenated_with_the_next_append(self):
        first = self.enqueue("FIRST")
        journal = self.store.path("task", mr.SUFFIX)
        with journal.open("a", encoding="ascii") as stream:  # simulate a power cut mid-write
            stream.write('{"id": "deadbeefdeadbeefdeadbeefdeadbeef", "sch')
        self.assertTrue(self.store.torn_receipt_tail("task"))
        self.assertEqual(self.receipt(first["id"])["state"], mr.ACCEPTED)  # reader survives
        second = self.enqueue("SECOND")  # the next append repairs instead of gluing
        self.assertFalse(self.store.torn_receipt_tail("task"))
        records, torn = mr.journal(self.store, "task")
        self.assertEqual(torn, 0)
        self.assertTrue(all(row.get("schema") == mr.SCHEMA for row in records))
        self.assertEqual(self.receipt(first["id"])["digest"], mr.digest("FIRST"))
        self.assertEqual(self.receipt(second["id"])["digest"], mr.digest("SECOND"))
        self.assertEqual(self.receipt(second["id"])["sequence"], 2)
        self.assertEqual([path.name for path in self.store.inbox("task")],
                         ["000000000001.msg", "000000000002.msg"])

    def test_oldest_uuid_stays_idempotent_beyond_four_megabytes_of_history(self):
        message_id = mr.new_id()
        oldest = self.enqueue("OLDEST", message_id)
        journal = self.store.path("task", mr.SUFFIX)
        newest = self.enqueue("NEWEST")
        # Large history for a completed message; the oldest UUID is outside any tail.
        history = self.enqueue("HISTORY")
        files = [mr.inbox_file(self.store, "task", history["sequence"])]
        self.store.begin_delivery("task", files, "gen-1")
        self.store.finish_delivery("task", files, "gen-1", delivered=True)
        row = self.receipt(history["id"])
        with journal.open("ab") as stream:
            while stream.tell() < 4 * 1024 * 1024:
                row["rev"] += 1
                stream.write((json.dumps(row, sort_keys=True) + "\n").encode("ascii"))
        self.assertGreater(journal.stat().st_size, 4 * 1024 * 1024)
        import tracemalloc
        tracemalloc.start()
        with mock.patch.object(Path, "read_bytes", side_effect=AssertionError("unbounded read")):
            self.assertEqual(self.receipt(oldest["id"])["id"], oldest["id"])
            self.assertEqual(self.receipt(newest["id"])["id"], newest["id"])
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        self.assertLess(peak, 1024 * 1024)
        replay = self.enqueue("OLDEST", message_id)
        self.assertEqual(replay["id"], oldest["id"])
        self.assertEqual(replay["sequence"], oldest["sequence"])
        self.assertEqual(len(self.store.inbox("task")), 3)
        with self.assertRaises(ValueError):
            self.enqueue("REWRITTEN HISTORY", message_id)


class LegacyOwnerTests(ReceiptStore):
    def test_legacy_adoption_fsync_failure_never_claims_acceptance(self):
        self.legacy_inbox(("LEGACY BYTES",))
        with mock.patch.object(mr.os, "fsync", side_effect=OSError("legacy payload fsync failed")):
            with self.assertRaises(OSError):
                self.store.reconcile_receipts("task")
        self.assertEqual(self.store.receipts("task"), [])
        self.assertEqual(self.store.inbox("task")[0].read_text(), "LEGACY BYTES")

    def legacy_inbox(self, messages=("ORPHAN ONE", "ORPHAN TWO")):
        box = self.store.path("task", ".inbox")
        box.mkdir(mode=0o700, parents=True, exist_ok=True)
        for index, text in enumerate(messages, 1):
            (box / ("%012d.msg" % index)).write_text(text, encoding="utf-8")
        return box

    def test_bash_owner_inbox_is_reconciled_as_accepted_without_a_false_ack(self):
        self.legacy_inbox()
        self.assertEqual(self.store.receipts("task"), [])
        created = self.store.reconcile_receipts("task")
        self.assertEqual([row["state"] for row in created], [mr.ACCEPTED, mr.ACCEPTED])
        self.assertEqual([row["origin"] for row in created], ["reconcile", "reconcile"])
        for row in created:
            self.assertEqual(row["acknowledged_epoch"], 0.0)
            self.assertEqual(row["applied"], 0)
            self.assertEqual(row["delivery_status"], "pending")
            self.assertTrue(mr.pending(row))
        self.assertEqual(self.receipt(1)["digest"], mr.digest("ORPHAN ONE"))
        self.assertEqual(self.store.reconcile_receipts("task"), [])  # idempotent

    def test_foreign_stems_and_missing_receipts_do_not_break_a_legacy_drain(self):
        box = self.legacy_inbox(("ONLY ONE",))
        (box / "not-a-sequence.msg").write_text("FOREIGN", encoding="utf-8")
        files, prompt = self.store.batch_locked("task")
        self.assertEqual(prompt, "Message #1:\nONLY ONE")
        self.store.begin_delivery_locked("task", files, "gen-1")
        self.store.finish_delivery_locked("task", files, "gen-1", delivered=True)
        self.assertEqual(self.receipt(1)["state"], mr.DELIVERED)
        self.assertEqual((box / "not-a-sequence.msg").read_text(encoding="utf-8"), "FOREIGN")


class ConcurrencyTests(ReceiptStore):
    def test_concurrent_threads_publish_complete_unique_ordered_receipts(self):
        errors = []

        def worker(index):
            try:
                with Lock(self.store.path("task", ".inbox")):
                    self.store.enqueue_locked("task", "PAYLOAD %02d %s" % (index, "x" * 4096))
            except Exception as error:  # pragma: no cover - reported below
                errors.append("%s: %s" % (type(error).__name__, error))

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        self.assertEqual(errors, [])
        records = self.store.receipts("task")
        self.assertEqual(len(records), 8)
        self.assertEqual(len({row["id"] for row in records}), 8)
        self.assertEqual(sorted(row["sequence"] for row in records), list(range(1, 9)))
        for record in records:
            path = self.store.path("task", ".inbox") / ("%012d.msg" % record["sequence"])
            self.assertEqual(mr.digest(path.read_text(encoding="utf-8")), record["digest"])

    def test_concurrent_same_id_senders_enqueue_exactly_one_message(self):
        message_id = mr.new_id()
        results, errors = [], []

        def worker():
            try:
                with Lock(self.store.path("task", ".inbox")):
                    results.append(self.store.enqueue_locked("task", "ONCE ONLY", message_id))
            except Exception as error:  # pragma: no cover - reported below
                errors.append("%s: %s" % (type(error).__name__, error))

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        self.assertEqual(errors, [])
        self.assertEqual(sorted(results), [1, 1, 1, 1])
        self.assertEqual([path.name for path in self.store.inbox("task")], ["000000000001.msg"])
        self.assertEqual(len(self.store.receipts("task")), 1)

    def test_multi_process_enqueue_keeps_sequences_unique_and_payloads_whole(self):
        sequences = sorted(int(value) for value in self.spawn_enqueues(3) if value.strip())
        self.assertEqual(sequences, [1, 2, 3])
        records = self.store.receipts("task")
        self.assertEqual(len(records), 3)
        self.assertEqual(len({row["id"] for row in records}), 3)
        for record in records:
            path = self.store.path("task", ".inbox") / ("%012d.msg" % record["sequence"])
            body = path.read_text(encoding="utf-8")
            self.assertEqual(len(body), 32768)
            self.assertEqual(mr.digest(body), record["digest"])

    def test_receipt_mutations_never_nest_the_inbox_lock(self):
        record = self.enqueue("NESTED")
        with Lock(self.store.path("task", ".inbox")):  # the lifecycle send/drain path holds this
            started = time.monotonic()
            self.store.enqueue_receipt_locked("task", "SECOND UNDER THE LOCK")
            files = self.store.inbox("task")
            self.store.begin_delivery_locked("task", files, "gen-1")
            self.store.finish_delivery_locked("task", files, "gen-1", delivered=True)
            self.store.reconcile_receipts_locked("task")
            self.store.acknowledge_message_locked("task", record["id"], source="assistant", text="done")
            self.store.mark_message_applied_locked("task", record["id"], note="checked")
            self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(self.receipt(1)["state"], mr.ACKNOWLEDGED)
        self.assertEqual(self.receipt(2)["state"], mr.DELIVERED)
        with Lock(self.store.path("task", ".inbox"), timeout=0):  # released cleanly
            pass

    def test_lifecycle_send_style_external_lock_nests_every_receipt_write(self):
        record = self.enqueue("FIRST")
        with Lock(self.store.path("task", ".inbox")):
            # Time the operations under an already-held external lock.
            started = time.monotonic()
            sequence = self.store.enqueue_locked("task", "FOLLOW UP")  # exactly lifecycle.send
            files, prompt = self.store.batch_locked("task")
            self.store.begin_delivery_locked("task", files, "gen-9")
            self.store.finish_delivery_locked("task", files, "gen-9", delivered=True)
            mr.cleanup_completed_locked(self.store, "task")
            self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(sequence, 2)
        self.assertIn("Message #1:\nFIRST", prompt)
        self.assertEqual(self.receipt(record["id"])["state"], mr.DELIVERED)
        self.assertEqual(self.receipt(sequence)["state"], mr.DELIVERED)
        self.assertEqual(self.store.inbox("task"), [])

    def test_another_waits_for_the_lock_instead_of_deadlocking(self):
        record = self.enqueue("CONTENDED")
        finished = []

        def worker():
            self.store.acknowledge_message("task", record["id"], source="assistant", text="acknowledged")
            finished.append(time.monotonic())

        with Lock(self.store.path("task", ".inbox")):
            thread = threading.Thread(target=worker, daemon=True)
            thread.start()
            thread.join(2)
            self.assertTrue(thread.is_alive())  # it is waiting for the lock we hold
            self.assertEqual(finished, [])
        thread.join(60)
        self.assertEqual(finished and len(finished), 1)
        self.assertEqual(self.receipt(record["id"])["state"], mr.ACKNOWLEDGED)

    def test_concurrent_acknowledgments_keep_one_consistent_revision(self):
        record = self.enqueue("RACE")
        errors = []

        def worker(index):
            try:
                self.store.acknowledge_message("task", record["id"], source="assistant",
                                               text="acknowledged by worker %d" % index)
            except Exception as error:  # pragma: no cover - reported below
                errors.append("%s: %s" % (type(error).__name__, error))

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        self.assertEqual(errors, [])
        final = self.receipt(record["id"])
        self.assertEqual(final["state"], mr.ACKNOWLEDGED)
        self.assertTrue(final["ack_digest"].startswith("sha256:"))
        self.assertFalse(mr.pending(final))
        self.assertEqual(final["applied"], 0)
        acks = [row for row in mr.journal(self.store, "task")[0] if row["state"] == mr.ACKNOWLEDGED]
        self.assertEqual(len(acks), 6)  # no lost update: every read-modify-write is serialized
        self.assertEqual(len({row["rev"] for row in acks}), 6)


class DeliveryHonestyTests(ReceiptStore):
    def test_default_begin_binds_to_generation_and_refuses_a_missing_generation(self):
        self.enqueue("BOUND BY DEFAULT")
        paths = self.store.inbox("task")
        started = self.store.begin_delivery("task", paths)
        self.assertEqual(started[0]["attempt_generation"], "gen-1")
        self.store.update("task", generation="")
        with self.assertRaises(ValueError):
            self.store.begin_delivery("task", paths)

    def test_ack_finish_race_preserves_both_explicit_ack_and_completed_delivery(self):
        record = self.enqueue("RACE BOUNDARY")
        paths = self.store.inbox("task")
        self.store.begin_delivery("task", paths, "gen-1")
        barrier = threading.Barrier(2)
        errors = []
        def worker(ack):
            try:
                barrier.wait(timeout=3)
                if ack:
                    self.store.acknowledge_message("task", record["id"], text="explicit assistant acknowledgment")
                else:
                    self.store.finish_delivery("task", paths, "gen-1", delivered=True)
            except Exception as error:
                errors.append(error)
        threads = [threading.Thread(target=worker, args=(ack,)) for ack in (True, False)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        final = self.receipt(record["id"])
        self.assertEqual(final["state"], mr.ACKNOWLEDGED)
        self.assertEqual(final["delivery_status"], mr.SENT)
        self.assertEqual(final["delivered_generation"], "gen-1")

    def test_empty_generation_cannot_complete_an_owned_attempt(self):
        record = self.enqueue("BOUND")
        paths = self.store.inbox("task")
        self.store.begin_delivery("task", paths, "gen-1")
        self.store.finish_delivery("task", paths, "", delivered=True)
        self.assertEqual(self.receipt(record["id"])["delivery_status"], mr.ATTEMPTING)

    def test_completed_queue_cleanup_checks_original_payload_before_unlink(self):
        record = self.enqueue("ORIGINAL")
        paths = self.store.inbox("task")
        self.store.begin_delivery("task", paths, "gen-1")
        self.store.finish_delivery("task", paths, "gen-1", delivered=True)
        paths[0].write_text("DIFFERENT PAYLOAD")
        with Lock(self.store.path("task", ".inbox")):
            with self.assertRaises(mr.ReceiptError):
                self.store.batch_locked("task")
        self.assertEqual(paths[0].read_text(), "DIFFERENT PAYLOAD")

    def test_accepted_delivered_and_acknowledged_are_three_different_facts(self):
        record = self.enqueue("FACT CHECK")
        files = self.store.inbox("task")
        self.store.begin_delivery_locked("task", files, "gen-1")
        attempted = self.receipt(record["id"])
        self.assertEqual(attempted["state"], mr.ACCEPTED)      # intent is not completion
        self.assertEqual(attempted["delivery_status"], mr.ATTEMPTING)
        self.assertTrue(mr.stale_attempt(attempted))
        self.store.finish_delivery_locked("task", files, "gen-1", delivered=True)
        delivered = self.receipt(record["id"])
        self.assertEqual(delivered["state"], mr.DELIVERED)
        self.assertEqual(delivered["delivered_generation"], "gen-1")
        self.assertEqual(delivered["acknowledged_epoch"], 0.0)  # delivery is not acknowledgment
        self.assertEqual(delivered["applied"], 0)               # and never semantic application
        self.assertTrue(mr.pending(delivered))
        self.assertFalse(mr.stale_attempt(delivered))
        with self.assertRaises(ValueError):
            self.store.mark_message_applied("task", record["id"], note="too early")
        acknowledged = self.store.acknowledge_message("task", record["id"], source="assistant",
                                                      text="applied your change in src/app.py",
                                                      note="assistant turn 2")
        self.assertEqual(acknowledged["state"], mr.ACKNOWLEDGED)
        self.assertEqual(acknowledged["applied"], 0)
        self.assertEqual(acknowledged["ack_digest"], mr.digest("applied your change in src/app.py"))
        self.assertFalse(mr.pending(acknowledged))
        applied = self.store.mark_message_applied("task", record["id"], note="verified in working tree")
        self.assertEqual(applied["applied"], 1)
        self.assertIn("verified in working tree", applied["applied_note"])

    def test_acknowledgment_requires_explicit_evidence(self):
        record = self.enqueue("EVIDENCE")
        for kwargs in ({}, {"source": "not a tag", "text": "yes"}, {"source": "assistant"},
                       {"source": "tool", "text": "tool execution succeeded"},
                       {"source": "assistant", "text": "   "}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.store.acknowledge_message("task", record["id"], **kwargs)
        self.assertEqual(self.receipt(record["id"])["state"], mr.ACCEPTED)
        with self.assertRaises(ValueError):
            self.store.acknowledge_message("task", mr.new_id(), source="assistant", text="ghost")
        with self.assertRaises(ValueError):
            self.store.mark_message_applied("task", record["id"], note="guessing")

    def test_delivery_never_regresses_an_acknowledged_receipt(self):
        record = self.enqueue("MONOTONIC")
        files = self.store.inbox("task")
        self.store.begin_delivery_locked("task", files, "gen-2")
        self.store.acknowledge_message("task", record["id"], source="assistant", text="acknowledged first")
        self.store.finish_delivery_locked("task", files, "gen-2", delivered=True)
        final = self.receipt(record["id"])
        self.assertEqual(final["state"], mr.ACKNOWLEDGED)
        self.assertIn("already acknowledged", final["attempt_outcome"])

    def test_failed_invocation_keeps_pending_payload_and_never_claims_delivery(self):
        record = self.enqueue("KEEP ME")
        files = self.store.inbox("task")
        self.store.begin_delivery_locked("task", files, "gen-1")
        self.store.finish_delivery_locked("task", files, "gen-1", delivered=False, outcome="provider exited 42")
        failed = self.receipt(record["id"])
        self.assertEqual(failed["state"], mr.ACCEPTED)
        self.assertEqual(failed["delivery_status"], mr.UNKNOWN)
        self.assertEqual(failed["attempts"], 1)
        self.assertEqual(failed["delivered_epoch"], 0.0)
        self.assertEqual([path.name for path in self.store.inbox("task")], ["000000000001.msg"])
        self.assertEqual(self.store.path("task", ".inbox/000000000001.msg").read_text(encoding="utf-8"), "KEEP ME")

    def test_stale_generation_cannot_complete_a_newer_owners_attempt(self):
        record = self.enqueue("GENERATION")
        files = self.store.inbox("task")
        self.store.begin_delivery_locked("task", files, "gen-a")
        self.store.begin_delivery_locked("task", files, "gen-b")  # a new owner took over
        self.store.finish_delivery_locked("task", files, "gen-a", delivered=True)
        stale = self.receipt(record["id"])
        self.assertEqual(stale["state"], mr.ACCEPTED)
        self.assertEqual(stale["delivery_status"], mr.ATTEMPTING)
        self.assertEqual(stale["attempts"], 2)
        self.assertEqual(stale["attempt_generation"], "gen-b")
        self.assertIn("no longer owns attempt", stale["attempt_outcome"])
        self.store.finish_delivery_locked("task", files, "gen-b", delivered=True)
        settled = self.receipt(record["id"])
        self.assertEqual(settled["state"], mr.DELIVERED)
        self.assertEqual(settled["delivered_generation"], "gen-b")

    def test_replay_after_owner_death_stays_pending_and_at_least_once(self):
        record = self.enqueue("REPLAY ME")
        files = self.store.inbox("task")
        self.store.begin_delivery_locked("task", files, "gen-a")  # owner dies here: no finish, no unlink
        self.store.update("task", state="stopped", generation="gen-b")
        stranded = self.receipt(record["id"])
        self.assertEqual(stranded["state"], mr.ACCEPTED)
        self.assertTrue(mr.stale_attempt(stranded))
        self.assertEqual(self.store.path("task", ".inbox/000000000001.msg").read_text(encoding="utf-8"), "REPLAY ME")
        replay_files, prompt = self.store.batch_locked("task")
        self.assertEqual(prompt, "Message #1:\nREPLAY ME")
        self.store.begin_delivery_locked("task", replay_files, "gen-b")
        self.store.finish_delivery_locked("task", replay_files, "gen-b", delivered=True)
        replayed = self.receipt(record["id"])
        self.assertEqual(replayed["state"], mr.DELIVERED)
        self.assertEqual(replayed["attempts"], 2)
        self.assertEqual(replayed["delivered_generation"], "gen-b")

    def test_finish_without_an_attempt_is_an_audited_no_op(self):
        record = self.enqueue("NO ATTEMPT")
        self.store.finish_delivery_locked("task", self.store.inbox("task"), "gen-1", delivered=True)
        ignored = self.receipt(record["id"])
        self.assertEqual(ignored["state"], mr.ACCEPTED)
        self.assertIn("no attempt in progress", ignored["attempt_outcome"])
        self.assertEqual(mr.finish_attempt(self.store, "task", [999], "gen-1", delivered=True), [])


class HostileInputTests(ReceiptStore):
    def test_traversal_and_junk_references_are_rejected_without_touching_the_disk(self):
        hostile = ["../../etc/passwd", "..\\..\\secret", "/etc/passwd", "task.inbox/000000000001.msg",
                   "", "   ", "\udcff", "\ud800\ud800", "1/2", "0x1", "-1", "1 2", None, True, 1.5, [1]]
        for reference in hostile:
            with self.subTest(reference=reference), self.assertRaises(ValueError):
                self.receipt(reference)
        self.assertIsNone(self.receipt(0))
        self.assertIsNone(self.receipt("f" * 32))
        for bad_id in ("../../evil", "..", "x" * 31, "9" * 33, "\udcff"):
            with self.subTest(bad_id=bad_id), self.assertRaises(ValueError):
                self.enqueue("PAYLOAD", bad_id)
        self.assertEqual(self.store.inbox("task"), [])
        self.assertEqual(self.store.receipts("task"), [])
        self.assertEqual(sorted(path.name for path in self.folder.iterdir()), ["logs", "work"])

    def test_dashed_uuid_lookup_normalizes_but_traversal_never_reaches_a_path(self):
        message_id = mr.new_id()
        record = self.enqueue("DASHED", message_id)
        dashed = "%s-%s-%s-%s-%s" % (message_id[:8], message_id[8:12], message_id[12:16],
                                    message_id[16:20], message_id[20:])
        self.assertEqual(self.receipt(dashed)["id"], message_id)
        self.assertEqual(self.receipt(int(record["sequence"]))["id"], message_id)
        self.assertEqual([path.name for path in self.store.inbox("task")], ["000000000001.msg"])

    def test_invalid_task_name_is_rejected_before_any_receipt_is_written(self):
        for bad in ("../escape", "with/slash", "with space", "x" * 129, ".hidden"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.store.receipts(bad)
        self.assertEqual(sorted(path.name for path in self.folder.iterdir()), ["logs", "work"])

    def test_non_unicode_payload_leaves_no_false_acceptance(self):
        with self.assertRaises((UnicodeError, ValueError)):
            self.enqueue("LONE \ud800 SURROGATE")
        self.assertEqual(self.store.inbox("task"), [])
        records = self.store.receipts("task")
        self.assertEqual([row["state"] for row in records], [mr.ROLLED_BACK])
        self.assertNotIn(mr.ACCEPTED, [row["state"] for row in records])
        with self.assertRaises(ValueError):
            self.receipt("\ud800")
        self.assertFalse(list(self.store.path("task", ".inbox").glob("*.tmp.*")))

    def test_journal_is_ascii_safe_for_unicode_payloads(self):
        record = self.enqueue("ПРИВЕТ мир \U0001f600")
        raw = self.store.path("task", mr.SUFFIX).read_bytes()
        raw.decode("ascii")  # no UTF-8 escape hazard for readers
        self.assertEqual(record["state"], mr.ACCEPTED)
        self.assertEqual(self.receipt(record["id"])["digest"], mr.digest("ПРИВЕТ мир \U0001f600"))
        self.assertEqual(self.store.path("task", ".inbox/000000000001.msg").read_text(encoding="utf-8"),
                         "ПРИВЕТ мир \U0001f600")
        self.assertNotIn("ПРИВЕТ", raw.decode("ascii"))

    def test_unknown_state_or_schema_is_reported_not_silently_erased(self):
        self.enqueue("SCHEMA")
        journal = self.store.path("task", mr.SUFFIX)
        journal.write_text('{"id": "%s", "schema": 99, "sequence": 1, "state": "accepted"}\n' % (mr.new_id(),),
                           encoding="ascii")
        with self.assertRaises(mr.ReceiptError) as caught:
            self.store.receipts("task")
        self.assertIn("schema", str(caught.exception))
        journal.write_text('{"id": "%s", "schema": 1, "sequence": 1, "state": "applied"}\n' % (mr.new_id(),),
                           encoding="ascii")
        with self.assertRaises(mr.ReceiptError) as caught:
            self.store.receipts("task")
        self.assertIn("unknown state", str(caught.exception))
        self.assertIn('"state": "applied"', journal.read_text(encoding="ascii"))  # evidence intact


class FixtureProvider(BaseProvider):
    engine = "fixture"
    supports_resume = True

    def command(self, model, effort, cwd, session="", chat_only=False):
        return [sys.executable, str(FIXTURE), session or "ses_receipts"], {}


class LifecycleIntegrationTests(ReceiptStore):
    def test_interrupted_message_is_retained_and_requires_explicit_retry(self):
        record = self.enqueue("UNKNOWN EXTERNAL EFFECT")
        paths, prompt = self.store.batch_locked("task")
        self.store.begin_delivery("task", paths, "gen-1")
        self.store.path("task", ".control").write_text(json.dumps(dict(
            action="interrupt", generation="gen-1", by="test", reason="unknown completion")))
        self.execute(prompt, paths, "gen-1")
        final = self.receipt(record["id"])
        self.assertEqual(final["state"], mr.ACCEPTED)
        self.assertEqual(final["delivery_status"], mr.UNKNOWN)
        self.assertEqual(final["attempts"], 1)
        self.assertEqual(paths[0].read_text(), "UNKNOWN EXTERNAL EFFECT")
        self.assertFalse((self.work / "turn.2.started").exists())
        with Lock(self.store.path("task", ".inbox")):
            self.assertEqual(self.store.batch_locked("task", include_unknown=False), ([], ""))
            self.assertEqual(self.store.batch_locked("task")[0], paths)

    def test_real_child_crash_after_completed_receipt_recovers_without_redelivery(self):
        record = self.enqueue("KNOWN COMPLETION")
        child = r'''
import os, sys
sys.path.insert(0, os.environ["RECEIPT_ROOT"])
sys.path.insert(0, os.environ["RECEIPT_ROOT"] + "/tests")
from neoxider_agents import lifecycle, message_receipts as mr
from neoxider_agents.state import Lock, Store
from test_message_receipts import FixtureProvider
store = Store(os.environ["RECEIPT_LOGS"])
with Lock(store.path("task", ".inbox")):
    paths, prompt = store.batch_locked("task")
    store.begin_delivery_locked("task", paths, "gen-1")
mr.cleanup_completed_locked = lambda *a, **k: os._exit(9)
lifecycle.execute(store, "task", {"dir": sys.argv[1], "_generation": "gen-1", "--no-progress": True},
                  FixtureProvider(), "fixture", "", "ses_receipts", prompt, True, paths)
'''
        env = dict(os.environ, RECEIPT_ROOT=str(ROOT), RECEIPT_LOGS=str(self.store.root),
                   PYTHONDONTWRITEBYTECODE="1", AGENT_TIMEOUT_SEC="10", AGENT_SILENCE_SEC="0")
        result = subprocess.run([sys.executable, "-B", "-c", child, str(self.work)], cwd=ROOT, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=20)
        self.assertEqual(result.returncode, 9, result.stdout.decode("utf-8", "replace"))
        self.assertEqual(self.receipt(record["id"])["state"], mr.DELIVERED)
        self.assertEqual(len(self.store.inbox("task")), 1)
        with Lock(self.store.path("task", ".inbox")):
            self.assertEqual(self.store.batch_locked("task"), ([], ""))
        self.assertEqual(self.store.inbox("task"), [])
        self.assertFalse((self.work / "turn.2.started").exists())

    def test_completed_receipt_failure_keeps_the_payload(self):
        record = self.enqueue("BOUNDARY FAILURE")
        paths, prompt = self.store.batch_locked("task")
        self.store.begin_delivery("task", paths, "gen-1")
        real_append = mr.append_locked
        def fail_completion(store, name, row):
            if row["delivery_status"] == mr.SENT:
                raise OSError("completion fsync failed")
            return real_append(store, name, row)
        with mock.patch.object(mr, "append_locked", side_effect=fail_completion):
            with self.assertRaises(OSError):
                self.execute(prompt, paths, "gen-1")
        self.assertEqual(paths[0].read_text(), "BOUNDARY FAILURE")
        self.assertEqual(self.receipt(record["id"])["delivery_status"], mr.ATTEMPTING)

    def test_failed_message_invocation_does_not_implicitly_retry_external_effects(self):
        record = self.enqueue("EXTERNAL EFFECT UNKNOWN")
        (self.work / "fail-turn-1").touch()
        paths, prompt = self.store.batch_locked("task")
        self.store.begin_delivery("task", paths, "gen-1")
        with mock.patch.object(FixtureProvider, "retry_reason", return_value="transient"), \
                mock.patch.object(FixtureProvider, "retry_limit", 2), \
                mock.patch.dict(os.environ, AGENT_RETRIES="2", AGENT_RETRY_DELAY="0"):
            self.assertEqual(self.execute(prompt, paths, "gen-1"), 42)
        self.assertEqual(self.receipt(record["id"])["delivery_status"], mr.UNKNOWN)
        self.assertFalse((self.work / "turn.2.started").exists())

    def test_preflight_and_legacy_interrupt_failures_preserve_metadata_and_inbox(self):
        from neoxider_agents.process import pid_stamp
        self.enqueue("ALREADY QUEUED")
        for running in (False, True):
            self.store.update("task", state="running" if running else "done", exit="42", session="ses_saved",
                              pid=os.getpid(), pid_start=pid_stamp(os.getpid()), core_version="2")
            provider = FixtureProvider()
            with mock.patch.object(provider, "resolve", side_effect=ValueError("invalid model")), \
                    mock.patch("neoxider_agents.providers.get_provider", return_value=provider):
                before = self.store.path("task", ".meta").read_bytes()
                queue = [(path.name, path.read_bytes()) for path in self.store.inbox("task")]
                with self.assertRaises(ValueError):
                    lifecycle.send(self.store, "task", "REJECTED", {})
                self.assertEqual(self.store.path("task", ".meta").read_bytes(), before)
                self.assertEqual([(path.name, path.read_bytes()) for path in self.store.inbox("task")], queue)
        self.store.update("task", core_version="")
        with mock.patch("neoxider_agents.providers.get_provider", return_value=FixtureProvider()):
            before = self.store.path("task", ".meta").read_bytes()
            with self.assertRaises(ValueError):
                lifecycle.send(self.store, "task", "LEGACY --NOW", {"--now": True})
            self.assertEqual(self.store.path("task", ".meta").read_bytes(), before)
            self.assertEqual(len(self.store.inbox("task")), 1)

    def execute(self, prompt, files, generation, resume=True, session="ses_receipts"):
        with mock.patch.dict(os.environ, {"AGENT_TIMEOUT_SEC": "60", "AGENT_SILENCE_SEC": "0",
                                          "AGENT_INBOX_MAX_TURNS": "4"}, clear=False), \
                mock.patch.dict(os.environ, {"AGENT_DETACHED": "", "AGENT_LAUNCHER_PID": ""}), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return lifecycle.execute(self.store, "task", {"dir": str(self.work), "--no-progress": True,
                                                          "_generation": generation},
                                     FixtureProvider(), "fixture-model", "", session, prompt, resume, files)

    def test_real_turn_records_delivered_without_inventing_acknowledgment(self):
        first = self.enqueue("FIRST FOLLOWUP")
        second = self.enqueue("SECOND FOLLOWUP")
        files, prompt = self.store.batch_locked("task")
        self.store.begin_delivery_locked("task", files, "gen-1")
        code = self.execute(prompt, files, "gen-1")
        self.assertEqual(code, 0)
        sent = (self.work / "turn.1.prompt").read_text(encoding="utf-8")
        self.assertIn("Message #1:\nFIRST FOLLOWUP", sent)
        self.assertIn("Message #2:\nSECOND FOLLOWUP", sent)
        self.assertEqual(self.store.inbox("task"), [])
        for record in (first, second):
            settled = self.receipt(record["id"])
            self.assertEqual(settled["state"], mr.DELIVERED)
            self.assertEqual(settled["delivered_generation"], "gen-1")
            self.assertEqual(settled["attempts"], 1)
            self.assertEqual(settled["acknowledged_epoch"], 0.0)
            self.assertEqual(settled["applied"], 0)
        self.assertEqual(self.store.read("task")["state"], "done")

    def test_failed_real_turn_keeps_the_queue_and_reports_unknown(self):
        record = self.enqueue("MUST SURVIVE")
        (self.work / "fail-turn-1").touch()  # the fixture provider fails this turn
        files, prompt = self.store.batch_locked("task")
        self.store.begin_delivery_locked("task", files, "gen-1")
        code = self.execute(prompt, files, "gen-1")
        self.assertEqual(code, 42)
        failed = self.receipt(record["id"])
        self.assertEqual(failed["state"], mr.ACCEPTED)
        self.assertEqual(failed["delivery_status"], mr.UNKNOWN)
        self.assertEqual(failed["attempts"], 1)
        self.assertEqual([path.name for path in self.store.inbox("task")], ["000000000001.msg"])
        self.assertEqual(self.store.path("task", ".inbox/000000000001.msg").read_text(encoding="utf-8"),
                         "MUST SURVIVE")

    def test_replay_after_a_failed_turn_delivers_through_the_real_provider(self):
        record = self.enqueue("SECOND CHANCE")
        (self.work / "fail-turn-1").touch()
        files, prompt = self.store.batch_locked("task")
        self.store.begin_delivery_locked("task", files, "gen-1")
        self.assertEqual(self.execute(prompt, files, "gen-1"), 42)
        (self.work / "fail-turn-1").unlink()
        self.store.update("task", generation="gen-2")
        files, prompt = self.store.batch_locked("task")
        self.store.begin_delivery_locked("task", files, "gen-2")
        self.assertEqual(self.execute(prompt, files, "gen-2"), 0)
        delivered = self.receipt(record["id"])
        self.assertEqual(delivered["state"], mr.DELIVERED)
        self.assertEqual(delivered["attempts"], 2)
        self.assertEqual(delivered["delivered_generation"], "gen-2")
        self.assertEqual(self.store.inbox("task"), [])
        self.assertIn("SECOND CHANCE", (self.work / "turn.2.prompt").read_text(encoding="utf-8"))

    def test_stopped_turn_reports_unknown_and_redelivers(self):
        record = self.enqueue("INTERRUPT ME")
        files, prompt = self.store.batch_locked("task")
        self.store.begin_delivery_locked("task", files, "gen-1")
        self.store.path("task", ".control").write_text(json.dumps(dict(action="stop", generation="gen-1", by="test", reason="stop fixture")))
        code = self.execute(prompt, files, "gen-1")
        self.assertEqual(code, 130)
        stopped = self.receipt(record["id"])
        self.assertEqual(stopped["state"], mr.ACCEPTED)
        self.assertEqual(stopped["delivery_status"], mr.UNKNOWN)
        self.assertIn("stopped before delivery", stopped["attempt_outcome"])
        self.assertEqual([path.name for path in self.store.inbox("task")], ["000000000001.msg"])


if __name__ == "__main__":
    unittest.main()
