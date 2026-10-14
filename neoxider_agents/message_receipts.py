"""Durable message receipts: opaque IDs, an honest acceptance transaction, explicit acks.

States mean exactly this and nothing more:

``intent``
    An acceptance transaction opened for a caller-supplied UUID. Not accepted yet.
``accepted``
    The message text is persisted in the legacy inbox **and** the acceptance record is
    persisted. Both, or neither: an intent is never reported as accepted.
``delivered``
    The text was included in a provider invocation that completed. This says nothing
    about the model reading, understanding, obeying or applying it.
``acknowledged``
    Written only from explicit caller-supplied assistant acknowledgment evidence.
``rolled_back``
    An intent whose queue file never became durable. The UUID stays bound to that
    payload digest, so it can never be reused for different content.

``applied`` is never inferred: nothing here reads provider output, answer text or tool
logs. ``applied`` can only be set on top of an already acknowledged receipt.

Legacy compatibility: the inbox layout, the ``%012d.msg`` names, the ``sequence`` file
and ``Store.enqueue_locked``'s integer return value are unchanged. Receipts are additive
sidecars in ``<task>.receipts.jsonl`` — outside the ``.inbox`` glob — so a legacy Bash
owner that only writes ``.msg`` files keeps working; its messages are reconciled into
accepted receipts, never into a false ack. Sequence numbers stay monotonic even when the
inbox is drained or removed, because receipts and ``<task>.sequence`` outlive it.

Durability rules:
* every snapshot is one fsynced ``O_APPEND`` line of complete, ASCII-safe JSON;
* a snapshot counts only if its line ends in a newline and validates — a torn final line
  is a crash artifact that readers ignore and the next write repairs by rewriting only
  validated lines atomically, keeping the pre-repair bytes as ``<task>.receipts.jsonl.bak``;
* corrupt *complete* lines are never silently erased: readers raise ``ReceiptError`` and
  an explicit repair refuses to drop them;
* lookups are exact over the whole journal, so an old UUID stays idempotent no matter how
  much history follows it.

Locking: ``state.Lock`` is an inter-process publication lock, never re-entrant. Every
mutation therefore exists twice: ``*_locked`` assumes the caller already holds the task
``.inbox`` lock (the lifecycle send/drain path, which does), and the plain name takes
that lock itself for standalone callers. Nothing here acquires the lock twice.
"""
import hashlib
import json
import math
import os
import re
import time
import uuid
from pathlib import Path

from .state import Lock, atomic_write, read_meta, sync_directory

SUFFIX = ".receipts.jsonl"
BACKUP = ".bak"
COUNTER = ".sequence"
SCHEMA = 1

INTENT = "intent"
ACCEPTED = "accepted"
DELIVERED = "delivered"
ACKNOWLEDGED = "acknowledged"
ROLLED_BACK = "rolled_back"
STATES = (INTENT, ACCEPTED, DELIVERED, ACKNOWLEDGED, ROLLED_BACK)
# Only ACCEPTED and above are accepted work; ROLLED_BACK/INTENT are pre-acceptance.
RANK = {INTENT: 0, ROLLED_BACK: 1, ACCEPTED: 2, DELIVERED: 3, ACKNOWLEDGED: 4}

PENDING = "pending"
ATTEMPTING = "attempting"
UNKNOWN = "unknown"
SENT = DELIVERED
COMMITTED = (ACCEPTED, DELIVERED, ACKNOWLEDGED)

# An opaque id is never a sequence, never derived from text and never a path component.
ID = re.compile(r"^[0-9a-f]{32}$")
DASHED = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
SEQUENCE = re.compile(r"^[0-9]{1,18}$")
SOURCE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


class ReceiptError(ValueError):
    """Durable receipt state is corrupt: fail honestly instead of erasing evidence."""


def new_id():
    """Opaque durable id, deliberately unrelated to the legacy int sequence."""
    return uuid.uuid4().hex


def valid_id(value):
    text = value.strip().lower() if isinstance(value, str) else str(value).strip().lower()
    if DASHED.fullmatch(text):
        text = text.replace("-", "")
    if not ID.fullmatch(text):
        raise ValueError("invalid message id %s: expected an opaque receipt id" % ascii(text[:64]))
    return text


