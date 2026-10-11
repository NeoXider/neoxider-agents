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


def which_native(engine):
    """PATH lookup that never returns an extensionless shell shim on Windows.

    A shim such as ~/bin/opencode (a sh script) cannot be started by CreateProcess (WinError 193) and
    PowerShell hands it to the shell, which opens the 'choose an app' dialog."""
    found = shutil.which(engine)
    if os.name != "nt":
        return found
    if found:
        sibling = Path(found).with_name(engine + "-real.exe")
        if sibling.is_file():
            return str(sibling)
        if Path(found).suffix.lower() in NATIVE_SUFFIXES:
            return found
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if not directory:
            continue
        sibling = Path(directory) / (engine + "-real.exe")
        if sibling.is_file():
            return str(sibling)
        for suffix in NATIVE_SUFFIXES:
            candidate = Path(directory) / (engine + suffix)
            if candidate.is_file():
                return str(candidate)
    return None


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
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    matches = re.findall(r'(?:%dp0%|%~dp0|\$basedir)[\\/]*([^"\r\n]+?\.(?:exe|js|cjs|mjs))', text, re.I)
    for match in matches:
        target = path.parent / match.replace("\\", "/")
        if target.is_file() and target.suffix.lower() == ".exe" and target.name.lower() != "node.exe":
            return [str(target)]
    for match in matches:
        target = path.parent / match.replace("\\", "/")
        if target.is_file() and target.suffix.lower() in (".js", ".cjs", ".mjs"):
            node = path.parent / "node.exe"
            node_path = str(node) if node.is_file() else shutil.which("node")
            if node_path:
                return [node_path, str(target)]
    raise ValueError("Cannot launch %s without a command shell; install its native executable or set AGENT_%s_BIN" % (engine, engine.upper()))


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
    deadline = time.monotonic() + 0.4
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


def is_transient_failure(text):
    if re.search(r"unauthorized|invalid api key|authentication|forbidden|(?:^|[^\w])model .*(not found|unavailable|unknown)|insufficient (quota|credit)|quota exceeded|payment required", text, re.I):
        return False
    return bool(re.search(r'"isretryable"\s*:\s*true|network_error|providerresponsestreamerror|stream (error|closed|interrupted)|econnreset|etimedout|enotfound|socket hang up|(?:^|[^\w])(429|500|502|503|504)(?:$|[^\w])|overloaded|temporarily unavailable|rate.?limit', text, re.I))
