"""Windows prerequisites and experimental copied console process boundary.

The probe is read-only. The experimental session creates only uniquely owned
profiles/desktops/private-copy ACLs. No original or shared ACL is changed.
API availability is not acceptance and never permits unrestricted fallback.
"""
import os
import platform
import threading
import time
from dataclasses import dataclass
from functools import wraps


_APIS = {
    "kernel32": ("CreateProcessW", "InitializeProcThreadAttributeList",
                 "UpdateProcThreadAttribute", "CreateJobObjectW",
                 "AssignProcessToJobObject", "TerminateJobObject"),
    "userenv": ("CreateAppContainerProfile", "DeleteAppContainerProfile",
                "DeriveAppContainerSidFromAppContainerName"),
    "user32": ("CreateDesktopW", "CloseDesktop"),
    "advapi32": ("GetNamedSecurityInfoW", "SetNamedSecurityInfoW"),
}


def probe():
    """Return JSON-compatible prerequisite evidence without security changes."""
    result = {"platform": platform.system(), "backend": "windows-appcontainer-write-restricted-detached",
              "supported": False, "api_presence": {}, "errors": [],
              "reason": "independent-acceptance-pending",
              "missing_acceptance": [
                  "private-desktop-and-handle-allowlist",
                  "inherited-child-outside-write-denial",
                  "everyone-writable-outside-denial",
                  "ambient-aap-aar-outside-denial-independent-acceptance",
                  "detached-runtime-compatibility-contract",
                  "token-process-and-local-broker-isolation",
                  "link-rename-and-safe-writeback-gates"]}
    if os.name != "nt":
        result["reason"] = "windows-required"
        return result
    import ctypes
    for library, names in _APIS.items():
        try:
            dll = ctypes.WinDLL(library, use_last_error=True)
        except OSError as exc:
            result["errors"].append({"library": library,
                                     "winerror": getattr(exc, "winerror", None)})
            for name in names:
                result["api_presence"][library + "." + name] = False
            continue
        for name in names:
            result["api_presence"][library + "." + name] = hasattr(dll, name)
    if not all(result["api_presence"].values()):
        result["reason"] = "required-windows-api-unavailable"
    return result


