#!/usr/bin/env python3
"""OpenAI-compatible /v1/chat/completions bridge over a CLI subagent (claude/codex/opencode/gemini).

Run:   agent.sh openai-server -e claude -m sonnet -f high -p 8801
       (or directly: python openai_server.py -e codex -m spark -p 8802)

Point any OpenAI-compatible client's base_url at this server (e.g. http://127.0.0.1:8801/v1) --
including CoreAI's own COREAI_TEST_BASE_URL for the Game-Creation Benchmark -- and it drives the
configured CLI agent as the LLM backend for every /v1/chat/completions call. One process = one
fixed engine/model/effort; start several processes on different ports (and/or --dir) to compare
providers/models/efforts side by side, or to run more than one at once.

THE SESSION MODEL -- one API process = one ongoing chat session, not a fresh agent every call:
  - The bridge keeps the `messages` array from the previous call. When a new call's `messages`
    is that exact array PLUS one or more new turns appended at the end (a deterministic, exact
    prefix check -- not a guess), only the NEW turns are sent to the SAME underlying CLI session
    via `agent.sh reply` (resume), instead of re-serializing the whole growing history into a
    brand-new `agent.sh run` every time. This saves the resend cost of an ever-growing history
    AND lets the underlying provider's own prompt caching (which keys on repeated message
    structure) actually apply -- something a fresh mega-prompt every call could not benefit from.
  - Any mismatch (edited/rolled-back history, a genuinely different conversation, first call
    ever, or a dead/errored previous session) falls back SAFELY to a brand-new `agent.sh run`
    with the full history -- never resumes onto a session that might disagree with the caller.
  - Engines without CLI-level resume support (`opencode`, `gemini` -- see provider.json's
    "supports_resume") always take the fresh-run path; there is no continuation to lose for them.
  - Consequence: **one bridge process serves ONE conversation at a time**, not many concurrent
    ones -- a lock serializes overlapping requests. This is the deliberate trade-off for the
    token/latency win above; it is NOT a general-purpose multi-conversation server. If your
    client only ever runs one conversation against a given port at a time (e.g. one benchmark
    scenario end to end), this is a clean fit; do not point multiple unrelated conversations at
    the same port expecting them to stay independent.
  - `POST .../reset` clears the session (drops the remembered `messages`/task, wipes the scratch
    working dir unless `--dir` was pinned) so the next call starts completely fresh.
  - **Idle sessions expire** (`--session-ttl`, default 1800s = 30 minutes): a session that hasn't
    been called in longer than that is treated exactly like a dead one -- the next call falls
    back to a fresh run instead of resuming it. Mirrors how a real chat/API session would time
    out rather than stay resumable forever, and keeps an abandoned conversation's context from
    growing unbounded. `GET /health` reports `session_idle_seconds`/`session_ttl_seconds`.
  - The session's working directory persists for the session's lifetime (unlike a one-shot
    completion server that could safely use a disposable per-call temp dir) -- it is wiped and
    recreated whenever a brand-new session starts, unless `--dir` pins a real project path.

WHAT THIS IS -- still a wire-compatible shim, NOT a low-latency native LLM backend:
  - Latency is a full CLI subprocess invocation (seconds to low minutes), not a token stream.
  - `stream: true` replays the finished answer as word-sized SSE deltas -- this is NOT real
    per-token streaming from the underlying provider, just a wire-compatible reveal of a result
    that was already fully generated before the first chunk goes out.
  - `tools` / function-calling is EMULATED: the agent is instructed via the prompt to describe a
    tool call as a strict JSON block instead of using its own shell/file tools, which is then
    reformatted into a real OpenAI `tool_calls` response. This is best-effort prompting, not a
    protocol the CLI natively speaks -- it can occasionally ignore the instruction or misformat it.
    The instructions are re-sent on every call that includes `tools` (even a continuation), not
    just the first -- simpler and more robust than tracking whether the schema already "stuck".
  - `usage` token counts are ESTIMATES (~4 chars/token, flagged with "neoxider_estimated": true)
    -- the wrapped CLIs don't expose real counts in a structured form. Good enough for cost
    panels/dashboards; not billing-grade.
  - A completion whose CLI invocation came back empty or in an error state is retried
    (`--retries`, default 1) before the bridge gives up -- a real OpenAI endpoint effectively
    never returns an empty 200, and one transient CLI hiccup should not zero a whole scenario.
    An unexpected bridge exception surfaces as an OpenAI-style {"error": ...} HTTP 500, never a
    bare connection reset.
  - `content` is a clean answer for every bundled engine. codex's non-interactive `exec` mode
    otherwise mixes its own startup banner/session-id/error-log/"tokens used" chrome (and, on
    Windows, a cp866-mojibake OS-notification line) into the same stream as the answer, so the
    codex provider runs it via `codex exec --json` and extracts just the final agent message --
    see providers/codex/provider.sh (`_provider_codex_emit`). `claude`/`opencode`/`gemini` were
    already clean. (If a provider ever regresses, the bridge reads the CLI's captured output
    verbatim with no extra cleanup, so raw chrome would show through.)
Zero dependencies (stdlib only); mirrors gui.py's process/log conventions but is fully standalone
(does not import gui.py) so the two servers can run/fail independently.
"""
import argparse, glob, json, os, re, shutil, subprocess, sys, tempfile, threading, time, urllib.parse, uuid
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
    LAST "---------- output ----------" marker (a run may append a reply's marker too).
    Line-anchored (the marker must be a WHOLE line), matching agent.sh's awk `/^...$/` -- a
    substring match would truncate an answer that merely contains the marker text mid-sentence."""
    marker = "---------- output ----------"
    lines = text.split("\n")
    last = -1
    for i, ln in enumerate(lines):
        if ln == marker:
            last = i
    if last == -1:
        return text
    return "\n".join(lines[last + 1:]).lstrip("\n")


def read_meta(name):
    """Same format agent.sh itself writes (key=value lines) -- mirrors gui.py's identical
    helper. Used to check a session's task is still healthy (not error/stalled) before
    resuming it."""
    d = {}
    try:
        with open(os.path.join(LOGDIR, name + ".meta"), encoding="utf-8", errors="ignore") as f:
            for line in f:
                if "=" in line:
                    k, v = line.rstrip("\n").split("=", 1)
                    d[k] = v
    except OSError:
        pass
    return d


def session_scratch_dir():
    """Path (not created) of the ONE stable working dir this bridge process's ongoing session
    lives in -- fixed per port, so it's dedicated to this instance and never collides with
    another bridge/task using the same LOGDIR."""
    return os.path.join(tempfile.gettempdir(), "neoxider-openai-bridge", "session-%d" % CFG.port)


def fresh_session_dir():
    """(Re)creates the working dir for a BRAND-NEW session. Wipes any leftover files from a
    previous, unrelated conversation first -- since this dir now persists for a whole session's
    lifetime (not a disposable per-call temp dir), a stray file the agent wrote in an earlier,
    different conversation must not leak into this new one. Never touches an operator-pinned
    --dir (that's a real project path -- respected as-is, exactly like every other command in
    this project)."""
    if CFG.dir:
        return CFG.dir
    d = session_scratch_dir()
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d, exist_ok=True)
    return d


def new_task_name():
    return "openai-%d-%s" % (CFG.port, uuid.uuid4().hex[:12])


def is_extension(prev, new):
    """True when `new` is exactly `prev` plus one or more messages appended at the end -- the
    deterministic (not heuristic) check that lets a continuation safely resume the existing CLI
    session instead of guessing. Any other relationship (edited/rolled-back history, a shorter
    or unrelated array, no prior session) is NOT an extension and must fall back to a fresh run."""
    return len(new) > len(prev) and new[:len(prev)] == prev


def _chatonly_env():
    """Env for every agent.sh subprocess this bridge launches: AGENT_CHAT_ONLY=1 tells the
    codex/claude provider scripts to lock the CLI down to text-only completion -- no file writes,
    no shell execution, no MCP servers (verified live: without this, codex could actually call a
    real configured MCP tool, e.g. a live Unity Editor's unityMCP server, instead of just
    answering in text). Unset for a normal `agent.sh run`/`reply` outside this bridge, which
    legitimately needs full file/shell/MCP access to do real coding work -- see
    providers/{codex,claude}/provider.sh's `_provider_*_chatonly_args`."""
    env = dict(os.environ)
    env["AGENT_CHAT_ONLY"] = "1"
    return env


def run_agent(engine, model, effort, workdir, prompt, name, timeout):
    args = [BASH, SK, "run", "-e", engine, "-C", to_git_bash_path(workdir), "-t", name]
    if model:
        args += ["-m", model]
    if effort:
        args += ["-f", effort]
    args.append(prompt)
    try:
        subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                        errors="replace", timeout=timeout, cwd=HERE, env=_chatonly_env())
    except subprocess.TimeoutExpired:
        pass  # the log up to the timeout is still readable/useful below
    return last_output(read_log(name))


