#!/usr/bin/env python3
"""OpenAI-compatible /v1/chat/completions bridge over CLI subagents (claude/codex/kimi/opencode/gemini).

Run:   agent.sh openai-server -e claude -m sonnet -f high -p 8801
       agent.sh openai-server -m codex/6-luna -m codex/6-sol -m claude/sonnet -p 8801
       agent.sh openai-server start --detach -m codex/6-luna -p 0      (background; prints the port)
       agent.sh openai-server list | stop <port|name|all> | check -m codex/6-luna
       (or directly: python openai_server.py -e codex -m spark -p 8802)

Point any OpenAI-compatible client's base_url at this server (e.g. http://127.0.0.1:8801/v1) --
including CoreAI's own COREAI_TEST_BASE_URL for the Game-Creation Benchmark -- and it drives the
configured CLI agents as the LLM backend for every /v1/chat/completions call.

ROUTING -- one process can serve several engine/model routes:
  - `-e X -m Y` (one model) is the historic single-route mode: every request is served by that
    route whatever its `model` field says.
  - Repeatable `-m engine/model[@effort]` (or `--models a,b`) configures several routes; the
    request's `model` field selects one by id (`codex/6-luna`), label (`codex/GPT-6 Luna`) or a
    unique bare alias (`6-luna`). The first route is the default. Multi-route bridges are strict by
    default (an unknown model is a 404 `model_not_found`, never a silent substitution); a single
    route is lenient (the default serves it) unless `--strict-models`. `--allow-any` additionally
    accepts any `engine/model` of a known engine on demand (loopback binds only).
  - Responses report the route's real label in `model` and its id in `neoxider_route`.
  - The request's `reasoning_effort` (or `reasoning.effort`, or a `@effort` model suffix) picks the
    effort per conversation, default `-f`; it is clamped to the levels provider.json advertises,
    ignored by engines without effort, part of the session identity (route variant
    `engine/model@effort`), and reported in the `neoxider` response object.

THE SESSION MODEL -- a pool of independent conversations, each one an ongoing CLI session:
  - Each conversation remembers its own `messages`, its own CLI task/session (task name, log, meta,
    provider session id) and its own scratch working directory
    (Temp/neoxider-openai-bridge/session-<port>-<id>). When a new call's `messages` is exactly a
    known conversation's array PLUS one or more new turns (a deterministic prefix check, never a
    guess), only the NEW turns are sent to THAT conversation's CLI session via `agent.sh reply`
    (resume) -- saving the resend cost and keeping the provider's prompt cache warm.
  - Anything else (edited/rolled-back history, a genuinely different conversation, a first call)
    gets a NEW session with a fresh run of the full history. A new or different conversation never
    overwrites, resumes or evicts another conversation's entry.
  - Independent conversations run concurrently (`--max-concurrency`, per-engine `--engine-limit`,
    a bounded queue with `--queue-timeout`); one conversation stays strictly sequential (a
    per-session lock), so two turns of it never race.
  - Sessions are evicted only by idle TTL (`--session-ttl`, default 1800s) or by the explicit
    capacity bound (`--max-sessions`, LRU of IDLE sessions, never an active one); every eviction
    is logged. `GET /health` lists the sessions (id, route, turns, idle seconds -- never content).
  - `POST .../reset` drops every remembered session (or only one route's: body {"model": ...}).
  - Engines without CLI-level resume support (`gemini`) always take the fresh-run path; their
    conversation still keeps its own working directory between turns.

ERRORS -- provider/CLI failures become OpenAI-style errors with a meaningful status:
  {"error": {"message", "type", "code", "param": null, "retryable"}} -- 401/403 auth or tier
  refusal (the provider's own text, e.g. an OpenCode FreeTierError), 402 billing, 429 rate/usage
  limit (+Retry-After when known), 404 unknown model, 400 provider-rejected request, 502/503
  provider crashed/unavailable, 504 timeout or stall. Non-retryable failures are never retried
  inside the bridge. Each failure logs one `incident` line with the mapped status.

WHAT THIS IS -- still a wire-compatible shim, NOT a low-latency native LLM backend:
  - First-token latency is a full CLI subprocess start (seconds), even in streaming mode.
  - `stream: true` on a LIVE-capable engine (claude) forwards REAL token deltas: the provider
    runs the CLI with `--output-format stream-json --include-partial-messages` piped through
    stream_text_filter.py so the task log grows as the model generates, the bridge tails that
    log and emits each new piece as an SSE chunk, and canonical fenced tool calls are converted
    into `delta.tool_calls` chunks AS EACH CALL'S JSON CLOSES (see LiveToolCallEmitter). The
    first ~350 chars are held back so a provider limit banner can still become an HTTP 429
    instead of leaking out as streamed content; a full end-of-turn parse reconciles any calls
    the incremental scanner could not recognize. `--no-live-stream` disables all of this and
    falls back to the legacy behavior: replaying the finished answer as word-sized SSE deltas
    (which is also what every non-live engine, e.g. codex, still does).
  - `tools` / function-calling is EMULATED: the agent is instructed via the prompt to describe a
    tool call as a strict JSON block instead of using its own shell/file tools, which is then
    reformatted into a real OpenAI `tool_calls` response. This is best-effort prompting, not a
    protocol the CLI natively speaks -- it can occasionally ignore the instruction or misformat it.
    The instructions are re-sent on every call that includes `tools` (even a continuation), not
    just the first -- simpler and more robust than tracking whether the schema already "stuck".
  - `usage` token counts are ESTIMATES (~4 chars/token, flagged with "neoxider_estimated": true)
    -- the wrapped CLIs don't expose real counts in a structured form. Good enough for cost
    panels/dashboards; not billing-grade.
  - A completion whose CLI invocation came back empty or with a transient failure (network,
    crash, unknown error) is retried (`--retries`, default 1) before the bridge gives up -- a real
    OpenAI endpoint effectively never returns an empty 200. Auth/billing/limit/model/bad-request
    refusals and timeouts/stalls are NOT retried internally (the client gets the mapped error and a
    `retryable` hint). An unexpected bridge exception surfaces as an OpenAI-style {"error": ...}
    HTTP 500 with an incident id, never a bare connection reset.
  - A stream watchdog (`--stall-timeout`, default 180s) aborts a CLI turn whose provider produced
    no output for that long (the known "stream hangs with the process alive" quirk) -> 504; a
    client that disconnects cancels its CLI run.
  - `content` is a clean answer for every bundled engine. codex's non-interactive `exec` mode
    otherwise mixes its own startup banner/session-id/error-log/"tokens used" chrome (and, on
    Windows, a cp866-mojibake OS-notification line) into the same stream as the answer, so the
    codex provider runs it via `codex exec --json` and extracts just the final agent message --
    see providers/codex/provider.py (`_provider_codex_emit`). `claude`/`opencode`/`gemini` were
    already clean. (If a provider ever regresses, the bridge reads the CLI's captured output
    verbatim with no extra cleanup, so raw chrome would show through.)
Zero dependencies (stdlib only); mirrors gui.py's process/log conventions but is fully standalone
(does not import gui.py) so the two servers can run/fail independently.
"""
import argparse, atexit, base64, binascii, contextlib, glob, hashlib, ipaddress, json, logging, os, queue, re, select, shlex, shutil, signal, socket, subprocess, sys, tempfile, threading, time, urllib.parse, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    # The core package and activity.redact live next to this file; a direct `python
    # tests/...` run or an import by path must still resolve them.
    sys.path.append(HERE)
SK = os.path.join(HERE, "agent.py")
PROVIDERS_DIR = os.path.join(HERE, "providers")
LOGDIR = os.environ.get("AGENT_CLI_LOGS") or os.path.expanduser("~/.claude/agent-cli-logs")
# WHY: each running bridge drops a bridge-<port>.json here so the GUI (gui.py) can list, inspect
# and stop bridges it didn't launch itself -- the process self-registers on bind and removes its
# own file on a clean exit; the GUI prunes files whose port no longer answers /health.
BRIDGES_DIR = os.path.join(LOGDIR, "bridges")
# Random per-process identity, published in the registry file AND in GET /health so
# gui.stop_bridge / `openai-server stop` only ever kill the process they recorded. A detached
# start hands the child a pre-chosen id (AGENT_BRIDGE_INSTANCE_ID) so the parent can find its
# registry entry even before it knows the port (--port 0).
INSTANCE_ID = uuid.uuid4().hex[:12]
# Cap on one request body; larger or malformed Content-Length gets 400/413 (--max-request-mb).
MAX_BODY_BYTES = 64 * 1024 * 1024
STARTED_AT = time.time()


class _RequestContext(threading.local):
    """Per-thread request state: which route and pooled session the current CLI turn belongs to.

    Every helper that used to read the single global SESSION/CFG engine reads these instead, so
    concurrent conversations never share history, task state, scratch dir or native processes.
    With no request bound (unit tests calling H._run directly) the helpers fall back to the
    module-level SESSION and to a route built from CFG -- the historic single-session behaviour."""
    session = None
    route = None
    cancel = None
    task = None
    last_run = None
    effort = None  # the request's reasoning-effort decision, reported in the response metadata


_CTX = _RequestContext()

# Opt-in diagnostics: quiet (WARNING) unless AGENT_LOG_LEVEL says otherwise -- request lines at
# DEBUG, infrastructure failures (provider config, registry writes) at WARNING. Payloads are
# never logged anywhere: no request bodies, prompts, auth headers, keys or model output.
LOG = logging.getLogger("neoxider.openai_server")
if not LOG.handlers:  # a re-import must not stack duplicate handlers
    _lh = logging.StreamHandler()  # stderr
    _lh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    LOG.addHandler(_lh)
try:
    LOG.setLevel(os.environ.get("AGENT_LOG_LEVEL", "WARNING").upper())
except ValueError:  # garbage in AGENT_LOG_LEVEL -> stay quiet rather than refuse to start
    LOG.setLevel(logging.WARNING)
LOG.propagate = False

PYTHON = sys.executable


def _cli_command(name, *arguments):
    """Resolve Windows npm launchers while keeping every spawn windowless."""
    from neoxider_agents.providers import executable
    try:
        return executable(name) + list(arguments)
    except FileNotFoundError:
        pass
    executable = shutil.which(name) or name
    if os.name == "nt" and executable.lower().endswith((".cmd", ".bat")):
        return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c",
                subprocess.list2cmdline([executable] + list(arguments))]
    return [executable] + list(arguments)


def load_providers():
    out = {}
    for pf in sorted(glob.glob(os.path.join(PROVIDERS_DIR, "*", "provider.json"))):
        name = os.path.basename(os.path.dirname(pf))
        try:
            with open(pf, encoding="utf-8") as f:
                out[name] = json.load(f)
        except Exception as e:
            LOG.warning("skipping unreadable provider config %s (%s)", name, type(e).__name__)
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


_PROVIDER_NOISE_RE = re.compile(r"^(?:\[(?:agent-activity|opencode|codex|kimi|gemini|claude)\] "
                                r"|session id: |AGENT_PROVIDER_ERROR: |!! TIMEOUT: )")


def _strip_provider_noise(text):
    """Drop a TRAILING run of the provider's own bookkeeping lines from an answer.

    Providers write those to stderr — `[opencode] ...` CLI diagnostics, `[agent-activity] ...`
    heartbeats, `AGENT_PROVIDER_ERROR: ...` provider failures, agent.sh's `!! TIMEOUT:` kill notice —
    and generic dispatch merges stderr into the same task log the answer is sliced from. The opencode
    provider now prints its answer LAST so they land before the marker, but a log written under any
    other ordering would still hand them back as completion content: that is exactly how a one-word
    vision answer ("Red") once shipped with ~80 `[opencode] unknown format "uint32" ignored in
    schema` lines glued to it. Only a trailing run is removed, and text that ends in anything else is
    returned byte-for-byte, so a diagnostic quoted inside the answer survives untouched."""
    lines = text.split("\n")
    end = len(lines)
    while end and not lines[end - 1].strip():
        end -= 1
    if not end or not _PROVIDER_NOISE_RE.match(lines[end - 1]):
        return text
    while end and (_PROVIDER_NOISE_RE.match(lines[end - 1]) or not lines[end - 1].strip()):
        end -= 1
    return "\n".join(lines[:end])


class ProviderNoiseStreamFilter:
    """Keep provider diagnostics out of SSE content before bytes reach the client."""

    _PREFIXES = ("[agent-activity] ", "[opencode] ", "[codex] ", "[kimi] ",
                 "[gemini] ", "[claude] ", "session id: ",
                 "AGENT_PROVIDER_ERROR: ", "!! TIMEOUT: ")

    def __init__(self, sink):
        self.sink = sink
        self.reset()

    def reset(self):
        self.at_line_start = True
        self.pending = ""
        self.dropping = False

    def feed(self, piece):
        output = []
        for char in piece:
            if self.dropping:
                if char == "\n":
                    self.dropping = False
                    self.at_line_start = True
                continue
            if self.at_line_start:
                self.pending += char
                if self.pending in self._PREFIXES:
                    self.pending = ""
                    self.dropping = True
                    continue
                if any(prefix.startswith(self.pending) for prefix in self._PREFIXES):
                    continue
                output.append(self.pending)
                self.pending = ""
                self.at_line_start = char == "\n"
            else:
                output.append(char)
                if char == "\n":
                    self.at_line_start = True
        if output:
            self.sink("".join(output))

    def finish(self):
        if self.pending:
            self.sink(self.pending)
            self.pending = ""


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
        return _strip_provider_noise(text)
    return _strip_provider_noise("\n".join(lines[last + 1:]).lstrip("\n"))


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
    """Path (not created) of the stable working dir of the CURRENT conversation -- one per pooled
    session (session-<port>-<session id>), so two conversations never share or wipe each other's
    directory. Without a bound session (direct helper calls) it is the historic per-port path."""
    base = os.path.join(tempfile.gettempdir(), "neoxider-openai-bridge")
    session = getattr(_CTX, "session", None)
    if session is not None and getattr(session, "id", None):
        return os.path.join(base, "session-%d-%s" % (CFG.port, session.id))
    return os.path.join(base, "session-%d" % CFG.port)


def fresh_session_dir(wipe=True):
    """(Re)creates the working dir for a BRAND-NEW session. Wipes any leftover files from a
    previous, unrelated conversation first -- since this dir now persists for a whole session's
    lifetime (not a disposable per-call temp dir), a stray file the agent wrote in an earlier,
    different conversation must not leak into this new one. Never touches an operator-pinned
    --dir (that's a real project path -- respected as-is, exactly like every other command in
    this project).

    wipe=False when the conversation is CONTINUING but the engine has no CLI-level resume
    (opencode/gemini), so every turn takes the fresh-run path. Deleting and recreating the
    directory under a live `opencode serve` on every single turn of one conversation is both
    pointless (same conversation -> the leak this guards against cannot happen) and destabilising
    -- the server holds that path as the session's project root."""
    if CFG.dir:
        return CFG.dir
    d = session_scratch_dir()
    if wipe:
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


def _hidden_windows_kwargs():
    """Prevent CLI subprocesses from opening tabs in the configured Windows terminal."""
    if os.name != "nt":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
            "startupinfo": startupinfo}


def _process_group_kwargs():
    """Start a hidden subprocess in a group we can terminate as one process tree."""
    if os.name == "nt":
        kwargs = _hidden_windows_kwargs()
        kwargs["creationflags"] |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        return kwargs
    return {"start_new_session": True}


_PROCESS_TREES_LOCK = threading.RLock()
_PROCESS_TREES = {}
_PROCESS_TREES_SHUTTING_DOWN = False
_SIGTERM_HANDLER_ACTIVE = False


