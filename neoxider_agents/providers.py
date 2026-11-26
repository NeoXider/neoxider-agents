"""Lazy provider plugins and native executable discovery (never a command shell)."""
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import tempfile
from functools import lru_cache
from threading import Event

ROOT = Path(__file__).resolve().parent.parent
ENGINES = ("codex", "claude", "kimi", "opencode", "gemini")
FAILURE = re.compile(r"usage limit|rate.?limit|quota exceeded|insufficient (quota|credit)|payment required|requires a newer version of|unrecognized_model|issue with the selected model|model[^.?!]*(not found|unavailable|unknown)|unauthoriz|invalid api key|authentication (failed|error)|token expired|session expired|not logged in|please (run|log ?in)", re.I)
RESET_PATTERNS = (
    re.compile(r"try again at ([^.;\n]+)", re.I),
    re.compile(r"resets?\s+in\s+(\d+(?:\.\d+)?\s*(?:ms|milliseconds?|seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h|days?|d))", re.I),
    re.compile(r"retry\s+after\s+(\d+(?:\.\d+)?\s*(?:ms|milliseconds?|seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h|days?|d))", re.I),
    re.compile(r"available again ([^.;\n]+)", re.I),
)
CONFLICT = re.compile(r"thread-store conflict|already has an active writer|code\s+-32600", re.I)
TRANSIENT = re.compile(r"stream error|Failed to execute|ECONNRESET|ECONNREFUSED|ETIMEDOUT|EAI_AGAIN|socket hang up|fetch failed|network error|overloaded|Bad Gateway|Service Unavailable|Gateway Time-?out|HTTP (502|503|504)|status[ =:]+(502|503|504)", re.I)


def hidden_options():
    if os.name != "nt":
        return {}
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 0
    return {"creationflags": subprocess.CREATE_NO_WINDOW, "startupinfo": startup}


NATIVE_SUFFIXES = (".exe", ".cmd", ".bat", ".ps1", ".py")


SHIM_EXEC = re.compile(
    r"""^\s*exec\s+(?P<q>["']?)(?P<path>[^"'\r\n]+?\.exe)(?P=q)(?:\s+"\$@")?\s*$""", re.I | re.M)
LAUNCHER_RELATIVE = re.compile(r'(?:%dp0%|%~dp0|\$basedir)[\\/]*([^"\r\n]+?\.(?:exe|js|cjs|mjs))', re.I)
LAUNCHER_ABSOLUTE = re.compile(r'"([A-Za-z]:[\\/][^"\r\n]+?\.exe)"', re.I)
# A shim that execs one of these runs a script, not the CLI itself: its argv cannot be rebuilt from the
# exe alone, so such a target is never taken (the PATH walk goes on to a .cmd launcher instead).
INTERPRETERS = frozenset({"node.exe", "bun.exe", "deno.exe", "python.exe", "pythonw.exe", "py.exe"})


def _head(path, limit=65536):
    """The first LIMIT bytes of a small launcher, decoded; never the whole of a large binary."""
    try:
        with open(path, "rb") as handle:
            return handle.read(limit).decode("utf-8-sig", "replace")
    except (OSError, ValueError):
        return ""


def _drive_path(raw, base):
    """MSYS (/c/...) and Cygwin (/cygdrive/c/...) spellings as drive paths; relative paths from BASE."""
    match = re.match(r"^/(?:cygdrive/)?([A-Za-z])/(.*)$", raw)
    if match:
        raw = match.group(1).upper() + ":/" + match.group(2)
    target = Path(raw)
    return target if target.is_absolute() else Path(base) / target


def shim_target(path):
    """The native CLI an extensionless sh shim execs, or None.

    Taken only for a plain `exec "<file>.exe" "$@"` (or without "$@"): fixed arguments after the exe, an
    interpreter (`exec node.exe cli.js`) or a target that does not exist cannot be launched from the file
    name alone, so they answer None and the caller keeps looking."""
    for match in SHIM_EXEC.finditer(_head(path, 8192)):
        target = _drive_path(match.group("path").strip(), Path(path).parent)
        if target.name.lower() in INTERPRETERS:
            continue
        if target.is_file():
            return str(target)
    return None


