"""Revive settled failed tasks in their SAVED session through the ordinary `send` path.

revive never resumes by itself: for each eligible task it launches one detached
`agent.py send NAME --prompt-file F`, so lifecycle.send -> preflight(resume=True)
takes the session from store.session(NAME) exactly as a manual follow-up would.
Safety gates: only error/stopped (opt-in silent/limited) tasks, quiet for --stale
seconds but within --within (unless named), not revived within --cooldown, at most
--max-attempts revives since the last run and --limit per sweep, never a deliberate
stop, never an unreset provider limit (without --force), never an unscoped bulk sweep.
"""
import json
import os
import subprocess
import sys
import time
from threading import Event

REVIVABLE = ("error", "stopped", "silent", "limited")
DEFAULT_STATES = ("error", "stopped")
DEFAULT_STALE = 600
DEFAULT_COOLDOWN = 300
DEFAULT_ATTEMPTS = 3
DEFAULT_WITHIN = 6 * 3600
DEFAULT_LIMIT = 10
PUBLISH_SECONDS = 30
HUMAN_CATEGORIES = ("auth", "model_unavailable")
PROMPT = ("[neoxider revive] The previous turn ended (state=%s, exit=%s%s) before the assignment finished. "
          "Continue the same assignment in this session: first check the working tree (git status/diff) "
          "for partial edits, then continue from where you stopped; do not redo finished work.")


def seconds(value, label, default):
    """SECONDS as a plain number, or a duration such as 90s, 10m, 1h."""
    if value is None:
        return float(default)
    text = str(value).strip()
    try:
        number = float(text)
    except ValueError:
        from .selection import parse_duration
        return parse_duration(text, label)
    if number < 0:
        raise ValueError(label + " must be a non-negative number of seconds")
    return number


def history(store, name):
    try:
        with store.path(name, ".history.jsonl").open(encoding="utf-8") as source:
            rows = []
            for line in source:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if isinstance(entry, dict):
                    rows.append(entry)
            return rows
    except OSError:
        return []


def stop_intent(store, name, meta, entries=None):
    """('crash'|'deliberate'|'ambiguous', detail) for a stopped task, from the record after its last turn start."""
    entries = history(store, name) if entries is None else entries
    last = None
    for entry in entries:
        kind = entry.get("kind")
        if kind == "stop" or kind in ("run", "restart", "revive") or (kind == "send" and not entry.get("queued")):
            last = entry
    if last is None:
        if not meta.get("stopped_by"):
            return "ambiguous", "no stop record (legacy task)"
        last = dict(kind="stop", by=meta.get("stopped_by"), reason=meta.get("reason", ""))
    if last.get("kind") != "stop":
        return "crash", "launcher died without a stop record"
    by, reason = str(last.get("by") or ""), str(last.get("reason") or "")
    if by == "watchdog":
        return "crash", "stopped by the watchdog: " + reason
    if by == "user" and reason.startswith("launcher stopped"):
        return "ambiguous", "launcher stopped (Ctrl+C or the parent session ended)"
    return "deliberate", "stopped by %s: %s" % (by or "orchestrator", reason or "no reason")


def streak(entries):
    """Revives since the last run/restart or manual send; the revive's own send does not reset it."""
    count, pending = 0, False
    for entry in entries:
        kind = entry.get("kind")
        if kind == "revive":
            count, pending = count + 1, True
        elif kind in ("run", "restart"):
            count, pending = 0, False
        elif kind == "send":
            if pending:
                pending = False
            else:
                count = 0
    return count


def quiet_seconds(store, name, meta, now):
    from .ux_tracking import _epoch
    epoch = max(_epoch(meta, "last_activity_epoch"), _epoch(meta, "activity_epoch"), _epoch(meta, "finished_epoch"))
    if not epoch:
        try:
            epoch = store.path(name, ".log").stat().st_mtime
        except OSError:
            epoch = now
    return max(0.0, now - epoch)