def _attach_process_tree(proc):
    """Remember a killable process container even if the direct child exits first.

    POSIX process groups outlive their leader, so the launch-time pgid is sufficient.  On
    Windows, ancestry-based ``taskkill /T`` stops working as soon as the wrapper exits; a Job
    Object remains a stable handle to every descendant and is therefore the primary mechanism.
    The Job setup is best-effort because some restricted hosts disallow nested jobs; taskkill is
    retained as a fallback for the ordinary live-wrapper case.
    """
    proc._neoxider_tree_state = "active"
    if os.name != "nt":
        proc._neoxider_pgid = proc.pid
        return
    try:
        import ctypes
        from ctypes import wintypes

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
        kernel32.SetInformationJobObject.argtypes = (
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
        kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
        kernel32.TerminateJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = 0x00002000  # KILL_ON_JOB_CLOSE
        configured = kernel32.SetInformationJobObject(
            job, 9, ctypes.byref(info), ctypes.sizeof(info))
        assigned = configured and kernel32.AssignProcessToJobObject(
            job, wintypes.HANDLE(int(proc._handle)))
        if not assigned:
            kernel32.CloseHandle(job)
            return
        proc._neoxider_job = job
        proc._neoxider_kernel32 = kernel32
    except Exception:
        # Containment is best-effort on restricted/nested Windows hosts. The process remains in
        # the registry and falls back to taskkill, so an unusual ctypes failure must not untrack it.
        return


def _close_process_tree_container(proc):
    """Close one claimed container. The caller exclusively owns it."""
    job = getattr(proc, "_neoxider_job", None)
    kernel32 = getattr(proc, "_neoxider_kernel32", None)
    if job and kernel32:
        try:
            kernel32.CloseHandle(job)
        except (AttributeError, OSError):
            pass
        proc._neoxider_job = None
    proc._neoxider_tree_state = "closed"


def _claim_process_tree(proc):
    """Atomically take cleanup ownership so a Job/PGID is never closed or killed twice."""
    if proc is None:
        return False
    with _PROCESS_TREES_LOCK:
        state = getattr(proc, "_neoxider_tree_state", None)
        if state in ("claimed", "closed"):
            return False
        registered = _PROCESS_TREES.get(id(proc))
        if registered is proc:
            del _PROCESS_TREES[id(proc)]
        proc._neoxider_tree_state = "claimed"
        return True


def _release_process_tree(proc):
    """Release containment only after removing descendants left by a successful wrapper.

    Closing a Windows Job Object has KILL_ON_JOB_CLOSE semantics.  POSIX has no equivalent
    closeable handle, so explicitly terminate the launch-time process group while this claimed
    ``proc`` still owns its identity; never rediscover a group from a possibly reused wrapper PID.
    """
    if not _claim_process_tree(proc):
        return
    if os.name == "nt":
        _close_process_tree_container(proc)
    else:
        _terminate_claimed_process_tree(proc)


def _popen_process_tree(args, **kwargs):
    # Keep launch+registration atomic with shutdown. Otherwise SIGTERM could drain the registry
    # after Popen but before registration, leaving exactly one untracked provider tree behind.
    with _PROCESS_TREES_LOCK:
        if _PROCESS_TREES_SHUTTING_DOWN:
            raise RuntimeError("bridge process shutdown is already in progress")
        proc = subprocess.Popen(args, **kwargs, **_process_group_kwargs())
        _attach_process_tree(proc)
        _PROCESS_TREES[id(proc)] = proc
        return proc


def _terminate_claimed_process_tree(proc):
    """Terminate a process tree already claimed from the central registry."""
    try:
        if os.name == "nt":
            job = getattr(proc, "_neoxider_job", None)
            kernel32 = getattr(proc, "_neoxider_kernel32", None)
            if job and kernel32:
                kernel32.TerminateJobObject(job, 1)
            elif proc.poll() is None:
                # taskkill /T is the supported way to include grandchildren created by bash.
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=10, check=False, **_hidden_windows_kwargs())
            if proc.poll() is None:
                proc.kill()  # taskkill failure is reported by exit code, not an exception
        else:
            # The group survives its leader. Never look the pgid up through proc.pid here: the
            # direct wrapper may already have exited while descendants remain in the group.
            os.killpg(getattr(proc, "_neoxider_pgid", proc.pid), signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.wait(timeout=10)
    except (OSError, subprocess.SubprocessError):
        pass
    finally:
        _close_process_tree_container(proc)


def _terminate_process_tree(proc):
    """Terminate and reap proc plus descendants. Safe when proc already exited."""
    if not _claim_process_tree(proc):
        return
    _terminate_claimed_process_tree(proc)


def _shutdown_process_trees():
    """Permanently stop new launches and terminate every registered provider container."""
    global _PROCESS_TREES_SHUTTING_DOWN
    with _PROCESS_TREES_LOCK:
        _PROCESS_TREES_SHUTTING_DOWN = True
        processes = list(_PROCESS_TREES.values())
        _PROCESS_TREES.clear()
        for proc in processes:
            proc._neoxider_tree_state = "claimed"
    for proc in processes:
        try:
            _terminate_claimed_process_tree(proc)
        except Exception:
            # Shutdown must make a best effort on every registered tree even if one platform
            # primitive misbehaves; never let the first broken handle skip all later providers.
            LOG.exception("failed to terminate provider process tree pid=%s", getattr(proc, "pid", "?"))
            try:
                proc.kill()
            except Exception:
                pass
            _close_process_tree_container(proc)


def _handle_sigterm(signum, _frame):
    """Make GUI ``kill(SIGTERM)`` deterministic even though default SIGTERM skips atexit."""
    global _SIGTERM_HANDLER_ACTIVE
    if _SIGTERM_HANDLER_ACTIVE:
        # Python may dispatch another SIGTERM while the first handler is between two tree
        # terminations.  The outer handler already owns the drained list; raising here would
        # abandon its remaining entries after the central registry has been cleared.
        return
    _SIGTERM_HANDLER_ACTIVE = True
    try:
        _shutdown_process_trees()
    finally:
        raise SystemExit(128 + signum)


# The core's own no-output watchdog (AGENT_SILENCE_SEC, set from --stall-timeout) normally ends a
# wedged provider first and records state=silent. The bridge kills the step itself only when the
# task files have not moved for this much longer -- a backstop for a wedged wrapper.
STALL_GRACE_SECONDS = 30


def _stall_seconds():
    """--stall-timeout in seconds, 0 = disabled (also for test configs without the field)."""
    try:
        return max(0, int(getattr(CFG, "stall_timeout", 0) or 0))
    except (TypeError, ValueError):
        return 0


def _task_progress(name):
    """A cheap token that changes whenever the core records provider activity for task `name`."""
    token = []
    for suffix in (".log", ".meta", ".activity.jsonl"):
        try:
            stat = os.stat(os.path.join(LOGDIR, name + suffix))
            token.append((stat.st_size, stat.st_mtime_ns))
        except OSError:
            token.append(None)
    return tuple(token)


def _run_agent_process(args, timeout, env):
    """Run an agent.sh step with an outer deadline that cannot orphan CLI grandchildren.

    Also aborts the whole tree when the HTTP client disconnected (_CTX.cancel) or the task made no
    progress for --stall-timeout (+ grace). The outcome (exit code, stderr tail, which watchdog
    fired) is kept in _CTX.last_run so the failure can be mapped to a precise HTTP error."""
    proc = _popen_process_tree(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, encoding="utf-8", errors="replace", cwd=HERE, env=env)
    outcome = {"returncode": None, "stderr": "", "timed_out": False, "stalled": False,
               "cancelled": False}
    cancel = getattr(_CTX, "cancel", None)
    task = getattr(_CTX, "task", None)
    stall = _stall_seconds()
    deadline = time.monotonic() + timeout
    last_token, last_change = None, time.monotonic()
    killed = False
    stderr_text = ""
    try:
        while True:
            try:
                _stdout, stderr_text = proc.communicate(timeout=0.25)
                break
            except subprocess.TimeoutExpired:
                pass
            now = time.monotonic()
            fired = None
            if cancel is not None and cancel.is_set():
                fired = "cancelled"
            elif now >= deadline:
                fired = "timed_out"
            elif stall and task:
                token = _task_progress(task)
                if token != last_token:
                    last_token, last_change = token, now
                elif now - last_change >= stall + STALL_GRACE_SECONDS:
                    fired = "stalled"
            if fired:
                outcome[fired] = True
                killed = True
                _terminate_process_tree(proc)
                try:
                    _stdout, stderr_text = proc.communicate(timeout=5)
                except (subprocess.TimeoutExpired, OSError, ValueError):
                    pass
                break
    finally:
        if not killed:
            _release_process_tree(proc)
    outcome["returncode"] = proc.returncode
    outcome["stderr"] = (stderr_text or "")[-4000:]
    _CTX.last_run = outcome
    return outcome


def _chatonly_env():
    """Least-privilege env for every agent.sh subprocess launched by this bridge.

    AGENT_CHAT_ONLY=1 removes writes and MCP access through provider-specific controls. Claude and
    opencode have their tools fully disabled; Codex and Gemini retain read capability and therefore
    may only back a loopback bridge. Normal `agent.sh run`/`reply` calls leave the variable unset and
    retain the full file, shell, and MCP access needed for coding work.
    """
    env = dict(os.environ)
    env.pop("AGENT_LAUNCHER_PID", None)
    env["AGENT_CHAT_ONLY"] = "1"
    env.pop("AGENT_CODEX_IMAGE_PATHS", None)
    env.pop("AGENT_OPENCODE_IMAGE_PATHS", None)
    image_paths = getattr(_REQUEST_IMAGES, "paths", ())
    if image_paths and CFG is not None:
        engine = _route().engine
        if engine == "codex":
            env["AGENT_CODEX_IMAGE_PATHS"] = "\n".join(image_paths)
        elif engine == "opencode" and not _use_opencode_native():
            env["AGENT_OPENCODE_IMAGE_PATHS"] = "\n".join(image_paths)
    # The bridge owns request-level retries. Disable agent.sh's nested retry layer so one
    # configured retry cannot multiply into several provider invocations.
    env["AGENT_RETRIES"] = "0"
    # Hand agent.sh a deadline slightly SHORTER than our own subprocess timeout, so its step
    # watchdog kills the CLI's whole process tree (and marks the task error/124) before we give
    # up on the subprocess. Without that, killing only the wrapper left an orphaned CLI grandchild
    # alive, still appending to the task log -- the exact case the "healthy" resume check below
    # has to defend against. Do not inherit an unbounded/larger value: the inner watchdog must
    # always fire before this process's outer timeout.
    if CFG is not None and getattr(CFG, "timeout", None):
        try:
            env["AGENT_TIMEOUT_SEC"] = str(max(1, int(CFG.timeout) - 5))
        except (TypeError, ValueError):
            pass
    # The stream watchdog: the core kills a provider that printed nothing for --stall-timeout
    # seconds and records state=silent (mapped to a 504 "stalled"), instead of the turn hanging
    # until the overall timeout with the process alive and idle.
    stall = _stall_seconds()
    if stall:
        env["AGENT_SILENCE_SEC"] = str(stall)
    return env


# Longest prompt still safe as a command-line argument. Windows caps a whole command line at
# 32767 characters and the spawn fails outright above it -- as WinError 206, which Python raises as
# FileNotFoundError, so stage longer histories for the core's --prompt-file input.
ARGV_PROMPT_MAX = 16000


@contextlib.contextmanager
def _prompt_argument(text):
    """Yields the extra argv for `text`, staging it in a file when it is too long to pass directly."""
    if len(text) <= ARGV_PROMPT_MAX:
        yield [text]
        return
    prompt_dir = os.path.join(LOGDIR, ".prompts")
    os.makedirs(prompt_dir, exist_ok=True)
    fd, path = tempfile.mkstemp(prefix="bridge-prompt-", suffix=".txt", dir=prompt_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        LOG.info("prompt of %d chars staged for --prompt-file", len(text))
        yield ["--prompt-file", to_git_bash_path(path)]
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def run_agent(engine, model, effort, workdir, prompt, name, timeout):
    args = [PYTHON, SK, "run", "--no-progress", "-e", engine, "-C", to_git_bash_path(workdir), "-t", name]
    if model:
        args += ["-m", model]
    if effort:
        args += ["-f", effort]
    _CTX.task, _CTX.last_run = name, None
    with _prompt_argument(prompt) as prompt_args:
        _run_agent_process(args + prompt_args, timeout, _chatonly_env())
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
    args = [PYTHON, SK, "reply", "--no-progress", "-e", engine]
    if model:
        args += ["-m", model]
    if effort:
        args += ["-f", effort]
    args += ["-C", to_git_bash_path(workdir), name]
    before = len(read_log(name))
    _CTX.task, _CTX.last_run = name, None
    with _prompt_argument(answer) as answer_args:
        _run_agent_process(args + answer_args, timeout, _chatonly_env())
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


# --------------------------------------------------------------------------------------------
# Live streaming (real token deltas). Engines here run their CLI in a streaming output mode
# (see providers/<engine>/provider.py + stream_text_filter.py) so the task log GROWS while the
# model generates; the bridge tails the log and forwards each new piece to the SSE client.
LIVE_STREAM_ENGINES = {"claude"}
OUTPUT_MARKER = "---------- output ----------"


class LiveStreamDied(Exception):
    """A live run/resume failed AFTER deltas already reached the client -- it can't be retried
    invisibly (the client would see the answer twice). The caller finalizes the stream with
    whatever text was already forwarded."""


def _stream_env():
    env = _chatonly_env()
    env["AGENT_STREAM_TEXT"] = "1"  # provider switches the CLI to incremental text output
    return env


def _log_path(name):
    return os.path.join(LOGDIR, name + ".log")


def _log_size(name):
    try:
        return os.path.getsize(_log_path(name))
    except OSError:
        return 0


def _tail_task_log(name, proc, timeout, on_delta, start_size=0):
    """Stream this turn's first output block, excluding the core's final answer replay."""
    import codecs
    path = _log_path(name)
    deadline = time.time() + timeout
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    pos = start_size
    candidate = ""
    line_start = True
    in_output = False
    replayed = False
    killed = False

    def consume(text):
        nonlocal candidate, line_start, in_output, replayed
        emitted = []
        for char in text.replace("\r", ""):
            if replayed:
                break
            if line_start:
                if char == "\n" and candidate == OUTPUT_MARKER:
                    if in_output:
                        replayed = True
                    in_output = True
                    candidate = ""
                else:
                    candidate += char
                    if not OUTPUT_MARKER.startswith(candidate):
                        if in_output:
                            emitted.append(candidate)
                        candidate = ""
                        line_start = char == "\n"
            else:
                if in_output:
                    emitted.append(char)
                line_start = char == "\n"
        if emitted:
            on_delta("".join(emitted))

    def drain():
        nonlocal pos
        try:
            size = os.path.getsize(path)
        except OSError:
            return False
        if size <= pos:
            return False
        with open(path, "rb") as file:
            file.seek(pos)
            data = file.read(min(65536, size - pos))
        pos += len(data)
        consume(decoder.decode(data))
        return True

    cancel = getattr(_CTX, "cancel", None)
    stall = _stall_seconds()
    last_growth = time.monotonic()
    outcome = {"timed_out": False, "stalled": False, "cancelled": False}
    while True:
        alive = proc.poll() is None
        if drain():
            last_growth = time.monotonic()
            continue
        if not alive:
            break
        if not killed:
            fired = None
            if cancel is not None and cancel.is_set():
                fired = "cancelled"
            elif time.time() > deadline:
                fired = "timed_out"
            elif stall and time.monotonic() - last_growth >= stall + STALL_GRACE_SECONDS:
                fired = "stalled"
            if fired:
                outcome[fired] = True
                _terminate_process_tree(proc)
                killed = True
        time.sleep(0.05)
    _CTX.last_run = outcome
    consume(decoder.decode(b"", final=True))
    if candidate and in_output and not replayed and candidate != OUTPUT_MARKER:
        on_delta(candidate)


def run_agent_live(engine, model, effort, workdir, prompt, name, timeout, on_delta):
    """run_agent, but with AGENT_STREAM_TEXT=1 and a log tail forwarding answer deltas while
    the CLI generates. Returns the same final answer text run_agent would."""
    args = [PYTHON, SK, "run", "--no-progress", "-e", engine, "-C", to_git_bash_path(workdir), "-t", name]
    if model:
        args += ["-m", model]
    if effort:
        args += ["-f", effort]
    _CTX.task, _CTX.last_run = name, None
    with _prompt_argument(prompt) as prompt_args:
        proc = _popen_process_tree(args + prompt_args, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, cwd=HERE, env=_stream_env())
        try:
            _tail_task_log(name, proc, timeout, on_delta)
        finally:
            _release_process_tree(proc)
    return last_output(read_log(name))


def reply_agent_live(engine, model, effort, workdir, name, answer, timeout, on_delta):
    """reply_agent with a live log tail. Same None contract as reply_agent: None when the reply
    appended nothing or ended in a bad state (the caller decides whether a fresh-run fallback
    is still invisible to the client or the stream must be finalized as-is)."""
    args = [PYTHON, SK, "reply", "--no-progress", "-e", engine]
    if model:
        args += ["-m", model]
    if effort:
        args += ["-f", effort]
    args += ["-C", to_git_bash_path(workdir), name]
    before_bytes = _log_size(name)
    before_text = len(read_log(name))
    _CTX.task, _CTX.last_run = name, None
    with _prompt_argument(answer) as answer_args:
        proc = _popen_process_tree(args + answer_args, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, cwd=HERE, env=_stream_env())
        try:
            _tail_task_log(name, proc, timeout, on_delta, start_size=before_bytes)
        finally:
            _release_process_tree(proc)
    after = read_log(name)
    if len(after) == before_text:
        return None
    state = read_meta(name).get("state")
    if state not in ("done", "waiting"):
        return None
    return last_output(after)


# ======================================================================================
# opencode NATIVE backend -- talk to `opencode serve`'s HTTP API instead of spawning the
# `opencode run` CLI per request. OFF BY DEFAULT since 0.2.0 (AGENT_OPENCODE_NATIVE_UNSAFE=1 opts
# in): it is faster -- no CLI process per completion, and REAL token streaming from the server's
# global event bus (session.next.text.delta) -- but it cannot be locked down to chat-only, so it
# would hand every caller shell/file/MCP access. See OPENCODE_NATIVE below for the measurements.
# Flow: POST /api/session {model} -> subscribe SSE GET /api/event -> POST /api/session/{id}/prompt
#       -> collect this session's text.delta fragments until session.next.step.ended.
# ======================================================================================
import urllib.request as _urlreq
import urllib.error as _urlerr

# The chat-only profile the bridge's opencode sessions must run under. `"tools": {"*": false}` in
# providers/opencode/chat-only.json disables EVERY tool -- built-ins and MCP servers alike -- and
# OPENCODE_CONFIG merges with (does not replace) the user's own config, so providers/models/auth
# still work. WHY this matters: AGENT_CHAT_ONLY=1 only reaches the agent.sh provider scripts.
OPENCODE_CHATONLY_AGENT = "neoxider-chat-only"
OPENCODE_CHATONLY_CONFIG = os.environ.get("AGENT_OPENCODE_CHATONLY_CONFIG") or os.path.join(
    PROVIDERS_DIR, "opencode", "chat-only.json")

# The native `opencode serve` path is FASTER (no CLI process per completion, real deltas from the
# server's event bus) but CANNOT be locked down, so it is OFF by default -- measured 2026-08-13 on
# opencode 1.18.16: `opencode run --agent neoxider-chat-only` answers "NONE" when asked what tools
# it has, while the identical profile applied over HTTP (agent on POST /api/session, and again via
# POST /api/session/{id}/agent) still answers "apply_patch, bash, edit, glob, grep, question, read,
# skill, todowrite, webfetch, websearch, write" -- the server path does not honour the agent's tool
# map. Before this default flipped, a LAN-bound bridge handed every
# device on the network shell and write access to this machine, plus every configured MCP server:
# "create notes.txt" really created the file, in the bridge's own checkout.
# AGENT_OPENCODE_NATIVE_UNSAFE=1 opts back in, with a loud banner, for a loopback-only bridge where
# the latency matters more than the boundary.
OPENCODE_NATIVE = (os.environ.get("AGENT_OPENCODE_NATIVE_UNSAFE") == "1"
                   and os.environ.get("OPENCODE_NO_NATIVE") != "1")
_OC = {"base": None, "proc": None, "lock": threading.Lock()}


def _free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _ensure_opencode_server():
    """Boot one hidden native `opencode serve` and cache its base URL."""
    with _OC["lock"]:
        if _OC["base"] and _OC["proc"] and _OC["proc"].poll() is None:
            return _OC["base"]
        stale_proc = _OC.get("proc")
        if stale_proc is not None:
            # A dead wrapper can still own a live POSIX group or Windows Job descendants. Dispose
            # that launch-time container before forgetting it or installing its replacement.
            _terminate_process_tree(stale_proc)
        _OC["proc"] = None
        _OC["base"] = None
        port = _free_port()
        env = dict(os.environ)
        env["OPENCODE_CONFIG"] = OPENCODE_CHATONLY_CONFIG.replace("\\", "/")
        proc = _popen_process_tree(
            _cli_command("opencode", "serve", "--port", str(port), "--hostname", "127.0.0.1"),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
        )
        base = "http://127.0.0.1:%d" % port
        # wait for /api/health
        for _ in range(120):
            try:
                with _urlreq.urlopen(base + "/api/health", timeout=2) as r:
                    if r.status == 200:
                        _OC["base"] = base
                        _OC["proc"] = proc
                        print("[openai-bridge] opencode serve ready at %s" % base, file=sys.stderr)
                        return base
            except Exception:
                time.sleep(0.5)
        _terminate_process_tree(proc)
        raise RuntimeError("opencode serve did not become healthy on %s" % base)


def opencode_native_kill():
    with _OC["lock"]:
        proc = _OC.get("proc")
        _terminate_process_tree(proc)
        if _OC.get("proc") is proc:
            _OC["proc"] = None
            _OC["base"] = None


def _oc_json(base, path, method="GET", body=None, timeout=30):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = _urlreq.Request(base + path, data=data, method=method,
                          headers={"Content-Type": "application/json"})
    with _urlreq.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8", "replace")
    return json.loads(raw) if raw.strip() else {}


def opencode_session_body(model, workdir):
    """The POST /api/session payload for one bridge completion. Three deliberate parts:
      - "model": providerID/modelID, so the pinned -m actually applies;
      - "agent": the chat-only profile (see OPENCODE_CHATONLY_AGENT) -- the tool lockdown;
      - "location": the working directory. WITHOUT this, `opencode serve` inherits the BRIDGE
        PROCESS's cwd, so -C/--dir was silently ignored and the scratch-dir isolation was void:
        a plain "create notes.txt" request wrote the file into this repository's own checkout
        (observed live, opencode 1.18.16)."""
    provider_id, _, model_id = (model or "").partition("/")
    body = {"agent": OPENCODE_CHATONLY_AGENT}
    if provider_id and model_id:  # a bare model -> let opencode use its configured default
        body["model"] = {"providerID": provider_id, "id": model_id}
    if workdir:
        body["location"] = {"directory": workdir}
    return body


def opencode_native_complete(model, prompt, timeout, on_delta=None, workdir=None):
    """One completion through opencode's native server. `model` is provider/model (first '/'
    splits providerID from modelID). Streams real text deltas to on_delta when given; returns the
    full assistant text. Raises on transport/boot failure so the caller's retry logic still runs."""
    base = _ensure_opencode_server()
    sess_body = opencode_session_body(model, workdir)
    sid = _oc_json(base, "/api/session", "POST", sess_body, timeout=15)["data"]["id"]

    pieces = []
    done = threading.Event()
    err = {}

    def _pump():
        # Subscribe to the GLOBAL event bus and keep only our session's text deltas.
        try:
            req = _urlreq.Request(base + "/api/event", headers={"Accept": "text/event-stream"})
            with _urlreq.urlopen(req, timeout=timeout) as r:
                for bline in r:
                    if done.is_set():
                        break
                    line = bline.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    try:
                        o = json.loads(line[5:].strip())
                    except Exception:
                        continue
                    d = o.get("data") or {}
                    if d.get("sessionID") != sid:
                        continue
                    t = o.get("type", "")
                    if t == "session.next.text.delta":
                        frag = d.get("delta")
                        if isinstance(frag, str) and frag:
                            pieces.append(frag)
                            if on_delta is not None:
                                try:
                                    on_delta(frag)
                                except Exception:
                                    pass
                    elif t in ("session.next.step.ended", "session.next.error"):
                        done.set()
                        break
        except Exception as e:
            err["e"] = e
        finally:
            done.set()

    th = threading.Thread(target=_pump, daemon=True)
    th.start()
    time.sleep(0.4)  # let the SSE connection establish before prompting (avoid missing early deltas)
    _oc_json(base, "/api/session/%s/prompt" % sid, "POST", {"prompt": {"text": prompt}}, timeout=15)
    done.wait(timeout=timeout)
    done.set()
    th.join(timeout=3)
    text = "".join(pieces)
    if not text.strip():
        # nothing streamed (early miss / non-text answer) -> read the finished message as a fallback
        try:
            msg = _oc_json(base, "/api/session/%s/message" % sid, "GET", timeout=15)
            acc = []

            def _walk(o):
                if isinstance(o, dict):
                    if o.get("type") == "text" and isinstance(o.get("text"), str):
                        acc.append(o["text"])
                    for v in o.values():
                        _walk(v)
                elif isinstance(o, list):
                    for v in o:
                        _walk(v)
            _walk(msg.get("data", msg))
            text = acc[-1] if acc else text
            if on_delta is not None and text:
                on_delta(text)
        except Exception:
            pass
    return text


def _use_opencode_native():
    return OPENCODE_NATIVE and _route().engine == "opencode"


# ======================================================================================
# Native claude path: ONE persistent `claude -p --input/output-format stream-json` process
# instead of a fresh CLI spawn per completion. A cold `claude` start boots the full agent
# environment (~24k tokens of tools/skills/memory, measured 7-11s wall); the persistent
# process pays that ONCE, then each turn costs only inference (~3.5s measured on Opus) and
# keeps the provider prompt cache warm across turns. Same trick `opencode serve` uses.
# Disable with CLAUDE_NO_NATIVE=1 to fall back to the agent.sh spawn-per-call path.
# ======================================================================================
CLAUDE_NATIVE = os.environ.get("CLAUDE_NO_NATIVE") != "1"
CLAUDE_NATIVE_TASK = "__claude_native__"
_CL = {"proc": None, "queue": None, "lock": threading.RLock()}


def _new_native_state():
    return {"proc": None, "queue": None, "lock": threading.RLock()}


def _cl():
    """The persistent-claude state of the CURRENT conversation (each pooled session owns its own
    process, so conversations never share a native context); the module-level _CL otherwise."""
    session = getattr(_CTX, "session", None)
    native = getattr(session, "native", None)
    return native if native is not None else _CL


def _use_claude_native():
    return CLAUDE_NATIVE and _route().engine == "claude"


def _claude_native_cmd():
    # Chat-only lockdown mirrors AGENT_CHAT_ONLY (no shell/file/MCP tools), --verbose is
    # REQUIRED by --print + stream-json, partial messages give live deltas for stream:true.
    cmd = ("claude -p --verbose --input-format stream-json --output-format stream-json "
           "--include-partial-messages --strict-mcp-config --disable-slash-commands "
           "--tools ''")
    route = _route()
    resolved_model, resolved_effort, _label = resolve_claude_model(route.model, route.effort)
    cmd += " --model " + shlex.quote(resolved_model)
    if resolved_effort:
        cmd += " --effort " + shlex.quote(resolved_effort)
    return cmd


def claude_native_alive():
    state = _cl()
    with state["lock"]:
        p = state["proc"]
        return p is not None and p.poll() is None


def _native_kill_state(state):
    with state["lock"]:
        p = state["proc"]
        # Keep the stale state visible until its container is disposed. An ensure racing this
        # kill cannot replace it early, and registry ownership makes repeated cleanup a no-op.
        _terminate_process_tree(p)
        if state["proc"] is p:
            state["proc"] = None
            state["queue"] = None


def claude_native_kill():
    _native_kill_state(_cl())


def _claude_native_ensure():
    """Start (or reuse) the persistent claude process; a reader thread pumps stdout lines into
    a queue so turns can be awaited with a real timeout (blocking readline has none)."""
    state = _cl()
    with state["lock"]:
        current = state["proc"]
        if current is not None and current.poll() is None:
            return
        if current is not None:
            # poll() only describes the wrapper. Its separate group/Job can still contain provider
            # children, so release that container before clearing state and spawning a replacement.
            _terminate_process_tree(current)
        state["proc"] = None
        state["queue"] = None
        q = queue.Queue()
        proc = _popen_process_tree(
            _cli_command(*shlex.split(_claude_native_cmd())),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace", cwd=_session().get("dir") or None,
        )

        def _pump(p=proc, q=q):
            try:
                for line in p.stdout:
                    q.put(line)
            except Exception:
                pass
            finally:
                q.put(None)  # EOF sentinel

        threading.Thread(target=_pump, daemon=True).start()
        state["proc"] = proc
        state["queue"] = q


def _native_error_text(event):
    """The provider's own words from an is_error result event (never the prompt)."""
    for key in ("result", "error", "subtype"):
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            return json.dumps(value, ensure_ascii=False)
    errors = event.get("errors")
    if isinstance(errors, list) and errors:
        return "; ".join(str(item) for item in errors[:3])
    return "native Claude turn reported an error"


def claude_native_send(prompt, timeout, on_delta=None):
    """One turn against the persistent process: write a stream-json user message, read events
    until the matching "result". Forwards partial text deltas to on_delta when given. Returns
    the final answer text ("" on failure -- caller decides whether to retry after a kill).

    _CTX.last_run records why a turn failed: a stall (no event for --stall-timeout), the outer
    timeout, a client disconnect, a dead process, or an is_error result with the provider's text
    (auth/limit/model errors) -- so the caller maps it instead of returning it as content."""
    _CTX.last_run = None
    _claude_native_ensure()
    state = _cl()
    p, q = state["proc"], state["queue"]
    try:
        p.stdin.write(json.dumps(
            {"type": "user", "message": {"role": "user", "content": prompt}},
            ensure_ascii=False) + "\n")
        p.stdin.flush()
    except OSError:
        claude_native_kill()
        _CTX.last_run = {"error": "native Claude process refused input (it exited)"}
        return ""
    deadline = time.time() + max(30, timeout)
    cancel = getattr(_CTX, "cancel", None)
    stall = _stall_seconds()
    last_event = time.monotonic()
    texts = []
    while True:
        if cancel is not None and cancel.is_set():
            claude_native_kill()
            _CTX.last_run = {"cancelled": True}
            return ""
        if time.time() >= deadline:
            _CTX.last_run = {"timed_out": True}
            break
        if stall and time.monotonic() - last_event >= stall:
            _CTX.last_run = {"stalled": True}
            break
        try:
            line = q.get(timeout=max(0.05, min(0.5, deadline - time.time())))
        except queue.Empty:
            continue
        last_event = time.monotonic()
        if line is None:  # process exited mid-turn
            claude_native_kill()
            _CTX.last_run = {"error": "native Claude process exited mid-turn"}
            return ""
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
        except ValueError:
            continue
        typ = o.get("type")
        if typ == "stream_event" and on_delta is not None:
            ev = o.get("event") or {}
            if ev.get("type") == "content_block_delta":
                frag = (ev.get("delta") or {}).get("text")
                if isinstance(frag, str) and frag:
                    try:
                        on_delta(frag)
                    except Exception:
                        pass
        elif typ == "assistant":
            for c in ((o.get("message") or {}).get("content") or []):
                if isinstance(c, dict) and c.get("type") == "text" and c.get("text"):
                    texts.append(c["text"])
        elif typ == "result":
            final = o.get("result")
            if o.get("is_error") is True and not looks_like_limit_banner(final):
                _CTX.last_run = {"error": _native_error_text(o), "provider_error": True}
                return ""
            if isinstance(final, str) and final.strip():
                return final
            return "\n".join(texts)
    # Timed out / stalled waiting for the result -- the process state is unknown; restart next call.
    claude_native_kill()
    return ""


def _claude_native_raw(messages, tools, tool_choice, on_delta, can_retry, on_retry, live):
    """The session model (see _raw_completion) implemented against the persistent process:
    an extension of the previous messages sends ONLY the new turns to the SAME process (true
    resume, no re-boot); anything else restarts the process with the full history."""
    retry_ok = can_retry or (lambda: True)
    session = _session()
    with _session_lock():
        prev = session["messages"]
        extension = (session["task_name"] == CLAUDE_NATIVE_TASK
                     and not session_expired()
                     and claude_native_alive()
                     and is_extension(prev, messages))
        if extension:
            prompt = build_prompt(messages[len(prev):], tools, tool_choice)
        else:
            claude_native_kill()
            # A non-extension, expired session, or dead native process is a new security
            # boundary. Always wipe scratch state; fresh_session_dir itself preserves a pinned
            # operator --dir.
            session["dir"] = fresh_session_dir(wipe=True)
            prompt = build_prompt(messages, tools, tool_choice)
        raw = ""
        failure = None
        for attempt in range(CFG.retries + 1):
            raw = claude_native_send(prompt, CFG.timeout, on_delta if live else None)
            if raw.strip():
                break
            failure = classify_failure_outcome(
                "claude", outcome=getattr(_CTX, "last_run", None), raw_text=raw)
            if isinstance(failure, ClientDisconnected):
                session["task_name"] = None
                session["last_activity"] = time.time()
                raise failure
            if attempt < CFG.retries and retry_ok() and failure.internal_retry:
                print("[openai-bridge] claude native empty turn, retry %d/%d"
                      % (attempt + 1, CFG.retries), file=sys.stderr)
                if on_retry is not None:
                    on_retry()
                claude_native_kill()
                prompt = build_prompt(messages, tools, tool_choice)  # resume impossible after a kill
                time.sleep(1)
            else:
                break
        if not raw.strip():
            session["task_name"] = None
            session["last_activity"] = time.time()
            claude_native_kill()
            raise failure or CompletionFailed("native Claude invocation did not complete")
        session["task_name"] = CLAUDE_NATIVE_TASK
        session["messages"] = messages
        session["last_activity"] = time.time()
        return raw


def resolve_claude_model(model, effort):
    """Mirror provider_claude_resolve with a typed native-CLI result and matching label."""
    alias = model or "opus5"
    base = alias
    derived_effort = ""
    for suffix in ("low", "medium", "high", "xhigh", "max"):
        marker = "-" + suffix
        if alias.endswith(marker):
            base = alias[:-len(marker)]
            derived_effort = suffix
            break
    resolved = {
        "": "claude-opus-5", "default": "claude-opus-5", "opus5": "claude-opus-5",
        "sonnet": "claude-sonnet-5", "opus": "opus", "haiku": "haiku",
        "sonnet55": "claude-sonnet-5-5", "opus55": "claude-opus-5-5",
    }.get(base)
    if resolved is None:
        raise ValueError("unsupported Claude model alias")
    resolved_effort = effort or derived_effort or ("high" if base in ("sonnet", "sonnet55") else "")
    allowed_efforts = set((PROVIDERS.get("claude") or {}).get("efforts") or ())
    if resolved_effort and resolved_effort not in allowed_efforts:
        raise ValueError("unsupported Claude effort")
    labels = (PROVIDERS.get("claude") or {}).get("model_labels") or {}
    display = labels.get(base or "opus5", base or "opus5")
    if resolved_effort:
        display = "%s (%s)" % (display, resolved_effort)
    return resolved, resolved_effort, "claude/%s" % display


def model_label(engine, model, effort):
    """Human-readable, versioned label for the `model` field in responses -- e.g.
    "claude/Sonnet 5 (low)", not the bare CLI alias ("claude/sonnet-low"), which doesn't say
    WHICH real model that alias currently points to (aliases like "opus"/"sonnet" are resolved
    to a specific dated model id by the provider's own CLI or by provider_<engine>_resolve;
    see providers/<engine>/provider.json's "model_labels" for the alias -> display-name map)."""
    if engine == "claude":
        return resolve_claude_model(model, effort)[2]
    p = PROVIDERS.get(engine, {})
    alias = model or (p.get("default_model") or "default")
    label = (p.get("model_labels") or {}).get(alias, alias)
    if effort:
        label = "%s (%s)" % (label, effort)
    # avoid a doubled prefix like "opencode/opencode/big-pickle" when the model id already
    # carries its own provider segment (opencode's catalog is "<backend>/<model>").
    if label.startswith(engine + "/"):
        return label
    return "%s/%s" % (engine, label)


def _content_text(content):
    """OpenAI message content is either a plain string or a list of content-part dicts
    ({"type":"text","text":...} / {"type":"image_url",...}). Codex and the safe OpenCode
    CLI bridge receive bounded data-URL images as attachments."""
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
                    source = p.get("image_url") or p.get("image")
                    url = source.get("url", "") if isinstance(source, dict) else source
                    image_engine = (_route().engine if CFG is not None else "")
                    if image_engine in ("codex", "opencode") and not _use_opencode_native() \
                            and isinstance(url, str) \
                            and url.lower().startswith("data:image/"):
                        parts.append("[image attached to %s CLI]" %
                                     ("Codex" if image_engine == "codex" else "OpenCode"))
                    else:
                        parts.append("[image omitted -- this bridge is text-only]")
                else:
                    parts.append(json.dumps(p, ensure_ascii=False))
            else:
                parts.append(str(p))
        return "\n".join(parts)
    return str(content)


_REQUEST_IMAGES = threading.local()
_BRIDGE_IMAGE_RE = re.compile(r"^data:image/(jpeg|png|webp);base64,(.+)$", re.I | re.S)
_BRIDGE_IMAGE_MAX_BYTES = 4 * 1024 * 1024
_BRIDGE_IMAGE_MAX_COUNT = 8


def _validated_bridge_images(messages):
    """Decode every image the CLI turn would receive, rejecting unsupported inputs."""
    images = []
    for message_index, message in enumerate(messages):
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for part_index, part in enumerate(content):
            if not isinstance(part, dict) or part.get("type") not in ("image_url", "image"):
                continue
            param = "messages[%d].content[%d].image_url" % (message_index, part_index)
            if len(images) >= _BRIDGE_IMAGE_MAX_COUNT:
                raise RequestValidationError(param, "at most 8 images are supported per CLI turn", 413)
            source = part.get("image_url") or part.get("image")
            url = source.get("url", "") if isinstance(source, dict) else source
            match = _BRIDGE_IMAGE_RE.fullmatch(url) if isinstance(url, str) else None
            if not match:
                raise RequestValidationError(
                    param, "requires a base64 PNG, JPEG, or WebP data URL; remote URLs are unsupported")
            encoded = match.group(2)
            if len(encoded) > (_BRIDGE_IMAGE_MAX_BYTES + 2) // 3 * 4:
                raise RequestValidationError(param, "image exceeds the 4 MiB limit", 413)
            try:
                raw = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error):
                raise RequestValidationError(param, "invalid base64 image data") from None
            if not raw:
                raise RequestValidationError(param, "image data is empty")
            if len(raw) > _BRIDGE_IMAGE_MAX_BYTES:
                raise RequestValidationError(param, "image exceeds the 4 MiB limit", 413)
            extension = "jpg" if match.group(1).lower() == "jpeg" else match.group(1).lower()
            signature_ok = (
                (extension == "png" and raw.startswith(b"\x89PNG\r\n\x1a\n"))
                or (extension == "jpg" and raw.startswith(b"\xff\xd8") and raw.endswith(b"\xff\xd9"))
                or (extension == "webp" and raw.startswith(b"RIFF") and raw[8:12] == b"WEBP")
            )
            if not signature_ok:
                raise RequestValidationError(param, "image bytes do not match the declared format")
            images.append((raw, extension))
    return images


def _lift_camera_tool_images(messages):
    """Turn a camera tool's JSON dataUrl into a real OpenAI image part.

    Some OpenAI clients serialize function results as plain JSON strings even when
    their own image-lifting step ran. Keep a bounded image attachment for the CLI
    instead of letting the model receive an unreadable base64 paragraph.
    """
    if CFG is None or _route().engine not in ("codex", "opencode") \
            or _use_opencode_native():
        return messages
    converted = []
    for message in messages:
        content = message.get("content")
        if message.get("role") not in ("tool", "function") or not isinstance(content, str):
            converted.append(message)
            continue
        if '"dataUrl"' not in content or '"camera"' not in content:
            converted.append(message)
            continue
        try:
            payload = json.loads(content)
        except (TypeError, ValueError):
            converted.append(message)
            continue
        if not isinstance(payload, dict) or payload.get("ok") is not True \
                or not isinstance(payload.get("dataUrl"), str) \
                or not isinstance(payload.get("camera"), str):
            converted.append(message)
            continue
        image_url = payload.pop("dataUrl")
        payload["imageAttached"] = True
        lifted = dict(message)
        lifted["content"] = [
            {"type": "text", "text": json.dumps(payload, ensure_ascii=False)},
            {"type": "image_url", "image_url": {"url": image_url}},
        ]
        converted.append(lifted)
    return converted


@contextlib.contextmanager
def _bridge_images(messages):
    """Attach bounded request images to one CLI step and remove files afterwards."""
    engine = _route().engine if CFG is not None else ""
    if engine not in ("codex", "opencode") or _use_opencode_native():
        yield
        return
    images = _validated_bridge_images(messages)
    if not images:
        yield
        return
    previous = getattr(_REQUEST_IMAGES, "paths", ())
    with tempfile.TemporaryDirectory(prefix="neoxider-bridge-images-") as directory:
        paths = []
        for index, (raw, extension) in enumerate(images):
            digest = hashlib.sha256(raw).hexdigest()
            path = os.path.join(directory, "%02d-%s.%s" % (index, digest, extension))
            with open(path, "wb") as image_file:
                image_file.write(raw)
            paths.append(to_git_bash_path(path) if engine == "codex"
                         else path.replace("\\", "/"))
        _REQUEST_IMAGES.paths = paths
        try:
            yield
        finally:
            _REQUEST_IMAGES.paths = previous


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
# bridge launches every engine with AGENT_CHAT_ONLY=1 (see providers/{codex,claude}/provider.py),
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


TOOL_CHOICE_NONE_INSTRUCTIONS = """
Tool use is disabled for THIS turn. Reply only with a normal prose answer. Do not emit a
`tool_calls` JSON block or any function-call syntax, even though tool definitions may appear in
the conversation history.
"""


def _selected_tool_name(tool_choice):
    if isinstance(tool_choice, dict):
        fn = tool_choice.get("function")
        if isinstance(fn, dict):
            return fn.get("name")
    return None


def _tool_choice_requires_call(tool_choice):
    return tool_choice == "required" or _selected_tool_name(tool_choice) is not None


def build_prompt(messages, tools, tool_choice="auto"):
    text = BASE_INSTRUCTIONS.strip() + "\n\n" + render_messages(messages)
    if tools:
        if tool_choice == "none":
            text += "\n\n" + TOOL_CHOICE_NONE_INSTRUCTIONS.strip()
        else:
            selected = _selected_tool_name(tool_choice)
            prompt_tools = tools
            if selected:
                prompt_tools = [t for t in tools if t.get("function", {}).get("name") == selected]
            text += "\n\n" + (TOOLCALL_INSTRUCTIONS % json.dumps(
                prompt_tools, ensure_ascii=False, indent=2))
            if tool_choice == "required":
                text += ("\n\nFor THIS turn, at least one action is REQUIRED. Return one or more "
                         "tool calls in the required JSON format; a prose-only answer is invalid.")
            elif selected:
                text += ("\n\nFor THIS turn, you MUST call the function %s at least once and MUST "
                         "NOT call any other function. A prose-only answer is invalid."
                         % json.dumps(selected, ensure_ascii=False))
    return text


# Fence bodies deliberately exclude backticks ([^`]): with DOTALL-".*?" a malformed tool_calls
# fence ("{...} trailing junk") made the match run PAST its own closing fence into the next one,
# so one bad fence swallowed a following perfectly valid one (calls lost). (?i) so a ```JSON tag
# (observed from models) is recognized too. This variant accepts a body that is either a JSON
# OBJECT ({...}) or a JSON ARRAY ([...]) -- Opus 4.8 emits a fenced array of call objects.
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


def _bare_args_array(obj, tools):
    """A fenced JSON ARRAY whose elements are BARE ARGUMENT OBJECTS of exactly one declared
    tool -- Fable 5's live G6 spelling (```json [ {"action":"spawn","targetName":...}, ... ] ```
    x75: the whole castle scored tools=0 because no element carried a function name). Same
    deterministic gate as extract_bare_object_lines, applied to array elements: every element
    must be a non-empty JSON object and the union of keys must fit (subset of parameter names)
    EXACTLY ONE tool -- ambiguity or any non-matching key rejects the whole array, so a plain
    data array survives as content. Returns call-shaped [{name, arguments}, ...] or None."""
    if not (isinstance(obj, list) and obj and tools):
        return None
    props_by_tool = tool_param_names(tools)
    candidates = None
    for o in obj:
        if not isinstance(o, dict) or not o:
            return None
        keys = {k for k in o if isinstance(k, str)}
        fits = {name for name, props in props_by_tool.items() if keys <= set(props)}
        candidates = fits if candidates is None else candidates & fits
        if not candidates:
            return None
    if len(candidates) != 1:
        return None
    name = next(iter(candidates))
    return [{"name": name, "arguments": o} for o in obj]


# Key aliases models use in place of "arguments" -- "parameters" observed live from Haiku 4.5
# ({"function": {"name": ..., "parameters": {...}}}): _to_calls silently took EMPTY args and
# every call failed schema validation ("9 failed, 0 spawns").
_ARGS_KEY_ALIASES = ("arguments", "parameters", "params", "input")


def _args_of(d):
    """The argument payload of a call-ish dict, honoring the argument-key aliases."""
    for key in _ARGS_KEY_ALIASES:
        if key in d:
            return d.get(key)
    return {}


def _call_shaped(obj, names, allow_unknown=False):
    """The object IS one tool call: the OpenAI shape ({"function": {"name": ...}}, its own
    marker), the flat shape with EXACTLY {name-or-action, arguments-alias} keys ({action,
    arguments} observed live from Opus 4.8, {name, parameters} from Haiku 4.5 -- both scored
    tools=0 or empty-args failures before). Normalized to {name, arguments} so downstream
    conversion just works. Exact-keys rules mean a data answer with extra fields survives as
    content. Outside explicit call arrays, the name must be a KNOWN declared tool."""
    if not isinstance(obj, dict):
        return None
    fn = obj.get("function") if isinstance(obj.get("function"), dict) else None
    call_name = None
    flat = False
    if fn is not None:
        call_name = fn.get("name")
    else:
        keys = set(obj.keys())
        # "tool" observed live from GPT-5.5 ({"tool": "world_command", "arguments": {...}}).
        for name_key in ("name", "action", "tool"):
            for args_key in _ARGS_KEY_ALIASES:
                if keys == {name_key, args_key}:
                    call_name = obj.get(name_key)
                    flat = True
                    break
            if flat:
                break
    if (isinstance(call_name, str) and call_name
            and (allow_unknown or (names and call_name in names))):
        if fn is not None:
            return {"name": call_name, "arguments": _args_of(fn)}
        return {"name": call_name, "arguments": _args_of(obj)}
    return None


def _salvage_array_calls(body, names, tools):
    """A JSON ARRAY of calls whose WHOLE-parse failed: one malformed element (observed live
    from Spark -- a duplicated '"function",' line in element 13 of 75) used to poison the
    entire castle build to tools=0. Brace-scans each top-level {...} span, parses leniently,
    keeps the recognizable calls and skips broken elements. A well-formed element that is NOT
    a recognizable call aborts the salvage (a data array must survive as content). Only used
    AFTER strict parsing failed."""
    start = body.find("[")
    if start == -1:
        return None
    calls = []
    pos, n = start + 1, len(body)
    while pos < n:
        ch = body[pos]
        if ch in " \t\r\n,":
            pos += 1
            continue
        if ch == "]":
            break
        if ch != "{":
            pos += 1
            continue
        end = _object_end(body, pos)
        if end == -1:
            break
        try:
            obj = json.loads(body[pos:end])
        except ValueError:
            pos = end  # the malformed element -- skip it, keep salvaging
            continue
        shaped = _call_shaped(obj, names)
        if shaped is None and isinstance(obj, dict) and tools:
            bare = _bare_args_array([obj], tools)
            shaped = bare[0] if bare else None
        if shaped is None:
            return None  # well-formed non-call element: it's a data answer, not a call array
        calls.append(shaped)
        pos = end
    return calls or None


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
    """Normalize extracted calls, preserving model names for client-side validation."""
    out = []
    for c in raw_calls:
        if not isinstance(c, dict):
            continue
        fn = c.get("function") if isinstance(c.get("function"), dict) else None
        name = c.get("name") or (fn or {}).get("name")
        if not isinstance(name, str) or not name:
            continue
        args = _args_of(fn) if (fn is not None and any(k in fn for k in _ARGS_KEY_ALIASES)) \
            else _args_of(c)
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
    intended. If stripping leaves neither calls nor visible text, the original text is
    returned so the client can see the model's answer."""
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
            # Failing that, a call ARRAY with one malformed element (Spark live) is salvaged
            # object-by-object instead of dropping every good call in it.
            jsonl = _parse_call_jsonl(body, names) or _salvage_array_calls(body, names, tools)
            if jsonl:
                call_fences.append(jsonl)
                cleaned = cleaned[:m.start()] + cleaned[m.end():]
            continue
        if isinstance(obj, list):
            # A JSON ARRAY of call objects in one fence (Opus 4.8's dominant spelling:
            # ```json [ {"name":...,"arguments":...}, ... ] ```). Consumed only when every
            # element is call-shaped, so a plain data array survives as content. Falls back to
            # the bare-argument-objects array (Fable 5's live spelling, see _bare_args_array).
            shaped = [_call_shaped(o, names) for o in obj]
            if obj and all(s is not None for s in shaped):
                call_fences.append(shaped)
                cleaned = cleaned[:m.start()] + cleaned[m.end():]
            else:
                bare = _bare_args_array(obj, tools)
                if bare:
                    call_fences.append(bare)
                    cleaned = cleaned[:m.start()] + cleaned[m.end():]
            continue
        if not isinstance(obj, dict):
            continue
        if "tool_calls" not in obj:
            # Alias wrapper keys around the call array ({"actions":[...]} observed live from
            # Fable 5 -- a whole scenario scored tools=0). Stricter than "tool_calls": every
            # element must be call-shaped, or the list must be a bare-args array, so a data
            # answer that happens to use these common key names survives as content.
            aliased = None
            for key in CANONICAL_WRAPPER_KEYS[1:]:
                raw = obj.get(key)
                if isinstance(raw, list) and raw and len(obj) == 1:
                    shaped = [_call_shaped(o, names) for o in raw]
                    aliased = shaped if all(shaped) else _bare_args_array(raw, tools)
                    break
            if aliased:
                call_fences.append(aliased)
                cleaned = cleaned[:m.start()] + cleaned[m.end():]
                continue
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
        if stripped.startswith("{") or stripped.startswith("["):
            # raw_decode instead of loads: a bare (unfenced) JSON value followed by trailing
            # prose ("{...}\nDone.") is still a real call block -- the prose becomes the display
            # text instead of the whole turn being lost. Accepts the same spellings as a fenced
            # block: any wrapper alias around the call array, a bare array of call objects, or
            # a bare array of argument objects (unfenced arrays observed live from GPT-5.5 --
            # whole scenarios scored tools=0 while the calls sat in plain sight as text).
            try:
                obj, end = json.JSONDecoder().raw_decode(stripped)
            except ValueError:
                obj, end = None, 0
                if stripped.startswith("["):
                    # A call array with one malformed element (Spark live): salvage the good
                    # calls object-by-object instead of losing the whole turn.
                    salvaged = _salvage_array_calls(stripped, names, tools)
                    if salvaged:
                        found = _to_calls(salvaged)
                        if found:
                            calls, cleaned = found, ""
            shaped_list = None
            if isinstance(obj, dict) and len(obj) == 1:
                for key in CANONICAL_WRAPPER_KEYS:
                    raw = obj.get(key)
                    if isinstance(raw, list) and raw:
                        if key == "tool_calls":
                            # The prescribed wrapper is trusted as-is (matches the fenced
                            # path); alias wrappers are gated on recognizable elements.
                            shaped_list = raw
                        else:
                            shaped = [_call_shaped(o, names) for o in raw]
                            shaped_list = (shaped if all(shaped)
                                           else _bare_args_array(raw, tools))
                        break
            elif isinstance(obj, list) and obj:
                shaped = [_call_shaped(o, names) for o in obj]
                shaped_list = shaped if all(shaped) else _bare_args_array(obj, tools)
            if shaped_list:
                found = _to_calls(shaped_list)
                if found:
                    calls, cleaned = found, stripped[end:]
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
            calls = _to_calls([{"name": n, "arguments": a} for n, a in found])
            for s, e in reversed(spans):          # strip the call spans from the display text
                cleaned = cleaned[:s] + cleaned[e:]
            cleaned = cleaned.strip()
    cleaned = cleaned.strip()
    # Parsing must never hide the model's entire answer when no usable call remains.
    return calls, cleaned if calls or cleaned else text


# How much streamed content is held back before the first byte goes to the client. Two jobs:
# (1) a provider limit banner (always < 300 chars, see looks_like_limit_banner) must still be
# able to become an HTTP 429 -- impossible once SSE headers are out; (2) very short answers
# behave exactly like the non-streaming path (parsed once, sent once).
STREAM_HOLDBACK_CHARS = 350

# The canonical tool-call fence body this bridge's own prompt prescribes -- the ONLY spelling
# the incremental scanner parses call-by-call. Everything else waits for fence close
# (complete-fence parse) or end of turn (full-parser reconciliation).
# Wrapper keys accepted around a call array: "tool_calls" is the prescribed one; the aliases
# were each observed live ({"actions":[...]} from Fable 5 scored a whole scenario tools=0,
# {"commands":[...bare arg objects...]} from Haiku 4.5 scored a whole G6 castle tools=0,
# {"requests":[{"tool":...}]} from GPT-5.5 scored a whole G5 scenario tools=0).
CANONICAL_WRAPPER_KEYS = ("tool_calls", "actions", "calls", "function_calls", "commands",
                          "requests")


def _match_canonical_prefix(body):
    """('yes', end_index) when body starts with {"<wrapper>":[ for any accepted wrapper key
    (end_index = first char after it), ('maybe', -1) while body is still a truncation of one,
    ('no', -1) otherwise. Whitespace between the JSON tokens is allowed."""
    any_maybe = False
    for key in CANONICAL_WRAPPER_KEYS:
        want = '{"%s":[' % key
        wi = 0
        mismatch = False
        for i, ch in enumerate(body):
            if wi < len(want) and ch == want[wi]:
                wi += 1
                if wi == len(want):
                    return "yes", i + 1
            elif ch in " \t\r\n":
                continue
            else:
                mismatch = True
                break
        if not mismatch:
            any_maybe = True
    return ("maybe" if any_maybe else "no"), -1


def _object_end(s, start):
    """Index just past the matching '}' of the JSON object opening at s[start] (which must be
    '{'), honoring strings and escapes; -1 while the object is still incomplete."""
    depth = 0
    in_str = False
    i = start
    while i < len(s):
        ch = s[i]
        if in_str:
            if ch == '"' and not _is_escaped(s, i):
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return -1


class LiveToolCallEmitter:
    """Splits a streamed answer into SSE content deltas and tool calls emitted AS THEY CLOSE.

    Text outside code fences flows to on_content (after the initial holdback). A fence whose
    body starts with the canonical {"tool_calls":[ prefix is scanned object-by-object: each
    completed call object becomes an on_call the moment its closing brace arrives -- THE point
    of live streaming (a 100-call castle turn is one fence that only closes at the very end of
    the turn, so per-fence granularity would defeat the feature). Any other fence is held
    intact until it closes, then either parsed as calls (the non-canonical spellings
    extract_tool_calls knows) or released verbatim as content. finish() reconciles with the
    authoritative full parser so a spelling the incremental scanner missed still produces
    tool_calls -- late, but correct.

    Single-threaded by design (fed from the log-tail loop); not thread-safe."""

    def __init__(self, names, tools, prior, on_content, on_call):
        self.names = names or set()
        self.tools = tools
        self.prior = prior
        self.on_content = on_content
        self.on_call = on_call
        self.raw_parts = []      # every piece fed, verbatim (the full answer so far)
        self.buf = ""            # unprocessed tail outside a fence
        self.pending = ""        # content held back until STREAM_HOLDBACK_CHARS
        self.wire_started = False
        self.call_index = 0
        self.emitted_keys = []   # (name, canonical-args) of live-emitted calls, for reconciliation
        self._reset_fence()
        self.in_fence = False

    def _reset_fence(self):
        self.fence_raw = ""      # fence verbatim, including ``` markers and header line
        self.fence_body = ""     # body text (after the header line)
        self.header_done = False
        self.canonical = None    # None undecided / True scanning array / False complete-at-close
        self.fence_bad = False
        self.array_tail = ""     # unparsed remainder of the canonical array body
        self.fence_emitted = 0

    def reset(self):
        """Forget everything from a failed attempt that never reached the wire -- the retry's
        stream starts clean. Illegal after wire_started (callers gate retries on that)."""
        assert not self.wire_started
        self.raw_parts = []
        self.buf = ""
        self.pending = ""
        self._reset_fence()
        self.in_fence = False

    @property
    def raw_text(self):
        return "".join(self.raw_parts)

    # ---- output helpers -------------------------------------------------------------------
    def _flush_pending(self):
        if not self.wire_started and self.pending:
            self.on_content(self.pending)
            self.pending = ""
        self.wire_started = True

    def _content(self, s):
        if not s:
            return
        if self.wire_started:
            self.on_content(s)
            return
        self.pending += s
        if len(self.pending) >= STREAM_HOLDBACK_CHARS:
            self._flush_pending()

    def _emit_call_obj(self, obj):
        converted = _to_calls([obj])
        if not converted:
            return
        call = converted[0]
        self._flush_pending()  # content that preceded the fence keeps its order
        self.emitted_keys.append((call["function"]["name"],
                                  _canonical_args(call["function"]["arguments"])))
        self.on_call(call, self.call_index)
        self.call_index += 1

    # ---- input ----------------------------------------------------------------------------
    def feed(self, piece):
        if not piece:
            return
        self.raw_parts.append(piece)
        self.buf += piece
        self._process()

    def _process(self):
        while True:
            if not self.in_fence:
                i = self.buf.find("```")
                if i == -1:
                    # Keep the last 2 chars: they may be the start of a fence split across reads.
                    safe = self.buf[:-2] if len(self.buf) > 2 else ""
                    if safe:
                        self._content(safe)
                        self.buf = self.buf[len(safe):]
                    return
                if i > 0:
                    self._content(self.buf[:i])
                self.buf = self.buf[i + 3:]
                self.in_fence = True
                self._reset_fence()
                self.fence_raw = "```"
                continue
            # inside a fence: take text up to the closing ``` (or all of it, minus a possible
            # partial backtick run at the very end)
            j = self.buf.find("```")
            if j == -1:
                keep = 0
                while keep < 2 and len(self.buf) > keep and self.buf[-(keep + 1)] == "`":
                    keep += 1
                take = self.buf[:len(self.buf) - keep] if keep else self.buf
                self.buf = self.buf[len(take):]
                closed = False
            else:
                take = self.buf[:j]
                self.buf = self.buf[j + 3:]
                closed = True
            if take:
                self.fence_raw += take
                self._fence_text(take)
            if not closed:
                return
            self.fence_raw += "```"
            self._fence_closed()
            self.in_fence = False

    def _fence_text(self, take):
        """Routes new in-fence text: finish the header line first, then body handling."""
        if not self.header_done:
            combined = self.fence_body + take  # fence_body temporarily holds the partial header
            nl = combined.find("\n")
            if nl == -1:
                self.fence_body = combined
                return
            self.fence_body = ""
            body_piece = combined[nl + 1:]
            self.header_done = True
            take = body_piece
            if not take:
                return
        self.fence_body += take
        if self.fence_bad or self.canonical is False:
            return
        if self.canonical is None:
            state, end = _match_canonical_prefix(self.fence_body)
            if state == "yes":
                self.canonical = True
                self.array_tail = self.fence_body[end:]
                self._scan_array()
            elif state == "no":
                bracket = self.fence_body.find("[")
                if bracket != -1 and not self.fence_body[:bracket].strip():
                    # A plain JSON array fence (no {"tool_calls": wrapper): Opus writes arrays
                    # of call objects, Fable writes arrays of bare argument objects -- both
                    # stream object-by-object through the same scanner.
                    self.canonical = True
                    self.array_tail = self.fence_body[bracket + 1:]
                    self._scan_array()
                else:
                    self.canonical = False  # some other spelling: parsed whole at fence close
            return
        self.array_tail += take
        self._scan_array()

    def _scan_array(self):
        """Emits each completed call object of the canonical tool_calls array as it closes."""
        s = self.array_tail
        pos = 0
        n = len(s)
        while not self.fence_bad:
            while pos < n and s[pos] in " \t\r\n,":
                pos += 1
            if pos >= n:
                break
            if s[pos] == "]":
                pos = n  # array done; the trailing '}' / whitespace is consumed silently
                break
            if s[pos] != "{":
                self.fence_bad = True
                break
            end = _object_end(s, pos)
            if end == -1:
                break  # object still streaming -- wait for more text
            try:
                obj = json.loads(s[pos:end])
            except ValueError:
                self.fence_bad = True
                break
            shaped = self._shape_streamed_obj(obj)
            if shaped is None:
                self.fence_bad = True
                break
            self._emit_call_obj(shaped)
            self.fence_emitted += 1
            pos = end
        self.array_tail = s[pos:]

    def _shape_streamed_obj(self, obj):
        """One streamed array element -> a call-shaped object. Accepts every _call_shaped
        spelling, plus a BARE ARGUMENT OBJECT when its keys fit exactly one declared tool
        (Fable 5's array-of-bare-args spelling, streamed object-by-object). Ambiguity (several
        tools fit) returns None -- the fence degrades to complete-at-close handling, where the
        whole-array key-union decides (see _bare_args_array)."""
        # An explicit name in a call array is the model's choice, even if undeclared.
        # Match the full parser: leave rejection/correction to the client.
        shaped = _call_shaped(obj, self.names, allow_unknown=True)
        if shaped is not None:
            return shaped
        if isinstance(obj, dict) and obj and self.tools:
            keys = {k for k in obj if isinstance(k, str)}
            fits = {name for name, props in tool_param_names(self.tools).items()
                    if keys <= set(props)}
            if len(fits) == 1:
                return {"name": next(iter(fits)), "arguments": obj}
        return None

    def _fence_calls_complete(self, body):
        """Complete-fence parse mirroring extract_tool_calls' per-fence logic: returns a list
        of call-shaped objects, or None when the body is not a recognizable call fence."""
        body = body.strip()
        if not body:
            return None
        try:
            obj = json.loads(body)
        except ValueError:
            return (_parse_call_jsonl(body, self.names)
                    or _salvage_array_calls(body, self.names, self.tools))
        if isinstance(obj, dict):
            for key in CANONICAL_WRAPPER_KEYS:
                raw = obj.get(key)
                if isinstance(raw, list) and raw:
                    shaped = [_call_shaped(c, self.names) for c in raw]
                    if all(shaped):
                        return shaped
                    return _bare_args_array(raw, self.tools)
            one = _call_shaped(obj, self.names)
            return [one] if one else None
        if isinstance(obj, list) and obj:
            shaped = [_call_shaped(c, self.names) for c in obj]
            if all(shaped):
                return shaped
            return _bare_args_array(obj, self.tools)
        return None

    def _fence_closed(self):
        if self.canonical and not self.fence_bad:
            return  # every call already emitted as it closed
        if self.fence_emitted:
            # A canonical fence that went bad mid-way: the clean prefix is already emitted and
            # can't be unsent; drop the remainder -- finish()'s reconciliation emits whatever
            # the authoritative parser still finds.
            return
        calls = self._fence_calls_complete(self.fence_body)
        if calls:
            for c in calls:
                self._emit_call_obj(c)
        else:
            self._content(self.fence_raw)

    # ---- end of stream ----------------------------------------------------------------------
    def finish(self):
        """End of the CLI turn: flush leftovers and reconcile with the authoritative parser.
        Returns (extra_calls, fallback_text): calls the live scanner missed (already emitted via
        on_call here), and -- only when NOTHING was sent to the wire and there are no calls --
        the parser's cleaned display text to send instead of raw held-back content."""
        if self.in_fence:
            if not (self.canonical and not self.fence_bad) and not self.fence_emitted:
                self._content(self.fence_raw)  # unterminated non-call fence: it's just text
            self.in_fence = False
        elif self.buf:
            self._content(self.buf)
            self.buf = ""
        auth_calls, auth_text = extract_tool_calls(self.raw_text, self.names, self.tools,
                                                   self.prior)
        remaining = []
        unmatched = list(self.emitted_keys)
        for c in auth_calls or []:
            k = (c["function"]["name"], _canonical_args(c["function"]["arguments"]))
            if k in unmatched:
                unmatched.remove(k)
            else:
                remaining.append(c)
        for c in remaining:
            self._flush_pending()
            self.emitted_keys.append((c["function"]["name"],
                                      _canonical_args(c["function"]["arguments"])))
            self.on_call(c, self.call_index)
            self.call_index += 1
        if not self.wire_started:
            # Nothing (content or calls) reached the client yet: behave exactly like the
            # non-streaming path -- send the parsed display text once.
            self.pending = ""
            return auth_text
        self._flush_pending()
        return None

    @property
    def any_calls(self):
        return self.call_index > 0


CFG = None  # set in main(); read by the request handler

# The context-free default session: only direct helper calls (unit tests) use it. Every HTTP
# request runs inside its own pooled Session bound to _CTX (see SessionPool), so one conversation
# can never overwrite another's remembered history, CLI task or scratch directory.
SESSION_LOCK = threading.Lock()
SESSION = {"task_name": None, "messages": [], "dir": None, "last_activity": 0.0}
ACTIVE_LOCK = threading.Lock()
ACTIVE_REQUESTS = 0


def _session():
    """The conversation the current CLI turn belongs to (see _RequestContext)."""
    session = getattr(_CTX, "session", None)
    return session if session is not None else SESSION


def _session_lock():
    session = getattr(_CTX, "session", None)
    return session.state_lock if session is not None else SESSION_LOCK


class RequestValidationError(ValueError):
    def __init__(self, param, message, status=400):
        super().__init__(message)
        self.param = param
        self.status = status


class BridgeError(RuntimeError):
    """An OpenAI-style error with a meaningful HTTP status, never a bare 500.

    `retryable` tells the CLIENT whether the same request can succeed later (a 429 after its
    Retry-After, a 5xx); `internal_retry` tells the BRIDGE whether a silent re-run is worth it
    (empty answers, crashes, network blips -- never auth/billing/limit/model/bad-request
    refusals, and never timeouts/stalls, which already spent the request's time budget)."""
    status = 500
    error_type = "server_error"
    code = "bridge_error"
    retryable = False

    def __init__(self, message="", status=None, error_type=None, code=None, retryable=None,
                 retry_after=None, param=None, internal_retry=False):
        super().__init__(message or "bridge request failed")
        if status is not None:
            self.status = status
        if error_type is not None:
            self.error_type = error_type
        if code is not None:
            self.code = code
        if retryable is not None:
            self.retryable = retryable
        self.retry_after = retry_after
        self.param = param
        self.internal_retry = internal_retry

    def payload(self, incident=None):
        error = {"message": str(self), "type": self.error_type, "code": self.code,
                 "param": self.param, "retryable": bool(self.retryable)}
        if self.retry_after:
            error["retry_after_seconds"] = int(self.retry_after)
        if incident:
            error["incident_id"] = incident
        return {"error": error}

    def headers(self):
        return {"Retry-After": str(int(self.retry_after))} if self.retry_after else {}


class CompletionFailed(BridgeError):
    """Provider invocation exhausted retries without a usable completed answer."""
    status = 502
    error_type = "server_error"
    code = "provider_failed"
    retryable = True


class ClientDisconnected(Exception):
    """The HTTP client went away mid-turn; its CLI run was cancelled and nothing is written."""


def _validate_content(content, param, allow_none=False):
    if content is None and allow_none:
        return
    if isinstance(content, str):
        return
    if not isinstance(content, list):
        raise RequestValidationError(param, "must be a string or an array of content parts")
    for i, part in enumerate(content):
        pp = "%s[%d]" % (param, i)
        if not isinstance(part, dict):
            raise RequestValidationError(pp, "must be an object")
        typ = part.get("type")
        if not isinstance(typ, str) or not typ:
            raise RequestValidationError(pp + ".type", "must be a non-empty string")
        if typ == "text" and not isinstance(part.get("text"), str):
            raise RequestValidationError(pp + ".text", "must be a string")
        if typ in ("image", "image_url") and "image_url" in part \
                and not isinstance(part.get("image_url"), (str, dict)):
            raise RequestValidationError(pp + ".image_url", "must be a string or object")


def validate_chat_request(body):
    """Validate the OpenAI-shaped fields before any provider/session code can run."""
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise RequestValidationError("messages", "is required and must be a non-empty array")
    roles = {"system", "developer", "user", "assistant", "tool", "function"}
    for i, message in enumerate(messages):
        p = "messages[%d]" % i
        if not isinstance(message, dict):
            raise RequestValidationError(p, "must be an object")
        role = message.get("role")
        if role not in roles:
            raise RequestValidationError(p + ".role", "must be a supported role")
        tool_calls = message.get("tool_calls")
        if tool_calls is not None:
            if role != "assistant" or not isinstance(tool_calls, list) or not tool_calls:
                raise RequestValidationError(p + ".tool_calls",
                                             "must be a non-empty array on an assistant message")
            for j, call in enumerate(tool_calls):
                cp = "%s.tool_calls[%d]" % (p, j)
                if not isinstance(call, dict):
                    raise RequestValidationError(cp, "must be an object")
                if call.get("type", "function") != "function":
                    raise RequestValidationError(cp + ".type", "must equal 'function'")
                if not isinstance(call.get("id"), str) or not call.get("id"):
                    raise RequestValidationError(cp + ".id", "must be a non-empty string")
                fn = call.get("function")
                if not isinstance(fn, dict):
                    raise RequestValidationError(cp + ".function", "must be an object")
                if not isinstance(fn.get("name"), str) or not fn.get("name"):
                    raise RequestValidationError(cp + ".function.name", "must be a non-empty string")
                if not isinstance(fn.get("arguments"), str):
                    raise RequestValidationError(cp + ".function.arguments", "must be a JSON string")
        _validate_content(message.get("content"), p + ".content",
                          allow_none=(role == "assistant" and tool_calls is not None))
        if role == "tool" and (not isinstance(message.get("tool_call_id"), str)
                               or not message.get("tool_call_id")):
            raise RequestValidationError(p + ".tool_call_id", "must be a non-empty string")
        if "name" in message and not isinstance(message.get("name"), str):
            raise RequestValidationError(p + ".name", "must be a string")

    tools = body.get("tools")
    declared = set()
    if tools is not None:
        if not isinstance(tools, list) or not tools:
            raise RequestValidationError("tools", "must be a non-empty array")
        for i, tool in enumerate(tools):
            p = "tools[%d]" % i
            if not isinstance(tool, dict):
                raise RequestValidationError(p, "must be an object")
            if tool.get("type") != "function":
                raise RequestValidationError(p + ".type", "must equal 'function'")
            fn = tool.get("function")
            if not isinstance(fn, dict):
                raise RequestValidationError(p + ".function", "must be an object")
            name = fn.get("name")
            if (not isinstance(name, str) or not name
                    or re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name) is None):
                raise RequestValidationError(p + ".function.name", "must be a valid function name")
            if name in declared:
                raise RequestValidationError(p + ".function.name", "must be unique")
            declared.add(name)
            if "description" in fn and not isinstance(fn.get("description"), str):
                raise RequestValidationError(p + ".function.description", "must be a string")
            if "parameters" in fn and not isinstance(fn.get("parameters"), dict):
                raise RequestValidationError(p + ".function.parameters", "must be an object")

    choice = body.get("tool_choice")
    if choice is not None:
        if isinstance(choice, str):
            if choice not in ("none", "auto", "required"):
                raise RequestValidationError("tool_choice", "must be 'none', 'auto', or 'required'")
            if choice == "required" and not declared:
                raise RequestValidationError("tool_choice", "requires at least one declared function")
        elif isinstance(choice, dict):
            fn = choice.get("function")
            if (choice.get("type") != "function" or not isinstance(fn, dict)
                    or not isinstance(fn.get("name"), str)):
                raise RequestValidationError("tool_choice", "must select a declared function")
            if fn["name"] not in declared:
                raise RequestValidationError("tool_choice.function.name", "must name a declared function")
        else:
            raise RequestValidationError("tool_choice", "must be a string or object")
    else:
        choice = "auto" if declared else "none"
    if not declared and choice == "auto":
        choice = "none"

    stream = body.get("stream", False)
    if not isinstance(stream, bool):
        raise RequestValidationError("stream", "must be a boolean")
    return _lift_camera_tool_images(messages), tools, choice, stream


