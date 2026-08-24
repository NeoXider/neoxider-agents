#!/usr/bin/env python3
"""Minimalist local web GUI over agent.sh — one dashboard for all CLI providers.

Run:  agent.sh gui [port] [--lan] [--token SECRET]   (or: python gui.py [port] ...)
Opens http://127.0.0.1:8765 by default -- a stable port so a browser tab can stay pinned
across restarts. Override with $AGENT_GUI_PORT or an explicit [port] arg.
Zero dependencies (stdlib).

LOOPBACK BY DEFAULT, AND A TOKEN IS MANDATORY OFF IT. Unlike openai_server.py (a text-only
completion bridge, safe enough to expose on a LAN), this panel LAUNCHES REAL SUBAGENTS: every
`run`/`reply` it starts inherits the provider's full-auto mode (`--dangerously-skip-permissions`,
`--sandbox danger-full-access`, `--yolo`, `--auto`) in any directory the caller names. Reachable
from the network without authentication, that is unauthenticated remote code execution on this
machine, so `--lan` REFUSES TO START without `--token SECRET` (or $AGENT_GUI_TOKEN). The token is
accepted as `?token=` (once -- it is then stored in a cookie), an `X-Agent-Token` header, or the
`agent_gui_token` cookie. Requests from loopback never need it: a local process could run agent.sh
directly anyway, and requiring it there would break the "is our panel already up?" probe.

The backend reads <name>.meta / <name>.log directly (fast, no parsing of `list`'s text),
while actions (run/reply/doctor) shell out to agent.sh so all the logic lives in one place.
Provider metadata (label/models/limits) is glob-loaded from providers/*/provider.json, and
per-provider info (version/login/rate limits) is fetched by shelling out to
`agent.sh provider-info <engine>` — the plugin's own provider.sh owns that logic, gui.py
does not hardcode any per-engine behavior.
"""
import json, math, logging, os, re, sys, time, subprocess, socket, urllib.parse, urllib.request, glob, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Opt-in diagnostics, same convention as openai_server.py: quiet (WARNING) unless AGENT_LOG_LEVEL
# says otherwise -- request lines at DEBUG, a bridge kill that could not go through at WARNING,
# expected failed health probes / stale-registry cleanups at DEBUG. Never logged anywhere:
# request bodies, prompts, answers, auth headers, the GUI token or API keys.
LOG = logging.getLogger("neoxider.gui")
if not LOG.handlers:  # a re-import must not stack duplicate handlers
    _lh = logging.StreamHandler()  # stderr
    _lh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    LOG.addHandler(_lh)
try:
    LOG.setLevel(os.environ.get("AGENT_LOG_LEVEL", "WARNING").upper())
except ValueError:  # garbage in AGENT_LOG_LEVEL -> stay quiet rather than refuse to start
    LOG.setLevel(logging.WARNING)
LOG.propagate = False

def to_git_bash_path(p):
    """C:/Git/CoreAI or C:\\Git\\CoreAI -> /c/Git/CoreAI — the canonical form agent.sh
    (git-bash) itself stores as dir= in .meta. Without this, GUI-launched tasks would be
    grouped separately from CLI-launched tasks in the same folder (different keys for the
    same directory)."""
    p = (p or "").replace("\\", "/")
    m = re.match(r"^([A-Za-z]):/(.*)$", p)
    return "/%s/%s" % (m.group(1).lower(), m.group(2)) if m else p

HERE = os.path.dirname(os.path.abspath(__file__))
# bash (git-bash) understands forward slashes in win paths, but NOT backslashes in argv -> normalize
SK = os.path.join(HERE, "agent.sh").replace("\\", "/")
HTML = os.path.join(HERE, "gui.html")
STATIC_DIR = os.path.join(HERE, "static")
LOCALES_DIR = os.path.join(HERE, "locales")
PROVIDERS_DIR = os.path.join(HERE, "providers")
# the exact git-bash agent.sh uses (otherwise native-python may pick up WSL bash and fail
# to find C:/... paths). Falls back to common git-bash locations so plain `python gui.py`
# also works (not just via `agent.sh gui`).
BASH = os.environ.get("AGENT_SH_BASH")
if not BASH:
    for c in (r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files\Git\usr\bin\bash.exe",
              r"C:\Program Files (x86)\Git\bin\bash.exe"):
        if os.path.exists(c):
            BASH = c
            break
    else:
        BASH = "bash"
LOGDIR = os.environ.get("AGENT_CLI_LOGS") or os.path.expanduser("~/.claude/agent-cli-logs")
PROJECTS_FILE = os.path.join(LOGDIR, "projects.json")
BRIDGES_DIR = os.path.join(LOGDIR, "bridges")  # openai_server.py drops bridge-<port>.json here
DOCTOR_CACHE_FILE = os.path.join(LOGDIR, "gui-doctor-cache.json")
# Silence threshold, shared with agent.sh (same env var, same default): a still-alive task that
# has not written anything for this long is reported as "running (no output for Nm)".
STALE_SEC = int(os.environ.get("AGENT_STALE_SEC") or 300)
MAX_BODY_BYTES = 10 * 1024 * 1024      # POST bodies above this get 413 before any byte is read
DEFAULT_WAIT_TIMEOUT = 60.0            # /api/wait ?timeout= is clamped to finite [0, cap]
WAIT_TIMEOUT_CAP = 300.0

_DEFAULT_PROVIDERS = {
    "codex":   {"label": "Codex", "models": ["5.5", "5.5-high", "spark"], "limits": "codex"},
    "claude":  {"label": "Claude", "models": ["sonnet", "opus", "haiku"], "limits": None},
    "kimi":    {"label": "Kimi Code", "models": ["k3", "k3-256k", "coding", "highspeed"], "limits": None},
    "opencode":{"label": "opencode", "models": [], "limits": None},
    "gemini":  {"label": "Gemini", "models": [], "limits": None},
}
def load_providers():
    """Glob-load providers/*/provider.json for display metadata (label/models/default_model/
    limits-flag). Falls back to a small built-in default set if the providers/ dir is missing
    or empty, so the GUI still works from a partial checkout."""
    out = {}
    for pf in sorted(glob.glob(os.path.join(PROVIDERS_DIR, "*", "provider.json"))):
        name = os.path.basename(os.path.dirname(pf))
        try:
            with open(pf, encoding="utf-8") as f:
                out[name] = json.load(f)
        except Exception:
            continue
    return out or _DEFAULT_PROVIDERS
PROVIDERS = load_providers()
ENGINES = list(PROVIDERS.keys())

def list_locales():
    """Scan locales/*.json for available UI languages -- dropping in one more file is
    enough to add a locale, no code change needed (the picker reads this list)."""
    out = []
    for lf in sorted(glob.glob(os.path.join(LOCALES_DIR, "*.json"))):
        code = os.path.splitext(os.path.basename(lf))[0]
        try:
            with open(lf, encoding="utf-8") as f:
                data = json.load(f)
            out.append({"code": code, "label": data.get("_label", code)})
        except Exception:
            continue
    return out

def load_projects():
    try:
        with open(PROJECTS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []
def save_projects(lst):
    try:
        with open(PROJECTS_FILE, "w", encoding="utf-8") as f:
            json.dump(lst, f, ensure_ascii=False)
    except OSError:
        pass

def task_state(name):
    """Effective state + raw meta for one task, for the /api/wait convenience endpoint --
    a lighter-weight lookup than list_tasks() since it only needs a single named task."""
    meta = read_meta(name)
    try:
        lm = os.path.getmtime(os.path.join(LOGDIR, name + ".log"))
    except OSError:
        lm = 0
    return eff_state(meta, lm, time.time()), meta

def read_meta(name):
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

def _win_pid_alive(pid):
    """Windows process liveness WITHOUT os.kill: on Windows python's os.kill(pid, 0) does NOT
    probe, it calls TerminateProcess -- using it as a liveness check would kill the task it asks
    about. OpenProcess+GetExitCodeProcess is the read-only equivalent."""
    import ctypes
    k32 = ctypes.windll.kernel32
    k32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    k32.OpenProcess.restype = ctypes.c_void_p
    k32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    PROCESS_QUERY_LIMITED_INFORMATION, STILL_ACTIVE, ERROR_ACCESS_DENIED = 0x1000, 259, 5
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, int(pid))
    if not h:
        return k32.GetLastError() == ERROR_ACCESS_DENIED  # exists, just not queryable by us
    try:
        code = ctypes.c_ulong()
        if k32.GetExitCodeProcess(h, ctypes.byref(code)):
            return code.value == STILL_ACTIVE
        return True
    finally:
        k32.CloseHandle(h)

def pid_alive(meta):
    """True / False / None (= unknown, e.g. a .meta written by an older version with no winpid).
    agent.sh records BOTH pid= (its own msys/unix pid) and winpid= (the Windows pid of the very
    same process) precisely so this side can check the same process the CLI checks."""
    if os.name == "nt":
        wp = str(meta.get("winpid") or "").strip()
        if not wp.isdigit():
            return None
        try:
            return _win_pid_alive(int(wp))
        except Exception:
            return None
    p = str(meta.get("pid") or "").strip()
    if not p.isdigit():
        return None
    try:
        os.kill(int(p), 0)          # POSIX only: signal 0 is a real, harmless probe there
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return None