def assess(store, name, meta, options, explicit, now):
    """One report row; row['action'] == 'revive' marks a candidate, anything else is the skip reason."""
    from .lifecycle import effective
    from .views import failure_cause
    state = effective(store, name, meta)
    session = store.session(name, meta)
    age = quiet_seconds(store, name, meta, now)
    row = dict(name=name, state_before=state, state_after=state, session_id=session, session_after="",
               resumed=False, action="", error=None, cause=failure_cause(store, name, meta, state),
               activity_age_sec=round(age, 1), engine=meta.get("engine", ""))
    force = options["force"]
    if state not in options["states"]:
        row["action"] = ("skip: %s is never revived" % state) if state not in REVIVABLE else (
            "skip: %s not selected; add --state %s" % (state, state))
        return row
    try:
        from .providers import get_provider
        provider = get_provider(meta.get("engine") or "claude")
    except (OSError, ValueError, RuntimeError) as error:
        row["action"] = "skip: engine unavailable"
        row["error"] = " ".join(str(error).split())
        return row
    if not provider.supports_resume:
        row["action"] = "skip: engine %s cannot resume a session; use restart --fresh" % provider.engine
        return row
    if not session:
        row["action"] = "skip: no saved session id; use restart --fresh"
        return row
    if age <= options["stale"]:
        row["action"] = "skip: active %ds ago (--stale %ds)" % (age, options["stale"])
        return row
    try:
        last = float(meta.get("revive_epoch") or 0)
    except ValueError:
        last = 0
    if last and now - last < options["cooldown"]:
        row["action"] = "skip: revived %ds ago (--cooldown %ds)" % (now - last, options["cooldown"])
        return row
    entries = history(store, name)
    attempts = streak(entries)
    if options["attempts"] and attempts >= options["attempts"]:
        row["action"] = "skip: %d revive(s) since the last run (--max-attempts %d); inspect, then send manually" % (
            attempts, options["attempts"])
        return row
    if state == "stopped":
        intent, detail = stop_intent(store, name, meta, entries)
        if intent == "deliberate" and not (force and explicit):
            row["action"] = "skip: %s; deliberate stop, revive NAME --force to override" % detail
            return row
        if intent == "ambiguous" and not force:
            row["action"] = "skip: %s; intent unknown, confirm with --force" % detail
            return row
    try:
        failure = json.loads(meta.get("failure_json") or "{}")
        # A heuristic category is raw-output noise, not evidence that a human must intervene.
        category = failure.get("category", "") if failure.get("confidence", "explicit") == "explicit" else ""
    except (ValueError, AttributeError):
        category = ""
    if category in HUMAN_CATEGORIES and not force:
        row["action"] = "skip: %s failure needs a human; fix it, then --force" % category
        return row
    from .providers import known_limits
    limit = known_limits(store.root, now).get(provider.engine)
    if limit and not force:
        row["action"] = "skip: %s usage limit not reset (resets %s); see neoxider doctor" % (
            provider.engine, limit.get("resets") or "unknown")
        return row
    row["action"] = "revive"
    return row


