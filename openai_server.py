#!/usr/bin/env python3
"""OpenAI-compatible /v1/chat/completions bridge over a CLI subagent (claude/codex/opencode/gemini).

Run:   agent.sh openai-server -e claude -m sonnet -f high -p 8801
       (or directly: python openai_server.py -e codex -m spark -p 8802)

Point any OpenAI-compatible client's base_url at this server (e.g. http://127.0.0.1:8801/v1) --
including CoreAI's own COREAI_TEST_BASE_URL for the Game-Creation Benchmark -- and it drives the
configured CLI agent as the LLM backend for every /v1/chat/completions call. One process = one
fixed engine/model/effort; start several processes on different ports (and/or --dir) to compare
providers/models/efforts side by side, or to run more than one at once.

WHAT THIS IS -- a wire-compatible shim, NOT a low-latency native LLM backend:
  - Every call is a FRESH, STATELESS `agent.sh run` (mirrors OpenAI's own stateless chat-completions
    contract -- the whole `messages` array is serialized into one prompt each call; nothing is
    resumed/remembered between calls, the CALLER's `messages` array IS the memory).
  - Latency is a full CLI subprocess invocation (seconds to low minutes), not a token stream.
  - `stream: true` replays the finished answer as word-sized SSE deltas -- this is NOT real
    per-token streaming from the underlying provider, just a wire-compatible reveal of a result
    that was already fully generated before the first chunk goes out.
  - `tools` / function-calling is EMULATED: the agent is instructed via the prompt to describe a
    tool call as a strict JSON block instead of using its own shell/file tools, which is then
    reformatted into a real OpenAI `tool_calls` response. This is best-effort prompting, not a
    protocol the CLI natively speaks -- it can occasionally ignore the instruction or misformat it.
  - `usage` token counts are always 0/0/0 -- the wrapped CLIs don't expose them in a structured form.
  - `content` can include raw CLI chrome for some engines -- e.g. codex's non-interactive `exec`
    mode prints its own startup banner/session-id/error-log lines to the same stream as the
    answer, and this bridge (like every other feature in this project -- `agent.sh last`, the
    GUI's chat view -- reads the CLI's raw captured output verbatim, with no engine-specific
    cleanup. `claude` was observed to return clean answers in testing; `codex` was not. Prefer
    `claude`/`opencode`/`gemini` when a clean `content` string matters to the caller.
Zero dependencies (stdlib only); mirrors gui.py's process/log conventions but is fully standalone
(does not import gui.py) so the two servers can run/fail independently.
"""
import argparse, glob, json, os, re, shutil, subprocess, sys, tempfile, time, urllib.parse, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
SK = os.path.join(HERE, "agent.sh").replace("\\", "/")
PROVIDERS_DIR = os.path.join(HERE, "providers")
LOGDIR = os.environ.get("AGENT_CLI_LOGS") or os.path.expanduser("~/.claude/agent-cli-logs")

BASH = os.environ.get("AGENT_SH_BASH")
if not BASH:
    for c in (r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files\Git\usr\bin\bash.exe",
              r"C:\Program Files (x86)\Git\bin\bash.exe"):
        if os.path.exists(c):
            BASH = c
            break
    else:
        BASH = "bash"


def load_providers():
    out = {}
    for pf in sorted(glob.glob(os.path.join(PROVIDERS_DIR, "*", "provider.json"))):
        name = os.path.basename(os.path.dirname(pf))
        try:
            with open(pf, encoding="utf-8") as f:
                out[name] = json.load(f)
        except Exception:
            continue
    return out


PROVIDERS = load_providers()


def to_git_bash_path(p):
    """C:/Git/CoreAI or C:\\Git\\CoreAI -> /c/Git/CoreAI -- see gui.py's identical helper."""
    p = (p or "").replace("\\", "/")
    m = re.match(r"^([A-Za-z]):/(.*)$", p)
    return "/%s/%s" % (m.group(1).lower(), m.group(2)) if m else p


def read_log(name):
    try:
        with open(os.path.join(LOGDIR, name + ".log"), encoding="utf-8", errors="ignore") as f:
            return f.read()
    except OSError:
        return ""


