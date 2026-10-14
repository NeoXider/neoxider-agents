"""Durable parent event backend: append-only records, paged cursors, honest at-least-once delivery.

Design (see ``docs/DURABLE_EVENTS.md``):

* One append-only log per task, ``<task>.events.jsonl``, plus one acknowledgment
  ledger per task, ``<task>.events.acks.jsonl``. Both are *additive sidecars* in the
  state root: they are outside the legacy ``.inbox``/``.log`` globs, so nothing else in
  this repository rewrites, replaces, compacts or prunes them.
* Appends use ``state.Lock``, ``O_APPEND`` and ``fsync``. Before another append,
  a torn trailing fragment is backed up and truncated under the same lock.
  Complete rows are never rewritten; interior corruption fails closed.
* ``last_record`` (used by ``head`` and by the next sequence number) streams and validates the
  whole log. It never answers from a tail shortcut, so interior damage can never be bypassed
  for a write or a head read.
* Delivery is **at least once and never silently truncated**. ``subscribe`` acknowledges
  a consumer only after the callback returned, so a callback failure replays the failed
  event. There is no exactly-once guarantee and none is claimed: a callback that acts and
  then raises will be called again for the same event, and two concurrent consumers each
  see the whole log.

Layout of one record (immutable once written):

``{"schema":1,"id":<32 hex>,"seq":<int>,"task":<name>,"generation":<token>,"parent":<id|"">,
   "epoch":<float>,"kind":<token>,"data":{<immutable JSON object>},"digest":"sha256:...",
   "key":<deterministic idempotency key>}``

``digest`` covers the content fields and ``key`` is derived from the same bytes, so a
mutated payload is detected on read instead of being served as fact. Sequence numbers are
per task and strictly increasing; they are never reused and never reset, not even after
every event has been acknowledged.
"""
import hashlib
import json
import math
import os
import re
import time
import uuid
from pathlib import Path
from threading import Event

from .state import Lock, native_path, valid_name

SCHEMA = 1
LOG_SUFFIX = ".events.jsonl"
ACK_SUFFIX = ".events.acks.jsonl"

DEFAULT_LIMIT = 100
MAX_LIMIT = 1000
MAX_RECORD_BYTES = 262144
MAX_LINE_BYTES = MAX_RECORD_BYTES + 4096
MAX_CURSOR = (1 << 63) - 1
MAX_CONSUMERS = 1024

ID = re.compile(r"^[0-9a-f]{32}$")
KIND = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
GENERATION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
CONSUMER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
PARENT = re.compile(r"^/?[A-Za-z0-9][A-Za-z0-9._:/@-]{0,127}$")

# High-signal credential shapes only. A deliberate, documented rejection is honest; a
# silent redaction would break the digest, the idempotency key and replay equality.
SECRET_VALUE = re.compile(
    r"^(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{20,}"
    r"|AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}|xox[abprs]-[A-Za-z0-9-]{10,}"
    r"|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{4,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----|Bearer\s+[A-Za-z0-9._~+/=-]{20,})$")
SECRET_KEY = re.compile(r"(?i)(?:^|_)(?:api[_-]?key|secret|password|passwd|access[_-]?key"
                        r"|private[_-]?key|credential|bearer[_-]?token)(?:$|_)")

# Disambiguating a torn tail from interior damage needs a probe of one byte.
_PROBE = 1


class EventError(RuntimeError):
    """Base class for every honest failure this module reports."""


class EventCorruption(EventError):
    """The log is damaged somewhere other than its final line; refuse to guess."""


class WaitTimeout(EventError):
    """No event arrived before the caller's timeout."""


# --------------------------------------------------------------------------------------
# validation


def _validate_task(task):
    if not isinstance(task, str):
        raise ValueError("invalid task name: expected a string")
    name = valid_name(task)
    if name.endswith(".") or name.split(".")[0].upper() in (
            "CON", "PRN", "AUX", "NUL", *["COM%d" % n for n in range(1, 10)],
            *["LPT%d" % n for n in range(1, 10)]):
        raise ValueError("invalid task name: reserved filename")
    return name


