"""Native Windows tool activity heartbeat; no polling subprocesses."""
import math
import os
import time

TOOLS = frozenset(name.lower() for name in (
    "dotnet", "MSBuild", "csc", "VBCSCompiler", "git", "python", "python3", "pwsh",
    "robocopy", "unzip", "cl", "link", "ninja", "make", "curl", "cargo", "node", "npm",
    "ilspycmd", "Cpp2IL"))


class ToolHeartbeat:
    def __init__(self, engine, provider_pid, clock=None, snapshot=None, windows=None):
        native = os.name == "nt" if windows is None else windows
        self.enabled = native and engine == "opencode" and os.environ.get("AGENT_OPENCODE_TOOL_KEEPALIVE", "1") == "1"
        self.pid = int(provider_pid)
        self.clock = clock or time.monotonic
        self.snapshot = snapshot
        try:
            period = float(os.environ.get("AGENT_OPENCODE_KEEPALIVE_SEC", "60"))
        except ValueError:
            period = 60
        self.period = max(0.1, period) if math.isfinite(period) and period >= 0 else 60
        self.previous = self.clock()

    def poll(self):
        if not self.enabled:
            return ""
        now = self.clock()
        if now - self.previous < self.period:
            return ""
        self.previous = now
        try:
            if self.snapshot is None:
                from .windows import processes
                rows = processes()
            else:
                rows = self.snapshot()
        except OSError:
            return ""
        children, names = {}, {}
        for pid, parent, name in rows:
            children.setdefault(parent, []).append(pid)
            names[pid] = str(name).replace("\\", "/").rsplit("/", 1)[-1].lower()
            if names[pid].endswith(".exe"):
                names[pid] = names[pid][:-4]
        descendants, todo = set(), list(children.get(self.pid, []))
        while todo:
            pid = todo.pop()
            if pid == self.pid or pid in descendants:
                continue
            descendants.add(pid)
            todo.extend(children.get(pid, []))
        count = sum(names.get(pid) in TOOLS for pid in descendants)
        return "[opencode] activity: tool running (%s helper processes)" % count if count else ""
