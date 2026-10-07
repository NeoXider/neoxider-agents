"""Reusable 100 ms Windows visible-window sampler, including console hosts."""
import argparse
import ctypes
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def window_process_details(result):
    from neoxider_agents.windows import processes
    rows = processes()
    executables = {pid: executable for pid, unused, executable in rows}
    parents = {pid: parent for pid, parent, unused in rows}
    for row in result.values():
        chain, current = [], row["pid"]
        while current and current not in chain and len(chain) < 32:
            chain.append(current)
            current = parents.get(current, 0)
        row.update(parent_pid=parents.get(row["pid"], 0), executable=executables.get(row["pid"], ""),
                   parent_chain=[{"pid": value, "executable": executables.get(value, "")} for value in chain])
    return result


def visible_windows(include_processes=True):
    if os.name != "nt":
        raise RuntimeError("visible-window sampling requires Windows")
    from ctypes import wintypes as w
    user = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
    user.EnumWindows.argtypes = [callback_type, w.LPARAM]
    user.EnumWindows.restype = w.BOOL
    user.IsWindowVisible.argtypes = [w.HWND]
    user.GetWindowThreadProcessId.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]
    user.GetWindowTextLengthW.argtypes = [w.HWND]
    user.GetWindowTextW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
    result = {}
    def visit(hwnd, unused):
        if user.IsWindowVisible(hwnd):
            pid = w.DWORD()
            user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            title = ctypes.create_unicode_buffer(user.GetWindowTextLengthW(hwnd) + 1)
            user.GetWindowTextW(hwnd, title, len(title))
            result[int(hwnd)] = {"handle": int(hwnd), "pid": pid.value,
                                 "parent_pid": 0, "parent_chain": [],
                                 "executable": "", "title": title.value}
        return True
    user.EnumWindows(callback_type(visit), 0)
    return window_process_details(result) if include_processes else result


class WindowSampler:
    def __init__(self, interval=0.1):
        self.interval = interval
        self.baseline = {}
        self.baseline_pids = set()
        self.observations = []
        self.samples = 0
        self.errors = []
        self.sample_gaps_ms = []
        self.snapshot_ms = []
        self.overruns = []
        self.started = 0.0
        self.stopped = 0.0
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        from neoxider_agents.windows import processes
        self.baseline_pids = {pid for pid, unused_parent, unused_name in processes()}
        self.baseline = visible_windows(False)
        self.started = time.time()
        self._thread = threading.Thread(target=self._run, name="visible-window-sampler", daemon=True)
        self._thread.start()
        return self

    def _run(self):
        seen = set()
        deadline = time.monotonic()
        previous = None
        while not self._stop.is_set():
            started = time.monotonic()
            if previous is not None:
                self.sample_gaps_ms.append((started - previous) * 1000)
            previous = started
            try:
                current = visible_windows(False)
                self.samples += 1
                changes = {}
                for handle, row in current.items():
                    identity = (handle, row["pid"])
                    original = self.baseline.get(handle)
                    if identity not in seen and (not original or original["pid"] != row["pid"]):
                        seen.add(identity)
                        changes[handle] = dict(row, at=time.time())
                if changes:
                    self.observations.extend(window_process_details(changes).values())
            except OSError as error:
                self.errors.append(str(error))
            ended = time.monotonic()
            self.snapshot_ms.append((ended - started) * 1000)
            deadline += self.interval
            missed = 0
            if ended > deadline:
                missed = int((ended - deadline) // self.interval) + 1
                self.overruns.append({"at": time.time(), "snapshot_ms": (ended - started) * 1000,
                                      "deadline_lag_ms": (ended - deadline) * 1000,
                                      "missed_intervals": missed})
                deadline += missed * self.interval
            self._stop.wait(max(0, deadline - ended))

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        self.stopped = time.time()
        return self.report()

    def report(self):
        consoles = ("conhost.exe", "openconsole.exe", "windowsterminal.exe", "cmd.exe", "powershell.exe", "pwsh.exe")
        new_consoles = [row for row in self.observations if row["executable"].lower() in consoles]
        relevant = [row for row in self.observations if row["pid"] not in self.baseline_pids or row in new_consoles]
        unrelated = [row for row in self.observations if row not in relevant]
        return {"platform": "windows", "interval_ms": round(self.interval * 1000),
                "started": self.started, "stopped": self.stopped, "samples": self.samples,
                "baseline_visible": len(self.baseline), "new_visible": relevant,
                "other_visible_changes": unrelated,
                "sample_gaps_ms": self.sample_gaps_ms, "snapshot_ms": self.snapshot_ms,
                "max_sample_gap_ms": max(self.sample_gaps_ms, default=0),
                "max_snapshot_ms": max(self.snapshot_ms, default=0), "overruns": self.overruns,
                "missed_intervals": sum(row["missed_intervals"] for row in self.overruns),
                "new_visible_consoles": new_consoles, "errors": self.errors,
                "passed": not relevant and not self.errors}

    def __enter__(self):
        return self.start()

    def __exit__(self, *unused):
        self.stop()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True)
    parser.add_argument("--before", type=float, default=0.3)
    parser.add_argument("--after", type=float, default=0.3)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("supply a command after --")
    from neoxider_agents.process import hidden_kwargs
    sampler = WindowSampler().start()
    returncode = 1
    try:
        sampler._stop.wait(args.before)
        returncode = subprocess.call(command, stdout=sys.stdout, stderr=sys.stderr, **hidden_kwargs(executable=command[0]))
        sampler._stop.wait(args.after)
    finally:
        report = sampler.stop()
        report["command_returncode"] = returncode
        Path(args.out).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print("window check: %s (%s samples, %s new visible windows)" %
              ("PASS" if report["passed"] else "FAIL", report["samples"], len(report["new_visible"])))
    return returncode or (0 if report["passed"] else 1)


if __name__ == "__main__":
    sys.exit(main())
