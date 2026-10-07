"""Ownership checks and task reports; no subprocess polling or provider dependencies."""
import json
import os
import sys
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from threading import Event

from .reporting import changed_files, file_changes, owned_paths, read_baseline, snapshot, stop_block
from .state import Lock, last_output

ATTRIBUTION = ("Changes are measured against this task's start in its working directory; "
               "concurrent writers cannot be distinguished. Declared --owns limits reported paths; "
               "overlap warnings identify possible shared files.")
MAX_CHAR = 0x10FFFF
ACTIVE = ("running", "idle", "orphaned")


def _redact(text):
    from activity import redact
    return redact(str(text))


def record_history(store, name, kind, **fields):
    """Keep control metadata only: prompts, message text and secrets never enter history."""
    allowed = ("by", "reason", "exit", "state", "sequence", "queued", "now", "fresh", "session")
    entry = dict(kind=kind, at_epoch=time.time())
    entry.update({key: _redact(value) if isinstance(value, str) else value
                  for key, value in fields.items() if key in allowed})
    path = store.path(name, ".history.jsonl")
    with Lock(path):
        fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as out:
            out.write(json.dumps(entry, ensure_ascii=False) + "\n")


def owns_list(value):
    if isinstance(value, (list, tuple)):
        return [str(item).strip().replace("\\", "/") for item in value if str(item).strip()]
    return [item.strip().replace("\\", "/") for item in str(value or "").split(",") if item.strip()]


def _absolute_pattern(directory, pattern):
    value = os.path.normpath(os.path.join(str(Path(directory).resolve()), pattern)).replace("\\", "/")
    return value.casefold() if os.name == "nt" else value


def _class_intervals(body):
    negated = body.startswith(("!", "^"))
    if negated:
        body = body[1:]
    intervals = []
    position = 0
    while position < len(body):
        if position + 2 < len(body) and body[position + 1] == "-":
            lower, upper = ord(body[position]), ord(body[position + 2])
            if lower <= upper:
                intervals.append((lower, upper))
            position += 3
        else:
            intervals.append((ord(body[position]), ord(body[position])))
            position += 1
    intervals.sort()
    merged = []
    for lower, upper in intervals:
        if merged and lower <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(upper, merged[-1][1]))
        else:
            merged.append((lower, upper))
    if not negated:
        return merged
    result, start = [], 1
    for lower, upper in merged:
        if start < lower:
            result.append((start, lower - 1))
        start = max(start, upper + 1)
    if start <= MAX_CHAR:
        result.append((start, MAX_CHAR))
    return result


def _glob_tokens(pattern):
    result = []
    index = 0
    while index < len(pattern):
        character = pattern[index]
        if character == "*":
            if not result or result[-1] != "*":
                result.append("*")
        elif character == "?":
            result.append([(1, MAX_CHAR)])
        elif character == "[":
            end = index + 1
            if end < len(pattern) and pattern[end] == "!":
                end += 1
            if end < len(pattern) and pattern[end] == "]":
                end += 1
            end = pattern.find("]", end)
            if end >= 0:
                result.append(_class_intervals(pattern[index + 1:end]))
                index = end
            else:
                result.append([(ord(character), ord(character))])
        else:
            result.append([(ord(character), ord(character))])
        index += 1
    return result


def globs_overlap(first, second):
    """Decide whether two fnmatch-style glob languages share a possible path."""
    left, right = _glob_tokens(first), _glob_tokens(second)
    pending, visited = deque([(0, 0)]), set()
    while pending:
        i, j = pending.popleft()
        if (i, j) in visited:
            continue
        visited.add((i, j))
        if i == len(left) and j == len(right):
            return True
        if i < len(left) and left[i] == "*":
            pending.append((i + 1, j))
        if j < len(right) and right[j] == "*":
            pending.append((i, j + 1))
        if i == len(left) or j == len(right):
            continue
        a = [(1, MAX_CHAR)] if left[i] == "*" else left[i]
        b = [(1, MAX_CHAR)] if right[j] == "*" else right[j]
        if any(max(lo, low) <= min(hi, high) for lo, hi in a for low, high in b):
            pending.append((i if left[i] == "*" else i + 1, j if right[j] == "*" else j + 1))
    return False


