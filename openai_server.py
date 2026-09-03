#!/usr/bin/env python3
"""OpenAI-compatible /v1/chat/completions bridge over a CLI subagent (claude/codex/kimi/opencode/gemini).

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
import argparse, atexit, contextlib, glob, ipaddress, json, logging, os, queue, re, shlex, shutil, signal, socket, subprocess, sys, tempfile, threading, time, urllib.parse, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
SK = os.path.join(HERE, "agent.sh").replace("\\", "/")
PROVIDERS_DIR = os.path.join(HERE, "providers")
LOGDIR = os.environ.get("AGENT_CLI_LOGS") or os.path.expanduser("~/.claude/agent-cli-logs")
# WHY: each running bridge drops a bridge-<port>.json here so the GUI (gui.py) can list, inspect
# and stop bridges it didn't launch itself -- the process self-registers on bind and removes its
# own file on a clean exit; the GUI prunes files whose port no longer answers /health.
BRIDGES_DIR = os.path.join(LOGDIR, "bridges")
# Random per-process identity, published in the registry file AND in GET /health so
# gui.stop_bridge only ever kills the process it recorded.
INSTANCE_ID = uuid.uuid4().hex[:12]
# Cap on one request body; larger or malformed Content-Length gets 400/413.
MAX_BODY_BYTES = 64 * 1024 * 1024

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


def _run_agent_process(args, timeout, env):
    """Run an agent.sh step with an outer deadline that cannot orphan CLI grandchildren."""
    proc = _popen_process_tree(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, encoding="utf-8", errors="replace", cwd=HERE, env=env)
    try:
        proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _terminate_process_tree(proc)
    else:
        _release_process_tree(proc)


def _chatonly_env():
    """Least-privilege env for every agent.sh subprocess launched by this bridge.

    AGENT_CHAT_ONLY=1 removes writes and MCP access through provider-specific controls. Claude and
    opencode have their tools fully disabled; Codex and Gemini retain read capability and therefore
    may only back a loopback bridge. Normal `agent.sh run`/`reply` calls leave the variable unset and
    retain the full file, shell, and MCP access needed for coding work.
    """
    env = dict(os.environ)
    env["AGENT_CHAT_ONLY"] = "1"
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
    return env


# Longest prompt still safe as a command-line argument. Windows caps a whole command line at
# 32767 characters and the spawn fails outright above it -- as WinError 206, which Python raises as
# FileNotFoundError, so the bridge answered a long conversation with an opaque 500. agent.sh takes
# anything longer through --prompt-file instead. Keep in step with AGENT_ARGV_PROMPT_MAX in agent.sh.
ARGV_PROMPT_MAX = 16000


@contextlib.contextmanager
def _prompt_argument(text):
    """Yields the extra argv for `text`, staging it in a file when it is too long to pass directly."""
    if len(text) <= ARGV_PROMPT_MAX:
        yield [text]
        return
    fd, path = tempfile.mkstemp(prefix="bridge-prompt-", suffix=".txt")
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
    args = [BASH, SK, "run", "--no-progress", "-e", engine, "-C", to_git_bash_path(workdir), "-t", name]
    if model:
        args += ["-m", model]
    if effort:
        args += ["-f", effort]
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
    args = [BASH, SK, "reply", "-e", engine]
    if model:
        args += ["-m", model]
    if effort:
        args += ["-f", effort]
    args += ["-C", to_git_bash_path(workdir), name]
    before = len(read_log(name))
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
# (see providers/<engine>/provider.sh + stream_text_filter.py) so the task log GROWS while the
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
    """Follows the task log while `proc` (the agent.sh subprocess) runs, forwarding every new
    piece of ANSWER text (everything after this run's own "---------- output ----------" marker
    line) to on_delta as it is appended. Byte-offset based with an incremental UTF-8 decoder --
    a multi-byte character split across two reads must not become mojibake."""
    import codecs
    path = _log_path(name)
    deadline = time.time() + timeout
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    pos = start_size
    pending = ""          # header text before this run's output marker
    after_marker = False
    killed = False

    def drain():
        nonlocal pos, pending, after_marker
        try:
            size = os.path.getsize(path)
        except OSError:
            return False
        if size <= pos:
            return False
        with open(path, "rb") as f:
            f.seek(pos)
            data = f.read(size - pos)
        pos = size
        # \r stripped: on Windows the provider's filter writes CRLF line endings into the log;
        # the final-answer path normalizes them via text-mode reads, so streamed deltas must
        # match or the streamed text differs from the reconciled one.
        text = decoder.decode(data).replace("\r", "")
        if not text:
            return True
        if after_marker:
            on_delta(text)
            return True
        pending += text
        idx = pending.find(OUTPUT_MARKER + "\n")
        if idx != -1:
            after_marker = True
            rest = pending[idx + len(OUTPUT_MARKER) + 1:]
            pending = ""
            if rest:
                on_delta(rest)
        return True

    while True:
        alive = proc.poll() is None
        if drain():
            continue  # keep draining back-to-back while data flows
        if not alive:
            break
        if not killed and time.time() > deadline:
            _terminate_process_tree(proc)
            killed = True
        time.sleep(0.05)


def run_agent_live(engine, model, effort, workdir, prompt, name, timeout, on_delta):
    """run_agent, but with AGENT_STREAM_TEXT=1 and a log tail forwarding answer deltas while
    the CLI generates. Returns the same final answer text run_agent would."""
    args = [BASH, SK, "run", "--no-progress", "-e", engine, "-C", to_git_bash_path(workdir), "-t", name]
    if model:
        args += ["-m", model]
    if effort:
        args += ["-f", effort]
    args.append(prompt)
    proc = _popen_process_tree(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               cwd=HERE, env=_stream_env())
    try:
        _tail_task_log(name, proc, timeout, on_delta)
    finally:
        _release_process_tree(proc)
    return last_output(read_log(name))


def reply_agent_live(engine, model, effort, workdir, name, answer, timeout, on_delta):
    """reply_agent with a live log tail. Same None contract as reply_agent: None when the reply
    appended nothing or ended in a bad state (the caller decides whether a fresh-run fallback
    is still invisible to the client or the stream must be finalized as-is)."""
    args = [BASH, SK, "reply", "-e", engine]
    if model:
        args += ["-m", model]
    if effort:
        args += ["-f", effort]
    args += ["-C", to_git_bash_path(workdir), name, answer]
    before_bytes = _log_size(name)
    before_text = len(read_log(name))
    proc = _popen_process_tree(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               cwd=HERE, env=_stream_env())
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
    """Boot one headless `opencode serve` (via the same bash the CLI path uses, so PATH/shims
    resolve identically on Windows) and cache its base URL. Reused across all requests."""
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
            [BASH, "-lc", "opencode serve --port %d --hostname 127.0.0.1" % port],
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
    return OPENCODE_NATIVE and CFG.engine == "opencode"


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


def _use_claude_native():
    return CLAUDE_NATIVE and CFG.engine == "claude"


def _claude_native_cmd():
    # Chat-only lockdown mirrors AGENT_CHAT_ONLY (no shell/file/MCP tools), --verbose is
    # REQUIRED by --print + stream-json, partial messages give live deltas for stream:true.
    cmd = ("claude -p --verbose --input-format stream-json --output-format stream-json "
           "--include-partial-messages --strict-mcp-config --disable-slash-commands "
           "--tools ''")
    resolved_model, resolved_effort, _label = resolve_claude_model(CFG.model, CFG.effort)
    cmd += " --model " + shlex.quote(resolved_model)
    if resolved_effort:
        cmd += " --effort " + shlex.quote(resolved_effort)
    return cmd


def claude_native_alive():
    with _CL["lock"]:
        p = _CL["proc"]
        return p is not None and p.poll() is None


def claude_native_kill():
    with _CL["lock"]:
        p = _CL["proc"]
        # Keep the stale state visible until its container is disposed. An ensure racing this
        # kill cannot replace it early, and registry ownership makes repeated cleanup a no-op.
        _terminate_process_tree(p)
        if _CL["proc"] is p:
            _CL["proc"] = None
            _CL["queue"] = None


def _claude_native_ensure():
    """Start (or reuse) the persistent claude process; a reader thread pumps stdout lines into
    a queue so turns can be awaited with a real timeout (blocking readline has none)."""
    with _CL["lock"]:
        current = _CL["proc"]
        if current is not None and current.poll() is None:
            return
        if current is not None:
            # poll() only describes the wrapper. Its separate group/Job can still contain provider
            # children, so release that container before clearing state and spawning a replacement.
            _terminate_process_tree(current)
        _CL["proc"] = None
        _CL["queue"] = None
        q = queue.Queue()
        proc = _popen_process_tree(
            [BASH, "-lc", _claude_native_cmd()],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace", cwd=SESSION.get("dir") or None,
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
        _CL["proc"] = proc
        _CL["queue"] = q


def claude_native_send(prompt, timeout, on_delta=None):
    """One turn against the persistent process: write a stream-json user message, read events
    until the matching "result". Forwards partial text deltas to on_delta when given. Returns
    the final answer text ("" on failure -- caller decides whether to retry after a kill)."""
    _claude_native_ensure()
    p, q = _CL["proc"], _CL["queue"]
    try:
        p.stdin.write(json.dumps(
            {"type": "user", "message": {"role": "user", "content": prompt}},
            ensure_ascii=False) + "\n")
        p.stdin.flush()
    except OSError:
        claude_native_kill()
        return ""
    deadline = time.time() + max(30, timeout)
    texts = []
    while time.time() < deadline:
        try:
            line = q.get(timeout=max(0.5, deadline - time.time()))
        except queue.Empty:
            break
        if line is None:  # process exited mid-turn
            claude_native_kill()
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
            if isinstance(final, str) and final.strip():
                return final
            return "\n".join(texts)
    # Timed out waiting for the result -- the process state is unknown; restart next call.
    claude_native_kill()
    return ""


def _claude_native_raw(messages, tools, tool_choice, on_delta, can_retry, on_retry, live):
    """The session model (see _raw_completion) implemented against the persistent process:
    an extension of the previous messages sends ONLY the new turns to the SAME process (true
    resume, no re-boot); anything else restarts the process with the full history."""
    retry_ok = can_retry or (lambda: True)
    with SESSION_LOCK:
        prev = SESSION["messages"]
        extension = (SESSION["task_name"] == CLAUDE_NATIVE_TASK
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
            SESSION["dir"] = fresh_session_dir(wipe=True)
            prompt = build_prompt(messages, tools, tool_choice)
        raw = ""
        for attempt in range(CFG.retries + 1):
            raw = claude_native_send(prompt, CFG.timeout, on_delta if live else None)
            if raw.strip():
                break
            if attempt < CFG.retries and retry_ok():
                print("[openai-bridge] claude native empty turn, retry %d/%d"
                      % (attempt + 1, CFG.retries), file=sys.stderr)
                if on_retry is not None:
                    on_retry()
                claude_native_kill()
                prompt = build_prompt(messages, tools, tool_choice)  # resume impossible after a kill
                time.sleep(1)
            else:
                if live and not raw.strip() and not retry_ok():
                    SESSION["task_name"] = None
                    SESSION["last_activity"] = time.time()
                    raise CompletionFailed("native Claude stream ended before completion")
                break
        if not raw.strip():
            SESSION["task_name"] = None
            SESSION["last_activity"] = time.time()
            raise CompletionFailed("native Claude invocation did not complete")
        SESSION["task_name"] = CLAUDE_NATIVE_TASK
        SESSION["messages"] = messages
        SESSION["last_activity"] = time.time()
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
    }.get(base)
    if resolved is None:
        raise ValueError("unsupported Claude model alias")
    resolved_effort = effort or derived_effort or ("high" if base == "sonnet" else "")
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


def _call_shaped(obj, names):
    """The object IS one tool call: the OpenAI shape ({"function": {"name": ...}}, its own
    marker), the flat shape with EXACTLY {name-or-action, arguments-alias} keys ({action,
    arguments} observed live from Opus 4.8, {name, parameters} from Haiku 4.5 -- both scored
    tools=0 or empty-args failures before). Normalized to {name, arguments} so downstream
    conversion just works. Exact-keys rules mean a data answer with extra fields survives as
    content. The name must be a KNOWN declared tool."""
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
    if isinstance(call_name, str) and call_name and names and call_name in names:
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


def _to_calls(raw_calls, declared_names=None):
    """Canonical normalization for every extracted call; optionally enforce declarations."""
    out = []
    for c in raw_calls:
        if not isinstance(c, dict):
            continue
        fn = c.get("function") if isinstance(c.get("function"), dict) else None
        name = c.get("name") or (fn or {}).get("name")
        if not isinstance(name, str) or not name:
            continue
        if declared_names is not None and name not in declared_names:
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
                calls = _to_calls(raw, names)
        # No .strip() here: matches iterate in reverse over ORIGINAL offsets, and stripping
        # leading whitespace mid-loop shifted every earlier match's span, garbling the display
        # text when more than one fence was stripped. One strip at the end is enough.
        cleaned = cleaned[:m.start()] + cleaned[m.end():]
    if calls is None and call_fences:
        calls = _to_calls([o for fence in reversed(call_fences) for o in fence], names)
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
                found = _to_calls(obj["tool_calls"], names)
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
                        found = _to_calls(salvaged, names)
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
                found = _to_calls(shaped_list, names)
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
            calls = _to_calls([{"name": n, "arguments": a} for n, a in found], names)
            for s, e in reversed(spans):          # strip the call spans from the display text
                cleaned = cleaned[:s] + cleaned[e:]
            cleaned = cleaned.strip()
    return calls, cleaned.strip()


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
        converted = _to_calls([obj], self.names)
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
        shaped = _call_shaped(obj, self.names)
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

# The one ongoing session this bridge process serves (see the module docstring's "THE SESSION
# MODEL"). SESSION_LOCK serializes every request that touches it -- by design, only one
# conversation is ever in flight against a given bridge instance at a time.
SESSION_LOCK = threading.Lock()
SESSION = {"task_name": None, "messages": [], "dir": None, "last_activity": 0.0}
REQUEST_LOCK = threading.Lock()
ACTIVE_LOCK = threading.Lock()
ACTIVE_REQUESTS = 0


class RequestValidationError(ValueError):
    def __init__(self, param, message):
        super().__init__(message)
        self.param = param


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
    return messages, tools, choice, stream


def _canonical_assistant_message(text, tool_calls):
    message = {"role": "assistant", "content": None if tool_calls else (text or "").strip()}
    if tool_calls:
        message["tool_calls"] = json.loads(json.dumps(tool_calls, ensure_ascii=False))
    return message


def _record_completion(messages, text, tool_calls, resumable=True):
    """Remember exactly the visible transcript, including the response returned to the caller."""
    canonical = list(messages) + [_canonical_assistant_message(text, tool_calls)]
    with SESSION_LOCK:
        SESSION["messages"] = canonical
        SESSION["last_activity"] = time.time()
        if not resumable:
            SESSION["task_name"] = None
            claude_native_kill()


def _invalidate_resume_session(messages):
    """A disconnected caller cannot be assumed to have received the assistant turn."""
    with SESSION_LOCK:
        SESSION["messages"] = list(messages)
        SESSION["task_name"] = None
        SESSION["last_activity"] = time.time()
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


class CompletionFailed(RuntimeError):
    """Provider invocation exhausted retries without a usable completed answer."""


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
    def log_message(self, fmt, *a):  # request lines only, DEBUG-gated (opt-in via AGENT_LOG_LEVEL)
        LOG.debug("%s %s", self.address_string(), fmt % a)

    def _send_json(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
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
        label = model_label(CFG.engine, CFG.model, CFG.effort)
        if p == "/health":
            # WHY lock-free: a completion holds SESSION_LOCK for its whole CLI call (seconds+),
            # so acquiring it here would make /health hang for the duration -- and a status probe
            # (e.g. the GUI's bridge list) would time out and wrongly treat a busy-but-live bridge
            # as dead. A momentarily inconsistent read of these simple fields is fine for /health.
            active, turns = bool(SESSION.get("task_name")), len(SESSION.get("messages") or ())
            with ACTIVE_LOCK:
                busy = ACTIVE_REQUESTS > 0
            try:
                idle = session_idle_seconds()
            except Exception:
                idle = None
            self._send_json(200, {"ok": True, "instance_id": INSTANCE_ID, "engine": CFG.engine,
                                   "model": label,
                                   "session_active": active, "session_turns": turns,
                                   "session_idle_seconds": None if idle is None else int(idle),
                                   "session_ttl_seconds": CFG.session_ttl,
                                   "timeout_seconds": CFG.timeout, "retries": CFG.retries,
                                   "busy": busy, "active_requests": ACTIVE_REQUESTS})
        elif p in ("/models", "/v1/models"):
            self._send_json(200, {"object": "list", "data": [{"id": label, "object": "model", "owned_by": "neoxider-agents"}]})
        elif p == "/":
            with SESSION_LOCK:
                active, turns = bool(SESSION["task_name"]), len(SESSION["messages"])
            self._send_json(200, {"neoxider_openai_bridge": True, "engine": CFG.engine, "model": label,
                                   "endpoint": "POST .../chat/completions", "reset_endpoint": "POST .../reset",
                                   "session_active": active, "session_turns": turns})
        else:
            self._send_json(404, {"error": {"message": "not found: " + p}})

    def _raw_completion(self, messages, tools, tool_choice="auto", on_delta=None,
                        can_retry=None, on_retry=None):
        """Implements THE SESSION MODEL (see module docstring): continue the existing CLI
        session via `agent.sh reply` when `messages` is a deterministic extension of what we
        saw last time and that session is still healthy; otherwise fall back to a brand-new
        `agent.sh run` with the full history. Holds SESSION_LOCK for the whole CLI call --
        by design, this bridge serves one conversation at a time.

        With on_delta set (and the engine live-capable, and --no-live-stream not given), the CLI
        runs in streaming output mode and every new piece of answer text is forwarded to
        on_delta while it generates. can_retry() gates the invisible fallbacks (resume->fresh,
        empty->retry): once deltas have reached the client they cannot be retried silently --
        LiveStreamDied is raised instead so the caller finalizes with what was already sent.
        on_retry() (if given) resets the caller's incremental state before a permitted retry."""
        live = (on_delta is not None
                and (CFG.engine in LIVE_STREAM_ENGINES or _use_opencode_native())
                and not getattr(CFG, "no_live_stream", False))
        if _use_claude_native():
            return _claude_native_raw(messages, tools, tool_choice, on_delta,
                                      can_retry, on_retry, live)
        retry_ok = can_retry or (lambda: True)
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
                answer = build_prompt(new_turns, tools, tool_choice)
                if live:
                    raw_text = reply_agent_live(CFG.engine, CFG.model, CFG.effort, SESSION["dir"],
                                                prev_name, answer, CFG.timeout, on_delta)
                else:
                    raw_text = reply_agent(CFG.engine, CFG.model, CFG.effort, SESSION["dir"],
                                           prev_name, answer, CFG.timeout)
                if raw_text is not None and not raw_text.strip():
                    # A resume that "succeeded" but produced an EMPTY answer is as useless to the
                    # caller as one that died -- fall back to a fresh run rather than returning "".
                    raw_text = None
                name = prev_name
                if raw_text is None and live:
                    if not retry_ok():
                        # The resume died/errored but its deltas already reached the client --
                        # a silent fresh-run would send the answer twice.
                        SESSION["last_activity"] = time.time()
                        raise LiveStreamDied()
                    if on_retry is not None:
                        on_retry()  # wipe the failed resume's partial state before falling back
            if raw_text is None or not raw_text.strip():
                # First call, a non-extension, an unhealthy session, OR a resume that appended
                # nothing (reply_agent returned None). Any of these -> a clean fresh run with the
                # FULL history, never a stale echo of the previous answer. Empty/errored fresh
                # runs are retried (--retries, default 1): a real OpenAI endpoint effectively
                # never returns an empty 200, and a transient CLI hiccup (rate-limit blip, session
                # startup race) should not zero a whole benchmark scenario.
                prompt = build_prompt(messages, tools, tool_choice)
                # Keep the directory only when this really is the same conversation carrying on
                # (the normal case for a no-resume engine, where every turn lands here anyway).
                continuing = (bool(SESSION["task_name"]) and bool(SESSION["dir"])
                              and not session_expired()
                              and is_extension(SESSION["messages"], messages))
                workdir = fresh_session_dir(wipe=not continuing)
                SESSION["dir"] = workdir
                completed = False
                for attempt in range(CFG.retries + 1):
                    name = new_task_name()
                    if _use_opencode_native():
                        try:
                            raw_text = opencode_native_complete(
                                CFG.model, prompt, CFG.timeout, on_delta if live else None,
                                workdir=workdir)
                            state = "done" if raw_text.strip() else "error"
                        except Exception as _oce:
                            incident = uuid.uuid4().hex[:12]
                            LOG.warning("opencode native incident %s exception=%s",
                                        incident, type(_oce).__name__)
                            raw_text, state = "", "error"
                    elif live:
                        raw_text = run_agent_live(CFG.engine, CFG.model, CFG.effort, workdir,
                                                  prompt, name, CFG.timeout, on_delta)
                        state = read_meta(name).get("state")
                    else:
                        raw_text = run_agent(CFG.engine, CFG.model, CFG.effort, workdir, prompt,
                                             name, CFG.timeout)
                        state = read_meta(name).get("state")
                    if raw_text.strip() and state != "error":
                        completed = True
                        break
                    if attempt < CFG.retries and retry_ok():
                        print("[openai-bridge] empty/errored completion (state=%s), retry %d/%d"
                              % (state, attempt + 1, CFG.retries), file=sys.stderr)
                        if on_retry is not None:
                            on_retry()
                        time.sleep(2)
                    elif not raw_text.strip() and live and not retry_ok():
                        SESSION["task_name"] = name
                        SESSION["messages"] = messages
                        SESSION["last_activity"] = time.time()
                        raise LiveStreamDied()
                    else:
                        break
                if not completed:
                    SESSION["task_name"] = None
                    SESSION["last_activity"] = time.time()
                    raise CompletionFailed("provider invocation did not complete")
            SESSION["task_name"] = name
            SESSION["messages"] = messages
            SESSION["last_activity"] = time.time()
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
                    with SESSION_LOCK:
                        SESSION["messages"] = list(messages)
                        SESSION["task_name"] = None
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
            with SESSION_LOCK:
                SESSION["messages"] = list(messages)
                SESSION["task_name"] = None
                SESSION["last_activity"] = time.time()
                claude_native_kill()
            raise CompletionFailed("provider did not satisfy tool_choice")

        if _record:
            _record_completion(messages, result_text, result_calls, resumable=resumable)
        return result_text, result_calls, result_usage

    def _reset_session(self):
        with SESSION_LOCK:
            claude_native_kill()
            old_dir = SESSION["dir"]
            SESSION["task_name"] = None
            SESSION["messages"] = []
            SESSION["dir"] = None
            SESSION["last_activity"] = 0.0
            if old_dir and not CFG.dir:
                shutil.rmtree(old_dir, ignore_errors=True)
        self._send_json(200, {"ok": True, "reset": True})

    def _sync_response(self, messages, tools, tool_choice="auto"):
        text, tool_calls, usage = self._run(messages, tools, tool_choice)
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

    def _abort_stream_safely(self):
        """Close a failed SSE response explicitly; never masquerade as a successful [DONE]."""
        try:
            payload = {"error": {"message": "stream terminated before completion",
                                 "type": "server_error", "code": "stream_failed"}}
            wire = "event: error\ndata: %s\n\n" % json.dumps(payload, separators=(",", ":"))
            self.wfile.write(wire.encode("utf-8"))
            self.wfile.flush()
        except (OSError, ValueError):
            pass

    def _stream_response(self, messages, tools, tool_choice="auto"):
        # Required/specific/disabled choices must be parsed and verified before any bytes become
        # irrevocable.  The replay path still speaks SSE, but can fail closed with a normal error.
        choice_live_safe = not tools or tool_choice == "auto"
        if (choice_live_safe
                and (CFG.engine in LIVE_STREAM_ENGINES or _use_opencode_native())
                and not getattr(CFG, "no_live_stream", False)):
            return self._stream_response_live(messages, tools, tool_choice)
        # Legacy/emulated path (non-live engines): run to completion, then replay the finished
        # answer as word-sized SSE deltas.
        text, tool_calls, _usage = self._run(messages, tools, tool_choice)
        cid = "chatcmpl-%s" % uuid.uuid4().hex
        created = int(time.time())
        label = model_label(CFG.engine, CFG.model, CFG.effort)
        self._sse_headers()

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
            _invalidate_resume_session(messages)

    def _stream_response_live(self, messages, tools, tool_choice="auto"):
        """REAL streaming: SSE chunks go out while the CLI generates. Headers are sent lazily
        on the first actual chunk, so a limit banner (held back, see STREAM_HOLDBACK_CHARS) can
        still surface as a clean HTTP 429 through do_POST's normal error path."""
        cid = "chatcmpl-%s" % uuid.uuid4().hex
        created = int(time.time())
        label = model_label(CFG.engine, CFG.model, CFG.effort)
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
                if isinstance(delta.get("content"), str):
                    state["content"].append(delta["content"])
                for call in delta.get("tool_calls") or []:
                    state["calls"][call["index"]] = {
                        "id": call["id"], "type": "function", "function": call["function"]}
                self.wfile.write(("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n").encode("utf-8"))
                self.wfile.flush()
            except (BrokenPipeError, ConnectionAbortedError, OSError):
                state["gone"] = True  # client went away: keep draining the CLI, stop writing

        emitter = LiveToolCallEmitter(
            tool_names(tools), tools, prior_call_keys(messages),
            on_content=lambda s: emit({"content": s}),
            on_call=lambda call, idx: emit({"tool_calls": [
                {"index": idx, "id": call["id"], "type": "function", "function": call["function"]}]}),
        )
        raw = self._raw_completion(messages, tools, tool_choice, on_delta=emitter.feed,
                                   can_retry=lambda: not emitter.wire_started,
                                   on_retry=emitter.reset)
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
        if p not in ("/chat/completions", "/v1/chat/completions", "/reset", "/v1/reset"):
            return self._send_json(404, {"error": {"message": "not found: " + p}})
        length, err = self._read_content_length()
        if err:
            return self._send_json(err[0], {"error": {"message": err[1]}})
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            return self._send_json(400, {"error": {"message": "invalid JSON body"}})
        if not isinstance(body, dict):
            return self._send_json(400, {"error": {"message": "request body must be a JSON object"}})
        if p in ("/reset", "/v1/reset"):
            return self._reset_session()
        try:
            messages, tools, tool_choice, stream = validate_chat_request(body)
        except RequestValidationError as e:
            return self._send_json(400, _parameter_error(e))
        global ACTIVE_REQUESTS
        with ACTIVE_LOCK:
            ACTIVE_REQUESTS += 1
        try:
            with REQUEST_LOCK:
                if stream:
                    self._stream_response(messages, tools, tool_choice)
                else:
                    self._sync_response(messages, tools, tool_choice)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            _invalidate_resume_session(messages)
        except ProviderLimitError:
            incident = uuid.uuid4().hex[:12]
            LOG.warning("provider limit incident %s", incident)
            if getattr(self, "_sse_started", False):
                return self._abort_stream_safely()  # 429 headers cannot follow an SSE body
            try:
                self._send_json(429, {"error": {"message": "provider quota exceeded",
                                                 "type": "rate_limit_error",
                                                 "code": "rate_limit_exceeded"}})
            except OSError:
                pass
        except Exception as e:  # noqa: BLE001 -- a bridge bug must surface as an OpenAI-style
            # error response, not a bare connection reset the client can't distinguish from a
            # network failure. A second status line would corrupt an already-started SSE stream.
            incident = uuid.uuid4().hex[:12]
            # WHY: the type alone sent one real bug (a FileNotFoundError that was really Windows
            # refusing an over-long command line) to a dead end. The message names the failure and
            # the traceback names the line.
            LOG.warning("bridge incident %s exception=%s: %s", incident, type(e).__name__, e)
            LOG.debug("bridge incident %s traceback", incident, exc_info=True)
            if getattr(self, "_sse_started", False):
                return self._abort_stream_safely()
            try:
                self._send_json(500, {"error": {"message": "bridge request failed",
                                                 "type": "server_error",
                                                 "code": "bridge_failure",
                                                 "incident_id": incident}})
            except OSError:
                pass
        finally:
            with ACTIVE_LOCK:
                ACTIVE_REQUESTS = max(0, ACTIVE_REQUESTS - 1)


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


