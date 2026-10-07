"""File deltas and persistent, launcher-visible termination results."""
import json
import hashlib
import fnmatch
import os
import stat
import time
from datetime import datetime
from pathlib import Path
from .state import atomic_write, last_output, tail

STOP_EXIT = 130
IGNORED_DIRS = frozenset((".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__",
                          ".pytest_cache", ".mypy_cache", ".ruff_cache", ".cache", "dist", "build"))
TEXT_LIMIT = 2 * 1024 * 1024
BASELINE_TEXT_LIMIT = 16 * 1024 * 1024


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def snapshot(directory, excluded=(), capture=None):
    """Hash working-tree contents, including pre-existing dirty and untracked files.

    Stat data is descriptive only: same-size writes with restored timestamps still count.
    Symlinks are hashed as links and never followed outside the working directory.
    """
    result = {}
    if not directory:
        return result
    root = Path(directory).resolve()
    excluded = tuple(os.path.normcase(str(Path(path).resolve())) for path in excluded)

    def ignored(path):
        value = os.path.normcase(str(path))
        return any(value == item or value.startswith(item + os.sep) for item in excluded)

    for parent, dirs, files in os.walk(str(root)):
        dirs[:] = [d for d in dirs if d not in IGNORED_DIRS and not ignored(Path(parent) / d)]
        for name in files:
            path = Path(parent) / name
            if name.startswith("PROGRESS.") and name.endswith(".md") or name.startswith(".agent") or ignored(path):
                continue
            try:
                info = path.lstat()
                if stat.S_ISLNK(info.st_mode):
                    data = os.readlink(str(path)).encode("utf-8", "surrogateescape")
                    digest = hashlib.sha256(data).hexdigest()
                    entry = dict(sha256=digest, size=len(data), mtime_ns=info.st_mtime_ns, type="symlink")
                elif stat.S_ISREG(info.st_mode):
                    digest = hashlib.sha256()
                    data = bytearray() if capture is not None and info.st_size <= TEXT_LIMIT else None
                    with path.open("rb") as source:
                        for block in iter(lambda: source.read(65536), b""):
                            digest.update(block)
                            if data is not None:
                                if len(data) + len(block) <= TEXT_LIMIT:
                                    data.extend(block)
                                else:
                                    data = None
                    entry = dict(sha256=digest.hexdigest(), size=info.st_size, mtime_ns=info.st_mtime_ns, type="file")
                    if data is not None:
                        capture(entry, bytes(data))
                else:
                    continue
                result[path.relative_to(root).as_posix()] = entry
            except OSError:
                pass
    return result


def _git_status(directory):
    """Git is sampled once at task start; dashboard polling never spawns it."""
    root = Path(directory).resolve()
    if not os.environ.get("GIT_DIR") and not any((parent / ".git").exists() for parent in (root,) + tuple(root.parents)):
        return None
    import subprocess
    from .process import hidden_kwargs
    try:
        proc = subprocess.run(["git", "--no-optional-locks", "-C", str(directory), "status", "--porcelain=v1", "-z", "--untracked-files=all"],
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10, **hidden_kwargs())
        return proc.stdout.decode("utf-8", "replace").split("\0")[:-1] if proc.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def begin_snapshot(store, name, directory, force=False):
    """One task baseline survives send/restart; a new run must pass force=True."""
    baseline = store.path(name, ".baseline.json")
    if baseline.is_file() and not force:
        return
    folder = store.path(name, ".baseline.files")
    if store.root.resolve() not in folder.resolve().parents:
        raise ValueError("baseline path leaves the task state directory; remove the linked baseline folder before running")
    folder.mkdir(mode=0o700, exist_ok=True)
    if force:
        for old in folder.iterdir():
            if old.is_file() and not old.is_symlink():
                try:
                    old.unlink()
                except OSError:
                    pass
        try:
            store.path(name, ".changes.json").unlink()
        except OSError:
            pass
    remaining = [BASELINE_TEXT_LIMIT]

    def capture(entry, data):
        if b"\0" in data or len(data) > remaining[0]:
            return
        try:
            text = data.decode("utf-8")
        except UnicodeError:
            return
        blob = entry["sha256"]
        if not (folder / blob).is_file():
            atomic_write(folder / blob, text)
            remaining[0] -= len(data)
        entry["content"] = blob

    data = dict(version=2, directory=Path(directory).resolve().as_posix(), created_epoch=time.time(),
                git_status=_git_status(directory), files=snapshot(directory, (store.root,), capture))
    atomic_write(baseline, json.dumps(data, ensure_ascii=False))
    store.update(name, delta_known=1)


