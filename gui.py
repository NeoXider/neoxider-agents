#!/usr/bin/env python3
"""Минималистичный локальный веб-GUI над agent.sh — единый пульт для всех CLI-провайдеров.

Запуск:  agent.sh gui [port]   (или: python gui.py [port])
Открывает http://127.0.0.1:8765 . Только localhost. Ноль зависимостей (stdlib).

Бэкенд читает <name>.meta / <name>.log напрямую (быстро, без парсинга текста list),
а действия (run/reply/doctor) шеллит в agent.sh, чтобы вся логика жила в одном месте.
"""
import json, os, sys, time, subprocess, urllib.parse, glob
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
# bash (git-bash) понимает прямые слэши в win-путях, но НЕ бэкслеши в argv -> нормализуем
SK = os.path.join(HERE, "agent.sh").replace("\\", "/")
HTML = os.path.join(HERE, "gui.html")
# точный git-bash из agent.sh (иначе native-python может дёрнуть WSL bash и не найти C:/... путь).
# Фолбэк на типовые пути git-bash, чтобы и прямой `python gui.py` работал (не только через agent.sh gui).
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
STALE_SEC = 300  # running + нет активности в логе дольше -> считаем stalled

_DEFAULT_PROVIDERS = {
    "codex":   {"label": "Codex", "models": ["5.5", "5.5-high", "spark"], "limits": "codex"},
    "claude":  {"label": "Claude", "models": ["sonnet", "opus", "haiku"], "limits": None},
    "opencode":{"label": "opencode", "models": [], "limits": None},
    "gemini":  {"label": "Gemini", "models": [], "limits": None},
}
def load_providers():
    try:
        with open(os.path.join(HERE, "providers.json"), encoding="utf-8") as f:
            return json.load(f).get("providers", _DEFAULT_PROVIDERS)
    except Exception:
        return _DEFAULT_PROVIDERS
PROVIDERS = load_providers()
ENGINES = list(PROVIDERS.keys())

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
    """Liveness через mtime лога (pid из git-bash не сопоставим с win-pid в python)."""
    st = meta.get("state", "?")
    if st == "running" and log_mtime and (nowt - log_mtime) > STALE_SEC:
        return "stalled"
    return st

