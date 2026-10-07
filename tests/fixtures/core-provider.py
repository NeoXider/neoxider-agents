"""Deterministic cross-platform provider: real child trees, exact stdin and file barriers."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def main():
    work = Path.cwd()
    if os.environ.get("FIXTURE_CHECK_PWD") and Path(os.environ.get("PWD", ".")).resolve() != work.resolve():
        print("ERROR stale logical working directory", flush=True)
        return 41
    prompt = sys.stdin.buffer.read().decode("utf-8")
    turns = work / "turns"
    turn = len(turns.read_text().splitlines()) + 1 if turns.exists() else 1
    session = sys.argv[1] if len(sys.argv) > 1 else "ses_fixture_" + str(os.getpid())
    (work / ("turn.%s.prompt" % turn)).write_text(prompt, encoding="utf-8")
    (work / ("turn.%s.session" % turn)).write_text(session, encoding="utf-8")
    (work / "partial.txt").write_text("partial edit survives", encoding="utf-8")
    with turns.open("a", encoding="utf-8") as file:
        file.write("started\n")
    print("session id: " + session, flush=True)
    if os.environ.get("FIXTURE_PROVIDER_ERROR"):
        print("AGENT_PROVIDER_ERROR: usage limit until tomorrow", flush=True)
    print('{"type":"item.started","item":{"type":"command_execution","command":"fixture block"}}', flush=True)
    if os.environ.get("FIXTURE_CHILD"):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(90)"],
                                 creationflags=0x08000008 if os.name == "nt" else 0)
        (work / "child.pid").write_text(str(child.pid))
    (work / ("turn.%s.started" % turn)).touch()
    if str(turn) in os.environ.get("FIXTURE_BLOCK_TURNS", "").split():
        deadline = time.monotonic() + 90
        while not (work / ("turn.%s.release" % turn)).exists():
            if time.monotonic() >= deadline:
                return 99
            time.sleep(0.025)
    delay = float(os.environ.get("FIXTURE_DELAY", "0"))
    time.sleep(delay)
    if (work / ("fail-turn-%s" % turn)).exists():
        print("fixture delivery failed", flush=True)
        return 42
    with (work / "delivered").open("a", encoding="utf-8") as file:
        file.write(prompt + "\n")
    (work / ("turn.%s.finished" % turn)).touch()
    if os.environ.get("FIXTURE_EMPTY"):
        return 0
    print("---------- output ----------", flush=True)
    print("ANSWER: " + prompt, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