def reply_agent(engine, model, effort, workdir, name, answer, timeout):
    """Continues an existing task's CLI session via `agent.sh reply` -- same dispatch machinery
    `run` uses, just resuming instead of starting over. -m/-f are re-sent because some providers
    (claude) need them again on resume (see agent.sh's PROVIDER_*_RESUME_NEEDS_MODEL).

    Returns None (NOT the log's last answer) when the reply appended nothing to the log -- e.g.
    `agent.sh reply` died before writing a new block because the session id could not be resolved.
    Without this guard `last_output(read_log(name))` would echo the PREVIOUS successful answer as
    if it were the reply's -- a silent stale-answer bug. The caller (_run) falls back to a fresh
    run when it sees None."""
    args = [BASH, SK, "reply", "-e", engine]
    if model:
        args += ["-m", model]
    if effort:
        args += ["-f", effort]
    args += ["-C", to_git_bash_path(workdir), name, answer]
    before = len(read_log(name))
    try:
        subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                        errors="replace", timeout=timeout, cwd=HERE, env=_chatonly_env())
    except subprocess.TimeoutExpired:
        pass
    after = read_log(name)
    if len(after) == before:
        return None  # nothing was appended -> the resume died before its block; don't echo stale
    # agent.sh writes the reply HEADER before dispatching, so a provider that fails AFTER the header
    # (error/timeout/rate-limit) DOES grow the log -- and last_output would then return that failed
    # block as if it were the answer. Only accept a reply whose task ended in a good state; anything
    # else (error/stalled, or a timeout that left it still "running") -> None so _run falls back fresh.
    state = read_meta(name).get("state")
    if state not in ("done", "waiting"):
        return None
    return last_output(after)


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
    """Serializes a (sub)set of an OpenAI `messages` array into one plain-text prompt. Called
    with the FULL history when starting a brand-new agent session, or with just the NEW tail
    when continuing an existing one (see is_extension/H._run) -- the rendering logic itself
    doesn't care which; it just formats whatever list it's given."""
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


