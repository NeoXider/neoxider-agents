"""Fast task views and wait: no subprocess polling and bounded transcript reads."""
import os
import sys
import time
import uuid
from pathlib import Path
from threading import Event
from .state import Lock, MARK, last_output, log_offset, output_chunks, tail, valid_name
from .lifecycle import effective
from .reporting import stop_block
from .providers import limit_line, parse_limit_reset

ICONS = {"running": "▶", "idle": "▷", "done": "✔", "waiting": "⏳", "error": "✖", "stalled": "⚠", "limited": "⛔", "silent": "◌", "stopped": "⏹", "orphaned": "⚠"}


def activity_text(store, name, meta):
    kind = meta.get("last_activity", "")
    if kind:
        return "last activity=" + kind[:160]
    from activity import summary
    latest = summary(store.path(name, ".log"))
    return "last activity=%s (%ss ago)" % (latest["kind"], latest["age_sec"]) if latest else ""


def task_list(store, limit=20):
    rows = store.scan()
    if limit:
        rows = rows[:limit]
    print("%-2s %-24s %-8s %-9s %-13s %-6s %-6s %-8s %s" % ("", "TASK", "STATE", "ENGINE", "MODEL", "AGE", "FILES", "SESSION", "DETAIL"))
    for name, meta, stamp in rows:
        state = effective(store, name, meta)
        detail = meta.get("reason", "")
        if state == "orphaned":
            detail = "orphaned provider alive, launcher dead; stop " + name
        if state == "limited":
            resets = parse_limit_reset(meta.get("reason", ""))
            detail = ('resets="%s"' % resets) + ("; " + detail if detail else "")
        queue = len(store.inbox(name))
        detail += ("; " if detail else "") + "%s undelivered message(s); %s" % (queue, activity_text(store, name, meta))
        print("%-2s %-24s %-8s %-9s %-13s %-6s %-6s %-8s %s" % (
            ICONS.get(state, "•"), name, state, meta.get("engine", "?"), meta.get("model", "?"),
            "%sm" % max(0, int((time.time() - stamp) / 60)), meta.get("files") or "0", meta.get("session", "")[:8], detail))
    return 0


def status(store, name):
    meta = store.read(name)
    if not meta:
        raise ValueError("no such task: " + name)
    state = effective(store, name, meta)
    label = state
    if state == "idle":
        try:
            age = int((time.time() - store.path(name, ".log").stat().st_mtime) / 60)
        except OSError:
            age = 0
        label = "running (no output for %sm)" % age
    print("%s task=%s  state=%s  engine=%s/%s  exit=%s  files=%s" % (
        ICONS.get(state, "•"), name, label, meta.get("engine", ""), meta.get("model", ""), meta.get("exit") or "–", meta.get("files") or "0"))
    print("   dir=" + meta.get("dir", ""))
    print("   session=" + (store.session(name, meta) or "–"))
    print("   %s undelivered message(s); %s" % (len(store.inbox(name)), activity_text(store, name, meta)))
    print("   started=%s  md=%s" % (meta.get("started", ""), store.path(name, ".md")))
    if state == "waiting":
        print('   → needs a REPLY: agent.sh reply %s "..."' % name)
    if state == "orphaned":
        print("   ⚠ orphaned provider alive, launcher dead; neoxider stop " + name)
    if state in ("error", "limited", "silent", "stopped", "stalled"):
        reason = meta.get("reason", "launcher stopped")
        if state == "limited":
            reason += '  resets="%s"' % parse_limit_reset(meta.get("reason", ""))
        print("   reason=" + reason)
    if meta.get("timeout"):
        print("   ⏱ killed by the step watchdog after %ss (AGENT_TIMEOUT_SEC)" % meta["timeout"])
    if state == "stopped":
        print(stop_block(store, name), end="")
    print("   --- current step (last lines) ---")
    from .logs import answer_text
    for line in [line for line in answer_text(store, name).splitlines() if line.strip()][-4:]:
        print("   " + line)
    return 0


def last(store, name, result=False):
    path = store.path(name, ".log")
    if not store.read(name):
        raise ValueError("no such task: " + name)
    state = effective(store, name)
    meta = store.read(name)
    if state in ("stopped", "limited") or (result and store.path(name, ".stop").is_file()):
        print(stop_block(store, name, meta), end="")
        if state == "limited":
            print(limit_line(name, meta.get("engine", ""), meta.get("model", ""), meta.get("reason", "")))
    else:
        from .logs import print_answer
        print_answer(store, name)
    store.seen(name)
    return 0