def first_prompt(name):
    """Первая строка первого PROMPT — как заголовок «чата» в дереве."""
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
ACT_RULES = [  # для running — по последней строке лога: что делает прямо сейчас
    (("read", "open", "cat ", "grep", "ls "), "📖"),
    (("edit", "appl", "writ", "patch", "creat", "wrote"), "✏️"),
    (("test", "pytest", "npm test", "dotnet test"), "🧪"),
    (("run", "exec", "$ ", "bash", "compil", "build"), "🔧"),
    (("token", "usage"), "🧮"),
]
TOPIC_RULES = [  # тема задачи — по тексту первого prompt
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
    """Мини файловый браузер (только каталоги) для выбора рабочей директории проекта из GUI."""
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

def codex_limits():
    """Структурные rate-limits codex из последней сессии (для панели справа)."""
    files = sorted(glob.glob(os.path.expanduser("~/.codex/sessions/**/*.jsonl"), recursive=True),
                   key=os.path.getmtime)[-8:]
    def find(d):
        if isinstance(d, dict):
            if "rate_limits" in d:
                return d["rate_limits"]
            for v in d.values():
                r = find(v)
                if r:
                    return r
        return None
    rl = None
    for f in files:
        try:
            for line in open(f, encoding="utf-8", errors="ignore"):
                if '"rate_limits"' in line:
                    try:
                        rl = find(json.loads(line)) or rl
                    except ValueError:
                        pass
        except OSError:
            pass
    return rl

def read_log(name):
    try:
        with open(os.path.join(LOGDIR, name + ".log"), encoding="utf-8", errors="ignore") as f:
            return f.read()
    except OSError:
        return ""

def spawn(args, terminal=False):
    """Фоновый запуск agent.sh. terminal=True -> отдельное окно консоли с живым выводом чата."""
    kw = dict(cwd=HERE)
    if terminal and os.name == "nt":
        kw["creationflags"] = 0x00000010  # CREATE_NEW_CONSOLE — видно чат живьём
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

def _sh(cmd, timeout=10):
    try:
        return subprocess.run([BASH, "-lc", cmd], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout, cwd=HERE).stdout.strip()
    except Exception:
        return ""

def provider_info(engine):
    """Инфо по конкретному провайдеру для правой панели (лимиты там, где CLI их отдаёт)."""
    ver = _sh("command -v %s >/dev/null 2>&1 && %s --version 2>&1 | head -1 || echo NOT_FOUND" % (engine, engine))
    info = {"engine": engine, "version": ver, "available": ver != "NOT_FOUND",
            "login": "", "limits": None, "now": time.time(), "note": ""}
    lim_src = (PROVIDERS.get(engine) or {}).get("limits")
    if lim_src == "codex":
        info["login"] = _sh("codex login status 2>&1 | head -1")
        info["limits"] = codex_limits()  # primary/secondary rate-limits
    elif engine == "claude":
        info["login"] = _sh("claude --version >/dev/null 2>&1 && echo 'CLI ok'")
        info["note"] = "Claude CLI не отдаёт остаток лимитов через API — только версия/доступность."
    else:
        info["note"] = "У этого провайдера нет CLI-эндпоинта лимитов."
    return info

class H(BaseHTTPRequestHandler):
    def log_message(self, *a):  # тихо
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
                                        "cwd": os.getcwd()}))
        elif u.path == "/api/providers":
            self._send(200, json.dumps({"providers": PROVIDERS}))
        elif u.path == "/api/browse":
            self._send(200, json.dumps(browse((q.get("path") or [""])[0])))
        elif u.path == "/api/thread":
            name = (q.get("task") or [""])[0]
            self._send(200, json.dumps({"name": name, "log": read_log(name)}))
        elif u.path == "/api/doctor":
            self._send(200, json.dumps({"text": run_sync(["doctor"], timeout=25)}))
        elif u.path == "/api/limits":
            self._send(200, json.dumps({"limits": codex_limits(), "now": time.time()}))
        elif u.path == "/api/provider":
            eng = (q.get("engine") or ["codex"])[0]
            self._send(200, json.dumps(provider_info(eng)))
        else:
            self._send(404, "not found")

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
            args = ["run", "-e", data.get("engine") or "codex"]
            if data.get("model"):    args += ["-m", data["model"]]
            if data.get("dir"):      args += ["-C", data["dir"]]
            if data.get("name"):     args += ["-t", data["name"]]
            if data.get("parent"):   args += ["-P", data["parent"]]
            if data.get("progress"): args += ["-p"]
            args.append(prompt)
            spawn(args, terminal=bool(data.get("terminal")))
            if data.get("dir"):  # запомнить проект
                pr = load_projects()
                if data["dir"] not in pr:
                    pr.append(data["dir"]); save_projects(pr)
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
            d = (data.get("dir") or "").strip()
            if not d:
                return self._send(400, json.dumps({"error": "dir required"}))
            pr = load_projects()
            if d not in pr:
                pr.append(d); save_projects(pr)
            self._send(200, json.dumps({"ok": True, "projects": pr}))
        else:
            self._send(404, "not found")

class Srv(ThreadingHTTPServer):
    allow_reuse_address = False  # чтобы поймать «уже запущен», а не поднять второй сервер поверх

def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    url = "http://127.0.0.1:%d" % port
    # Идемпотентность: GUI/логика общие для всех провайдеров. Если сервер уже поднят
    # (другой агент/сам пользователь), НЕ падаем и не дублируем — открываем браузер на него.
    try:
        srv = Srv(("127.0.0.1", port), H)
    except OSError:
        print("[agent-gui] уже запущен на %s — открываю браузер" % url)
        try:
            import webbrowser; webbrowser.open(url)
        except Exception:
            pass
        return
    print("[agent-gui] %s  (logs: %s)  Ctrl-C to stop" % (url, LOGDIR))
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