def _canonical_assistant_message(text, tool_calls):
    message = {"role": "assistant", "content": None if tool_calls else (text or "").strip()}
    if tool_calls:
        message["tool_calls"] = json.loads(json.dumps(tool_calls, ensure_ascii=False))
    return message


def _record_completion(messages, text, tool_calls, resumable=True):
    """Remember exactly the visible transcript, including the response returned to the caller."""
    canonical = list(messages) + [_canonical_assistant_message(text, tool_calls)]
    session = _session()
    with _session_lock():
        session["messages"] = canonical
        session["last_activity"] = time.time()
        if isinstance(session, Session):
            session.turns += 1
        if not resumable:
            session["task_name"] = None
            claude_native_kill()


def _invalidate_resume_session(messages):
    """A disconnected caller cannot be assumed to have received the assistant turn."""
    session = _session()
    with _session_lock():
        session["messages"] = list(messages)
        session["task_name"] = None
        session["last_activity"] = time.time()
        claude_native_kill()


def _parameter_error(exc):
    return {"error": {"message": "invalid parameter '%s': %s" % (exc.param, exc),
                      "type": "invalid_request_error", "param": exc.param,
                      "code": "invalid_parameter"}}


def _configured_api_key():
    """The shared secret callers must present, or "" when the bridge is open. `--api-key` wins over
    AGENT_OPENAI_KEY so one process can differ from the shell's default."""
    return (getattr(CFG, "api_key", "") or "").strip()


