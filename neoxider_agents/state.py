"""Legacy-compatible atomic task state, publication locks and bounded log access."""
import os
import json
import re
import shutil
import time
import uuid
from pathlib import Path
from threading import Event

MARK = "---------- output ----------"
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def native_path(value):
    if os.name == "nt" and re.match(r"^/[A-Za-z]/", str(value)):
        return str(value)[1].upper() + ":" + str(value)[2:]
    return str(value)


def valid_name(name):
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise ValueError("invalid task name '%s': use 1-128 ASCII letters/digits followed by letters, digits, '.', '_' or '-'" % name)
    stem = name.split('.', 1)[0].upper()
    if name.endswith('.') or re.fullmatch(r'(?:CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])', stem):
        raise ValueError("invalid task name '%s': reserved filename or trailing dot" % name)
    return name


def atomic_write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp." + uuid.uuid4().hex)
    fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(text.encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        deadline = time.monotonic() + 2
        delay = 0.005
        while True:
            try:
                os.replace(str(temporary), str(path))
                sync_directory(path.parent)
                break
            except PermissionError:
                if os.name != "nt" or time.monotonic() >= deadline:
                    raise
                # Windows readers without FILE_SHARE_DELETE briefly hold the destination.
                Event().wait(delay)
                delay = min(0.1, delay * 2)
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass


def sync_directory(path):
    """Persist rename/unlink entries on platforms supporting directory fsync."""
    if os.name == "nt":
        return  # Windows does not expose directory fsync through the stdlib.
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def read_meta(path):
    try:
        fields = {}
        for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                fields.setdefault(key, value)
        return fields
    except (OSError, UnicodeError):
        return {}


class Lock:
    """Immutable directory generations also interlock with Phase 1 Bash writers."""
    def __init__(self, path, timeout=30):
        self.path = Path(str(path) + ".lock.d")
        self.timeout = timeout
        self.token = "%s-%s" % (os.getpid(), uuid.uuid4().hex)
        self.owner = self.path / ("owner." + self.token)

    def __enter__(self):
        from .process import pid_stamp
        deadline = time.monotonic() + self.timeout if self.timeout is not None else None
        sleeper = Event()
        candidate = Path(str(self.path) + ".candidate." + self.token)
        while True:
            try:
                candidate.mkdir(mode=0o700)
                (candidate / self.owner.name).write_text("%s %s %s\n" % (os.getpid(), self.token, pid_stamp(os.getpid())), encoding="ascii")
                os.rename(str(candidate), str(self.path))
                return self
            except (FileExistsError, PermissionError, OSError):
                try:
                    (candidate / self.owner.name).unlink()
                    candidate.rmdir()
                except OSError:
                    pass
            holders = self._holders()
            if holders and all(not alive for _, alive in holders) and self._reclaim():
                continue
            if deadline and time.monotonic() >= deadline:
                detail = ", ".join("held by pid %s (%s)" % (pid, "alive" if alive else "dead") for pid, alive in holders)
                raise ValueError("task lock timed out: " + str(self.path) + ("; " + detail if detail else "; holder unknown"))
            sleeper.wait(0.1)

    def _holders(self):
        from .process import pid_alive
        try:
            holders = []
            for owner in self.path.glob("owner*"):
                fields = owner.read_text(encoding="ascii").split()
                if not fields or not fields[0].isdigit():
                    return []  # Unknown writers must not be evicted.
                # Bash generations use pid-random-time; Python uses pid-UUID.
                native = len(fields) > 1 and fields[1].startswith(fields[0] + "-")
                stamp = (fields[2] if len(fields) > 2 else "") if native else (fields[1] if len(fields) > 1 else "")
                pid = fields[0]
                if self.path.name.endswith(".owner.lock.d"):
                    meta = read_meta(self.path.with_name(self.path.name[:-len(".owner.lock.d")] + ".meta"))
                    if pid == meta.get("pid") and meta.get("winpid"):
                        pid, stamp = meta["winpid"], meta.get("pid_start", "") if meta.get("core_version") else ""
                holders.append((pid, pid_alive(pid, stamp)))
            return holders
        except (OSError, UnicodeError, ValueError):
            return []

    def _reclaim(self):
        # Keep this inode: unlinking an advisory lock lets another contender lock
        # a different inode. OS locks are released automatically after a crash.
        guard = Path(str(self.path) + ".reap")
        try:
            with guard.open("a+b") as stream:
                if not guard.stat().st_size:
                    stream.write(b"0")
                    stream.flush()
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                try:
                    # Another reaper may already have published a live generation.
                    holders = self._holders()
                    if not holders or any(alive for _, alive in holders):
                        return False
                    retired = Path(str(self.path) + ".retired." + uuid.uuid4().hex)
                    os.rename(str(self.path), str(retired))
                    try:
                        shutil.rmtree(str(retired))
                    except OSError:
                        pass  # Publication is already free; cleanup is best effort.
                    return True
                finally:
                    if os.name == "nt":
                        stream.seek(0)
                        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        except OSError:
            return False

    def __exit__(self, *unused):
        deadline = time.monotonic() + 2
        while True:
            try:
                self.owner.unlink()
                break
            except FileNotFoundError:
                break
            except OSError:
                if time.monotonic() >= deadline:
                    break
                Event().wait(0.01)
        try:
            self.path.rmdir()
        except OSError:
            pass


def tail(path, limit=65536):
    try:
        with Path(path).open("rb") as stream:
            stream.seek(0, 2)
            stream.seek(max(0, stream.tell() - limit))
            return stream.read(limit).decode("utf-8", "replace")
    except OSError:
        return ""


def last_output(path):
    """Read backwards to the final marker; memory stays bounded for huge threads."""
    chunks = []
    try:
        with Path(path).open("rb") as stream:
            stream.seek(0, 2)
            pos = stream.tell()
            while pos > 0:
                size = min(pos, 65536)
                pos -= size
                stream.seek(pos)
                chunk = stream.read(size)
                chunks.insert(0, chunk)
                block = b"".join(chunks)
                matches = list(re.finditer(rb"(?:^|\n)---------- output ----------\r?\n", block))
                if matches:
                    block = block[matches[-1].end():]
                    break
                if len(block) >= 1048576:
                    break
            else:
                block = b"".join(chunks)
        text = block.decode("utf-8", "replace")
        return "\n".join(line for line in text.splitlines() if not line.startswith("[agent-activity] ")) + ("\n" if text else "")
    except OSError:
        return ""


def output_offset(path):
    with Path(path).open("rb") as stream:
        stream.seek(0, 2)
        pos = stream.tell()
        suffix = b""
        while pos:
            size = min(pos, 65536)
            pos -= size
            stream.seek(pos)
            block = stream.read(size) + suffix
            matches = list(re.finditer(rb"\n---------- output ----------\r?\n", block))
            if matches:
                return pos + matches[-1].end()
            if pos == 0 and block.startswith((MARK + "\n").encode()):
                return len(MARK) + 1
            suffix = block[:64]
    return 0


def output_chunks(path):
    offset = output_offset(path)
    with Path(path).open("r", encoding="utf-8", errors="replace", newline="") as stream:
        stream.seek(offset)
        start = True
        skip = False
        while True:
            line = stream.readline(65536)
            if not line:
                return
            if start:
                skip = line.startswith("[agent-activity] ")
            if not skip:
                yield line
            start = line.endswith("\n")


def log_offset(path, lines=0, last_step=False):
    """Locate a tail boundary without estimating line sizes or retaining the log."""
    with Path(path).open("rb") as stream:
        stream.seek(0, 2)
        pos = stream.tell()
        if not pos or (not lines and not last_step):
            return 0
        if lines:
            stream.seek(pos - 1)
            remaining = lines + (stream.read(1) == b"\n")
        overlap = b""
        while pos:
            size = min(pos, 65536)
            pos -= size
            stream.seek(pos)
            block = stream.read(size)
            if last_step:
                combined = block + overlap
                index = combined.rfind(b"\n========== [")
                if index >= 0:
                    return pos + index + 1
                if pos == 0 and combined.startswith(b"========== ["):
                    return 0
                overlap = combined[:32]
            else:
                end = len(block)
                while remaining:
                    index = block.rfind(b"\n", 0, end)
                    if index < 0:
                        break
                    remaining -= 1
                    if not remaining:
                        return pos + index + 1
                    end = index
    return 0


class Store:
    def __init__(self, root=None):
        value = root or os.environ.get("AGENT_CLI_LOGS") or str(Path.home() / ".claude/agent-cli-logs")
        self.root = Path(native_path(value))
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def path(self, name, suffix):
        return self.root / (valid_name(name) + suffix)

    def read(self, name):
        data = read_meta(self.path(name, ".meta"))
        if data and data.get("state") != "running":
            from .logs import prune_task
            prune_task(self, name, data)
        return data

    def update(self, name, **fields):
        with Lock(self.path(name, ".meta")):
            return self.update_locked(name, **fields)

    def update_locked(self, name, **fields):
        """Caller holds the .meta publication lock; never take it twice."""
        for key, value in fields.items():
            if not re.fullmatch(r"[A-Za-z0-9_-]+", key) or "\n" in str(value) or "\r" in str(value):
                raise ValueError("metadata must contain single-line keys/values")
        path = self.path(name, ".meta")
        data = read_meta(path)
        data.update({key: str(value) for key, value in fields.items()})
        atomic_write(path, "".join("%s=%s\n" % item for item in data.items()))
        return data

    def scan(self):
        result = []
        with os.scandir(str(self.root)) as entries:
            for entry in entries:
                if entry.name.endswith(".meta") and NAME.fullmatch(entry.name[:-5]) and entry.is_file():
                    try:
                        result.append((entry.name[:-5], read_meta(entry.path), entry.stat().st_mtime))
                    except OSError:
                        pass
        from .logs import prune_task
        for name, meta, _ in result:
            prune_task(self, name, meta)
        return sorted(result, key=lambda item: (-item[2], item[0]))

    def resolve(self, ref=""):
        if ref and NAME.fullmatch(ref) and self.path(ref, ".meta").is_file():
            return ref
        for name, meta, _ in self.scan():
            if not ref or meta.get("session") == ref:
                return name
        if re.fullmatch(r"(?:[0-9a-f-]{36}|ses_[A-Za-z0-9_.-]+|session_[A-Za-z0-9_.-]+)", ref):
            return valid_name("session-" + ref)
        raise ValueError("no such task '%s'" % ref)

    def session(self, name, meta=None):
        data = meta if meta is not None else self.read(name)
        if data.get("session"):
            return data["session"]
        offset = int(data.get("session_log_offset") or 0)
        path = self.path(name, ".log")
        try:
            size = path.stat().st_size
        except OSError:
            return ""
        block = tail(path, min(65536, max(0, size - offset)))
        engine = True
        session = ""
        for line in block.splitlines():
            if line.startswith("========== ["):
                engine = False
            elif line == MARK:
                engine = True
            elif engine and re.fullmatch(r"session id: [A-Za-z0-9_.-]+", line):
                session = line.split(": ", 1)[1]
        return session

    def inbox(self, name):
        box = self.path(name, ".inbox")
        try:
            return sorted(Path(entry.path) for entry in os.scandir(str(box)) if entry.name.endswith(".msg") and entry.is_file())
        except FileNotFoundError:
            return []

    def enqueue_receipt(self, name, text, message_id=None):
        with Lock(self.path(name, ".inbox")):
            return self.enqueue_receipt_locked(name, text, message_id)

    def enqueue(self, name, text, message_id=None):
        return self.enqueue_receipt(name, text, message_id)["sequence"]

    def begin_delivery(self, name, paths, generation="", turn=0):
        with Lock(self.path(name, ".inbox")):
            return self.begin_delivery_locked(name, paths, generation, turn)

    def finish_delivery(self, name, paths, generation="", delivered=False, outcome=""):
        with Lock(self.path(name, ".inbox")):
            return self.finish_delivery_locked(name, paths, generation, delivered, outcome)

    def enqueue_locked(self, name, text, message_id=None):
        """Queue one message and return its legacy int sequence (unchanged contract).

        The caller holds the `.inbox` lock (as `lifecycle.send` does); receipts never
        take that lock again.
        """
        return self.enqueue_receipt_locked(name, text, message_id)["sequence"]

    def enqueue_receipt_locked(self, name, text, message_id=None):
        """Queue one message and return its durable receipt. Caller holds the `.inbox` lock.

        Acceptance is a transaction: intent snapshot, then the queue file, then the
        accepted snapshot. A crash therefore leaves an intent that reconcile finishes
        under the caller's UUID, or a rolled-back intent — never a receipt for a
        message that was not persisted.
        """
        from . import message_receipts
        return message_receipts.enqueue_locked(self, name, text, message_id=message_id,
                                               generation=read_meta(self.path(name, ".meta")).get("generation", ""))

    # Durable receipts: additive sidecars outside the .inbox glob (see message_receipts).
    # The `*_locked` names assume the caller holds the `.inbox` lock; the plain names take it.
    def receipts(self, name):
        from . import message_receipts
        return message_receipts.listing(self, name)

    def receipt(self, name, ref):
        from . import message_receipts
        return message_receipts.receipt(self, name, ref)

    def begin_delivery_locked(self, name, paths, generation="", turn=0):
        from . import message_receipts
        return message_receipts.begin_attempt_locked(self, name, message_receipts.sequences(paths),
                                                     generation=generation, turn=turn)

    def finish_delivery_locked(self, name, paths, generation="", delivered=False, outcome=""):
        from . import message_receipts
        return message_receipts.finish_attempt_locked(self, name, message_receipts.sequences(paths),
                                                      generation=generation, delivered=delivered, outcome=outcome)

    def reconcile_receipts(self, name):
        from . import message_receipts
        return message_receipts.reconcile(self, name)

    def reconcile_receipts_locked(self, name):
        from . import message_receipts
        return message_receipts.reconcile_locked(self, name)

    def repair_receipts(self, name):
        from . import message_receipts
        return message_receipts.repair(self, name)

    def torn_receipt_tail(self, name):
        from . import message_receipts
        return message_receipts.torn_tail(self, name)

    def acknowledge_message(self, name, ref, source="assistant", text="", note=""):
        from . import message_receipts
        return message_receipts.acknowledge(self, name, ref, source=source, text=text, note=note)

    def acknowledge_message_locked(self, name, ref, source="assistant", text="", note=""):
        from . import message_receipts
        return message_receipts.acknowledge_locked(self, name, ref, source=source, text=text, note=note)

    def mark_message_applied(self, name, ref, note=""):
        from . import message_receipts
        return message_receipts.mark_applied(self, name, ref, note=note)

    def mark_message_applied_locked(self, name, ref, note=""):
        from . import message_receipts
        return message_receipts.mark_applied_locked(self, name, ref, note=note)

    def batch_locked(self, name, include_unknown=True):
        from . import message_receipts as mr
        mr.reconcile_locked(self, name)
        files = [path for path in self.inbox(name) if path.stem.isdigit()]
        if not include_unknown:
            files = [path for path in files if mr.receipt_locked(self, name, int(path.stem))["delivery_status"]
                     not in (mr.UNKNOWN, mr.ATTEMPTING)]
        return files, "\n\n".join("Message #%s:\n%s" % (int(path.stem), path.read_text(encoding="utf-8")) for path in files)

    def seen(self, name):
        try:
            self.path(name, ".seen").touch(mode=0o600)
        except OSError:
            pass

    def stage_event(self, name, kind, data, event_id=None, meta=None):
        """Capture an immutable publication intent before attempting the event append."""
        from .events import check_payload, idempotency_key, valid_id
        meta = self.read(name) if meta is None else meta
        envelope = dict(task=name, generation=meta.get("generation", ""),
                        parent=meta.get("parent", ""), kind=kind, data=check_payload(data))
        envelope["event_id"] = valid_id(event_id) if event_id else idempotency_key(
            name, envelope["generation"], envelope["parent"], kind, envelope["data"])
        path = self.path(name, ".event-outbox") / (envelope["event_id"] + ".json")
        with Lock(self.path(name, ".event-outbox")):
            if path.exists():
                existing = json.loads(path.read_text(encoding="utf-8"))
                content = {key: value for key, value in existing.items() if key != "intent_order"}
                if content != envelope:
                    raise ValueError("event intent identity changed")
                return existing
            counter = self.path(name, ".event-sequence")
            high = int(counter.read_text(encoding="ascii")) if counter.exists() else 0
            for pending in path.parent.glob("*.json"):
                row = json.loads(pending.read_text(encoding="utf-8"))
                high = max(high, row["intent_order"])
            if not 0 <= high < (1 << 63) - 1:
                raise ValueError("event intent sequence is corrupt/exhausted")
            envelope["intent_order"] = high + 1
            raw = json.dumps(envelope, sort_keys=True, separators=(",", ":"))
            atomic_write(path, raw)
            atomic_write(counter, str(high + 1))
        return envelope

    def publish_events(self, name):
        """Replay captured bytes, including terminal intents from earlier generations."""
        from .events import EventStore
        try:
            with Lock(self.path(name, ".event-outbox")):
                meta = self.read(name)
                if meta.get("event_pending") == "1" and meta.get("terminal_intent"):
                    intent = json.loads(meta["terminal_intent"])
                    from .events import valid_id
                    identifier = valid_id(intent["event_id"])
                    target = self.path(name, ".event-outbox") / (identifier + ".json")
                    if not target.exists():
                        atomic_write(target, json.dumps(intent, sort_keys=True, separators=(",", ":")))
                pending = [(json.loads(path.read_text(encoding="utf-8")), path)
                           for path in self.path(name, ".event-outbox").glob("*.json")]
                orders = [row["intent_order"] for row, _ in pending]
                if any(type(value) is not int or not 0 < value < (1 << 63) for value in orders) or len(set(orders)) != len(orders):
                    raise ValueError("event intent ordering is corrupt")
                for envelope, path in sorted(pending, key=lambda row: row[0]["intent_order"]):
                    if envelope.get("task") != name or path.stem != envelope.get("event_id"):
                        raise ValueError("event outbox task/identity mismatch")
                    EventStore(self.root).emit(**{key: value for key, value in envelope.items() if key != "intent_order"})
                    path.unlink()
                    sync_directory(path.parent)
            self.update(name, event_pending=0, event_error="")
            return True
        except (OSError, ValueError, RuntimeError, KeyError, TypeError) as error:
            from activity import redact
            self.update(name, event_pending=1, event_error=redact(str(error))[:500])
            return False

    def lifecycle_event(self, name, kind, data, event_id=None):
        self.stage_event(name, kind, data, event_id)
        return self.publish_events(name)

    def terminal_event(self, name, **fields):
        """Terminal outcome and its captured event survive a publisher process crash."""
        import hashlib
        meta = dict(self.read(name), **{k: str(v) for k, v in fields.items()})
        try:
            answer = self.path(name, ".answer").read_bytes()
        except FileNotFoundError:
            answer = b""
        payload = dict(state=meta.get("state", ""), exit_code=meta.get("exit", ""),
                       session=meta.get("session", ""), answer_sha256=hashlib.sha256(answer).hexdigest(),
                       files=meta.get("files", ""), tests_status="not_run", qa_status="not_run")
        identifier = uuid.uuid5(uuid.NAMESPACE_URL, "neoxider:%s:%s:terminal" %
                                (name, meta.get("generation", ""))).hex
        intent = self.stage_event(name, "terminal", payload, identifier, meta)
        self.update(name, **dict(fields, terminal_intent=json.dumps(intent, sort_keys=True), event_pending=1))
        return self.publish_events(name)