def mine(meta):
    parent = os.environ.get("AGENT_PARENT")
    orchestrator = os.environ.get("AGENT_ORCHESTRATOR_ID")
    return meta.get("parent") == parent if parent else meta.get("orchestrator") == orchestrator if orchestrator else True


def pending(store, strict=False):
    count = 0
    cutoff = time.time() - float(os.environ.get("AGENT_PENDING_HOURS", 24)) * 3600
    for name, meta, stamp in store.scan():
        if not mine(meta):
            continue
        state = effective(store, name, meta)
        if state in ("running", "idle"):
            continue
        queue = len(store.inbox(name))
        try:
            seen = store.path(name, ".seen").stat().st_mtime
        except OSError:
            seen = 0
        if not queue and state != "stopped" and (stamp < cutoff or stamp <= seen):
            continue
        count += 1
        print("%s unread  %s  state=%s  %s undelivered message(s)  -> agent.sh last %s" % (ICONS.get(state, "•"), name, state, queue, name))
        if state == "stopped":
            print(stop_block(store, name), end="")
    if not count:
        print("[agent.sh] pending: no unread results")
    return 3 if count and strict else 0


def wait(store, names, timeout=0, poll=5, strict=False):
    if not names:
        names = [name for name, meta, _ in store.scan() if mine(meta) and effective(store, name, meta) in ("running", "idle")]
    for name in names:
        valid_name(name)
    if not names:
        print("[agent.sh] wait: no tasks to watch — nothing to do")
        return 0
    from .process import pid_stamp
    token = uuid.uuid4().hex
    watched = []
    for name in names:
        meta = store.read(name)
        if not (meta.get("core_version") and meta.get("detached") == "1" and meta.get("state") == "running"):
            continue
        with Lock(store.path(name, ".waiter")):
            meta = store.read(name)
            if meta.get("core_version") and meta.get("detached") == "1" and meta.get("state") == "running":
                store.update(name, wait_pid=os.getpid(), wait_start=pid_stamp(os.getpid()), wait_token=token)
                watched.append(name)
    try:
        return _wait(store, names, timeout, poll, strict)
    except BaseException:
        from .lifecycle import stop
        for name in watched:
            if store.read(name).get("wait_token") == token:
                try:
                    stop(store, name, "user", "launcher stopped")
                except (OSError, ValueError):
                    pass
        raise
    finally:
        for name in watched:
            try:
                with Lock(store.path(name, ".waiter")):
                    if store.read(name).get("wait_token") == token:
                        store.update(name, wait_pid="", wait_start="", wait_token="")
            except (OSError, ValueError):
                pass


def _wait(store, names, timeout, poll, strict=False):
    from .runtime import pipe_closed
    started = time.monotonic()
    settled = set()
    code = 0
    sleeper = Event()
    while len(settled) < len(names):
        if pipe_closed():
            from .lifecycle import stop
            for name in names:
                if store.read(name).get("wait_pid") == str(os.getpid()):
                    stop(store, name, "user", "launcher stopped")
            return 130
        for name in names:
            if name in settled:
                continue
            meta = store.read(name)
            state = effective(store, name, meta)
            if state not in ("running", "idle"):
                if state == "stopped":
                    code = 130
                settled.add(name)
                print("[agent.sh] %s settled: %s -> %s (exit=%s, files=%s)" % (
                    ICONS.get(state, "•"), name, state, meta.get("exit", ""), meta.get("files", "")), file=sys.stderr)
        if len(settled) == len(names):
            break
        if timeout and time.monotonic() - started >= timeout:
            code = 2
            print("[agent.sh] wait: timeout after %ss; still running: %s" % (timeout, " ".join(name for name in names if name not in settled)), file=sys.stderr)
            break
        sleeper.wait(poll)
    for name in names:
        state = effective(store, name)
        print("\n========== wait | %s | %s ==========" % (name, state))
        if store.read(name):
            last(store, name, result=True)
        else:
            print("(no log for %s)" % name)
    settled_states = [effective(store, name) for name in names]
    ok = sum(1 for state in settled_states if state in ("done", "waiting"))
    limited = sum(1 for state in settled_states if state == "limited")
    failed = len(names) - ok - limited
    if strict and code == 0 and (limited + failed) > 0:
        code = 3
    print("WAIT_DONE tasks=%s rc=%s ok=%s limited=%s failed=%s" % (len(names), code, ok, limited, failed))
    return code