def eff_state(meta, log_mtime, nowt):
    """THE state machine — mirrored verbatim in agent.sh's eff_state(), so the CLI and this panel
    can never contradict each other (they used to: the CLI looked only at the pid and said
    "running", this looked only at the log's mtime and said "stalled", for the same task).
      running + pid dead            -> stalled
      running + pid alive + quiet   -> idle  ("running (no output for Nm)") — an honest third state
      running + pid unknown + quiet -> stalled (the old mtime-only rule, for pre-winpid .meta files)
    A codex/claude step flushes its log only when it ENDS, so silence alone never means dead."""
    st = meta.get("state", "?")
    if st != "running":
        return st
    alive = pid_alive(meta)
    if alive is False:
        return "stalled"
    if log_mtime and (nowt - log_mtime) > STALE_SEC:
        return "idle" if alive else "stalled"
    return "running"

# states that mean "this task has not finished yet" (used by /api/wait and the SSE log stream)
LIVE_STATES = ("running", "idle")

def clamp_wait_timeout(raw, default=DEFAULT_WAIT_TIMEOUT, cap=WAIT_TIMEOUT_CAP):
    """Finite seconds in [0, cap] from an untrusted ?timeout= value; garbage/NaN -> default."""
    try:
        t = float(raw)
    except (TypeError, ValueError):
        return default
    if math.isnan(t):
        return default
    if not math.isfinite(t):
        return cap if t > 0 else 0.0
    return min(max(t, 0.0), cap)

def first_prompt(name):
    """First line of the first PROMPT — used as the "chat" title in the tree."""
    lines = read_log(name).splitlines()
    for i, l in enumerate(lines):
        if l.strip() in ("> PROMPT:", "> ANSWER:"):
            buf = []
            j = i + 1
            while j < len(lines) and not lines[j].startswith("----------"):
                if lines[j].strip():
                    buf.append(lines[j].strip())
                j += 1
            return (" ".join(buf))[:90]
    return ""

ACT_BY_STATE = {"done": "✅", "waiting": "⏳", "error": "❌", "stalled": "⚠️"}
ACT_RULES = [  # for running — based on the log's last line: what it's doing right now
    (("read", "open", "cat ", "grep", "ls "), "📖"),
    (("edit", "appl", "writ", "patch", "creat", "wrote"), "✏️"),
    (("test", "pytest", "npm test", "dotnet test"), "🧪"),
    (("run", "exec", "$ ", "bash", "compil", "build"), "🔧"),
    (("token", "usage"), "🧮"),
]
# TOPIC_RULES: task topic guessed from the first prompt's text. Keyword lists intentionally
# include Russian keywords (исправ/почин/etc.) alongside English ones, since chats may be in
# either language — these are matched data, not comments, so they stay untranslated.
TOPIC_RULES = [
    (("fix", "bug", "race", "crash", "исправ", "почин"), "🐛"),
    (("readme", "doc", "docs", "документ", "коммент"), "📖"),
    (("test", "тест", "spec"), "🧪"),
    (("refactor", "cleanup", "рефактор", "упрост"), "♻️"),
    (("audit", "review", "check", "verify", "аудит", "провер"), "🔍"),
    (("ui", "uitk", "panel", "window", "layout", "интерфейс"), "🎨"),
    (("benchmark", "perf", "бенчмарк"), "📊"),
    (("security", "secret", "token", "безопас"), "🔒"),
    (("add", "create", "implement", "feature", "добав", "созда", "реализ"), "✨"),
]

def activity_emoji(name, state):
    if state in ACT_BY_STATE:
        return ACT_BY_STATE[state]
    if state in LIVE_STATES:   # idle is still a live task, just a quiet one
        lines = [l for l in read_log(name).splitlines() if l.strip()]
        last = (lines[-1] if lines else "").lower()
        for kws, em in ACT_RULES:
            if any(k in last for k in kws):
                return em
        return "💭"
    return "•"

def topic_emoji(title):
    t = (title or "").lower()
    for kws, em in TOPIC_RULES:
        if any(k in t for k in kws):
            return em
    return "📝"

def list_tasks():
    nowt = time.time()
    out = []
    try:
        metas = [f for f in os.listdir(LOGDIR) if f.endswith(".meta")]
    except OSError:
        metas = []
    for mf in metas:
        name = mf[:-5]
        meta = read_meta(name)
        logp = os.path.join(LOGDIR, name + ".log")
        try:
            lm = os.path.getmtime(logp)
        except OSError:
            lm = os.path.getmtime(os.path.join(LOGDIR, mf)) if os.path.exists(os.path.join(LOGDIR, mf)) else 0
        st = eff_state(meta, lm, nowt)
        title = first_prompt(name)
        out.append({
            "name": name,
            "state": st,
            "engine": meta.get("engine", "?"),
            "model": meta.get("model", "?"),
            "dir": meta.get("dir", ""),
            "parent": meta.get("parent", ""),
            "title": title,
            "act": activity_emoji(name, st),
            "topic": topic_emoji(title),
            "files": meta.get("files", "0"),
            "exit": meta.get("exit", ""),
            "session": (meta.get("session", "") or "")[:8],
            "started": meta.get("started", ""),
            "kind": meta.get("kind", ""),  # "api-test" for agent.sh test-api tasks, else ""
            "timeout": meta.get("timeout", ""),  # set when the step watchdog killed the task (exit 124)
            "idle_sec": int(nowt - lm) if lm else None,
            "updated": lm,
        })
    out.sort(key=lambda t: t["updated"] or 0, reverse=True)
    return out

def list_dirs(path):
    try:
        return sorted(e for e in os.listdir(path)
                      if os.path.isdir(os.path.join(path, e)) and not e.startswith("."))
    except OSError:
        return []

def browse(raw):
    """Mini file browser (directories only) for picking a project working dir from the GUI."""
    base = os.path.abspath(raw.strip() or os.path.expanduser("~"))
    if not os.path.isdir(base):
        base = os.path.expanduser("~")
    parent = os.path.dirname(base)
    if parent == base:
        parent = ""
    return {
        "path": base.replace("\\", "/"),
        "parent": parent.replace("\\", "/") if parent else "",
        "dirs": list_dirs(base),
        "shortcuts": [p.replace("\\", "/") for p in
                      [os.path.expanduser("~"), "C:/Git", "C:/", HERE] if os.path.isdir(p)],
    }

def read_log(name):
    try:
        with open(os.path.join(LOGDIR, name + ".log"), encoding="utf-8", errors="ignore") as f:
            return f.read()
    except OSError:
        return ""

# ---- Full-dialog parsing (/api/dialog) ---------------------------------------------------
# The chat tab shows the WHOLE conversation of a task, Claude-Code-style: every run/reply
# step in order, tool calls as collapsed one-liners, thinking blocks present in the data but
# hidden by default, and real durations wherever the log actually carries timestamps.
# Engines log very differently, so the parser is layered and ALWAYS degrades to raw text:
#   1. current agent.sh format:  "========== [run] <ts> | engine=.. ==========" steps with
#      "> PROMPT:" / "> ANSWER:" and "---------- output ----------" sections;
#   2. legacy codex plaintext inside the output: block marker lines "user"/"codex"/
#      "thinking"/"exec", tool results ending in "Wall time: N seconds" / "succeeded in Nms:";
#   3. raw structured JSONL (codex `exec --json`, kimi/claude stream-json) — possible verbatim
#      in a log when a provider's python cleanup layer is missing and it cats the raw stream;
#   4. anything else -> one raw text block, never an empty pane, never an exception.

def fmt_dur(sec):
    """Short human duration: 1.2s / 45s / 3m 20s / 1h 5m. None or negative -> "" (the caller
    shows NOTHING rather than a made-up number when the log has no timestamps)."""
    if sec is None:
        return ""
    try:
        sec = float(sec)
    except (TypeError, ValueError):
        return ""
    if sec < 0:
        return ""
    if sec < 9.95:
        return "%.1fs" % sec
    n = int(round(sec))
    if n < 60:
        return "%ds" % n
    m, s = divmod(n, 60)
    if m < 60:
        return "%dm %ds" % (m, s) if s else "%dm" % m
    h, m = divmod(m, 60)
    return "%dh %dm" % (h, m) if m else "%dh" % h

_STEP_HDR = re.compile(r"^=+\s*\[(\w+)\]\s+(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s*\|\s*(.*?)\s*=+\s*$")
_OUT_MARK = "---------- output ----------"
_CODEX_MARKS = ("user", "codex", "thinking", "exec")

def _ts_epoch(ts):
    try:
        return time.mktime(time.strptime(ts, "%Y-%m-%d %H:%M:%S"))
    except (ValueError, OverflowError):
        return None

