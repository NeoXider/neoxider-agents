"""Explicit detached wave: each owned foreground task has one Python wrapper.

An OpenCode wave starts its tasks one at a time. The next child is launched only
after the previous task produced its FIRST actual provider activity of its own run
(a thinking/text/tool/read/command event from the provider, not the runtime's
process-tree heartbeat), never after a session id: ten OpenCode sessions created
in one burst made the OpenCode SQLite store answer "database is locked" while the
session was created, the model was set and the user message was written. The
barrier serializes STARTUP only — a task keeps running while the next one starts.
Other engines keep the plain burst launch.
"""
import json
import os
import re
import subprocess
import sys
import time
from threading import Event
from .cli import ROOT
from .process import hidden_kwargs, kill_tree, pid_stamp
from .lifecycle import preflight
from .state import atomic_write, read_meta, valid_name

OPENCODE_ENGINE = "opencode"
STARTUP_TIMEOUT = 180.0
MAX_STARTUP_TIMEOUT = 3600.0
# Digest kinds that prove the provider really worked on this run.
READY_KINDS = frozenset(("text", "thinking", "tool", "command", "read", "created", "edited"))
# The runtime heartbeat appends "[<engine>] activity: tool running (N helper processes)":
# a process-tree signal, not provider activity. It must not release the next launch.
HEARTBEAT_DETAIL = re.compile(r"^\[[^\]]+\]\s+activity:\s")
# Terminal states that mean "this task will never produce its first activity".
FAILED_STATES = frozenset(("error", "limited", "silent", "stopped"))
# Terminal states that mean the task is finished; advancing is safe.
COMPLETED_STATES = frozenset(("done", "waiting"))


def startup_timeout():
    """Bounded startup deadline; an invalid or non-positive override never disables the barrier."""
    raw = (os.environ.get("AGENT_FAN_STARTUP_TIMEOUT_SEC") or "").strip()
    if not raw:
        return STARTUP_TIMEOUT
    try:
        value = float(raw)
    except ValueError:
        value = 0.0
    if not (0.0 < value < float("inf")):
        print("[agent.sh] fan: ignoring AGENT_FAN_STARTUP_TIMEOUT_SEC=%r; using %ss" % (raw, STARTUP_TIMEOUT), file=sys.stderr)
        return STARTUP_TIMEOUT
    return min(value, MAX_STARTUP_TIMEOUT)


def activity_rows(store, name, since):
    """Digest rows of THIS run only.

    The lifecycle clears `.activity.jsonl` when a run starts, and every row
    carries the epoch time it was written, so rows of an older generation are
    ignored instead of being mistaken for fresh provider activity.
    """
    path = store.path(name, ".activity.jsonl")
    try:
        stat = path.stat()
    except OSError:
        return []
    if stat.st_mtime + 1.5 < since:
        return []  # Untouched since this task was launched: a previous generation's sidecar.
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        stamp = record.get("recorded_at")
        if isinstance(stamp, (int, float)) and stamp < since:
            continue
        rows.append(record)
    return rows


def ready_kind(row):
    """The digest kind of a row that proves real provider activity, else "".

    A "tool" row whose detail is the runtime's process-tree heartbeat
    ("[opencode] activity: tool running (N helper processes)") is NOT provider
    activity: the provider has not accepted the prompt yet. A real provider tool
    digest (a tool name and its arguments) is accepted.
    """
    kind = str(row.get("kind", ""))
    if kind not in READY_KINDS:
        return ""
    detail = str(row.get("detail", ""))
    if kind == "tool" and HEARTBEAT_DETAIL.match(detail):
        return ""
    return kind


def await_activity(store, record, timeout):
    """Wait until one launched task shows its first actual provider activity.

    Returns (ready, reason). `ready` is True when the task produced assistant or
    tool activity, or finished successfully before any activity. `ready` is False
    when the task failed/limited/stopped before its first activity, exited, or
    the bounded startup timeout expired — the wave must then stop scheduling.
    """
    name = record["name"]
    process = record["process"]
    since = record["since"]
    baseline = record["generation"]
    deadline = time.monotonic() + timeout
    while True:
        for row in activity_rows(store, name, since):
            kind = ready_kind(row)
            if kind:
                return True, "%s produced its first actual provider activity (%s)" % (name, kind)
        meta = read_meta(store.path(name, ".meta")) or {}
        generation = meta.get("generation", "")
        if generation and generation != baseline:
            state = meta.get("state", "")
            if state in FAILED_STATES:
                return False, "task %s reached state=%s before its first actual provider activity" % (name, state)
            if state in COMPLETED_STATES:
                return True, "task %s finished (state=%s) before its first actual provider activity" % (name, state)
        code = process.poll()
        if code is not None:
            return False, "task %s exited with code %s before its first actual provider activity" % (name, code)
        if time.monotonic() >= deadline:
            return False, "task %s produced no actual provider activity within %ss" % (name, timeout)
        Event().wait(0.1)