def launch(store, name, row, options):
    """Resume through `send NAME --prompt-file F` in a detached owner; returns the updated row."""
    from .cli import ROOT
    from .lifecycle import effective
    from .process import hidden_kwargs, kill_tree, pid_stamp
    from .state import Lock, atomic_write
    from .ux_tracking import record_history
    try:
        with Lock(store.path(name, ".revive"), timeout=0):
            meta = store.read(name)
            state = effective(store, name, meta)
            if state != row["state_before"]:
                row.update(action="skip: state changed to %s" % state, state_after=state)
                return row
            try:
                last = float(meta.get("revive_epoch") or 0)
            except ValueError:
                last = 0
            if last and time.time() - last < options["cooldown"]:
                row["action"] = "skip: revived concurrently"
                return row
            store.update(name, revive_epoch=time.time(), revive_count=int(meta.get("revive_count") or 0) + 1)
            record_history(store, name, "revive", by="revive", state=state, reason=row["cause"] or "", session=row["session_id"])
            reason = "; ".join(item for item in (row["cause"], " ".join((meta.get("reason") or "").split())[:200]) if item)
            text = options.get("message") or PROMPT % (state, meta.get("exit") or "-", "; " + reason if reason else "")
            prompt = store.path(name, ".revive.prompt")
            atomic_write(prompt, text)
            env = dict(os.environ, AGENT_DETACHED="1", AGENT_LAUNCHER_PID="")
            for key, field in (("AGENT_PARENT", "parent"), ("AGENT_ORCHESTRATOR_ID", "orchestrator")):
                if meta.get(field):
                    env[key] = meta[field]
                else:
                    env.pop(key, None)
            keep = os.environ.get("AGENT_KEEP_LOGS") == "1" or meta.get("keep_logs") == "1"
            output = open(str(store.path(name, ".launcher.log")), "ab") if keep else open(os.devnull, "wb")
            extra = {} if os.name == "nt" else {"start_new_session": True}
            try:
                process = subprocess.Popen([sys.executable, str(ROOT / "agent.py"), "send", name, "--prompt-file", str(prompt)],
                                           stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT, env=env,
                                           close_fds=True, **dict(hidden_kwargs(), **extra))
            finally:
                output.close()
            stamp = pid_stamp(process.pid)
            generation = meta.get("generation", "")
            deadline = time.monotonic() + PUBLISH_SECONDS
            sleeper = Event()
            while True:
                data = store.read(name)
                if data.get("generation", "") != generation:
                    break  # send published its owner: the turn resumed in the saved session
                if process.poll() is not None:
                    data = store.read(name)
                    if data.get("generation", "") != generation:
                        break  # published and already finished between the two reads
                    row.update(action="failed", state_after=effective(store, name, data),
                               error="send exited %s before resuming%s" % (process.returncode, (
                                   ": " + data["last_send_error"]) if data.get("last_send_error") else ""))
                    return row
                if time.monotonic() >= deadline:
                    kill_tree(process.pid, stamp=stamp)
                    row.update(action="failed", error="send did not publish its owner within %ss" % PUBLISH_SECONDS)
                    return row
                sleeper.wait(0.05)
            data = store.read(name)
            # Just published: the old activity stamp would read as idle until the turn's first output.
            after = "running" if data.get("state") == "running" else effective(store, name, data)
            row.update(resumed=True, action="revived (send, pid %s)" % process.pid,
                       state_after=after, session_after=store.session(name, data))
            if row["session_after"] and row["session_after"] != row["session_id"]:
                row["error"] = "session changed: %s -> %s" % (row["session_id"], row["session_after"])
            return row
    except ValueError as error:
        if "lock timed out" in str(error):
            row["action"] = "skip: another revive is in progress"
            return row
        row.update(action="failed", error=" ".join(str(error).split()))
        return row
    except OSError as error:
        row.update(action="failed", error=" ".join(str(error).split()))
        return row


def sweep(store, patterns, options):
    """Assess every selected task, launch the candidates (unless dry-run) and return the rows."""
    from . import selection
    names, unmatched = selection.select(store, patterns)
    for item in unmatched:
        print("[agent.sh] no task matches '%s'" % item, file=sys.stderr)
    exact = set(item for item in patterns if not selection.is_glob(item))
    from .lifecycle import effective
    rows = []
    now = time.time()
    started = older = 0
    for name in names:
        meta = store.read(name)
        if not meta:
            continue
        explicit = name in exact
        if not explicit and effective(store, name, meta) not in options["states"]:
            continue
        if not explicit and options["within"] and quiet_seconds(store, name, meta, now) > options["within"]:
            older += 1  # Long-abandoned tasks are never resurrected by a filter or glob.
            continue
        row = assess(store, name, meta, options, explicit, now)
        if row["action"] == "revive":
            if options["limit"] and started >= options["limit"]:
                row["action"] = "skip: deferred by --limit %d" % options["limit"]
            elif options["dry"]:
                row["action"] = "would revive"
                started += 1
            else:
                row = launch(store, name, row, options)
                started += 1
        rows.append(row)
    options["older"] = older
    return rows


def short(session):
    return session[:12] if session else "-"


def render(rows, dry, out=None):
    out = out or sys.stdout
    if not rows:
        print("revive: no error/stopped tasks match", file=out)
        return
    width = max([len(row["name"]) for row in rows] + [4])
    line = "%-{w}s %-8s %-8s %-12s %s".format(w=width)
    print(line % ("TASK", "BEFORE", "AFTER", "SESSION", "ACTION"), file=out)
    for row in rows:
        action = row["action"] + (" [%s]" % row["cause"] if row["cause"] and row["action"] != "revive" else "")
        if row["error"]:
            action += " ERROR: " + row["error"]
        print(line % (row["name"], row["state_before"], row["state_after"], short(row["session_id"]), action), file=out)