def guard_ownership(store, name, directory, owns, strict=False):
    """Caller holds the shared ownership lock through guard and task publication."""
    from .lifecycle import effective
    patterns = owns_list(owns)
    if any(len(pattern) > 512 for pattern in patterns):
        raise ValueError("--owns glob is too long; use shorter directory globs")
    warnings = []
    for other, meta, _ in store.scan():
        if other == name or effective(store, other, meta) not in ACTIVE:
            continue
        matches = []
        for pattern in patterns:
            left = _absolute_pattern(directory, pattern)
            for foreign in owns_list(meta.get("owns", "")):
                if globs_overlap(left, _absolute_pattern(meta.get("dir", directory), foreign)):
                    matches.append("%s intersects %s" % (pattern, foreign))
        if matches:
            warnings.append("ownership overlap with running task '%s': %s; choose disjoint --owns or wait/stop %s" %
                            (other, "; ".join(sorted(set(matches))), other))
    if warnings and strict:
        raise ValueError("--strict-owns refused: " + " | ".join(warnings))
    for warning in warnings:
        print("[neoxider] warning: " + warning, file=sys.stderr)
    return warnings


def _epoch(meta, key, fallback=0):
    try:
        return float(meta.get(key) or fallback)
    except (TypeError, ValueError):
        return float(fallback)


def _start(meta, fallback=0):
    stamp = _epoch(meta, "task_started_epoch") or _epoch(meta, "started_epoch")
    if not stamp:
        try:
            stamp = datetime.strptime(meta.get("started", ""), "%Y-%m-%d %H:%M:%S").timestamp()
        except ValueError:
            stamp = fallback
    return stamp


def _number(value):
    try:
        number = float(value)
        return int(number) if number.is_integer() else number
    except (TypeError, ValueError):
        return None


def _usage(meta):
    data = {}
    for key in ("usage", "usage_json"):
        try:
            parsed = json.loads(meta.get(key, "{}"))
            if isinstance(parsed, dict):
                data.update(parsed)
        except (ValueError, TypeError):
            pass
    for key in ("input_tokens", "output_tokens", "total_tokens", "tokens", "cache_read_tokens", "cache_write_tokens"):
        value = _number(meta.get(key))
        if value is not None:
            data[key] = value
    total = data.get("total_tokens", data.get("tokens"))
    if total is None and isinstance(data.get("input_tokens"), (int, float)) and isinstance(data.get("output_tokens"), (int, float)):
        total = data["input_tokens"] + data["output_tokens"]
    cost = _number(meta.get("cost", meta.get("total_cost_usd", meta.get("cost_usd"))))
    if cost is None:
        cost = _number(data.get("cost", data.get("total_cost_usd")))
    return data or None, total, cost


def _delta_names(store, name, meta, current=None):
    if current is not None:
        from .reporting import _different, _delta_keys
        baseline = read_baseline(store, name)
        if not baseline:
            return []
        initial = baseline.get("files", baseline)
        names = [path for path in _delta_keys(baseline, initial, current) if _different(initial.get(path), current.get(path))]
        return owned_paths(store, name, meta.get("dir", ""), names)
    if meta.get("changed_names") is not None and meta.get("state") not in ACTIVE:
        return [path for path in meta.get("changed_names", "").split(" | ") if path]
    return changed_files(store, name, meta.get("dir", ""))


def dashboard_data(store):
    from .lifecycle import effective
    from .views import mine
    rows, scans = [], {}
    now = time.time()
    for name, meta, stamp in store.scan():
        if not mine(meta):
            continue
        state = effective(store, name, meta)
        directory = meta.get("dir", "")
        current = None
        if state in ACTIVE and directory and Path(directory).is_dir():
            normalized = str(Path(directory).resolve())
            if normalized not in scans:
                scans[normalized] = snapshot(directory, (store.root,))
            current = scans[normalized]
        unreconciled = state not in ACTIVE and meta.get("state") in ACTIVE
        names = [path for path in meta.get("changed_names", "").split(" | ") if path] if unreconciled else _delta_names(store, name, meta, current)
        from .reporting import partial_warning
        baseline = read_baseline(store, name)
        warning = partial_warning(baseline, current) or meta.get("baseline_warning", "")
        activity_epoch = _epoch(meta, "last_activity_epoch") or _epoch(meta, "activity_epoch")
        if not activity_epoch:
            try:
                activity_epoch = store.path(name, ".log").stat().st_mtime
            except OSError:
                activity_epoch = stamp
        usage, tokens, cost = _usage(meta)
        rows.append(dict(name=name, state=state, engine=meta.get("engine", ""), model=meta.get("model", ""),
                         age_sec=max(0, round(now - _start(meta, stamp), 1)),
                         last_activity=_redact(meta.get("last_activity", ""))[:160],
                         activity_age_sec=max(0, round(now - activity_epoch, 1)),
                         files_changed=len(names) if meta.get("delta_known") != "0" and not unreconciled else None,
                         baseline_partial=bool(baseline.get("partial")), baseline_warning=warning,
                         queued=len(store.inbox(name)), usage=usage, tokens=tokens, cost_usd=cost))
    return rows