def request_authorized(headers, path):
    """True when this request may proceed. Open bridge (no key configured) -> everything passes,
    which keeps the loopback default working exactly as before. With a key configured, the OpenAI
    convention is honored -- `Authorization: Bearer <key>` -- plus a plain `X-Api-Key: <key>` for
    clients that cannot set an Authorization header.

    `/health` and `/` stay open ON PURPOSE: they carry no conversation data (engine/model label and
    session counters only) and the GUI's bridge list, the port-liveness probe and any uptime check
    poll them. Everything that can spend tokens, read the session or mutate it -- /v1/models,
    /v1/chat/completions, /reset -- requires the key. Comparison is constant-time so the endpoint
    cannot be turned into a byte-by-byte oracle for the key."""
    key = _configured_api_key()
    if not key:
        return True
    p = urllib.parse.urlparse(path).path
    if p in ("/health", "/"):
        return True
    presented = ""
    auth = headers.get("Authorization") or ""
    if auth[:7].lower() == "bearer ":
        presented = auth[7:].strip()
    if not presented:
        presented = (headers.get("X-Api-Key") or "").strip()
    import hmac
    return hmac.compare_digest(presented, key)


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
    session = _session()
    if not session["task_name"]:
        return None
    return time.time() - session["last_activity"]


def session_expired():
    """True when the remembered session has gone unused longer than --session-ttl seconds --
    treated exactly like a dead session (falls back to a fresh run), so an abandoned
    conversation can't be resumed indefinitely and its context can't grow forever. Mirrors how
    a real chat/API session would time out rather than stay resumable forever."""
    idle = session_idle_seconds()
    return idle is None or idle > CFG.session_ttl


# --------------------------------------------------------------------------------------------
# Provider/CLI failure -> HTTP status. The core records every failed turn in the task meta:
# state (error/limited/silent), exit (124 timeout, 125 silent, 126 provider failure) and
# failure_json {category, reason}; `reason` is the provider's own (already redacted) error
# payload, e.g. opencode's APIError JSON with statusCode 403 and a FreeTierError body.
_STATUS_IN_TEXT_RE = re.compile(r"(?:status(?:[ _]?code)?|http)[\s\"'=:]+([1-5]\d\d)\b", re.I)
_FORBIDDEN_RE = re.compile(r"forbidden|permission|not allowed|access denied|free[ _-]?tier|"
                           r"can only be used from|not available in your|unsupported (?:country|region)",
                           re.I)
_BILLING_RE = re.compile(r"payment required|insufficient[ _-]?(?:credit|funds|balance)|billing|"
                         r"credit balance|out of credits|top[ -]?up|add (?:more )?credits", re.I)
_AUTH_RE = re.compile(r"unauthori[sz]ed|invalid[ _-]?api[ _-]?key|authentication (?:failed|error|"
                      r"required)|not logged in|please (?:run /?)?log ?in|token (?:has )?expired|"
                      r"session expired", re.I)
_LIMIT_RE = re.compile(r"rate[ _-]?limit|ratelimit|too many requests|usage limit|quota|"
                       r"hit your (?:session|usage|weekly) limit", re.I)
_MODEL_RE = re.compile(r"model_not_found|unrecognized_model|unknown model|issue with the selected "
                       r"model|model[^\n]{0,80}(?:not found|unavailable|does not exist|not supported)",
                       re.I)
_BAD_REQUEST_RE = re.compile(r"bad request|invalid_request|context[_ ]length|maximum context|"
                             r"prompt is too long|request too large", re.I)
_UNAVAILABLE_RE = re.compile(r"service unavailable|overloaded|temporarily unavailable", re.I)
_NETWORK_RE = re.compile(r"econnreset|econnrefused|etimedout|eai_again|enotfound|socket hang up|"
                         r"network error|connection (?:reset|refused)|fetch failed", re.I)
_CLI_MISSING_RE = re.compile(r"CLI not found|is not recognized as an internal or external command|"
                             r"No such file or directory", re.I)
_RETRY_AFTER_RE = re.compile(
    r"(?:retry[- _]after|resets?\s+in|reset\s+in|try again in|available again in|retry in)"
    r"[\s\"':=]*((?:\d+(?:\.\d+)?\s*(?:ms|milliseconds?|seconds?|secs?|s|minutes?|mins?|m|hours?|"
    r"hrs?|h|days?|d)?\s*,?\s*(?:and\s+)?){1,3})", re.I)
_DURATION_PART_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([a-z]*)", re.I)


def _redact_text(text):
    try:
        from activity import redact
        return redact(text)
    except Exception:  # redaction is best-effort here; the core already redacted `reason`
        return text


def _retry_after_seconds(text):
    """Seconds from "retry after 30s" / "resets in 2h 5m" / a Retry-After header in the payload."""
    match = _RETRY_AFTER_RE.search(text or "")
    if not match:
        return None
    total = 0.0
    for number, unit in _DURATION_PART_RE.findall(match.group(1)):
        unit = unit.lower()
        scale = (0.001 if unit.startswith("ms") or unit.startswith("milli")
                 else 60 if unit.startswith("m") else 3600 if unit.startswith("h")
                 else 86400 if unit.startswith("d") else 1)
        total += float(number) * scale
    if total <= 0:
        return None
    return int(min(86400, max(1, round(total))))


def _provider_error_details(reason):
    """(message, http status, provider isRetryable, provider error type) from an error payload:
    the opencode/AI-SDK APIError JSON, an OpenAI-style {"error": {...}} body, or plain text."""
    found = {"message": "", "status": None, "retryable": None, "kind": ""}

    def walk(value, depth=0):
        if depth > 6:
            return
        if isinstance(value, str):
            text = value.strip()
            if text[:1] in "{[":
                try:
                    walk(json.loads(text), depth + 1)
                except ValueError:
                    pass
            return
        if isinstance(value, list):
            for item in value[:5]:
                walk(item, depth + 1)
            return
        if not isinstance(value, dict):
            return
        for key in ("statusCode", "status", "status_code", "code"):
            number = value.get(key)
            if isinstance(number, bool):
                continue
            if isinstance(number, str) and number.isdigit():
                number = int(number)
            if found["status"] is None and isinstance(number, int) and 100 <= number <= 599:
                found["status"] = number
        if found["retryable"] is None and isinstance(value.get("isRetryable"), bool):
            found["retryable"] = value["isRetryable"]
        kind = value.get("type")
        if not found["kind"] and isinstance(kind, str) and kind and kind != "error":
            found["kind"] = kind
        message = value.get("message")
        if not found["message"] and isinstance(message, str) and message.strip():
            found["message"] = message.strip()
        for key in ("data", "error", "responseBody", "body", "detail"):
            if key in value:
                walk(value[key], depth + 1)

    walk(reason)
    if not found["message"] and isinstance(reason, str):
        found["message"] = reason.strip()
    return found["message"], found["status"], found["retryable"], found["kind"]


def _error_summary(engine, message, status=None, kind=""):
    text = _redact_text(" ".join(str(message or "").split()))[:700]
    extras = []
    if status:
        extras.append("provider HTTP %d" % status)
    if kind and kind not in text:
        extras.append(kind)
    return "%s: %s%s" % (engine or "provider", text or "provider failure",
                         " (%s)" % ", ".join(extras) if extras else "")


