"""Fixture provider whose agent launches a subagent through the CLI, like a real orchestrating
agent would: `agent.py run` with NO -P flag. NEST_DEPTH levels below this one are created.
The lineage (who is whose parent) must come from the environment the core gives this process."""
import os
import subprocess
import sys


def main():
    sys.stdin.buffer.read()
    print("session id: ses_nest_%d" % os.getpid(), flush=True)
    depth = int(os.environ.get("NEST_DEPTH", "0"))
    if depth > 0:
        name = "%s-%d" % (os.environ["NEST_PREFIX"], depth - 1)
        env = dict(os.environ, NEST_DEPTH=str(depth - 1))
        flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
        result = subprocess.run([sys.executable, os.environ["NEST_AGENT"], "run", "-e", "fixture", "-t", name,
                                 "-C", os.getcwd(), "nested task " + name], env=env, cwd=os.getcwd(),
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=90, creationflags=flags)
        print("child %s exit %s" % (name, result.returncode), flush=True)
    print("---------- output ----------", flush=True)
    print("ANSWER: depth %d done" % depth, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
