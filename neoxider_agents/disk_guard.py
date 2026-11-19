"""Free-space checks for the volumes a worker must write to.

A provider that cannot write its session/rollout files does not fail loudly: on 2026-11-19 five Codex
workers hung on their first command while C: had 9 MB free, and the silence watchdog only noticed
40 minutes later. Refuse a launch on a nearly full volume, and name low disk in a silence stop.
"""
import os
import shutil
from pathlib import Path

DEFAULT_MIN_FREE_MB = 512


def min_free_mb():
    value = os.environ.get("AGENT_MIN_FREE_MB", "")
    try:
        return max(0, int(value)) if value.strip() else DEFAULT_MIN_FREE_MB
    except ValueError:
        return DEFAULT_MIN_FREE_MB


def provider_home(engine):
    if engine == "codex":
        return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    if engine == "claude":
        return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    return Path.home()


def watched_paths(store_root, engine, work_dir=None):
    paths = [Path(store_root), provider_home(engine)]
    if work_dir:
        paths.append(Path(work_dir))
    temp = os.environ.get("TEMP") or os.environ.get("TMPDIR")
    if temp:
        paths.append(Path(temp))
    return paths


def low_volumes(paths, minimum_mb=None):
    """Return [(path, free_mb)] for distinct existing volumes below the threshold."""
    minimum_mb = min_free_mb() if minimum_mb is None else minimum_mb
    if minimum_mb <= 0:
        return []
    seen, low = set(), []
    for path in paths:
        probe = Path(path)
        while not probe.exists() and probe.parent != probe:
            probe = probe.parent
        try:
            anchor = os.path.splitdrive(str(probe.resolve()))[0] or str(probe.resolve().anchor)
            if anchor in seen:
                continue
            seen.add(anchor)
            free_mb = shutil.disk_usage(str(probe)).free // (1024 * 1024)
        except OSError:
            continue
        if free_mb < minimum_mb:
            low.append((str(probe), int(free_mb)))
    return low


def describe(low):
    return ", ".join("%s has %d MB free" % item for item in low)