def _age(seconds):
    return "%ss" % int(seconds) if seconds < 60 else "%sm" % int(seconds / 60) if seconds < 3600 else "%.1fh" % (seconds / 3600)


def top(store, opts=None):
    opts = opts or {}
    once = opts.get("--once") or opts.get("--json") or not sys.stdout.isatty()
    try:
        interval = float(opts.get("interval", opts.get("--interval", 2)))
    except (ValueError, TypeError):
        raise ValueError("top: --interval must be a positive number")
    if interval <= 0:
        raise ValueError("top: --interval must be a positive number")
    sleeper = Event()
    try:
        while True:
            rows = dashboard_data(store)
            if opts.get("--json"):
                print(json.dumps(rows, ensure_ascii=False))
            else:
                if not once:
                    print("\033[2J\033[H", end="")
                print("%-24s %-9s %-6s %-8s %-6s %-5s %-9s %-9s %s" %
                      ("TASK", "STATE", "AGE", "ACTIVITY", "FILES", "QUEUE", "TOKENS", "COST USD", "LAST ACTIVITY"))
                for row in rows:
                    print("%-24s %-9s %-6s %-8s %-6s %-5s %-9s %-9s %s" %
                          (row["name"], row["state"], _age(row["age_sec"]), _age(row["activity_age_sec"]),
                           row["files_changed"] if row["files_changed"] is not None else "?", row["queued"],
                           row["tokens"] if row["tokens"] is not None else "-",
                           "$%.4f" % row["cost_usd"] if row["cost_usd"] is not None else "-", row["last_activity"]))
                    if row["baseline_warning"]:
                        print("[neoxider] %s: %s" % (row["name"], row["baseline_warning"]))
                if not rows:
                    print("No tasks for this orchestrator.")
                if not once:
                    print("Refreshing every %ss; Ctrl+C to exit." % interval, flush=True)
            if once:
                return 0
            sleeper.wait(interval)
    except KeyboardInterrupt:
        return 0


def overlaps(store, name, names):
    """Report possible shared writers, rather than assert unobservable authorship."""
    if not names:
        return []
    from .lifecycle import effective
    data = store.read(name)
    directory = data.get("dir", "")
    if not directory:
        return []
    ours = {_absolute_pattern(directory, path) for path in names}
    result = []
    ours_start, ours_end = _start(data), _epoch(data, "finished_epoch", time.time())
    for other, meta, _ in store.scan():
        if other == name or not meta.get("dir"):
            continue
        other_start, other_end = _start(meta), _epoch(meta, "finished_epoch", time.time())
        if ours_end < other_start or other_end < ours_start:
            continue
        state = effective(store, other, meta)
        foreign = _delta_names(store, other, meta)
        shared = ours & {_absolute_pattern(meta["dir"], path) for path in foreign}
        if shared:
            relative = [path for path in names if _absolute_pattern(directory, path) in shared]
            result.append(dict(task=other, state=state, files=sorted(relative)))
    return result