def log(store, name, opts):
    path = store.path(name, ".log")
    if not path.is_file():
        print("[neoxider] raw log expired/cleaned; neoxider peek %s / last %s" % (name, name))
        return 0
    offset = log_offset(path, int(opts.get("-n", 0)), bool(opts.get("-l")))
    with path.open(encoding="utf-8", errors="replace", newline="") as stream:
        stream.seek(offset)
        for chunk in iter(lambda: stream.read(65536), ""):
            print(chunk, end="")
    if opts.get("-f"):
        offset = path.stat().st_size
        while effective(store, name) in ("running", "idle"):
            Event().wait(0.2)
            if not path.exists():
                break
            if path.stat().st_size < offset:
                offset = 0
            with path.open("rb") as stream:
                stream.seek(offset)
                chunk = stream.read(65536)
                offset = stream.tell()
            print(chunk.decode("utf-8", "replace"), end="", flush=True)
    return 0


def clean(store, opts):
    count = 0
    failures = []
    dry = bool(opts.get("-n") or opts.get("--dry-run"))
    for name, meta, _ in store.scan():
        state = effective(store, name, meta)
        if state in ("running", "idle", "orphaned") or (state == "waiting" and not opts.get("--all")):
            continue
        if store.inbox(name) and not (opts.get("--all") or opts.get("--purge")):
            continue
        paths = [store.path(name, ".md"), store.path(name, ".launcher.log")]
        from .logs import prune_task
        before = len(failures)
        if prune_task(store, name, meta, force=True, dry=dry, errors=failures):
            print("[agent.sh] %s %s" % ("would remove" if dry else "remove", store.path(name, ".log")))
            count += 1
        if len(failures) > before and opts.get("--purge"):
            # Preserve the task and its only answer when migration/removal failed.
            continue
        if meta.get("dir") and (meta.get("progress") == "1" or not meta.get("core_version")):
            paths.append(Path(meta["dir"]) / ("PROGRESS.%s.md" % name))
        if opts.get("--purge"):
            try:
                paths += [Path(p.path) for p in os.scandir(str(store.root))
                          if p.name.startswith(name + ".") and p.is_file() and p.name != name + ".log"]
                box = store.path(name, ".inbox")
                try:
                    if box.is_dir():
                        paths += list(box.iterdir()) + [box]
                except OSError as error:
                    failures.append((box, error))
                baseline = store.path(name, ".baseline.files")
                allowed_root = store.root.resolve()
                resolved_baseline = baseline.resolve()
                try:
                    if baseline.is_dir() and not baseline.is_symlink() and allowed_root in resolved_baseline.parents:
                        paths += sorted(baseline.rglob("*"), key=lambda p: len(p.parts), reverse=True) + [baseline]
                except OSError as error:
                    failures.append((baseline, error))
            except OSError as error:
                failures.append((store.path(name, ".meta"), error))
                continue
        for path in dict.fromkeys(paths):
            try:
                path.stat()
                resolved = path.resolve()
                allowed = store.root.resolve()
                project = Path(meta["dir"]).resolve() if meta.get("dir") else None
                project_progress = project and path.name == "PROGRESS.%s.md" % name and resolved.parent == project
                if resolved != allowed and allowed not in resolved.parents and not project_progress:
                    print("[neoxider] skip cleanup path outside task state: " + str(path))
                    continue
                print("[agent.sh] %s %s" % ("would remove" if dry else "remove", path))
                if not dry:
                    path.rmdir() if path.is_dir() else path.unlink()
                count += 1
            except FileNotFoundError:
                continue
            except OSError as error:
                failures.append((path, error))
    print("[agent.sh] clean: %s file(s)" % count)
    for path, error in failures:
        print("[neoxider] could not remove %s: %s; retry clean" % (path, error), file=sys.stderr)
    if failures:
        print("[neoxider] clean: %s file(s) could not be removed" % len(failures), file=sys.stderr)
    return 1 if failures else 0
