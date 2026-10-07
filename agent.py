#!/usr/bin/env python3
"""Native entry point; no Bash or WSL dependency."""
import json
import os
import sys


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    argv = sys.argv[1:]
    if len(argv) == 2 and argv[0] == "--argv-file":
        with open(argv[1], encoding="utf-8-sig") as source:
            argv = json.load(source)
    elif argv == ["--argv-stdin"]:
        argv = json.loads(sys.stdin.buffer.read().decode("utf-8-sig"))
    elif argv == ["--argv-env"]:
        argv = json.loads(os.environ.pop("NEOXIDER_ARGV_JSON"))
    if not isinstance(argv, list) or not all(isinstance(arg, str) for arg in argv):
        raise ValueError("argv transport must contain a JSON list of strings")
    from neoxider_agents.cli import main as core
    return core(argv)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        os._exit(130)
    except KeyboardInterrupt:
        sys.exit(130)