def classify_failure_outcome(engine, meta=None, outcome=None, raw_text=""):
    """Map one failed CLI turn to a BridgeError (or ClientDisconnected) -- see ERRORS above.

    Inputs: the task meta the core wrote (state/exit/failure_json/reason), the bridge's own
    run outcome (watchdog flags, wrapper stderr) and the answer text. Only provider/wrapper error
    text is pattern-matched; the model's answer never is (except for the exact limit banner)."""
    meta = meta or {}
    outcome = outcome or {}
    if outcome.get("cancelled"):
        return ClientDisconnected("client disconnected; the CLI run was cancelled")
    state = str(meta.get("state") or "")
    exit_code = str(meta.get("exit") or "")
    try:
        failure = json.loads(meta.get("failure_json") or "{}")
    except (TypeError, ValueError):
        failure = {}
    if not isinstance(failure, dict):
        failure = {}
    category = str(failure.get("category") or "")
    reason = str(failure.get("reason") or meta.get("reason") or outcome.get("error") or "")
    stderr = str(outcome.get("stderr") or "")
    engine = engine or "provider"
    if (outcome.get("stalled") or state == "silent" or exit_code == "125"
            or category == "watchdog_silence"):
        quiet = _stall_seconds() or meta.get("silence") or "the configured"
        return CompletionFailed("%s produced no output for %s seconds (stream watchdog, --stall-timeout);"
                           " its CLI process tree was stopped" % (engine, quiet),
                           status=504, code="provider_stalled", retryable=True)
    if outcome.get("timed_out") or exit_code == "124" or category == "watchdog_timeout":
        return CompletionFailed("%s did not finish within --timeout %s seconds; its CLI process tree was"
                           " stopped" % (engine, getattr(CFG, "timeout", "?")),
                           status=504, code="provider_timeout", retryable=True)
    message, status, provider_retryable, kind = _provider_error_details(reason)
    if status is None:
        found = _STATUS_IN_TEXT_RE.search(reason)
        status = int(found.group(1)) if found else None
    evidence = " ".join((reason, message, kind))
    summary = _error_summary(engine, message or reason, status, kind)
    retry_after = _retry_after_seconds(reason)

    def error(http, code, error_type, retryable, internal=False):
        return CompletionFailed(summary, status=http, error_type=error_type, code=code,
                           retryable=retryable, retry_after=retry_after if http == 429 else None,
                           internal_retry=internal and provider_retryable is not False)

    billing = bool(_BILLING_RE.search(evidence))
    if status == 401:
        return error(401, "provider_auth_failed", "authentication_error", False)
    if status == 402 or (billing and (status == 429 or category == "quota" or state == "limited")):
        return error(402, "provider_billing", "insufficient_quota", False)
    if status == 403:
        return error(403, "provider_forbidden", "permission_error", False)
    if status == 404 or (status in (400, 422) and _MODEL_RE.search(evidence)):
        return error(404, "model_not_found", "invalid_request_error", False)
    if status == 429 or state == "limited" or category == "quota":
        return error(429, "rate_limit_exceeded", "rate_limit_error", True)
    if status == 408 or status == 504:
        return error(504, "provider_timeout", "server_error", True)
    if status in (400, 413, 422):
        return error(400, "provider_bad_request", "invalid_request_error", False)
    if status in (503, 529):
        return error(503, "provider_unavailable", "server_error", True, internal=True)
    if status is not None and status >= 500:
        return error(502, "provider_failed", "server_error", True, internal=True)
    if category == "auth":
        if _FORBIDDEN_RE.search(evidence):
            return error(403, "provider_forbidden", "permission_error", False)
        return error(401, "provider_auth_failed", "authentication_error", False)
    if category == "model_unavailable":
        return error(404, "model_not_found", "invalid_request_error", False)
    if category in ("network", "tls"):
        return error(503, "provider_unavailable", "server_error", True, internal=True)
    if category == "transport":
        if _UNAVAILABLE_RE.search(evidence):
            return error(503, "provider_unavailable", "server_error", True, internal=True)
        return error(502, "provider_failed", "server_error", True, internal=True)
    if category == "provider_oom":
        return error(502, "provider_crashed", "server_error", True, internal=True)
    if category == "cancelled":
        return error(503, "provider_cancelled", "server_error", True)
    if _CLI_MISSING_RE.search(stderr):
        return CompletionFailed(_error_summary(engine, stderr.strip().splitlines()[-1]),
                           status=503, code="provider_unavailable", retryable=False)
    if looks_like_limit_banner(raw_text):
        return CompletionFailed(_error_summary(engine, raw_text.strip()), status=429,
                           error_type="rate_limit_error", code="rate_limit_exceeded",
                           retryable=True, retry_after=_retry_after_seconds(raw_text))
    if reason:
        if _FORBIDDEN_RE.search(evidence) and _AUTH_RE.search(evidence) is None:
            return error(403, "provider_forbidden", "permission_error", False)
        if _AUTH_RE.search(evidence):
            return error(401, "provider_auth_failed", "authentication_error", False)
        if billing:
            return error(402, "provider_billing", "insufficient_quota", False)
        if _LIMIT_RE.search(evidence):
            return error(429, "rate_limit_exceeded", "rate_limit_error", True)
        if _MODEL_RE.search(evidence):
            return error(404, "model_not_found", "invalid_request_error", False)
        if _BAD_REQUEST_RE.search(evidence):
            return error(400, "provider_bad_request", "invalid_request_error", False)
        if _NETWORK_RE.search(evidence) or _UNAVAILABLE_RE.search(evidence):
            return error(503, "provider_unavailable", "server_error", True, internal=True)
        return error(502, "provider_failed", "server_error", True, internal=True)
    if state in ("", "done", "waiting") and not raw_text.strip():
        return CompletionFailed("%s returned an empty answer" % engine, status=502,
                           code="empty_completion", retryable=True, internal_retry=True)
    detail = stderr.strip().splitlines()[-1] if stderr.strip() else ""
    exit_text = exit_code or (str(outcome.get("returncode")) if outcome.get("returncode") else "")
    return CompletionFailed(_error_summary(engine, detail or "the CLI turn failed (state=%s exit=%s)"
                                      % (state or "unknown", exit_text or "?")),
                       status=502, code="provider_failed", retryable=True, internal_retry=True)


def _resume_failure(engine, name):
    """After a failed `reply`: the mapped error when a fresh-run fallback would only repeat it
    (auth/limit/model refusals, a stall/timeout, a cancelled client) -- else None (fall back)."""
    outcome = getattr(_CTX, "last_run", None) or {}
    meta = read_meta(name)
    if (meta.get("state") not in ("error", "limited", "silent")
            and not any(outcome.get(key) for key in ("cancelled", "stalled", "timed_out"))):
        return None
    failure = classify_failure_outcome(engine, meta, outcome)
    if isinstance(failure, ClientDisconnected) or not failure.internal_retry:
        return failure
    return None


def _log_incident(error, route=None, kind="provider"):
    incident = uuid.uuid4().hex[:12]
    LOG.warning("%s incident %s status=%s code=%s model=%s retryable=%s: %s", kind, incident,
                getattr(error, "status", "?"), getattr(error, "code", "?"),
                getattr(route, "id", "?"), getattr(error, "retryable", "?"),
                " ".join(str(error).split())[:800])
    return incident


# --------------------------------------------------------------------------------------------
# Routing: one process can serve several engine/model routes (see ROUTING in the docstring).
ROUTES = []            # configured routes; ROUTES[0] is the default
_DYNAMIC_ROUTES = {}   # --allow-any routes created on demand, keyed by lower-cased request id
_ROUTES_LOCK = threading.Lock()
_CFG_ROUTE = {}
MAX_DYNAMIC_ROUTES = 32
_ROUTE_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}$")


def route_id(engine, model="", effort=""):
    """The id a client sends as `model`: engine/model[@effort]. A model id that already carries
    its own provider segment (opencode's "opencode/x") is not double-prefixed."""
    model = model or ""
    if engine and model.startswith(engine + "/"):
        base = model
    else:
        base = "%s/%s" % (engine, model or "default")
    return base + ("@" + effort if effort else "")


class Route:
    """One routable engine/model/effort plus its live readiness for /health."""

    def __init__(self, engine, model="", effort="", dynamic=False, variant_of=None):
        self.engine = engine or ""
        self.model = model or ""
        self.effort = effort or ""
        self.dynamic = dynamic
        self.variant_of = variant_of  # the configured route a per-request reasoning effort derives from
        self.variants = {}
        self.id = route_id(self.engine, self.model, self.effort)
        self._label = None
        self._applied_effort = None
        self._stats_lock = threading.Lock()
        self.state = "unknown"
        self.last_error = None
        self.last_ok_at = None
        self.requests = 0
        self.failures = 0

    @property
    def label(self):
        if self._label is None:
            try:
                self._label = model_label(self.engine, self.model, self.effort)
            except ValueError:
                self._label = self.id
        return self._label

    def keys(self):
        label = self.label
        keys = {self.id, label, route_id(self.engine, self.model)}
        if self.effort and label.endswith(" (%s)" % self.effort):
            keys.add(label[:-len(" (%s)" % self.effort)])
        return {key.lower() for key in keys if key}

    def bare_keys(self):
        keys = {self.model} if self.model else set()
        if self.model.startswith(self.engine + "/"):
            keys.add(self.model[len(self.engine) + 1:])
        return {key.lower() for key in keys if key}

    def mark_probing(self):
        with self._stats_lock:
            self.state = "probing"

    def mark_ok(self):
        with self._stats_lock:
            self.state, self.last_ok_at = "ok", time.time()
            self.requests += 1

    def mark_error(self, error):
        with self._stats_lock:
            self.state = "error"
            self.requests += 1
            self.failures += 1
            self.last_error = {"status": getattr(error, "status", 500),
                               "code": getattr(error, "code", "bridge_error"),
                               "message": " ".join(str(error).split())[:500],
                               "retryable": bool(getattr(error, "retryable", False)),
                               "at": int(time.time())}

    @property
    def supports_effort(self):
        return bool((PROVIDERS.get(self.engine) or {}).get("efforts"))

    @property
    def applied_effort(self):
        """The effort the CLI actually receives: the configured/requested one, or the provider's
        own default when none is set (codex: medium, claude sonnet: high) -- None when unknown."""
        if self._applied_effort is None:
            effort = self.effort
            try:
                if self.engine == "claude":
                    effort = resolve_claude_model(self.model, self.effort)[1]
                elif self.engine in PROVIDERS:
                    from neoxider_agents.providers import get_provider
                    effort = get_provider(self.engine).resolve(self.model, self.effort)[1]
            except Exception:  # unknown/stub engine or alias: report what was configured
                effort = self.effort
            self._applied_effort = effort or ""
        return self._applied_effort or None

    def describe(self, default=False):
        with self._stats_lock:
            ready = {"ok": True, "error": False}.get(self.state)
            return {"id": self.id, "label": self.label, "engine": self.engine,
                    "model": self.model, "effort": self.effort, "default": default,
                    "applied_effort": self.applied_effort if self.supports_effort else None,
                    "effort_supported": self.supports_effort,
                    "variant_of": self.variant_of.id if self.variant_of is not None else None,
                    "dynamic": self.dynamic, "state": self.state, "ready": ready,
                    "requests": self.requests, "failures": self.failures,
                    "last_ok_at": int(self.last_ok_at) if self.last_ok_at else None,
                    "last_error": dict(self.last_error) if self.last_error else None}


def parse_model_spec(spec, effort=""):
    """`engine/model[@effort]` -> Route, or None when `spec` does not start with a known engine.

    `@effort` is split off only when it is one of that provider's advertised efforts. An OpenCode
    Zen id written without its provider segment ("opencode/space-bunny-free") keeps it, because
    `opencode run -m` needs provider/model; a bundled alias ("opencode/muse") stays an alias."""
    spec = (spec or "").strip()
    head, sep, rest = spec.partition("/")
    if not sep or head not in PROVIDERS or not rest:
        return None
    model = rest
    base, at, suffix = model.rpartition("@")
    if at and base and suffix in (PROVIDERS[head].get("efforts") or ()):
        model, effort = base, suffix
    if head == "opencode" and "/" not in model and model not in (PROVIDERS[head].get("models") or ()):
        model = "opencode/" + model
    return Route(head, model, effort)


def build_routes(engine, specs, effort="", engine_explicit=False):
    """The configured routes, default first. One spec keeps the historic `-e X -m Y` meaning
    exactly (the model goes to the CLI verbatim) unless it names its own engine; several specs
    each select a route, and bare ones use `-e` (default codex)."""
    if not specs:
        return [Route(engine, "", effort)]
    if len(specs) == 1:
        parsed = parse_model_spec(specs[0], effort)
        if parsed is None or (engine_explicit and (parsed.engine != engine or engine == "opencode")):
            return [Route(engine, specs[0], effort)]
        return [parsed]
    routes, seen = [], set()
    for spec in specs:
        route = parse_model_spec(spec, effort) or Route(engine, spec, effort)
        if route.id not in seen:
            seen.add(route.id)
            routes.append(route)
    return routes


def _cfg_route():
    """The route equivalent of a historic single-model CFG (unit tests, imports by gui.py)."""
    model = getattr(CFG, "model", "") or ""
    if isinstance(model, list):
        model = model[0] if model else ""
    key = (getattr(CFG, "engine", "") or "", model, getattr(CFG, "effort", "") or "")
    if _CFG_ROUTE.get("key") != key:
        _CFG_ROUTE["key"], _CFG_ROUTE["route"] = key, Route(*key)
    return _CFG_ROUTE["route"]


def _routes():
    return ROUTES or [_cfg_route()]


def _all_routes():
    with _ROUTES_LOCK:
        dynamic = [route for key, route in _DYNAMIC_ROUTES.items() if key == route.id.lower()]
    return _routes() + dynamic


def _route():
    """The route of the current CLI turn; the default route outside a request."""
    route = getattr(_CTX, "route", None)
    if route is not None:
        return route
    return _routes()[0]


def _strict_models():
    strict = getattr(CFG, "strict_models", None)
    if strict is not None:
        return bool(strict)
    return len(_routes()) > 1 or bool(getattr(CFG, "allow_any", False))


def _dynamic_route(spec):
    """--allow-any: a route for any `engine/model` of a known engine, created on first use."""
    route = parse_model_spec(spec)
    if route is None or not _ROUTE_MODEL_RE.match(route.model) or route.model.startswith("-"):
        return None
    if route.engine == "claude":
        try:
            resolve_claude_model(route.model, route.effort)
        except ValueError:
            return None
    route.dynamic = True
    with _ROUTES_LOCK:
        existing = _DYNAMIC_ROUTES.get(route.id.lower())
        if existing is not None:
            _DYNAMIC_ROUTES[spec.lower()] = existing
            return existing
        if len({r.id for r in _DYNAMIC_ROUTES.values()}) >= MAX_DYNAMIC_ROUTES:
            return None
        _DYNAMIC_ROUTES[route.id.lower()] = route
        _DYNAMIC_ROUTES[spec.lower()] = route
    LOG.warning("added on-demand route %s (--allow-any)", route.id)
    return route


def resolve_route(requested):
    """The route a request's `model` field selects (see ROUTING in the module docstring)."""
    routes = _routes()
    if requested is None or (isinstance(requested, str) and not requested.strip()):
        return routes[0]
    if not isinstance(requested, str):
        raise RequestValidationError("model", "must be a string")
    key = requested.strip().lower()
    for route in routes:
        if key in route.keys():
            return route
    bare = [route for route in routes if key in route.bare_keys()]
    if len(bare) == 1:
        return bare[0]
    with _ROUTES_LOCK:
        known = _DYNAMIC_ROUTES.get(key)
    if known is not None:
        return known
    base, at, suffix = key.rpartition("@")
    if at and base and suffix in EFFORT_LEVELS:
        # `codex/6-luna@high` for a configured `codex/6-luna`: the effort variant of that route.
        for route in routes:
            if base in route.keys() or base in route.bare_keys():
                return route_for_effort(route, suffix)[0]
    if getattr(CFG, "allow_any", False):
        route = _dynamic_route(requested.strip())
        if route is not None:
            return route
    if _strict_models():
        raise BridgeError("model '%s' is not served by this bridge; available: %s"
                          % (requested.strip()[:120], ", ".join(r.id for r in routes)),
                          status=404, error_type="invalid_request_error", code="model_not_found",
                          param="model")
    return routes[0]


# Reasoning effort per request (OpenAI `reasoning_effort`, or `reasoning: {"effort": ...}`).
EFFORT_LEVELS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")


def requested_effort(body):
    """The request's reasoning effort, lower-cased, or None when absent. 400 when malformed."""
    value = body.get("reasoning_effort")
    param = "reasoning_effort"
    if value is None and isinstance(body.get("reasoning"), dict):
        value, param = body["reasoning"].get("effort"), "reasoning.effort"
    if value is None:
        return None
    if not isinstance(value, str) or value.strip().lower() not in EFFORT_LEVELS:
        raise RequestValidationError(param, "must be one of %s" % ", ".join(EFFORT_LEVELS))
    return value.strip().lower()


def _nearest_effort(wanted, supported):
    """The supported level closest to `wanted` on the none..max scale; ties go to the cheaper one."""
    rank = {level: index for index, level in enumerate(EFFORT_LEVELS)}
    known = [level for level in supported if level in rank] or list(supported)
    if wanted in known or not known:
        return wanted
    return min(known, key=lambda level: (abs(rank.get(level, 0) - rank[wanted]), rank.get(level, 0)))


def route_for_effort(route, wanted):
    """(route, info): the route variant one conversation runs on for a requested effort.

    The effort is part of a conversation's identity: a different effort is a different route id
    (`codex/6-luna@high`), so it never resumes a session that was started with another effort.
    A level the engine does not advertise (provider.json "efforts") is clamped to the nearest one
    it does -- reported, never silent; an engine without an effort setting ignores the field."""
    supported = list((PROVIDERS.get(route.engine) or {}).get("efforts") or ())
    info = {"requested": wanted, "supported": bool(supported), "clamped": False,
            "source": "request" if wanted else "default"}
    if not wanted or not supported:
        info["applied"] = route.applied_effort if supported else None
        if wanted and not supported:
            info["note"] = "%s has no reasoning-effort setting; the requested effort was ignored" % (
                route.engine or "this engine")
        return route, info
    effort = _nearest_effort(wanted, supported)
    info["clamped"] = effort != wanted
    if info["clamped"]:
        info["note"] = "%s supports %s; %s was clamped to %s" % (
            route.engine, "/".join(supported), wanted, effort)
    base = route.variant_of or route
    if effort == base.effort:
        variant = base
    else:
        with _ROUTES_LOCK:
            variant = base.variants.get(effort)
            if variant is None:
                wanted_id = route_id(base.engine, base.model, effort)
                variant = next((r for r in _routes() if r.id == wanted_id), None)
                if variant is None:
                    variant = Route(base.engine, base.model, effort, dynamic=base.dynamic,
                                    variant_of=base)
                base.variants[effort] = variant
    info["applied"] = variant.applied_effort
    return variant, info


def _effort_routes():
    """Configured/dynamic routes followed by the effort variants requests have created."""
    routes = _all_routes()
    with _ROUTES_LOCK:
        variants = [v for r in routes for v in r.variants.values() if v.variant_of is r]
    return routes + variants


def _response_metadata(route):
    """The `neoxider` object in every completion: what actually ran, for benchmark reports."""
    info = getattr(_CTX, "effort", None) or {}
    session = getattr(_CTX, "session", None)
    base = route.variant_of or route
    return {"route": route.id, "base_route": base.id, "engine": route.engine,
            "model": route.model, "label": route.label,
            "reasoning_effort": info.get("applied", route.applied_effort if route.supports_effort else None),
            "reasoning_effort_requested": info.get("requested"),
            "reasoning_effort_source": info.get("source", "default"),
            "reasoning_effort_supported": info.get("supported", route.supports_effort),
            "reasoning_effort_clamped": info.get("clamped", False),
            "reasoning_effort_note": info.get("note"),
            "session": getattr(session, "id", None)}


# --------------------------------------------------------------------------------------------
# The session pool: independent conversations, each with its own CLI session (see THE SESSION
# MODEL in the module docstring).
DEFAULT_MAX_CONCURRENCY = 8
DEFAULT_MAX_QUEUE = 64
DEFAULT_QUEUE_TIMEOUT = 300
DEFAULT_MAX_SESSIONS = 64


class Session(dict):
    """One conversation: its remembered `messages`, CLI task (`task_name` -> its own log/meta and
    provider session id), scratch dir and, for claude, its own persistent native process.
    dict-shaped so the session-model code reads it exactly like the historic module SESSION."""

    __hash__ = object.__hash__

    def __init__(self, route):
        super().__init__(task_name=None, messages=[], dir=None, last_activity=0.0)
        self.id = uuid.uuid4().hex[:10]
        self.route = route
        self.turn_lock = threading.Lock()   # one turn of this conversation at a time
        self.state_lock = threading.Lock()  # the historic SESSION_LOCK, per conversation
        self.native = _new_native_state()
        self.created = self.last_used = time.time()
        self.busy = False
        self.closed = False
        self.turns = 0

    def __eq__(self, other):
        return self is other

    def __ne__(self, other):
        return self is not other

    def describe(self, now=None, detail=False):
        """Bookkeeping only -- never message content."""
        now = now or time.time()
        task = self.get("task_name")
        info = {"id": self.id, "model": self.route.id, "turns": self.turns,
                "messages": len(self.get("messages") or ()), "busy": self.busy,
                "resumable": bool(task), "idle_seconds": int(max(0, now - self.last_used)),
                "age_seconds": int(max(0, now - self.created))}
        if detail:
            info["task"] = task if task and task != CLAUDE_NATIVE_TASK else None
            info["native_process"] = task == CLAUDE_NATIVE_TASK
            info["cli_session"] = (read_meta(task).get("session") or None) if info["task"] else None
            info["dir"] = self.get("dir")
        return info


def _cfg_int(name, default, minimum=0):
    try:
        value = getattr(CFG, name, None)
        return max(minimum, int(default if value is None else value))
    except (TypeError, ValueError):
        return default


def _close_session(session, reason, quiet=False):
    """Release one evicted conversation: its native process and its own scratch dir."""
    session.closed = True
    try:
        _native_kill_state(session.native)
    except Exception:
        LOG.exception("could not stop the native process of session %s", session.id)
    directory = session.get("dir")
    pinned = getattr(CFG, "dir", "") or ""
    if (directory and directory != pinned
            and os.path.basename(directory).startswith("session-")
            and os.path.basename(os.path.dirname(directory)) == "neoxider-openai-bridge"):
        shutil.rmtree(directory, ignore_errors=True)
    log = LOG.debug if quiet else LOG.warning
    log("evicted session %s (model %s, %d turn(s), idle %ds): %s", session.id, session.route.id,
        session.turns, int(time.time() - session.last_used), reason)


