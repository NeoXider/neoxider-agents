"""Explicit detached wave: each owned foreground task has one Python wrapper."""
import json
import os
import subprocess
import sys
import time
from threading import Event
from .cli import ROOT
from .process import hidden_kwargs, kill_tree, pid_stamp
from .lifecycle import preflight
from .state import atomic_write, valid_name


def fan(store, base, prompts, opts):
    if not prompts:
        raise ValueError("fan: needs >=1 prompt")
    defaults = dict(engine="claude", model="", effort="", dir=os.getcwd())
    defaults.update(opts)
    preflight(store, valid_name("%s-01" % base), defaults, False)
    names = []
    launched = []
    for index, prompt in enumerate(prompts, 1):
        name = valid_name("%s-%02d" % (base, index))
        argv = ["run", "-t", name]
        for key, flag in (("engine", "-e"), ("model", "-m"), ("effort", "-f"), ("dir", "-C"), ("parent", "-P")):
            if key in opts:
                argv += [flag, opts[key]]
        for flag in ("--no-progress", "--no-terse", "--verbose", "--terminal", "--progress", "--log", "--notify", "--strict-owns", "-v"):
            if opts.get(flag):
                argv.append(flag)
        if opts.get("owns"):
            argv += ["--owns", opts["owns"]]
        prompt_file = store.path(name, ".fan.prompt")
        atomic_write(prompt_file, prompt)
        argv += ["--prompt-file", str(prompt_file)]
        keep_output = opts.get("--log") or os.environ.get("AGENT_KEEP_LOGS") == "1"
        output = open(store.path(name, ".launcher.log"), "wb") if keep_output else open(os.devnull, "wb")
        try:
            process = subprocess.Popen([sys.executable, str(ROOT / "agent.py"), *argv], stdin=subprocess.DEVNULL,
                                       env={**os.environ, "AGENT_DETACHED": "1", "AGENT_LAUNCHER_PID": ""},
                                       stdout=output, stderr=subprocess.STDOUT, close_fds=True, **hidden_kwargs())
        finally:
            output.close()
        names.append(name)
        launched.append((name, process, pid_stamp(process.pid)))
        print("[agent.sh] ⇉ fanned %s (pid %s)" % (name, process.pid), file=sys.stderr)
    deadline = time.monotonic() + 15
    for name, process, stamp in launched:
        while store.read(name).get("pid") != str(process.pid):
            if process.poll() is not None:
                raise ValueError("fan: task %s failed before publishing its owner; use neoxider status %s (or --log to retain launcher output)" % (name, name))
            if time.monotonic() >= deadline:
                kill_tree(process.pid, stamp=stamp)
                raise ValueError("fan: task %s did not publish its owner within 15s" % name)
            Event().wait(0.05)
    print("[agent.sh] launched %s parallel task(s) under '%s'. Poll: agent.sh list" % (len(names), base), file=sys.stderr)
    print("[agent.sh] Start ONE tracked background job: neoxider wait %s; completion is its exit." % " ".join(names), file=sys.stderr)
    return 0
