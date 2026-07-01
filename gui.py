#!/usr/bin/env python3
"""Minimalist local web GUI over agent.sh — one dashboard for all CLI providers.

Run:  agent.sh gui [port]   (or: python gui.py [port])
Opens http://127.0.0.1:8765 by default -- a stable port so a browser tab can stay pinned
across restarts. Override with $AGENT_GUI_PORT or an explicit [port] arg. Localhost only.
Zero dependencies (stdlib).

The backend reads <name>.meta / <name>.log directly (fast, no parsing of `list`'s text),
while actions (run/reply/doctor) shell out to agent.sh so all the logic lives in one place.
Provider metadata (label/models/limits) is glob-loaded from providers/*/provider.json, and
per-provider info (version/login/rate limits) is fetched by shelling out to
`agent.sh provider-info <engine>` — the plugin's own provider.sh owns that logic, gui.py
does not hardcode any per-engine behavior.
"""
import json, os, re, sys, time, subprocess, urllib.parse, glob
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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
STALE_SEC = 300  # running + no log activity for longer than this -> treat as stalled

_DEFAULT_PROVIDERS = {
    "codex":   {"label": "Codex", "models": ["5.5", "5.5-high", "spark"], "limits": "codex"},
    "claude":  {"label": "Claude", "models": ["sonnet", "opus", "haiku"], "limits": None},
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

def eff_state(meta, log_mtime, nowt):
    """Liveness via the log's mtime (a git-bash pid isn't comparable to a win-pid in python)."""
    st = meta.get("state", "?")
    if st == "running" and log_mtime and (nowt - log_mtime) > STALE_SEC:
        return "stalled"
    return st

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
    if state == "running":
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

def doctor_text(force=False):
    text, cached = _cached("doctor", lambda: run_sync(["doctor"], timeout=25), force)
    return text, cached

def codex_limits():
    """primary/secondary codex rate-limits only, via the same provider-info plugin path
    (kept for the standalone /api/limits endpoint)."""
    return provider_info("codex").get("limits")

class H(BaseHTTPRequestHandler):
    def log_message(self, *a):  # silent
        pass

    def _send(self, code, body, ctype="application/json"):
        b = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
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
        elif u.path == "/api/doctor":
            force = (q.get("force") or ["0"])[0] == "1"
            text, cached = doctor_text(force)
            self._send(200, json.dumps({"text": text, "cached": cached}))
        elif u.path == "/api/limits":
            self._send(200, json.dumps({"limits": codex_limits(), "now": time.time()}))
        elif u.path == "/api/provider":
            eng = (q.get("engine") or ["codex"])[0]
            force = (q.get("force") or ["0"])[0] == "1"
            self._send(200, json.dumps(provider_info(eng, force)))
        elif u.path == "/api/locales":
            self._send(200, json.dumps({"locales": list_locales()}))
        elif u.path == "/api/wait":
            name = (q.get("task") or [""])[0]
            if not name:
                return self._send(400, json.dumps({"error": "task required"}))
            timeout = min(float((q.get("timeout") or ["60"])[0] or 60), 300)
            deadline = time.time() + timeout
            st, meta = task_state(name)
            while st == "running" and time.time() < deadline:
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
                if st != "running":
                    self.wfile.write(b"event: done\ndata: {}\n\n")
                    self.wfile.flush()
                    break
                time.sleep(0.5)
        except (BrokenPipeError, ConnectionAbortedError, OSError):
            pass

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        try:
            data = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            data = {}
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
        else:
            self._send(404, "not found")

class Srv(ThreadingHTTPServer):
    allow_reuse_address = False  # so we detect "already running" instead of starting a second server on top

def prewarm_cache():
    """Populate the doctor/provider-info cache for every known engine in the background on
    startup, so switching providers in the GUI doesn't eat a ~9s cold shell-out the first
    time you pick one that isn't cached yet -- everything is warm within a few seconds of
    the server starting, not on first click."""
    import threading
    def run():
        doctor_text()
        for eng in ENGINES:
            provider_info(eng)
    threading.Thread(target=run, daemon=True).start()

def main():
    # explicit CLI arg > $AGENT_GUI_PORT env var > 8765 default -- keeping the port stable
    # across restarts (rather than only ever accepting a positional arg) is what lets a
    # browser tab stay pinned to the same URL run after run.
    port = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("AGENT_GUI_PORT") or 8765)
    url = "http://127.0.0.1:%d" % port
    # Idempotency: the GUI/logic are shared across all providers. If a server is already up
    # (another agent/the user themself), DO NOT fail or duplicate it — just open the browser on it.
    try:
        srv = Srv(("127.0.0.1", port), H)
    except OSError:
        print("[agent-gui] already running at %s — opening browser" % url)
        try:
            import webbrowser; webbrowser.open(url)
        except Exception:
            pass
        return
    print("[agent-gui] %s  (logs: %s)  Ctrl-C to stop" % (url, LOGDIR))
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
