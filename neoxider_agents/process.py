"""Hidden provider launches, process identity and cancellation without polling forks."""
import atexit
import hashlib
import os
import signal
import subprocess
import sys
import threading
import weakref
from pathlib import Path

_trees = weakref.WeakSet()
_events = weakref.WeakSet()
_signals_installed = False


class LauncherStopped(KeyboardInterrupt):
    """Lets the owning command persist a stopped result after launcher cancellation."""


def hidden_kwargs(terminal=False, executable=""):
    if os.name != "nt":
        return {}
    # A detached process has no console, so every console child it starts (node -> codex.exe) opens a new
    # visible window in the default terminal. A hidden console is inherited by the whole tree instead.
    flags = subprocess.CREATE_NEW_PROCESS_GROUP
    result = {"creationflags": flags}
    if not terminal:
        result["creationflags"] |= subprocess.CREATE_NO_WINDOW
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = subprocess.SW_HIDE
        result["startupinfo"] = startup
    return result


def pid_stamp(pid):
    try:
        pid = int(pid)
        if pid <= 0:
            return ""
        if os.name == "nt":
            from .windows import pid_stamp as windows_stamp
            return windows_stamp(pid)
        path = Path("/proc/%s/stat" % pid)
        if path.exists():
            fields = path.read_text().rsplit(")", 1)[1].split()
            return fields[19]
        if sys.platform == "darwin":
            import ctypes
            class BsdInfo(ctypes.Structure):
                _fields_ = [("prefix", ctypes.c_byte * 120),
                            ("seconds", ctypes.c_uint64), ("microseconds", ctypes.c_uint64)]
            info = BsdInfo()
            lib = ctypes.CDLL("/usr/lib/libproc.dylib")
            size = lib.proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info))
            if size == ctypes.sizeof(info):
                return "%s.%s" % (info.seconds, info.microseconds)
    except (OSError, ValueError, TypeError, IndexError):
        pass
    return ""


def pid_alive(pid, stamp=""):
    try:
        pid = int(pid)
        if pid <= 0:
            return False
        if os.name == "nt":
            from .windows import pid_alive as windows_alive
            return windows_alive(pid, stamp)
        if stamp and pid_stamp(pid) != str(stamp):
            return False
        os.kill(pid, 0)
        try:
            if Path("/proc/%s/stat" % pid).read_text().rsplit(")", 1)[1].split()[0] == "Z":
                return False
        except OSError:
            pass
        return True
    except PermissionError:
        return True
    except (OSError, ValueError, TypeError):
        return False