def _dur_ms_from_result(text):
    """Per-tool-call duration from codex's own report lines, if present ('Wall time:
    63.5 seconds' / 'succeeded in 1115ms:' / 'in 1.2s:'). None when absent."""
    m = re.search(r"Wall time:\s*([\d.]+)\s*seconds", text)
    if m:
        return int(float(m.group(1)) * 1000)
    # codex prints "succeeded in 1115ms:" / "failed in 1.2s:" around the command's output
    m = re.search(r"(?:succeeded|failed)\s+in\s+([\d.]+)\s*ms\b", text)
    if m:
        return int(float(m.group(1)))
    m = re.search(r"(?:succeeded|failed)\s+in\s+([\d.]+)\s*s\b", text)
    if m:
        return int(float(m.group(1)) * 1000)
    return None

def _text_block(blocks, text):
    """Append/merge a text block (consecutive text fragments become one block)."""
    text = (text or "").strip("\n")
    if not text.strip():
        return
    if blocks and blocks[-1]["type"] == "text":
        blocks[-1]["text"] += "\n" + text
    else:
        blocks.append({"type": "text", "text": text})

def _parse_jsonl(lines):
    """Structured JSONL streams. Returns None when nothing recognisable was found (caller then
    tries the next format). Recognises codex `exec --json` events, kimi stream-json roles and
    claude stream-json message envelopes; unknown event kinds are skipped, never fatal."""
    blocks, recognized, last_tool = [], 0, None
    for line in lines:
        s = line.strip()
        if not s or s[0] != "{":
            continue
        try:
            o = json.loads(s)
        except ValueError:
            continue
        if not isinstance(o, dict):
            continue
        t = o.get("type")
        # --- codex exec --json: {"type":"item.completed","item":{...}}
        if t in ("item.started", "item.completed") and isinstance(o.get("item"), dict):
            recognized += 1
            if t != "item.completed":
                continue
            it = o["item"]
            it_t = it.get("type")
            if it_t == "agent_message":
                _text_block(blocks, it.get("text") or "")
            elif it_t == "reasoning":
                txt = it.get("text") or ""
                if txt.strip():
                    blocks.append({"type": "thinking", "text": txt.strip("\n")})
            elif it_t == "command_execution":
                last_tool = {"type": "tool", "name": "exec", "arg": it.get("command") or "",
                             "result": (it.get("aggregated_output") or "").strip("\n"),
                             "dur_ms": None}
                blocks.append(last_tool)
            elif it_t in ("mcp_tool_call", "custom_tool_call"):
                last_tool = {"type": "tool",
                             "name": it.get("tool") or it.get("name") or it_t,
                             "arg": json.dumps(it.get("arguments") or it.get("input") or "", ensure_ascii=False),
                             "result": json.dumps(it.get("result") or it.get("output") or "", ensure_ascii=False),
                             "dur_ms": None}
                blocks.append(last_tool)
            elif it_t == "file_change":
                ch = it.get("changes") or []
                arg = ", ".join(str(c.get("path") or "") for c in ch if isinstance(c, dict))
                blocks.append({"type": "tool", "name": "file_change", "arg": arg,
                               "result": "", "dur_ms": None})
        elif t in ("thread.started", "turn.started", "turn.completed", "turn.failed"):
            recognized += 1  # codex session chrome -- known, but nothing to render
        # --- kimi stream-json: {"role":"assistant","content":...,"tool_calls":[...]}
        elif o.get("role") == "assistant":
            recognized += 1
            content = o.get("content")
            if isinstance(content, list):  # claude-style content blocks inside a kimi/claude line
                for c in content:
                    if not isinstance(c, dict):
                        continue
                    if c.get("type") == "text":
                        _text_block(blocks, c.get("text") or "")
                    elif c.get("type") == "thinking":
                        blocks.append({"type": "thinking", "text": (c.get("thinking") or "").strip("\n")})
                    elif c.get("type") == "tool_use":
                        last_tool = {"type": "tool", "name": c.get("name") or "tool",
                                     "arg": json.dumps(c.get("input") or "", ensure_ascii=False),
                                     "result": "", "dur_ms": None}
                        blocks.append(last_tool)
            elif isinstance(content, str):
                _text_block(blocks, content)
            for tc in (o.get("tool_calls") or []):
                fn = (tc or {}).get("function") or {}
                last_tool = {"type": "tool", "name": fn.get("name") or "tool",
                             "arg": str(fn.get("arguments") or ""), "result": "", "dur_ms": None}
                blocks.append(last_tool)
        elif o.get("role") == "tool":
            recognized += 1
            res = o.get("content")
            if not isinstance(res, str):
                res = json.dumps(res, ensure_ascii=False)
            if last_tool is not None and not last_tool["result"]:
                last_tool["result"] = res.strip("\n")
            else:
                blocks.append({"type": "tool", "name": o.get("name") or "tool", "arg": "",
                               "result": res.strip("\n"), "dur_ms": None})
        elif o.get("role") in ("meta", "user", "system"):
            recognized += 1  # session hints / echoed input -- not rendered
        # --- claude stream-json envelope: {"type":"assistant","message":{"content":[...]}}
        elif t in ("assistant", "user") and isinstance(o.get("message"), dict):
            recognized += 1
            content = (o["message"] or {}).get("content")
            if isinstance(content, list):
                for c in content:
                    if not isinstance(c, dict):
                        continue
                    ct = c.get("type")
                    if ct == "text":
                        _text_block(blocks, c.get("text") or "")
                    elif ct == "thinking":
                        blocks.append({"type": "thinking", "text": (c.get("thinking") or "").strip("\n")})
                    elif ct == "tool_use":
                        last_tool = {"type": "tool", "name": c.get("name") or "tool",
                                     "arg": json.dumps(c.get("input") or "", ensure_ascii=False),
                                     "result": "", "dur_ms": None}
                        blocks.append(last_tool)
                    elif ct == "tool_result":
                        res = c.get("content")
                        if not isinstance(res, str):
                            res = json.dumps(res, ensure_ascii=False)
                        if last_tool is not None and not last_tool["result"]:
                            last_tool["result"] = res.strip("\n")
            elif isinstance(content, str):
                _text_block(blocks, content)
        elif t in ("system", "result", "stream_event", "rate_limit_event"):
            recognized += 1
    return blocks if recognized else None

def _parse_codex_plaintext(lines):
    """Legacy codex `exec` plaintext: bare marker lines user/codex/thinking/exec delimit blocks;
    an exec block's first line is the command, the rest is its output (ending in a duration
    line). Banner/chrome before the first marker and the trailing 'tokens used' block are
    dropped. 'user' blocks are returned as user_text so the caller can use them as the step
    prompt when the log has no > PROMPT: section (very old logs have no step headers at all)."""
    blocks, mode, buf = [], None, []
    def flush():
        nonlocal buf
        text = "\n".join(buf).strip("\n")
        buf = []
        if mode in (None, "user", "skip"):
            if mode == "user" and text.strip():
                blocks.append({"type": "user_text", "text": text})
            return
        if mode == "thinking":
            if text.strip():
                blocks.append({"type": "thinking", "text": text})
            return
        if mode == "exec":
            parts = text.split("\n", 1)
            cmd = parts[0].strip()
            result = parts[1] if len(parts) > 1 else ""
            blocks.append({"type": "tool", "name": "exec", "arg": cmd,
                           "result": result.strip("\n"),
                           "dur_ms": _dur_ms_from_result(result)})
            return
        _text_block(blocks, text)  # mode == "codex"
    for line in lines:
        if line in _CODEX_MARKS:
            flush()
            mode = line
        elif line.startswith("tokens used"):
            flush()
            mode = "skip"
        else:
            buf.append(line)
    flush()
    return blocks

def parse_output_blocks(text):
    """One step's raw output -> ordered blocks (text / tool / thinking / user_text).
    Never raises; unrecognised content comes back as a single raw text block."""
    try:
        lines = text.split("\n")
        blocks = _parse_jsonl(lines)
        if blocks is not None:
            return blocks
        if any(l in _CODEX_MARKS for l in lines):
            return _parse_codex_plaintext(lines)
        blocks = []
        _text_block(blocks, text)
        return blocks
    except Exception:
        return [{"type": "text", "text": text}] if text else []