def last_output(text):
    """Same block agent.sh's own `last_output` awk one-liner extracts: everything after the
    LAST "---------- output ----------" marker (a run may append a reply's marker too)."""
    marker = "---------- output ----------"
    idx = text.rfind(marker)
    return text[idx + len(marker):].lstrip("\n") if idx != -1 else text


def scratch_dir(name):
    """Isolated per-request working dir so a completion-only call can't wander off and edit a
    real project's files -- used unless the operator explicitly pins --dir."""
    d = os.path.join(tempfile.gettempdir(), "neoxider-openai-bridge", name)
    os.makedirs(d, exist_ok=True)
    return d


def run_agent(engine, model, effort, workdir, prompt, name, timeout):
    args = [BASH, SK, "run", "-e", engine, "-C", to_git_bash_path(workdir), "-t", name]
    if model:
        args += ["-m", model]
    if effort:
        args += ["-f", effort]
    args.append(prompt)
    try:
        subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                        errors="replace", timeout=timeout, cwd=HERE)
    except subprocess.TimeoutExpired:
        pass  # the log up to the timeout is still readable/useful below
    return last_output(read_log(name))


def model_label(engine, model, effort):
    """Human-readable, versioned label for the `model` field in responses -- e.g.
    "claude/Sonnet 5 (low)", not the bare CLI alias ("claude/sonnet-low"), which doesn't say
    WHICH real model that alias currently points to (aliases like "opus"/"sonnet" are resolved
    to a specific dated model id by the provider's own CLI or by provider_<engine>_resolve;
    see providers/<engine>/provider.json's "model_labels" for the alias -> display-name map)."""
    p = PROVIDERS.get(engine, {})
    alias = model or (p.get("default_model") or "default")
    label = (p.get("model_labels") or {}).get(alias, alias)
    if effort:
        label = "%s (%s)" % (label, effort)
    return "%s/%s" % (engine, label)


def _content_text(content):
    """OpenAI message content is either a plain string or a list of content-part dicts
    ({"type":"text","text":...} / {"type":"image_url",...}). This bridge is text-only -- an
    image part is noted, not rendered, since the wrapped CLI agent can't see it either way."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict):
                if p.get("type") == "text":
                    parts.append(p.get("text", ""))
                elif p.get("type") in ("image_url", "image"):
                    parts.append("[image omitted -- this bridge is text-only]")
                else:
                    parts.append(json.dumps(p, ensure_ascii=False))
            else:
                parts.append(str(p))
        return "\n".join(parts)
    return str(content)


def render_messages(messages):
    """Serializes an OpenAI `messages` array into one plain-text prompt -- there is no session
    to resume, so the FULL history is re-sent to a fresh agent every call, exactly like a real
    OpenAI-compatible provider would see it from any other stateless client."""
    lines = []
    for m in messages:
        role = (m.get("role") or "user").upper()
        if role == "TOOL":
            name = m.get("name") or m.get("tool_call_id") or ""
            lines.append("[TOOL RESULT%s]\n%s" % (" for " + name if name else "", _content_text(m.get("content"))))
        elif m.get("tool_calls"):
            calls = "; ".join(
                "%s(%s)" % (tc.get("function", {}).get("name", "?"), tc.get("function", {}).get("arguments", ""))
                for tc in m["tool_calls"]
            )
            lines.append("[ASSISTANT CALLED FUNCTION] %s" % calls)
        else:
            lines.append("[%s]\n%s" % (role, _content_text(m.get("content"))))
    return "\n\n".join(lines)


TOOLCALL_INSTRUCTIONS = """
[Function-calling mode] You are acting as a plain text/function-calling completion backend for an
external application -- NOT as an autonomous coding agent. Do NOT use your own shell, file, or
edit tools for this request; do not modify any files on disk. You have exactly this set of
callable functions available to the CALLING APPLICATION (OpenAI function-calling schema):

%s