class SessionPool:
    """Conversation sessions plus the global/per-engine concurrency slots and wait queue."""

    def __init__(self):
        self.cond = threading.Condition()
        self.sessions = {}
        self.active = 0
        self.engine_active = {}
        self.waiting = 0
        self.evicted = 0
        self.rejected = 0

    @staticmethod
    def _deadline(timeout):
        if timeout is None:
            timeout = _cfg_int("queue_timeout", DEFAULT_QUEUE_TIMEOUT)
        return time.monotonic() + max(0, timeout)

    def _expired_locked(self, now):
        ttl = _cfg_int("session_ttl", 1800)
        doomed = []
        for session in list(self.sessions.values()):
            if session.busy:
                continue
            if session.closed:
                doomed.append((session, "closed", True))
            elif ttl and now - session.last_used > ttl:
                doomed.append((session, "idle longer than --session-ttl %ds" % ttl, False))
        for session, _reason, _quiet in doomed:
            self.sessions.pop(session.id, None)
            session.closed = True
            self.evicted += 1
        return doomed

    def _capacity_locked(self, keep):
        limit = _cfg_int("max_sessions", DEFAULT_MAX_SESSIONS, 1)
        doomed = []
        while len(self.sessions) > limit:
            idle = [s for s in self.sessions.values() if not s.busy and s is not keep]
            if not idle:
                break  # every other session is active: never evict an active conversation
            victim = min(idle, key=lambda s: s.last_used)
            self.sessions.pop(victim.id, None)
            victim.closed = True
            self.evicted += 1
            doomed.append((victim, "capacity --max-sessions %d (least recently used idle session)"
                           % limit, False))
        return doomed

    @staticmethod
    def _close_all(doomed):
        for session, reason, quiet in doomed:
            _close_session(session, reason, quiet)

    def evict(self):
        """TTL eviction pass (also run by the janitor thread)."""
        with self.cond:
            doomed = self._expired_locked(time.time())
        self._close_all(doomed)
        return len(doomed)

    def checkout(self, route, messages, cancel=None, timeout=None):
        """The conversation `messages` continues (longest exact-prefix match on this route), with
        its turn lock held -- or a brand-new session. Waits for an in-flight turn of the same
        conversation; never hands one conversation's session to another."""
        deadline = self._deadline(timeout)
        excluded = set()
        while True:
            chosen = target = None
            with self.cond:
                doomed = self._expired_locked(time.time())
                candidates = [
                    s for s in self.sessions.values()
                    if not s.closed and s.id not in excluded and s.route.id == route.id
                    and s["messages"] and is_extension(s["messages"], messages)]
                candidates.sort(key=lambda s: (-len(s["messages"]), s.busy, -s.last_used))
                for session in candidates:
                    if session.turn_lock.acquire(blocking=False):
                        chosen = session
                        break
                if chosen is None and not candidates:
                    chosen = Session(route)
                    chosen.turn_lock.acquire()
                    self.sessions[chosen.id] = chosen
                    doomed += self._capacity_locked(chosen)
                if chosen is not None:
                    chosen.busy = True
                    chosen.last_used = time.time()
                else:
                    target = candidates[0]
                    self.waiting += 1
            self._close_all(doomed)
            if chosen is not None:
                return chosen
            got = False
            try:
                while not got:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    if cancel is not None and cancel.is_set():
                        raise ClientDisconnected("client disconnected while queued")
                    got = target.turn_lock.acquire(timeout=min(0.5, remaining))
            finally:
                with self.cond:
                    self.waiting -= 1
            if not got:
                with self.cond:
                    self.rejected += 1
                raise BridgeError("timed out waiting for the previous turn of this conversation "
                                  "(--queue-timeout)", status=503, code="bridge_busy",
                                  retryable=True, retry_after=5)
            with self.cond:
                still = (not target.closed and target.id in self.sessions
                         and is_extension(target["messages"], messages))
                if still:
                    target.busy = True
                    target.last_used = time.time()
                    return target
                target.turn_lock.release()
                excluded.add(target.id)

    def checkin(self, session, discard=False):
        doomed = []
        with self.cond:
            session.busy = False
            session.last_used = time.time()
            if discard or session.closed or not session["messages"]:
                # A probe, a reset-while-busy, or a first turn that never produced a transcript:
                # nothing a later request could continue, so it goes now (not a live conversation).
                if self.sessions.pop(session.id, None) is not None or not session.closed:
                    doomed.append((session, "discarded after its turn", True))
                session.closed = True
            session.turn_lock.release()
            self.cond.notify_all()
        self._close_all(doomed)

    def acquire_slot(self, route, cancel=None, timeout=None):
        """One of --max-concurrency global slots and the route engine's --engine-limit slots."""
        deadline = self._deadline(timeout)
        maximum = _cfg_int("max_concurrency", DEFAULT_MAX_CONCURRENCY)
        engine_limit = (getattr(CFG, "engine_limits", None) or {}).get(route.engine)
        engine = route.engine

        def free():
            return ((not maximum or self.active < maximum)
                    and (not engine_limit or self.engine_active.get(engine, 0) < engine_limit))

        with self.cond:
            if not free():
                if self.waiting >= _cfg_int("max_queue", DEFAULT_MAX_QUEUE):
                    self.rejected += 1
                    raise BridgeError("bridge queue is full (%d requests waiting); retry later"
                                      % self.waiting, status=503, code="bridge_busy",
                                      retryable=True, retry_after=5)
                self.waiting += 1
                try:
                    while not free():
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            self.rejected += 1
                            raise BridgeError(
                                "no free slot within --queue-timeout (max-concurrency %s, %s limit"
                                " %s); retry later" % (maximum or "unlimited", engine,
                                                        engine_limit or "none"),
                                status=503, code="bridge_busy", retryable=True, retry_after=5)
                        if cancel is not None and cancel.is_set():
                            raise ClientDisconnected("client disconnected while queued")
                        self.cond.wait(min(0.5, remaining))
                finally:
                    self.waiting -= 1
            self.active += 1
            self.engine_active[engine] = self.engine_active.get(engine, 0) + 1

    def release_slot(self, route):
        with self.cond:
            self.active = max(0, self.active - 1)
            self.engine_active[route.engine] = max(0, self.engine_active.get(route.engine, 0) - 1)
            self.cond.notify_all()

    def reset(self, route_id=None):
        """Forget every (or one route's) conversation; a busy one is dropped when its turn ends."""
        doomed = []
        with self.cond:
            for session in list(self.sessions.values()):
                if route_id and session.route.id != route_id:
                    continue
                session.closed = True
                if not session.busy:
                    self.sessions.pop(session.id, None)
                    doomed.append((session, "POST /reset", False))
        self._close_all(doomed)
        return len(doomed)

    def close_all(self):
        with self.cond:
            doomed = [(s, "bridge shutdown", True) for s in self.sessions.values()]
            self.sessions.clear()
        self._close_all(doomed)

    def list_sessions(self):
        with self.cond:
            return list(self.sessions.values())

    def snapshot(self):
        with self.cond:
            return {"active_slots": self.active, "engine_active": dict(self.engine_active),
                    "queue_depth": self.waiting, "evicted": self.evicted,
                    "rejected": self.rejected}


POOL = SessionPool()


@contextlib.contextmanager
def _conversation(route, messages, cancel=None, discard=False):
    """Bind one request to its pooled session and a concurrency slot for the CLI turn."""
    session = POOL.checkout(route, messages, cancel)
    try:
        POOL.acquire_slot(route, cancel)
        try:
            saved = (_CTX.session, _CTX.route, _CTX.cancel, _CTX.task, _CTX.last_run)
            _CTX.session, _CTX.route, _CTX.cancel = session, route, cancel
            _CTX.task = _CTX.last_run = None
            try:
                yield session
            finally:
                (_CTX.session, _CTX.route, _CTX.cancel, _CTX.task, _CTX.last_run) = saved
        finally:
            POOL.release_slot(route)
    finally:
        POOL.checkin(session, discard=discard)


class _DisconnectWatch:
    """Polls the client socket while a turn runs; a closed connection sets `cancel`, which stops
    the CLI process tree instead of letting an abandoned request burn provider time."""

    def __init__(self, handler, cancel):
        self.cancel = cancel
        self.stop_event = threading.Event()
        sock = getattr(handler, "connection", None)
        if isinstance(sock, socket.socket):
            self.sock = sock
            threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while not self.stop_event.wait(0.5):
            try:
                readable, _w, _x = select.select([self.sock], [], [], 0)
                if not readable:
                    continue
                data = self.sock.recv(1, socket.MSG_PEEK)
            except (OSError, ValueError):
                data = b""
            if data == b"":
                if not self.stop_event.is_set():
                    self.cancel.set()
            return  # closed, or the client is sending more bytes (alive): stop watching

    def stop(self):
        self.stop_event.set()


def _model_object(route, default=False):
    """An OpenAI /v1/models entry plus the label and live readiness of the route."""
    info = route.describe(default)
    return {"id": route.id, "object": "model", "created": int(STARTED_AT),
            "owned_by": "neoxider-agents", "label": info["label"], "engine": route.engine,
            "default": default, "state": info["state"], "ready": info["ready"],
            "reasoning_effort": info["applied_effort"],
            "reasoning_efforts": list((PROVIDERS.get(route.engine) or {}).get("efforts") or ())}


def health_payload():
    """GET /health: per-model readiness, per-conversation sessions (bookkeeping only, never
    content), queue depth, plus the historic single-session fields."""
    now = time.time()
    routes = _all_routes()
    default = routes[0]
    sessions = POOL.list_sessions()
    legacy = [SESSION] if (SESSION.get("task_name") or SESSION.get("messages")) else []
    every = list(sessions) + legacy
    latest = max(every, key=lambda s: s.get("last_activity") or 0, default=None)
    resumable = [s for s in every if s.get("task_name")]
    idle = min((now - (s.get("last_activity") or now) for s in resumable), default=None)
    pool = POOL.snapshot()
    with ACTIVE_LOCK:
        active_requests = ACTIVE_REQUESTS
    by_route = {}
    for session in sessions:
        by_route[session.route.id] = by_route.get(session.route.id, 0) + 1
    models = []
    for index, route in enumerate(_effort_routes()):
        info = route.describe(index == 0)
        info["sessions"] = by_route.get(route.id, 0)
        info["active"] = sum(1 for s in sessions if s.busy and s.route is route)
        models.append(info)
    return {
        "ok": True, "instance_id": INSTANCE_ID, "pid": os.getpid(),
        "engine": default.engine, "model": default.label, "default_model": default.id,
        "models": models,
        "sessions": [s.describe(now) for s in sessions],
        "session_count": len(sessions),
        "max_sessions": _cfg_int("max_sessions", DEFAULT_MAX_SESSIONS, 1),
        "session_active": bool(resumable), "session_turns": len(latest.get("messages") or ()) if latest else 0,
        "session_idle_seconds": None if idle is None else int(idle),
        "session_ttl_seconds": getattr(CFG, "session_ttl", None),
        "timeout_seconds": getattr(CFG, "timeout", None), "retries": getattr(CFG, "retries", None),
        "stall_timeout_seconds": _stall_seconds(),
        "busy": active_requests > 0, "active_requests": active_requests,
        "active_turns": pool["active_slots"], "engine_active": pool["engine_active"],
        "queue_depth": pool["queue_depth"],
        "max_concurrency": _cfg_int("max_concurrency", DEFAULT_MAX_CONCURRENCY),
        "engine_limits": dict(getattr(CFG, "engine_limits", None) or {}),
        "evicted_sessions": pool["evicted"], "rejected_requests": pool["rejected"],
        "reasoning_effort_policy": "request reasoning_effort selects the effort per conversation "
                                   "(part of its session identity); unsupported levels are clamped "
                                   "to the nearest the engine advertises; engines without effort "
                                   "support ignore it (effort_supported=false)",
        "uptime_seconds": int(now - STARTED_AT),
    }


class H(BaseHTTPRequestHandler):
    def log_message(self, fmt, *a):  # request lines only, DEBUG-gated (opt-in via AGENT_LOG_LEVEL)
        LOG.debug("%s %s", self.address_string(), fmt % a)

    def _send_json(self, code, obj, headers=None):
        b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(b)

    def _reject_unauthorized(self):
        """OpenAI-shaped 401 for a missing/wrong key. Returns True when the caller must stop."""
        if request_authorized(self.headers, self.path):
            return False
        self._send_json(401, {"error": {
            "message": "missing or invalid API key -- send 'Authorization: Bearer <key>'",
            "type": "invalid_request_error", "code": "invalid_api_key"}})
        return True

    def do_GET(self):
        if self._reject_unauthorized():
            return
        p = urllib.parse.urlparse(self.path).path
        if p == "/health":
            # WHY lock-free: a completion holds its session's lock for the whole CLI call
            # (seconds+); /health must answer instantly so a status probe (the GUI's bridge list,
            # `openai-server list`) never mistakes a busy-but-live bridge for a dead one.
            self._send_json(200, health_payload())
        elif p in ("/models", "/v1/models"):
            self._send_json(200, {"object": "list", "data": [
                _model_object(route, index == 0) for index, route in enumerate(_all_routes())]})
        elif p.startswith(("/models/", "/v1/models/")):
            wanted = urllib.parse.unquote(p.split("/models/", 1)[1])
            try:
                route = resolve_route(wanted)
            except (BridgeError, RequestValidationError):
                route = None
            if route is None or wanted.lower() not in route.keys() | route.bare_keys():
                return self._send_json(404, BridgeError(
                    "model '%s' is not served by this bridge" % wanted[:120], status=404,
                    error_type="invalid_request_error", code="model_not_found",
                    param="model").payload())
            self._send_json(200, _model_object(route, route is _routes()[0]))
        elif p in ("/sessions", "/v1/sessions"):
            now = time.time()
            self._send_json(200, {"object": "list", "data": [
                session.describe(now, detail=True) for session in POOL.list_sessions()]})
        elif p == "/":
            health = health_payload()
            self._send_json(200, {"neoxider_openai_bridge": True, "engine": health["engine"],
                                   "model": health["model"], "models": [m["id"] for m in health["models"]],
                                   "endpoint": "POST .../chat/completions", "reset_endpoint": "POST .../reset",
                                   "session_active": health["session_active"],
                                   "session_turns": health["session_turns"]})
        else:
            self._send_json(404, {"error": {"message": "not found: " + p, "type": "invalid_request_error",
                                            "code": "not_found", "param": None}})

    def _raw_completion(self, messages, tools, tool_choice="auto", on_delta=None,
                        can_retry=None, on_retry=None):
        """Implements THE SESSION MODEL (see module docstring) for the CURRENT conversation:
        continue its CLI session via `agent.sh reply` when `messages` is a deterministic
        extension of what it saw last time and that session is still healthy; otherwise fall back
        to a brand-new `agent.sh run` with the full history. Holds the conversation's own state
        lock for the whole CLI call -- other conversations are unaffected.

        With on_delta set (and the engine live-capable, and --no-live-stream not given), the CLI
        runs in streaming output mode and every new piece of answer text is forwarded to
        on_delta while it generates. can_retry() gates the invisible fallbacks (resume->fresh,
        empty->retry): once deltas have reached the client they cannot be retried silently --
        LiveStreamDied is raised instead so the caller finalizes with what was already sent.
        on_retry() (if given) resets the caller's incremental state before a permitted retry.

        A failed turn raises the mapped BridgeError (see classify_failure_outcome); only
        internally-retryable failures (empty answer, crash, network) are re-run."""
        route = _route()
        engine, model, effort = route.engine, route.model, route.effort
        live = (on_delta is not None
                and (engine in LIVE_STREAM_ENGINES or _use_opencode_native())
                and not getattr(CFG, "no_live_stream", False))
        if _use_claude_native():
            return _claude_native_raw(messages, tools, tool_choice, on_delta,
                                      can_retry, on_retry, live)
        retry_ok = can_retry or (lambda: True)
        session = _session()
        with _session_lock():
            supports_resume = bool((PROVIDERS.get(engine) or {}).get("supports_resume"))
            prev_name = session["task_name"]
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
            name = prev_name
            if supports_resume and healthy and is_extension(session["messages"], messages):
                new_turns = messages[len(session["messages"]):]
                if engine in ("codex", "opencode") and not _use_opencode_native():
                    _validated_bridge_images(new_turns)
                answer = build_prompt(new_turns, tools, tool_choice)
                _CTX.last_run = None
                if live:
                    with _bridge_images(new_turns):
                        raw_text = reply_agent_live(engine, model, effort, session["dir"],
                                                    prev_name, answer, CFG.timeout, on_delta)
                else:
                    with _bridge_images(new_turns):
                        raw_text = reply_agent(engine, model, effort, session["dir"],
                                               prev_name, answer, CFG.timeout)
                if raw_text is not None and not raw_text.strip():
                    # A resume that "succeeded" but produced an EMPTY answer is as useless to the
                    # caller as one that died -- fall back to a fresh run rather than returning "".
                    raw_text = None
                if raw_text is None:
                    # A refusal (auth, limit, model), a stall/timeout or a cancelled client would
                    # only repeat on a fresh run with the whole history -- surface it now.
                    refusal = _resume_failure(engine, prev_name)
                    if refusal is not None:
                        session["task_name"] = None
                        session["last_activity"] = time.time()
                        raise refusal
                if raw_text is None and live:
                    if not retry_ok():
                        # The resume died/errored but its deltas already reached the client --
                        # a silent fresh-run would send the answer twice.
                        session["last_activity"] = time.time()
                        raise LiveStreamDied()
                    if on_retry is not None:
                        on_retry()  # wipe the failed resume's partial state before falling back
            if raw_text is None or not raw_text.strip():
                # First call, a non-extension, an unhealthy session, OR a resume that appended
                # nothing (reply_agent returned None). Any of these -> a clean fresh run with the
                # FULL history, never a stale echo of the previous answer. Empty/transiently
                # failed fresh runs are retried (--retries, default 1): a real OpenAI endpoint
                # effectively never returns an empty 200, and a transient CLI hiccup should not
                # zero a whole benchmark scenario. Refusals and timeouts are not retried.
                if engine in ("codex", "opencode") and not _use_opencode_native():
                    _validated_bridge_images(messages)
                prompt = build_prompt(messages, tools, tool_choice)
                # Keep the directory only when this really is the same conversation carrying on
                # (the normal case for a no-resume engine, where every turn lands here anyway).
                continuing = (bool(session["task_name"]) and bool(session["dir"])
                              and not session_expired()
                              and is_extension(session["messages"], messages))
                workdir = fresh_session_dir(wipe=not continuing)
                session["dir"] = workdir
                completed = False
                failure = None
                for attempt in range(CFG.retries + 1):
                    name = new_task_name()
                    _CTX.last_run = None
                    meta = {}
                    if _use_opencode_native():
                        try:
                            raw_text = opencode_native_complete(
                                model, prompt, CFG.timeout, on_delta if live else None,
                                workdir=workdir)
                            state = "done" if raw_text.strip() else "error"
                        except Exception as _oce:
                            incident = uuid.uuid4().hex[:12]
                            LOG.warning("opencode native incident %s exception=%s",
                                        incident, type(_oce).__name__)
                            raw_text, state = "", "error"
                            meta = {"state": "error", "reason": "opencode serve request failed (%s)"
                                    % type(_oce).__name__}
                    elif live:
                        with _bridge_images(messages):
                            raw_text = run_agent_live(engine, model, effort, workdir,
                                                      prompt, name, CFG.timeout, on_delta)
                        meta = read_meta(name)
                        state = meta.get("state")
                    else:
                        with _bridge_images(messages):
                            raw_text = run_agent(engine, model, effort, workdir, prompt,
                                                 name, CFG.timeout)
                        meta = read_meta(name)
                        state = meta.get("state")
                    outcome = getattr(_CTX, "last_run", None) or {}
                    cancel = getattr(_CTX, "cancel", None)
                    if outcome.get("cancelled") or (cancel is not None and cancel.is_set()):
                        session["task_name"] = None
                        session["last_activity"] = time.time()
                        raise ClientDisconnected("client disconnected; the CLI run was cancelled")
                    if (raw_text.strip() and state not in ("error", "limited", "silent", "stopped")
                            and not outcome.get("stalled") and not outcome.get("timed_out")):
                        completed = True
                        break
                    failure = classify_failure_outcome(engine, meta, outcome, raw_text)
                    if isinstance(failure, ClientDisconnected):
                        session["task_name"] = None
                        session["last_activity"] = time.time()
                        raise failure
                    if attempt < CFG.retries and retry_ok() and failure.internal_retry:
                        print("[openai-bridge] %s on %s (state=%s), retry %d/%d"
                              % (failure.code, route.id, state, attempt + 1, CFG.retries),
                              file=sys.stderr)
                        if on_retry is not None:
                            on_retry()
                        time.sleep(2)
                    elif not raw_text.strip() and live and not retry_ok():
                        session["task_name"] = name
                        session["messages"] = messages
                        session["last_activity"] = time.time()
                        raise LiveStreamDied()
                    else:
                        break
                if not completed:
                    session["task_name"] = None
                    session["last_activity"] = time.time()
                    raise failure or CompletionFailed("provider invocation did not complete")
            session["task_name"] = name
            session["messages"] = messages
            session["last_activity"] = time.time()
        return raw_text

    def _run(self, messages, tools, tool_choice="auto", _retry_left=1, _record=True):
        # NB: called unbound in tests (H._run(object(), ...)) -- route through the class, not
        # through self, so a dummy receiver keeps working. _retry_left is threaded as an argument
        # (not a self attribute) for the same reason: a dummy object() has no __dict__.
        raw_text = H._raw_completion(self, messages, tools, tool_choice)
        if looks_like_limit_banner(raw_text):
            raise ProviderLimitError(raw_text.strip())
        declared_names = tool_names(tools)
        tool_calls, text = extract_tool_calls(raw_text, declared_names, tools,
                                              prior_call_keys(messages))
        usage_prompt = _rough_tokens(build_prompt(messages, tools, tool_choice))
        usage_completion = _rough_tokens(raw_text)
        usage = {"prompt_tokens": usage_prompt, "completion_tokens": usage_completion,
                 "total_tokens": usage_prompt + usage_completion,
                 "neoxider_estimated": True}

        # No-tool-call recovery. When the request DECLARED tools but the model emitted NONE while clearly
        # intending to act -- its prose names one of the offered tools -- it almost always described the
        # call in words (or produced a fenced block even the salvage pass could not parse) instead of
        # emitting the block. This is the single biggest source of "0 tool calls -> scenario fails" on the
        # agentic Claude CLI (it sometimes treats the text protocol as optional). Retry ONCE with an explicit
        # nudge; keep the retry only if it actually produced calls, so a legitimate prose answer that happens
        # to mention a tool name is never worsened. Gated off with AGENT_TOOLCALL_RETRY=0.
        selected_name = _selected_tool_name(tool_choice)
        forbidden_call = bool(tool_calls) and (
            tool_choice == "none"
            or (selected_name is not None and any(
                tc["function"]["name"] != selected_name for tc in tool_calls)))
        missing_required_call = _tool_choice_requires_call(tool_choice) and not tool_calls
        choice_violation = forbidden_call or missing_required_call
        resumable = True
        result_text, result_calls, result_usage = text, tool_calls, usage
        if (tools and _retry_left > 0
                and os.environ.get("AGENT_TOOLCALL_RETRY", "1") not in ("0", "false", "no", "")):
            low = (raw_text or "").lower()
            names_tool = any(n and n.lower() in low for n in tool_names(tools))
            # Also recover the common free-build case where the model NARRATES its plan instead of
            # emitting the block ("I'll build a castle. Starting with the walls...") without ever naming
            # the tool -- observed live on smaller/free models on open-ended G6-style tasks (0 tool calls).
            # Safe to broaden: the retry is only KEPT if it actually produces calls (below), so a genuine
            # no-action prose answer that stays prose on retry is returned unchanged (just one wasted call).
            intent_markers = ("i'll ", "i will ", "let me ", "let's ", "i'm going to ", "i am going to ",
                              "starting with", "first, i", "first i", "next, i", "now i", "here's the plan",
                              "i'll start", "step 1", "to build", "let me build", "let me create")
            action_intent = bool(low.strip()) and any(k in low for k in intent_markers)
            if choice_violation or (not tool_calls and (names_tool or action_intent)):
                if tool_choice == "none":
                    nudge = ("Tool use is disabled for this turn. Reply again in plain prose only, "
                             "without any tool_calls block or function-call syntax.")
                elif selected_name:
                    nudge = ("The response must call ONLY %s at least once. Reply with ONLY the "
                             "fenced ```json {\"tool_calls\":[...]} block and no prose."
                             % selected_name)
                elif tool_choice == "required":
                    nudge = ("At least one tool call is required. Reply with ONLY the fenced "
                             "```json {\"tool_calls\":[...]} block and no prose.")
                else:
                    nudge = ("You described an action but did not emit a tool call. If the task requires an "
                             "action, respond with ONLY the fenced ```json {\"tool_calls\":[...]} block from the "
                             "instructions -- no prose before or after -- and nothing else.")
                retry_messages = list(messages) + [{"role": "user", "content": nudge}]
                retry_has_calls = False
                try:
                    r_text, r_calls, r_usage = H._run(
                        self, retry_messages, tools, tool_choice,
                        _retry_left=0, _record=False)
                    retry_has_calls = bool(r_calls)
                finally:
                    # Any hidden turn changes native/CLI context, even when it succeeds. The
                    # next real request must therefore start fresh from its full visible history.
                    session = _session()
                    with _session_lock():
                        session["messages"] = list(messages)
                        session["task_name"] = None
                        claude_native_kill()
                    resumable = False
                if tool_choice == "none" and not r_calls:
                    result_text, result_calls, result_usage = r_text, r_calls, r_usage
                elif r_calls:
                    result_text, result_calls, result_usage = r_text, r_calls, r_usage

        # A constrained choice is a response contract, not a hint.  Never downgrade a provider
        # violation into an OpenAI 200 containing prose or calls to a different function.
        final_forbidden = bool(result_calls) and (
            tool_choice == "none"
            or (selected_name is not None and any(
                tc["function"]["name"] != selected_name for tc in result_calls)))
        final_missing = _tool_choice_requires_call(tool_choice) and not result_calls
        if final_forbidden or final_missing:
            session = _session()
            with _session_lock():
                session["messages"] = list(messages)
                session["task_name"] = None
                session["last_activity"] = time.time()
                claude_native_kill()
            raise CompletionFailed("%s did not satisfy tool_choice %s" % (
                _route().engine or "provider",
                json.dumps(tool_choice, ensure_ascii=False)[:120]),
                code="tool_choice_unsatisfied")

        if _record:
            _record_completion(messages, result_text, result_calls, resumable=resumable)
        return result_text, result_calls, result_usage

    def _reset_session(self, route_id=None):
        """Forget remembered conversations: all of them, or only `route_id`'s."""
        dropped = POOL.reset(route_id)
        if route_id is None:
            with SESSION_LOCK:
                claude_native_kill()
                old_dir = SESSION["dir"]
                SESSION["task_name"] = None
                SESSION["messages"] = []
                SESSION["dir"] = None
                SESSION["last_activity"] = 0.0
                if old_dir and not CFG.dir:
                    shutil.rmtree(old_dir, ignore_errors=True)
        self._send_json(200, {"ok": True, "reset": True, "sessions": dropped,
                              "model": route_id})

    def _sync_response(self, messages, tools, tool_choice="auto"):
        text, tool_calls, usage = self._run(messages, tools, tool_choice)
        message = {"role": "assistant", "content": None if tool_calls else text.strip()}
        if tool_calls:
            message["tool_calls"] = tool_calls
        route = _route()
        self._send_json(200, {
            "id": "chatcmpl-%s" % uuid.uuid4().hex,
            "object": "chat.completion",
            "created": int(time.time()),
            "model": route.label,
            "neoxider_route": route.id,
            "neoxider": _response_metadata(route),
            "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if tool_calls else "stop"}],
            "usage": usage,
        })

    def _sse_headers(self):
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
        self._sse_started = True  # a second HTTP status line is now impossible -- see do_POST

    def _abort_stream_safely(self, payload=None):
        """Close a failed SSE response explicitly; never masquerade as a successful [DONE]."""
        try:
            payload = payload or {"error": {"message": "stream terminated before completion",
                                            "type": "server_error", "code": "stream_failed",
                                            "param": None, "retryable": True}}
            wire = "event: error\ndata: %s\n\n" % json.dumps(payload, separators=(",", ":"),
                                                             ensure_ascii=False)
            self.wfile.write(wire.encode("utf-8"))
            self.wfile.flush()
        except (OSError, ValueError):
            pass

    def _stream_response(self, messages, tools, tool_choice="auto"):
        # Required/specific/disabled choices must be parsed and verified before any bytes become
        # irrevocable.  The replay path still speaks SSE, but can fail closed with a normal error.
        choice_live_safe = not tools or tool_choice == "auto"
        route = _route()
        if (choice_live_safe
                and (route.engine in LIVE_STREAM_ENGINES or _use_opencode_native())
                and not getattr(CFG, "no_live_stream", False)):
            return self._stream_response_live(messages, tools, tool_choice)
        # Legacy/emulated path (non-live engines): run to completion, then replay the finished
        # answer as word-sized SSE deltas.
        text, tool_calls, _usage = self._run(messages, tools, tool_choice)
        cid = "chatcmpl-%s" % uuid.uuid4().hex
        created = int(time.time())
        label = route.label
        self._sse_headers()

        def emit(delta, finish_reason=None):
            chunk = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": label,
                      "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}]}
            if finish_reason:
                chunk["neoxider"] = _response_metadata(route)
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
            _invalidate_resume_session(messages)

    def _stream_response_live(self, messages, tools, tool_choice="auto"):
        """REAL streaming: SSE chunks go out while the CLI generates. Headers are sent lazily
        on the first actual chunk, so a limit banner (held back, see STREAM_HOLDBACK_CHARS) can
        still surface as a clean HTTP 429 through do_POST's normal error path."""
        cid = "chatcmpl-%s" % uuid.uuid4().hex
        created = int(time.time())
        route = _route()
        label = route.label
        state = {"headers": False, "gone": False, "content": [], "calls": {}}

        def emit(delta, finish_reason=None):
            if state["gone"]:
                return
            try:
                if not state["headers"]:
                    state["headers"] = True
                    self._sse_headers()
                chunk = {"id": cid, "object": "chat.completion.chunk", "created": created,
                          "model": label,
                          "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}]}
                if not state.get("role_sent"):
                    state["role_sent"] = True
                    chunk["choices"][0]["delta"] = dict(chunk["choices"][0]["delta"])
                    chunk["choices"][0]["delta"]["role"] = "assistant"
                if finish_reason:
                    chunk["neoxider"] = _response_metadata(route)
                if isinstance(delta.get("content"), str):
                    state["content"].append(delta["content"])
                for call in delta.get("tool_calls") or []:
                    state["calls"][call["index"]] = {
                        "id": call["id"], "type": "function", "function": call["function"]}
                self.wfile.write(("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n").encode("utf-8"))
                self.wfile.flush()
            except (BrokenPipeError, ConnectionAbortedError, OSError):
                # Client went away: stop writing and cancel the CLI run instead of letting an
                # abandoned turn keep spending provider time.
                state["gone"] = True
                cancel = getattr(_CTX, "cancel", None)
                if cancel is not None:
                    cancel.set()

        emitter = LiveToolCallEmitter(
            tool_names(tools), tools, prior_call_keys(messages),
            on_content=lambda s: emit({"content": s}),
            on_call=lambda call, idx: emit({"tool_calls": [
                {"index": idx, "id": call["id"], "type": "function", "function": call["function"]}]}),
        )
        noise_filter = ProviderNoiseStreamFilter(emitter.feed)

        def reset_stream():
            noise_filter.reset()
            emitter.reset()

        raw = self._raw_completion(messages, tools, tool_choice, on_delta=noise_filter.feed,
                                   can_retry=lambda: not emitter.wire_started,
                                   on_retry=reset_stream)
        noise_filter.finish()
        if not emitter.wire_started and looks_like_limit_banner(raw):
            raise ProviderLimitError((raw or "").strip())  # -> 429, headers not sent yet
        fallback_text = emitter.finish()
        if fallback_text is not None and not emitter.any_calls:
            if fallback_text.strip():
                emit({"content": fallback_text.strip()})
            else:
                emit({})  # empty answer: still open the stream so the client gets a valid turn
        visible_calls = [state["calls"][i] for i in sorted(state["calls"])] or None
        try:
            emit({}, "tool_calls" if emitter.any_calls else "stop")
            if not state["gone"]:
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionAbortedError, OSError):
            state["gone"] = True
        if state["gone"]:
            _invalidate_resume_session(messages)
        else:
            # Only a fully delivered stream becomes resumable context.  Recording before the
            # finish chunk made a BrokenPipe preserve a truncated assistant turn as if received.
            _record_completion(messages, "".join(state["content"]), visible_calls)

    def _read_content_length(self):
        """(n, None) on success or (None, (http_code, message)); missing header -> 0,
        non-integer -> 400, over MAX_BODY_BYTES -> 413 before any byte is read."""
        raw = (self.headers.get("Content-Length") or "").strip()
        if not raw:
            return 0, None
        if not (raw.isascii() and raw.isdigit()):
            return None, (400, "malformed Content-Length header")
        n = int(raw)
        if n > MAX_BODY_BYTES:
            return None, (413, "request body too large (%d bytes; limit %d)" % (n, MAX_BODY_BYTES))
        return n, None

    def do_POST(self):
        if self._reject_unauthorized():
            return
        # Preserve the raw route shape: urlparse("//v1/reset").path is "/reset".
        p = self.path.partition("?")[0]
        if len(p) > 1 and p.endswith("/"):
            p = p[:-1]
        if p not in ("/chat/completions", "/v1/chat/completions", "/reset", "/v1/reset",
                     "/admin/shutdown"):
            return self._send_json(404, {"error": {"message": "not found: " + p,
                                                   "type": "invalid_request_error",
                                                   "code": "not_found", "param": None}})
        length, err = self._read_content_length()
        if err:
            return self._send_json(err[0], {"error": {
                "message": err[1], "type": "invalid_request_error",
                "code": "request_too_large" if err[0] == 413 else "invalid_request",
                "param": None}})
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            return self._send_json(400, {"error": {"message": "invalid JSON body",
                                                   "type": "invalid_request_error",
                                                   "code": "invalid_json", "param": None}})
        if not isinstance(body, dict):
            return self._send_json(400, {"error": {"message": "request body must be a JSON object",
                                                   "type": "invalid_request_error",
                                                   "code": "invalid_json", "param": None}})
        if p == "/admin/shutdown":
            return _admin_shutdown(self, body)
        if p in ("/reset", "/v1/reset"):
            if body.get("model") is None:
                return self._reset_session()
            try:
                return self._reset_session(resolve_route(body.get("model")).id)
            except RequestValidationError as e:
                return self._send_json(400, _parameter_error(e))
            except BridgeError as e:
                return self._send_json(e.status, e.payload())
        global ACTIVE_REQUESTS
        saved_route = getattr(_CTX, "route", None)
        saved_effort = getattr(_CTX, "effort", None)
        route = None
        try:
            try:
                route = resolve_route(body.get("model"))
                wanted = requested_effort(body)
                via_model = wanted is None and route.variant_of is not None
                route, effort_info = route_for_effort(route, wanted)
                if via_model:
                    effort_info.update(requested=route.effort, source="model")
            except RequestValidationError as e:
                return self._send_json(400, _parameter_error(e))
            except BridgeError as e:
                return self._send_json(e.status, e.payload())
            _CTX.route = route  # image/camera handling during validation depends on the engine
            _CTX.effort = effort_info
            try:
                messages, tools, tool_choice, stream = validate_chat_request(body)
            except RequestValidationError as e:
                return self._send_json(400, _parameter_error(e))
            with ACTIVE_LOCK:
                ACTIVE_REQUESTS += 1
            cancel = threading.Event()
            watch = _DisconnectWatch(self, cancel)
            try:
                with _conversation(route, messages, cancel):
                    try:
                        if stream:
                            self._stream_response(messages, tools, tool_choice)
                        else:
                            self._sync_response(messages, tools, tool_choice)
                    except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError,
                            ClientDisconnected):
                        # The caller cannot be assumed to have received this turn: its own
                        # session must not resume past it. Nothing can be written back.
                        _invalidate_resume_session(messages)
                        LOG.info("client disconnected; model=%s turn cancelled", route.id)
                        return
                route.mark_ok()
            except RequestValidationError as e:
                if getattr(self, "_sse_started", False):
                    return self._abort_stream_safely()
                return self._send_json(e.status, _parameter_error(e))
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError,
                    ClientDisconnected):
                LOG.info("client disconnected while queued; model=%s",
                         getattr(route, "id", "?"))
            except ProviderLimitError as e:
                limit = BridgeError(_error_summary(route.engine or "provider", str(e)),
                                    status=429, error_type="rate_limit_error",
                                    code="rate_limit_exceeded", retryable=True,
                                    retry_after=_retry_after_seconds(str(e)))
                incident = _log_incident(limit, route)
                route.mark_error(limit)
                if getattr(self, "_sse_started", False):
                    return self._abort_stream_safely(limit.payload(incident))  # no 2nd status line
                try:
                    self._send_json(429, limit.payload(incident), limit.headers())
                except OSError:
                    pass
            except BridgeError as e:
                incident = _log_incident(e, route)
                if e.code != "bridge_busy":
                    route.mark_error(e)
                if getattr(self, "_sse_started", False):
                    return self._abort_stream_safely(e.payload(incident))
                try:
                    if e.headers():
                        self._send_json(e.status, e.payload(incident), e.headers())
                    else:
                        self._send_json(e.status, e.payload(incident))
                except OSError:
                    pass
            except LiveStreamDied:
                # The provider failed after deltas already reached the client: nothing can be
                # retried invisibly, so the stream ends with a mapped error event (never [DONE]).
                died = CompletionFailed("%s: the provider stream ended before completion; partial "
                                        "output was already sent" % (route.engine or "provider"),
                                        code="stream_interrupted")
                incident = _log_incident(died, route)
                route.mark_error(died)
                if getattr(self, "_sse_started", False):
                    return self._abort_stream_safely(died.payload(incident))
                try:
                    self._send_json(died.status, died.payload(incident))
                except OSError:
                    pass
            except Exception as e:  # noqa: BLE001 -- a bridge bug must surface as an OpenAI-style
                # error response, not a bare connection reset the client can't distinguish from a
                # network failure. A second status line would corrupt an already-started SSE stream.
                incident = uuid.uuid4().hex[:12]
                # WHY: the type alone sent one real bug (a FileNotFoundError that was really Windows
                # refusing an over-long command line) to a dead end. The message names the failure and
                # the traceback names the line.
                LOG.warning("bridge incident %s status=500 exception=%s: %s", incident,
                            type(e).__name__, e)
                LOG.debug("bridge incident %s traceback", incident, exc_info=True)
                payload = {"error": {"message": "bridge request failed: internal %s (incident %s; "
                                                "see the bridge log)" % (type(e).__name__, incident),
                                     "type": "server_error", "code": "bridge_failure",
                                     "param": None, "retryable": True, "incident_id": incident}}
                if getattr(self, "_sse_started", False):
                    return self._abort_stream_safely(payload)
                try:
                    self._send_json(500, payload)
                except OSError:
                    pass
            finally:
                watch.stop()
                with ACTIVE_LOCK:
                    ACTIVE_REQUESTS = max(0, ACTIVE_REQUESTS - 1)
        finally:
            _CTX.route = saved_route
            _CTX.effort = saved_effort