def parse_dialog(text, log_mtime=0, now=None):
    """Whole .log -> ordered step list. A step is one run/reply: its prompt, its parsed output
    blocks, its start timestamp and (when derivable) its duration. Duration sources, in order:
    the NEXT step's header timestamp (real measured interval), else the log's mtime for the
    final step of a finished task. A log with no step headers at all becomes one synthetic
    'log' step over the raw content — the unrecognised-format fallback."""
    now = time.time() if now is None else now
    steps, cur, section = [], None, None
    def new_step(kind, ts="", info=""):
        return {"kind": kind, "ts": ts, "epoch": _ts_epoch(ts) if ts else None,
                "info": info, "prompt_label": "", "prompt_lines": [], "out_lines": []}
    for line in text.split("\n"):
        m = _STEP_HDR.match(line)
        if m:
            cur = new_step(m.group(1), m.group(2), m.group(3))
            steps.append(cur)
            section = None
            continue
        if cur is None:
            if not line.strip():
                continue  # hdr() writes a leading blank line before the first step header
            cur = new_step("log")  # no header (legacy/foreign log): everything is output
            steps.append(cur)
            section = "out"
        if line in ("> PROMPT:", "> ANSWER:"):
            section = "prompt"
            cur["prompt_label"] = line[2:-1]
        elif line == _OUT_MARK:
            section = "out"  # repeated markers (provider cleanup chrome) just re-enter output
        elif section == "prompt":
            cur["prompt_lines"].append(line)
        elif section == "out":
            cur["out_lines"].append(line)
    for i, st in enumerate(steps):
        st["prompt"] = "\n".join(st.pop("prompt_lines")).strip()
        blocks = parse_output_blocks("\n".join(st.pop("out_lines")))
        if not st["prompt"] and blocks and blocks[0]["type"] == "user_text":
            st["prompt"] = blocks.pop(0)["text"]  # legacy log: the 'user' block IS the prompt
        st["blocks"] = [b for b in blocks if b["type"] != "user_text"] or blocks
        end = steps[i + 1]["epoch"] if i + 1 < len(steps) else (log_mtime or None)
        dur = (end - st["epoch"]) if (st["epoch"] and end) else None
        st["duration_s"] = max(0.0, dur) if dur is not None else None
        st["duration"] = fmt_dur(st["duration_s"])
        for b in st["blocks"]:
            if b.get("dur_ms") is not None:
                b["duration"] = fmt_dur(b["dur_ms"] / 1000.0)
    return steps

_DIALOG_BLOCK_CAP = 20000  # chars per block in the default (non-full) payload
_DIALOG_STEP_LIMIT = 30    # steps in the default payload; full=1 lifts both caps
_DIALOG_CACHE = {}         # single-slot: the panel views one task at a time

def dialog_payload(name, full=False, offset=None, limit=None):
    """Structured conversation for the chat tab. Default payload = the LAST
    _DIALOG_STEP_LIMIT steps with oversized blocks capped (has_more flags the rest);
    full=1 returns every step and every byte — the 'show the whole dialog' control.
    Parsed steps are cached keyed by (mtime, size) so the 3s auto-refresh re-parses only
    when the log actually changed."""
    try:
        logp = os.path.join(LOGDIR, name + ".log")
        st_ = os.stat(logp)
        mtime, size = st_.st_mtime, st_.st_size
    except OSError:
        mtime, size = 0, 0
    now = time.time()
    meta = read_meta(name)
    state = eff_state(meta, mtime, now)
    key = (name, mtime, size)
    hit = _DIALOG_CACHE.get("parsed")
    if hit and hit[0] == key:
        steps = hit[1]
    else:
        steps = parse_dialog(read_log(name), mtime, now)
        _DIALOG_CACHE.clear()
        _DIALOG_CACHE["parsed"] = (key, steps)
    total = len(steps)
    if full:
        off, lim = 0, total
    else:
        lim = limit if limit is not None else _DIALOG_STEP_LIMIT
        off = offset if offset is not None else max(0, total - lim)
    out = []
    for i in range(off, min(total, off + lim)):
        st = dict(steps[i])
        st["i"] = i
        if not full:
            blocks = []
            for b in st["blocks"]:
                b = dict(b)
                for fld in ("text", "arg", "result"):
                    v = b.get(fld)
                    if isinstance(v, str) and len(v) > _DIALOG_BLOCK_CAP:
                        b[fld] = v[:_DIALOG_BLOCK_CAP]
                        b["truncated"] = True
                blocks.append(b)
            st["blocks"] = blocks
        out.append(st)
    return {"name": name, "state": state, "engine": meta.get("engine", "?"),
            "model": meta.get("model", "?"), "total_steps": total, "offset": off,
            "has_more": off > 0, "steps": out, "now": now,
            "mtime": mtime, "log_size": size}