def launcher_argv(path):
    """argv a .cmd/.bat/.ps1 launcher starts without a command shell, or None when it cannot be told."""
    path = Path(path)
    text = _head(path)
    relative = LAUNCHER_RELATIVE.findall(text)
    for match in relative:
        target = path.parent / match.replace("\\", "/")
        if target.is_file() and target.suffix.lower() == ".exe" and target.name.lower() not in INTERPRETERS:
            return [str(target)]
    for match in relative:
        target = path.parent / match.replace("\\", "/")
        if target.is_file() and target.suffix.lower() in (".js", ".cjs", ".mjs"):
            node = path.parent / "node.exe"
            node_path = str(node) if node.is_file() else shutil.which("node")
            if node_path:
                return [node_path, str(target)]
    for match in LAUNCHER_ABSOLUTE.findall(text):
        target = Path(match)
        if target.is_file() and target.name.lower() not in INTERPRETERS:
            return [str(target)]
    return None


LAUNCHER_SUFFIXES = (".cmd", ".bat", ".ps1")


def which_native(engine):
    """PATH lookup that never returns an extensionless shell shim on Windows.

    A shim such as ~/bin/opencode (a sh script) cannot be started by CreateProcess (WinError 193) and
    PowerShell hands it to the shell, which opens the 'choose an app' dialog.

    What the user's own PATH runs wins: an .exe found on PATH is returned as is, a .cmd launcher whose
    target can be read is returned (executable() unwraps it to that target), and an extensionless shim
    resolves to the file it execs. A conventional `<engine>-real.exe` copy next to them is only the
    fallback for a launcher or shim whose target cannot be read: preferring it unconditionally ran a stale
    copy (opencode 1.18.27 left as opencode-real.exe while the shims had moved to 2.0.32), and that old CLI
    rejected the flags of the new one - every OpenCode launch printed the usage text and failed (observed
    live 2026-11-26)."""
    found = shutil.which(engine)
    if os.name != "nt":
        return found
    if found:
        suffix = Path(found).suffix.lower()
        if suffix in LAUNCHER_SUFFIXES:
            if launcher_argv(found):
                return found
        elif suffix in NATIVE_SUFFIXES:
            return found
        else:
            target = shim_target(found)
            if target:
                return target
        sibling = Path(found).with_name(engine + "-real.exe")
        if sibling.is_file():
            return str(sibling)
    unreadable = None
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if not directory:
            continue
        for suffix in NATIVE_SUFFIXES:
            candidate = Path(directory) / (engine + suffix)
            if not candidate.is_file():
                continue
            if suffix in LAUNCHER_SUFFIXES and not launcher_argv(candidate):
                unreadable = unreadable or str(candidate)
                continue
            return str(candidate)
        shim = Path(directory) / engine
        target = shim_target(shim) if shim.is_file() else None
        if target:
            return target
        sibling = Path(directory) / (engine + "-real.exe")
        if sibling.is_file():
            return str(sibling)
    # Only an unreadable launcher exists: return it so executable() names the problem.
    return unreadable


def executable(engine):
    """Unwrap npm launchers to native exe/Node, preserving spaced UTF-8 paths."""
    chosen = os.environ.get("AGENT_%s_BIN" % engine.upper()) or which_native(engine)
    if not chosen:
        raise FileNotFoundError("%s CLI not found in PATH" % engine)
    path = Path(chosen)
    if os.name != "nt" or path.suffix.lower() not in (".cmd", ".bat", ".ps1"):
        if path.suffix.lower() == ".py":
            return [sys.executable, str(path)]
        return [str(path)]
    argv = launcher_argv(path)
    if argv:
        return argv
    raise ValueError("Cannot launch %s without a command shell; install its native executable or set AGENT_%s_BIN" % (engine, engine.upper()))


@lru_cache(maxsize=32)
def _run_help_cached(argv, stamp):
    try:
        code, help_text = capture(list(argv) + ["run", "--help"], timeout=10)
    except (OSError, ValueError):
        return ""
    return help_text if code == 0 and help_text else ""


def run_help(engine, base=None):
    """`run --help` text of the CLI (BASE argv, else the engine's executable), "" when unreadable.

    Spawned once per CLI file: the cache key is the resolved argv plus the file's size and mtime, so a
    long-lived GUI or API server notices an upgraded CLI, and a launch no longer pays one `run --help`
    per probe per turn. BASE is the argv the launch will really use (an isolated runtime profile pins its
    own binary), so the probe asks that binary, not whatever is first on PATH."""
    try:
        argv = tuple(base) if base else tuple(executable(engine))
    except (OSError, ValueError):
        return ""
    try:
        info = os.stat(argv[0])
        stamp = (info.st_size, info.st_mtime_ns)
    except (OSError, ValueError, IndexError):
        stamp = None
    return _run_help_cached(argv, stamp)


