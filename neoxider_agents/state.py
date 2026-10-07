"""Legacy-compatible atomic task state, publication locks and bounded log access."""
import os
import re
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
    if not NAME.fullmatch(name):
        raise ValueError("invalid task name '%s': use 1-128 ASCII letters/digits followed by letters, digits, '.', '_' or '-'" % name)
    return name


def atomic_write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp." + uuid.uuid4().hex)
    fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(text.encode("utf-8"))
        deadline = time.monotonic() + 2
        delay = 0.005
        while True:
            try:
                os.replace(str(temporary), str(path))
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
        except FileNotFoundError:
            pass


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
        from .process import pid_alive
        deadline = time.monotonic() + self.timeout if self.timeout is not None else None
        sleeper = Event()
        candidate = Path(str(self.path) + ".candidate." + self.token)
        while True:
            try:
                candidate.mkdir(mode=0o700)
                (candidate / self.owner.name).write_text("%s %s\n" % (os.getpid(), self.token), encoding="ascii")
                os.rename(str(candidate), str(self.path))
                return self
            except (FileExistsError, PermissionError, OSError):
                try:
                    (candidate / self.owner.name).unlink()
                    candidate.rmdir()
                except OSError:
                    pass
            try:
                owners = list(self.path.glob("owner*"))
                for owner in owners:
                    fields = owner.read_text(encoding="ascii").split()
                    age = time.time() - owner.stat().st_mtime
                    native = len(fields) > 1 and bool(re.fullmatch(r"[0-9]+-[0-9a-f]{32}", fields[1]))
                    known_dead = native and not pid_alive(fields[0])
                    if self.path.name.endswith(".owner.lock.d"):
                        meta = read_meta(self.path.with_name(self.path.name[:-len(".owner.lock.d")] + ".meta"))
                        if fields and fields[0] == meta.get("pid") and meta.get("winpid"):
                            known_dead = not pid_alive(meta["winpid"], meta.get("pid_start", "") if meta.get("core_version") else "")
                            if not known_dead:
                                continue
                    if fields and (known_dead or (not pid_alive(fields[0]) and age >= 10)):
                        owner.unlink()
                self.path.rmdir()
                continue
            except (OSError, ValueError):
                pass
            if deadline and time.monotonic() >= deadline:
                raise ValueError("task lock timed out: " + str(self.path))
            sleeper.wait(0.1)

    def __exit__(self, *unused):
        deadline = time.monotonic() + 2
        while True:
            try:
                self.owner.unlink()
                break
            except FileNotFoundError:
                break
            except PermissionError:
                if time.monotonic() >= deadline:
                    raise
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
        for key, value in fields.items():
            if not re.fullmatch(r"[A-Za-z0-9_-]+", key) or "\n" in str(value) or "\r" in str(value):
                raise ValueError("metadata must contain single-line keys/values")
        path = self.path(name, ".meta")
        with Lock(path):
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

    def enqueue_locked(self, name, text):
        box = self.path(name, ".inbox")
        box.mkdir(mode=0o700, exist_ok=True)
        try:
            sequence = int((box / "sequence").read_text())
        except FileNotFoundError:
            sequence = 0
        sequence = max([sequence] + [int(p.stem) for p in self.inbox(name)]) + 1
        atomic_write(box / ("%012d.msg" % sequence), text)
        atomic_write(box / "sequence", str(sequence) + "\n")
        return sequence

    def batch_locked(self, name):
        files = self.inbox(name)
        return files, "\n\n".join("Message #%s:\n%s" % (int(path.stem), path.read_text(encoding="utf-8")) for path in files)

    def seen(self, name):
        self.path(name, ".seen").touch(mode=0o600)
