"""Isolated stdlib benchmarks: 200 settled tasks, 30 real wrappers, no network."""
import argparse
import contextlib
import ctypes
import hashlib
import io
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from neoxider_agents.process import spawn, hidden_kwargs, pid_alive
from neoxider_agents.state import Store, atomic_write, last_output

BUDGETS = {"list": 0.5, "status": 0.15, "pending": 0.5,
           "startup": 0.25, "wait_cpu_percent": 1.0, "large_log_peak_bytes": 1048576}


def process_rows():
    if os.name == "nt":
        from neoxider_agents.windows import processes
        return processes()
    rows = []
    try:
        for entry in os.scandir("/proc"):
            if entry.name.isdigit():
                try:
                    fields = (Path(entry.path) / "stat").read_text().rsplit(")", 1)[1].split()
                    rows.append((int(entry.name), int(fields[1]), fields[0]))
                except OSError:
                    pass
    except OSError:
        pass
    return rows


def descendants(roots, rows=None):
    rows = process_rows() if rows is None else rows
    result = set(roots)
    while True:
        found = {pid for pid, parent, unused in rows if parent in result}
        if found <= result:
            return result
        result.update(found)


def process_metrics(pid):
    if os.name == "nt":
        from ctypes import wintypes as w
        from neoxider_agents.windows import OpenProcess, CloseHandle, GetProcessTimes
        handle = OpenProcess(0x1000, False, int(pid))
        if not handle:
            return {}
        class Counters(ctypes.Structure):
            _fields_ = [("cb", w.DWORD), ("PageFaultCount", w.DWORD)] + [
                (name, ctypes.c_size_t) for name in ("PeakWorkingSetSize", "WorkingSetSize",
                 "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
                 "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]
        try:
            fields = [w.FILETIME() for unused in range(4)]
            okay = GetProcessTimes(handle, *(ctypes.byref(field) for field in fields))
            cpu = sum((value.dwHighDateTime << 32) | value.dwLowDateTime for value in fields[2:]) / 10000000 if okay else None
            counters = Counters()
            counters.cb = ctypes.sizeof(counters)
            query = ctypes.WinDLL("psapi").GetProcessMemoryInfo
            query.argtypes = [w.HANDLE, ctypes.POINTER(Counters), w.DWORD]
            query.restype = w.BOOL
            rss = counters.WorkingSetSize if query(handle, ctypes.byref(counters), counters.cb) else None
            stamp = str((fields[0].dwHighDateTime << 32) | fields[0].dwLowDateTime) if okay else ""
            return {"cpu_seconds": cpu, "rss_bytes": rss, "stamp": stamp}
        finally:
            CloseHandle(handle)
    try:
        fields = Path("/proc/%s/stat" % pid).read_text().rsplit(")", 1)[1].split()
        return {"cpu_seconds": (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK"),
                "rss_bytes": int(fields[21]) * os.sysconf("SC_PAGE_SIZE"), "stamp": fields[19]}
    except (OSError, ValueError):
        return {}


def write_json(path, value):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")


def warm_worker(args):
    sys.path.insert(0, str(Path(args.root).resolve()))
    # The initial import above is for OS supervision; command modules come from the snapshot.
    for name in list(sys.modules):
        if name == "neoxider_agents" or name.startswith("neoxider_agents."):
            del sys.modules[name]
    from neoxider_agents.cli import main
    from neoxider_agents.state import Store
    store = Store()
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        main(["status", args.names[0]])
    events = []
    def audit(event, unused):
        if event in ("subprocess.Popen", "os.system", "os.posix_spawn", "os.spawn"):
            events.append(event)
    sys.addaudithook(audit)
    profile = None
    if os.environ.get("AGENT_BENCH_PROFILE") == "1":
        import cProfile
        profile = cProfile.Profile()
        profile.enable()
    before = time.process_time()
    start = time.perf_counter()
    boundary = []
    original_error = sys.stderr
    class IdleBoundary:
        def write(self, text):
            if "wait: timeout after" in text and not boundary:
                boundary.append((time.perf_counter() - start, time.process_time() - before))
            return original_error.write(text)
        def flush(self):
            original_error.flush()
    with contextlib.redirect_stderr(IdleBoundary()):
        code = main(["wait", *args.names, "--timeout", str(args.idle_seconds), "--poll", "5"])
    total_duration, total_cpu = time.perf_counter() - start, time.process_time() - before
    if profile:
        profile.disable()
        profile.dump_stats(args.out + ".prof")
    duration, cpu = boundary[0] if boundary else (total_duration, total_cpu)
    write_json(args.out, {"elapsed_seconds": duration, "cpu_seconds": cpu,
                         "cpu_percent_one_core": 100 * cpu / duration, "child_spawns": len(events),
                         "total_seconds_with_formatting": total_duration, "total_cpu_seconds_with_formatting": total_cpu,
                         "spawn_audit_events": events, "exit": code, "tasks": len(args.names)})
    return 0 if code == 2 else 1


class Bench:
    def __init__(self, args):
        self.args = args
        self.base = Path(args.scratch).resolve()
        if os.name == "nt" and (self.base.drive.upper() != "D:" or not self.base.as_posix().startswith("D:/Temp/agents-ux/")):
            raise ValueError("Windows benchmark scratch must be under D:/Temp/agents-ux/")
        self.base.mkdir(parents=True, exist_ok=True)
        self.work = Path(tempfile.mkdtemp(prefix=args.engine + "-", dir=str(self.base)))
        self.logs = self.work / "state"
        self.logs.mkdir()
        self.empty = self.work / "stdin.txt"
        self.empty.write_bytes(b"")
        self.trees = []
        self.names = ["running-%03d" % n for n in range(args.running)]
        self.result = {"engine": args.engine, "finished": args.finished, "running": args.running,
                       "platform": sys.platform, "python": sys.version.split()[0], "scratch": self.work.as_posix(),
                       "budgets": BUDGETS, "ci_margin": args.ci_margin, "commands": {}}
        if args.engine == "core":
            self.root = self.snapshot(Path(args.root))
            self.command = [sys.executable, str(self.root / "agent.py")]
        else:
            self.root = Path(args.root).resolve()
            if not (self.root / "agent.sh").is_file():
                raise ValueError("legacy --root must be the archived main snapshot")
            bash = "C:/Program Files/Git/bin/bash.exe" if os.name == "nt" else shutil.which("bash")
            if not bash or not Path(bash).is_file():
                raise ValueError("legacy measurement needs Bash")
            self.command = [bash, (self.root / "agent.sh").as_posix()]
        self.env = dict(os.environ, AGENT_CLI_LOGS=self.logs.as_posix(), TMPDIR=self.work.as_posix(),
                        AGENT_PARENT="core-bench", AGENT_ORCHESTRATOR_ID="core-bench", AGENT_RETRIES="0",
                        AGENT_SILENCE_SEC="0", AGENT_TIMEOUT_SEC="600", PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        if args.engine == "core":
            self.env.update(AGENT_PROVIDER_DIR=(self.root / "tests/fixtures/providers").as_posix(),
                            FIXTURE_SCRIPT=(self.root / "tests/fixtures/core-provider.py").as_posix(), FIXTURE_BLOCK_TURNS="1")
        else:
            fixture = self.work / "control-provider.sh"
            shutil.copy2(REPO / "tests/fixtures/control-provider.sh", fixture)
            self.env.update(BASH_ENV=fixture.as_posix(), FIXTURE_DELAY="300", FIXTURE_BLOCK_TURNS="")
        self.store = Store(self.logs)

    def snapshot(self, source):
        destination = self.work / "source"
        destination.mkdir()
        before = {}
        files = [source / name for name in ("agent.py", "activity.py")]
        for name in ("neoxider_agents", "providers", "tests/fixtures"):
            files += [path for path in (source / name).rglob("*") if path.is_file() and "__pycache__" not in path.parts]
        for path in files:
            content = path.read_bytes()
            before[path] = hashlib.sha256(content).digest()
            target = destination / path.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        if any(hashlib.sha256(path.read_bytes()).digest() != digest for path, digest in before.items()):
            raise RuntimeError("source changed while snapshot was copied; freeze it first")
        self.result["source_sha256"] = hashlib.sha256(b"".join(before.values())).hexdigest()
        return destination

    def seed(self):
        for n in range(self.args.finished):
            name = "finished-%03d" % n
            # Compatible with both Bash main and Python state readers.
            atomic_write(self.logs / (name + ".meta"), "engine=fixture\nmodel=fake\nsession=ses_%s\nstate=done\nexit=0\nfiles=0\ndir=%s\nparent=core-bench\nlast_activity=assistant answer\n" % (n, self.work.as_posix()))
            atomic_write(self.logs / (name + ".log"), "---------- output ----------\nanswer\n")

    def start_tasks(self):
        pending = []
        for index, name in enumerate(self.names):
            folder = self.work / name
            folder.mkdir()
            tree = spawn(self.command + ["run", "-e", "fixture", "-t", name, "-C", folder.as_posix(),
                                        "--no-progress", "--no-terse", "BENCH"], self.empty, str(self.root), self.env)
            self.trees.append(tree)
            pending.append((tree, folder, name))
            if len(pending) == self.args.launch_batch or index == len(self.names) - 1:
                deadline = time.monotonic() + 30
                for child, directory, label in pending:
                    while not (directory / "turn.1.started").exists():
                        if child.process.poll() is not None or time.monotonic() >= deadline:
                            raise RuntimeError("%s did not start: %s" % (label, child.process.communicate(timeout=3)[0][-4000:].decode("utf-8", "replace")))
                        time.sleep(0.05)
                pending = []
        self.result["started_tasks"] = len(self.trees)

    def command_time(self, command, repeat=None):
        samples, codes = [], []
        for index in range(repeat or self.args.repeat):
            started = time.perf_counter()
            result = subprocess.run(self.command + command, env=self.env, cwd=str(self.root), stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, timeout=240, **hidden_kwargs())
            samples.append(time.perf_counter() - started)
            codes.append(result.returncode)
            if index == 0:
                (self.work / (command[0] + ".txt")).write_bytes(result.stdout)
        return {"median_seconds": statistics.median(samples), "min_seconds": min(samples),
                "samples_seconds": samples, "exit_codes": codes}

    def warm_commands(self):
        os.environ.update(self.env)
        sys.path.insert(0, str(self.root))
        for name in list(sys.modules):
            if name == "neoxider_agents" or name.startswith("neoxider_agents.") or name == "activity":
                del sys.modules[name]
        from neoxider_agents.cli import main
        commands = {"list": ["list", "0"], "status": ["status", "running-000"], "pending": ["pending"],
                    "peek": ["peek", "running-000"], "last": ["last", "finished-000"],
                    "result": ["result", "finished-000"], "send": ["send", "running-000", "BENCH FOLLOW"],
                    "stop": ["stop", "running-000"], "wait_settled": ["wait", "finished-000"]}
        for label, command in commands.items():
            count = 1 if label == "stop" else self.args.repeat
            samples, codes = [], []
            for index in range(count + (0 if label in ("send", "stop") else 1)):
                output = io.StringIO()
                start = time.perf_counter()
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                    code = main(command)
                elapsed = time.perf_counter() - start
                if index or label in ("send", "stop"):
                    samples.append(elapsed)
                    codes.append(code)
                if index == 0:
                    (self.work / (label + "-warm.txt")).write_text(output.getvalue(), encoding="utf-8")
            self.result["commands"][label]["warm"] = {"median_seconds": statistics.median(samples), "samples_seconds": samples, "exit_codes": codes}

    def runtime_metrics(self):
        roots = {tree.pid for tree in self.trees}
        rows = process_rows()
        owned = descendants(roots, rows)
        metrics = {pid: process_metrics(pid) for pid in owned}
        for pid, parent, executable in rows:
            if pid in metrics:
                metrics[pid].update(parent_pid=parent, executable=executable)
        counts = [len(descendants({pid}, rows)) for pid in roots]
        rss = [metrics[pid].get("rss_bytes") for pid in roots if metrics.get(pid, {}).get("rss_bytes") is not None]
        self.result["runtime"] = {"wrapper_count": len(roots), "total_processes": len(owned),
                                  "processes_per_task": counts, "wrapper_rss_median_bytes": statistics.median(rss) if rss else None,
                                  "wrapper_rss_total_bytes": sum(rss) if rss else None,
                                  "total_rss_bytes": sum(row.get("rss_bytes") or 0 for row in metrics.values()), "processes": metrics,
                                  "provider_trees_excluded_from_wrapper_budget": True}

    def wait_idle(self):
        path = self.work / "wait-idle.json"
        command = ([sys.executable, str(Path(__file__).resolve()), "--worker-wait", "--root", str(self.root),
                    "--idle-seconds", str(self.args.idle_seconds), "--out", str(path), *self.names]
                   if self.args.engine == "core" else self.command + ["wait", *self.names, "--timeout", str(self.args.idle_seconds), "--poll", "5"])
        tree = spawn(command, self.empty, str(self.root), self.env)
        output_path = self.work / "wait-idle.txt"
        def pump():
            with output_path.open("wb") as stream:
                while True:
                    chunk = tree.process.stdout.read1(16384)
                    if not chunk:
                        break
                    stream.write(chunk)
        reader = threading.Thread(target=pump, daemon=True)
        reader.start()
        seen, spawned = descendants({tree.pid}), set()
        sampled_cpu = {}
        started = time.perf_counter()
        first_cpu = process_metrics(tree.pid).get("cpu_seconds", 0) or 0
        last_cpu = first_cpu
        deadline_hit = False
        try:
            while tree.process.poll() is None:
                current = descendants({tree.pid})
                spawned.update(current - seen)
                seen.update(current)
                for pid in current:
                    value = process_metrics(pid).get("cpu_seconds")
                    if value is not None:
                        sampled_cpu[pid] = max(sampled_cpu.get(pid, 0), value)
                last_cpu = process_metrics(tree.pid).get("cpu_seconds", last_cpu) or last_cpu
                if time.perf_counter() - started > self.args.idle_seconds + 90:
                    deadline_hit = True
                    tree.kill()
                    tree.process.wait(timeout=5)
                    break
                time.sleep(0.05)
            reader.join(timeout=5)
            if self.args.engine == "core" and path.exists():
                self.result["wait_idle"] = json.loads(path.read_text(encoding="utf-8"))
                self.result["wait_idle"]["sampled_child_spawns"] = len(spawned)
            else:
                elapsed = time.perf_counter() - started
                self.result["wait_idle"] = {"elapsed_seconds": elapsed, "cpu_seconds": last_cpu - first_cpu,
                                           "cpu_percent_one_core": 100 * (last_cpu - first_cpu) / elapsed,
                                           "sampled_tree_cpu_seconds_lower_bound": sum(sampled_cpu.values()) - first_cpu,
                                           "sampled_child_spawns": len(spawned), "child_spawns_per_minute_lower_bound": 60 * len(spawned) / elapsed,
                                           "exit": tree.process.returncode, "sampling_ms": 50, "deadline_hit": deadline_hit,
                                           "completed": not deadline_hit}
                if deadline_hit and self.args.engine == "core":
                    raise RuntimeError("core wait exceeded bounded deadline")
        finally:
            tree.close()
            reader.join(timeout=5)
            tree.process.stdout.close()

    def large_log(self):
        path = self.work / "large.log"
        with path.open("wb") as stream:
            for unused in range(11):
                stream.write(b"x" * 1048576)
            stream.write(b"\n---------- output ----------\nFINAL\n")
        import tracemalloc
        tracemalloc.start()
        start = time.perf_counter()
        answer = last_output(path)
        seconds = time.perf_counter() - start
        unused, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        self.result["large_log"] = {"bytes": path.stat().st_size, "seconds": seconds, "peak_bytes": peak, "answer_correct": answer == "FINAL\n"}

    def startup(self):
        samples = []
        for unused in range(self.args.repeat):
            started = time.perf_counter()
            subprocess.run([sys.executable, "-c", "pass"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, **hidden_kwargs())
            samples.append(time.perf_counter() - started)
        code = "import neoxider_agents.cli,sys,json; print(json.dumps(sorted(sys.modules)))"
        result = subprocess.run([sys.executable, "-X", "importtime", "-c", code], cwd=str(self.root), env=self.env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, **hidden_kwargs())
        (self.work / "importtime.txt").write_bytes(result.stderr)
        modules = json.loads(result.stdout)
        forbidden = [name for name in ("ctypes", "subprocess", "http.server", "neoxider_agents.windows", "neoxider_agents.runtime") if name in modules]
        self.result["startup"] = {"python_median_seconds": statistics.median(samples), "samples_seconds": samples,
                                  "heavy_imports": forbidden, "importtime_log": str(self.work / "importtime.txt")}

    def check(self):
        checks = {}
        for label in ("list", "status", "pending"):
            value = self.result["commands"][label]["warm"]["median_seconds"]
            checks[label] = {"value": value, "budget": BUDGETS[label], "ci_budget": BUDGETS[label] * self.args.ci_margin,
                             "passed": value < BUDGETS[label] * self.args.ci_margin}
        wait = self.result["wait_idle"]
        checks["wait_cpu"] = {"value": wait["cpu_percent_one_core"], "budget": 1, "passed": wait["cpu_percent_one_core"] < self.args.ci_margin}
        checks["wait_spawns"] = {"value": wait["child_spawns"], "budget": 0, "passed": wait["child_spawns"] == 0 and wait["sampled_child_spawns"] == 0}
        checks["startup"] = {"value": self.result["startup"]["python_median_seconds"], "budget": BUDGETS["startup"],
                             "passed": self.result["startup"]["python_median_seconds"] < BUDGETS["startup"] * self.args.ci_margin}
        checks["lazy_imports"] = {"value": self.result["startup"]["heavy_imports"], "passed": not self.result["startup"]["heavy_imports"]}
        checks["bounded_log"] = {"value": self.result["large_log"]["peak_bytes"], "budget": BUDGETS["large_log_peak_bytes"],
                                 "passed": self.result["large_log"]["peak_bytes"] < BUDGETS["large_log_peak_bytes"] and self.result["large_log"]["answer_correct"]}
        checks["wrapper_processes"] = {"value": self.result["runtime"]["wrapper_count"], "budget": self.args.running,
                                       "passed": self.result["runtime"]["wrapper_count"] == self.args.running and self.result["runtime"]["total_processes"] == self.args.running * 2}
        self.result["checks"] = checks
        self.result["passed"] = all(row["passed"] for row in checks.values())

    def run(self):
        try:
            self.seed()
            phase = time.perf_counter()
            self.start_tasks()
            self.result["start_tasks_seconds"] = time.perf_counter() - phase
            self.runtime_metrics()
            self.wait_idle()
            commands = {"list": ["list", "0"], "status": ["status", "running-000"], "pending": ["pending"],
                        "last": ["last", "finished-000"], "wait_settled": ["wait", "finished-000"]}
            if self.args.engine == "core":
                commands.update(peek=["peek", "running-000"], result=["result", "finished-000"], send=["send", "running-000", "COLD FOLLOW"])
            for label, command in commands.items():
                if self.args.engine == "legacy" and label == "pending" and self.args.baseline_timings:
                    self.result["commands"][label] = {"cold": json.loads(Path(self.args.baseline_timings).read_text())[label], "reused_200_finished_baseline": True}
                    continue
                self.result["commands"][label] = {"cold": self.command_time(command, 1 if self.args.engine == "legacy" else None)}
            if self.args.engine == "core":
                self.result["commands"]["stop"] = {"cold": self.command_time(["stop", self.names[-1]], 1)} if len(self.names) > 1 else {}
                self.warm_commands()
                self.large_log()
                self.startup()
                self.check()
            else:
                self.result["unsupported_on_main"] = ["peek", "send", "stop", "result"]
            return self.result
        finally:
            for tree in self.trees:
                tree.close()
            for tree in self.trees:
                try:
                    tree.process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    tree.process.kill()
            self.result["cleanup_live_wrapper_pids"] = [tree.pid for tree in self.trees if pid_alive(tree.pid)]
            write_json(self.args.out, self.result)


def prove_budgets(args):
    import copy
    from types import SimpleNamespace
    good = {"commands": {name: {"warm": {"median_seconds": 0}} for name in ("list", "status", "pending")},
            "wait_idle": {"cpu_percent_one_core": 0, "child_spawns": 0, "sampled_child_spawns": 0},
            "startup": {"python_median_seconds": 0, "heavy_imports": []},
            "large_log": {"peak_bytes": 0, "answer_correct": True},
            "runtime": {"wrapper_count": 30, "total_processes": 60}}
    defects = [(name, ("commands", name, "warm", "median_seconds"), BUDGETS[name]) for name in ("list", "status", "pending")]
    defects += [("wait_cpu", ("wait_idle", "cpu_percent_one_core"), 1),
                ("wait_spawns", ("wait_idle", "child_spawns"), 1),
                ("startup", ("startup", "python_median_seconds"), 0.25),
                ("lazy_imports", ("startup", "heavy_imports"), ["ctypes"]),
                ("bounded_log", ("large_log", "peak_bytes"), 1048576),
                ("wrapper_processes", ("runtime", "total_processes"), 61)]
    bench = object.__new__(Bench)
    bench.args = SimpleNamespace(ci_margin=1, running=30)
    bench.result = copy.deepcopy(good)
    bench.check()
    assert bench.result["passed"]
    rows = []
    for label, keys, value in defects:
        bench.result = copy.deepcopy(good)
        target = bench.result
        for key in keys[:-1]:
            target = target[key]
        target[keys[-1]] = value
        bench.check()
        caught = not bench.result["passed"] and not bench.result["checks"][label]["passed"]
        rows.append({"planted_budget_violation": label, "caught": caught})
    write_json(args.out, rows)
    print("budget defects: %s/%s caught" % (sum(row["caught"] for row in rows), len(rows)))
    return 0 if all(row["caught"] for row in rows) else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", choices=("core", "legacy"), default="core")
    parser.add_argument("--root", default=str(REPO))
    parser.add_argument("--scratch", default="D:/Temp/agents-ux/bench" if os.name == "nt" else str(Path(tempfile.gettempdir()) / "agents-core/bench"))
    parser.add_argument("--out", required=True)
    parser.add_argument("--finished", type=int, default=200)
    parser.add_argument("--running", type=int, default=30)
    parser.add_argument("--idle-seconds", type=int, default=10)
    parser.add_argument("--repeat", type=int, default=5)
    parser.add_argument("--launch-batch", type=int, default=5)
    parser.add_argument("--ci-margin", type=float, default=1)
    parser.add_argument("--baseline-timings")
    parser.add_argument("--worker-wait", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--prove-budgets", action="store_true")
    parser.add_argument("names", nargs="*")
    args = parser.parse_args(argv)
    if args.worker_wait:
        return warm_worker(args)
    if args.prove_budgets:
        return prove_budgets(args)
    if min(args.finished, args.running, args.idle_seconds, args.repeat) < 1:
        parser.error("counts/durations must be positive")
    bench = Bench(args)
    try:
        result = bench.run()
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
        bench.result["error"] = str(error)
        write_json(args.out, bench.result)
        print("benchmark failed: " + str(error), file=sys.stderr)
        return 1
    print(json.dumps({"engine": args.engine, "budgets": BUDGETS, "ci_margin": args.ci_margin,
                      "checks": result.get("checks"), "out": args.out}, indent=2))
    return 1 if args.check and not result.get("passed", False) else 0


if __name__ == "__main__":
    sys.exit(main())