def fan(store, base, prompts, opts):
    if not prompts:
        raise ValueError("fan: needs >=1 prompt")
    defaults = dict(engine="claude", model="", effort="", dir=os.getcwd())
    defaults.update(opts)
    resolved, _provider, _model, _effort, _session = preflight(store, valid_name("%s-01" % base), defaults, False)
    engine = resolved.get("engine", defaults["engine"])
    barrier = engine == OPENCODE_ENGINE
    timeout = startup_timeout()
    names = []
    launched = []
    if barrier and len(prompts) > 1:
        print("[agent.sh] fan: opencode startup barrier on (first actual provider activity per task, %ss timeout)" % timeout, file=sys.stderr)
    for index, prompt in enumerate(prompts, 1):
        name = valid_name("%s-%02d" % (base, index))
        if barrier and index > 1:
            ready, reason = await_activity(store, launched[-1], timeout)
            if not ready:
                unlaunched = [valid_name("%s-%02d" % (base, number)) for number in range(index, len(prompts) + 1)]
                message = ("fan: opencode startup barrier stopped the wave: %s. launched: %s; not launched: %s. "
                           "Launched tasks keep running — inspect them with neoxider status NAME." %
                           (reason, ", ".join(names) or "none", ", ".join(unlaunched)))
                print("[agent.sh] " + message, file=sys.stderr)
                raise ValueError(message)
        argv = ["run", "-t", name]
        for key, flag in (("engine", "-e"), ("model", "-m"), ("effort", "-f"), ("dir", "-C"), ("parent", "-P")):
            if key in opts:
                argv += [flag, opts[key]]
        for flag in ("--no-progress", "--no-terse", "--verbose", "--terminal", "--progress", "--log", "--notify", "--strict-owns", "-v"):
            if opts.get(flag):
                argv.append(flag)
        if opts.get("owns"):
            argv += ["--owns", opts["owns"]]
        prompt_file = store.path(name, ".fan.prompt")
        atomic_write(prompt_file, prompt)
        argv += ["--prompt-file", str(prompt_file)]
        keep_output = opts.get("--log") or os.environ.get("AGENT_KEEP_LOGS") == "1"
        output = open(store.path(name, ".launcher.log"), "wb") if keep_output else open(os.devnull, "wb")
        since = time.time()
        generation = (read_meta(store.path(name, ".meta")) or {}).get("generation", "")
        try:
            process = subprocess.Popen([sys.executable, str(ROOT / "agent.py"), *argv], stdin=subprocess.DEVNULL,
                                       env={**os.environ, "AGENT_DETACHED": "1", "AGENT_LAUNCHER_PID": ""},
                                       stdout=output, stderr=subprocess.STDOUT, close_fds=True, **hidden_kwargs())
        finally:
            output.close()
        names.append(name)
        launched.append({"name": name, "process": process, "stamp": pid_stamp(process.pid),
                         "since": since, "generation": generation})
        print("[agent.sh] ⇉ fanned %s (pid %s)" % (name, process.pid), file=sys.stderr)
    deadline = time.monotonic() + 15
    for name, process, stamp in ((row["name"], row["process"], row["stamp"]) for row in launched):
        while store.read(name).get("pid") != str(process.pid):
            if process.poll() is not None:
                raise ValueError("fan: task %s failed before publishing its owner; use neoxider status %s (or --log to retain launcher output)" % (name, name))
            if time.monotonic() >= deadline:
                kill_tree(process.pid, stamp=stamp)
                raise ValueError("fan: task %s did not publish its owner within 15s" % name)
            Event().wait(0.05)
    print("[agent.sh] launched %s parallel task(s) under '%s'. Poll: agent.sh list" % (len(names), base), file=sys.stderr)
    print("[agent.sh] Start ONE tracked background job: neoxider wait %s; completion is its exit." % " ".join(names), file=sys.stderr)
    return 0
