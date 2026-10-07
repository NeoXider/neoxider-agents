"""Artifact-free deterministic provider, all instrumentation outside its work dir."""
import json
import os
from pathlib import Path
import re
import sys
import time

capture = Path(os.environ["UX_CAPTURE"])
capture.mkdir(parents=True, exist_ok=True)
turn = len(list(capture.glob("prompt-*.txt"))) + 1
prompt = sys.stdin.buffer.read().decode("utf-8")
(capture / ("prompt-%s.txt" % turn)).write_text(prompt, encoding="utf-8")
(capture / ("argv-%s.json" % turn)).write_text(json.dumps(sys.argv[1:]), encoding="utf-8")
print("session id: " + (sys.argv[1] if len(sys.argv) > 1 else "ses_ux"), flush=True)
for index in range(int(os.environ.get("UX_NOISE_LINES", "0"))):
    print("[noise] " + str(index) + "x" * 1000, flush=True)
delay = float(os.environ.get("UX_DELAY", "0")) if turn == 1 else 0
if delay:
    print("[agent-activity] tool read", flush=True)
    time.sleep(delay)
match = re.search(r"Maintain a Markdown checkpoint file named EXACTLY (PROGRESS\.[A-Za-z0-9._-]+\.md)", prompt)
if match:
    Path(match.group(1)).write_text("# Progress\nFake provider checkpoint\n", encoding="utf-8")
print(os.environ.get("UX_ANSWER", "FINAL ANSWER"), flush=True)
sys.exit(int(os.environ.get("UX_EXIT", "0")))
