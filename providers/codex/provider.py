"""Codex isolated execution, explicit resume model and persistent writer lock."""
import errno
import os
from pathlib import Path
import re
from threading import Event
import time
from neoxider_agents.providers import BaseProvider, CONFLICT, capture, executable


class ResumeBusy(ValueError):
    code = 75


class Provider(BaseProvider):
    engine = "codex"
    retry_limit = 1

    def resolve(self, model="", effort=""):
        aliases = {"": "gpt-5.6-terra", "5.6": "gpt-5.6-terra", "5.6-terra": "gpt-5.6-terra",
                   "terra": "gpt-5.6-terra", "default": "gpt-5.6-terra", "5.6-sol": "gpt-5.6-sol", "sol": "gpt-5.6-sol",
                   "5.6-high": "gpt-5.6-sol", "high": "gpt-5.6-sol", "5.6-luna": "gpt-5.6-luna", "luna": "gpt-5.6-luna",
                   "6-sol": "gpt-6-sol", "sol6": "gpt-6-sol", "gpt6-sol": "gpt-6-sol",
                   "6.1-sol": "gpt-6.1-sol", "sol61": "gpt-6.1-sol", "gpt61-sol": "gpt-6.1-sol",
                   "6-luna": "gpt-6-luna", "luna6": "gpt-6-luna", "gpt6-luna": "gpt-6-luna",
                   "spark": "gpt-5.3-codex-spark", "5.3": "gpt-5.3-codex-spark", "5.3-spark": "gpt-5.3-codex-spark",
                   "codex-spark": "gpt-5.3-codex-spark"}
        return aliases.get(model, model), effort or ("high" if model in ("high", "5.6-high") else "medium")

    def command(self, model, effort, cwd, session="", chat_only=False):
        import json
        chat_only = chat_only or os.environ.get("AGENT_CHAT_ONLY") == "1"
        args = executable(self.engine) + ["exec"]
        if session:
            args += ["resume"]
        args += ["-m", model]
        if effort:
            args += ["-c", "model_reasoning_effort=" + json.dumps(effort)]
        sandbox = "read-only" if chat_only else os.environ.get("AGENT_CODEX_SANDBOX", "danger-full-access")
        args += ["-c", "sandbox_mode=" + json.dumps(sandbox)] if session else ["--sandbox", sandbox]
        if chat_only or os.environ.get("AGENT_CODEX_USER_CONFIG") != "1":
            args += ["--ignore-user-config"]
            if not chat_only:
                for item in os.environ.get("AGENT_CODEX_MCP", "").split(","):
                    name, sep, url = item.strip().partition("=")
                    if sep and url and re.fullmatch(r"[A-Za-z0-9_-]+", name):
                        args += ["-c", "mcp_servers.%s.url=%s" % (name, json.dumps(url))]
                    elif item.strip():
                        import sys
                        print("neoxider: AGENT_CODEX_MCP: skipping malformed entry (want name=url, ASCII server name)", file=sys.stderr)
        args += ["--skip-git-repo-check"]
        if not session:
            args += ["-C", str(cwd)]
        if chat_only:
            for image in os.environ.get("AGENT_CODEX_IMAGE_PATHS", "").splitlines():
                if image and os.path.isfile(image):
                    args += ["-i", image]
        args += ["--json"]
        return args + ([session, "-"] if session else ["-"]), {}

    @staticmethod
    def writer_held(session):
        lock = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "thread-writer-locks" / (session + ".lock")
        try:
            stream = lock.open("r+b", buffering=0)
        except FileNotFoundError:
            return False
        except OSError:
            return None
        with stream:
            try:
                if os.name == "nt":
                    import msvcrt
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                return False
            except OSError as exc:
                return True if exc.errno in (errno.EACCES, errno.EAGAIN, getattr(errno, "EDEADLK", -1)) else None

    def prepare_resume(self, session, cwd):
        value = os.environ.get("AGENT_CODEX_WRITER_WAIT_SEC", os.environ.get("AGENT_TIMEOUT_SEC", "1800"))
        seconds = int(value) if value.isdigit() else 1800
        start = time.monotonic()
        sleeper = Event()
        pause = 0.1
        while session:
            held = self.writer_held(session)
            if held is None:
                return "Codex thread-writer lock probe unavailable; relying on conflict detection/retry"
            if not held:
                elapsed = int(time.monotonic() - start)
                return "Codex thread writer released after %ss; resume continuing" % elapsed if elapsed else ""
            remaining = seconds - (time.monotonic() - start)
            if remaining <= 0:
                raise ResumeBusy("Codex thread writer still held after bounded %ss wait; no resume was started" % seconds)
            sleeper.wait(min(pause, remaining))
            pause = min(0.5, pause * 2)
        return ""

    def retry_reason(self, text, code, session=""):
        return "Codex thread-writer conflict" if session and CONFLICT.search(text) else ""

    def retry_delay(self, attempt):
        return 0

    def doctor(self):
        info = super().doctor()
        info["note"] = ""
        if info["available"]:
            _, login = capture(executable(self.engine) + ["login", "status"])
            info["login"] = login.splitlines()[0] if login else ""
            info["limits"] = self._limits()
        return info

    @staticmethod
    def _limits():
        import glob
        import json
        from neoxider_agents.state import tail
        root = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        files = sorted(glob.iglob(str(root / "sessions/**/*.jsonl"), recursive=True), key=os.path.getmtime)[-8:]

        def find(value):
            if isinstance(value, dict):
                if value.get("rate_limits"):
                    return value["rate_limits"]
                for nested in value.values():
                    found = find(nested)
                    if found:
                        return found
            return None
        limits = None
        for path in files:
            for line in tail(path, 1048576).splitlines():
                if '"rate_limits"' in line:
                    try:
                        limits = find(json.loads(line)) or limits
                    except ValueError:
                        continue
        return limits