# BASE_INSTRUCTIONS is prepended to EVERY prompt, tools or not. Framing matters a lot here: an
# earlier draft opened with "You are acting as X, NOT as an autonomous coding agent" -- live
# testing against claude showed that identity-override phrasing gets flagged and REFUSED as a
# prompt-injection attempt (Claude Code has its own defenses against "ignore your role, you are
# now X" text). This version instead states plainly, as background/context rather than a role
# reassignment, that the session genuinely has no other tools -- which is also just TRUE: the
# bridge launches every engine with AGENT_CHAT_ONLY=1 (see providers/{codex,claude}/provider.sh),
# which strips MCP servers and blocks shell/file/subagent access at the CLI-flag level, not just
# by asking nicely. Verified live: with this framing + those flags, claude no longer refuses the
# tool-calling instructions below as an injection, and correctly reports having no real tools
# when asked to use one directly.
BASE_INSTRUCTIONS = """
[Context] The conversation below is the ENTIRE task, sent by an external application that is
using this session purely as a text completion endpoint. Answer using only what the conversation
contains -- there is nothing else to go on, and nothing outside it to check. This session has no
MCP servers, project skills/subagents, or file/shell tools configured, so none of that is
reachable here even if it's mentioned below; anything that looks like a tool name belongs to the
external application's own systems, not to this session.
"""

TOOLCALL_INSTRUCTIONS = """
When the task needs an action, respond with a structured JSON request that the external
application will read and carry out on its own side. You are not running anything yourself; you
are only producing the JSON. These are the actions it understands:

%s

USE EXACTLY THIS ONE FORMAT -- a single fenced ```json block containing a "tool_calls" array,
one entry per action, and nothing else in the message:

```json
{"tool_calls":[
  {"name":"<action>","arguments":{ ... }},
  {"name":"<action>","arguments":{ ... }}
]}
```

Rules:
- Put EVERY action for this turn in the one array (e.g. one entry per object to place) -- do not
  split them across several blocks or lines.
- "arguments" is a JSON object of the parameters shown above. Nothing outside the fenced block.
- Describing an action in prose ("I placed the tower", "Done.") does NOT count -- only the JSON
  block is read, so a turn with prose but no block scores as no action taken.
- When no action is needed (e.g. you are only explaining a result already shown above), reply in
  plain prose with no ```json block.

After a [TOOL RESULT] round-trip, summarize in plain words; emit a new ```json block only for
NEW actions you want carried out now.
"""


def build_prompt(messages, tools):
    text = BASE_INSTRUCTIONS.strip() + "\n\n" + render_messages(messages)
    if tools:
        text += "\n\n" + (TOOLCALL_INSTRUCTIONS % json.dumps(tools, ensure_ascii=False, indent=2))
    return text