def _admin_shutdown(handler, body):
    """POST /admin/shutdown {"instance_id": ...}: graceful stop for `openai-server stop`. Only a
    loopback caller that names this process's instance id (from the local registry) may use it;
    the API key gate (when configured) already ran."""
    client = (getattr(handler, "client_address", None) or ("",))[0]
    if not _host_is_loopback(str(client)):
        return handler._send_json(403, {"error": {
            "message": "shutdown is only accepted from this machine", "type": "permission_error",
            "code": "forbidden", "param": None}})
    if body.get("instance_id") != INSTANCE_ID:
        return handler._send_json(409, {"error": {
            "message": "instance_id does not match this bridge", "type": "invalid_request_error",
            "code": "instance_mismatch", "param": "instance_id"}})
    handler._send_json(200, {"ok": True, "shutting_down": True, "instance_id": INSTANCE_ID})
    server = getattr(handler, "server", None)
    if server is not None:
        LOG.warning("shutdown requested over HTTP by %s", client)
        threading.Thread(target=server.shutdown, daemon=True).start()


def _lan_ips():
    """Best-effort list of this host's LAN IPv4 addresses, for printing a reachable base_url when
    the bridge is bound to all interfaces. Uses a connect-less UDP socket (no packets are sent);
    falls back to resolving the hostname. Returns [] if nothing non-loopback is found."""
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


_PUBLIC_IP_CACHE = {"ip": None, "done": False}


def _public_ip():
    """Best-effort public/WAN IPv4 of this host via a short query to an external echo service,
    cached for the process lifetime. Returns None when offline or the lookup fails. NOTE: this is
    the router's internet address -- reaching the bridge through it ALSO needs a port-forward on
    the router, and exposing an agent-driving bridge to the internet is dangerous."""
    if _PUBLIC_IP_CACHE["done"]:
        return _PUBLIC_IP_CACHE["ip"]
    ip = None
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip"):
        try:
            with _urlreq.urlopen(url, timeout=2.5) as r:
                cand = r.read().decode("utf-8", "replace").strip()
            parts = cand.split(".")
            if len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
                ip = cand
                break
        except Exception:
            continue
    _PUBLIC_IP_CACHE["ip"] = ip
    _PUBLIC_IP_CACHE["done"] = True
    return ip


def _bridge_file(port):
    return os.path.join(BRIDGES_DIR, "bridge-%d.json" % int(port))


def _cfg_routes(cfg):
    routes = getattr(cfg, "routes", None)
    if routes:
        return list(routes)
    return [Route(getattr(cfg, "engine", "") or "", getattr(cfg, "model", "") or "",
                  getattr(cfg, "effort", "") or "")]


def _own_pid_stamp():
    try:
        from neoxider_agents.process import pid_stamp
        return pid_stamp(os.getpid())
    except Exception:
        return ""


def register_bridge(cfg):
    """Write this bridge's metadata so the GUI and `openai-server list/stop` can discover/stop it.
    Best-effort: a failure here must never stop the bridge from serving, so every error is
    swallowed."""
    try:
        os.makedirs(BRIDGES_DIR, exist_ok=True)
        all_ifaces = cfg.host in ("0.0.0.0", "::")
        shown_host = "127.0.0.1" if all_ifaces else cfg.host
        # when bound to all interfaces, record the LAN URLs so the GUI can show a reachable
        # address for a phone/other PC (127.0.0.1 only works on this machine).
        lan_urls = ["http://%s:%d" % (ip, cfg.port) for ip in _lan_ips()] if all_ifaces else []
        # public/WAN address too (only reachable with a router port-forward; see _public_ip note)
        public_ip = _public_ip() if all_ifaces else None
        public_url = "http://%s:%d" % (public_ip, cfg.port) if public_ip else ""
        routes = _cfg_routes(cfg)
        default = routes[0]
        rec = {
            "port": cfg.port,
            "host": cfg.host,
            "base_url": "http://%s:%d" % (shown_host, cfg.port),
            "lan_urls": lan_urls,
            "public_url": public_url,
            "engine": default.engine,
            "model": default.model,
            "effort": default.effort,
            "label": default.label,
            "models": [{"id": r.id, "label": r.label, "engine": r.engine, "model": r.model,
                        "effort": r.effort} for r in routes],
            "name": getattr(cfg, "name", "") or "",
            "dir": cfg.dir,
            "lan": all_ifaces,
            "pid": os.getpid(),
            "pid_stamp": _own_pid_stamp(),
            "parent_pid": os.getppid() if hasattr(os, "getppid") else None,
            "instance_id": INSTANCE_ID,
            "started": time.time(),
            "timeout": cfg.timeout,
            "session_ttl": cfg.session_ttl,
            "max_concurrency": getattr(cfg, "max_concurrency", None),
            "detached": os.environ.get("AGENT_BRIDGE_DETACHED") == "1",
            "log": os.environ.get("AGENT_BRIDGE_LOG", "") if os.environ.get("AGENT_BRIDGE_DETACHED") == "1" else "",
            # the KEY itself is never written here -- only whether one is required, so the GUI can
            # show an open LAN bridge as unprotected without leaking the secret into a world-
            # readable file under ~/.claude/agent-cli-logs.
            "auth": bool((getattr(cfg, "api_key", "") or "").strip()),
        }
        tmp = _bridge_file(cfg.port) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False)
        os.replace(tmp, _bridge_file(cfg.port))
    except Exception as e:
        LOG.warning("bridge registry write failed (%s)", type(e).__name__)


def unregister_bridge(port, instance_id=None):
    """Remove this bridge's registry file (only our own when instance_id is given)."""
    path = _bridge_file(port)
    if instance_id is not None:
        try:
            with open(path, encoding="utf-8") as f:
                if json.load(f).get("instance_id") != instance_id:
                    return
        except (OSError, ValueError, AttributeError):
            pass
    try:
        os.remove(path)
    except OSError as e:
        # expected on the double-exit path (file already gone) or a never-registered port
        LOG.debug("registry removal skipped (%s)", e.strerror or type(e).__name__)


def _port_arg(v):
    """argparse type for -p/--port: an integer in 0..65535 (0 = pick a free port) or a clean
    CLI error."""
    try:
        p = int(v)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("port must be an integer, got %r" % v)
    if not 0 <= p <= 65535:
        raise argparse.ArgumentTypeError("port must be an integer in 0..65535 (0 = any free port), got %r" % v)
    return p


def _host_is_loopback(host):
    value = (host or "").strip().lower()
    if value == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False  # fail closed for hostnames whose bind scope is not self-evident


def default_bind_host():
    return os.environ.get("AGENT_OPENAI_HOST") or "127.0.0.1"


def validate_network_config(cfg):
    """Return a startup refusal reason, or None when the requested bind is safe enough."""
    if getattr(cfg, "lan", False) and getattr(cfg, "localhost", False):
        return "--lan and --localhost cannot be used together"
    routes = _cfg_routes(cfg)
    for route in routes:
        if route.engine == "claude":
            try:
                resolve_claude_model(route.model, route.effort)
            except ValueError:
                return "unsupported Claude model/effort; choose values advertised by the provider"
    if _host_is_loopback(cfg.host):
        return None
    if not (getattr(cfg, "api_key", "") or "").strip():
        return "non-loopback binding requires --api-key"
    if getattr(cfg, "allow_any", False):
        return "--allow-any is loopback-only (it would let remote callers pick read-capable engines)"
    for route in routes:
        if route.engine in ("codex", "gemini"):
            return ("%s chat-only mode retains read capability and is loopback-only; "
                    "use --localhost" % route.engine)
        if route.engine == "opencode" and OPENCODE_NATIVE:
            return "unsafe native OpenCode mode is loopback-only; unset AGENT_OPENCODE_NATIVE_UNSAFE"
    return None


# --------------------------------------------------------------------------------------------
# Command line: serve (default) / start [--detach] / list / stop / check.
SUBCOMMANDS = ("start", "serve", "list", "ls", "status", "stop", "check")


def _non_negative_int(v):
    try:
        number = int(v)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("expected a non-negative integer, got %r" % v)
    if number < 0:
        raise argparse.ArgumentTypeError("expected a non-negative integer, got %r" % v)
    return number


def build_parser(prog="openai_server.py"):
    ap = argparse.ArgumentParser(
        prog=prog,
        description="OpenAI-compatible /v1/chat/completions bridge over CLI subagents. "
                    "Subcommands: [start|serve] (default), list, stop PORT|NAME|all, check.")
    ap.add_argument("-e", "--engine", default=None, choices=sorted(PROVIDERS) or None,
                    help="engine for bare -m values (default: codex)")
    ap.add_argument("-m", "--model", action="append", default=None, metavar="MODEL",
                    help="model alias passed to the CLI (one -m with -e = the historic single-route "
                         "bridge), or ENGINE/MODEL[@EFFORT]; repeat to serve several routes from one "
                         "process -- the request's `model` field picks one, the first is the default")
    ap.add_argument("--models", action="append", default=None, metavar="LIST",
                    help="comma-separated ENGINE/MODEL[@EFFORT] routes (same as repeated -m)")
    ap.add_argument("-f", "--effort", default="", help="effort level for routes without @EFFORT")
    ap.add_argument("-C", "--dir", default="",
                    help="working dir for every session (default: a scratch temp dir per "
                         "conversation, wiped when that conversation ends). Pin this to a real "
                         "project path if the agent should operate there instead.")
    ap.add_argument("-p", "--port", type=_port_arg, default=os.environ.get("AGENT_OPENAI_PORT") or 8801,
                    help="port to bind (default 8801; 0 = pick a free port and print it)")
    ap.add_argument("--host", default=default_bind_host(),
                    help="interface to bind (default: 127.0.0.1 = this machine only). "
                         "Set AGENT_OPENAI_HOST to change the "
                         "default, or pass --localhost to restrict to this machine only.")
    ap.add_argument("--lan", action="store_true",
                    help="explicitly bind all interfaces (0.0.0.0); requires an API key")
    ap.add_argument("--localhost", action="store_true",
                    help="restrict to 127.0.0.1 (this machine only). Use when you do NOT want other "
                         "devices on the network to reach the bridge.")
    ap.add_argument("--timeout", type=int, default=240, help="max seconds to wait for one completion (default: 240)")
    ap.add_argument("--retries", type=int, default=1,
                    help="how many times to re-run a completion that came back empty or with a "
                         "transient failure (network/crash); refusals and timeouts are never "
                         "re-run (default: 1)")
    ap.add_argument("--no-live-stream", action="store_true",
                    help="disable real token streaming for live-capable engines (claude); "
                         "stream:true then replays the finished answer as word-sized SSE deltas")
    ap.add_argument("--api-key", default=os.environ.get("AGENT_OPENAI_KEY") or "",
                    help="shared secret every caller must present as 'Authorization: Bearer <key>' "
                         "(or 'X-Api-Key: <key>'). Empty = open bridge (the historic behaviour, fine "
                         "on loopback). STRONGLY recommended whenever the bridge is reachable from "
                         "anything but this machine. Default: $AGENT_OPENAI_KEY.")
    ap.add_argument("--session-ttl", type=int, default=1800,
                    help="seconds of inactivity before a conversation's session is evicted and its "
                         "next call starts fresh (default: 1800 = 30 minutes)")
    ap.add_argument("--max-sessions", type=_non_negative_int, default=DEFAULT_MAX_SESSIONS,
                    help="remembered conversations; above it the least recently used IDLE one is "
                         "evicted, never an active one (default: %d)" % DEFAULT_MAX_SESSIONS)
    ap.add_argument("--max-concurrency", type=_non_negative_int, default=DEFAULT_MAX_CONCURRENCY,
                    help="CLI turns running at once across all conversations; 0 = unlimited "
                         "(default: %d; 1 restores the historic one-at-a-time bridge)"
                         % DEFAULT_MAX_CONCURRENCY)
    ap.add_argument("--engine-limit", action="append", default=None, metavar="ENGINE=N",
                    help="per-engine cap on concurrent CLI turns, e.g. claude=1 (repeatable)")
    ap.add_argument("--max-queue", type=_non_negative_int, default=DEFAULT_MAX_QUEUE,
                    help="requests allowed to wait for a slot before new ones get 503 "
                         "(default: %d)" % DEFAULT_MAX_QUEUE)
    ap.add_argument("--queue-timeout", type=_non_negative_int, default=DEFAULT_QUEUE_TIMEOUT,
                    help="seconds a request may wait for a slot / its conversation's previous turn "
                         "before 503 bridge_busy (default: %d)" % DEFAULT_QUEUE_TIMEOUT)
    ap.add_argument("--stall-timeout", type=_non_negative_int, default=180,
                    help="stream watchdog: abort a CLI turn whose provider printed nothing for N "
                         "seconds and answer 504 (default: 180; 0 = off)")
    ap.add_argument("--max-request-mb", type=_non_negative_int, default=64,
                    help="largest accepted request body in MiB (default: 64)")
    strict = ap.add_mutually_exclusive_group()
    strict.add_argument("--strict-models", dest="strict_models", action="store_const", const=True,
                        default=None, help="404 for a request model no route serves (default when "
                                           "several routes are configured)")
    strict.add_argument("--no-strict-models", dest="strict_models", action="store_const",
                        const=False, help="serve unknown request models with the default route "
                                          "(the default for a single route)")
    ap.add_argument("--allow-any", action="store_true",
                    help="also route any ENGINE/MODEL of a known engine on demand (loopback only)")
    ap.add_argument("--check", "--probe", dest="check", action="store_true",
                    help="at startup send one tiny completion per route and print ok or the mapped "
                         "error (results also appear in /health)")
    ap.add_argument("--detach", action="store_true",
                    help="(start) run in the background, log to %s/bridge-<port>.log, register, "
                         "print the port and exit" % BRIDGES_DIR.replace("\\", "/"))
    ap.add_argument("--name", default="", help="a name for `openai-server list/stop NAME`")
    ap.add_argument("--json", action="store_true", help="(start --detach / check) print JSON")
    return ap