def diff(store, name, opts=None):
    opts = opts or {}
    meta = store.read(name)
    if not meta:
        raise ValueError("diff: no such task '%s'; use neoxider list" % name)
    if not read_baseline(store, name):
        raise ValueError("diff: task '%s' has no start baseline; start a new task to track changes" % name)
    meta, state = _explicit_state(store, name, meta)
    changes = _explicit_changes(store, name, meta, state)
    warning = store.read(name).get("baseline_warning", "")
    if warning:
        print("[neoxider] " + warning, file=sys.stderr)
    names = [entry["path"] for entry in changes]
    for conflict in overlaps(store, name, names):
        print("[neoxider] warning: task '%s' also changed %s during overlapping task lifetimes; authorship is ambiguous" %
              (conflict["task"], ", ".join(conflict["files"])), file=sys.stderr)
    if opts.get("--names"):
        for path in names:
            print(path)
    elif opts.get("--stat"):
        for entry in changes:
            lines = "line counts unavailable" if entry["insertions"] is None else "+%s -%s" % (entry["insertions"], entry["deletions"])
            print("%-9s %s | %s" % (entry["status"], entry["path"], lines))
        print("%s file(s) changed" % len(changes))
    else:
        import difflib
        from .reporting import TEXT_LIMIT
        baseline = read_baseline(store, name)
        initial = baseline.get("files", baseline)
        print(ATTRIBUTION)
        for entry in changes:
            old, new = "" if entry["status"] == "added" else None, "" if entry["status"] == "deleted" else None
            before = initial.get(entry["path"], {})
            try:
                if isinstance(before, dict) and before.get("content"):
                    old = (store.path(name, ".baseline.files") / before["content"]).read_bytes().decode("utf-8")
                path = Path(meta["dir"]) / entry["path"]
                if entry["status"] != "deleted" and not path.is_symlink() and path.stat().st_size <= TEXT_LIMIT:
                    import hashlib
                    raw = path.read_bytes()
                    if b"\0" not in raw and len(raw) <= TEXT_LIMIT and hashlib.sha256(raw).hexdigest() == entry.get("after_sha256"):
                        new = raw.decode("utf-8")
            except (OSError, UnicodeError):
                pass
            if old is None or new is None:
                print("%s %s (text patch unavailable)" % (entry["status"], entry["path"]))
                continue
            patch = difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                                         fromfile="a/" + entry["path"], tofile="b/" + entry["path"])
            for line in patch:
                print(line, end="" if line.endswith("\n") else "\n")
        print("%s file(s) changed" % len(changes))
    return 0


def _explicit_state(store, name, meta):
    from .lifecycle import effective
    state = effective(store, name, meta)
    if state == "stopped" and meta.get("state") == "running":
        from .reporting import record_stop
        record_stop(store, name, by="user", reason="launcher stopped")
        meta = store.read(name)
    return meta, state


def _explicit_changes(store, name, meta, state):
    settled = state not in ACTIVE
    cache = store.path(name, ".changes.json")
    return file_changes(store, name, meta.get("dir", ""), freeze=settled and not cache.is_file(), frozen=settled)


def collect_result(store, name):
    meta = store.read(name)
    if not meta:
        raise ValueError("result: no such task '%s'; use neoxider list" % name)
    meta, state = _explicit_state(store, name, meta)
    try:
        answer = store.path(name, ".answer").read_text(encoding="utf-8")
    except OSError:
        answer = last_output(store.path(name, ".log"))
    stopped = stop_block(store, name, meta) if state == "stopped" else None
    changes = _explicit_changes(store, name, meta, state)
    baseline = read_baseline(store, name)
    warning = store.read(name).get("baseline_warning", "")
    start = _start(meta)
    end = _epoch(meta, "finished_epoch") or _epoch(meta, "ended_epoch")
    if not end and state not in ACTIVE:
        try:
            end = datetime.strptime(meta.get("stopped_at", ""), "%Y-%m-%d %H:%M:%S").timestamp()
        except ValueError:
            end = store.path(name, ".meta").stat().st_mtime
    usage, tokens, cost = _usage(meta)
    history = []
    try:
        with store.path(name, ".history.jsonl").open(encoding="utf-8") as source:
            for line in source:
                try:
                    entry = json.loads(line)
                    if isinstance(entry, dict):
                        history.append(entry)
                except ValueError:
                    pass
    except OSError:
        pass
    return dict(task=name, state=state, exit_code=_number(meta.get("exit")), final_answer=answer, stop_report=stopped,
                engine=meta.get("engine", ""), model=meta.get("model", ""), session=store.session(name, meta),
                directory=meta.get("dir", ""), changed_files=changes, baseline_known=bool(read_baseline(store, name)),
                baseline_partial=bool(baseline.get("partial")), baseline_warning=warning,
                duration_sec=max(0, round((end or time.time()) - start, 3)) if start else None,
                usage=usage, tokens=tokens, cost_usd=cost, history=history,
                overlaps=overlaps(store, name, [entry["path"] for entry in changes]), attribution=ATTRIBUTION)


def result(store, name, opts=None):
    opts = opts or {}
    data = collect_result(store, name)
    if opts.get("--json"):
        print(json.dumps(data, ensure_ascii=False))
    else:
        if data.get("baseline_warning"):
            print("[neoxider] " + data["baseline_warning"])
        print("task=%s state=%s exit=%s duration=%ss files=%s" %
              (name, data["state"], data["exit_code"], data["duration_sec"], len(data["changed_files"])))
        answer = data["stop_report"] or data["final_answer"]
        print(answer, end="" if answer.endswith("\n") else "\n")
        if data["overlaps"]:
            print("warning: shared files with " + ", ".join(entry["task"] for entry in data["overlaps"]), file=sys.stderr)
    store.seen(name)
    return 0