def run_supports_flag(engine, flag, base=None):
    """True when the CLI's own `run --help` advertises --FLAG.

    The pinned CLI is authoritative about its own flags: the installed `opencode run` has no --dir and a
    launch that passes it dies with "Unrecognized flag" (observed live 2026-11-24), while the .sh path of
    the same provider already carries the directory via cwd. A failed probe answers False: on the Python
    path the directory is passed through subprocess cwd anyway, so a missing flag costs nothing, whereas a
    spurious one kills the run."""
    help_text = run_help(engine, base)
    return any(("--" + flag) in line for line in help_text.splitlines()) if help_text else False


run_supports_flag.cache_clear = _run_help_cached.cache_clear


def run_log_level(engine, level="error", base=None):
    """LEVEL spelled the way this CLI's `run --log-level` accepts it.

    OpenCode 1.x lists the choices in capitals ("DEBUG", "INFO", "WARN", "ERROR") and rejects
    `error` with its usage text; 2.x takes lower case. The CLI's own help decides; without a readable
    help the lower-case spelling of current releases is used."""
    help_text = run_help(engine, base)
    if help_text and ('"%s"' % level.upper()) in help_text and ('"%s"' % level.lower()) not in help_text:
        return level.upper()
    return level


def model_variant_args(engine, model, effort, base=None):
    """argv tail that selects MODEL and its reasoning variant on a `provider/model#variant` CLI.

    The pinned OpenCode CLI (v2.4.0) takes the variant only inside the model id; a separate --variant dies
    with "Unrecognized flag: --variant in command opencode run" before any provider activity (observed live
    2026-11-25), so `-f high` used to kill every launch. A variant already in the id wins; a CLI that
    advertises --variant keeps the separate flag; without a model there is nothing to attach it to and the
    CLI default applies."""
    if effort and model and "#" in model:
        return ["-m", model]
    if effort and run_supports_flag(engine, "variant", base):
        return (["-m", model] if model else []) + ["--variant", effort]
    if effort and model:
        return ["-m", model + "#" + effort]
    return ["-m", model] if model else []

def capture(argv, timeout=8):
    try:
        deadline = float(os.environ.get("AGENT_DIAGNOSTIC_DEADLINE", "inf"))
        timeout = min(timeout, max(0.001, deadline - time.monotonic()))
    except ValueError:
        pass
    if os.environ.get("AGENT_DIAGNOSTIC_HELPER") == "1":
        return _diagnostic_capture(argv, timeout)
    try:
        process = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, timeout=timeout, **hidden_options())
        return process.returncode, process.stdout.decode("utf-8", "replace").strip()
    except subprocess.TimeoutExpired:
        return 124, ""
    except OSError:
        return 1, ""


def diagnostic_pipe_read(stream):
    """Read only ready bytes; no background reader or buffered blocking close."""
    fd = stream.fileno()
    if os.name == "nt":
        import ctypes
        import msvcrt
        from .windows import _declare, HANDLE, w
        available = w.DWORD()
        peek = _declare("PeekNamedPipe", [HANDLE, ctypes.c_void_p, w.DWORD, ctypes.c_void_p,
                                          ctypes.POINTER(w.DWORD), ctypes.c_void_p], w.BOOL)
        if not peek(msvcrt.get_osfhandle(fd), None, 0, None, ctypes.byref(available), None):
            error = ctypes.get_last_error()
            if error in (109, 232, 233):
                return b""
            raise OSError(error, "diagnostic pipe query failed")
        return os.read(fd, min(4096, available.value)) if available.value else None
    import select
    ready, unused, unused = select.select([fd], [], [], 0)
    return os.read(fd, 4096) if ready else None


def drain_diagnostic_pipe(stream, data, limit, deadline):
    while time.monotonic() < deadline:
        block = diagnostic_pipe_read(stream)
        if not block:
            return ""
        remaining = limit - len(data)
        data.extend(block[:max(0, remaining)])
        if len(block) > remaining:
            return "output exceeded limit"
    return "deadline exceeded"