def read_baseline(store, name):
    try:
        data = json.loads(store.path(name, ".baseline.json").read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        return data
    except (OSError, ValueError):
        return {}


def _different(initial, current):
    if isinstance(initial, list):  # Phase 2A baselines remain readable.
        return initial != ([current["size"], current["mtime_ns"]] if current else None)
    if not initial or not current:
        return initial != current
    return (initial.get("sha256"), initial.get("type")) != (current.get("sha256"), current.get("type"))


def owned_paths(store, name, directory, paths):
    """A declared ownership scope excludes other workers' disjoint workspace edits."""
    owns = store.read(name).get("owns", "")
    patterns = [pattern.strip().replace("\\", "/") for pattern in owns.split(",") if pattern.strip()]
    if not patterns:
        return sorted(paths)
    root = str(Path(directory).resolve())

    def normalize(value):
        value = os.path.normpath(os.path.join(root, value)).replace("\\", "/")
        return value.casefold() if os.name == "nt" else value

    patterns = [normalize(pattern) for pattern in patterns]
    return sorted(path for path in paths if any(fnmatch.fnmatchcase(normalize(path), pattern) for pattern in patterns))


def changed_files(store, name, directory, scope=True):
    baseline = read_baseline(store, name)
    if not baseline:
        return []
    initial = baseline.get("files", baseline)
    current = snapshot(directory, (store.root,)) if directory and Path(directory).is_dir() else {}
    names = sorted(key for key in set(initial) | set(current) if _different(initial.get(key), current.get(key)))
    return owned_paths(store, name, directory, names) if scope else names


def file_changes(store, name, directory, freeze=False, frozen=True):
    """Detailed deltas against the task start, with bounded text reads for line counts."""
    import difflib
    if frozen and not freeze:
        try:
            saved = json.loads(store.path(name, ".changes.json").read_text(encoding="utf-8"))
            if isinstance(saved, list):
                return saved
        except (OSError, ValueError):
            pass
    baseline = read_baseline(store, name)
    if not baseline:
        return []
    initial = baseline.get("files", baseline)
    current = snapshot(directory, (store.root,)) if directory and Path(directory).is_dir() else {}
    result = []
    for key in owned_paths(store, name, directory, set(initial) | set(current)):
        before, after = initial.get(key), current.get(key)
        if not _different(before, after):
            continue
        entry = dict(path=key, status="added" if before is None else "deleted" if after is None else "modified",
                     insertions=None, deletions=None, binary=False, after_sha256=after.get("sha256") if after else None)
        old_text, new_text = "" if before is None else None, "" if after is None else None
        try:
            if isinstance(before, dict) and before.get("content"):
                old_text = (store.path(name, ".baseline.files") / before["content"]).read_bytes().decode("utf-8")
            if after and after.get("type") == "file" and after.get("size", TEXT_LIMIT + 1) <= TEXT_LIMIT:
                raw = (Path(directory) / key).read_bytes()
                if len(raw) <= TEXT_LIMIT and b"\0" not in raw:
                    new_text = raw.decode("utf-8")
        except (OSError, UnicodeError):
            pass
        if old_text is not None and new_text is not None:
            old_lines, new_lines = old_text.splitlines(keepends=True), new_text.splitlines(keepends=True)
            additions = removals = 0
            for kind, i, j, a, b in difflib.SequenceMatcher(None, old_lines, new_lines).get_opcodes():
                if kind != "equal":
                    removals += j - i
                    additions += b - a
            entry.update(insertions=additions, deletions=removals)
        else:
            entry["binary"] = True
        result.append(entry)
    if freeze:
        try:
            atomic_write(store.path(name, ".changes.json"), json.dumps(result, ensure_ascii=False))
        except OSError:
            pass
    return result


def stop_block(store, name, meta=None):
    data = meta or store.read(name)
    try:
        return store.path(name, ".stop").read_text(encoding="utf-8")
    except OSError:
        return make_stop_block(store, name, data)


def make_stop_block(store, name, data):
    from activity import redact
    from .logs import answer_text
    partial = data.get("partial_result") or answer_text(store, name)[-1200:].strip()
    digest = data.get("last_activity") or tail(store.path(name, ".log"), 4096).strip().splitlines()[-1:] or ["none"]
    if isinstance(digest, list):
        digest = digest[0]
    partial, digest = redact(partial), redact(digest)
    started = data.get("task_started_epoch") or data.get("started_epoch")
    if not started:
        try:
            started = datetime.strptime(data.get("started", ""), "%Y-%m-%d %H:%M:%S").timestamp()
        except ValueError:
            started = time.time()
    duration = max(0, time.time() - float(started))
    changed = data.get("changed_names", "")
    files = data.get("files", "0") if data.get("delta_known") != "0" else "unknown (legacy task; no start snapshot)"
    return ("■ STOPPED task=%s by=%s at %s reason=%s\n"
            "· ran %.1fs · last activity: %s\n"
            "· files changed by this agent (delta since start): %s%s\n"
            "· partial result: %s\n· session=%s\n"
            "· resume: neoxider send %s \"...\" / neoxider restart %s\n") % (
                name, data.get("stopped_by", "orchestrator"), data.get("stopped_at") or now(),
                redact(data.get("reason") or "launcher stopped"), duration, digest,
                files, " " + changed if changed else "", partial or "(none)",
                store.session(name, data) or "(unavailable)", name, name)


def record_stop(store, name, by="orchestrator", reason="stopped by orchestrator", code=STOP_EXIT,
                state="stopped", partial="", activity=""):
    meta = store.read(name)
    names = changed_files(store, name, meta.get("dir", ""))
    from activity import redact
    from .ux_tracking import record_history
    fields = dict(state=state, exit=code, reason=redact(reason), stopped_by=by, stopped_at=now(), finished_epoch=time.time(),
                  files=len(names), changed_names=" | ".join(names), delta_known=int(store.path(name, ".baseline.json").is_file()), session=store.session(name, meta))
    if partial:
        fields["partial_result"] = redact(" ".join(partial[-1200:].split()))
    if activity:
        fields["last_activity"] = redact(" ".join(activity.split()))[:500]
    data = store.update(name, **fields)
    file_changes(store, name, meta.get("dir", ""), freeze=True)
    record_history(store, name, "stop", by=by, reason=reason, exit=code, state=state)
    block = make_stop_block(store, name, data)
    atomic_write(store.path(name, ".stop"), block)
    return block


def render_md(store, name):
    data = store.read(name)
    path = store.path(name, ".md")
    temporary = path.with_suffix(".md.tmp.%s" % os.getpid())
    with temporary.open("w", encoding="utf-8", newline="\n") as out:
        out.write("# Subagent task: %s\n\n" % name)
        for key in ("state", "engine", "model", "dir", "session", "exit", "files", "reason"):
            out.write("- **%s:** %s\n" % (key.title(), data.get(key, "")))
        out.write('- **Resume:** `neoxider send %s "..."`\n\n```text\n' % name)
        try:
            with store.path(name, ".answer").open("r", encoding="utf-8", errors="replace") as answer:
                for chunk in iter(lambda: answer.read(65536), ""):
                    out.write(chunk)
        except OSError:
            from .state import output_chunks
            try:
                for chunk in output_chunks(store.path(name, ".log")):
                    out.write(chunk)
            except OSError:
                pass
        out.write("\n```\n")
    os.replace(str(temporary), str(path))
