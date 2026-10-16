"""Select several tasks at once: exact names, globs and state filters shared by status, top, wait and stop."""
import fnmatch
import os
import re
import time
from pathlib import Path

ACTIVE_STATES = ("running", "idle", "waiting", "orphaned")
FAILED_STATES = ("error", "limited", "silent", "stalled")
KNOWN_STATES = ("running", "idle", "done", "waiting", "error", "stalled", "limited", "silent", "stopped", "orphaned")
GROUPS = {"active": ACTIVE_STATES, "failed": FAILED_STATES}
GLOB = re.compile(r"[*?\[]")
DURATION = re.compile(r"(\d+(?:\.\d+)?)([smhd])")
UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def is_glob(text):
    return bool(GLOB.search(text))


def parse_duration(text, label):
    """90s, 15m, 2h, 1d or a combination such as 1h30m; a bare number is rejected as ambiguous."""
    value = text.strip().lower()
    parts = DURATION.findall(value)
    if not parts or "".join(number + unit for number, unit in parts) != value:
        raise ValueError("%s must look like 90s, 15m, 2h, 1d or 1h30m" % label)
    seconds = sum(float(number) * UNITS[unit] for number, unit in parts)
    if seconds <= 0:
        raise ValueError("%s must be longer than zero" % label)
    return seconds


def parse_states(text, label="--state"):
    states = []
    for item in text.lower().split(","):
        item = item.strip()
        if not item:
            continue
        if item in GROUPS:
            states.extend(GROUPS[item])
        elif item in KNOWN_STATES:
            states.append(item)
        else:
            raise ValueError("%s: unknown state '%s'; use %s or the groups %s" % (label, item, ", ".join(KNOWN_STATES), ", ".join(GROUPS)))
    if not states:
        raise ValueError("%s needs at least one state" % label)
    return tuple(dict.fromkeys(states))


def wants_selection(args, opts):
    """True when the request names several tasks, a glob, or a filter, and therefore needs a table."""
    return len(args) > 1 or any(is_glob(arg) for arg in args) or any(
        key in opts for key in ("--active", "--here", "--json", "state", "since"))


def select(store, patterns, opts=None, owned_only=True):
    """Return (names, unmatched_globs) ordered like the store scan (newest first).

    Exact names are explicit and bypass the ownership filter; globs and pure filters see only tasks owned by
    the current orchestrator, so `stop 'pl-*'` cannot touch another parent's workers.
    """
    from .lifecycle import effective
    from .ux_tracking import _start
    from .views import mine
    opts = opts or {}
    exact = [item for item in patterns if not is_glob(item)]
    globs = [item for item in patterns if is_glob(item)]
    for item in exact:
        if not store.path(item, ".meta").is_file():
            raise ValueError("no such task '%s'" % item)
    states = parse_states(opts["state"]) if "state" in opts else ()
    if opts.get("--active"):
        states = tuple(dict.fromkeys(states + ACTIVE_STATES)) if states else ACTIVE_STATES
    cutoff = time.time() - parse_duration(opts["since"], "--since") if "since" in opts else 0
    here = Path(os.getcwd()).resolve() if opts.get("--here") else None
    matched_globs = set()
    names = []
    for name, meta, stamp in store.scan():
        explicit = name in exact
        by_glob = [item for item in globs if fnmatch.fnmatchcase(name, item)]
        if patterns and not explicit and not by_glob:
            continue
        if not explicit:
            if owned_only and not mine(meta):
                continue
        matched_globs.update(by_glob)
        if explicit and not states and not cutoff and here is None:
            names.append(name)
            continue
        if states and effective(store, name, meta) not in states:
            continue
        if cutoff and _start(meta, stamp) < cutoff:
            continue
        if here is not None and not _under(meta.get("dir", ""), here):
            continue
        names.append(name)
    return names, [item for item in globs if item not in matched_globs]


def _under(directory, root):
    if not directory:
        return False
    try:
        path = Path(directory).resolve()
    except OSError:
        return False
    return path == root or root in path.parents


def expand(store, patterns):
    """Expand globs in a name list for stop/wait; exact names pass through unchanged and in order."""
    if not any(is_glob(item) for item in patterns):
        return list(patterns)
    result = []
    for item in patterns:
        if not is_glob(item):
            result.append(item)
            continue
        names, _ = select(store, [item])
        if not names:
            raise ValueError("no task matches '%s'" % item)
        result.extend(sorted(names))
    return list(dict.fromkeys(result))