def close_diagnostic_tree(tree):
    """Always close the pipe, even when containment/reaping reports an error."""
    from .process import kill_tree, pid_alive, pid_stamp
    owned = {}
    problem = ""
    current = pid_stamp(tree.pid)
    if not current or current == tree.stamp:
        try:
            if os.name == "nt":
                from .windows import processes
                rows = processes()
            else:
                from .process import _posix_rows
                rows = _posix_rows()
        except Exception:
            rows = []
            problem = "diagnostic descendant inventory failed"
        parents = {tree.pid}
        for unused in rows:
            added = {pid for pid, parent, unused_value in rows if parent in parents and pid not in parents}
            if not added:
                break
            parents.update(added)
        for pid, parent, unused_value in rows:
            same_group = False
            if os.name != "nt":
                try:
                    same_group = os.getpgid(pid) == tree.pid
                except OSError:
                    pass
            if pid in parents or same_group:
                stamp = pid_stamp(pid)
                if stamp:
                    owned[pid] = stamp
    deadline = time.monotonic() + 0.4
    try:
        try:
            tree.close()
        except Exception:
            problem = "diagnostic containment cleanup failed"
            try:
                kill_tree(tree.pid, stamp=tree.stamp, job_name=tree.job_name)
            except Exception:
                pass
        try:
            tree.process.wait(timeout=0.2)
        except Exception:
            problem = "diagnostic process cleanup incomplete"
            try:
                kill_tree(tree.pid, stamp=tree.stamp, job_name=tree.job_name)
            except Exception:
                pass
            try:
                tree.process.wait(timeout=max(0, min(0.2, deadline - time.monotonic())))
            except Exception:
                pass
        # A nested version probe may be in its own session. Stop only descendants
        # whose creation stamps were captured before the supervising process exited.
        for pid, stamp in owned.items():
            if pid != tree.pid and pid_alive(pid, stamp):
                kill_tree(pid, stamp=stamp)
        sleeper = Event()
        while owned and time.monotonic() < deadline:
            owned = {pid: stamp for pid, stamp in owned.items() if pid_alive(pid, stamp)}
            if owned:
                sleeper.wait(min(0.01, max(0, deadline - time.monotonic())))
        if owned:
            problem = "diagnostic descendants cleanup incomplete"
    finally:
        try:
            if tree.process.stdout:
                try:
                    tree.process.stdout.close()
                except Exception:
                    problem = "diagnostic stdout cleanup incomplete"
                    try:
                        tree.process.stdout.raw.close()
                    except Exception:
                        pass
        finally:
            try:
                tree.close()
            except Exception:
                problem = "diagnostic containment cleanup incomplete"
    return problem


def _diagnostic_capture(argv, timeout):
    """Bound CLI output inside the diagnostic helper, before it becomes JSON."""
    from .process import spawn
    data, sleeper = bytearray(), Event()
    deadline = time.monotonic() + timeout
    with tempfile.TemporaryDirectory(prefix="neoxider-version-"):
        tree = spawn(argv, os.devnull)
        problem = ""
        try:
            while True:
                problem = drain_diagnostic_pipe(tree.process.stdout, data, 128 * 1024, deadline)
                if problem or tree.process.poll() is not None:
                    break
                sleeper.wait(min(0.02, max(0, deadline - time.monotonic())))
            code = tree.process.returncode
        finally:
            # Drain a completed parent's buffered bytes before closing its inherited pipe.
            try:
                if not problem:
                    problem = drain_diagnostic_pipe(tree.process.stdout, data, 128 * 1024, deadline)
            except (OSError, ValueError):
                problem = "diagnostic output read failed"
            finally:
                cleanup = close_diagnostic_tree(tree)
        if problem or cleanup:
            if "deadline" in problem:
                raise TimeoutError("diagnostic CLI deadline exceeded")
            raise RuntimeError("diagnostic CLI output or cleanup failed")
        return code or 0, bytes(data).decode("utf-8", "replace").strip()


class BaseProvider:
    engine = ""
    supports_resume = True
    retry_limit = 0

    def resolve(self, model="", effort=""):
        return model, effort

    def prepare_resume(self, session, cwd):
        return ""

    def doctor(self):
        try:
            base = executable(self.engine)
        except FileNotFoundError:
            return {"engine": self.engine, "version": "NOT_FOUND", "available": False,
                    "login": "", "limits": None, "note": ""}
        except (OSError, ValueError):
            return {"engine": self.engine, "version": "?", "available": None,
                    "login": "", "limits": None, "state": "error", "note": "executable resolution failed"}
        code, version = capture(base + ["--version"])
        if code or not version:
            return {"engine": self.engine, "version": "?", "available": None, "login": "", "limits": None,
                    "state": "timeout" if code == 124 else "error", "note": "CLI version probe did not succeed"}
        return {"engine": self.engine, "version": version.splitlines()[0] if version else "?",
                "available": True, "login": "", "limits": None,
                "note": "No CLI limits endpoint for this provider."}

    def retry_reason(self, text, code, session=""):
        return ""

    def retry_delay(self, attempt):
        return min(120, 15 * 2 ** max(0, attempt - 1))