def spawn(args, terminal=False):
    """Background launch of agent.sh. terminal=True -> a separate console window with a live chat view."""
    kw = dict(cwd=HERE)
    if terminal and os.name == "nt":
        kw["creationflags"] = 0x00000010  # CREATE_NEW_CONSOLE — chat visible live
    elif os.name == "nt":
        kw.update(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        kw["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | NEW_PROCESS_GROUP
    else:
        kw.update(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        kw["start_new_session"] = True
    subprocess.Popen([BASH, SK] + args, **kw)

def run_sync(args, timeout=30):
    try:
        p = subprocess.run([BASH, SK] + args, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout, cwd=HERE)
        return (p.stdout or "") + (p.stderr or "")
    except Exception as e:
        return "error: %s" % e

# Doctor/provider-info both shell out to agent.sh, which is not cheap (subprocess + the
# provider's own CLI --version/login calls) -- cache each by key with a short TTL so switching
# providers or an idle poll doesn't re-shell-out every few seconds. A manual "refresh" button in
# the GUI bypasses the cache via ?force=1. Read-through: return the cached value if still fresh,
# otherwise recompute and repopulate -- and always keep the last-good value even on a failed
# recompute, so a transient error doesn't blank out a panel that was showing good data.
_CACHE = {}
_CACHE_TTL = 30
_DOCTOR_REFRESHING = False
_DOCTOR_LOCK = threading.Lock()

def _cached(key, compute, force=False):
    now = time.time()
    hit = _CACHE.get(key)
    if not force and hit and (now - hit["at"]) < _CACHE_TTL:
        return hit["value"], True
    value = compute()
    _CACHE[key] = {"value": value, "at": now}
    return value, False

def provider_info(engine, force=False):
    """Info for one provider, for the right-hand panel (limits where the CLI exposes them).
    Shells out to `agent.sh provider-info <engine>`, which sources providers/<engine>/provider.sh
    and calls its provider_<engine>_doctor — all per-engine logic lives in the plugin, not here."""
    def compute():
        raw = run_sync(["provider-info", engine], timeout=25)
        try:
            return json.loads(raw)
        except ValueError:
            return {"engine": engine, "version": "NOT_FOUND", "available": False,
                    "login": "", "limits": None, "note": "provider-info returned invalid JSON"}
    info, cached = _cached("provider:" + engine, compute, force)
    info = dict(info)
    info["now"] = time.time()
    info["cached"] = cached
    return info

def parse_doctor_text(text):
    """Best-effort parser for pre-JSON doctor output and old persistent caches. New agent.sh
    returns structured JSON, but keeping this makes upgrades cache-safe and prevents a malformed
    probe from turning into a raw error box."""
    payload = {"generated_at": time.time(), "engines": [], "raw": text or "",
               "deep_engines": []}
    by_name = {}
    in_engines = False
    current_limit_engine = None
    for line in (text or "").splitlines():
        if line.startswith("=== engines"):
            in_engines = True; current_limit_engine = None; continue
        if line.startswith("==="):
            in_engines = False
            m = re.match(r"===\s+(codex|claude)\s+", line)
            current_limit_engine = m.group(1) if m else None
            continue
        if in_engines:
            m = re.match(r"\s*(\w+)\s+(ok|-|—)\s+(.*)", line)
            if m:
                name, marker, version = m.groups()
                info = by_name.setdefault(name, {"engine": name, "version": "", "available": True,
                                                  "login": "", "limits": None, "note": ""})
                info["available"] = marker == "ok"
                info["version"] = version if marker == "ok" else "NOT_FOUND"
                info["state"] = "ok" if marker == "ok" else "not_installed"
                continue
            m = re.match(r"\s*(\w+)\s+auth\s+(.*)", line)
            if m and m.group(1) in by_name:
                info = by_name[m.group(1)]; info["login"] = m.group(2)
                if re.search(r"required|not logged|logged out", info["login"], re.I):
                    info["state"] = "not_logged_in"
        elif current_limit_engine:
            m = re.match(r"\s*(\w+)\s+\[[#-]+\]\s+([\d.]+)%\s+\(window\s+(\d+)([dhm])\)(?:\s+resets in\s+(\d+)h(\d+)m)?", line)
            if m and current_limit_engine in by_name:
                label, used, amount, unit, hours, minutes = m.groups()
                scale = {"m": 1, "h": 60, "d": 1440}[unit]
                win = {"label": label, "used_percent": float(used),
                       "window_minutes": int(amount) * scale, "resets_at": None}
                if hours is not None:
                    win["resets_at"] = time.time() + int(hours) * 3600 + int(minutes) * 60
                limits = by_name[current_limit_engine].setdefault("limits", None)
                if not limits:
                    limits = {"source": "legacy_text", "plan_type": "?", "windows": []}
                    by_name[current_limit_engine]["limits"] = limits
                limits["windows"].append(win)
    payload["engines"] = list(by_name.values())
    return payload

def _doctor_valid(payload):
    return isinstance(payload, dict) and isinstance(payload.get("engines"), list) and \
           isinstance(payload.get("raw"), str)

def _load_doctor_cache(path=None):
    path = path or DOCTOR_CACHE_FILE
    try:
        with open(path, encoding="utf-8") as f:
            hit = json.load(f)
        if isinstance(hit.get("at"), (int, float)) and _doctor_valid(hit.get("value")):
            _CACHE["doctor"] = hit
            return True
    except (OSError, ValueError, TypeError):
        pass
    return False

def _save_doctor_cache(hit, path=None):
    path = path or DOCTOR_CACHE_FILE
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(hit, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, path)
    except OSError:
        pass                         # memory cache still works on a read-only/unavailable log dir

def _compute_doctor():
    raw = run_sync(["doctor", "--json"], timeout=60)
    try:
        payload = json.loads(raw)
    except ValueError:
        payload = parse_doctor_text(raw)
    if not _doctor_valid(payload) or not payload["engines"] or raw.startswith("error:"):
        raise ValueError("doctor returned no usable data")
    return payload

def _refresh_doctor_worker():
    global _DOCTOR_REFRESHING
    try:
        payload = _compute_doctor()
        hit = {"value": payload, "at": time.time()}
        with _DOCTOR_LOCK:
            _CACHE["doctor"] = hit
        _save_doctor_cache(hit)
    except Exception:
        pass                         # never replace last-known-good data with an error/timeout
    finally:
        with _DOCTOR_LOCK:
            _DOCTOR_REFRESHING = False

def _start_doctor_refresh(force=False):
    global _DOCTOR_REFRESHING
    hit = _CACHE.get("doctor")
    if not force and hit and time.time() - hit["at"] < _CACHE_TTL:
        return False
    with _DOCTOR_LOCK:
        if _DOCTOR_REFRESHING:
            return False
        _DOCTOR_REFRESHING = True
    threading.Thread(target=_refresh_doctor_worker, daemon=True).start()
    return True

def _empty_doctor_payload():
    return {"generated_at": 0, "engines": [
        {"engine": eng, "version": "", "available": None, "login": "", "limits": None,
         "note": "", "state": "checking"} for eng in ENGINES],
        "raw": "", "deep_engines": []}

def doctor_cached_only(refresh=False, force=False):
    """Return last-known-good structured data immediately, including across panel restarts.
    Optionally start a refresh in a daemon thread; this function itself never waits for a CLI."""
    if "doctor" not in _CACHE:
        _load_doctor_cache()
    if refresh:
        _start_doctor_refresh(force)
    hit = _CACHE.get("doctor")
    value = dict(hit["value"]) if hit else _empty_doctor_payload()
    age = max(0, time.time() - hit["at"]) if hit else None
    value.update({"cached": bool(hit), "stale": age is None or age >= _CACHE_TTL,
                  "age_seconds": age, "refreshing": _DOCTOR_REFRESHING})
    return value

def doctor_text(force=False):
    """Synchronous compatibility helper used by tests/older callers; timeout is deliberately
    well above measured runtime and errors never overwrite a good cache."""
    if not force and "doctor" in _CACHE and time.time() - _CACHE["doctor"]["at"] < _CACHE_TTL:
        return _CACHE["doctor"]["value"]["raw"], True
    try:
        payload = _compute_doctor()
        hit = {"value": payload, "at": time.time()}
        _CACHE["doctor"] = hit; _save_doctor_cache(hit)
        return payload["raw"], False
    except Exception:
        hit = _CACHE.get("doctor")
        return (hit["value"]["raw"], True) if hit else ("", True)

def engine_models(engine, force=False):
    """Selectable model ids for a provider. opencode exposes a rich dynamic catalog
    (provider/model across every configured backend) via `opencode models` -- surface it so
    the bridge form can offer real choices instead of a blank free-text box. Other engines
    fall back to the static list in provider.json. Cached (60s) since `opencode models` shells
    out to the CLI."""
    static = list((PROVIDERS.get(engine) or {}).get("models") or [])
    if engine != "opencode":
        return static
    def compute():
        # WHY via git-bash: `opencode` is an npm shim (opencode.cmd) that native-Windows python
        # subprocess can't resolve by bare name; the same git-bash agent.sh uses finds it on PATH.
        try:
            p = subprocess.run([BASH, "-lc", "opencode models"], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=20, cwd=HERE)
            got = [l.strip() for l in (p.stdout or "").splitlines()
                   if l.strip() and "/" in l and " " not in l.strip()]
            return got or static
        except Exception:
            return static
    models, _ = _cached("models:" + engine, compute, force)
    return models

def codex_limits():
    """primary/secondary codex rate-limits only, via the same provider-info plugin path
    (kept for the standalone /api/limits endpoint)."""
    return provider_info("codex").get("limits")

# ---- OpenAI-compatible bridges (agent.sh openai-server) ---------------------------------
# Each running bridge self-registers a bridge-<port>.json in BRIDGES_DIR (see openai_server.py).
# The GUI lists them, probes /health for live status, and can start/stop them.

def _bridge_health(base_url, timeout=1.5):
    """GET <base_url>/health. Returns the parsed dict if the bridge answers, else None
    (unreachable = dead/stale)."""
    try:
        with urllib.request.urlopen(base_url.rstrip("/") + "/health", timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:  # expected for a dead/busy bridge -- DEBUG-gated, type only
        LOG.debug("health probe failed (%s)", type(e).__name__)
        return None

def list_bridges():
    """All known bridges with a live/stale flag. A file whose port no longer answers /health
    and is older than a few seconds is pruned (the process died without cleaning up, e.g. it
    was force-killed) so the panel doesn't accumulate ghosts."""
    out = []
    try:
        files = sorted(glob.glob(os.path.join(BRIDGES_DIR, "bridge-*.json")))
    except OSError:
        files = []
    nowt = time.time()
    for bf in files:
        try:
            with open(bf, encoding="utf-8") as f:
                rec = json.load(f)
        except Exception:
            continue
        # each bridge request (claude/codex/gemini engines) spawns an `openai-<port>-<hex>` task
        # whose full transcript lands in LOGDIR -- surface the count so the user knows requests
        # are logged and can jump to them. opencode proxies to `opencode serve` and logs nothing here.
        port = rec.get("port", 0)
        try:
            rec["requests"] = len(glob.glob(os.path.join(LOGDIR, "openai-%d-*.log" % port)))
        except Exception:
            rec["requests"] = 0
        health = _bridge_health(rec.get("base_url") or "")
        if health is not None:
            rec["live"] = True
            rec["health"] = health
        elif not port_available(port):
            # /health didn't answer but the port is still bound -> the bridge is ALIVE but busy
            # handling a request (a completion holds its lock; /health is momentarily slow). Do
            # NOT prune it -- that was deleting live bridges mid-request. Show it as busy instead.
            rec["live"] = True
            rec["busy"] = True
        elif (nowt - rec.get("started", 0)) > 5:
            # port is genuinely free (nothing listening) and it's not a just-launched bridge
            # still binding -> the process is gone; remove the stale registry file.
            try:
                os.remove(bf)
            except OSError:
                pass
            LOG.debug("pruned stale bridge registry (port %d no longer answers /health)", port)
            continue
        else:
            rec["live"] = False
        out.append(rec)
    out.sort(key=lambda r: r.get("started", 0), reverse=True)
    return out

def port_available(port, host="0.0.0.0"):
    """True if the port is genuinely free right now. Binds 0.0.0.0 with SO_EXCLUSIVEADDRUSE on
    Windows so the test fails if ANYTHING already holds the port -- a bridge bound to 127.0.0.1
    (localhost mode) OR to 0.0.0.0 (LAN mode). WHY not 127.0.0.1: Windows lets you bind
    127.0.0.1:P even while 0.0.0.0:P is in use, so a loopback bind-test missed LAN bridges and
    reported a busy port as free. Also catches Windows reserved ranges (WinError 10013)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        s.bind((host, int(port)))
        return True
    except OSError:
        return False
    finally:
        s.close()

def lan_ips():
    """This host's non-loopback IPv4 addresses, for printing a URL the other device can actually
    open (mirrors openai_server.py's _lan_ips). Connect-less UDP socket: no packets are sent."""
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            if ip and not ip.startswith("127."):
                ips.append(ip)
        finally:
            s.close()
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip and ip not in ips and not ip.startswith("127."):
                ips.append(ip)
    except Exception:
        pass
    return ips


def kill_pid(pid):
    """Terminate a bridge process by pid. taskkill /T on Windows (the bash launcher exec's into
    python, so the recorded pid IS python, but /T also reaps any stragglers); SIGTERM elsewhere."""
    if isinstance(pid, bool) or isinstance(pid, float):
        # JSON true would coerce to pid 1 and 1.9/1.0 would silently truncate to 1 -- refuse
        # the type confusion before any numeric conversion (also covers nan/inf OverflowError).
        LOG.warning("kill_pid: refusing non-integer pid %r", pid)
        return False
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        # 0 and negative pids are signal targets for whole process groups (or every
        # killable process) on POSIX -- never hand one to os.kill or taskkill.
        LOG.warning("kill_pid: refusing non-positive pid %s", pid)
        return False
    try:
        if os.name == "nt":
            proc = subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                                  capture_output=True, timeout=10)
            if proc.returncode != 0:
                LOG.warning("kill_pid: could not terminate pid %s (taskkill status=%s)",
                            pid, proc.returncode)
                return False
        else:
            os.kill(pid, 15)
        return True
    except Exception as e:  # exception type/status only -- never the surrounding command line
        status = getattr(e, "returncode", getattr(e, "winerror", None))
        LOG.warning("kill_pid: could not terminate pid %s (%s%s)", pid, type(e).__name__,
                    "" if status is None else " status=%s" % status)
        return False

def coerce_port(value, default=None):
    """An integer TCP port in 1..65535, or `default`; every user-supplied bridge port goes
    through this so garbage can't become bridge--1.json or a 70000 bind."""
    try:
        p = int(value)
    except (TypeError, ValueError):
        return default
    return p if 1 <= p <= 65535 else default


def start_bridge(data):
    """Spawn `agent.sh openai-server` in the background from the GUI's form. Returns a dict
    with ok/error and the resolved base_url."""
    engine = data.get("engine") or "codex"
    asked_raw = data.get("port")
    port = 8801 if asked_raw in (None, "") else coerce_port(asked_raw)
    if port is None:
        return {"error": "port must be an integer in 1..65535"}
    localhost_only = data.get("localhost", True)
    host = "127.0.0.1" if localhost_only else "0.0.0.0"
    # A busy/reserved port used to fail silently ("clicked start, nothing appeared"). Instead,
    # walk up to the next free port so the click always launches something; tell the client which
    # port was actually used (and whether it differs from what was asked).
    asked = port
    if not port_available(port):
        found = None
        for cand in range(port + 1, port + 50):
            if port_available(cand):
                found = cand
                break
        if found is None:
            return {"error": "no free port near %d" % asked}
        port = found
    args = ["openai-server", "-e", engine, "-p", str(port)]
    if data.get("model"):  args += ["-m", str(data["model"]).strip()]
    if data.get("effort"): args += ["-f", str(data["effort"]).strip()]
    rdir = to_git_bash_path(data.get("dir") or "")
    if rdir:               args += ["-C", rdir]
    args += ["--localhost"] if localhost_only else ["--lan"]
    # An empty key keeps the historic open bridge (fine on loopback); a LAN bridge without one is
    # reported back so the panel can warn instead of quietly exposing the machine.
    api_key = str(data.get("api_key") or "").strip()
    if api_key:
        args += ["--api-key", api_key]
    spawn(args, terminal=bool(data.get("terminal")))
    shown = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    return {"ok": True, "base_url": "http://%s:%d" % (shown, port),
            "port": port, "asked_port": asked, "reassigned": port != asked,
            "auth": bool(api_key), "unprotected_lan": (not localhost_only) and not api_key}

def stop_bridge(port, expected_instance_id=None):
    """Stop the bridge on <port> and drop its registry file -- FAIL-CLOSED: kill_pid runs only
    when the registry record and the live GET /health carry the same non-empty instance_id.
    A supplied expected_instance_id must exactly equal the registry id (a missing registry id
    counts as a mismatch). Unreachable /health never kills: on a still-held port the bridge is
    alive but unverifiable (busy/wedged) -> keep the registry and report ok:false so the user can
    retry; only a genuinely free port proves the process is gone and lets the stale file go.
    Even with matching identity, a missing/unusable pid or a failed kill keeps the registry and
    reports ok:false -- never claim a bridge was stopped when its termination is unconfirmed."""
    coerced = coerce_port(port)
    if coerced is None:
        return {"ok": False, "killed": False, "error": "port must be an integer in 1..65535",
                "port": port}
    port = coerced
    bf = os.path.join(BRIDGES_DIR, "bridge-%d.json" % port)
    rec = {}
    try:
        with open(bf, encoding="utf-8") as f:
            rec = json.load(f)
    except Exception:
        pass
    rec_id = rec.get("instance_id")
    if expected_instance_id is not None and expected_instance_id != rec_id:
        return {"ok": False, "killed": False, "port": port,
                "error": "instance mismatch -- the bridge list is stale, refresh it"}
    health = _bridge_health(rec.get("base_url") or "")
    if health is None:
        if not port_available(port):
            # alive but /health unverifiable (a completion holds its lock, or it is wedged):
            # identity cannot be proven, so never kill AND never drop the registry -- the
            # record is the only thing left to identify this bridge after a retry.
            return {"ok": False, "killed": False, "stale_registry": False, "port": port,
                    "error": "bridge on port %d did not answer /health while the port is "
                             "still in use -- identity unavailable, retry once it is idle" % port}
        # nothing listens -> the recorded process is gone; remove the stale registry safely.
        try:
            os.remove(bf)
        except OSError:
            pass
        LOG.debug("pruned stale bridge registry for port %d (unreachable, port free)", port)
        return {"ok": True, "killed": False, "stale_registry": True, "port": port}
    live_id = health.get("instance_id")
    if rec_id and live_id and live_id != rec_id:
        # someone else owns this port/pid now -- only the stale file may go
        try:
            os.remove(bf)
        except OSError:
            pass
        LOG.debug("dropped stale registry for port %d (another instance owns the port now)", port)
        return {"ok": True, "killed": False, "stale_registry": True, "port": port}
    if not rec_id or not live_id:
        # reachable but identity missing/unprovable on either side -> keep the registry,
        # refuse to kill (fail closed), let the panel surface the error.
        return {"ok": False, "killed": False, "stale_registry": False, "port": port,
                "error": "could not verify the bridge identity on port %d -- "
                         "refresh the list and retry" % port}
    pid = rec.get("pid")
    if not pid or not kill_pid(pid):
        # identity matched, but the kill could not be confirmed (missing/unusable pid, or
        # kill_pid failed) -- the bridge may still be running: keep the registry (it is the
        # only handle left for a retry) and fail closed so a restart never spawns on top.
        reason = ("no usable process id in its registry entry" if not pid
                  else "its recorded process did not terminate")
        LOG.warning("stop_bridge: refusing to drop registry for port %d -- %s", port, reason)
        return {"ok": False, "killed": False, "stale_registry": False, "port": port,
                "error": "could not stop the bridge on port %d -- %s, "
                         "refresh the list and retry" % (port, reason)}
    try:
        os.remove(bf)
    except OSError:
        pass
    return {"ok": True, "killed": True, "port": port}

def restart_bridge(data):
    """Stop the bridge on <port> and relaunch it on the SAME port with new engine/model/effort/
    localhost/dir -- lets the GUI switch a running bridge's model (and local/LAN binding) in place
    without retyping the whole form. Aborts with the stop error when stopping failed (held or
    unverifiable bridge) and never silently launches on another port."""
    port = coerce_port(data.get("port"))
    if port is None:
        return {"error": "port must be an integer in 1..65535"}
    stopped = stop_bridge(port, data.get("instance_id"))
    if not stopped.get("ok"):
        return {"ok": False, "error": stopped.get("error")
                or "could not stop the bridge on port %d" % port,
                "port": port, "stopped": stopped}
    for _ in range(20):  # give the kernel up to ~3s to free the port after the kill
        if port_available(port):
            break
        time.sleep(0.15)
    else:
        # stopping claimed success but the port never freed (e.g. a foreign live bridge owns
        # it): launching now would silently reassign ports -- refuse instead.
        return {"ok": False, "error": "port %d is still in use -- the old bridge did not "
                                      "release it, no new bridge was started" % port,
                "port": port}
    data = dict(data)
    data["port"] = port
    return start_bridge(data)

# Set by main(): the shared secret required from non-loopback clients ("" = loopback-only panel,
# no token needed). See the module docstring for why this panel is stricter than the API bridge.
GUI_TOKEN = ""
TOKEN_COOKIE = "agent_gui_token"


def is_loopback(addr):
    """True for 127.0.0.0/8 and ::1 (incl. the IPv4-mapped ::ffff:127.0.0.1 form)."""
    a = (addr or "").strip()
    if a.startswith("::ffff:"):
        a = a[7:]
    return a == "::1" or a.startswith("127.")


def presented_token(headers, path):
    """The token this request carries, from (in order) ?token=, X-Agent-Token, or the cookie."""
    q = urllib.parse.parse_qs(urllib.parse.urlparse(path or "").query)
    tok = (q.get("token") or [""])[0].strip()
    if tok:
        return tok
    tok = (headers.get("X-Agent-Token") or "").strip()
    if tok:
        return tok
    cookie = headers.get("Cookie") or ""
    for part in cookie.split(";"):
        k, _, v = part.strip().partition("=")
        if k == TOKEN_COOKIE:
            return v.strip()
    return ""


def gui_authorized(client_addr, headers, path):
    """Whether this request may touch the panel. No token configured -> open (the loopback-only
    default, unchanged). Loopback clients -> always allowed (see the module docstring). Everyone
    else must present the token; compared in constant time so the panel is not a guessing oracle."""
    if not GUI_TOKEN:
        return True
    if is_loopback(client_addr):
        return True
    import hmac
    return hmac.compare_digest(presented_token(headers, path), GUI_TOKEN)


class H(BaseHTTPRequestHandler):
    def log_message(self, fmt, *a):  # request lines at DEBUG only (AGENT_LOG_LEVEL); token/key
        # query params are redacted so a `?token=...` URL never lands in the log
        LOG.debug("%s %s", self.address_string(),
                  re.sub(r"((?:api[_-]?key|token)=)[^&\s\"']*", r"\1<redacted>", fmt % a))

    def _reject_unauthorized(self):
        """401 for a network client with no/!wrong token. Returns True when the caller must stop."""
        if gui_authorized(self.client_address[0] if self.client_address else "", self.headers,
                          self.path):
            return False
        self._send(401, json.dumps({"error": "missing or invalid token -- open this panel as "
                                             "http://<host>:<port>/?token=<secret>"}))
        return True

    def _maybe_set_token_cookie(self):
        """After a successful ?token=... page load, remember it in a cookie so the page's own
        fetch() calls (which carry no query string) keep working without touching any JS."""
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        tok = (q.get("token") or [""])[0].strip()
        if GUI_TOKEN and tok and tok == GUI_TOKEN:
            self.send_header("Set-Cookie",
                             "%s=%s; Path=/; SameSite=Strict; Max-Age=604800" % (TOKEN_COOKIE, tok))

    def _send(self, code, body, ctype="application/json"):
        b = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self._maybe_set_token_cookie()
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self._reject_unauthorized():
            return
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path in ("/", "/index.html"):
            try:
                with open(HTML, "rb") as f:
                    self._send(200, f.read(), "text/html")
            except OSError:
                self._send(500, "gui.html not found")
        elif u.path == "/api/tasks":
            self._send(200, json.dumps({"tasks": list_tasks(), "engines": ENGINES,
                                        "providers": PROVIDERS, "projects": load_projects(),
                                        "cwd": to_git_bash_path(os.getcwd())}))
        elif u.path == "/api/providers":
            self._send(200, json.dumps({"providers": PROVIDERS}))
        elif u.path == "/api/browse":
            self._send(200, json.dumps(browse((q.get("path") or [""])[0])))
        elif u.path == "/api/thread":
            name = (q.get("task") or [""])[0]
            self._send(200, json.dumps({"name": name, "log": read_log(name)}))
        elif u.path == "/api/dialog":
            name = (q.get("task") or [""])[0]
            if not name:
                return self._send(400, json.dumps({"error": "task required"}))
            full = (q.get("full") or ["0"])[0] == "1"
            try:
                self._send(200, json.dumps(dialog_payload(name, full=full)))
            except Exception as e:  # never blank the pane: fall back to the raw log as one step
                self._send(200, json.dumps({"name": name, "state": "?", "engine": "?",
                                            "model": "?", "total_steps": 1, "offset": 0,
                                            "has_more": False, "now": time.time(),
                                            "mtime": 0, "log_size": 0,
                                            "steps": [{"kind": "log", "ts": "", "epoch": None,
                                                       "info": "", "i": 0, "prompt": "",
                                                       "prompt_label": "", "duration_s": None,
                                                       "duration": "",
                                                       "blocks": [{"type": "text",
                                                                   "text": read_log(name)}]}],
                                            "parse_error": str(e)}))
        elif u.path == "/api/doctor":
            if (q.get("deep") or ["0"])[0] == "1":
                # Explicit user action only: this costs a real model call and may take minutes.
                self._send(200, json.dumps({"raw": run_sync(["doctor", "--deep"], timeout=240)}))
            else:
                force = (q.get("force") or ["0"])[0] == "1"
                cached_only = (q.get("cached") or ["0"])[0] == "1"
                # Always return immediately. The normal/force route merely starts a background
                # refresh; browser polling paints the new snapshot when it is ready.
                self._send(200, json.dumps(doctor_cached_only(not cached_only, force)))
        elif u.path == "/api/limits":
            self._send(200, json.dumps({"limits": codex_limits(), "now": time.time()}))
        elif u.path == "/api/provider":
            eng = (q.get("engine") or ["codex"])[0]
            force = (q.get("force") or ["0"])[0] == "1"
            self._send(200, json.dumps(provider_info(eng, force)))
        elif u.path == "/api/bridges":
            self._send(200, json.dumps({"bridges": list_bridges(), "now": time.time()}))
        elif u.path == "/api/models":
            eng = (q.get("engine") or ["codex"])[0]
            force = (q.get("force") or ["0"])[0] == "1"
            self._send(200, json.dumps({"engine": eng, "models": engine_models(eng, force)}))
        elif u.path == "/api/locales":
            self._send(200, json.dumps({"locales": list_locales()}))
        elif u.path == "/api/wait":
            name = (q.get("task") or [""])[0]
            if not name:
                return self._send(400, json.dumps({"error": "task required"}))
            timeout = clamp_wait_timeout((q.get("timeout") or [""])[0])
            deadline = time.time() + timeout
            st, meta = task_state(name)
            while st in LIVE_STATES and time.time() < deadline:
                time.sleep(0.5)
                st, meta = task_state(name)
            self._send(200, json.dumps({"name": name, "state": st, "model": meta.get("model", "?"),
                                        "log": read_log(name)}))
        elif u.path == "/api/stream":
            name = (q.get("task") or [""])[0]
            if not name:
                return self._send(400, json.dumps({"error": "task required"}))
            self._stream_log(name)
        elif u.path.startswith("/locales/") and u.path.endswith(".json"):
            self._serve_static(LOCALES_DIR, u.path[len("/locales/"):], "application/json")
        elif u.path.startswith("/static/"):
            self._serve_static(STATIC_DIR, u.path[len("/static/"):])
        else:
            self._send(404, "not found")

    def _serve_static(self, root, rel, ctype=None):
        """Serve a file from `root`, rejecting any path that resolves outside it
        (directory traversal via ../ or an absolute path)."""
        rel = urllib.parse.unquote(rel)
        full = os.path.normpath(os.path.join(root, rel))
        if os.path.commonpath([os.path.abspath(root), full]) != os.path.abspath(root):
            return self._send(403, "forbidden")
        try:
            with open(full, "rb") as f:
                data = f.read()
        except OSError:
            return self._send(404, "not found")
        if not ctype:
            ext = os.path.splitext(full)[1]
            ctype = {".js": "text/javascript", ".css": "text/css",
                     ".json": "application/json", ".png": "image/png"}.get(ext, "application/octet-stream")
        self._send(200, data, ctype)

    def _stream_log(self, name):
        """Server-Sent Events: tail a task's .log in real time instead of making the client
        poll /api/thread. Sends each new line as its own `data:` event as soon as it's written,
        and a final `event: done` once the task leaves the "running" state (or after ~60s of
        no new output, so a stream never hangs open forever on a stuck/forgotten task)."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        sent = 0
        idle = 0
        try:
            while idle < 120:  # 120 * 0.5s = 60s of silence -> give up and close
                text = read_log(name)
                if len(text) > sent:
                    for line in text[sent:].splitlines():
                        self.wfile.write(("data: " + json.dumps(line) + "\n\n").encode("utf-8"))
                    sent = len(text)
                    idle = 0
                    self.wfile.flush()
                else:
                    idle += 1
                st, _ = task_state(name)
                if st not in LIVE_STATES:   # idle = alive but quiet -> keep the stream open
                    self.wfile.write(b"event: done\ndata: {}\n\n")
                    self.wfile.flush()
                    break
                time.sleep(0.5)
        except (BrokenPipeError, ConnectionAbortedError, OSError):
            pass

    def _read_json_object_body(self):
        """Request body as a JSON object; answers 400/413 itself, None = caller must stop."""
        raw_len = (self.headers.get("Content-Length") or "").strip()
        if raw_len and not (raw_len.isascii() and raw_len.isdigit()):
            self._send(400, json.dumps({"error": "malformed Content-Length header"}))
            return None
        n = int(raw_len) if raw_len else 0
        if n > MAX_BODY_BYTES:
            self._send(413, json.dumps({"error": "request body too large"}))
            return None
        try:
            data = json.loads(self.rfile.read(n) if n else b"{}")
        except ValueError:
            self._send(400, json.dumps({"error": "invalid JSON body"}))
            return None
        if not isinstance(data, dict):
            self._send(400, json.dumps({"error": "request body must be a JSON object"}))
            return None
        return data

    def do_POST(self):
        if self._reject_unauthorized():
            return
        u = urllib.parse.urlparse(self.path)
        data = self._read_json_object_body()
        if data is None:
            return
        if u.path == "/api/run":
            prompt = (data.get("prompt") or "").strip()
            if not prompt:
                return self._send(400, json.dumps({"error": "empty prompt"}))
            rdir = to_git_bash_path(data.get("dir") or "")
            args = ["run", "-e", data.get("engine") or "codex"]
            if data.get("model"):    args += ["-m", data["model"]]
            if data.get("effort"):   args += ["-f", data["effort"]]
            if rdir:                 args += ["-C", rdir]
            if data.get("name"):     args += ["-t", data["name"]]
            if data.get("parent"):   args += ["-P", data["parent"]]
            if data.get("progress"): args += ["-p"]
            args.append(prompt)
            spawn(args, terminal=bool(data.get("terminal")))
            if rdir:  # remember the project
                pr = load_projects()
                if rdir not in pr:
                    pr.append(rdir); save_projects(pr)
            self._send(200, json.dumps({"ok": True}))
        elif u.path == "/api/reply":
            task = (data.get("task") or "").strip()
            answer = (data.get("answer") or "").strip()
            if not task or not answer:
                return self._send(400, json.dumps({"error": "task and answer required"}))
            args = ["reply", task, answer]
            if data.get("progress"): args = ["reply", "-p", task, answer]
            spawn(args, terminal=bool(data.get("terminal")))
            self._send(200, json.dumps({"ok": True}))
        elif u.path == "/api/project":
            d = to_git_bash_path((data.get("dir") or "").strip())
            if not d:
                return self._send(400, json.dumps({"error": "dir required"}))
            pr = load_projects()
            if d not in pr:
                pr.append(d); save_projects(pr)
            self._send(200, json.dumps({"ok": True, "projects": pr}))
        elif u.path == "/api/test-api":
            base_url = (data.get("base_url") or "").strip()
            goal = (data.get("goal") or "").strip()
            if not base_url or not goal:
                return self._send(400, json.dumps({"error": "base_url and goal are required"}))
            rdir = to_git_bash_path(data.get("dir") or "") or HERE
            name = data.get("name") or ("api-test-%d" % int(time.time()))
            args = ["test-api", "--base-url", base_url, "--goal", goal, "-e", data.get("engine") or "codex",
                    "-C", rdir, "-t", name]
            if data.get("model"):  args += ["-m", data["model"]]
            if data.get("effort"): args += ["-f", data["effort"]]
            spawn(args, terminal=bool(data.get("terminal")))
            self._send(200, json.dumps({"ok": True, "name": name}))
        elif u.path == "/api/bridge/start":
            self._send(200, json.dumps(start_bridge(data)))
        elif u.path == "/api/bridge/stop":
            port = data.get("port")
            if port in (None, ""):
                return self._send(400, json.dumps({"error": "port required"}))
            self._send(200, json.dumps(stop_bridge(port, data.get("instance_id"))))
        elif u.path == "/api/bridge/restart":
            self._send(200, json.dumps(restart_bridge(data)))
        else:
            self._send(404, "not found")

class Srv(ThreadingHTTPServer):
    allow_reuse_address = False  # so we detect "already running" instead of starting a second server on top

def prewarm_cache():
    """Start only one doctor refresh on startup. Doctor already probes every provider, while
    eagerly launching five separate provider-info calls duplicates Windows process creation and
    can exhaust a busy machine; the right-panel provider cache remains lazy."""
    def run():
        # Disk cache is loaded synchronously and the expensive probe starts in its own thread.
        doctor_cached_only(refresh=True)
    threading.Thread(target=run, daemon=True).start()

def is_our_panel(port):
    """True only if whatever holds <port> answers like THIS panel. The default port is a popular
    one -- it was found occupied by an unrelated WebSocket server, and because the old code treated
    ANY bind failure as "already running", `agent.sh gui` printed a success line and opened a tab
    that could only fail with "invalid Connection header". Identity is checked by asking /api/tasks
    for its shape (works against older panel versions too, unlike a version header)."""
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/api/tasks" % port, timeout=2) as r:
            o = json.loads(r.read().decode("utf-8", "replace") or "{}")
        return isinstance(o, dict) and "tasks" in o and "engines" in o
    except Exception:
        return False

def choose_port(asked):
    """Decide which port to actually serve on. Returns (port, status):
         "asked" — the requested port is free, use it;
         "ours"  — OUR panel already answers there (caller just opens the browser, no second
                   server: the GUI is shared across providers, so a running one is reused);
         "moved" — the port is held by SOMETHING ELSE; `port` is the next free one;
         "none"  — busy and nothing free nearby.
    The "moved" case is the one that used to be invisible: any bind failure was read as "already
    running", so `agent.sh gui` claimed success while the browser tab hit a foreign server."""
    if port_available(asked):
        return asked, "asked"
    if is_our_panel(asked):
        return asked, "ours"
    for cand in range(asked + 1, asked + 50):
        if port_available(cand):
            return cand, "moved"
    return asked, "none"

def parse_argv(argv, env):
    """(port, host, token) from the CLI arguments + environment. Kept as a pure function so the
    LAN/token rules are unit-testable without binding a socket.
      - a bare positional number is the port (explicit arg > $AGENT_GUI_PORT > 8765);
      - --lan / $AGENT_GUI_HOST=0.0.0.0 binds all interfaces, --localhost forces loopback back;
      - --token SECRET / $AGENT_GUI_TOKEN is the shared secret for non-loopback clients.
    Raises SystemExit with an explanation when LAN is requested without a token -- an
    unauthenticated LAN panel is remote code execution, so it must not start (see the docstring)."""
    port = None
    host = (env.get("AGENT_GUI_HOST") or "").strip() or "127.0.0.1"
    token = (env.get("AGENT_GUI_TOKEN") or "").strip()
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--lan":
            host = "0.0.0.0"
        elif a == "--localhost":
            host = "127.0.0.1"
        elif a == "--token":
            token = (argv[i + 1] if i + 1 < len(argv) else "").strip()
            i += 1
        elif a.startswith("--token="):
            token = a.split("=", 1)[1].strip()
        elif a.isdigit():
            port = int(a)
        else:
            raise SystemExit("[agent-gui] unknown argument %r (usage: gui [port] [--lan] "
                             "[--token SECRET] [--localhost])" % a)
        i += 1
    if port is None:
        port = int(env.get("AGENT_GUI_PORT") or 8765)
    if host not in ("127.0.0.1", "localhost", "::1") and not token:
        raise SystemExit(
            "[agent-gui] REFUSING to bind %s without a token.\n"
            "            This panel starts real subagents in full-auto mode in any directory a\n"
            "            caller names -- exposed to the network unauthenticated, that is remote\n"
            "            code execution on this machine.\n"
            "            Start it as:  agent.sh gui %d --lan --token <secret>\n"
            "            (or set AGENT_GUI_TOKEN=<secret>), then open\n"
            "            http://<this-host>:%d/?token=<secret> on the other device." % (host, port, port))
    return port, host, token


def main():
    # explicit CLI arg > $AGENT_GUI_PORT env var > 8765 default -- keeping the port stable
    # across restarts (rather than only ever accepting a positional arg) is what lets a
    # browser tab stay pinned to the same URL run after run. That priority is unchanged; only
    # what happens when the chosen port is BUSY changed (see choose_port).
    global GUI_TOKEN
    asked, host, GUI_TOKEN = parse_argv(sys.argv[1:], os.environ)
    port, how = choose_port(asked)
    if how == "ours":
        url = "http://127.0.0.1:%d" % port
        print("[agent-gui] already running at %s - opening browser" % url)
        try:
            import webbrowser; webbrowser.open(url)
        except Exception:
            pass
        return
    if how == "none":
        print("[agent-gui] ERROR: port %d is busy (held by another program, not this panel) "
              "and no free port in %d..%d - free it or set AGENT_GUI_PORT"
              % (asked, asked + 1, asked + 49))
        return
    if how == "moved":
        print("!" * 72)
        print("[agent-gui] port %d is BUSY - held by another program, not this panel." % asked)
        print("[agent-gui] STARTED ON PORT %d INSTEAD -> http://127.0.0.1:%d" % (port, port))
        print("[agent-gui] (pin that URL, or free port %d / set AGENT_GUI_PORT to pick another)" % asked)
        print("!" * 72)
    url = "http://127.0.0.1:%d" % port
    try:
        srv = Srv((host, port), H)
    except OSError as e:
        # lost a race for the port between the check and the bind (or it is reserved by Windows)
        print("[agent-gui] could not bind %s:%d: %s" % (host, port, e))
        return
    print("[agent-gui] %s  (logs: %s)  Ctrl-C to stop" % (url, LOGDIR))
    if host not in ("127.0.0.1", "localhost", "::1"):
        for ip in lan_ips():
            print("[agent-gui] LAN: http://%s:%d/?token=<your token>  (other devices use THIS url)"
                  % (ip, port))
        print("[agent-gui] the token is required for every non-loopback request; open the ?token= "
              "url once and the panel stores it in a cookie.")
    prewarm_cache()
    try:
        import webbrowser; webbrowser.open(url)
    except Exception:
        pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[agent-gui] stopped")

if __name__ == "__main__":
    main()
