"""Private bounded raw tails, durable compact activity and clean final answers."""
import json
import os
from pathlib import Path
import sys
import threading
import time
import uuid
from .state import atomic_write, last_output, tail

DEFAULT_CAP = 2 * 1024 * 1024


def enabled(value):
    return str(value).lower() in ("1", "true", "yes", "on")


def cap_bytes():
    try:
        return max(1024, int(os.environ.get("AGENT_LOG_MAX_BYTES", DEFAULT_CAP)))
    except ValueError:
        return DEFAULT_CAP


class TailWriter:
    """One shared writer for the pump/watchdog; trim before exceeding the cap."""
    def __init__(self, path, keep=False):
        self.path, self.keep = Path(path), keep
        self.cap = cap_bytes()
        self.lock = threading.RLock()
        self.stream = None

    def __enter__(self):
        fd = os.open(str(self.path), os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o600)
        self.stream = os.fdopen(fd, "a+b")
        return self

    def write(self, text):
        data = text.encode("utf-8")
        with self.lock:
            self.stream.seek(0, 2)
            size = self.stream.tell()
            if not self.keep and size + len(data) > self.cap:
                # Half-cap slack avoids rewriting the tail on every subsequent line.
                retained = max(0, self.cap // 2 - len(data))
                self.stream.seek(max(0, size - retained))
                previous = self.stream.read(retained) if retained else b""
                data = (previous + data)[-self.cap:]
                while data and data[0] & 0xC0 == 0x80:
                    data = data[1:]
                self.stream.seek(0)
                self.stream.truncate()
            self.stream.write(data)
            self.stream.flush()

    def flush(self):
        with self.lock:
            self.stream.flush()

    def __exit__(self, *unused):
        self.stream.close()


def prune_task(store, name, meta=None, force=False, dry=False, errors=None):
    data = meta if meta is not None else store.read(name)
    if data.get("state") == "running" or (not force and enabled(data.get("keep_logs", "0"))):
        return False
    path = store.path(name, ".log")
    try:
        hours = max(0, float(os.environ.get("AGENT_LOG_TTL_HOURS", 24)))
    except ValueError:
        hours = 24
    try:
        expired = time.time() - path.stat().st_mtime >= hours * 3600
        if force or expired:
            if not dry:
                if not store.path(name, ".answer").exists():
                    # Migrate a legacy final answer before removing its only copy.
                    from .state import output_chunks
                    answer = store.path(name, ".answer")
                    temporary = answer.with_name(answer.name + ".migration." + uuid.uuid4().hex)
                    try:
                        with temporary.open("x", encoding="utf-8", newline="\n") as out:
                            for chunk in output_chunks(path):
                                out.write(chunk)
                        # Publish a complete copy without overwriting another pruner's answer.
                        try:
                            os.link(str(temporary), str(answer))
                        except FileExistsError:
                            pass
                    finally:
                        try:
                            temporary.unlink()
                        except FileNotFoundError:
                            pass
                        except OSError as error:
                            if errors is not None:
                                errors.append((temporary, error))
                path.unlink()
            return True
    except FileNotFoundError as error:
        # A vanished raw log is a successful race loser. A vanished migration
        # file with the raw log still present is a failed explicit cleanup.
        if errors is not None:
            try:
                path.stat()
            except FileNotFoundError:
                pass
            except OSError as stat_error:
                errors.append((path, stat_error))
            else:
                errors.append((path, error))
    except OSError as error:
        # Leaving the raw file in place is the retry record for the next state read.
        if errors is not None:
            errors.append((path, error))
    return False


def record_digest(path, kind, detail, engine="", stamp=None):
    from activity import compact
    # Store only the small redacted digest, never provider payloads or reasoning.
    row = dict(recorded_at=stamp or time.time(), engine=engine, kind=kind,
               detail=compact(detail, 240))
    try:
        with Path(path).open("a", encoding="utf-8", newline="\n") as out:
            out.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass


def save_answer(store, name, output_filter=None, fallback=""):
    path = store.path(name, ".answer")
    temporary = path.with_name(path.name + ".tmp.%s" % os.getpid())
    fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as out:
        chunks = output_filter.iter_answer() if output_filter and output_filter.had_answer else [fallback]
        for chunk in chunks:
            out.write(chunk)
    os.replace(str(temporary), str(path))
    if output_filter and output_filter.answer_path:
        try:
            Path(output_filter.answer_path).unlink()
        except OSError:
            pass


def answer_text(store, name):
    try:
        return store.path(name, ".answer").read_text(encoding="utf-8")
    except OSError:
        return last_output(store.path(name, ".log"))


def print_answer(store, name):
    path = store.path(name, ".answer")
    try:
        if path.is_file():
            with path.open(encoding="utf-8") as source:
                for chunk in iter(lambda: source.read(65536), ""):
                    print(chunk, end="")
        else:
            from .state import output_chunks
            if store.path(name, ".log").exists():
                for chunk in output_chunks(store.path(name, ".log")):
                    print(chunk, end="")
    except OSError:
        # Another reader may just have migrated and removed the legacy capture.
        print(answer_text(store, name), end="")


def digest_rows(store, name, count=25):
    result = []
    for line in tail(store.path(name, ".activity.jsonl"), max(65536, count * 1024)).splitlines():
        try:
            row = json.loads(line)
            if "kind" in row:
                result.append(row)
            elif "event" in row:
                from activity import digest
                result.extend(dict(recorded_at=row.get("recorded_at", 0), kind=k, detail=d)
                              for k, d in digest(row["event"]))
        except (ValueError, TypeError):
            pass
    return result[-count:] if count else []


def peek(store, name, opts):
    from .lifecycle import effective
    from activity import compact
    prune_task(store, name)
    count = int(opts.get("-n", 25))
    follow = opts.get("-f", False)
    seen = None
    while True:
        rows = digest_rows(store, name, count)
        if rows:
            if seen is None:
                fresh = rows
            else:
                fresh = [row for row in rows if row.get("recorded_at", 0) > seen]
            for row in fresh:
                if opts.get("--raw"):
                    print(json.dumps(row, ensure_ascii=False))
                else:
                    print("%s %s" % (row["kind"], row["detail"]))
            seen = max(row.get("recorded_at", 0) for row in rows)
        elif seen is None:
            # Read legacy captures through the established digest parser.
            from activity import entries
            legacy = list(entries(tail(store.path(name, ".log")).splitlines(), time.time()))[-count:]
            for row in legacy:
                print("%s %s" % (row[1], row[2]))
            if not legacy:
                meta = store.read(name)
                print(compact(meta.get("last_activity") or meta.get("state", "no activity")))
            seen = 0
        if opts.get("_footer"):
            from activity import compact
            meta = store.read(name)
            footer = "task=%s state=%s queued=%s last=%s" % (
                name, effective(store, name, meta), len(store.inbox(name)), compact(meta.get("last_activity", "")))
            if footer != opts.get("_last_footer"):
                print("· " + footer)
                opts["_last_footer"] = footer
        if not follow or effective(store, name) not in ("running", "idle"):
            return 0
        sys.stdout.flush()
        threading.Event().wait(0.25)


def watch(store, name, opts):
    flags = dict(opts, **{"-f": True, "_footer": True})
    try:
        return peek(store, name, flags)
    finally:
        from .lifecycle import effective
        meta = store.read(name)
        print("task=%s state=%s queued=%s last=%s" % (
            name, effective(store, name, meta), len(store.inbox(name)), meta.get("last_activity", "")))