def get_provider(engine):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", engine):
        raise ValueError("invalid provider name: " + engine)
    root = Path(os.environ.get("AGENT_PROVIDER_DIR") or ROOT / "providers")
    path = root / engine / "provider.py"
    if not path.is_file():
        raise ValueError("unknown engine '%s': expected %s/<engine>/provider.py" % (engine, root))
    name = "neoxider_provider_" + engine
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    provider = module.Provider() if hasattr(module, "Provider") else module.provider
    provider.engine = engine
    return provider


def provider_info(root=None):
    directory = Path(root or os.environ.get("AGENT_PROVIDER_DIR") or ROOT / "providers")
    result = {}
    for entry in sorted(directory.iterdir()):
        path = entry / "provider.json"
        if entry.is_dir() and path.is_file():
            try:
                result[entry.name] = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                pass
    return result


def failure_reason(text, live=False):
    lines = text.splitlines()
    markers = [i for i, line in enumerate(lines) if line == "---------- output ----------"]
    if markers:
        lines = lines[markers[-1] + 1:]
    for line in reversed(lines):
        if "AGENT_PROVIDER_ERROR: " in line:
            return line.split("AGENT_PROVIDER_ERROR: ", 1)[1]
    if not live:
        for line in reversed(lines):
            if FAILURE.search(line):
                return line
    return ""


def provider_failure_reason(text, live=False):
    return failure_reason(text, live)


def parse_limit_reset(text):
    """Conservatively extract a provider-limit reset hint; "unknown" when absent."""
    for pattern in RESET_PATTERNS:
        match = pattern.search(text or "")
        if match:
            return " ".join(match.group(1).split()).replace('"', "'") or "unknown"
    return "unknown"


def format_limit_hit(name, engine, model, resets):
    """Canonical machine-greppable usage-limit marker (resets must already be redacted)."""
    return 'LIMIT_HIT task=%s engine=%s model=%s resets="%s"' % (name, engine, model, resets)


def limit_line(name, engine, model, reason):
    """Canonical LIMIT_HIT marker for a limited task; secrets are redacted."""
    from activity import redact
    return format_limit_hit(name, engine, model, redact(parse_limit_reset(reason or "")))


_ABSOLUTE_RESET = re.compile(
    r"([A-Za-z]{3,9})\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\s+(\d{1,2}):(\d{2})\s*(AM|PM)?", re.I)
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")


def parse_reset_epoch(text):
    """Local epoch of an absolute reset like "Oct 22nd, 2026 11:27 AM"; None for relative or unknown text."""
    match = _ABSOLUTE_RESET.search(text or "")
    if not match:
        return None
    month, day, year, hour, minute, meridiem = match.groups()
    if month[:3].lower() not in _MONTHS:
        return None
    hour = int(hour)
    if meridiem:
        hour = hour % 12 + (12 if meridiem.upper() == "PM" else 0)
    try:
        stamp = time.mktime((int(year), _MONTHS.index(month[:3].lower()) + 1, int(day), hour, int(minute), 0, 0, 0, -1))
    except (OverflowError, ValueError):
        return None
    return stamp


def limits_path(root=None):
    """File that remembers the last usage limit each engine reported (best effort, secrets redacted)."""
    base = root or os.environ.get("AGENT_CLI_LOGS") or str(Path.home() / ".claude/agent-cli-logs")
    return Path(base) / "limits.json"


