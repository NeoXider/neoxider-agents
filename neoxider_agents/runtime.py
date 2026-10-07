"""One provider tree, in-process log pump and interruptible watchdog/control polling."""
import json
import os
import signal
import threading
import time
from collections import deque
from .state import Lock, atomic_write, MARK
from .reporting import now


def number_env(key, default):
    try:
        return max(0, float(os.environ.get(key, default)))
    except ValueError:
        return default


def pipe_closed():
    from .process import stdout_closed
    return stdout_closed()


def retry_wait(turn, delay):
    if delay <= 0:
        return True
    from .process import ControlSignal, pid_alive, pid_stamp
    parent_stamp = pid_stamp(turn.parent_pid)
    deadline = time.monotonic() + delay
    handlers = {}
    for key in ("SIGTERM", "SIGHUP", "SIGINT", "SIGBREAK"):
        sig = getattr(signal, key, None)
        if sig is not None:
            handlers[sig] = signal.signal(sig, turn._signal)
    try:
        with ControlSignal(turn.name) as control:
            while time.monotonic() < deadline:
                meta = turn.store.read(turn.name)
                try:
                    request = json.loads(turn.store.path(turn.name, ".control").read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    request = {}
                if meta.get("state") == "stopped" or (request.get("generation") == meta.get("generation") and request.get("action") == "stop"):
                    turn.action, turn.reason, turn.by = "stop", request.get("reason", meta.get("reason", "stopped by orchestrator")), request.get("by", "orchestrator")
                elif pipe_closed() or (meta.get("wait_pid") and not pid_alive(meta["wait_pid"], meta.get("wait_start", ""))) or (not os.environ.get("AGENT_DETACHED") and not pid_alive(turn.parent_pid, parent_stamp)):
                    turn.action, turn.reason, turn.by = "stop", "launcher stopped", "user"
                if turn.action == "stop":
                    return False
                control.wait(min(0.25, max(0, deadline - time.monotonic())))
        return True
    finally:
        for sig, old in handlers.items():
            signal.signal(sig, old)


class Turn:
    def __init__(self, store, name, provider, model, effort, directory, session, prompt, terminal=False):
        self.store, self.name = store, name
        self.provider, self.model, self.effort = provider, model, effort
        self.directory, self.session, self.prompt = directory, session, prompt
        self.terminal = terminal
        self.action = None
        self.reason = ""
        self.by = ""
        self.output = deque(maxlen=32)
        self.last_activity = ""
        self.last_output_time = time.monotonic()
        self.tree = None
        self.wake = threading.Event()
        self.last_meta_time = 0
        self.parent_pid = int(os.environ.get("AGENT_LAUNCHER_PID") or os.getppid())
        if os.name == "nt" and not os.environ.get("AGENT_LAUNCHER_PID"):
            from .windows import processes
            ancestry = {pid: (parent, exe.lower()) for pid, parent, exe in processes()}
            while self.parent_pid in ancestry and ancestry[self.parent_pid][1] in ("py.exe", "pyw.exe"):
                self.parent_pid = ancestry[self.parent_pid][0]

    def _signal(self, signum, frame):
        self.action, self.reason, self.by = "stop", "launcher stopped", "user"
        self.wake.set()

    def _pump(self, stream, log, output_filter):
        pending = b""
        while True:
            chunk = stream.read1(16384) if hasattr(stream, "read1") else stream.read(16384)
            if not chunk:
                break
            self.last_output_time = time.monotonic()
            pending += chunk
            pieces = pending.split(b"\n")
            pending = pieces.pop()
            for piece in pieces:
                self._line(piece.decode("utf-8", "replace") + "\n", log, output_filter)
        if pending:
            self._line(pending.decode("utf-8", "replace"), log, output_filter)

    def _line(self, line, log, output_filter):
        if line.endswith("\r\n"):
            line = line[:-2] + "\n"
        for fragment in output_filter.feed(line):
            log.write(fragment)
            self.output.append(fragment[-4096:])
            if fragment.strip():
                self.last_activity = fragment.strip().splitlines()[-1][:500]
        if output_filter.events:
            kind, detail = output_filter.events[-1]
            self.last_activity = "%s %s" % (kind, detail)
            log.write("[agent-activity] " + self.last_activity + "\n")
        log.flush()
        sid = output_filter.session
        if sid and sid != self.session:
            self.session = sid
            self.store.update(self.name, session=sid)
        # Filter activity sidecar is redacted and decoded in-process.
        if time.monotonic() - self.last_meta_time >= 0.5:
            self.store.update(self.name, activity_epoch=time.time(), last_activity=" ".join(self.last_activity.split())[:500],
                              partial_result=" ".join(output_filter.last_assistant[-1200:].split()))
            self.last_meta_time = time.monotonic()

    def run(self):
        from .process import spawn, ControlSignal, pid_stamp, pid_alive
        from .output import OutputFilter
        from .heartbeat import ToolHeartbeat
        prompt_path = self.store.path(self.name, ".prompt")
        atomic_write(prompt_path, self.prompt)
        argv, overrides = self.provider.command(self.model, self.effort, self.directory,
                                               session=self.session, chat_only=os.environ.get("AGENT_CHAT_ONLY") == "1")
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1",
                   PWD=self.directory, AGENT_ACTIVITY_FILE=str(self.store.path(self.name, ".activity.jsonl")), AGENT_TASK=self.name)
        env.update(overrides)
        signals = {}
        for key in ("SIGTERM", "SIGHUP", "SIGINT", "SIGBREAK"):
            sig = getattr(signal, key, None)
            if sig is not None:
                signals[sig] = signal.signal(sig, self._signal)
        output_filter = OutputFilter(self.provider.engine, task=self.name, logs=self.store.root)
        previous_activity_file = os.environ.get("AGENT_ACTIVITY_FILE")
        os.environ["AGENT_ACTIVITY_FILE"] = str(self.store.path(self.name, ".activity.jsonl"))
        started = time.monotonic()
        parent_stamp = pid_stamp(self.parent_pid)
        code = 0
        control = ControlSignal(self.name)
        try:
            with Lock(self.store.path(self.name, ".inbox")):
                meta = self.store.read(self.name)
                try:
                    request = json.loads(self.store.path(self.name, ".control").read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    request = {}
                if meta.get("state") == "stopped" or (request.get("generation") == meta.get("generation") and request.get("action") == "stop"):
                    self.action, self.reason, self.by = "stop", request.get("reason", meta.get("reason", "stopped by orchestrator")), request.get("by", "orchestrator")
                    return 130, output_filter, self.action, self.reason, self.by
                self.tree = spawn(argv, str(prompt_path), self.directory, env, terminal=self.terminal)
            for sig in signals:
                signal.signal(sig, self._signal)
            self.store.update(self.name, provider_pid=self.tree.pid, provider_start=self.tree.stamp,
                              job_name=self.tree.job_name, control_event=getattr(control, "name", self.name))
            heartbeat = ToolHeartbeat(self.provider.engine, self.tree.pid)
            with self.store.path(self.name, ".log").open("a", encoding="utf-8", newline="\n") as log:
                pump = threading.Thread(target=self._pump, args=(self.tree.process.stdout, log, output_filter), daemon=True)
                pump.start()
                previous = None
                interval = 0.1
                while self.tree.process.poll() is None:
                    own_meta = self.store.read(self.name)
                    if own_meta.get("state") == "stopped":
                        self.action, self.reason, self.by = "stop", own_meta.get("reason", "stopped by orchestrator"), own_meta.get("stopped_by", "orchestrator")
                    try:
                        stat = self.store.path(self.name, ".control").stat()
                        version = stat.st_mtime_ns
                        if version != previous:
                            previous = version
                            with Lock(self.store.path(self.name, ".inbox")):
                                request = json.loads(self.store.path(self.name, ".control").read_text(encoding="utf-8"))
                                self.store.path(self.name, ".control").unlink()
                                if request.get("generation") == self.store.read(self.name).get("generation"):
                                    self.action = request.get("action")
                                    self.reason = request.get("reason", "stopped by orchestrator")
                                    self.by = request.get("by", "orchestrator")
                            interval = 0.1
                    except (OSError, ValueError):
                        pass
                    elapsed = time.monotonic() - started
                    activity = heartbeat.poll()
                    if activity:
                        self.last_output_time = time.monotonic()
                        self.last_activity = activity
                        log.write("[agent-activity] " + activity + "\n")
                        log.flush()
                        self.store.update(self.name, activity_epoch=time.time(), last_activity=activity)
                    timeout = number_env("AGENT_TIMEOUT_SEC", 1800)
                    if self.provider.engine == "opencode":
                        timeout = number_env("AGENT_OPENCODE_TIMEOUT_SEC", timeout)
                    silence = number_env("AGENT_SILENCE_SEC", 600)
                    if timeout and elapsed >= timeout:
                        self.action, self.reason, self.by, code = "stop", "step watchdog timeout", "watchdog", 124
                        log.write("!! TIMEOUT: step exceeded AGENT_TIMEOUT_SEC=%ss and was killed\n" % timeout)
                    elif silence and time.monotonic() - self.last_output_time >= silence:
                        self.action, self.reason, self.by, code = "stop", "no output for %ss (AGENT_SILENCE_SEC)" % silence, "watchdog", 125
                        log.write("!! SILENT: " + self.reason + "\n")
                    elif output_filter.provider_error:
                        self.action, self.reason, self.by, code = "failure", output_filter.provider_error, "watchdog", 126
                    elif pipe_closed() or (own_meta.get("wait_pid") and not pid_alive(own_meta["wait_pid"], own_meta.get("wait_start", ""))) or (not os.environ.get("AGENT_DETACHED") and not pid_alive(self.parent_pid, parent_stamp)):
                        self.action, self.reason, self.by = "stop", "launcher stopped", "user"
                    if self.action:
                        self.tree.kill()
                        break
                    control.wait(interval)
                    if self.wake.is_set():
                        continue
                    interval = min(0.5, interval + 0.05)
                try:
                    self.tree.process.wait(timeout=5)
                except Exception:
                    self.tree.kill()
                # Kill descendants retaining the pipe before waiting for pump EOF.
                pump.join(0.25)
                if pump.is_alive():
                    self.tree.kill()
                    pump.join(2)
                for fragment in output_filter.finish():
                    log.write(fragment)
                log.flush()
            if not code:
                code = self.tree.process.returncode or 0
            return code, output_filter, self.action, self.reason, self.by
        finally:
            if self.tree:
                self.tree.close()
            control.close()
            if previous_activity_file is None:
                os.environ.pop("AGENT_ACTIVITY_FILE", None)
            else:
                os.environ["AGENT_ACTIVITY_FILE"] = previous_activity_file
            for sig, old in signals.items():
                signal.signal(sig, old)
            try:
                prompt_path.unlink()
            except OSError:
                pass