def _locked(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return call


@dataclass(frozen=True)
class ConsoleBudgets:
    staging_bytes: int = 256 * 1024 * 1024
    staging_files: int = 10000
    staging_depth: int = 64
    staging_seconds: int = 60
    active_processes: int = 8
    process_memory_bytes: int = 512 * 1024 * 1024
    job_memory_bytes: int = 1024 * 1024 * 1024

    def validate(self):
        from .enforcement import UnsafeScope
        for name,value in self.__dict__.items():
            if type(value) is not int or value<=0:
                raise UnsafeScope("budget","Positive integer resource budgets required.")
        if (self.active_processes>64 or self.staging_files>100000 or self.staging_depth>128 or
                self.staging_seconds>300 or self.staging_bytes>8*1024**3 or
                self.process_memory_bytes>8*1024**3 or self.job_memory_bytes>16*1024**3):
            raise UnsafeScope("budget","Experimental resource budget ceiling exceeded.")
        return self


class _StageMeter:
    def __init__(self, budgets, started, root):
        self.budgets, self.started, self.root = budgets, started, root
        self.entries = self.bytes = 0

    def check_time(self):
        from .enforcement import UnsafeScope
        if time.monotonic() - self.started >= self.budgets.staging_seconds:
            raise UnsafeScope("staging-budget", "Staging time budget exceeded.")

    def visit(self, path):
        from .enforcement import UnsafeScope
        import stat
        self.check_time()
        self.entries += 1
        info = path.lstat()
        if stat.S_ISREG(info.st_mode):
            self.bytes += info.st_size
        if (self.entries > self.budgets.staging_files or
                self.bytes > self.budgets.staging_bytes or
                len(path.relative_to(self.root).parts) > self.budgets.staging_depth):
            raise UnsafeScope("staging-budget", "Aggregate staging inspection budget exceeded.")


class WindowsConsoleSession:
    """EXPERIMENTAL console-only native session; public enforcement stays off.

    Materializes pinned copies, never writes back to the original workspace.
    Root must review/test this boundary before wiring it into provider launch.
    Network/credentials and non-Windows platforms currently refuse.
    """

    @classmethod
    def prepare(cls, request, runtime_files, executable, private_parent, budgets=None):
        from .enforcement import (validate_request, _checked_path,
                                  EnforcementUnavailable, UnsafeScope)
        from pathlib import Path
        import uuid
        import stat
        if os.name != "nt":
            raise EnforcementUnavailable("windows-required", "Windows required.")
        if budgets is not None and not isinstance(budgets, ConsoleBudgets):
            raise UnsafeScope("budget", "Expected ConsoleBudgets.")
        budgets = (budgets or ConsoleBudgets()).validate()
        started = time.monotonic()
        from .enforcement import ScopeRequest
        meter = (_StageMeter(budgets, started, Path(request.workspace))
                 if isinstance(request, ScopeRequest) and isinstance(request.workspace, str) else None)
        request = validate_request(request, _visit=meter.visit if meter else None)
        if request.network or request.credentials:
            raise EnforcementUnavailable("capability-not-accepted",
                                         "Network and credential grants are not accepted.")
        if not isinstance(runtime_files, dict) or executable not in runtime_files:
            raise UnsafeScope("runtime-required", "Named pinned runtime executable required.")
        parent = Path(private_parent)
        if not stat.S_ISDIR(_checked_path(parent).st_mode):
            raise UnsafeScope("private-parent", "Existing private parent directory required.")
        source_root = Path(os.path.realpath(request.workspace))
        parent = Path(os.path.realpath(parent))
        if (source_root==parent or source_root in parent.parents or parent in source_root.parents):
            raise UnsafeScope("overlapping-roots","Source and private parent must be disjoint.")
        self = cls()
        self._lock = threading.RLock()
        self.budgets=budgets
        self._staging_bytes=0
        self._staging_files=3  # Session root, runtime and temp; workspace counted below.
        self._staging_started=started
        self._stage_meter=_StageMeter(budgets, started, Path(request.workspace))
        self._api = _Native()
        self._handles = []
        self._profile = None
        self._sid = None
        self._restriction_sid = None
        self._desktop = None
        self._job = None
        self._process = None
        self._exit_code = None
        self.stdin = self.stdout = None
        self._owned_streams = ()
        self.pid = None
        self.name = "neoxider-console-" + uuid.uuid4().hex
        self.root = parent / self.name
        self.root.mkdir()  # Must be a new owned directory; never reuse.
        self.workspace = self.root / "workspace"
        self.runtime = self.root / "runtime"
        self.temp = self.root / "temp"
        self.evidence = {"experimental": True, "source_files": {},
                         "runtime_files": {}, "writable_infrastructure": [],
                         "backend": "windows-appcontainer-write-restricted-detached",
                         "auto_writeback": False, "network": False,
                         "credentials": False}
        self.evidence["budgets"]=dict(self.budgets.__dict__)
        self._runtime_bindings = []
        try:
            for folder in (self.workspace, self.runtime, self.temp):
                folder.mkdir()
            self._source_snapshot(Path(request.workspace), self.workspace)
            import hashlib
            for relative, binding in runtime_files.items():
                self._safe_relative(relative)
                if len(Path(relative).parts) > self.budgets.staging_depth:
                    raise UnsafeScope("staging-depth", "Runtime path exceeds depth budget.")
                if not isinstance(binding, tuple) or len(binding) != 2:
                    raise UnsafeScope("runtime-pin", "Runtime binding must be (source, sha256).")
                source, expected = binding
                if not isinstance(source, str) or not Path(source).is_absolute():
                    raise UnsafeScope("runtime-source", "Absolute runtime source path required.")
                if not isinstance(expected, str) or len(expected) != 64:
                    raise UnsafeScope("runtime-pin", "Runtime SHA256 required.")
                self._consume_stage(Path(source))
                target = self.runtime / relative
                directory = self.runtime
                for component in Path(relative).parent.parts:
                    directory = directory / component
                    if not directory.exists():
                        self._consume_stage(None)
                        directory.mkdir()
                unused,identity=self._api.read_nofollow(Path(source),destination=target,
                    max_bytes=self.budgets.staging_bytes-self._staging_bytes,
                    check_budget=self._stage_meter.check_time)
                digest=identity["sha256"]
                self._staging_bytes+=identity["captured_bytes"]
                if digest.lower() != expected.lower():
                    raise UnsafeScope("runtime-pin-mismatch", "Runtime source hash changed.")
                self.evidence["runtime_files"][relative] = {
                    "source": str(source), "sha256": digest, "identity": identity}
                self._runtime_bindings.append((relative, digest))
            self.executable = self.runtime / executable
            self._sid, self._sid_text = self._api.create_profile(self.name, owner=self)
            self._profile = self.name
            self._restriction_sid,self._restriction_text=self._api.create_normal_sid()
            self.evidence["restricting_sid"]=self._restriction_text
            self._api.permissions(self.root, self._sid_text, False,self._restriction_text)
            # Private snapshots are readonly except explicitly granted exact scopes.
            for base, dirs, files in os.walk(str(self.root)):
                for name in dirs + files:
                    self._stage_meter.check_time()
                    self._api.permissions(Path(base) / name, self._sid_text, False,self._restriction_text)
            for relative in request.writable:
                self._stage_meter.check_time()
                target = self.workspace / relative
                self._api.permissions(target, self._sid_text, True,self._restriction_text)
                if target.is_dir():
                    for base, dirs, files in os.walk(str(target)):
                        for name in dirs + files:
                            self._api.permissions(Path(base) / name, self._sid_text, True,self._restriction_text)
            self._api.permissions(self.temp, self._sid_text, True,self._restriction_text)
            self.evidence["writable_infrastructure"].append(str(self.temp))
            self.profile_path = self._api.profile_path(self._sid_text)
            self._api.permissions(Path(self.profile_path),self._sid_text,True,self._restriction_text)
            for base,dirs,files in os.walk(self.profile_path):
                for name in dirs+files:
                    self._api.permissions(Path(base)/name,self._sid_text,True,self._restriction_text)
            self.evidence["writable_infrastructure"].append(self.profile_path)
            self._desktop, self._desktop_name = self._api.create_desktop(
                self.name, self._sid_text,self._restriction_text)
            self._runtime_bindings = tuple(self._runtime_bindings)
            return self
        except BaseException:
            self._close_after_failure()
            raise

    @staticmethod
    def _safe_relative(relative):
        from .enforcement import UnsafeScope
        from pathlib import Path
        if (not isinstance(relative, str) or not relative or
                any(char in relative for char in "\\:*?[]\x00") or
                Path(relative).is_absolute() or ".." in Path(relative).parts or
                any(part in ("", ".") or part.endswith((" ", "."))
                    for part in relative.split("/"))):
            raise UnsafeScope("runtime-relative-path", "Exact relative runtime paths required.")
        reserved=("CON","PRN","AUX","NUL")+tuple(
            prefix+str(i) for prefix in ("COM","LPT") for i in range(1,10))
        if any(part.split(".",1)[0].upper() in reserved for part in relative.split("/")):
            raise UnsafeScope("runtime-device-path","Windows device aliases refused.")
        if relative.lower().endswith((".pyc", ".pyo")):
            raise UnsafeScope("cached-python-runtime", "Cached Python runtime inputs refused.")

    def _source_snapshot(self, source, destination):
        from pathlib import Path
        import stat
        from .enforcement import UnsafeScope, _checked_path
        def capture(original, target):
            self._stage_meter.visit(original)
            info = _checked_path(original)
            self._consume_stage(original)
            if stat.S_ISDIR(info.st_mode):
                target.mkdir(exist_ok=True)
                with os.scandir(str(original)) as entries:
                    for entry in entries:
                        capture(Path(entry.path), target / entry.name)
            else:
                if original.name.lower().endswith((".pyc", ".pyo")):
                    raise UnsafeScope("cached-python-source", "Cached Python inputs refused.")
                unused,identity=self._api.read_nofollow(original,destination=target,
                    max_bytes=self.budgets.staging_bytes-self._staging_bytes,
                    check_budget=self._stage_meter.check_time)
                self._staging_bytes+=identity["captured_bytes"]
                self.evidence["source_files"][original.relative_to(source).as_posix()] = {
                    "sha256": identity["sha256"], "identity": identity}
        capture(source, destination)

    def _consume_stage(self,path):
        from .enforcement import UnsafeScope
        import stat
        info=path.lstat() if path is not None else None
        size=info.st_size if info is not None and stat.S_ISREG(info.st_mode) else 0
        self._staging_files+=1
        if (self._staging_files>self.budgets.staging_files or
                self._staging_bytes+size>self.budgets.staging_bytes or
                time.monotonic()-self._staging_started>self.budgets.staging_seconds):
            raise UnsafeScope("staging-budget","Aggregate staging budget exceeded.")

    @_locked
    def launch(self, argv=()):
        import subprocess
        import hashlib
        from .enforcement import EnforcementError, UnsafeScope
        if self._process or not self._profile:
            raise EnforcementError("session-state", "Session closed or already launched.")
        if not isinstance(argv, (tuple, list)) or not all(isinstance(v, str) for v in argv):
            raise EnforcementError("argv", "String argv sequence required.")
        command = subprocess.list2cmdline([str(self.executable)] + list(argv))
        env = {key: os.environ[key] for key in ("SystemRoot", "WINDIR", "SystemDrive")
               if key in os.environ}
        env.update(TEMP=str(self.temp), TMP=str(self.temp),
                   LOCALAPPDATA=self.profile_path, APPDATA=self.profile_path,
                   USERPROFILE=str(self.root), HOME=str(self.root),
                   PATH=str(self.runtime) + os.pathsep + os.path.join(
                       os.environ.get("SystemRoot", "C:/Windows"), "System32"),
                   PYTHONDONTWRITEBYTECODE="1", PYTHONNOUSERSITE="1")
        retained = []
        try:
            for relative, digest in self._runtime_bindings:
                unused, captured = self._api.read_nofollow(self.runtime / relative, retained,hash_only=True)
                if captured["sha256"] != digest:
                    raise UnsafeScope("runtime-copy-changed", "Private runtime changed before launch.")
            (self._process, self.pid, self._job,
             self.stdin, self.stdout) = self._api.launch(
                str(self.executable), command, str(self.workspace), env,
                self._sid, self._desktop_name,self._restriction_sid,self.budgets)
            self.evidence["launch_token"]=self._api.last_launch_token
            self._owned_streams = (self.stdin,self.stdout)
            return self
        except BaseException:
            self._close_after_failure()
            raise
        finally:
            self._api.release_resources([
                ("runtime-pin", lambda handle=handle: self._api.close(handle))
                for handle in reversed(retained)], raise_errors=False)
            if self._api._pending_releases:
                import sys
                if sys.exc_info()[1] is not None:
                    self._close_after_failure()
                else:
                    self.close()

    @_locked
    def poll(self):
        if self._process:
            value = self._api.poll(self._process)
            if value is not None:
                self._exit_code = value
            return value
        return self._exit_code

    def wait(self, timeout=None):
        import subprocess
        with self._lock:
            if not self._process:
                if self._exit_code is not None:
                    return self._exit_code
                raise RuntimeError("Session has no launched process.")
            # Own a duplicate while waiting; close/terminate stay responsive
            # and cannot close or reuse the waiter's numeric handle.
            process = self._api.duplicate(self._process)
        try:
            if not self._api.wait(process, timeout):
                raise subprocess.TimeoutExpired(str(self.executable), timeout)
            value = self._api.poll(process)
            with self._lock:
                self._exit_code = value
        except BaseException:
            # A duplicate-release fault here must not replace the original
            # wait/poll failure; the duplicate stays owned for retry either
            # way and the fault is recorded in native cleanup evidence.
            try:
                self._api.close_wait_duplicate(process)
            except BaseException:
                pass
            raise
        self._api.close_wait_duplicate(process)
        return value

    def _terminate_job(self):
        if self._job:
            self._api.check(self._api.TerminateJobObject(self._job, 125))

    def _close_job(self):
        if self._job:
            self._api.close(self._job)  # KILL_ON_JOB_CLOSE includes descendants.
            self._job = None  # Clear ONLY on close success; failures stay owned.

    @_locked
    def terminate(self):
        # Termination and job-handle closure are independent checked attempts:
        # a termination fault never suppresses the close attempt, and the
        # handle clears only when closure succeeds. Both errors are preserved.
        primary = None
        try:
            self._terminate_job()
        except BaseException as error:
            primary = error
        try:
            self._close_job()
        except BaseException as error:
            if primary is None:
                raise
            raise error from primary
        if primary is not None:
            raise primary

    def _close_after_failure(self):
        import sys
        original = sys.exc_info()[1]
        try:
            self.close()
        except BaseException as cleanup_error:
            # Keep the original failing operation authoritative and preserve
            # cleanup evidence on that exception for the caller's receipt.
            original.cleanup_evidence = dict(self.evidence)
            original.cleanup_error = str(cleanup_error)
            original.cleanup_session = self  # prepare() may not have returned it.

    @_locked
    def close(self):
        if not hasattr(self, "_api"):
            return
        from .enforcement import EnforcementError
        history = self.evidence.setdefault("cleanup_errors", [])
        pending = []
        def release(attribute, function):
            value = getattr(self, attribute)
            if value:
                function(value)
                setattr(self, attribute, None)  # Never discard a failed handle.
        def close_process(handle):
            if not self._api.wait(handle, 5):
                raise TimeoutError("Owned process did not exit during cleanup.")
            self._exit_code = self._api.poll(handle)
            self._api.close(handle)
        def delete_profile(name):
            code = self._api.delete_profile(name)
            self.evidence["profile_delete_hresult"] = code
            if code:
                raise OSError(code, "Owned profile deletion failed.")
        streams = []
        for stream in (self.stdin, self.stdout) + self._owned_streams:
            if stream is not None and all(stream is not value for value in streams):
                streams.append(stream)
        actions = [("job-terminate", self._terminate_job),
                   ("job-close", self._close_job)]
        actions.extend(("stream-close", stream.close) for stream in streams)
        actions.extend((label, lambda attr=attr, fn=fn: release(attr, fn)) for label, attr, fn in (
            ("process-close", "_process", close_process),
            ("desktop-close", "_desktop", self._api.close_desktop),
            ("profile-delete", "_profile", delete_profile),
            ("sid-free", "_sid", self._api.free_sid),
            ("restricting-sid-free", "_restriction_sid", self._api.local_free)))
        actions.append(("native-resources", lambda: self._api.release_resources([])))
        # Two bounded passes. All other resources are attempted even when an
        # earlier release fails. Failed objects remain owned for explicit retry.
        for attempt in (1, 2):
            pending = []
            for label, action in actions:
                try:
                    action()
                except BaseException as error:
                    history.append({"step": label, "attempt": attempt,
                                    "type": type(error).__name__, "message": str(error)})
                    pending.append((label, action))
            actions = pending
            if not actions:
                break
        self._owned_streams = tuple(stream for stream in streams if not stream.closed)
        if self.stdin is not None and self.stdin.closed:
            self.stdin = None
        if self.stdout is not None and self.stdout.closed:
            self.stdout = None
        self.evidence["cleanup_pending"] = [label for label, action in pending]
        self.evidence["native_cleanup_errors"] = list(self._api.cleanup_errors)
        with self._api._release_lock:
            self.evidence["native_cleanup_pending"] = [
                label for label, action in self._api._pending_releases]
        self.evidence["retained_copy"] = str(self.root)
        if pending:
            raise EnforcementError("cleanup-failed", "Owned resources require cleanup retry.", dict(self.evidence))

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        self.close()


class _Native:
    """No-follow copied inputs and child-specific native objects only."""
    def __init__(self):
        import ctypes as c
        from ctypes import wintypes as w
        if c.sizeof(c.c_void_p) != 8:
            from .enforcement import EnforcementUnavailable
            raise EnforcementUnavailable("windows-x64-required", "Only the measured x64 ABI is supported.")
        self.c, self.w = c, w
        self._pending_releases = []
        self.cleanup_errors = []
        # Threadsafe ownership ledger: concurrent waiters register failed
        # duplicate releases here; session cleanup retries them. Every access
        # to _pending_releases holds this lock.
        self._release_lock = threading.RLock()
        self.K = c.WinDLL("kernel32", use_last_error=True)
        self.A = c.WinDLL("advapi32", use_last_error=True)
        self.U = c.WinDLL("user32", use_last_error=True)
        self.E = c.WinDLL("userenv", use_last_error=True)
        self.O = c.WinDLL("ole32", use_last_error=True)
        P, H = c.c_void_p, w.HANDLE
        class SA(c.Structure):
            _fields_ = [("length", w.DWORD), ("descriptor", P), ("inherit", w.BOOL)]
        class SC(c.Structure):
            _fields_ = [("sid", P), ("capabilities", P), ("count", w.DWORD), ("reserved", w.DWORD)]
        class SidAttributes(c.Structure):
            _fields_ = [("sid",P),("attributes",w.DWORD)]
        class GenericMap(c.Structure):
            _fields_=[("read",w.DWORD),("write",w.DWORD),("execute",w.DWORD),("all",w.DWORD)]
        class SI(c.Structure):
            _fields_ = [("cb", w.DWORD), ("reserved", w.LPWSTR), ("desktop", w.LPWSTR),
                ("title", w.LPWSTR)] + [(n, w.DWORD) for n in (
                "x", "y", "xsize", "ysize", "charsx", "charsy", "fill", "flags")] + [
                ("show", w.WORD), ("reserved2size", w.WORD), ("reserved2", P),
                ("stdin", H), ("stdout", H), ("stderr", H)]
        class SX(c.Structure):
            _fields_ = [("si", SI), ("attributes", P)]
        class PI(c.Structure):
            _fields_ = [("process", H), ("thread", H), ("pid", w.DWORD), ("tid", w.DWORD)]
        class BasicLimits(c.Structure):
            _fields_ = [("PerProcessUserTimeLimit",c.c_int64),("PerJobUserTimeLimit",c.c_int64),
                ("LimitFlags",w.DWORD),("MinimumWorkingSetSize",c.c_size_t),
                ("MaximumWorkingSetSize",c.c_size_t),("ActiveProcessLimit",w.DWORD),
                ("Affinity",c.c_size_t),("PriorityClass",w.DWORD),("SchedulingClass",w.DWORD)]
        class IoCounters(c.Structure):
            _fields_ = [(name,c.c_uint64) for name in ("ReadOperationCount","WriteOperationCount",
                "OtherOperationCount","ReadTransferCount","WriteTransferCount","OtherTransferCount")]
        class ExtendedLimits(c.Structure):
            _fields_ = [("BasicLimitInformation",BasicLimits),("IoInfo",IoCounters)] + [
                (name,c.c_size_t) for name in ("ProcessMemoryLimit","JobMemoryLimit",
                    "PeakProcessMemoryUsed","PeakJobMemoryUsed")]
        self.SA, self.SC, self.SX, self.PI = SA, SC, SX, PI
        self.ExtendedLimits = ExtendedLimits
        self.SidAttributes = SidAttributes
        self.GenericMap=GenericMap
        self._defs = {
            "CloseHandle": (self.K, [H], w.BOOL),
            "GetCurrentProcess": (self.K, [], H),
            "DuplicateHandle": (self.K, [H,H,H,c.POINTER(H),w.DWORD,w.BOOL,w.DWORD], w.BOOL),
            "LocalFree": (self.K, [P], P),
            "CoTaskMemFree": (self.O, [P], None),
            "CreateFileW": (self.K, [w.LPCWSTR,w.DWORD,w.DWORD,P,w.DWORD,w.DWORD,H], H),
            "ReadFile": (self.K,[H,P,w.DWORD,c.POINTER(w.DWORD),P],w.BOOL),
            "GetFileInformationByHandle": (self.K,[H,P],w.BOOL),
            "CreateAppContainerProfile": (self.E,[w.LPCWSTR,w.LPCWSTR,w.LPCWSTR,P,w.DWORD,c.POINTER(P)],w.LONG),
            "DeleteAppContainerProfile": (self.E,[w.LPCWSTR],w.LONG),
            "GetAppContainerFolderPath": (self.E,[w.LPCWSTR,c.POINTER(P)],w.LONG),
            "FreeSid": (self.A,[P],P),
            "ConvertSidToStringSidW": (self.A,[P,c.POINTER(P)],w.BOOL),
            "ConvertStringSidToSidW": (self.A,[w.LPCWSTR,c.POINTER(P)],w.BOOL),
            "ConvertStringSecurityDescriptorToSecurityDescriptorW": (self.A,[w.LPCWSTR,w.DWORD,c.POINTER(P),P],w.BOOL),
            "GetSecurityDescriptorDacl": (self.A,[P,c.POINTER(w.BOOL),c.POINTER(P),c.POINTER(w.BOOL)],w.BOOL),
            "GetSecurityDescriptorSacl": (self.A,[P,c.POINTER(w.BOOL),c.POINTER(P),c.POINTER(w.BOOL)],w.BOOL),
            "SetNamedSecurityInfoW": (self.A,[w.LPWSTR,w.DWORD,w.DWORD,P,P,P,P],w.DWORD),
            "GetNamedSecurityInfoW": (self.A,[w.LPWSTR,w.DWORD,w.DWORD,P,P,P,P,c.POINTER(P)],w.DWORD),
            "ConvertSecurityDescriptorToStringSecurityDescriptorW": (self.A,[P,w.DWORD,w.DWORD,c.POINTER(P),c.POINTER(w.DWORD)],w.BOOL),
            "GetProcessWindowStation": (self.U,[],H),
            "GetUserObjectInformationW": (self.U,[H,c.c_int,P,w.DWORD,c.POINTER(w.DWORD)],w.BOOL),
            "CreateDesktopW": (self.U,[w.LPCWSTR,P,P,w.DWORD,w.DWORD,c.POINTER(SA)],H),
            "CloseDesktop": (self.U,[H],w.BOOL),
            "WaitForSingleObject": (self.K,[H,w.DWORD],w.DWORD),
            "GetExitCodeProcess": (self.K,[H,c.POINTER(w.DWORD)],w.BOOL),
            "CreatePipe": (self.K,[c.POINTER(H),c.POINTER(H),c.POINTER(SA),w.DWORD],w.BOOL),
            "SetHandleInformation": (self.K,[H,w.DWORD,w.DWORD],w.BOOL),
            "InitializeProcThreadAttributeList": (self.K,[P,w.DWORD,w.DWORD,c.POINTER(c.c_size_t)],w.BOOL),
            "UpdateProcThreadAttribute": (self.K,[P,w.DWORD,c.c_size_t,P,c.c_size_t,P,P],w.BOOL),
            "DeleteProcThreadAttributeList": (self.K,[P],None),
            "CreateProcessW": (self.K,[w.LPCWSTR,w.LPWSTR,P,P,w.BOOL,w.DWORD,P,w.LPCWSTR,c.POINTER(SX),c.POINTER(PI)],w.BOOL),
            "OpenProcessToken": (self.A,[H,w.DWORD,c.POINTER(H)],w.BOOL),
            "CreateRestrictedToken": (self.A,[H,w.DWORD,w.DWORD,P,w.DWORD,P,w.DWORD,P,c.POINTER(H)],w.BOOL),
            "IsTokenRestricted": (self.A,[H],w.BOOL),
            "GetTokenInformation": (self.A,[H,c.c_int,P,w.DWORD,c.POINTER(w.DWORD)],w.BOOL),
            "DuplicateTokenEx": (self.A,[H,w.DWORD,P,c.c_int,c.c_int,c.POINTER(H)],w.BOOL),
            "AccessCheck": (self.A,[P,H,w.DWORD,c.POINTER(GenericMap),P,c.POINTER(w.DWORD),c.POINTER(w.DWORD),c.POINTER(w.BOOL)],w.BOOL),
            "CreateProcessAsUserW": (self.A,[H,w.LPCWSTR,w.LPWSTR,P,P,w.BOOL,w.DWORD,P,w.LPCWSTR,c.POINTER(SX),c.POINTER(PI)],w.BOOL),
            "CreateJobObjectW": (self.K,[P,w.LPCWSTR],H),
            "SetInformationJobObject": (self.K,[H,c.c_int,P,w.DWORD],w.BOOL),
            "QueryInformationJobObject": (self.K,[H,c.c_int,P,w.DWORD,P],w.BOOL),
            "TerminateJobObject": (self.K,[H,w.UINT],w.BOOL),
            "GetProcessMitigationPolicy": (self.K,[H,c.c_int,P,c.c_size_t],w.BOOL),
            "EqualSid": (self.A,[P,P],w.BOOL),
            "AssignProcessToJobObject": (self.K,[H,H],w.BOOL),
            "ResumeThread": (self.K,[H],w.DWORD),
            "TerminateProcess": (self.K,[H,w.UINT],w.BOOL)}
        for name, (dll, args, result) in self._defs.items():
            fn = getattr(dll, name)
            fn.argtypes, fn.restype = args, result
            setattr(self, name, fn)

    def check(self, result):
        if not result:
            raise self.c.WinError(self.c.get_last_error())
        return result

    def close(self, handle):
        self.check(self.CloseHandle(handle))

    def release_resources(self, actions, raise_errors=True):
        with self._release_lock:
            actions = self._pending_releases + list(actions)
            self._pending_releases = []
            for attempt in (1, 2):
                pending = []
                for label, action in actions:
                    try:
                        action()
                    except BaseException as error:
                        self.cleanup_errors.append({"step": label, "attempt": attempt, "error": str(error)})
                        pending.append((label, action))
                actions = pending
                if not pending:
                    break
            self._pending_releases = actions
        if actions and raise_errors:
            from .enforcement import EnforcementError
            raise EnforcementError("native-cleanup-failed", "Native resources remain owned for retry.", list(self.cleanup_errors))

    def retain_release(self, label, action, error):
        """Record a failed checked release and keep it owned for retry."""
        with self._release_lock:
            self.cleanup_errors.append({"step": label, "attempt": 1, "error": str(error)})
            self._pending_releases.append((label, action))

    def close_wait_duplicate(self, handle):
        """Checked release of one wait() duplicate; failures stay owned."""
        try:
            self.close(handle)
        except BaseException as error:
            self.retain_release("wait-duplicate-close",
                                lambda handle=handle: self.close(handle), error)
            raise

    def local_free(self, pointer):
        if self.LocalFree(pointer):
            raise self.c.WinError(self.c.get_last_error())

    def duplicate(self, handle):
        value = self.w.HANDLE()
        current = self.GetCurrentProcess()
        self.check(self.DuplicateHandle(current,handle,current,self.c.byref(value),0,False,2))
        return value.value

    def free_sid(self, sid):
        if self.FreeSid(sid):
            raise self.c.WinError(self.c.get_last_error())

    def delete_profile(self, name):
        return self.DeleteAppContainerProfile(name)

    def create_profile(self, name, owner=None):
        c, w = self.c, self.w
        sid = c.c_void_p()
        code = self.CreateAppContainerProfile(name,name,"Owned Neoxider console session",None,0,c.byref(sid))
        if code < 0:
            # No profile was created; never claim or delete anything.
            raise OSError(code, "CreateAppContainerProfile failed")
        # Register actual ownership immediately after successful creation and
        # before conversion: only this created profile is ever claimed. Later
        # session cleanup retries it through checked, retryable releases.
        if owner is not None:
            owner._sid = sid
            owner._profile = name
        text = c.c_void_p()
        converted = False
        try:
            self.check(self.ConvertSidToStringSidW(sid,c.byref(text)))
            value = c.wstring_at(text)
            converted = True
            return sid, value
        except BaseException:
            # Route every release through checked retryable cleanup and keep
            # the original conversion failure authoritative. With an owner,
            # the registered profile/SID stay owned for session cleanup retry.
            if owner is None:
                self.release_resources([
                    ("profile-sid-free", lambda: self.free_sid(sid)),
                    ("profile-delete", lambda: self._checked_delete_profile(name))],
                    raise_errors=False)
            raise
        finally:
            if text:
                if converted:
                    self.local_free(text)
                else:
                    self.release_resources(
                        [("profile-sid-text-free", lambda: self.local_free(text))],
                        raise_errors=False)

    def _checked_delete_profile(self, name):
        code = self.delete_profile(name)
        if code:
            raise OSError(code, "Owned profile deletion failed.")
        return code

    def create_normal_sid(self):
        import uuid
        values=[int.from_bytes(uuid.uuid4().bytes[i:i+4],"little") for i in (0,4,8,12)]
        text="S-1-5-21-"+"-".join(str(value) for value in values)
        sid=self.c.c_void_p()
        self.check(self.ConvertStringSidToSidW(text,self.c.byref(sid)))
        return sid,text

    def diagnostic_access(self,token,app_groups=True):
        c,w=self.c,self.w
        copy=w.HANDLE()
        self.check(self.DuplicateTokenEx(token,8|4,None,2,2,c.byref(copy)))
        descriptor=self.descriptor("O:SYG:SYD:(A;;GA;;;WD)"+(
            "(A;;GA;;;AC)(A;;GA;;;S-1-15-2-2)" if app_groups else ""))
        result={}
        try:
            mapping=self.GenericMap(0x120089,0x120116,0x1200a0,0x1f01ff)
            for name,mask in (("read",1),("write",2)):
                privileges=c.create_string_buffer(1024)
                size,granted,allowed=w.DWORD(1024),w.DWORD(),w.BOOL()
                self.check(self.AccessCheck(descriptor,copy,mask,c.byref(mapping),privileges,c.byref(size),c.byref(granted),c.byref(allowed)))
                result[name]=bool(allowed.value)
            return result
        finally:
            self.CloseHandle(copy.value);self.LocalFree(descriptor)

    def profile_path(self, sid_text):
        c = self.c
        value = c.c_void_p()
        code = self.GetAppContainerFolderPath(sid_text,c.byref(value))
        if code < 0:
            raise OSError(code, "GetAppContainerFolderPath failed")
        try:
            return c.wstring_at(value)
        finally:
            self.CoTaskMemFree(value)

    def descriptor(self, text):
        c = self.c
        value = c.c_void_p()
        self.check(self.ConvertStringSecurityDescriptorToSecurityDescriptorW(text,1,c.byref(value),None))
        return value

    def user_sid_text(self):
        c, w = self.c, self.w
        token, text, size = w.HANDLE(), c.c_void_p(), w.DWORD()
        self.check(self.OpenProcessToken(self.GetCurrentProcess(), 8, c.byref(token)))
        try:
            self.GetTokenInformation(token, 1, None, 0, c.byref(size))
            information = c.create_string_buffer(size.value)
            self.check(self.GetTokenInformation(token, 1, information, len(information), c.byref(size)))
            self.check(self.ConvertSidToStringSidW(c.c_void_p.from_buffer(information), c.byref(text)))
            return c.wstring_at(text)
        finally:
            if text:
                self.LocalFree(text)
            self.close(token)

    def permissions(self, path, sid, writable, restricting_sid=None):
        c, w = self.c, self.w
        # The combined token does not satisfy OWNER RIGHTS as its ordinary
        # access pass. Grant the host user explicitly on owned objects only.
        # Package/restricting grants omit WRITE_DAC/WRITE_OWNER, even writable.
        rights="0x1301bf" if writable else "GRGX"
        restricting="(A;OICI;%s;;;%s)"%(rights,restricting_sid) if restricting_sid else ""
        sd = self.descriptor("D:P(A;OICI;RC;;;OW)(A;OICI;GA;;;%s)(A;OICI;%s;;;%s)%sS:(ML;;NW;;;LW)" % (
            self.user_sid_text(), rights, sid,restricting))
        try:
            present, defaulted = w.BOOL(), w.BOOL()
            dacl, sacl = c.c_void_p(), c.c_void_p()
            self.check(self.GetSecurityDescriptorDacl(sd,c.byref(present),c.byref(dacl),c.byref(defaulted)))
            self.check(self.GetSecurityDescriptorSacl(sd,c.byref(present),c.byref(sacl),c.byref(defaulted)))
            for flags, da, sa in ((4|0x80000000,dacl,None),(0x10,None,sacl)):
                code=self.SetNamedSecurityInfoW(str(path),1,flags,None,None,da,sa)
                if code:
                    raise OSError(code,"Owned private copy permissions failed")
        finally:
            self.LocalFree(sd)

    def security_fingerprint(self, path):
        import hashlib
        c, w = self.c, self.w
        descriptor, text = c.c_void_p(), c.c_void_p()
        count = w.DWORD()
        flags = 1 | 4 | 0x10
        code=self.GetNamedSecurityInfoW(str(path),1,flags,None,None,None,None,c.byref(descriptor))
        if code:
            raise OSError(code,"Read-only source security inspection failed")
        try:
            self.check(self.ConvertSecurityDescriptorToStringSecurityDescriptorW(
                descriptor,1,flags,c.byref(text),c.byref(count)))
            return hashlib.sha256(c.wstring_at(text).encode("utf-8")).hexdigest()
        finally:
            if text:
                self.LocalFree(text)
            if descriptor:
                self.LocalFree(descriptor)

    def read_nofollow(self, path, retained=None,destination=None,hash_only=False,max_bytes=128*1024*1024,check_budget=None):
        from .enforcement import _checked_path, UnsafeScope
        import struct
        import hashlib
        c, w = self.c, self.w
        path = path.absolute()
        _checked_path(path)
        held = []
        completed = False
        output=None
        try:
            # Share-read only locks component rename/delete during capture.
            for ancestor in list(reversed(path.parents)):
                h = self.CreateFileW(str(ancestor),0x80000000,1,None,3,0x02200000,None)
                if h == c.c_void_p(-1).value:
                    raise c.WinError(c.get_last_error())
                held.append(h)
                info = c.create_string_buffer(52)
                self.check(self.GetFileInformationByHandle(h,info))
                if struct.unpack_from("I",info.raw)[0] & 0x400:
                    raise UnsafeScope("reparse-ancestor","Reparse ancestor refused.")
            handle = self.CreateFileW(str(path),0x80000000,1,None,3,0x00200000,None)
            if handle == c.c_void_p(-1).value:
                raise c.WinError(c.get_last_error())
            held.append(handle)
            info = c.create_string_buffer(52)
            self.check(self.GetFileInformationByHandle(handle,info))
            fields = struct.unpack("13I",info.raw)
            if fields[0] & (0x400|0x10) or fields[10] != 1:
                raise UnsafeScope("linked-native-file","Reparse/directory/hardlink input refused.")
            if (fields[8]<<32 | fields[9]) > min(128*1024*1024,max_bytes):
                raise UnsafeScope("file-size-limit","Experimental capture limit is 128 MiB per file.")
            chunks, buffer, got = [], c.create_string_buffer(65536), w.DWORD()
            digest=hashlib.sha256()
            captured_bytes=0
            if destination is not None:
                output=open(str(destination),"xb")
            while True:
                if check_budget:
                    check_budget()
                self.check(self.ReadFile(handle,buffer,len(buffer),c.byref(got),None))
                if not got.value:
                    break
                captured_bytes+=got.value
                if captured_bytes>min(128*1024*1024,max_bytes):
                    raise UnsafeScope("capture-budget","Native captured size exceeded budget.")
                piece=buffer.raw[:got.value]
                digest.update(piece)
                if output is not None:
                    output.write(piece)
                elif not hash_only:
                    chunks.append(piece)
            after = c.create_string_buffer(52)
            self.check(self.GetFileInformationByHandle(handle,after))
            final_fields = struct.unpack("13I",after.raw)
            # Reading may update LastAccessTime (DWORDs 3/4). Write-share is
            # denied and identity/creation/write-time/size/link count stay bound.
            stable = (0,1,2,5,6,7,8,9,10,11,12)
            if any(fields[index] != final_fields[index] for index in stable):
                raise UnsafeScope("source-changed","Source identity/metadata changed during capture.")
            completed = True
            return (None if output is not None or hash_only else b"".join(chunks)), {
                "volume":fields[7],"file_index_high":fields[11],
                "file_index_low":fields[12],"links":fields[10],
                "sha256":digest.hexdigest(),"captured_bytes":captured_bytes}
        finally:
            actions = []
            if output is not None:
                actions.append(("capture-output", output.close))
            if completed and retained is not None:
                retained.extend(held)
            else:
                for handle in reversed(held):
                    actions.append(("capture-input", lambda handle=handle: self.close(handle)))
            import sys
            self.release_resources(actions, raise_errors=sys.exc_info()[1] is None)

    def create_desktop(self, name, sid, restricting_sid=None):
        c, w = self.c, self.w
        station = self.GetProcessWindowStation()
        text, needed = c.create_unicode_buffer(1024), w.DWORD()
        self.check(self.GetUserObjectInformationW(station,2,text,c.sizeof(text),c.byref(needed)))
        extra="(A;;0x1ff;;;%s)"%restricting_sid if restricting_sid else ""
        descriptor=self.descriptor("D:P(A;;RC;;;OW)(A;;GA;;;%s)(A;;0x1ff;;;%s)%s" % (
            self.user_sid_text(), sid,extra))
        try:
            sa=self.SA(c.sizeof(self.SA),descriptor,False)
            desktop=self.check(self.CreateDesktopW(name,None,None,0,0xF01FF,c.byref(sa)))
            return desktop,text.value+"\\"+name
        finally:
            self.LocalFree(descriptor)

    def close_desktop(self, handle):
        self.check(self.CloseDesktop(handle))

    def poll(self, process):
        waited=self.WaitForSingleObject(process,0)
        if waited==258:
            return None
        if waited!=0:
            raise self.c.WinError(self.c.get_last_error())
        code=self.w.DWORD()
        self.check(self.GetExitCodeProcess(process,self.c.byref(code)))
        return code.value

    def wait(self, process, timeout):
        result=self.WaitForSingleObject(process,0xFFFFFFFF if timeout is None else max(0,int(timeout*1000)))
        if result==0xFFFFFFFF:
            raise self.c.WinError(self.c.get_last_error())
        return result==0

    def launch(self, executable, command, cwd, env, sid, desktop, restricting_sid,budgets):
        import msvcrt
        c,w=self.c,self.w
        attrs=None
        attrs_initialized=False
        handles=[]
        pi=self.PI()
        job=None
        stdin=stdout=None
        primary=restricted=None
        try:
            primary_handle=w.HANDLE()
            self.check(self.OpenProcessToken(self.GetCurrentProcess(),0xB,c.byref(primary_handle)))
            primary=primary_handle.value
            restricted_handle=w.HANDLE()
            exact_sid=self.SidAttributes(restricting_sid,0)
            self.check(self.CreateRestrictedToken(primary,0x1|0x8,0,None,0,None,1,c.byref(exact_sid),c.byref(restricted_handle)))
            restricted=restricted_handle.value
            self.check(self.IsTokenRestricted(restricted))
            self.primary_write_restriction_diagnostic=self.diagnostic_access(restricted)
            ir,iw,orr,ow=(w.HANDLE() for unused in range(4))
            sa=self.SA(c.sizeof(self.SA),None,True)
            self.check(self.CreatePipe(c.byref(ir),c.byref(iw),c.byref(sa),0))
            handles.extend((ir.value,iw.value))
            self.check(self.CreatePipe(c.byref(orr),c.byref(ow),c.byref(sa),0))
            handles.extend((orr.value,ow.value))
            self.check(self.SetHandleInformation(iw,1,0))
            self.check(self.SetHandleInformation(orr,1,0))
            size=c.c_size_t()
            self.InitializeProcThreadAttributeList(None,3,0,c.byref(size))
            attrs=c.create_string_buffer(size.value)
            self.check(self.InitializeProcThreadAttributeList(attrs,3,0,c.byref(size)))
            attrs_initialized=True
            sc=self.SC(sid,None,0,0)
            mitigation=c.c_uint64(1<<28)
            allowed=(w.HANDLE*2)(ir.value,ow.value)
            for key,value in ((0x20009,sc),(0x20007,mitigation),(0x20002,allowed)):
                self.check(self.UpdateProcThreadAttribute(attrs,0,key,c.byref(value),c.sizeof(value),None,None))
            sx=self.SX()
            sx.si.cb=c.sizeof(sx)
            sx.si.desktop=desktop
            sx.si.flags=0x101
            sx.si.show=0
            sx.si.stdin,sx.si.stdout,sx.si.stderr=ir.value,ow.value,ow.value
            sx.attributes=c.cast(attrs,c.c_void_p)
            environment=c.create_unicode_buffer("\x00".join(k+"="+v for k,v in sorted(env.items()))+"\x00\x00")
            self.check(self.CreateProcessAsUserW(restricted,executable,c.create_unicode_buffer(command),None,None,True,
                0x80000|4|8|0x400,environment,cwd,c.byref(sx),c.byref(pi)))
            child_token=w.HANDLE()
            self.check(self.OpenProcessToken(pi.process,8|2,c.byref(child_token)))
            try:
                needed=w.DWORD()
                self.GetTokenInformation(child_token,11,None,0,c.byref(needed))
                information=c.create_string_buffer(needed.value)
                self.check(self.GetTokenInformation(child_token,11,information,len(information),c.byref(needed)))
                count=w.DWORD.from_buffer(information).value
                offset=(c.sizeof(w.DWORD)+c.alignment(self.SidAttributes)-1)&~(c.alignment(self.SidAttributes)-1)
                entries=(self.SidAttributes*count).from_buffer(information,offset) if count else []
                strings=[]
                for entry in entries:
                    text=c.c_void_p()
                    self.check(self.ConvertSidToStringSidW(entry.sid,c.byref(text)))
                    strings.append(c.wstring_at(text));self.LocalFree(text)
                app=w.DWORD()
                self.check(self.GetTokenInformation(child_token,29,c.byref(app),c.sizeof(app),c.byref(needed)))
                self.last_launch_token={"appcontainer":app.value,"restricted":bool(self.IsTokenRestricted(child_token)),"restricting_sids":strings}
                expected_text=c.c_void_p()
                self.check(self.ConvertSidToStringSidW(restricting_sid,c.byref(expected_text)))
                try:
                    expected=c.wstring_at(expected_text)
                finally:
                    self.LocalFree(expected_text)
                from .enforcement import EnforcementUnavailable
                if app.value != 1 or not self.IsTokenRestricted(child_token) or strings != [expected]:
                    raise EnforcementUnavailable("token-invariant", "Suspended token does not match the exact two-principal design.", self.last_launch_token)
                self.GetTokenInformation(child_token,31,None,0,c.byref(needed))
                package=c.create_string_buffer(needed.value)
                self.check(self.GetTokenInformation(child_token,31,package,len(package),c.byref(needed)))
                if not self.EqualSid(c.c_void_p.from_buffer(package),sid):
                    raise EnforcementUnavailable("package-invariant", "Suspended package SID changed.")
                self.GetTokenInformation(child_token,30,None,0,c.byref(needed))
                capabilities=c.create_string_buffer(needed.value)
                self.check(self.GetTokenInformation(child_token,30,capabilities,len(capabilities),c.byref(needed)))
                policy=w.DWORD()
                self.check(self.GetProcessMitigationPolicy(pi.process,4,c.byref(policy),c.sizeof(policy)))
                if w.DWORD.from_buffer(capabilities).value or policy.value & 1 != 1:
                    raise EnforcementUnavailable("security-invariant", "Capabilities or Win32k policy changed before resume.")
                self.last_launch_token.update(capabilities=0,win32k_disabled=True)
                self.last_launch_token["accesscheck_aap_aar"]=self.diagnostic_access(child_token)
                self.last_launch_token["primary_accesscheck_aap_aar"]=self.primary_write_restriction_diagnostic
                self.last_launch_token["primary_accesscheck_world_only"]=self.diagnostic_access(restricted,False)
            finally:
                self.CloseHandle(child_token.value)
            job=self.check(self.CreateJobObjectW(None,None))
            limits=self.ExtendedLimits()
            limits.BasicLimitInformation.LimitFlags=0x2000|0x8|0x100|0x200
            limits.BasicLimitInformation.ActiveProcessLimit=budgets.active_processes
            limits.ProcessMemoryLimit=budgets.process_memory_bytes
            limits.JobMemoryLimit=budgets.job_memory_bytes
            self.check(self.SetInformationJobObject(job,9,c.byref(limits),c.sizeof(limits)))
            observed=self.ExtendedLimits()
            self.check(self.QueryInformationJobObject(job,9,c.byref(observed),c.sizeof(observed),None))
            if (observed.BasicLimitInformation.LimitFlags & limits.BasicLimitInformation.LimitFlags != limits.BasicLimitInformation.LimitFlags or
                    observed.BasicLimitInformation.ActiveProcessLimit != budgets.active_processes or
                    observed.ProcessMemoryLimit != budgets.process_memory_bytes or
                    observed.JobMemoryLimit != budgets.job_memory_bytes):
                raise EnforcementUnavailable("job-invariant", "Kernel did not accept the requested resource limits.")
            self.check(self.AssignProcessToJobObject(job,pi.process))
            for handle in (ir.value,ow.value):
                self.close(handle);handles.remove(handle)
            def stream(handle, flags, mode):
                fd=msvcrt.open_osfhandle(handle,flags|os.O_BINARY)
                handles.remove(handle)
                try:
                    return os.fdopen(fd,mode,buffering=0)
                except BaseException:
                    self.release_resources([("pipe-fd",lambda:os.close(fd))],raise_errors=False)
                    raise
            stdin=stream(iw.value,os.O_WRONLY,"wb")
            stdout=stream(orr.value,os.O_RDONLY,"rb")
            if self.ResumeThread(pi.thread)==0xFFFFFFFF:
                raise c.WinError(c.get_last_error())
            self.close(pi.thread)
            pi.thread=None
            self.DeleteProcThreadAttributeList(attrs)
            attrs_initialized=False
            releases=[("launch-token", lambda handle=handle: self.close(handle)) for handle in (restricted,primary) if handle]
            restricted=primary=None
            self.release_resources(releases)
            return pi.process,pi.pid,job,stdin,stdout
        except BaseException:
            actions=[]
            if pi.process:
                process=pi.process
                def dispose_process():
                    if self.poll(process) is None:
                        self.check(self.TerminateProcess(process,125))
                    if not self.wait(process,5):
                        raise TimeoutError("Failed launch process did not settle.")
                    self.close(process)
                actions.append(("failed-process",dispose_process))
            if pi.thread:
                actions.append(("failed-thread-close",lambda:self.close(pi.thread)))
            if job:
                actions.append(("failed-job-close",lambda:self.close(job)))
            for stream in (stdin,stdout):
                if stream:
                    actions.append(("failed-stream-close",stream.close))
            self.release_resources(actions,raise_errors=False)
            raise
        finally:
            actions=[]
            if attrs_initialized:
                actions.append(("launch-attributes",lambda:self.DeleteProcThreadAttributeList(attrs)))
            for handle in handles:
                actions.append(("launch-pipe",lambda handle=handle:self.close(handle)))
            for handle in (restricted,primary):
                if handle:
                    actions.append(("launch-token",lambda handle=handle:self.close(handle)))
            self.release_resources(actions,raise_errors=False)