def valid_parent(value):
    """Parent labels are identifiers, including SDK IDs; they are not event UUIDs."""
    if not isinstance(value, str) or len(value) > 128 or (value and not PARENT.fullmatch(value)):
        raise ValueError("invalid parent: expected a bounded identifier (at most 128 ASCII characters)")
    return value


class Cursor(int):
    """A sequence bound to a task; use to_dict() to preserve binding across JSON."""
    def __new__(cls, task, seq):
        instance = int.__new__(cls, cursor_value(seq, task))
        instance.task = _validate_task(task)
        return instance

    def to_dict(self):
        return {"task": self.task, "seq": int(self)}


def valid_id(value):
    """Opaque, lowercase hex event id. Never a sequence and never a path component."""
    text = str(value).strip().lower()
    if not ID.fullmatch(text):
        raise ValueError("invalid event id %s: expected 32 lowercase hex characters" % ascii(text[:64]))
    return text


def valid_kind(value):
    text = str(value or "")
    if not KIND.fullmatch(text):
        raise ValueError("invalid event kind %s: use 1-64 letters/digits starting with a letter, then '.', '_' or '-'"
                         % ascii(text[:64]))
    return text


def valid_generation(value):
    text = str(value if value is not None else "")
    if text and not GENERATION.fullmatch(text):
        raise ValueError("invalid generation %s: use a short token such as '3' or 'gen-3'"
                         % ascii(text[:64]))
    return text


def valid_consumer(value):
    text = str(value or "")
    if not CONSUMER.fullmatch(text) or ".." in text:
        raise ValueError("invalid consumer %s: use 1-64 letters/digits starting with a letter, "
                         "then '.', '_', ':' or '-' (no '..')" % ascii(text[:64]))
    return text


def cursor_value(value, task=None):
    """Cursors are non-negative integers. Junk, bools and floats are rejected, not guessed."""
    if isinstance(value, (Cursor, dict)):
        bound = value.task if isinstance(value, Cursor) else value.get("task")
        if task is None or bound != task:
            raise ValueError("event cursor belongs to another task")
        if isinstance(value, dict):
            if set(value) != {"task", "seq"}:
                raise ValueError("invalid event cursor fields")
            value = value["seq"]
        else:
            value = int(value)
    if value is None:
        return 0
    if isinstance(value, bool):
        raise ValueError("invalid event cursor: expected a non-negative sequence, not a boolean")
    if isinstance(value, int):
        number = value
    elif isinstance(value, str):
        text = value.strip()
        if not re.fullmatch(r"\d{1,19}", text):
            raise ValueError("invalid event cursor %s: expected a non-negative integer sequence"
                             % ascii(text[:64]))
        number = int(text)
    else:
        raise ValueError("invalid event cursor %s: expected a non-negative integer sequence"
                         % ascii(str(value)[:64]))
    if number < 0 or number > MAX_CURSOR:
        raise ValueError("invalid event cursor %d: out of range" % number)
    return number


def bounded_limit(value):
    number = cursor_value(value)
    if number <= 0:
        raise ValueError("invalid limit %d: expected a positive integer" % number)
    return min(number, MAX_LIMIT)


def _scan_secrets(value, trail, found):
    if isinstance(value, dict):
        for key, item in value.items():
            if SECRET_KEY.search(str(key)) and item not in (None, False, ""):
                found.append(trail + "." + str(key))
            _scan_secrets(item, trail + "." + str(key), found)
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _scan_secrets(item, "%s[%d]" % (trail, index), found)
    elif isinstance(value, str) and SECRET_VALUE.match(value.strip()):
        found.append(trail)


