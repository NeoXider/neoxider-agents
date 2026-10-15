"""Reuse the GUI and bridge parsers without an intermediate Bash runtime."""
import subprocess
import sys
from pathlib import Path
from .process import hidden_kwargs


def launch_service(command, args):
    operands = {"--token"} if command == "gui" else {"-e", "--engine", "-m", "--model", "-f", "--effort", "-C", "--dir", "-p", "--port", "--host", "--timeout", "--retries", "--api-key", "--session-ttl"}
    switches = {"--lan", "--localhost"}
    if command == "openai-server":
        # Subcommands (start/list/stop/check) are positionals and pass through unchanged.
        operands.update({"--models", "--max-concurrency", "--engine-limit", "--max-queue", "--queue-timeout",
                         "--max-sessions", "--stall-timeout", "--max-request-mb", "--name", "--wait"})
        switches.update({"--no-live-stream", "--detach", "--allow-any", "--strict-models", "--no-strict-models",
                         "--check", "--probe", "--json", "--force"})
    iterator = iter(args)
    for argument in iterator:
        flag = argument.split("=", 1)[0]
        if flag in operands:
            if "=" not in argument:
                try:
                    next(iterator)
                except StopIteration:
                    raise ValueError(command + ": option '" + flag + "' needs an operand")
        elif argument.startswith("-") and flag not in switches:
            raise ValueError(command + ": unknown option '" + flag + "'")
    path = Path(__file__).resolve().parent.parent / ("gui.py" if command == "gui" else "openai_server.py")
    executable = sys.executable
    if command == "gui" and sys.platform == "win32":
        windowless = Path(executable).with_name("pythonw.exe")
        if windowless.is_file() and not any(arg in ("--help", "-h") for arg in args):
            executable = str(windowless)
    import os
    env = dict(os.environ)
    env.pop("AGENT_LAUNCHER_PID", None)
    process = subprocess.Popen([executable, str(path), *args], stdin=subprocess.DEVNULL,
                               stdout=sys.stdout, stderr=sys.stderr, env=env, **hidden_kwargs())
    try:
        return process.wait()
    except BaseException:
        # WHY: the bridge runs detached from this console on Windows, so Ctrl-C (or a stopped
        # launcher) never reaches it; without this the server and its CLI children outlived us.
        # A deliberate background bridge uses `openai-server start --detach` instead.
        if command == "openai-server" and process.poll() is None:
            from .process import kill_tree, pid_stamp
            stamp = pid_stamp(process.pid)
            if os.name != "nt":
                try:
                    process.wait(timeout=3)  # POSIX shares the Ctrl-C; let it clean up first
                except (subprocess.TimeoutExpired, KeyboardInterrupt):
                    pass
            if process.poll() is None:
                kill_tree(process.pid, stamp=stamp)
        raise
