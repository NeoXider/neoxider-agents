"""Lazy provider plugins and native executable discovery (never a command shell)."""
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
ENGINES = ("codex", "claude", "kimi", "opencode", "gemini")
FAILURE = re.compile(r"usage limit|rate.?limit|quota exceeded|insufficient (quota|credit)|payment required|requires a newer version of|unrecognized_model|issue with the selected model|model[^.?!]*(not found|unavailable|unknown)|unauthoriz|invalid api key|authentication (failed|error)|token expired|session expired|not logged in|please (run|log ?in)", re.I)
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
        process = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, timeout=timeout, **hidden_options())
        return process.returncode, process.stdout.decode("utf-8", "replace").strip()
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""


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
        except (OSError, ValueError):
            return {"engine": self.engine, "version": "NOT_FOUND", "available": False,
                    "login": "", "limits": None, "note": ""}
        _, version = capture(base + ["--version"])
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


def is_transient_failure(text):
    if re.search(r"unauthorized|invalid api key|authentication|forbidden|(?:^|[^\w])model .*(not found|unavailable|unknown)|insufficient (quota|credit)|quota exceeded|payment required", text, re.I):
        return False
    return bool(re.search(r'"isretryable"\s*:\s*true|network_error|providerresponsestreamerror|stream (error|closed|interrupted)|econnreset|etimedout|enotfound|socket hang up|(?:^|[^\w])(429|500|502|503|504)(?:$|[^\w])|overloaded|temporarily unavailable|rate.?limit', text, re.I))