def _posix_rows():
    rows = []
    if Path("/proc").is_dir():
        for entry in os.scandir("/proc"):
            if entry.name.isdigit():
                try:
                    fields = (Path(entry.path) / "stat").read_text().rsplit(")", 1)[1].split()
                    rows.append((int(entry.name), int(fields[1]), fields[19]))
                except (OSError, IndexError, ValueError):
                    pass
        return rows
    if sys.platform == "darwin":
        import ctypes
        lib = ctypes.CDLL("/usr/lib/libproc.dylib")
        lib.proc_listpids.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_int]
        size = lib.proc_listpids(1, 0, None, 0)
        buffer = (ctypes.c_int32 * (max(0, size) // 4 + 1024))()
        size = lib.proc_listpids(1, 0, buffer, ctypes.sizeof(buffer))
        for pid in buffer[:max(0, size) // 4]:
            if pid > 0:
                info = (ctypes.c_byte * 136)()
                if lib.proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info)) == 136:
                    data = bytes(info)
                    parent = int.from_bytes(data[16:20], sys.byteorder)
                    seconds = int.from_bytes(data[120:128], sys.byteorder)
                    micros = int.from_bytes(data[128:136], sys.byteorder)
                    rows.append((pid, parent, "%s.%s" % (seconds, micros)))
    return rows


def _kill_posix_tree(pid, stamp=""):
    expected = stamp or pid_stamp(pid)
    rows = _posix_rows()
    owned = {pid: expected}
    while True:
        added = {child: fingerprint for child, parent, fingerprint in rows if parent in owned and child not in owned}
        if not added:
            break
        owned.update(added)
    if expected and pid_stamp(pid) != str(expected):
        return False
    killed = False
    # A legacy launcher may share the caller's group. Never signal that whole group.
    for child, fingerprint in sorted(owned.items(), key=lambda item: item[0] != pid):
        if fingerprint and pid_stamp(child) != str(fingerprint):
            continue
        try:
            os.kill(child, signal.SIGKILL)
            killed = True
        except OSError:
            pass
    return killed


def kill_tree(pid, winpid="", stamp="", job_name=""):
    try:
        pid = int(pid)
        if pid <= 0:
            return False
        if os.name == "nt":
            from .windows import kill_tree as windows_kill
            if winpid and not str(winpid).isdigit():
                job_name = job_name or str(winpid)
            elif winpid:
                pid = int(winpid)
            return windows_kill(pid, stamp, job_name)
        if not pid_alive(pid, stamp):
            return False
        group = os.getpgid(pid)
        if group == pid:
            os.killpg(group, signal.SIGKILL)
            return True
        return _kill_posix_tree(pid, stamp)
    except (OSError, ValueError, TypeError):
        return False


def _shutdown():
    for tree in list(_trees):
        tree.kill()


def _cancel(signum, unused_frame):
    _shutdown()
    raise LauncherStopped("launcher stopped (signal %s)" % signum)


def _install_signals():
    global _signals_installed
    if _signals_installed or threading.current_thread() is not threading.main_thread():
        return
    for name in ("SIGTERM", "SIGHUP", "SIGBREAK"):
        value = getattr(signal, name, None)
        if value is not None:
            signal.signal(value, _cancel)
    atexit.register(_shutdown)
    _signals_installed = True


class ProcessTree:
    def __init__(self, process, job=None):
        self.process = process
        self.pid = process.pid
        self.stamp = pid_stamp(self.pid)
        self.job = job
        self.job_name = job.name if job else ""
        self._closed = False
        _trees.add(self)

    def kill(self):
        if self._closed:
            return
        if self.job:
            try:
                self.job.kill()
            except OSError:
                kill_tree(self.pid, stamp=self.stamp)
        else:
            current = pid_stamp(self.pid)
            if current and self.stamp and current != self.stamp:
                return
            try:
                # Our setsid group can outlive its original provider process.
                os.killpg(self.pid, signal.SIGKILL)
            except OSError:
                pass

    def close(self):
        if self._closed:
            return
        self.kill()
        if self.job:
            self.job.close()
        self._closed = True
        _trees.discard(self)


def spawn(argv, prompt_file, cwd=None, env=None, terminal=False):
    """Prompt bytes go to stdin; the provider never receives prompt text in argv."""
    _install_signals()
    options = hidden_kwargs(terminal, argv[0])
    job = None
    if os.name == "nt":
        from .windows import Job, ensure_lifetime_job, resume
        ensure_lifetime_job()
        job = Job()
        options["creationflags"] |= 4  # CREATE_SUSPENDED: assign before user code runs.
    else:
        options["start_new_session"] = True
    process = None
    try:
        with open(str(prompt_file), "rb") as prompt:
            process = subprocess.Popen(argv, stdin=prompt, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, cwd=cwd, env=env,
                                       close_fds=True, **options)
        if job:
            job.assign(process._handle)
            resume(process.pid)
        return ProcessTree(process, job)
    except BaseException:
        if process:
            process.kill()
            process.wait()
            if process.stdout:
                process.stdout.close()
        if job:
            job.close()
        raise


def _wake(unused_signum, unused_frame):
    for event in list(_events):
        event._event.set()


class ControlSignal:
    def __init__(self, name):
        key = hashlib.sha256(str(name).encode("utf-8")).hexdigest()[:32]
        self.name = "Local\\NeoxiderControl-" + key if os.name == "nt" else "neoxider-" + key
        if os.name == "nt":
            from .windows import NamedEvent
            self._event = NamedEvent(self.name)
        else:
            self._event = threading.Event()
            _events.add(self)
            if threading.current_thread() is threading.main_thread():
                signal.signal(signal.SIGUSR1, _wake)

    def wait(self, timeout=0.5):
        result = self._event.wait(timeout)
        if os.name != "nt":
            self._event.clear()
        return result

    def set(self):
        self._event.set()

    def close(self):
        if os.name == "nt":
            self._event.close()
        else:
            _events.discard(self)

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        self.close()


def signal_task(meta):
    name = meta.get("control_event") or meta.get("control_signal") or ""
    if not name:
        return False
    if os.name == "nt":
        from .windows import signal_event
        return signal_event(name)
    pid = meta.get("pid", "")
    stamp = meta.get("pid_start") or meta.get("pid_stamp", "")
    if not pid_alive(pid, stamp):
        return False
    try:
        os.kill(int(pid), signal.SIGUSR1)
        return True
    except OSError:
        return False


def stdout_closed():
    if getattr(sys.stdout, "closed", False):
        return True
    try:
        fd = sys.stdout.fileno()
    except (OSError, ValueError, AttributeError):
        return False
    try:
        if os.name == "nt":
            import msvcrt
            from .windows import pipe_disconnected
            return pipe_disconnected(msvcrt.get_osfhandle(fd))
        import select
        if hasattr(select, "poll"):
            poller = select.poll()
            poller.register(fd, select.POLLERR | select.POLLHUP)
            return bool(poller.poll(0))
        if hasattr(select, "kqueue"):
            with select.kqueue() as queue:
                change = select.kevent(fd, filter=select.KQ_FILTER_WRITE,
                                       flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT)
                events = queue.control([change], 1, 0)
                return any(event.flags & select.KQ_EV_EOF for event in events)
        return False
    except (OSError, ValueError, AttributeError):
        return True
