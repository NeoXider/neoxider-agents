"""File deltas and persistent, launcher-visible termination results."""
import json
import os
import time
from datetime import datetime
from pathlib import Path
from .state import atomic_write, last_output, tail

STOP_EXIT = 130


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def snapshot(directory):
    """Stat fingerprints include dirty files modified again during a resumed turn."""
    result = {}
    root = Path(directory)
    for parent, dirs, files in os.walk(str(root)):
        dirs[:] = [d for d in dirs if d not in (".git", "node_modules", ".venv", "__pycache__")]
        for name in files:
            path = Path(parent) / name
            try:
                info = path.stat()
                result[path.relative_to(root).as_posix()] = [info.st_size, info.st_mtime_ns]
            except OSError:
                pass
    return result


def begin_snapshot(store, name, directory):
    atomic_write(store.path(name, ".baseline.json"), json.dumps(snapshot(directory)))
    store.update(name, delta_known=1)


def changed_files(store, name, directory):
    try:
        initial = json.loads(store.path(name, ".baseline.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    current = snapshot(directory) if directory and Path(directory).is_dir() else {}
    return sorted(key for key in set(initial) | set(current) if initial.get(key) != current.get(key))


def stop_block(store, name, meta=None):
    data = meta or store.read(name)
    try:
        return store.path(name, ".stop").read_text(encoding="utf-8")
    except OSError:
        return make_stop_block(store, name, data)


def make_stop_block(store, name, data):
    partial = data.get("partial_result") or last_output(store.path(name, ".log"))[-1200:].strip()
    digest = data.get("last_activity") or tail(store.path(name, ".log"), 4096).strip().splitlines()[-1:] or ["none"]
    if isinstance(digest, list):
        digest = digest[0]
    started = data.get("started_epoch")
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
                data.get("reason") or "launcher stopped", duration, digest,
                files, " " + changed if changed else "", partial or "(none)",
                store.session(name, data) or "(unavailable)", name, name)


def record_stop(store, name, by="orchestrator", reason="stopped by orchestrator", code=STOP_EXIT,
                state="stopped", partial="", activity=""):
    meta = store.read(name)
    names = changed_files(store, name, meta.get("dir", ""))
    fields = dict(state=state, exit=code, reason=reason, stopped_by=by, stopped_at=now(),
                  files=len(names), changed_names=" | ".join(names), delta_known=int(store.path(name, ".baseline.json").is_file()), session=store.session(name, meta))
    if partial:
        fields["partial_result"] = " ".join(partial[-1200:].split())
    if activity:
        fields["last_activity"] = " ".join(activity.split())[:500]
    data = store.update(name, **fields)
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
            with store.path(name, ".log").open("r", encoding="utf-8", errors="replace") as log:
                for chunk in iter(lambda: log.read(65536), ""):
                    out.write(chunk)
        except OSError:
            pass
        out.write("\n```\n")
    os.replace(str(temporary), str(path))