def check_payload(data, allow_secrets=False):
    """Event payloads are immutable JSON objects: no bytes, no NaN/Infinity, no credentials."""
    if not isinstance(data, dict):
        raise ValueError("event data must be a JSON object, not %s" % type(data).__name__)
    try:
        def keys(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if not isinstance(key, str):
                        raise ValueError("event data keys must be strings")
                    keys(item)
            elif isinstance(value, (list, tuple)):
                for item in value:
                    keys(item)
        keys(data)
        text = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        raise ValueError("event data must be finite JSON with string keys and bounded nesting")
    if len(text.encode("ascii")) > MAX_RECORD_BYTES:
        raise ValueError("event data is %d bytes; the per-record limit is %d"
                         % (len(text.encode("ascii")), MAX_RECORD_BYTES))
    if not allow_secrets:
        found = []
        _scan_secrets(data, "data", found)
        if found:
            raise ValueError("event data looks like it carries credentials at %s; pass "
                             "allow_secrets=True if that is really intended" % ", ".join(sorted(set(found))[:4]))
    return json.loads(text)


# --------------------------------------------------------------------------------------
# content digest / idempotency key


def canonical(task, generation, parent, kind, data):
    """Deterministic content bytes. Same content always yields the same bytes."""
    return json.dumps({"data": data, "generation": generation, "kind": kind, "parent": parent,
                       "task": task}, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False)


def digest_of(task, generation, parent, kind, data):
    return "sha256:" + hashlib.sha256(canonical(task, generation, parent, kind, data).encode("ascii")).hexdigest()


def idempotency_key(task, generation, parent, kind, data):
    """Deterministic 32-hex key: same contents reuse it, different contents must not."""
    return hashlib.sha256(canonical(task, generation, parent, kind, data).encode("ascii")).hexdigest()[:32]


# --------------------------------------------------------------------------------------
# durable append / bounded read primitives


def _sync_directory(path):
    if os.name == "nt":
        return
    try:  # the directory entry itself must survive a power cut, not only the file bytes
        fd = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def _append(path, line):
    """One complete line, O_APPEND, fsynced. Append-only: never replace, never compact."""
    data = line.encode("ascii")
    path.parent.mkdir(parents=True, exist_ok=True)
    created = not path.exists()
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        while data:
            written = os.write(fd, data)
            if written <= 0:
                raise OSError("event append made no progress")
            data = data[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    if created:
        _sync_directory(path.parent)


def _safe_file(path):
    if path.is_symlink() or (path.exists() and
                            (not path.is_file() or path.stat().st_nlink > 1)):
        raise ValueError("event sidecar must be a regular unlinked file: " + path.name)


def _repair(path, offset):
    """Caller holds the sidecar lock and has validated every complete row."""
    backup = path.with_name(path.name + ".torn." + uuid.uuid4().hex + ".bak")
    fd = os.open(str(backup), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as saved, path.open("rb") as source:
        while True:
            block = source.read(65536)
            if not block:
                break
            saved.write(block)
        saved.flush()
        os.fsync(saved.fileno())
    _sync_directory(path.parent)
    with path.open("r+b") as live:
        live.truncate(offset)
        live.flush()
        os.fsync(live.fileno())


def _epoch(value):
    try:
        finite = math.isfinite(value)
    except (TypeError, OverflowError):
        finite = False
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not finite or value < 0:
        raise ValueError("invalid event timestamp")
    return value


def _decode(line, path, task, where):
    """One complete line -> one validated record. Every failure is interior corruption."""
    try:
        record = json.loads(line.decode("ascii"))
    except (ValueError, UnicodeError, RecursionError):
        raise EventCorruption("%s %s: line is not one complete JSON record" % (path.name, where))
    if not isinstance(record, dict) or type(record.get("schema")) is not int or record.get("schema") != SCHEMA:
        raise EventCorruption("%s %s: record is not an events schema %d snapshot" % (path.name, where, SCHEMA))
    for field in ("id", "seq", "task", "generation", "parent", "kind", "epoch", "data", "digest", "key"):
        if field not in record:
            raise EventCorruption("%s %s: record is missing %r" % (path.name, where, field))
    if record.get("task") != task:
        # Cross-task ids are never resolved across task files; a foreign record is damage.
        raise EventCorruption("%s %s: record belongs to task %s, not %s"
                              % (path.name, where, ascii(str(record.get("task"))[:64]), task))
    try:
        if record["id"] != valid_id(record["id"]):
            raise ValueError("noncanonical event id")
        if type(record["seq"]) is not int or not 1 <= record["seq"] <= MAX_CURSOR:
            raise ValueError("invalid sequence")
        if not isinstance(record["kind"], str):
            raise ValueError("invalid event kind")
        valid_kind(record["kind"])
        if not isinstance(record["generation"], str):
            raise ValueError("invalid generation")
        valid_generation(record["generation"])
        valid_parent(record["parent"])
        _epoch(record["epoch"])
        check_payload(record["data"], allow_secrets=True)
        expected = digest_of(record["task"], record.get("generation", ""), record.get("parent", ""),
                             record["kind"], record["data"])
    except ValueError as error:
        raise EventCorruption("%s %s: payload is not canonical JSON (%s)" % (path.name, where, str(error)[:120]))
    if record["digest"] != expected or record["key"] != expected[7:39]:
        raise EventCorruption("%s %s: payload digest mismatch; the record was mutated after it was written"
                              % (path.name, where))
    return record


def records_of(path, task, after=0, torn=None):
    """Yield every complete, validated record with ``seq > after``.

    A torn **trailing** line is skipped and reported through ``torn`` (that is the only
    damage a crash can leave); anything else — an unparsable line, a foreign task, a digest
    mismatch, a sequence that does not advance — raises ``EventCorruption`` instead of being
    dropped. Callers bound their own memory: this generator never holds more than one record.
    """
    task = _validate_task(task)
    if Path(path).name != task + LOG_SUFFIX:
        raise ValueError("event log path does not match task")
    _safe_file(Path(path))
    path = Path(path)
    offset = number = previous = 0
    try:
        stream = open(str(path), "rb")
    except FileNotFoundError:
        return
    except OSError as error:
        raise EventCorruption("%s: cannot read the event log (%s)" % (path.name, str(error)[:120]))
    with stream:
        while True:
            line = stream.readline(MAX_LINE_BYTES)
            if not line:
                return
            number += 1
            where = "line %d (byte %d)" % (number, offset)
            if not line.endswith(b"\n"):
                if len(line) >= MAX_LINE_BYTES:
                    raise EventCorruption("%s %s: event record exceeds the %d byte limit"
                                          % (path.name, where, MAX_RECORD_BYTES))
                # A crash can only tear the last line; more data after it is interior damage.
                if stream.read(_PROBE):
                    raise EventCorruption("%s %s: incomplete event record followed by more data; "
                                          "interior corruption" % (path.name, where))
                if torn is not None:
                    torn.append(offset)
                return
            offset += len(line)
            try:
                record = _decode(line, path, task, where)
            except EventCorruption as error:
                raise EventCorruption("interior corruption: " + str(error))
            if record["seq"] != previous + 1:
                raise EventCorruption("%s %s: sequence %d does not follow %d; the sequence was reset"
                                      % (path.name, where, record["seq"], previous))
            previous = record["seq"]
            if record["seq"] > after:
                yield record


def scan(path, task, after=0, limit=None):
    """Collect at most ``limit`` records after a cursor.

    Full validation with bounded collection. The caller must hold the sidecar lock.
    """
    start = cursor_value(after, task)
    bound = bounded_limit(DEFAULT_LIMIT if limit is None else limit)
    records, torn, count = [], [], 0
    for record in records_of(path, task, after=start, torn=torn):
        count += 1
        if len(records) < bound:
            records.append(record)
    return records, bool(torn), count > bound


def last_record(path, task):
    """Newest complete record after validating the entire log, with bounded memory."""
    newest = None
    for record in records_of(path, task):
        newest = record
    return newest


# --------------------------------------------------------------------------------------
# results


class Page:
    """One bounded page after a cursor. ``cursor`` is what a consumer acknowledges."""

    __slots__ = ("task", "events", "cursor", "head", "more", "torn", "truncated")

    def __init__(self, task, events, cursor, head, torn=False, truncated=False):
        self.task = task
        self.events = events
        self.cursor = Cursor(task, cursor)
        self.head = head
        self.more = head > cursor
        self.torn = bool(torn)
        self.truncated = bool(truncated)

    def __len__(self):
        return len(self.events)

    def __iter__(self):
        return iter(self.events)

    def __bool__(self):
        return bool(self.events)

    def __repr__(self):
        return "Page(task=%s, events=%d, cursor=%s, head=%s, more=%s, torn=%s)" % (
            self.task, len(self.events), self.cursor, self.head, self.more, self.torn)

    def to_dict(self):
        return {"task": self.task, "events": self.events, "cursor": self.cursor.to_dict(), "head": self.head,
                "more": self.more, "torn": self.torn, "truncated": self.truncated}


class WaitReport:
    """What one delivery run did; repeat effects cannot be measured by this backend."""

    __slots__ = ("task", "consumer", "delivered", "cursor", "runs", "stopped", "torn")

    def __init__(self, task, consumer, delivered, cursor, runs=1, stopped=False, torn=False):
        self.task = task
        self.consumer = consumer
        self.delivered = delivered
        self.cursor = Cursor(task, cursor)
        self.runs = runs
        self.stopped = bool(stopped)
        self.torn = bool(torn)

    def __repr__(self):
        return "WaitReport(task=%s, consumer=%s, delivered=%d, cursor=%s, stopped=%s, torn=%s)" % (
            self.task, self.consumer, self.delivered, self.cursor, self.stopped, self.torn)

    def to_dict(self):
        return {"task": self.task, "consumer": self.consumer, "delivered": self.delivered,
                "cursor": self.cursor.to_dict(), "runs": self.runs, "stopped": self.stopped, "torn": self.torn}


def _cancelled(stop):
    if stop is None:
        return False
    return bool(stop() if callable(stop) else stop.is_set())


# --------------------------------------------------------------------------------------
# the store


class EventStore:
    """Durable per-task event backend rooted at a state directory."""

    def __init__(self, root=None):
        value = root or os.environ.get("AGENT_CLI_LOGS") or str(Path.home() / ".claude/agent-cli-logs")
        self.root = Path(native_path(value))
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    # -- paths ----------------------------------------------------------------------------

    def path(self, task, suffix=LOG_SUFFIX):
        if suffix not in (LOG_SUFFIX, ACK_SUFFIX):
            raise ValueError("invalid event sidecar suffix")
        path = self.root / (_validate_task(task) + suffix)
        _safe_file(path)
        return path

    def ack_path(self, task):
        return self.path(task, ACK_SUFFIX)

    # -- write ----------------------------------------------------------------------------

    def emit(self, task, generation="", parent="", kind="", data=None, event_id=None, allow_secrets=False):
        """Append one durable event and return it.

        ``event_id`` is an idempotency key: re-emitting identical content under the same key
        returns the stored event (``duplicate=True``) and appends nothing, while the same key
        with different content is rejected instead of overwriting history. Omit ``event_id``
        for a fresh opaque id and no deduplication at all.
        """
        name = _validate_task(task)
        gen = valid_generation(generation)
        par = valid_parent(parent)
        what = valid_kind(kind)
        payload = check_payload(data, allow_secrets)
        digest = digest_of(name, gen, par, what, payload)
        key = digest[7:39]
        identifier = valid_id(event_id) if event_id is not None else uuid.uuid4().hex

        path = self.path(name)
        with Lock(path):
            previous, existing, torn = None, None, []
            for item in records_of(path, name, torn=torn):
                previous = item
                if item["id"] == identifier:
                    existing = item
            if existing is not None and existing["digest"] != digest:
                raise ValueError("event id %s already exists in task %s with different content" % (identifier, name))
            if torn:
                _repair(path, torn[0])
            if existing is not None:
                existing["duplicate"] = True
                return existing
            if previous and previous["seq"] == MAX_CURSOR:
                raise ValueError("event sequence exhausted")
            record = {"schema": SCHEMA, "id": identifier, "seq": int(previous["seq"]) + 1 if previous else 1,
                      "task": name, "generation": gen, "parent": par, "epoch": time.time(), "kind": what,
                      "data": payload, "digest": digest, "key": key}
            line = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"
            _append(path, line)
        # Hand back what was actually written, so the caller cannot mutate stored state.
        record = _decode(line.encode("ascii"), path, name, "emit")
        record["duplicate"] = False
        return record

    def find_locked(self, path, task, identifier):
        """Id lookup for an idempotent retry; memory stays one record wide."""
        found = None
        for record in records_of(path, task):
            if record["id"] == identifier:
                found = record
        if found is not None:
            found["duplicate"] = False
        return found

    # -- read -----------------------------------------------------------------------------

    def read(self, task, after=None, limit=DEFAULT_LIMIT):
        """One bounded page of events strictly after ``after`` (a sequence or a cursor)."""
        name = _validate_task(task)
        start = cursor_value(after, name)
        bound = bounded_limit(limit)
        records, torn, head, count = [], [], 0, 0
        path = self.path(name)
        with Lock(path):
            for record in records_of(path, name, torn=torn):
                head = record["seq"]
                if head > start:
                    count += 1
                    if len(records) < bound:
                        records.append(record)
        return Page(name, records, records[-1]["seq"] if records else start,
                    head, bool(torn), count > bound)

    def find(self, task, event_id):
        """One event by opaque id, or None. Ids are per task and never resolve across tasks."""
        name = _validate_task(task)
        path = self.path(name)
        with Lock(path):
            return self.find_locked(path, name, valid_id(event_id))

    def head(self, task):
        """Newest sequence for this task, or 0. Never decreases, never resets."""
        name = _validate_task(task)
        path = self.path(name)
        with Lock(path):
            record = last_record(path, name)
        return int(record["seq"]) if record else 0

    def inspect(self, task):
        """Health snapshot: record count, head, bytes, and whether the trailing line was torn.

        Streamed, so counting a multi-gigabyte log costs one record of memory.
        """
        name = _validate_task(task)
        path = self.path(name)
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        torn, count, head = [], 0, 0
        with Lock(path):
            for record in records_of(path, name, torn=torn):
                count += 1
                head = int(record["seq"])
        return {"task": name, "records": count, "head": head, "bytes": size, "torn": bool(torn)}

    # -- consumer acknowledgment ---------------------------------------------------------

    def _fold_acks(self, path, task, repair=False):
        """Newest cursor per consumer. Interior damage here fails honest, like the log itself."""
        latest, epoch, torn, offset, number = {}, {}, [], 0, 0
        _safe_file(path)
        head = self.head(task)
        try:
            stream = open(str(path), "rb")
        except FileNotFoundError:
            return latest, epoch, bool(torn), number
        except OSError as error:
            raise EventCorruption("%s: cannot read the acknowledgment ledger (%s)"
                                  % (path.name, str(error)[:120]))
        with stream:
            while True:
                line = stream.readline(MAX_LINE_BYTES)
                if not line:
                    break
                number += 1
                where = "line %d (byte %d)" % (number, offset)
                if not line.endswith(b"\n"):
                    if len(line) >= MAX_LINE_BYTES or stream.read(_PROBE):
                        raise EventCorruption("%s %s: incomplete acknowledgment followed by more data"
                                              % (path.name, where))
                    torn.append(True)
                    break
                try:
                    record = json.loads(line.decode("ascii"))
                except (ValueError, UnicodeError, RecursionError):
                    raise EventCorruption("%s %s: line is not one complete JSON acknowledgment"
                                          % (path.name, where))
                if not isinstance(record, dict) or type(record.get("schema")) is not int or record.get("schema") != SCHEMA:
                    raise EventCorruption("%s %s: not an events schema %d acknowledgment"
                                          % (path.name, where, SCHEMA))
                if record.get("task") != task:
                    raise EventCorruption("%s %s: acknowledgment belongs to task %s, not %s"
                                          % (path.name, where, ascii(str(record.get("task"))[:64]), task))
                try:
                    if type(record.get("seq")) is not int or record["seq"] != number:
                        raise ValueError("acknowledgment journal sequence must advance by one")
                    who = valid_consumer(record.get("consumer"))
                    value = record["cursor"]
                    if type(value) is not int or not 1 <= value <= head or value <= latest.get(who, 0):
                        raise ValueError("acknowledgment cursor must strictly advance within log head")
                    timestamp = _epoch(record["epoch"])
                    if who not in latest and len(latest) >= MAX_CONSUMERS:
                        raise ValueError("acknowledgment consumer limit exceeded")
                except (ValueError, KeyError) as error:
                    raise EventCorruption("%s %s: invalid acknowledgment (%s)" % (path.name, where, error))
                latest[who] = value
                epoch[who] = timestamp
                offset += len(line)
        if torn and repair:
            _repair(path, offset)
        return latest, epoch, bool(torn), number - int(bool(torn))

    def cursors(self, task):
        """Every consumer's persisted cursor for this task, independently of each other."""
        name = _validate_task(task)
        path = self.ack_path(name)
        with Lock(path):
            latest, epoch, torn, count = self._fold_acks(path, name)
        return latest

    def cursor(self, task, consumer):
        """The consumer's persisted cursor (0 when it has never acknowledged anything)."""
        name = _validate_task(task)
        who = valid_consumer(consumer)
        path = self.ack_path(name)
        with Lock(path):
            latest, epoch, torn, count = self._fold_acks(path, name)
        return Cursor(name, latest.get(who, 0))

    def ack(self, task, consumer, cursor):
        """Persist one consumer's progress. Monotonic: equal or lower is a no-op, never a reset.

        Acknowledging past the log head is refused: a consumer cannot be ahead of the events
        that exist. Cursor comparison and the append happen under the same lock, so two racing
        acknowledgments cannot interleave or lose the higher value.
        """
        name = _validate_task(task)
        who = valid_consumer(consumer)
        value = cursor_value(cursor, name)
        path = self.ack_path(name)
        with Lock(path):
            latest, epoch, torn, count = self._fold_acks(path, name, repair=True)
            current = latest.get(who, 0)
            if value <= current:
                return {"task": name, "consumer": who, "cursor": current, "advanced": False,
                        "epoch": epoch.get(who, 0)}
            if who not in latest and len(latest) >= MAX_CONSUMERS:
                raise ValueError("event consumer limit exceeded")
            head = self.head(name)
            if value > head:
                raise ValueError("cannot acknowledge sequence %d for task %s: the log head is %d"
                                 % (value, name, head))
            record = {"schema": SCHEMA, "seq": count + 1, "task": name, "consumer": who,
                      "cursor": value, "epoch": time.time()}
            _append(path, json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n")
        return {"task": name, "consumer": who, "cursor": value, "advanced": True, "epoch": record["epoch"]}

    # -- poll / wait / subscribe ----------------------------------------------------------

    def wait(self, task, consumer=None, after=None, timeout=None, limit=DEFAULT_LIMIT,
             poll_interval=0.05, stop=None):
        """Block until events after the cursor are available. Reads only; acknowledges nothing."""
        name = _validate_task(task)
        start = cursor_value(after, name) if after is not None else (
            self.cursor(name, consumer) if consumer is not None else 0)
        sleeper = Event()
        deadline = time.monotonic() + float(timeout) if timeout is not None else None
        while True:
            if _cancelled(stop):
                raise WaitTimeout("event wait cancelled for task " + name)
            page = self.read(name, after=start, limit=limit)
            if page.events:
                return page
            if _cancelled(stop) or (deadline is not None and time.monotonic() >= deadline):
                raise WaitTimeout("no event for task %s after sequence %d%s"
                                  % (name, start, " (cancelled)" if _cancelled(stop) else ""))
            sleeper.wait(min(0.25, max(0.001, float(poll_interval))))

    def subscribe(self, task, callback, consumer=None, after=None, timeout=None, limit=DEFAULT_LIMIT,
                  poll_interval=0.05, stop=None, until=None, acknowledge=True):
        """Deliver events to ``callback`` until ``until`` says stop, ``stop`` fires, or timeout.

        Delivery is at least once and **not** exactly once. The durable cursor advances only
        after ``callback`` returns, so a raising callback loses nothing: its successful
        prefix is acknowledged and the failed event is replayed by the next run. A callback
        that acts and then raises will be invoked again for that event, so callbacks should
        be idempotent (use the event ``id``).
        """
        name = _validate_task(task)
        who = valid_consumer(consumer) if consumer is not None else None
        durable = bool(who) and acknowledge
        start = cursor_value(after, name) if after is not None else (self.cursor(name, who) if who else 0)
        sleeper = Event()
        position = start
        delivered, torn = 0, False
        deadline = time.monotonic() + float(timeout) if timeout is not None else None
        while True:
            if _cancelled(stop):
                return WaitReport(name, who or "", delivered, position, stopped=True, torn=torn)
            if deadline is not None and time.monotonic() >= deadline:
                raise WaitTimeout("event subscription timed out for task " + name)
            page = self.read(name, after=position, limit=limit)
            torn = torn or page.torn
            if page.events:
                for record in page.events:
                    if _cancelled(stop):
                        return WaitReport(name, who or "", delivered, position, stopped=True, torn=torn)
                    if deadline is not None and time.monotonic() >= deadline:
                        raise WaitTimeout("event subscription timed out for task " + name)
                    sequence = record["seq"]
                    callback(record)
                    delivered += 1
                    position = sequence
                    if durable:
                        self.ack(name, who, position)
                    if until is not None and until(record):
                        return WaitReport(name, who or "", delivered, position, stopped=True, torn=torn)
                continue
            if _cancelled(stop):
                return WaitReport(name, who or "", delivered, position, stopped=True, torn=torn)
            if deadline is not None and time.monotonic() >= deadline:
                raise WaitTimeout("no new event for task %s after sequence %d within %ss"
                                  % (name, position, timeout))
            sleeper.wait(min(0.25, max(0.001, float(poll_interval))))

    def drain(self, task, callback, consumer=None, limit=DEFAULT_LIMIT, acknowledge=True):
        """Replay every event after this consumer's durable cursor, then return the report.

        A callback failure raises out of the drain with the successful prefix already
        acknowledged, so the next drain resumes at the failed event instead of skipping it.
        """
        name = _validate_task(task)
        who = valid_consumer(consumer) if consumer is not None else None
        durable = bool(who) and acknowledge
        position = self.cursor(name, who) if who else 0
        delivered, torn = 0, False
        while True:
            page = self.read(name, after=position, limit=bounded_limit(limit))
            torn = torn or page.torn
            if not page.events:
                return WaitReport(name, who or "", delivered, position, torn=torn)
            for record in page.events:
                sequence = record["seq"]
                callback(record)
                delivered += 1
                position = sequence
                if durable:
                    self.ack(name, who, position)
