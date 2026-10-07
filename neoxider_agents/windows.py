"""Windows process identities, lifetime jobs and named control events."""
import ctypes
import os
import threading
import uuid
from ctypes import wintypes as w

kernel = ctypes.WinDLL("kernel32", use_last_error=True)
HANDLE = w.HANDLE
INVALID_HANDLE = ctypes.c_void_p(-1).value
SIZE_T = ctypes.c_size_t
ULONG_PTR = ctypes.c_size_t
PROCESS_QUERY = 0x1000
SYNCHRONIZE = 0x100000
_lifetime_job = None
_lifetime_lock = threading.Lock()


class BasicLimits(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64), ("LimitFlags", w.DWORD),
                ("MinimumWorkingSetSize", SIZE_T), ("MaximumWorkingSetSize", SIZE_T),
                ("ActiveProcessLimit", w.DWORD), ("Affinity", ULONG_PTR),
                ("PriorityClass", w.DWORD), ("SchedulingClass", w.DWORD)]


class IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in
               ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class ExtendedLimits(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", BasicLimits), ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", SIZE_T), ("JobMemoryLimit", SIZE_T),
                ("PeakProcessMemoryUsed", SIZE_T), ("PeakJobMemoryUsed", SIZE_T)]


class ProcessEntry(ctypes.Structure):
    _fields_ = [("dwSize", w.DWORD), ("cntUsage", w.DWORD),
                ("th32ProcessID", w.DWORD), ("th32DefaultHeapID", ULONG_PTR),
                ("th32ModuleID", w.DWORD), ("cntThreads", w.DWORD),
                ("th32ParentProcessID", w.DWORD), ("pcPriClassBase", w.LONG),
                ("dwFlags", w.DWORD), ("szExeFile", w.WCHAR * 260)]


class ThreadEntry(ctypes.Structure):
    _fields_ = [("dwSize", w.DWORD), ("cntUsage", w.DWORD),
                ("th32ThreadID", w.DWORD), ("th32OwnerProcessID", w.DWORD),
                ("tpBasePri", w.LONG), ("tpDeltaPri", w.LONG), ("dwFlags", w.DWORD)]


def _declare(name, arguments, result):
    function = getattr(kernel, name)
    function.argtypes, function.restype = arguments, result
    return function


CloseHandle = _declare("CloseHandle", [HANDLE], w.BOOL)
OpenProcess = _declare("OpenProcess", [w.DWORD, w.BOOL, w.DWORD], HANDLE)
GetProcessTimes = _declare("GetProcessTimes", [HANDLE] + [ctypes.POINTER(w.FILETIME)] * 4, w.BOOL)
WaitForSingleObject = _declare("WaitForSingleObject", [HANDLE, w.DWORD], w.DWORD)
CreateJobObject = _declare("CreateJobObjectW", [ctypes.c_void_p, w.LPCWSTR], HANDLE)
SetJobInformation = _declare("SetInformationJobObject", [HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD], w.BOOL)
AssignJob = _declare("AssignProcessToJobObject", [HANDLE, HANDLE], w.BOOL)
TerminateJob = _declare("TerminateJobObject", [HANDLE, w.UINT], w.BOOL)
OpenJob = _declare("OpenJobObjectW", [w.DWORD, w.BOOL, w.LPCWSTR], HANDLE)
GetCurrentProcess = _declare("GetCurrentProcess", [], HANDLE)
GetProcessId = _declare("GetProcessId", [HANDLE], w.DWORD)
CreateSnapshot = _declare("CreateToolhelp32Snapshot", [w.DWORD, w.DWORD], HANDLE)
ProcessFirst = _declare("Process32FirstW", [HANDLE, ctypes.POINTER(ProcessEntry)], w.BOOL)
ProcessNext = _declare("Process32NextW", [HANDLE, ctypes.POINTER(ProcessEntry)], w.BOOL)
ThreadFirst = _declare("Thread32First", [HANDLE, ctypes.POINTER(ThreadEntry)], w.BOOL)
ThreadNext = _declare("Thread32Next", [HANDLE, ctypes.POINTER(ThreadEntry)], w.BOOL)
OpenThread = _declare("OpenThread", [w.DWORD, w.BOOL, w.DWORD], HANDLE)
ResumeThread = _declare("ResumeThread", [HANDLE], w.DWORD)
TerminateProcess = _declare("TerminateProcess", [HANDLE, w.UINT], w.BOOL)
CreateEvent = _declare("CreateEventW", [ctypes.c_void_p, w.BOOL, w.BOOL, w.LPCWSTR], HANDLE)
OpenEvent = _declare("OpenEventW", [w.DWORD, w.BOOL, w.LPCWSTR], HANDLE)
SetEvent = _declare("SetEvent", [HANDLE], w.BOOL)


def _check(value):
    if not value:
        raise ctypes.WinError(ctypes.get_last_error())
    return value


def creation_stamp(handle):
    fields = [w.FILETIME() for unused in range(4)]
    if not GetProcessTimes(handle, *(ctypes.byref(field) for field in fields)):
        return ""
    return str((fields[0].dwHighDateTime << 32) | fields[0].dwLowDateTime)


def pid_stamp(pid):
    handle = OpenProcess(PROCESS_QUERY, False, int(pid))
    if not handle:
        return ""
    try:
        return creation_stamp(handle)
    finally:
        CloseHandle(handle)