def register_bridge(cfg):
    """Write this bridge's metadata so the GUI can discover/stop it. Best-effort: a failure
    here must never stop the bridge from serving, so every error is swallowed."""
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
        rec = {
            "port": cfg.port,
            "host": cfg.host,
            "base_url": "http://%s:%d" % (shown_host, cfg.port),
            "lan_urls": lan_urls,
            "public_url": public_url,
            "engine": cfg.engine,
            "model": cfg.model,
            "effort": cfg.effort,
            "label": model_label(cfg.engine, cfg.model, cfg.effort),
            "dir": cfg.dir,
            "lan": all_ifaces,
            "pid": os.getpid(),
            "instance_id": INSTANCE_ID,
            "started": time.time(),
            "timeout": cfg.timeout,
            "session_ttl": cfg.session_ttl,
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


def unregister_bridge(port):
    try:
        os.remove(_bridge_file(port))
    except OSError as e:
        # expected on the double-exit path (file already gone) or a never-registered port
        LOG.debug("registry removal skipped (%s)", e.strerror or type(e).__name__)


def _port_arg(v):
    """argparse type for -p/--port: an integer in 1..65535 or a clean CLI error."""
    try:
        p = int(v)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("port must be an integer, got %r" % v)
    if not 1 <= p <= 65535:
        raise argparse.ArgumentTypeError("port must be an integer in 1..65535, got %r" % v)
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
    if cfg.engine == "claude":
        try:
            resolve_claude_model(cfg.model, cfg.effort)
        except ValueError:
            return "unsupported Claude model/effort; choose values advertised by the provider"
    if _host_is_loopback(cfg.host):
        return None
    if not (getattr(cfg, "api_key", "") or "").strip():
        return "non-loopback binding requires --api-key"
    if cfg.engine in ("codex", "gemini"):
        return ("%s chat-only mode retains read capability and is loopback-only; "
                "use --localhost" % cfg.engine)
    if cfg.engine == "opencode" and OPENCODE_NATIVE:
        return "unsafe native OpenCode mode is loopback-only; unset AGENT_OPENCODE_NATIVE_UNSAFE"
    return None


def main():
    global CFG
    if os.name != "nt":
        os.umask(0o077)
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
    ap.add_argument("-p", "--port", type=_port_arg, default=os.environ.get("AGENT_OPENAI_PORT") or 8801)
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
                     help="how many times to re-run a completion whose CLI invocation came back "
                          "empty or in an error state before giving up (default: 1)")
    ap.add_argument("--no-live-stream", action="store_true",
                     help="disable real token streaming for live-capable engines (claude); "
                          "stream:true then replays the finished answer as word-sized SSE deltas")
    ap.add_argument("--api-key", default=os.environ.get("AGENT_OPENAI_KEY") or "",
                     help="shared secret every caller must present as 'Authorization: Bearer <key>' "
                          "(or 'X-Api-Key: <key>'). Empty = open bridge (the historic behaviour, fine "
                          "on loopback). STRONGLY recommended whenever the bridge is reachable from "
                          "anything but this machine. Default: $AGENT_OPENAI_KEY.")
    ap.add_argument("--session-ttl", type=int, default=1800,
                     help="seconds of inactivity before the ongoing session is treated as expired and the "
                          "next call starts fresh instead of resuming it (default: 1800 = 30 minutes)")
    CFG = ap.parse_args()
    CFG.retries = max(0, CFG.retries)  # a negative value would skip the run loop entirely
    if getattr(CFG, "lan", False):
        CFG.host = "0.0.0.0"
    if getattr(CFG, "localhost", False):
        CFG.host = "127.0.0.1"
    refusal = validate_network_config(CFG)
    if refusal:
        ap.error(refusal)

    try:
        srv = ThreadingHTTPServer((CFG.host, CFG.port), H)
    except OSError as e:
        print("[openai-bridge] could not bind %s:%d: %s" % (CFG.host, CFG.port, e), file=sys.stderr)
        sys.exit(1)

    register_bridge(CFG)
    atexit.register(unregister_bridge, CFG.port)
    atexit.register(_shutdown_process_trees)
    signal.signal(signal.SIGTERM, _handle_sigterm)

    label = model_label(CFG.engine, CFG.model, CFG.effort)
    all_ifaces = CFG.host in ("0.0.0.0", "::")
    shown_host = "127.0.0.1" if all_ifaces else CFG.host
    base = "http://%s:%d" % (shown_host, CFG.port)
    print("[openai-bridge] %s  ->  %s  (Ctrl-C to stop)" % (base, label))
    print("[openai-bridge] point an OpenAI-compatible client's base_url at %s/v1 (or %s -- both work)" % (base, base))
    if all_ifaces:
        lan = _lan_ips()
        if lan:
            for ip in lan:
                print("[openai-bridge] LAN: reachable from other devices (phone/APK, another PC) at http://%s:%d/v1" % (ip, CFG.port))
        else:
            print("[openai-bridge] LAN: bound to all interfaces on port %d (could not autodetect this host's LAN IP)" % CFG.port)
        pub = _public_ip()
        if pub:
            print("[openai-bridge] PUBLIC: this host's internet IP is http://%s:%d/v1 -- only reachable from" % (pub, CFG.port))
            print("[openai-bridge]         outside your network if you add a router port-forward for TCP %d." % CFG.port)
        print("[openai-bridge] WARNING: bound to all interfaces -- this bridge drives a CLI agent with your")
        print("[openai-bridge]          credentials/tools. Only expose it on a trusted network, and open the")
        print("[openai-bridge]          port in the firewall (Windows PowerShell, as admin):")
        print("[openai-bridge]          New-NetFirewallRule -DisplayName 'agent-openai %d' -Direction Inbound -Action Allow -Protocol TCP -LocalPort %d" % (CFG.port, CFG.port))
        if _configured_api_key():
            print("[openai-bridge] AUTH: a key IS required -- callers must send 'Authorization: Bearer <key>'.")
        else:
            print("[openai-bridge] AUTH: NO KEY SET -- anyone who can reach port %d can spend your tokens." % CFG.port)
            print("[openai-bridge]       Start with --api-key <secret> (or AGENT_OPENAI_KEY=<secret>) to require one.")
    elif _configured_api_key():
        print("[openai-bridge] AUTH: a key is required ('Authorization: Bearer <key>').")
    if _use_opencode_native():
        print("[openai-bridge] !! AGENT_OPENCODE_NATIVE_UNSAFE=1 -- opencode's native server path is ON.")
        print("[openai-bridge] !! Those sessions keep FULL tools (bash/write/edit + your MCP servers):")
        print("[openai-bridge] !! opencode does not honour a chat-only agent over HTTP. Loopback only.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[openai-bridge] stopped")
    finally:
        _shutdown_process_trees()
        srv.server_close()


if __name__ == "__main__":
    main()