def record_limit(root, engine, model, reason):
    """Remember an engine's usage limit so doctor and later runs can warn before the next call fails."""
    from activity import redact
    path = limits_path(root)
    try:
        try:
            known = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            known = {}
        if not isinstance(known, dict):
            known = {}
        resets = redact(parse_limit_reset(reason or ""))
        known[engine] = dict(model=model, resets=resets, resets_epoch=parse_reset_epoch(resets),
                             recorded_epoch=time.time(), reason=redact(" ".join((reason or "").split()))[:300])
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name("%s.tmp.%s" % (path.name, os.getpid()))
        temporary.write_text(json.dumps(known, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(temporary, path)
    except OSError:
        pass  # diagnostics only: never fail a task because the note could not be written


def clear_limit(root, engine):
    """Forget an engine's recorded limit after it completed a turn again (credits bought or window reset)."""
    path = limits_path(root)
    try:
        known = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(known, dict) and known.pop(engine, None) is not None:
            temporary = path.with_name("%s.tmp.%s" % (path.name, os.getpid()))
            temporary.write_text(json.dumps(known, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(temporary, path)
    except (OSError, ValueError):
        pass


def known_limits(root=None, now=None):
    """Recorded limits that are still in force: absolute resets in the future, relative/unknown ones for 6 hours."""
    try:
        known = json.loads(limits_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    now = time.time() if now is None else now
    active = {}
    for engine, row in (known.items() if isinstance(known, dict) else ()):
        if not isinstance(row, dict):
            continue
        until = row.get("resets_epoch")
        if not isinstance(until, (int, float)):
            until = float(row.get("recorded_epoch") or 0) + 6 * 3600
        if until > now:
            active[engine] = dict(row, until=until)
    return active


TRANSPORT = re.compile(
    r"provider\.transport|ECONNRESET|ECONNREFUSED|ECONNABORTED|ETIMEDOUT|ENOTFOUND|EAI_AGAIN|ENETUNREACH"
    r"|EHOSTUNREACH|EPIPE|socket hang up|socket connection was closed|fetch failed|network error"
    r"|can't reach|getaddrinfo|dns error|tls handshake|connection (?:reset|closed|aborted|refused)"
    r"|reconnecting\.\.\. waiting for network \(|error sending request"
    r"|no such host is known|name or service not known|workspace routing discovery (?:timed out|failed)", re.I)
NOT_TRANSPORT = re.compile(
    r"unauthorized|invalid api key|authentication|forbidden|payment required|insufficient (?:quota|credit)"
    r"|quota exceeded|usage limit|rate.?limit|model .*(?:not found|unavailable|unknown)|context window"
    r"|\bHTTP (?:401|402|403|429)\b|\b429 too many requests\b", re.I)


def transport_retry_reason(text):
    """Why a turn the provider ended on a network fault is worth another attempt, or ''.

    Seven OpenCode team leads died together on 2026-11-25: a DNS blip (`getaddrinfo ENOTFOUND opencode.ai`)
    and a few `ECONNRESET`s reached the wrapper as a parsed provider error, which the watchdog treats as
    terminal (exit 126), so each task stopped although the service was back within a minute. A parsed
    provider error stays terminal for everything else (quota, auth, an unknown model); only a fault of the
    network path between this machine and the provider is retried."""
    text = text or ""
    if not TRANSPORT.search(text) or NOT_TRANSPORT.search(text):
        return ""
    return "transient network transport failure"

FREE_LIMIT = re.compile(r"provider\.free-limit|free usage limit reached", re.I)


def free_limit_retry_reason(text):
    """Why a turn ended by a free-tier usage limit is worth waiting for, or ''.

    The OpenCode free tier answers `429 provider.free-limit` ("Free usage limit reached") from a rolling window
    with no reset time: a wave of nine workers was stopped as `limited` together on 2026-11-25 although the same
    route answered again two minutes later. Paid quotas ("usage limit", "quota exceeded") name a reset or need
    the owner and stay terminal; only this free rolling limit is waited out."""
    return "free-tier usage limit (rolling window)" if FREE_LIMIT.search(text or "") else ""


def free_limit_reset_note(seconds, now=None):
    """Human wording of a provider-reported reset: `provider reports reset in 12.9 h (~05:00 local)`."""
    import time
    span = "%.1f h" % (seconds / 3600.0) if seconds >= 3600 else "%d min" % max(1, round(seconds / 60.0))
    at = time.strftime("%H:%M", time.localtime((now if now is not None else time.time()) + seconds))
    return "provider reports reset in %s (~%s local)" % (span, at)

def is_transient_failure(text):
    if re.search(r"unauthorized|invalid api key|authentication|forbidden|(?:^|[^\w])model .*(not found|unavailable|unknown)|insufficient (quota|credit)|quota exceeded|payment required", text, re.I):
        return False
    return bool(re.search(r'"isretryable"\s*:\s*true|network_error|providerresponsestreamerror|stream (error|closed|interrupted)|econnreset|etimedout|enotfound|socket hang up|(?:^|[^\w])(429|500|502|503|504)(?:$|[^\w])|overloaded|temporarily unavailable|rate.?limit', text, re.I))
