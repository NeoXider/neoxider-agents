"""Read one UTF-8 prompt from stdin and exchange it with Kimi ACP over stdio.

Kimi 0.34 print mode requires prompt argv. ACP preserves exact bytes and avoids
Windows' command-line limit without a prompt-bearing process command line.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Thread
from neoxider_agents.providers import ROOT, executable, hidden_options


def emit(event):
    sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
    sys.stdout.flush()


class Adapter:
    def __init__(self, process, chat_only=False):
        self.process = process
        self.chat_only = chat_only
        self.sequence = 0
        self.prompting = False
        self.text = ""
        self.total_chars = 0
        self.last_message = None

    def send(self, message):
        data = json.dumps(message, ensure_ascii=False).encode("utf-8") + b"\n"
        self.process.stdin.write(data)
        self.process.stdin.flush()

    def handle(self, message):
        method = message.get("method")
        params = message.get("params") or {}
        if method == "session/update" and self.prompting:
            update = params.get("update") or {}
            kind = update.get("sessionUpdate")
            if kind == "agent_message_chunk":
                content = update.get("content") or {}
                if content.get("type") == "text":
                    identity = update.get("messageId")
                    delta = content.get("text") or ""
                    if identity and self.last_message and identity != self.last_message:
                        delta = "\n" + delta
                    self.last_message = identity or self.last_message
                    self.text = (self.text + delta)[-262144:]
                    self.total_chars += len(delta)
                    emit({"role": "assistant", "content": delta, "type": "acp.delta"})
            elif kind in ("tool_call", "tool_call_update"):
                emit({"type": "tool_use", "part": {"tool": update.get("title") or update.get("kind") or "tool",
                       "state": {"input": update.get("rawInput") or {}, "status": update.get("status")}}})
            elif kind == "agent_thought_chunk":
                emit({"type": "thinking"})
        if method and "id" in message:
            if method == "session/request_permission":
                options = params.get("options") or []
                allows = [item for item in options if item.get("kind") in ("allow_once", "allow_always")]
                outcome = ({"outcome": "selected", "optionId": allows[0]["optionId"]}
                           if allows and not self.chat_only else {"outcome": "cancelled"})
                self.send({"jsonrpc": "2.0", "id": message["id"], "result": {"outcome": outcome}})
            else:
                self.send({"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32601, "message": "Client capability not enabled"}})

    def request(self, method, params):
        self.sequence += 1
        identity = self.sequence
        self.send({"jsonrpc": "2.0", "id": identity, "method": method, "params": params})
        while True:
            line = self.process.stdout.readline()
            if not line:
                raise RuntimeError("Kimi ACP ended before responding to " + method)
            try:
                message = json.loads(line.decode("utf-8", "replace"))
            except ValueError:
                emit({"type": "error", "message": line.decode("utf-8", "replace").strip()})
                continue
            if not isinstance(message, dict):
                continue
            if message.get("id") == identity and "method" not in message:
                if message.get("error"):
                    error = message["error"]
                    raise RuntimeError(error.get("message") or str(error))
                return message.get("result") or {}
            self.handle(message)


def stderr_pump(process):
    for line in process.stderr:
        sys.stderr.write("[kimi] " + line.decode("utf-8", "replace"))
        sys.stderr.flush()


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--model", default="")
    parser.add_argument("--session", default="")
    parser.add_argument("--chat-only", action="store_true")
    options = parser.parse_args(argv)
    prompt = sys.stdin.buffer.read().decode("utf-8-sig")
    args = executable("kimi")
    if options.model:
        args += ["-m", options.model]
    if options.chat_only:
        args += ["--agent-file", str(ROOT / "providers/kimi/chat-only-agent.md")]
    else:
        args += ["--auto"]
    args += ["acp"]
    process = subprocess.Popen(args, cwd=options.cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, **hidden_options())
    pump = Thread(target=stderr_pump, args=(process,), daemon=True)
    pump.start()
    adapter = Adapter(process, options.chat_only)
    try:
        init = adapter.request("initialize", {"protocolVersion": 1, "clientInfo": {"name": "neoxider-agents", "version": "2"},
                                             "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False}})
        params = {"cwd": str(Path(options.cwd).resolve()), "mcpServers": []}
        if options.session:
            capabilities = init.get("agentCapabilities") or {}
            if (capabilities.get("sessionCapabilities") or {}).get("resume") is not None:
                params["sessionId"] = options.session
                adapter.request("session/resume", params)
            elif capabilities.get("loadSession"):
                params["sessionId"] = options.session
                adapter.request("session/load", params)
            else:
                raise RuntimeError("Kimi ACP server does not support session resume/load")
            session = options.session
        else:
            session = adapter.request("session/new", params).get("sessionId")
        if not session:
            raise RuntimeError("Kimi ACP session/new returned no sessionId")
        emit({"role": "meta", "type": "session.resume_hint", "session_id": session})
        adapter.prompting = True
        adapter.request("session/prompt", {"sessionId": session, "prompt": [{"type": "text", "text": prompt}]})
        if adapter.text and adapter.total_chars <= 262144:
            emit({"role": "assistant", "content": adapter.text, "type": "acp.final"})
        return 0
    except (RuntimeError, OSError, ValueError) as error:
        emit({"type": "error", "message": str(error)})
        return 1
    finally:
        if process.stdin:
            process.stdin.close()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        pump.join(timeout=1)


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