def pid_alive(pid, stamp=""):
    handle = OpenProcess(PROCESS_QUERY | SYNCHRONIZE, False, int(pid))
    if not handle:
        return False
    try:
        return WaitForSingleObject(handle, 0) == 258 and (not stamp or creation_stamp(handle) == str(stamp))
    finally:
        CloseHandle(handle)


class Job:
    def __init__(self, name=None):
        self.name = name or "Local\\NeoxiderProvider-%s-%s" % (os.getpid(), uuid.uuid4().hex)
        self.handle = _check(CreateJobObject(None, self.name))
        limits = ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
        try:
            _check(SetJobInformation(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)))
        except BaseException:
            self.close()
            raise

    def assign(self, process_handle):
        _check(AssignJob(self.handle, HANDLE(int(process_handle))))

    def kill(self, code=130):
        if self.handle:
            _check(TerminateJob(self.handle, code))

    def close(self):
        if self.handle:
            CloseHandle(self.handle)
            self.handle = None


def ensure_lifetime_job():
    """The launcher owns the only handle; its death closes and kills the job."""
    global _lifetime_job
    with _lifetime_lock:
        if _lifetime_job is None:
            job = Job("Local\\NeoxiderLauncher-%s-%s" % (os.getpid(), uuid.uuid4().hex))
            try:
                job.assign(GetCurrentProcess())
            except BaseException:
                job.close()
                raise
            # Never close this handle explicitly: the job contains this process too.
            _lifetime_job = job


def resume(pid):
    snapshot = _check(CreateSnapshot(4, 0))
    count = 0
    try:
        item = ThreadEntry()
        item.dwSize = ctypes.sizeof(item)
        more = ThreadFirst(snapshot, ctypes.byref(item))
        while more:
            if item.th32OwnerProcessID == pid:
                thread = _check(OpenThread(2, False, item.th32ThreadID))
                try:
                    if ResumeThread(thread) == 0xffffffff:
                        raise ctypes.WinError(ctypes.get_last_error())
                    count += 1
                finally:
                    CloseHandle(thread)
            more = ThreadNext(snapshot, ctypes.byref(item))
    finally:
        CloseHandle(snapshot)
    if not count:
        raise OSError("provider primary thread disappeared before resume")


def processes():
    snapshot = CreateSnapshot(2, 0)
    if snapshot == INVALID_HANDLE:
        raise ctypes.WinError(ctypes.get_last_error())
    result = []
    try:
        item = ProcessEntry()
        item.dwSize = ctypes.sizeof(item)
        more = ProcessFirst(snapshot, ctypes.byref(item))
        while more:
            result.append((item.th32ProcessID, item.th32ParentProcessID, item.szExeFile))
            more = ProcessNext(snapshot, ctypes.byref(item))
    finally:
        CloseHandle(snapshot)
    return result


def kill_tree(pid, stamp="", job_name=""):
    if job_name:
        handle = OpenJob(8, False, job_name)
        if handle:
            try:
                _check(TerminateJob(handle, 130))
                return True
            finally:
                CloseHandle(handle)
    pid = int(pid)
    if not pid_alive(pid, stamp):
        return False
    rows = processes()
    descendants = {pid}
    while True:
        added = {child for child, parent, unused in rows if parent in descendants}
        if added <= descendants:
            break
        descendants.update(added)
    handles = []
    try:
        for child in descendants:
            handle = OpenProcess(1 | SYNCHRONIZE | PROCESS_QUERY, False, child)
            if handle:
                handles.append((child, handle, creation_stamp(handle)))
        # Stop the root first, preventing new children while the snapshot is killed.
        for child, handle, observed in sorted(handles, key=lambda item: item[0] != pid):
            if child != pid or not stamp or observed == str(stamp):
                TerminateProcess(handle, 130)
        for unused, handle, unused_stamp in handles:
            WaitForSingleObject(handle, 3000)
    finally:
        for unused, handle, unused_stamp in handles:
            CloseHandle(handle)
    return True


class NamedEvent:
    def __init__(self, name):
        self.name = name
        self.handle = _check(CreateEvent(None, False, False, name))

    def wait(self, timeout):
        return WaitForSingleObject(self.handle, max(0, int(timeout * 1000))) == 0

    def set(self):
        _check(SetEvent(self.handle))

    def close(self):
        if self.handle:
            CloseHandle(self.handle)
            self.handle = None


def signal_event(name):
    handle = OpenEvent(2, False, name)
    if not handle:
        return False
    try:
        return bool(SetEvent(handle))
    finally:
        CloseHandle(handle)


def pipe_disconnected(handle):
    """Query a pipe writer without writing heartbeat bytes into user output."""
    get_type = _declare("GetFileType", [HANDLE], w.DWORD)
    if get_type(handle) != 3:
        return False
    class IoStatus(ctypes.Structure):
        _fields_ = [("status", ctypes.c_void_p), ("information", ULONG_PTR)]
    class PipeLocal(ctypes.Structure):
        _fields_ = [(name, w.DWORD) for name in
                   ("type", "configuration", "maximum", "current", "inbound_quota",
                    "read_available", "outbound_quota", "write_available", "state", "end")]
    query = ctypes.WinDLL("ntdll").NtQueryInformationFile
    query.argtypes = [HANDLE, ctypes.POINTER(IoStatus), ctypes.c_void_p, w.ULONG, w.ULONG]
    query.restype = w.LONG
    status, local = IoStatus(), PipeLocal()
    result = query(handle, ctypes.byref(status), ctypes.byref(local), ctypes.sizeof(local), 24)
    return result in (-1073741648, -1073741493) or (result == 0 and local.state in (1, 4))