def summary(rows, dry, older=0):
    resumed = [row for row in rows if row["resumed"]]
    failed = [row for row in rows if row["action"] == "failed"]
    candidates = [row for row in rows if row["action"] == "would revive"]
    skipped = [row for row in rows if not row["resumed"] and row["action"] != "would revive"]
    return dict(dry_run=bool(dry), resumed=resumed, candidates=candidates, skipped=skipped,
                summary=dict(resumed=len(resumed), candidates=len(candidates), failed=len(failed),
                             skipped=len(skipped) - len(failed), ignored_older=older))


def footer(data, out=None):
    counts = data["summary"]
    if data["dry_run"]:
        text = "revive (dry run, nothing changed): %d would revive, %d skipped" % (counts["candidates"], counts["skipped"])
    else:
        text = "revive: %d resumed, %d skipped, %d failed" % (counts["resumed"], counts["skipped"], counts["failed"])
    if counts["ignored_older"]:
        text += "; %d older task(s) ignored (quiet longer than --within; name them to include)" % counts["ignored_older"]
    print(text, file=out or sys.stdout)


def parse_options(opts):
    from .selection import parse_states
    states = parse_states(opts["state"]) if "state" in opts else DEFAULT_STATES
    refused = [state for state in states if state not in REVIVABLE]
    if refused:
        raise ValueError("revive: --state accepts only %s (never %s)" % (", ".join(REVIVABLE), ", ".join(refused)))
    attempts = seconds(opts.get("max_attempts"), "revive: --max-attempts", DEFAULT_ATTEMPTS)
    if attempts != int(attempts):
        raise ValueError("revive: --max-attempts must be a whole number (0 = unlimited)")
    limit = seconds(opts.get("limit"), "revive: --limit", DEFAULT_LIMIT)
    if limit != int(limit):
        raise ValueError("revive: --limit must be a whole number (0 = unlimited)")
    interval = seconds(opts.get("interval"), "revive: --interval", 60)
    if opts.get("--watch") and interval <= 0:
        raise ValueError("revive: --interval must be a positive number of seconds")
    return dict(states=states, stale=seconds(opts.get("stale"), "revive: --stale", DEFAULT_STALE),
                cooldown=seconds(opts.get("cooldown"), "revive: --cooldown", DEFAULT_COOLDOWN),
                within=seconds(opts.get("within"), "revive: --within", DEFAULT_WITHIN), limit=int(limit),
                attempts=int(attempts), force=bool(opts.get("--force")), dry=bool(opts.get("--dry-run") or opts.get("-n")),
                message=opts.get("message", ""), interval=interval)


def command(store, patterns, opts):
    options = parse_options(opts)
    as_json = bool(opts.get("--json"))
    if not patterns and not options["dry"] and not (os.environ.get("AGENT_PARENT") or os.environ.get("AGENT_ORCHESTRATOR_ID")):
        raise ValueError("revive: no task scope; name tasks or a glob, or set AGENT_PARENT/AGENT_ORCHESTRATOR_ID "
                         "(as for stop --all-mine); preview with --dry-run")
    if not opts.get("--watch"):
        rows = sweep(store, patterns, options)
        data = summary(rows, options["dry"], options.get("older", 0))
        if as_json:
            print(json.dumps(data, ensure_ascii=False))
        else:
            render(rows, options["dry"])
            footer(data)
        return 3 if data["summary"]["failed"] else 0
    if options["dry"]:
        raise ValueError("revive: --watch cannot be combined with --dry-run; run --dry-run once instead")
    revived = []
    failures = 0
    print("[agent.sh] revive --watch: every %ss (Ctrl+C stops; revives use the saved session)" % options["interval"], file=sys.stderr, flush=True)
    try:
        while True:
            rows = sweep(store, patterns, options)
            acted = [row for row in rows if row["resumed"] or row["action"] == "failed"]
            failures += sum(1 for row in acted if row["action"] == "failed")
            revived += [row for row in acted if row["resumed"]]
            if acted:
                if as_json:
                    print(json.dumps(dict(at_epoch=round(time.time(), 3), rows=acted), ensure_ascii=False), flush=True)
                else:
                    print(time.strftime("%H:%M:%S"), flush=True)
                    render(acted, False)
                    sys.stdout.flush()
            time.sleep(options["interval"])
    except KeyboardInterrupt:
        pass
    names = ", ".join("%s (%s)" % (row["name"], short(row["session_id"])) for row in revived) or "nothing"
    print("revive --watch stopped: revived %d time(s): %s; %d failed" % (len(revived), names, failures), file=sys.stderr, flush=True)
    return 3 if failures else 0