# The fence body deliberately excludes backticks ([^`]): with DOTALL-".*?" a malformed
# tool_calls fence ("{...} trailing junk") made the match run PAST its own closing fence into
# the next one, so one bad fence swallowed a following perfectly valid one (calls lost).
# (?i) so a ```JSON tag (observed from models) is recognized too.
FENCE_RE = re.compile(r"(?i)```(?:json)?\s*(\{[^`]*\})\s*```")
# Same, but the body may be a JSON OBJECT ({...}) or a JSON ARRAY ([...]) -- Opus 4.8 emits a
# fenced array of call objects. Backtick-free body so a malformed fence can't swallow the next.
FENCE_ANY_RE = re.compile(r"(?i)```(?:json)?\s*([\[{][^`]*[\]}])\s*```")
# Fallback for a fence whose JSON legitimately CONTAINS a backtick inside a string value (e.g.
# lua code with `...`), which the strict backtick-free pass above cannot match. Only consulted
# when the strict pass produced nothing, and a body is only consumed when json.loads accepts it
# — so the old cross-fence swallowing cannot silently eat a valid neighbour.
FENCE_FALLBACK_RE = re.compile(r"(?i)```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
# A fenced code block with an explicit language tag (```lua, ```python, ...) is example/code
# content, not a place the model writes real Format-2 calls -- masked out before the func-syntax
# fallback so `world_command(...)` inside a lua example is not executed (see extract_tool_calls).
# EXCEPT call-intent tags: models label their real calls with fences like ```function_call /
# ```tool_call / ```tool_code (observed live from Opus 4.8) -- those are requests, not examples.
TAGGED_FENCE_RE = re.compile(r"```([A-Za-z][\w+-]*)[^\n]*\n.*?```", re.DOTALL)
CALL_INTENT_TAGS = {"function_call", "function_calls", "tool_call", "tool_calls",
                    "tool_code", "tool", "call", "calls", "toolcall", "functioncall"}
FUNC_HEAD_RE = re.compile(r"([A-Za-z_]\w*)\s*\(")
IDENT_RE = re.compile(r"^\w+$")


def _is_escaped(s, i):
    """True when s[i] is escaped by an ODD run of backslashes immediately before it. A single
    lookbehind (`s[i-1] == "\\"`) can't tell `\\"` (escaped quote) from `\\\\"` (escaped
    backslash + REAL closing quote) -- a string argument ending in a backslash (a Windows path
    like "C:\\Games\\") made the quote scanner think the string never closed and silently
    dropped the whole call."""
    n = 0
    k = i - 1
    while k >= 0 and s[k] == "\\":
        n += 1
        k -= 1
    return n % 2 == 1


def tool_names(tools):
    """The set of callable function names in an OpenAI `tools` array (both the modern
    {"type":"function","function":{"name":...}} and a bare {"name":...} shape)."""
    names = set()
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") if isinstance(t.get("function"), dict) else None
        n = (fn or {}).get("name") or t.get("name")
        if n:
            names.add(n)
    return names


def _canonical_args(args):
    """Stable comparison key for a call's arguments: parse if a JSON string, then dump with
    sorted keys -- so {"a":1,"b":2} and {"b":2,"a":1} (or the same dict re-serialized) compare
    equal. Unparsable strings fall back to their stripped raw text."""
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            return args.strip()
    try:
        return json.dumps(args, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return str(args)


def prior_call_keys(messages):
    """The set of (name, canonical-args) for every tool call ALREADY MADE earlier in this
    conversation (assistant messages carrying `tool_calls`). Used to drop ECHOES: after a
    tool-result round-trip, models tend to restate the calls they already made -- in exactly
    the `name({...})` spelling this conversation's history shows them (render_messages
    serializes prior calls that way) -- as part of a summary. Re-parsing those restated lines
    as NEW calls re-executed them each round (observed live: a 5-spawn scenario ballooned to
    15 tool calls across echo round-trips). Format-1 fenced JSON is exempt -- that block is an
    explicit, deliberate calling format, not a summary style."""
    keys = set()
    for m in messages or []:
        for tc in (m.get("tool_calls") or []) if isinstance(m, dict) else []:
            fn = tc.get("function") if isinstance(tc, dict) else None
            if isinstance(fn, dict) and fn.get("name"):
                keys.add((fn["name"], _canonical_args(fn.get("arguments", ""))))
    return keys


def tool_param_names(tools):
    """{function name: [parameter property names...]} from an OpenAI `tools` array's JSON
    schemas. Used by extract_func_calls to map a SINGLE positional scalar argument onto a
    one-parameter function (e.g. `execute_lua("print(1)")` -> {"code": "print(1)"}) -- with
    more than one parameter there is no safe mapping, so a positional scalar stays unparsed."""
    out = {}
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") if isinstance(t.get("function"), dict) else t
        n = fn.get("name")
        if not n:
            continue
        props = ((fn.get("parameters") or {}).get("properties") or {})
        out[n] = [k for k in props if isinstance(k, str)]
    return out


def _parse_arg_value(v):
    """Best-effort convert one function-call argument's raw text to a JSON value: strict JSON
    first (numbers, booleans, null, "quoted", [lists], {objects}), then a Python literal
    (single-quoted strings, True/False/None), then a bare unquoted string as a last resort."""
    v = v.strip()
    if not v:
        return ""
    try:
        return json.loads(v)
    except ValueError:
        pass
    try:
        import ast
        return ast.literal_eval(v)
    except (ValueError, SyntaxError):
        pass
    if len(v) >= 2 and v[0] in "\"'" and v[-1] == v[0]:
        return v[1:-1]
    return v


def _split_top_level(s, sep=","):
    """Split on `sep` only at the top level -- not inside quotes or ()/[]/{} nesting -- so an
    argument value that itself contains commas/brackets/quotes stays in one piece."""
    out, buf, depth, quote = [], [], 0, None
    for i, c in enumerate(s):
        if quote:
            buf.append(c)
            if c == quote and not _is_escaped(s, i):
                quote = None
        elif c in "\"'":
            quote = c
            buf.append(c)
        elif c in "([{":
            depth += 1
            buf.append(c)
        elif c in ")]}":
            depth -= 1
            buf.append(c)
        elif c == sep and depth == 0:
            out.append("".join(buf))
            buf = []
        else:
            buf.append(c)
    if buf:
        out.append("".join(buf))
    return out


def extract_func_calls(text, names, params=None):
    """Fallback tool-call syntax: CLI agents (codex especially) tend to emit calls as literal
    `name(arg=value, arg=value)` lines -- the way they'd WRITE a call in code -- instead of the
    prompted `{"tool_calls":[...]}` JSON block, so the JSON reparser missed a model that had
    actually solved the task. This recognizes every `name(...)` whose name is one of the known
    tool `names` (gate against false positives on prose), parenthesis-balanced and quote-aware,
    and returns [(name, args_dict), ...] plus the (start,end) span of each so the caller can
    strip them from the display text. Empty-paren mentions (`world_command()`) are treated as
    prose, not calls.

    Accepted argument spellings, in order of preference:
      1. name=value pairs:            world_command(action="spawn", x=1)
      2. one positional JSON object:  world_command({"action": "spawn", "x": 1})
         (gpt-5.5's dominant spelling -- literally how you'd write the call in an OpenAI SDK;
         dropping it silently zeroed whole benchmark groups before this was accepted)
      3. one positional scalar, ONLY when `params` says the function takes exactly one
         parameter: execute_lua("print(1)") -> {"code": "print(1)"}. A scalar that itself
         looks like a JSON-object blob is NOT wrapped this way -- that is shape 2 that failed
         to parse, and stuffing it into the sole parameter would double-wrap it."""
    if not names:
        return [], []
    found, spans = [], []
    consumed_end = -1  # a known-tool name INSIDE another call's string argument (e.g. lua code
    #                    mentioning world_command) must not become a second, phantom call
    for m in FUNC_HEAD_RE.finditer(text):
        name = m.group(1)
        if name not in names or m.start() < consumed_end:
            continue
        open_paren = m.end() - 1
        depth, quote, j = 0, None, open_paren
        while j < len(text):
            c = text[j]
            if quote:
                if c == quote and not _is_escaped(text, j):
                    quote = None
            elif c in "\"'":
                quote = c
            elif c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        if depth != 0:
            continue  # unbalanced parens -> not a clean call
        inner = text[open_paren + 1:j]
        if not inner.strip():
            continue  # `name()` with no args reads as prose, not a real call
        args = {}
        for seg in _split_top_level(inner):
            k, eq, val = seg.partition("=")
            k = k.strip()
            if not eq or not IDENT_RE.match(k):
                continue  # positional/garbled segment -> skip rather than guess
            args[k] = _parse_arg_value(val)
        if not args:
            val = _parse_arg_value(inner.strip())
            if isinstance(val, dict):
                # One positional JSON object -> it IS the arguments dict.
                args = {k: v for k, v in val.items() if isinstance(k, str)}
            else:
                props = (params or {}).get(name) or []
                looks_like_failed_object = isinstance(val, str) and val.lstrip().startswith("{")
                if len(props) == 1 and not looks_like_failed_object:
                    args = {props[0]: val}
        if not args:
            continue  # nothing parsed cleanly -> most likely prose that happened to match
        found.append((name, args))
        spans.append((m.start(), j + 1))
        consumed_end = j + 1
    return found, spans


def extract_bare_object_lines(text, tools):
    """Last-resort spelling, observed live from gpt-5.3-codex-spark in a single-tool scenario:
    the ENTIRE message is bare JSON argument objects, one per line, with no function name at all
    ({"action":"spawn","targetName":...} x400 -- the model 'saved' the redundant tool name).
    Deterministic recovery gate, deliberately strict so prose can never trigger it:
      - EVERY non-blank, non-fence-marker line must parse as a non-empty JSON object;
      - the union of every object's keys must fit (subset of parameter names) EXACTLY ONE tool
        across all lines -- ambiguity or any non-matching key rejects the whole message.
    Returns [(name, args), ...] or [] when the message is not this shape."""
    props_by_tool = tool_param_names(tools)
    objs = []
    for raw_line in text.split("\n"):
        ln = raw_line.strip()
        if not ln or ln.startswith("```"):
            continue  # blank / fence markers around the block don't disqualify it
        if not (ln.startswith("{") and ln.endswith("}")):
            return []
        try:
            o = json.loads(ln)
        except ValueError:
            return []
        if not isinstance(o, dict) or not o:
            return []
        objs.append(o)
    if not objs:
        return []
    candidates = None
    for o in objs:
        keys = {k for k in o if isinstance(k, str)}
        fits = {name for name, props in props_by_tool.items() if keys <= set(props)}
        candidates = fits if candidates is None else candidates & fits
        if not candidates:
            return []
    if len(candidates) != 1:
        return []
    name = next(iter(candidates))
    return [(name, o) for o in objs]


def _call_shaped(obj, names):
    """The object IS one tool call: either the OpenAI shape ({"function": {"name": ...}}, its
    own marker) or the flat shape with EXACTLY {name, arguments} keys (a data answer with extra
    fields must survive as content). The name must be a KNOWN declared tool."""
    if not isinstance(obj, dict):
        return None
    fn = obj.get("function") if isinstance(obj.get("function"), dict) else None
    flat_exact = set(obj.keys()) == {"name", "arguments"}
    call_name = (fn or {}).get("name") if fn else (obj.get("name") if flat_exact else None)
    if isinstance(call_name, str) and call_name and names and call_name in names:
        return obj
    return None


def _parse_call_jsonl(body, names):
    """A fence whose body is SEVERAL call objects, one per line (JSONL) -- observed live from
    Opus 4.8 ('the world_command calls (JSONL, one call per line)'), which is not valid JSON as
    a whole so the whole fence used to be dropped. Every non-blank line must parse as a
    call-shaped object; anything else rejects the whole body."""
    objs = []
    for ln in body.split("\n"):
        ln = ln.strip()
        if not ln:
            continue
        try:
            o = json.loads(ln)
        except ValueError:
            return None
        c = _call_shaped(o, names)
        if c is None:
            return None
        objs.append(c)
    return objs or None


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


def extract_tool_calls(text, names=None, tools=None, prior=None):
    """Best-effort: look for fenced ```json {"tool_calls":[...]} ``` block(s) (the LAST one
    that yields a non-empty call list wins, in case the agent narrates before committing to
    its final answer), falling back to a bare (unfenced) JSON object if that's the whole
    trailing message, and finally to literal `name(arg=value, ...)` / `name({...})` call
    syntax for any known tool `names` (see extract_func_calls -- codex CLI emits these
    instead of the JSON block). Pass the original `tools` array too when available: its JSON
    schemas let a single positional scalar map onto a one-parameter function. `prior` is the
    set from prior_call_keys(messages): func-syntax lines that exactly repeat an
    already-executed call are ECHOES of the conversation history's own rendering style, not
    new requests, and are dropped (kept in the display text) -- see prior_call_keys.
    Returns (tool_calls_or_None, display_text).

    Every fenced block that parses as a dict containing a "tool_calls" key is stripped from
    display_text regardless of whether it produced a real call -- despite the prompt telling
    it not to, a model sometimes still echoes a stray `{"tool_calls":[]}` alongside its real
    prose answer (observed live against Claude after a tool-result round-trip); leaving that
    JSON noise in a user-facing `content` string would be wrong even though no real call was
    intended."""
    calls = None
    cleaned = text
    call_fences = []  # per-fence LISTS of call objects, in reverse text order; un-reversed below
    for m in reversed(list(FENCE_ANY_RE.finditer(text))):
        body = m.group(1).strip()
        try:
            obj = json.loads(body)
        except ValueError:
            # Not valid JSON as a whole -- maybe SEVERAL call objects one per line (JSONL,
            # observed live from Opus 4.8). Only consumed when every line is call-shaped.
            jsonl = _parse_call_jsonl(body, names)
            if jsonl:
                call_fences.append(jsonl)
                cleaned = cleaned[:m.start()] + cleaned[m.end():]
            continue
        if isinstance(obj, list):
            # A JSON ARRAY of call objects in one fence (Opus 4.8's dominant spelling:
            # ```json [ {"name":...,"arguments":...}, ... ] ```). Consumed only when every
            # element is call-shaped, so a plain data array survives as content.
            shaped = [_call_shaped(o, names) for o in obj]
            if obj and all(s is not None for s in shaped):
                call_fences.append(shaped)
                cleaned = cleaned[:m.start()] + cleaned[m.end():]
            continue
        if not isinstance(obj, dict):
            continue
        if "tool_calls" not in obj:
            # ONE fenced JSON block PER CALL, shaped like an OpenAI tool-call object (observed
            # live from Sonnet 5) or the exact flat {name, arguments} spelling -- see
            # _call_shaped for the gates that keep plain JSON answers intact.
            c = _call_shaped(obj, names)
            if c is not None:
                call_fences.append([c])
                cleaned = cleaned[:m.start()] + cleaned[m.end():]
            continue
        if calls is None:
            raw = obj.get("tool_calls")
            if isinstance(raw, list) and raw:
                calls = _to_calls(raw)
        # No .strip() here: matches iterate in reverse over ORIGINAL offsets, and stripping
        # leading whitespace mid-loop shifted every earlier match's span, garbling the display
        # text when more than one fence was stripped. One strip at the end is enough.
        cleaned = cleaned[:m.start()] + cleaned[m.end():]
    if calls is None and call_fences:
        calls = _to_calls([o for fence in reversed(call_fences) for o in fence])
    if calls is None and not call_fences and "```" in cleaned:
        # Strict pass found nothing: retry with the backtick-tolerant fallback so a tool_calls
        # fence whose string values contain backticks still parses. json.loads gates every
        # candidate, so nothing is consumed unless it really is the call block.
        for m in reversed(list(FENCE_FALLBACK_RE.finditer(cleaned))):
            try:
                obj = json.loads(m.group(1))
            except ValueError:
                continue
            if isinstance(obj, dict) and isinstance(obj.get("tool_calls"), list) and obj["tool_calls"]:
                found = _to_calls(obj["tool_calls"])
                if found:
                    calls = found
                    cleaned = cleaned[:m.start()] + cleaned[m.end():]
                    break
    if calls is None:
        stripped = cleaned.strip()
        if stripped.startswith("{") and '"tool_calls"' in stripped:
            # raw_decode instead of loads: a bare (unfenced) JSON object followed by trailing
            # prose ("{...}\nDone.") is still a real call block -- the prose becomes the display
            # text instead of the whole turn being lost.
            try:
                obj, end = json.JSONDecoder().raw_decode(stripped)
                raw = obj.get("tool_calls") if isinstance(obj, dict) else None
                if isinstance(raw, list) and raw:
                    found = _to_calls(raw)
                    if found:
                        calls, cleaned = found, stripped[end:]
            except ValueError:
                pass
    if calls is None and names:
        # Mask language-tagged code fences (```lua, ```python, ...) before scanning ONLY when the
        # model is explaining -- i.e. there is real prose OUTSIDE the fences. Call syntax inside a
        # ```lua the model wrote as an EXAMPLE ("here is how you could...") must not execute. But
        # when the fenced block IS essentially the whole answer (little/no prose around it), it is
        # the model's actual call, whatever language tag it slapped on (spark wraps real
        # world_command(...) calls in ```python). Call-intent tags (```function_call) are always
        # treated as calls. Same-length whitespace replacement keeps every span valid in `cleaned`.
        prose_outside = TAGGED_FENCE_RE.sub(" ", cleaned)
        prose_outside = re.sub(r"```[^\n]*|```", " ", prose_outside)  # also drop bare-fence markers
        explaining = len(prose_outside.strip()) >= 40  # a sentence's worth of prose = an explainer
        if explaining:
            scan_text = TAGGED_FENCE_RE.sub(
                lambda m: m.group(0) if (m.group(1) or "").lower() in CALL_INTENT_TAGS
                else " " * len(m.group(0)),
                cleaned)
        else:
            scan_text = cleaned  # the fence is the answer -- scan its contents for real calls
        found, spans = extract_func_calls(scan_text, names, tool_param_names(tools))
        if found and prior:
            # Drop exact repeats of already-executed calls (echoes of the history's own
            # rendering style, not new requests -- see prior_call_keys). Their text spans stay
            # in the display text: an echo is part of the model's summary prose.
            fresh = [(i, (n, a)) for i, (n, a) in enumerate(found)
                     if (n, _canonical_args(a)) not in prior]
            spans = [spans[i] for i, _ in fresh]
            found = [fa for _, fa in fresh]
        if not found:
            # Nameless spelling: the whole message is bare JSON argument objects, one per line,
            # keys fitting exactly one tool (see extract_bare_object_lines). The whole message IS
            # the calls, so the display text ends up empty after span-stripping.
            bare = extract_bare_object_lines(scan_text, tools)
            if bare and prior:
                bare = [(n, a) for n, a in bare if (n, _canonical_args(a)) not in prior]
            if bare:
                found = bare
                spans = [(0, len(cleaned))] * len(bare)
        if found:
            calls = [{"id": "call_%s" % uuid.uuid4().hex[:24], "type": "function",
                      "function": {"name": n, "arguments": json.dumps(a, ensure_ascii=False)}}
                     for n, a in found]
            for s, e in reversed(spans):          # strip the call spans from the display text
                cleaned = cleaned[:s] + cleaned[e:]
            cleaned = cleaned.strip()
    return calls, cleaned.strip()


CFG = None  # set in main(); read by the request handler

# The one ongoing session this bridge process serves (see the module docstring's "THE SESSION
# MODEL"). SESSION_LOCK serializes every request that touches it -- by design, only one
# conversation is ever in flight against a given bridge instance at a time.
SESSION_LOCK = threading.Lock()
SESSION = {"task_name": None, "messages": [], "dir": None, "last_activity": 0.0}


class ProviderLimitError(Exception):
    """Raised when the CLI's answer is actually the provider's own usage-limit banner (e.g.
    Claude Code's "You've hit your session limit · resets 7:40am"). Returning that text as a
    normal 200 completion poisoned live benchmark runs — every scenario scored ~0 as if the
    MODEL failed, when the account was simply rate-limited. Surfaced as an OpenAI-style 429 so
    the caller can attribute it to the environment (and retrying inside the bridge is pointless
    -- the limit outlives any retry)."""


# Deliberately narrow: the whole answer must BE the banner — short, at most two lines, and the
# limit wording must appear right at the start (Claude's real banner opens with it). A short
# answer that merely RELAYS or QUOTES a limit phrase mid-sentence stays a normal completion.
LIMIT_BANNER_RE = re.compile(
    r"^\W{0,8}(you'?ve\s+|you have\s+)?(hit your (session|usage|weekly) limit"
    r"|usage limit (reached|exceeded)|rate limit (reached|exceeded))",
    re.IGNORECASE)


def looks_like_limit_banner(text):
    t = (text or "").strip()
    return (bool(t) and len(t) < 300 and t.count("\n") <= 1
            and bool(LIMIT_BANNER_RE.search(t)))


def _rough_tokens(s):
    """Crude ~4-chars-per-token estimate. The wrapped CLIs expose no structured token counts,
    but a rough non-zero `usage` is far more useful to dashboards/benchmark cost panels than the
    0/0/0 this bridge used to return -- callers that need exact billing numbers should not be
    pointed at a CLI bridge in the first place."""
    return (len(s or "") + 3) // 4


def session_idle_seconds():
    if not SESSION["task_name"]:
        return None
    return time.time() - SESSION["last_activity"]


def session_expired():
    """True when the remembered session has gone unused longer than --session-ttl seconds --
    treated exactly like a dead session (falls back to a fresh run), so an abandoned
    conversation can't be resumed indefinitely and its context can't grow forever. Mirrors how
    a real chat/API session would time out rather than stay resumable forever."""
    idle = session_idle_seconds()
    return idle is None or idle > CFG.session_ttl


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
            with SESSION_LOCK:
                active, turns = bool(SESSION["task_name"]), len(SESSION["messages"])
                idle = session_idle_seconds()
            self._send_json(200, {"ok": True, "engine": CFG.engine, "model": label,
                                   "session_active": active, "session_turns": turns,
                                   "session_idle_seconds": None if idle is None else int(idle),
                                   "session_ttl_seconds": CFG.session_ttl,
                                   "timeout_seconds": CFG.timeout, "retries": CFG.retries})
        elif p.endswith("/models"):
            self._send_json(200, {"object": "list", "data": [{"id": label, "object": "model", "owned_by": "neoxider-agents"}]})
        elif p == "/":
            with SESSION_LOCK:
                active, turns = bool(SESSION["task_name"]), len(SESSION["messages"])
            self._send_json(200, {"neoxider_openai_bridge": True, "engine": CFG.engine, "model": label,
                                   "endpoint": "POST .../chat/completions", "reset_endpoint": "POST .../reset",
                                   "session_active": active, "session_turns": turns})
        else:
            self._send_json(404, {"error": {"message": "not found: " + p}})

    def _run(self, messages, tools):
        """Implements THE SESSION MODEL (see module docstring): continue the existing CLI
        session via `agent.sh reply` when `messages` is a deterministic extension of what we
        saw last time and that session is still healthy; otherwise fall back to a brand-new
        `agent.sh run` with the full history. Holds SESSION_LOCK for the whole CLI call --
        by design, this bridge serves one conversation at a time."""
        with SESSION_LOCK:
            supports_resume = bool((PROVIDERS.get(CFG.engine) or {}).get("supports_resume"))
            prev_name = SESSION["task_name"]
            # Healthy means POSITIVELY finished ("done"/"waiting"), not merely "not errored":
            # after a subprocess timeout the wrapper's meta can stay "running" while an orphaned
            # CLI grandchild keeps appending to the log -- resuming onto that session could
            # misattribute the orphan's late output as the next reply's answer.
            healthy = (
                bool(prev_name)
                and not session_expired()
                and read_meta(prev_name).get("state") in ("done", "waiting")
            )
            raw_text = None
            if supports_resume and healthy and is_extension(SESSION["messages"], messages):
                new_turns = messages[len(SESSION["messages"]):]
                answer = build_prompt(new_turns, tools)
                raw_text = reply_agent(CFG.engine, CFG.model, CFG.effort, SESSION["dir"], prev_name, answer, CFG.timeout)
                if raw_text is not None and not raw_text.strip():
                    # A resume that "succeeded" but produced an EMPTY answer is as useless to the
                    # caller as one that died -- fall back to a fresh run rather than returning "".
                    raw_text = None
                name = prev_name
            if raw_text is None or not raw_text.strip():
                # First call, a non-extension, an unhealthy session, OR a resume that appended
                # nothing (reply_agent returned None). Any of these -> a clean fresh run with the
                # FULL history, never a stale echo of the previous answer. Empty/errored fresh
                # runs are retried (--retries, default 1): a real OpenAI endpoint effectively
                # never returns an empty 200, and a transient CLI hiccup (rate-limit blip, session
                # startup race) should not zero a whole benchmark scenario.
                prompt = build_prompt(messages, tools)
                workdir = fresh_session_dir()
                SESSION["dir"] = workdir
                for attempt in range(CFG.retries + 1):
                    name = new_task_name()
                    raw_text = run_agent(CFG.engine, CFG.model, CFG.effort, workdir, prompt, name, CFG.timeout)
                    state = read_meta(name).get("state")
                    if raw_text.strip() and state != "error":
                        break
                    if attempt < CFG.retries:
                        print("[openai-bridge] empty/errored completion (state=%s), retry %d/%d"
                              % (state, attempt + 1, CFG.retries), file=sys.stderr)
                        time.sleep(2)
            SESSION["task_name"] = name
            SESSION["messages"] = messages
            SESSION["last_activity"] = time.time()
        if looks_like_limit_banner(raw_text):
            raise ProviderLimitError(raw_text.strip())
        tool_calls, text = extract_tool_calls(raw_text, tool_names(tools), tools,
                                              prior_call_keys(messages))
        usage_prompt = _rough_tokens(build_prompt(messages, tools))
        usage_completion = _rough_tokens(raw_text)
        usage = {"prompt_tokens": usage_prompt, "completion_tokens": usage_completion,
                 "total_tokens": usage_prompt + usage_completion,
                 "neoxider_estimated": True}
        return text, tool_calls, usage

    def _reset_session(self):
        with SESSION_LOCK:
            old_dir = SESSION["dir"]
            SESSION["task_name"] = None
            SESSION["messages"] = []
            SESSION["dir"] = None
            SESSION["last_activity"] = 0.0
            if old_dir and not CFG.dir:
                shutil.rmtree(old_dir, ignore_errors=True)
        self._send_json(200, {"ok": True, "reset": True})

    def _sync_response(self, messages, tools):
        text, tool_calls, usage = self._run(messages, tools)
        message = {"role": "assistant", "content": None if tool_calls else text.strip()}
        if tool_calls:
            message["tool_calls"] = tool_calls
        self._send_json(200, {
            "id": "chatcmpl-%s" % uuid.uuid4().hex,
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model_label(CFG.engine, CFG.model, CFG.effort),
            "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if tool_calls else "stop"}],
            "usage": usage,
        })

    def _stream_response(self, messages, tools):
        text, tool_calls, _usage = self._run(messages, tools)
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
        if p.endswith("/reset"):
            return self._reset_session()
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
        try:
            if body.get("stream"):
                self._stream_response(messages, tools)
            else:
                self._sync_response(messages, tools)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass  # client went away -- nothing to answer
        except ProviderLimitError as e:
            try:
                self._send_json(429, {"error": {"message": str(e), "type": "rate_limit_error",
                                                 "code": "rate_limit_exceeded"}})
            except OSError:
                pass
        except Exception as e:  # noqa: BLE001 -- a bridge bug must surface as an OpenAI-style
            # error response, not a bare connection reset the client can't distinguish from a
            # network failure. (_run finishes before any response bytes go out, including in the
            # streaming path, so sending a 500 here is always still possible.)
            try:
                self._send_json(500, {"error": {"message": "bridge failure: %s" % e,
                                                 "type": "server_error"}})
            except OSError:
                pass


def main():
    global CFG
    ap = argparse.ArgumentParser(
        prog="openai_server.py",
        description="OpenAI-compatible /v1/chat/completions bridge over a CLI subagent.")
    ap.add_argument("-e", "--engine", default="codex", choices=sorted(PROVIDERS) or None,
                     help="which CLI subagent backs this server (default: codex)")
    ap.add_argument("-m", "--model", default="", help="model alias passed to agent.sh (default: provider default)")
    ap.add_argument("-f", "--effort", default="", help="effort level passed to agent.sh (default: provider default)")
    ap.add_argument("-C", "--dir", default="",
                     help="working dir for the agent's session (default: a scratch temp dir, "
                          "wiped and recreated each time a brand-new session starts). Pin this "
                          "to a real project path if the agent should operate there instead.")
    ap.add_argument("-p", "--port", type=int, default=int(os.environ.get("AGENT_OPENAI_PORT") or 8801))
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--timeout", type=int, default=240, help="max seconds to wait for one completion (default: 240)")
    ap.add_argument("--retries", type=int, default=1,
                     help="how many times to re-run a completion whose CLI invocation came back "
                          "empty or in an error state before giving up (default: 1)")
    ap.add_argument("--session-ttl", type=int, default=1800,
                     help="seconds of inactivity before the ongoing session is treated as expired and the "
                          "next call starts fresh instead of resuming it (default: 1800 = 30 minutes)")
    CFG = ap.parse_args()
    CFG.retries = max(0, CFG.retries)  # a negative value would skip the run loop entirely

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
