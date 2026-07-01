#!/usr/bin/env python3
"""tests/live_smoke_openai_server.py -- end-to-end smoke test for openai_server.py against a
REAL CLI subagent.

NOT part of the fast, free unit suites (test_gui.py / test_openai_server.py / test_agent_sh.sh)
-- those are pure-logic, zero real CLI calls, safe to run on every commit. This script actually
drives a real claude/codex/etc CLI subprocess through a live HTTP server: it costs real time (a
full CLI invocation per step, low minutes total) and real usage against your subscription. Run
it deliberately, not automatically:

    python tests/live_smoke_openai_server.py [--engine claude] [--model sonnet] [--effort low]

Exercises a real openai_server.py instance on a scratch port with AGENT_CLI_LOGS pointed at a
scratch temp dir (never touches your real ~/.claude/agent-cli-logs):
  - GET /health, GET /v1/models, error responses (empty/missing messages -> 400, invalid JSON
    body -> 400, unknown path -> 404)
  - a fresh completion
  - a session-continuation completion -- verifies the task count does NOT grow (for engines with
    supports_resume) and that the model actually recalls context from 2 turns earlier
  - a tool-calling round trip (call -> tool result -> final answer)
  - divergence -- a genuinely different conversation correctly starts a new session
  - POST .../reset
  - idle-timeout expiry (a short --session-ttl so the test finishes in seconds, not 30 minutes)
  - streaming (stream: true) -- verifies SSE chunks + a terminating "data: [DONE]"
  - two concurrent requests -- verifies no cross-contamination

Prints a final "N/N passed" summary and exits 1 on any failure, matching this project's other
test scripts' style (see tests/test_agent_sh.sh).
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)

PASS = 0
FAIL = 0


def check(cond, desc):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok   - %s" % desc)
    else:
        FAIL += 1
        print("  FAIL - %s" % desc)


def load_supports_resume(engine):
    try:
        with open(os.path.join(REPO_ROOT, "providers", engine, "provider.json"), encoding="utf-8") as f:
            return bool(json.load(f).get("supports_resume"))
    except Exception:
        return False


def http_get(base, path):
    with urllib.request.urlopen(base + path, timeout=30) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def http_post_json(base, path, body, timeout=200):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(base + path, data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def http_post_stream(base, path, body, timeout=200):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(base + path, data=data, headers={"Content-Type": "application/json"}, method="POST")
    lines = []
    with urllib.request.urlopen(req, timeout=timeout) as r:
        for raw in r:
            line = raw.decode("utf-8", errors="replace").strip()
            if line:
                lines.append(line)
    return lines


def wait_for_server(base, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(base + "/health", timeout=2)
            return True
        except Exception:
            time.sleep(0.3)
    return False


def count_tasks(logdir, port):
    return len([f for f in os.listdir(logdir) if f.startswith("openai-%d-" % port) and f.endswith(".meta")])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--engine", default="claude")
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--effort", default="low")
    ap.add_argument("--port", type=int, default=8977)
    args = ap.parse_args()

    supports_resume = load_supports_resume(args.engine)
    scratch_logdir = tempfile.mkdtemp(prefix="openai-server-smoke-logs-")
    env = dict(os.environ)
    env["AGENT_CLI_LOGS"] = scratch_logdir
    base = "http://127.0.0.1:%d" % args.port

    proc = subprocess.Popen(
        [sys.executable, os.path.join(REPO_ROOT, "openai_server.py"),
         "-e", args.engine, "-m", args.model, "-f", args.effort,
         "-p", str(args.port), "--timeout", "180", "--session-ttl", "6"],
        cwd=REPO_ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        print("[smoke] %s/%s/%s on port %d (supports_resume=%s), scratch logs at %s"
              % (args.engine, args.model, args.effort, args.port, supports_resume, scratch_logdir))
        if not wait_for_server(base):
            check(False, "server came up within 15s")
            return 1

        status, body = http_get(base, "/health")
        check(status == 200 and body.get("ok") is True, "GET /health returns ok")
        check(body.get("session_active") is False, "fresh server has no active session")

        status, body = http_get(base, "/v1/models")
        check(status == 200 and bool(body.get("data")), "GET /v1/models lists a model")

        status, _ = http_post_json(base, "/v1/chat/completions", {"messages": []})
        check(status == 400, "empty messages -> 400")
        status, _ = http_post_json(base, "/v1/chat/completions", {})
        check(status == 400, "missing messages -> 400")

        req = urllib.request.Request(base + "/v1/chat/completions", data=b"{not valid json",
                                      headers={"Content-Type": "application/json"}, method="POST")
        try:
            urllib.request.urlopen(req, timeout=10)
            check(False, "invalid JSON body -> 400")
        except urllib.error.HTTPError as e:
            check(e.code == 400, "invalid JSON body -> 400")

        try:
            urllib.request.urlopen(base + "/definitely/not/a/real/path", timeout=10)
            check(False, "unknown GET path -> 404")
        except urllib.error.HTTPError as e:
            check(e.code == 404, "unknown GET path -> 404")

        print("[smoke] fresh completion (real CLI call, please wait) ...")
        status, body = http_post_json(base, "/v1/chat/completions", {
            "messages": [{"role": "user", "content": "My favorite number is 42. Reply with just: OK"}]
        })
        check(status == 200, "fresh completion returns 200")
        check(bool(body.get("choices", [{}])[0].get("message", {}).get("content")), "fresh completion has content")
        n1 = count_tasks(scratch_logdir, args.port)
        check(n1 == 1, "exactly one task created after the first call (got %d)" % n1)

        print("[smoke] continuation (should recall context) ...")
        status, body = http_post_json(base, "/v1/chat/completions", {
            "messages": [
                {"role": "user", "content": "My favorite number is 42. Reply with just: OK"},
                {"role": "assistant", "content": "OK"},
                {"role": "user", "content": "What is my favorite number? Just the number."},
            ]
        })
        answer = body.get("choices", [{}])[0].get("message", {}).get("content") or ""
        check("42" in answer, "continuation correctly recalls context (got: %r)" % answer)
        n2 = count_tasks(scratch_logdir, args.port)
        if supports_resume:
            check(n2 == n1, "task count did not grow on continuation (still %d)" % n2)
        else:
            check(n2 == n1 + 1, "engine has no resume support -> continuation started a new session")
        _, health = http_get(base, "/health")
        check(health.get("session_turns") == 3, "session_turns reflects 3 messages (got %s)" % health.get("session_turns"))

        print("[smoke] tool-call turn ...")
        tools = [{"type": "function", "function": {
            "name": "get_weather", "description": "Get current weather for a city",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}}]
        status, body = http_post_json(base, "/v1/chat/completions", {
            "messages": [
                {"role": "user", "content": "My favorite number is 42. Reply with just: OK"},
                {"role": "assistant", "content": "OK"},
                {"role": "user", "content": "What is my favorite number? Just the number."},
                {"role": "assistant", "content": answer},
                {"role": "user", "content": "Now check the weather in Oslo. Use the tool."},
            ],
            "tools": tools,
        })
        msg = body.get("choices", [{}])[0].get("message", {})
        calls = msg.get("tool_calls") or []
        check(len(calls) == 1 and calls[0]["function"]["name"] == "get_weather", "model correctly requested get_weather")
        call_id = calls[0]["id"] if calls else "call_missing"

        print("[smoke] feeding the tool result back ...")
        status, body = http_post_json(base, "/v1/chat/completions", {
            "messages": [
                {"role": "user", "content": "My favorite number is 42. Reply with just: OK"},
                {"role": "assistant", "content": "OK"},
                {"role": "user", "content": "What is my favorite number? Just the number."},
                {"role": "assistant", "content": answer},
                {"role": "user", "content": "Now check the weather in Oslo. Use the tool."},
                {"role": "assistant", "content": None, "tool_calls": calls},
                {"role": "tool", "tool_call_id": call_id, "name": "get_weather", "content": "11C, rainy"},
            ],
            "tools": tools,
        })
        final_answer = body.get("choices", [{}])[0].get("message", {}).get("content") or ""
        check("11" in final_answer or "rain" in final_answer.lower(), "tool result correctly incorporated (got: %r)" % final_answer)
        n_after_tools = count_tasks(scratch_logdir, args.port)
        if supports_resume:
            check(n_after_tools == n1, "tool round-trip stayed in the same session")

        print("[smoke] unrelated conversation (should start a new session) ...")
        http_post_json(base, "/v1/chat/completions", {"messages": [{"role": "user", "content": "Reply with exactly: UNRELATED"}]})
        n3 = count_tasks(scratch_logdir, args.port)
        check(n3 == n_after_tools + 1, "divergence correctly started a new session (task count %d -> %d)" % (n_after_tools, n3))

        status, body = http_post_json(base, "/reset", {})
        check(status == 200 and body.get("reset") is True, "POST /reset succeeds")
        _, health = http_get(base, "/health")
        check(health.get("session_active") is False, "session inactive after reset")

        print("[smoke] idle-timeout expiry (waiting past --session-ttl=6s) ...")
        http_post_json(base, "/v1/chat/completions", {"messages": [{"role": "user", "content": "Reply with exactly: TTL-BASE"}]})
        n4 = count_tasks(scratch_logdir, args.port)
        time.sleep(8)
        http_post_json(base, "/v1/chat/completions", {
            "messages": [
                {"role": "user", "content": "Reply with exactly: TTL-BASE"},
                {"role": "assistant", "content": "TTL-BASE"},
                {"role": "user", "content": "Reply with exactly: TTL-AFTER"},
            ]
        })
        n5 = count_tasks(scratch_logdir, args.port)
        check(n5 == n4 + 1, "expired session fell back to a fresh run (task count %d -> %d)" % (n4, n5))

        print("[smoke] streaming request ...")
        http_post_json(base, "/reset", {})
        lines = http_post_stream(base, "/v1/chat/completions", {
            "messages": [{"role": "user", "content": "Reply with exactly: STREAM-OK"}], "stream": True
        })
        check(any(l == "data: [DONE]" for l in lines), "streaming response ends with data: [DONE]")
        check(any('"role": "assistant"' in l for l in lines), "streaming response has a role delta")

        print("[smoke] two concurrent requests ...")
        http_post_json(base, "/reset", {})
        results = {}

        def worker(key, text):
            _, b = http_post_json(base, "/v1/chat/completions", {"messages": [{"role": "user", "content": "Reply with exactly: " + text}]})
            results[key] = b.get("choices", [{}])[0].get("message", {}).get("content") or ""

        t1 = threading.Thread(target=worker, args=("a", "CONC-A"))
        t2 = threading.Thread(target=worker, args=("b", "CONC-B"))
        t1.start(); t2.start(); t1.join(); t2.join()
        check("CONC-A" in results.get("a", "") and "CONC-B" in results.get("b", ""),
              "concurrent requests both got their own correct answer")

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        shutil.rmtree(scratch_logdir, ignore_errors=True)
        shutil.rmtree(os.path.join(tempfile.gettempdir(), "neoxider-openai-bridge", "session-%d" % args.port), ignore_errors=True)

    print()
    print("%d/%d passed" % (PASS, PASS + FAIL))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