def digest(text):
    return "sha256:" + hashlib.sha256(str(text).encode("utf-8", "surrogatepass")).hexdigest()


def compact(value, limit=240):
    text = " ".join(str(value).split())
    try:
        from activity import compact as activity_compact
        return activity_compact(text, limit)
    except Exception:  # pragma: no cover - redaction is best effort, never fatal
        return text[:limit]


def path_for(store, name):
    """`<task>.receipts.jsonl` in the state root: validated name, outside the inbox glob."""
    return store.path(name, SUFFIX)


def counter_path(store, name):
    """Durable high-water mark; survives draining or removing the inbox itself."""
    return store.path(name, COUNTER)


def inbox_file(store, name, sequence):
    """Legacy `%012d.msg` path from an int sequence: no id ever reaches the filesystem."""
    return store.path(name, ".inbox") / ("%012d.msg" % int(sequence))


def sequences(paths):
    """Legacy sequence numbers carried by `.msg` paths; foreign stems stay untracked."""
    result = []
    for path in paths or ():
        try:
            result.append(int(Path(path).stem))
        except (TypeError, ValueError):
            continue
    return result


def _decode(text, name):
    try:
        record = json.loads(text)
    except ValueError:
        return None
    if not isinstance(record, dict):
        return None
    if type(record.get("schema")) is not int or record.get("schema") != SCHEMA:
        raise ReceiptError("message receipt journal %s has schema %r (expected %s)"
                           % (name, record.get("schema"), SCHEMA))
    identifier = record.get("id")
    if not isinstance(identifier, str) or not ID.fullmatch(identifier):
        raise ReceiptError("message receipt journal %s has an invalid id" % name)
    if record.get("state") not in STATES:
        raise ReceiptError("message receipt journal %s has unknown state %r" % (name, record.get("state")))
    sequence = record.get("sequence")
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
        raise ReceiptError("message receipt journal %s has an invalid sequence %r" % (name, sequence))
    if record.get("task") != name:
        raise ReceiptError("message receipt journal %s holds a receipt for task %r" % (name, record.get("task")))
    if sequence < 1 or sequence >= 10 ** 18:
        raise ReceiptError("invalid sequence range")
    if not isinstance(record.get("digest"), str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", record["digest"]):
        raise ReceiptError("invalid receipt payload digest")
    for key in ("rev", "bytes", "attempts", "attempt_turn", "applied"):
        value = record.get(key)
        if type(value) is not int or value < (1 if key == "rev" else 0):
            raise ReceiptError("invalid receipt field: " + key)
    if record["applied"] not in (0, 1) or (record["applied"] and record["state"] != ACKNOWLEDGED):
        raise ReceiptError("invalid applied rank")
    if record.get("delivery_status") not in (PENDING, ATTEMPTING, UNKNOWN, SENT):
        raise ReceiptError("invalid delivery status")
    for key in ("intent_epoch", "queued_epoch", "attempt_epoch", "delivered_epoch", "acknowledged_epoch", "applied_epoch", "updated_epoch"):
        value = record.get(key)
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ReceiptError("invalid receipt timestamp: " + key)
    if record["state"] == ACKNOWLEDGED and (not record.get("ack_source") or not record["acknowledged_epoch"]
                                           or not (record.get("ack_digest") or record.get("ack_note"))):
        raise ReceiptError("acknowledgment lacks explicit evidence")
    if record["delivery_status"] == SENT and not record["delivered_epoch"]:
        raise ReceiptError("delivery lacks completion boundary")
    if record["delivery_status"] == SENT and RANK[record["state"]] < RANK[DELIVERED]:
        raise ReceiptError("invalid delivery rank")
    if record["state"] == DELIVERED and not record["delivered_epoch"]:
        raise ReceiptError("delivered state lacks completion boundary")
    for key in ("queued_generation", "attempt_generation", "delivered_generation", "ack_source",
                "ack_digest", "ack_note", "attempt_outcome", "applied_note", "origin"):
        if not isinstance(record.get(key), str):
            raise ReceiptError("invalid receipt field: " + key)
    if record["state"] == ACKNOWLEDGED:
        source = record["ack_source"]
        if not SOURCE.fullmatch(source) or not (source == "assistant" or source.startswith("assistant:")):
            raise ReceiptError("invalid assistant acknowledgment source")
        if record["ack_digest"] and not re.fullmatch(r"sha256:[0-9a-f]{64}", record["ack_digest"]):
            raise ReceiptError("invalid acknowledgment digest")
    if record["delivery_status"] == ATTEMPTING and not record["attempt_generation"]:
        raise ReceiptError("attempt lacks generation")
    return record


MAX_LINE_BYTES = 65536


def _lines(path):
    """Stream bounded physical lines; an incomplete final line is never committed."""
    try:
        stream = path.open("rb")
    except FileNotFoundError:
        return
    with stream:
        while True:
            data = stream.readline(MAX_LINE_BYTES + 1)
            if not data:
                return
            if len(data) > MAX_LINE_BYTES:
                raise ReceiptError("oversized message receipt snapshot")
            complete = data.endswith(b"\n")
            if not complete:
                yield "", False
                return
            try:
                yield data[:-1].decode("utf-8"), True
            except UnicodeError as error:
                raise ReceiptError("invalid receipt encoding") from error


def _records(store, name):
    for text, complete in _lines(path_for(store, name)):
        if not complete:
            continue
        record = _decode(text, name)
        if record is None:
            raise ReceiptError("message receipt journal %s has a corrupt snapshot: %s"
                               % (name, ascii(text[:120])))
        yield record


def _transition(previous, record):
    if previous is None:
        return
    if (previous["sequence"] != record["sequence"] or previous["digest"] != record["digest"]
            or record["rev"] <= previous["rev"]
            or (RANK[record["state"]] < RANK[previous["state"]]
                and not (previous["state"] == ROLLED_BACK and record["state"] == INTENT))
            or record["attempts"] < previous["attempts"]
            or record["applied"] < previous["applied"]):
        raise ReceiptError("message receipt binding/rank/revision regressed: " + record["id"])


def journal(store, name):
    """Compatibility materialization; operational lookups stream the full journal."""
    return list(_records(store, name)), int(_torn(path_for(store, name)))


def rows(store, name):
    latest = {}
    slots = {}
    for record in _records(store, name):
        owner = slots.setdefault(record["sequence"], record["id"])
        if owner != record["id"]:
            raise ReceiptError("message sequence has multiple UUID bindings")
        _transition(latest.get(record["id"]), record)
        latest[record["id"]] = record
    return list(latest.values())


def torn_tail(store, name):
    # Validate complete snapshots even when the tail itself is torn.
    for _ in _records(store, name):
        pass
    return _torn(path_for(store, name))


def _torn(path):
    try:
        with path.open("rb") as stream:
            size = stream.seek(0, 2)
            if not size:
                return False
            stream.seek(size - 1)
            return stream.read(1) != b"\n"
    except (OSError, ValueError):
        return False


def repair(store, name):
    with Lock(store.path(name, ".inbox")):
        return repair_locked(store, name)


def repair_locked(store, name):
    """Validate before mutation and retain exact pre-repair bytes under the lock."""
    path = path_for(store, name)
    count = sum(1 for _ in _records(store, name))
    rows(store, name)  # validate historical transitions before modifying evidence
    if not _torn(path):
        return count
    backup = path.with_name(path.name + BACKUP)
    if backup.exists():
        backup = path.with_name(path.name + BACKUP + "." + uuid.uuid4().hex)
    temporary = path.with_name(path.name + ".repair." + uuid.uuid4().hex)
    try:
        with path.open("rb") as source, backup.open("xb") as kept:
            while True:
                chunk = source.read(65536)
                if not chunk:
                    break
                kept.write(chunk)
            kept.flush()
            os.fsync(kept.fileno())
        with temporary.open("wb") as out:
            for text, complete in _lines(path):
                if complete:
                    out.write((text + "\n").encode("utf-8"))
            out.flush()
            os.fsync(out.fileno())
        os.replace(str(temporary), str(path))
        _sync_directory(path.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return count


def _sync_directory(path):
    sync_directory(path)


def _write_line(path, line):
    data = line.encode("ascii")
    if len(data) > MAX_LINE_BYTES:
        raise ReceiptError("oversized message receipt snapshot")
    path.parent.mkdir(parents=True, exist_ok=True)
    created = not path.exists()
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    boundary = os.lseek(fd, 0, os.SEEK_END)
    try:
        try:
            while data:
                written = os.write(fd, data)
                if not written:
                    raise OSError("receipt append made no progress")
                data = data[written:]
            os.fsync(fd)
        except OSError:
            # The publication lock excludes another writer. A failed append must
            # not leave a complete snapshot that claims a durable boundary.
            os.ftruncate(fd, boundary)
            try:
                os.fsync(fd)
            except OSError:
                pass  # Storage is failing; report the original failure to caller.
            raise
    finally:
        os.close(fd)
    if created:
        _sync_directory(path.parent)


def append_locked(store, name, record):
    """Publish one snapshot. Repairs a torn tail first so appends never concatenate."""
    row = dict(record)
    row["schema"] = SCHEMA
    row["task"] = name
    previous = receipt_locked(store, name, row["id"])
    row["rev"] = (previous["rev"] if previous else 0) + 1
    row["updated_epoch"] = time.time()
    path = path_for(store, name)
    if _torn(path):
        repair_locked(store, name)
    _decode(json.dumps(row), name)
    _transition(previous, row)
    _write_line(path, json.dumps(row, ensure_ascii=True, sort_keys=True) + "\n")
    return row


def _rank_key(record):
    return (float(record.get("updated_epoch") or 0), int(record.get("rev") or 0))


def normalize(ref):
    """Accept an opaque id or a sequence; reject traversal, junk and surrogates."""
    if ref is None or isinstance(ref, bool):
        raise ValueError("invalid message reference: expected a receipt id or a message sequence")
    if isinstance(ref, int):
        if ref < 0:
            raise ValueError("invalid message sequence %d" % ref)
        return "sequence", ref
    text = ref.strip() if isinstance(ref, str) else str(ref).strip()
    if SEQUENCE.fullmatch(text):
        return "sequence", int(text)
    try:
        return "id", valid_id(text)
    except ValueError:
        raise ValueError("invalid message reference %s: use a receipt id or a message sequence" % ascii(text[:64]))


def receipt_locked(store, name, ref):
    """Exact full-history lookup; a conflicting sequence binding fails closed."""
    kind, value = normalize(ref)
    best = None
    for record in _records(store, name):
        if (record.get("sequence") if kind == "sequence" else record.get("id")) != value:
            continue
        _transition(best, record)
        best = record
    return best


def receipt(store, name, ref):
    with Lock(store.path(name, ".inbox")):
        record = receipt_locked(store, name, ref)
        if record and record["state"] in COMMITTED:
            _check_payload(store, name, record)
        return record


def listing(store, name):
    with Lock(store.path(name, ".inbox")):
        records = rows(store, name)
        for record in records:
            if record["state"] in COMMITTED:
                _check_payload(store, name, record)
        return sorted(records, key=lambda item: (item["sequence"], _rank_key(item)))


def _blank(identifier, sequence, payload, size, generation, origin, state):
    stamp = time.time()
    return dict(id=identifier, sequence=int(sequence), state=state, digest=payload, bytes=int(size),
                origin=origin, intent_epoch=stamp, queued_epoch=0.0, queued_generation=generation or "",
                attempts=0, delivery_status=PENDING, attempt_generation="", attempt_turn=0,
                attempt_epoch=0.0, attempt_outcome="", delivered_epoch=0.0, delivered_generation="",
                acknowledged_epoch=0.0, ack_source="", ack_digest="", ack_note="",
                applied=0, applied_epoch=0.0, applied_note="")


def snapshot_locked(store, name, sequence, message_id=None, payload="", size=0, generation="",
             origin="enqueue", state=ACCEPTED, **extra):
    """Publish one validated snapshot for an existing queue slot. Writes no queue file.

    `enqueue` uses it for the transaction; replay/repair tooling and tests use it to
    record history for messages whose legacy `.msg` file is already gone.
    """
    record = _blank(valid_id(message_id) if message_id else new_id(), sequence, payload or digest(payload),
                    size, generation, origin, state)
    record.update(extra)
    if state == ACCEPTED:
        _check_payload(store, name, record)
    return append_locked(store, name, record)


def snapshot(store, name, sequence, **kwargs):
    with Lock(store.path(name, ".inbox")):
        return snapshot_locked(store, name, sequence, **kwargs)


def allocate(store, name):
    """Validated high-water marks, including every INTENT, survive counter reset."""
    high = 0
    for path in (store.path(name, ".inbox") / "sequence", counter_path(store, name)):
        try:
            value = path.read_text(encoding="ascii").strip()
        except FileNotFoundError:
            continue
        if not SEQUENCE.fullmatch(value):
            raise ReceiptError("corrupt message sequence counter: " + path.name)
        high = max(high, int(value))
    high = max([high] + sequences(store.inbox(name)))
    for record in rows(store, name):
        high = max(high, record["sequence"])
    if high >= 10 ** 18 - 1:
        raise ReceiptError("message sequence counter exhausted")
    return high + 1


def persist_sequence(store, name, sequence):
    """Legacy `.inbox/sequence` artifact plus a durable counter that outlives the inbox."""
    high = max(int(sequence), allocate(store, name) - 1)
    atomic_write(store.path(name, ".inbox") / "sequence", str(high) + "\n")
    atomic_write(counter_path(store, name), str(high) + "\n")


def enqueue(store, name, text, message_id=None, generation="", origin="enqueue"):
    """Accept one message, taking the task `.inbox` lock."""
    with Lock(store.path(name, ".inbox")):
        return enqueue_locked(store, name, text, message_id=message_id, generation=generation, origin=origin)


def enqueue_locked(store, name, text, message_id=None, generation="", origin="enqueue"):
    """Accept one message: intent -> queue -> accepted. Caller holds the `.inbox` lock."""
    identifier = valid_id(message_id) if message_id else new_id()
    payload, raw = digest(text), str(text).encode("utf-8", "surrogatepass")
    # A caller-supplied id must be checked for idempotency; a fresh uuid4 cannot exist yet.
    existing = receipt_locked(store, name, identifier) if message_id else None
    if existing is not None:
        if existing["digest"] != payload:
            raise ValueError("message id %s is already bound to different content" % identifier)
        if existing["state"] in COMMITTED:
            _check_payload(store, name, existing)
            return existing
        if existing["state"] == INTENT:
            if _check_payload(store, name, existing, required=False):
                _sync_payload(inbox_file(store, name, existing["sequence"]))
                persist_sequence(store, name, existing["sequence"])
                return append_locked(store, name, dict(existing, state=ACCEPTED, queued_epoch=time.time()))
        # Resume the original reservation, never create a second binding for this UUID.
        intent = existing
        sequence = existing["sequence"]
    else:
        sequence = allocate(store, name)
        intent = append_locked(store, name, _blank(identifier, sequence, payload, len(raw), generation, origin, INTENT))
    try:
        box = store.path(name, ".inbox")
        box.mkdir(mode=0o700, exist_ok=True)
        sync_directory(box.parent)
        atomic_write(inbox_file(store, name, sequence), text)
    except (OSError, UnicodeError) as error:
        append_locked(store, name, dict(intent, state=ROLLED_BACK,
                                 attempt_outcome="queue write failed: " + compact(str(error))))
        raise
    persist_sequence(store, name, sequence)
    return append_locked(store, name, dict(intent, state=ACCEPTED, queued_epoch=time.time()))


def ensure_accepted_locked(store, name, sequence, origin="reconcile"):
    """Guarantee a real accepted receipt for a queued message, or None when unbacked.

    A missing payload is never turned into an accepted empty message.
    """
    record = receipt_locked(store, name, int(sequence))
    if record is not None and record["state"] in COMMITTED:
        _check_payload(store, name, record)
        return record
    try:
        raw = inbox_file(store, name, sequence).read_bytes()
    except FileNotFoundError:
        raw = None
    if raw is None:
        # No durable queue file: only an unfinished intent may be closed here.
        if record is not None and record["state"] == INTENT:
            append_locked(store, name, dict(record, state=ROLLED_BACK,
                                     attempt_outcome="no durable queue file for this intent"))
        return None
    text = raw.decode("utf-8")
    if record:
        _check_payload(store, name, record)
    _sync_payload(inbox_file(store, name, sequence))
    if record is not None:
        persist_sequence(store, name, sequence)
        return append_locked(store, name, dict(record, state=ACCEPTED, queued_epoch=time.time()))
    identifier = new_id()
    blank = _blank(identifier, int(sequence), digest(text), len(raw),
                   record.get("queued_generation", "") if record else "", origin, ACCEPTED)
    return append_locked(store, name, dict(blank, queued_epoch=time.time()))


def ensure_accepted(store, name, sequence, origin="reconcile"):
    with Lock(store.path(name, ".inbox")):
        return ensure_accepted_locked(store, name, sequence, origin)


def _sync_payload(path):
    # Adoption/recovery must establish durability even for a legacy writer that
    # merely closed its file, or a recovered queue whose flush outcome is unknown.
    with path.open("r+b" if os.name == "nt" else "rb") as stream:
        os.fsync(stream.fileno())
    sync_directory(path.parent)
    sync_directory(path.parent.parent)


def _check_payload(store, name, record, required=True):
    path = inbox_file(store, name, record["sequence"])
    try:
        size, checksum = 0, hashlib.sha256()
        with path.open("rb") as stream:
            while True:
                chunk = stream.read(65536)
                if not chunk:
                    break
                checksum.update(chunk)
                size += len(chunk)
    except FileNotFoundError:
        if required and not (record["delivery_status"] == SENT and record["delivered_epoch"]):
            raise ReceiptError("accepted message payload is missing: " + record["id"])
        return False
    if "sha256:" + checksum.hexdigest() != record["digest"] or size != record["bytes"]:
        raise ReceiptError("queued message differs from receipt: " + record["id"])
    return True


def cleanup_completed_locked(store, name):
    """Remove only matching payloads with a durable known-completion boundary."""
    for record in rows(store, name):
        if record["delivery_status"] == SENT and record["delivered_epoch"]:
            if _check_payload(store, name, record):
                inbox_file(store, name, record["sequence"]).unlink()
                sync_directory(store.path(name, ".inbox"))


def reconcile(store, name):
    """Finish interrupted acceptance and adopt a legacy owner's messages. Takes the lock."""
    with Lock(store.path(name, ".inbox")):
        return reconcile_locked(store, name)


def reconcile_locked(store, name):
    """As `reconcile`, for a caller that already holds the `.inbox` lock."""
    cleanup_completed_locked(store, name)
    created = []
    queued = sequences(store.inbox(name))
    for record in rows(store, name):
        if record["state"] in COMMITTED:
            _check_payload(store, name, record)
        if record["state"] != INTENT:
            continue
        adopted = ensure_accepted_locked(store, name, record["sequence"], origin="recovered")
        if adopted is not None:
            created.append(adopted)
    for sequence in queued:
        if receipt_locked(store, name, sequence) is None:
            adopted = ensure_accepted_locked(store, name, sequence)
            if adopted is not None:
                created.append(adopted)
    return created


def begin_attempt(store, name, batch, generation="", turn=0):
    """Record delivery intent for the next provider invocation. Takes the lock."""
    with Lock(store.path(name, ".inbox")):
        return begin_attempt_locked(store, name, batch, generation=generation, turn=turn)


def begin_attempt_locked(store, name, batch, generation="", turn=0):
    """As `begin_attempt`, for a caller that already holds the `.inbox` lock."""
    generation = generation or read_meta(store.path(name, ".meta")).get("generation", "")
    if not generation:
        raise ValueError("delivery attempt requires a task generation")
    records = []
    for sequence in sorted({int(value) for value in batch}):
        record = ensure_accepted_locked(store, name, sequence, origin="drain")
        if record is None:
            continue
        record = dict(record)
        record["attempts"] = int(record.get("attempts") or 0) + 1
        record["attempt_generation"] = generation or record.get("attempt_generation", "")
        record["attempt_turn"] = int(turn or 0)
        record["attempt_epoch"] = time.time()
        record["attempt_outcome"] = ""
        record["delivery_status"] = ATTEMPTING
        records.append(append_locked(store, name, record))
    return records


def finish_attempt(store, name, batch, generation="", delivered=False, outcome=""):
    """Close an attempt at a known boundary. Takes the lock."""
    with Lock(store.path(name, ".inbox")):
        return finish_attempt_locked(store, name, batch, generation=generation, delivered=delivered,
                                     outcome=outcome)


def finish_attempt_locked(store, name, batch, generation="", delivered=False, outcome=""):
    """As `finish_attempt`, for a caller that already holds the `.inbox` lock.

    Completion is the only known delivery boundary; anything else is unknown, never
    delivered and never acknowledged.
    """
    records = []
    for sequence in sorted({int(value) for value in batch}):
        record = receipt_locked(store, name, sequence)
        if record is None:
            continue  # never fabricate a receipt for a message this task never accepted
        record = dict(record)
        note = outcome or (SENT if delivered else UNKNOWN)
        attempted = record.get("attempt_generation") or ""
        if record["state"] not in COMMITTED:
            note = "ignored: message is %s; %s" % (record["state"], note)
        elif record.get("delivery_status") != ATTEMPTING:
            note = "ignored: no attempt in progress (%s); %s" % (record.get("delivery_status", ""), note)
        elif generation != attempted:
            note = "ignored: generation %s no longer owns attempt %s; %s" % (generation, attempted, note)
        else:
            if record["state"] == ACKNOWLEDGED:
                note = "already acknowledged; " + note
            record["delivery_status"] = SENT if delivered else UNKNOWN
            if delivered:
                record["delivered_epoch"] = time.time()
                record["delivered_generation"] = generation or attempted
                if RANK[DELIVERED] > RANK[record["state"]]:
                    record["state"] = DELIVERED
        record["attempt_outcome"] = note
        records.append(append_locked(store, name, record))
    return records


def _require(store, name, ref):
    record = receipt_locked(store, name, ref)
    if record is None:
        raise ValueError("no message receipt %s for task %s" % (ascii(str(ref)[:64]), name))
    return record


def acknowledge(store, name, ref, source="assistant", text="", note=""):
    """Explicit assistant acknowledgment evidence; nothing calls this implicitly."""
    with Lock(store.path(name, ".inbox")):
        return acknowledge_locked(store, name, ref, source=source, text=text, note=note)


def acknowledge_locked(store, name, ref, source="assistant", text="", note=""):
    """As `acknowledge`, for a caller that already holds the `.inbox` lock."""
    if not SOURCE.fullmatch(source or "") or not (source == "assistant" or source.startswith("assistant:")):
        raise ValueError("acknowledgment source must be a short tag such as 'assistant'")
    if not str(text).strip() and not str(note).strip():
        raise ValueError("acknowledged requires explicit acknowledgment evidence; nothing infers it")
    record = dict(_require(store, name, ref))
    if record["state"] not in COMMITTED:
        raise ValueError("cannot acknowledge a %s message; acceptance never completed" % record["state"])
    record["state"] = ACKNOWLEDGED
    record["acknowledged_epoch"] = time.time()
    record["ack_source"] = source
    record["ack_digest"] = digest(text) if str(text) else ""
    record["ack_note"] = compact(note) if note else ""
    return append_locked(store, name, record)


def mark_applied(store, name, ref, note=""):
    """Semantic application asserted by a caller; never derived from provider output."""
    with Lock(store.path(name, ".inbox")):
        return mark_applied_locked(store, name, ref, note=note)


def mark_applied_locked(store, name, ref, note=""):
    """As `mark_applied`, for a caller that already holds the `.inbox` lock."""
    record = dict(_require(store, name, ref))
    if record["state"] != ACKNOWLEDGED:
        raise ValueError("applied requires an explicit acknowledged receipt; id %s is %s"
                         % (record["id"], record["state"]))
    record["applied"] = 1
    record["applied_epoch"] = time.time()
    record["applied_note"] = compact(note) if note else ""
    return append_locked(store, name, record)


def pending(record):
    """True while the message is not explicitly acknowledged."""
    return record["state"] != ACKNOWLEDGED


def stale_attempt(record):
    """True when an owner died mid-invocation: the outcome is unknown, not delivered."""
    return record["state"] in COMMITTED and record.get("delivery_status") == ATTEMPTING


def summary(record):
    return "id=%s #%s %s attempts=%s delivery=%s delivered_gen=%s ack=%s applied=%s" % (
        record["id"], record["sequence"], record["state"], record.get("attempts", 0),
        record.get("delivery_status", PENDING), record.get("delivered_generation") or "-",
        record.get("acknowledged_epoch") or "-", record.get("applied", 0))