Decide ONE of:
(a) Answer directly in plain prose (no function call needed -- this is ALSO the right
    choice when a previous function call's result is already visible above and you are
    now just explaining/using that result), or
(b) Call exactly one function from the list above.

Your FINAL message must be ONLY one of:
- Plain prose with your answer, and NOTHING else -- no JSON, no code fence, not even an
  empty one (case a), or
- A single fenced block below and NOTHING else before or after it (case b):
```json
{"tool_calls":[{"name":"<function name>","arguments":{...arguments matching its parameters schema...}}]}
```
"""


def build_prompt(messages, tools):
    text = render_messages(messages)
    if tools:
        text += "\n\n" + (TOOLCALL_INSTRUCTIONS % json.dumps(tools, ensure_ascii=False, indent=2))
    return text


FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _to_calls(raw_calls):
    out = []
    for c in raw_calls:
        name = c.get("name") or (c.get("function") or {}).get("name")
        if not name:
            continue
        args = c.get("arguments", (c.get("function") or {}).get("arguments", {}))
        if not isinstance(args, str):
            args = json.dumps(args, ensure_ascii=False)
        out.append({"id": "call_%s" % uuid.uuid4().hex[:24], "type": "function",
                     "function": {"name": name, "arguments": args}})
    return out or None


def extract_tool_calls(text):
    """Best-effort: look for fenced ```json {"tool_calls":[...]} ``` block(s) (the LAST one
    that yields a non-empty call list wins, in case the agent narrates before committing to
    its final answer), falling back to a bare (unfenced) JSON object if that's the whole
    trailing message. Returns (tool_calls_or_None, display_text).

    Every fenced block that parses as a dict containing a "tool_calls" key is stripped from
    display_text regardless of whether it produced a real call -- despite the prompt telling
    it not to, a model sometimes still echoes a stray `{"tool_calls":[]}` alongside its real
    prose answer (observed live against Claude after a tool-result round-trip); leaving that
    JSON noise in a user-facing `content` string would be wrong even though no real call was
    intended."""
    calls = None
    cleaned = text
    for m in reversed(list(FENCE_RE.finditer(text))):
        try:
            obj = json.loads(m.group(1))
        except ValueError:
            continue
        if not isinstance(obj, dict) or "tool_calls" not in obj:
            continue
        if calls is None:
            raw = obj.get("tool_calls")
            if isinstance(raw, list) and raw:
                calls = _to_calls(raw)
        cleaned = (cleaned[:m.start()] + cleaned[m.end():]).strip()
    if calls is None:
        stripped = cleaned.strip()
        if stripped.startswith("{") and stripped.endswith("}") and '"tool_calls"' in stripped:
            try:
                obj = json.loads(stripped)
                raw = obj.get("tool_calls")
                if isinstance(raw, list) and raw:
                    found = _to_calls(raw)
                    if found:
                        calls, cleaned = found, ""
            except ValueError:
                pass
    return calls, cleaned.strip()


CFG = None  # set in main(); read by the request handler


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):  # silent
        pass

    def _send_json(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        p = urllib.parse.urlparse(self.path).path
        label = model_label(CFG.engine, CFG.model, CFG.effort)
        if p == "/health":
            self._send_json(200, {"ok": True, "engine": CFG.engine, "model": label})
        elif p.endswith("/models"):
            self._send_json(200, {"object": "list", "data": [{"id": label, "object": "model", "owned_by": "neoxider-agents"}]})
        elif p == "/":
            self._send_json(200, {"neoxider_openai_bridge": True, "engine": CFG.engine, "model": label,
                                   "endpoint": "POST .../chat/completions"})
        else:
            self._send_json(404, {"error": {"message": "not found: " + p}})

    def _run(self, messages, tools):
        prompt = build_prompt(messages, tools)
        name = "openai-%d-%s" % (CFG.port, uuid.uuid4().hex[:12])
        workdir = CFG.dir or scratch_dir(name)
        try:
            raw_text = run_agent(CFG.engine, CFG.model, CFG.effort, workdir, prompt, name, CFG.timeout)
        finally:
            if not CFG.dir:
                shutil.rmtree(workdir, ignore_errors=True)
        tool_calls, text = extract_tool_calls(raw_text)
        return text, tool_calls

    def _sync_response(self, messages, tools):
        text, tool_calls = self._run(messages, tools)
        message = {"role": "assistant", "content": None if tool_calls else text.strip()}
        if tool_calls:
            message["tool_calls"] = tool_calls
        self._send_json(200, {
            "id": "chatcmpl-%s" % uuid.uuid4().hex,
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model_label(CFG.engine, CFG.model, CFG.effort),
            "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if tool_calls else "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        })

    def _stream_response(self, messages, tools):
        text, tool_calls = self._run(messages, tools)
        cid = "chatcmpl-%s" % uuid.uuid4().hex
        created = int(time.time())
        label = model_label(CFG.engine, CFG.model, CFG.effort)

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        # No Content-Length is possible here (the body is generated as we go), and OpenAI SSE
        # clients are expected to stop at the "data: [DONE]" sentinel rather than rely on the
        # connection closing -- but close it anyway for plain HTTP clients (curl, requests, ...)
        # that don't know that convention and would otherwise hang waiting for more.
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        def emit(delta, finish_reason=None):
            chunk = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": label,
                      "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}]}
            self.wfile.write(("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n").encode("utf-8"))
            self.wfile.flush()

        try:
            emit({"role": "assistant"})
            if tool_calls:
                emit({"tool_calls": [
                    {"index": i, "id": tc["id"], "type": "function", "function": tc["function"]}
                    for i, tc in enumerate(tool_calls)
                ]})
                emit({}, "tool_calls")
            else:
                words = text.strip().split(" ")
                for i, w in enumerate(words):
                    emit({"content": w + (" " if i < len(words) - 1 else "")})
                emit({}, "stop")
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionAbortedError, OSError):
            pass  # client went away mid-stream -- nothing left to do

    def do_POST(self):
        p = urllib.parse.urlparse(self.path).path
        if not p.endswith("/chat/completions"):
            return self._send_json(404, {"error": {"message": "not found: " + p}})
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            return self._send_json(400, {"error": {"message": "invalid JSON body"}})
        messages = body.get("messages") or []
        if not messages:
            return self._send_json(400, {"error": {"message": "'messages' is required and must be a non-empty array"}})
        tools = body.get("tools") or None
        if body.get("stream"):
            self._stream_response(messages, tools)
        else:
            self._sync_response(messages, tools)


def main():
    global CFG
    ap = argparse.ArgumentParser(
        prog="openai_server.py",
        description="OpenAI-compatible /v1/chat/completions bridge over a CLI subagent.")
    ap.add_argument("-e", "--engine", default="codex", choices=sorted(PROVIDERS) or None,
                     help="which CLI subagent backs this server (default: codex)")
    ap.add_argument("-m", "--model", default="", help="model alias passed to agent.sh (default: provider default)")
    ap.add_argument("-f", "--effort", default="", help="effort level passed to agent.sh (default: provider default)")
    ap.add_argument("-C", "--dir", default="", help="working dir for the agent (default: a fresh scratch temp dir per call)")
    ap.add_argument("-p", "--port", type=int, default=int(os.environ.get("AGENT_OPENAI_PORT") or 8801))
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--timeout", type=int, default=240, help="max seconds to wait for one completion (default: 240)")
    CFG = ap.parse_args()

    try:
        srv = ThreadingHTTPServer((CFG.host, CFG.port), H)
    except OSError as e:
        print("[openai-bridge] could not bind %s:%d: %s" % (CFG.host, CFG.port, e), file=sys.stderr)
        sys.exit(1)

    label = model_label(CFG.engine, CFG.model, CFG.effort)
    base = "http://%s:%d" % (CFG.host, CFG.port)
    print("[openai-bridge] %s  ->  %s  (Ctrl-C to stop)" % (base, label))
    print("[openai-bridge] point an OpenAI-compatible client's base_url at %s/v1 (or %s -- both work)" % (base, base))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[openai-bridge] stopped")


if __name__ == "__main__":
    main()