def _engine_limits(values, ap):
    limits = {}
    for value in values or ():
        for item in value.split(","):
            engine, sep, number = item.strip().partition("=")
            if not sep or engine not in PROVIDERS or not number.strip().isdigit() or int(number) < 1:
                ap.error("--engine-limit wants ENGINE=N with a known engine and N >= 1, got %r" % item)
            limits[engine] = int(number)
    return limits


def parse_config(argv):
    """Parse and validate the serve/start/check options into a CFG (routes included)."""
    ap = build_parser()
    cfg = ap.parse_args(argv)
    cfg.retries = max(0, cfg.retries)  # a negative value would skip the run loop entirely
    if cfg.lan:
        cfg.host = "0.0.0.0"
    if cfg.localhost:
        cfg.host = "127.0.0.1"
    specs = [value.strip() for value in (cfg.model or ()) if value.strip()]
    for value in cfg.models or ():
        specs.extend(item.strip() for item in value.split(",") if item.strip())
    engine_explicit = cfg.engine is not None
    engine = cfg.engine or "codex"
    if engine not in PROVIDERS:
        ap.error("unknown engine %r" % engine)
    for spec in specs:
        if spec.startswith("-"):
            ap.error("model %r looks like an option" % spec)
    cfg.routes = build_routes(engine, specs, cfg.effort, engine_explicit)
    default = cfg.routes[0]
    cfg.engine, cfg.model, cfg.effort = default.engine, default.model, default.effort
    cfg.engine_limits = _engine_limits(cfg.engine_limit, ap)
    refusal = validate_network_config(cfg)
    if refusal:
        ap.error(refusal)
    return cfg


class _BridgeHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 128  # bursts of parallel clients must not be refused at accept()


_PROBE_PROMPT = "Reply with exactly the word OK and nothing else."


def probe_route(route):
    """One tiny completion through the normal pooled path; the probe session is discarded."""
    messages = [{"role": "user", "content": _PROBE_PROMPT}]
    started = time.monotonic()
    result = {"model": route.id, "label": route.label}
    try:
        with _conversation(route, messages, discard=True):
            text, _calls, _usage = H._run(object(), messages, None, "none")
        route.mark_ok()
        result.update(ok=True, answer=" ".join(text.split())[:60])
    except ProviderLimitError as e:
        error = BridgeError(_error_summary(route.engine, str(e)), status=429,
                            error_type="rate_limit_error", code="rate_limit_exceeded",
                            retryable=True, retry_after=_retry_after_seconds(str(e)))
        route.mark_error(error)
        result.update(ok=False, status=429, code=error.code, message=str(error))
    except BridgeError as e:
        route.mark_error(e)
        result.update(ok=False, status=e.status, code=e.code, message=str(e),
                      retryable=bool(e.retryable))
    except Exception as e:  # noqa: BLE001 -- a probe must report, never crash the bridge
        error = BridgeError("internal %s during the probe" % type(e).__name__)
        route.mark_error(error)
        result.update(ok=False, status=500, code="bridge_failure", message=str(error))
    result["seconds"] = round(time.monotonic() - started, 1)
    return result


def _format_probe(result):
    if result.get("ok"):
        return "[openai-bridge] check %s: ok in %.1fs (%r)" % (result["model"], result["seconds"],
                                                                result.get("answer", ""))
    return "[openai-bridge] check %s: HTTP %s %s after %.1fs -- %s" % (
        result["model"], result.get("status"), result.get("code"), result["seconds"],
        result.get("message", ""))


def probe_routes(routes, emit=None):
    """Probe every route concurrently (bounded by the pool) and return the results in order."""
    results = [None] * len(routes)

    def work(index, route):
        results[index] = probe_route(route)
        if emit is not None:
            emit(results[index])

    threads = [threading.Thread(target=work, args=(i, r), daemon=True) for i, r in enumerate(routes)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return results


def _install_config(cfg):
    global CFG, ROUTES, MAX_BODY_BYTES
    CFG = cfg
    ROUTES = list(cfg.routes)
    MAX_BODY_BYTES = max(1, int(getattr(cfg, "max_request_mb", 64) or 64)) * 1024 * 1024


def cmd_check(cfg):
    """`openai-server check`: probe every configured route, print, exit 0 only if all answered."""
    _install_config(cfg)
    try:
        results = probe_routes(ROUTES, emit=None if cfg.json else
                               (lambda result: print(_format_probe(result), flush=True)))
    finally:
        POOL.close_all()
        _shutdown_process_trees()
    if cfg.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0 if all(result.get("ok") for result in results) else 1


def _janitor():
    while True:
        time.sleep(30)
        try:
            POOL.evict()
        except Exception:
            LOG.exception("session eviction pass failed")


def serve(cfg):
    global INSTANCE_ID
    _install_config(cfg)
    preset = os.environ.get("AGENT_BRIDGE_INSTANCE_ID", "")
    if re.fullmatch(r"[0-9a-f]{12}", preset):
        INSTANCE_ID = preset
    try:
        srv = _BridgeHTTPServer((cfg.host, cfg.port), H)
    except OSError as e:
        print("[openai-bridge] could not bind %s:%d: %s" % (cfg.host, cfg.port, e), file=sys.stderr)
        return 1
    cfg.port = srv.server_address[1]  # --port 0 -> the port the OS picked

    if cfg.check:
        for route in ROUTES:
            route.mark_probing()  # before registering, so a detached parent never sees "unknown"
    register_bridge(cfg)
    atexit.register(unregister_bridge, cfg.port, INSTANCE_ID)
    atexit.register(_shutdown_process_trees)
    signal.signal(signal.SIGTERM, _handle_sigterm)
    if hasattr(signal, "SIGBREAK"):
        try:
            signal.signal(signal.SIGBREAK, _handle_sigterm)
        except (OSError, ValueError):
            pass

    all_ifaces = cfg.host in ("0.0.0.0", "::")
    shown_host = "127.0.0.1" if all_ifaces else cfg.host
    base = "http://%s:%d" % (shown_host, cfg.port)
    print("[openai-bridge] %s  ->  %s  (Ctrl-C to stop)" % (base, ROUTES[0].label))
    print("[openai-bridge] point an OpenAI-compatible client's base_url at %s/v1 (or %s -- both work)" % (base, base))
    print("[openai-bridge] port=%d pid=%d instance=%s" % (cfg.port, os.getpid(), INSTANCE_ID))
    if len(ROUTES) > 1 or getattr(cfg, "allow_any", False):
        print("[openai-bridge] models (request `model` field): %s%s; %s" % (
            ", ".join(r.id for r in ROUTES), " + any ENGINE/MODEL (--allow-any)" if cfg.allow_any else "",
            "unknown model -> 404" if _strict_models() else "unknown model -> default route"))
    print("[openai-bridge] concurrency: %s turn(s) at once%s, queue %d, %d remembered conversation(s), "
          "stall watchdog %s" % (
              cfg.max_concurrency or "unlimited",
              "".join(", %s<=%d" % item for item in sorted(cfg.engine_limits.items())),
              cfg.max_queue, cfg.max_sessions,
              ("%ds" % cfg.stall_timeout) if cfg.stall_timeout else "off"), flush=True)
    if all_ifaces:
        lan = _lan_ips()
        if lan:
            for ip in lan:
                print("[openai-bridge] LAN: reachable from other devices (phone/APK, another PC) at http://%s:%d/v1" % (ip, cfg.port))
        else:
            print("[openai-bridge] LAN: bound to all interfaces on port %d (could not autodetect this host's LAN IP)" % cfg.port)
        pub = _public_ip()
        if pub:
            print("[openai-bridge] PUBLIC: this host's internet IP is http://%s:%d/v1 -- only reachable from" % (pub, cfg.port))
            print("[openai-bridge]         outside your network if you add a router port-forward for TCP %d." % cfg.port)
        print("[openai-bridge] WARNING: bound to all interfaces -- this bridge drives a CLI agent with your")
        print("[openai-bridge]          credentials/tools. Only expose it on a trusted network, and open the")
        print("[openai-bridge]          port in the firewall (Windows PowerShell, as admin):")
        print("[openai-bridge]          New-NetFirewallRule -DisplayName 'agent-openai %d' -Direction Inbound -Action Allow -Protocol TCP -LocalPort %d" % (cfg.port, cfg.port))
        if _configured_api_key():
            print("[openai-bridge] AUTH: a key IS required -- callers must send 'Authorization: Bearer <key>'.")
        else:
            print("[openai-bridge] AUTH: NO KEY SET -- anyone who can reach port %d can spend your tokens." % cfg.port)
            print("[openai-bridge]       Start with --api-key <secret> (or AGENT_OPENAI_KEY=<secret>) to require one.")
    elif _configured_api_key():
        print("[openai-bridge] AUTH: a key is required ('Authorization: Bearer <key>').")
    if any(r.engine == "opencode" for r in ROUTES) and OPENCODE_NATIVE:
        print("[openai-bridge] !! AGENT_OPENCODE_NATIVE_UNSAFE=1 -- opencode's native server path is ON.")
        print("[openai-bridge] !! Those sessions keep FULL tools (bash/write/edit + your MCP servers):")
        print("[openai-bridge] !! opencode does not honour a chat-only agent over HTTP. Loopback only.")
    sys.stdout.flush()
    threading.Thread(target=_janitor, daemon=True).start()
    if cfg.check:
        threading.Thread(target=probe_routes, args=(ROUTES,),
                         kwargs={"emit": lambda r: print(_format_probe(r), flush=True)},
                         daemon=True).start()
    try:
        srv.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\n[openai-bridge] stopped")
    finally:
        POOL.close_all()
        _shutdown_process_trees()
        srv.server_close()
        unregister_bridge(cfg.port, INSTANCE_ID)
    return 0


# ---- registry tools: list / stop / start --detach ----------------------------------------
def _registry_records():
    records = []
    for path in sorted(glob.glob(os.path.join(BRIDGES_DIR, "bridge-*.json"))):
        try:
            with open(path, encoding="utf-8") as f:
                record = json.load(f)
        except (OSError, ValueError):
            continue
        port = record.get("port") if isinstance(record, dict) else None
        if isinstance(port, int) and not isinstance(port, bool) and 0 < port <= 65535:
            record["_file"] = path
            records.append(record)
    return records


def _http_json(url, method="GET", body=None, timeout=2.0, api_key=""):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    request = _urlreq.Request(url, data=data, method=method, headers=headers)
    try:
        with _urlreq.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8", "replace") or "{}")
    except _urlerr.HTTPError as error:
        try:
            return error.code, json.loads(error.read().decode("utf-8", "replace") or "{}")
        except ValueError:
            return error.code, {}
    except (OSError, ValueError):
        return None, None


def _local_base(record):
    return "http://127.0.0.1:%d" % record["port"] if record.get("lan") else (
        record.get("base_url") or "http://127.0.0.1:%d" % record["port"])


def _pid_alive_checked(record):
    """True/False when the recorded process (pid + creation stamp) is provably alive/dead."""
    pid = record.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        from neoxider_agents.process import pid_alive
        return pid_alive(pid, record.get("pid_stamp") or "")
    except Exception:
        return False


def _port_in_use(port):
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", int(port))) == 0
    except OSError:
        return False
    finally:
        probe.close()


def _bridge_status(record):
    """(status, health): live (identity proven by /health), unresponsive (process alive),
    stale (another process owns the port) or dead."""
    code, health = _http_json(_local_base(record) + "/health")
    if code == 200 and isinstance(health, dict):
        if record.get("instance_id") and health.get("instance_id") not in (None, record["instance_id"]):
            return "stale", health
        return "live", health
    if _pid_alive_checked(record):
        return "unresponsive", None
    if time.time() - float(record.get("started") or 0) < 5:
        return "starting", None
    return "dead", None


def _drop_registry(record):
    try:
        os.remove(record["_file"])
    except (OSError, KeyError):
        pass


def cmd_list(argv):
    ap = argparse.ArgumentParser(prog="openai-server list",
                                 description="List registered bridges with live health.")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    rows = []
    for record in _registry_records():
        status, health = _bridge_status(record)
        if status in ("dead", "stale"):
            _drop_registry(record)  # the process is gone; never leave ghosts behind
            continue
        models = [m.get("id") for m in (health or {}).get("models") or ()] or \
            [m.get("id") for m in record.get("models") or ()] or [record.get("label") or "?"]
        rows.append({"port": record["port"], "pid": record.get("pid"), "status": status,
                     "name": record.get("name") or "", "models": models,
                     "base_url": record.get("base_url"),
                     "sessions": (health or {}).get("session_count"),
                     "active": (health or {}).get("active_turns"),
                     "queue": (health or {}).get("queue_depth"),
                     "uptime_seconds": (health or {}).get("uptime_seconds"),
                     "detached": bool(record.get("detached")), "log": record.get("log") or "",
                     "instance_id": record.get("instance_id")})
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print("no running bridges (registry: %s)" % BRIDGES_DIR)
        return 0
    print("%-6s %-7s %-12s %-9s %-6s %-5s %-8s %-14s %s" % (
        "PORT", "PID", "STATUS", "SESSIONS", "ACTIVE", "QUEUE", "UPTIME", "NAME", "MODELS"))
    for row in rows:
        uptime = row["uptime_seconds"]
        shown = "-" if uptime is None else "%dh%02dm" % (uptime // 3600, uptime % 3600 // 60)
        print("%-6s %-7s %-12s %-9s %-6s %-5s %-8s %-14s %s" % (
            row["port"], row["pid"], row["status"], "-" if row["sessions"] is None else row["sessions"],
            "-" if row["active"] is None else row["active"], "-" if row["queue"] is None else row["queue"],
            shown, row["name"] or "-", ", ".join(row["models"])))
    return 0


def _kill_bridge_tree(record):
    """Terminate the recorded bridge and every descendant (identity checked by creation stamp)."""
    pid = record.get("pid")
    stamp = record.get("pid_stamp") or ""
    from neoxider_agents import process as core_process
    if os.name == "nt":
        return core_process.kill_tree(pid, stamp=stamp)
    try:
        os.kill(pid, signal.SIGTERM)  # its handler stops every provider tree first
    except OSError:
        return False
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and core_process.pid_alive(pid, stamp):
        time.sleep(0.1)
    if core_process.pid_alive(pid, stamp):
        return core_process._kill_posix_tree(pid, stamp)
    return True


def _stop_one(record, force=False, wait=10.0, api_key=""):
    port = record["port"]
    status, health = _bridge_status(record)
    result = {"port": port, "pid": record.get("pid"), "stopped": False}
    if status in ("dead", "stale"):
        _drop_registry(record)
        result.update(stale_registry=True, message="not running; removed its stale registry entry")
        return result
    identity = status == "live" or (bool(record.get("pid_stamp")) and status == "unresponsive")
    if not identity and not force:
        result["message"] = ("could not verify the bridge identity on port %d (no /health answer and "
                             "no recorded process stamp); retry, or pass --force" % port)
        return result
    graceful = False
    if status == "live":
        code, _payload = _http_json(_local_base(record) + "/admin/shutdown", "POST",
                                    {"instance_id": record.get("instance_id")}, timeout=5,
                                    api_key=api_key)
        graceful = code == 200
    deadline = time.monotonic() + (wait if graceful else 0)
    while time.monotonic() < deadline and _pid_alive_checked(record):
        time.sleep(0.2)
    killed = False
    if _pid_alive_checked(record) or (status == "live" and not graceful):
        killed = _kill_bridge_tree(record)
        end = time.monotonic() + 5
        while time.monotonic() < end and _pid_alive_checked(record):
            time.sleep(0.1)
    if _pid_alive_checked(record):
        result["message"] = "process %s did not exit; registry kept for a retry" % record.get("pid")
        return result
    _drop_registry(record)
    result.update(stopped=True, graceful=graceful, killed=bool(killed))
    return result


def cmd_stop(argv):
    ap = argparse.ArgumentParser(prog="openai-server stop",
                                 description="Stop registered bridges and their whole process trees.")
    ap.add_argument("targets", nargs="+", help="PORT, NAME (--name at start), instance id, or all")
    ap.add_argument("--force", action="store_true",
                    help="kill a recorded pid even when its identity cannot be verified")
    ap.add_argument("--wait", type=float, default=10.0,
                    help="seconds to wait for a graceful exit before killing the tree (default 10)")
    ap.add_argument("--api-key", default=os.environ.get("AGENT_OPENAI_KEY") or "",
                    help="key for an authenticated bridge's graceful shutdown (default $AGENT_OPENAI_KEY)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    records = _registry_records()
    selected, missing = [], []
    for target in args.targets:
        if target == "all":
            matches = records
        elif target.isdigit():
            matches = [r for r in records if r["port"] == int(target)]
        else:
            matches = [r for r in records if target in (r.get("name"), r.get("instance_id"))]
        if not matches and target != "all":
            missing.append(target)
        for record in matches:
            if record not in selected:
                selected.append(record)
    results = [_stop_one(record, args.force, args.wait, args.api_key) for record in selected]
    if args.json:
        print(json.dumps({"results": results, "missing": missing}, ensure_ascii=False, indent=2))
    else:
        for target in missing:
            print("[openai-bridge] stop %s: no registered bridge matches" % target)
        for result in results:
            if result["stopped"]:
                print("[openai-bridge] stopped port %s (pid %s, %s)" % (
                    result["port"], result["pid"], "graceful" if result.get("graceful") else "killed tree"))
            else:
                print("[openai-bridge] port %s: %s" % (result["port"], result.get("message")))
        if not selected and not missing:
            print("[openai-bridge] no running bridges")
    ok = not missing and all(r["stopped"] or r.get("stale_registry") for r in results)
    return 0 if ok else 1


def _detached_kwargs(breakaway):
    if os.name != "nt":
        return {"start_new_session": True}
    flags = (getattr(subprocess, "DETACHED_PROCESS", 0x8)
             | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200))
    if breakaway:
        flags |= getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000)
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE
    return {"creationflags": flags, "startupinfo": startup}


def _find_registry(instance):
    for record in _registry_records():
        if record.get("instance_id") == instance:
            return record
    return None


_DETACHED_CHILDREN = []


def cmd_start_detached(cfg, argv):
    """Start the same command line in the background and report where it listens."""
    instance = uuid.uuid4().hex[:12]
    os.makedirs(BRIDGES_DIR, exist_ok=True)
    log_path = os.path.join(BRIDGES_DIR, "bridge-%s.log" % (cfg.port or "auto-" + instance))
    try:
        if os.path.getsize(log_path) > 5 * 1024 * 1024:
            os.remove(log_path)
    except OSError:
        pass
    child_args = [arg for arg in argv if arg not in ("--detach", "--json")]
    command = [sys.executable, os.path.abspath(__file__)] + child_args
    env = dict(os.environ)
    env.pop("AGENT_LAUNCHER_PID", None)
    env.update(AGENT_BRIDGE_INSTANCE_ID=instance, AGENT_BRIDGE_LOG=log_path,
               AGENT_BRIDGE_DETACHED="1", AGENT_CLI_LOGS=LOGDIR, PYTHONUNBUFFERED="1",
               PYTHONIOENCODING="utf-8")
    with open(log_path, "ab") as log:
        log.write(("\n==== %s start: %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"),
                                              " ".join(child_args))).encode("utf-8"))
        log.flush()
        proc = None
        for breakaway in (True, False):
            try:
                proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log,
                                        stderr=subprocess.STDOUT, env=env, cwd=HERE,
                                        close_fds=True, **_detached_kwargs(breakaway))
                break
            except OSError:
                if not breakaway:
                    raise
    _DETACHED_CHILDREN.append(proc)  # intentionally outlives this handle; never reaped here
    deadline = time.monotonic() + 30
    record = None
    while time.monotonic() < deadline:
        record = _find_registry(instance)
        if record is not None or proc.poll() is not None:
            break
        time.sleep(0.1)
    if record is None:
        try:
            with open(log_path, encoding="utf-8", errors="replace") as f:
                tail = f.read()[-2000:]
        except OSError:
            tail = ""
        if proc.poll() is None:
            try:
                from neoxider_agents.process import kill_tree
                kill_tree(proc.pid)
            except Exception:
                proc.kill()
        print("[openai-bridge] the background bridge did not start (exit %s). Log %s:\n%s"
              % (proc.poll(), log_path, tail), file=sys.stderr)
        return 1
    base = record.get("base_url") or "http://127.0.0.1:%d" % record["port"]
    probes = []
    if cfg.check:
        wait_until = time.monotonic() + max(60, cfg.timeout * 2 + 30)
        while time.monotonic() < wait_until:
            code, health = _http_json(base + "/health")
            models = (health or {}).get("models") or []
            if code == 200 and models and all(m.get("state") != "probing" for m in models):
                probes = models
                break
            time.sleep(1)
    summary = {"ok": True, "port": record["port"], "pid": record.get("pid"),
               "base_url": base + "/v1", "log": log_path, "instance_id": instance,
               "models": [m.get("id") for m in record.get("models") or ()],
               "name": record.get("name") or ""}
    if cfg.check:
        summary["check"] = [{"model": m.get("id"), "state": m.get("state"),
                             "error": m.get("last_error")} for m in probes] or "timed out"
    if cfg.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    print("[openai-bridge] started in the background: %s/v1 (port %d, pid %s)"
          % (base, record["port"], record.get("pid")))
    print("[openai-bridge] models: %s" % ", ".join(summary["models"]))
    print("[openai-bridge] log: %s" % log_path)
    print("[openai-bridge] stop: openai-server stop %s" % (record.get("name") or record["port"]))
    for model in probes:
        error = model.get("last_error")
        if error:
            print("[openai-bridge] check %s: HTTP %s %s -- %s" % (
                model.get("id"), error.get("status"), error.get("code"), error.get("message")))
        else:
            print("[openai-bridge] check %s: %s" % (model.get("id"), model.get("state")))
    if cfg.check and not probes:
        print("[openai-bridge] check: still probing; see /health or the log")
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass
    if os.name != "nt":
        os.umask(0o077)
    command = "serve"
    if argv and argv[0] in SUBCOMMANDS:
        command = argv.pop(0)
    if command in ("list", "ls", "status"):
        return cmd_list(argv)
    if command == "stop":
        return cmd_stop(argv)
    cfg = parse_config(argv)
    if command == "check":
        return cmd_check(cfg)
    if cfg.detach:
        return cmd_start_detached(cfg, argv)
    return serve(cfg)


if __name__ == "__main__":
    sys.exit(main() or 0)
