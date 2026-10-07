"""Foreground task ownership, durable follow-ups, stop and restart."""
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path
from threading import Event
from .state import Lock, MARK, atomic_write, last_output, native_path, valid_name
from .reporting import STOP_EXIT, begin_snapshot, changed_files, now, record_stop, render_md, stop_block
from .runtime import Turn, number_env, retry_wait

DEFAULT_RESTART = ("The previous turn was interrupted. First check the working tree (git status/diff) "
                   "for partial edits, then continue the assignment from where you stopped; do not redo finished work.")
TERSE = ("\n\n[Style] Work token-efficiently: keep output, explanations, and reasoning narration minimal; "
         "do not restate the task or your plan back at length; give only what is needed. "
         "Do not re-read files you have already seen or explore beyond the task. "
         "If something is ambiguous, make a reasonable assumption and note it in one line rather than stopping "
         "— ask only if you are truly blocked and cannot proceed safely.")


def progress(name, resume=False):
    if resume:
        return "\n\n[Progress] Keep PROGRESS.%s.md current as you continue (summary, checklist, log, conclusions)." % name
    return ("\n\n[Progress protocol] Maintain a Markdown checkpoint file named EXACTLY PROGRESS.%s.md "
            "in the working directory — one file PER TASK, never a shared PROGRESS.md. "
            "If it already exists, READ IT FIRST and continue where it left off — do not redo finished steps. "
            "Sections: 1. Summary (TL;DR) — goal, status, conclusion. 2. Checklist — [x]/[ ]/[~]. "
            "3. Log — meaningful steps, outcomes, findings. 4. Conclusions / next steps. "
            "Update BEFORE and AFTER each significant step. Keep it concise. Do NOT git commit.") % name


def looks_waiting(answer):
    for line in [line.strip() for line in answer.splitlines() if line.strip()][-6:]:
        line = re.sub(r"\s+\([^)]*\)$", "", line).rstrip(" \t\"')]}\r")
        if line.endswith("?") and any(c.isalnum() for c in line[:-1]):
            return True
        if re.search(r"should i |shall i |do you want|please (confirm|clarify|specify)|уточни|подтверд|как (мне )?поступ", line, re.I):
            return True
    return False


def owner_alive(meta):
    from .process import pid_alive
    return pid_alive(meta.get("winpid") or meta.get("pid"), meta.get("pid_start", "") if meta.get("core_version") else "")


def effective(store, name, meta=None, reconcile=True):
    from .process import pid_alive
    data = meta if meta is not None else store.read(name)
    state = data.get("state", "stalled")
    if state != "running":
        return state
    if not owner_alive(data):
        if pid_alive(data.get("provider_pid"), data.get("provider_start", "")):
            return "orphaned"
        if data.get("core_version") and reconcile:
            record_stop(store, name, by="user", reason="launcher stopped")
            return "stopped"
        return "stalled"
    try:
        idle = time.time() - store.path(name, ".log").stat().st_mtime
    except OSError:
        idle = 0
    return "idle" if idle > number_env("AGENT_STALE_SEC", 300) else "running"


def header(store, name, kind, prompt, detail):
    path = store.path(name, ".log")
    fd = os.open(str(path), os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as log:
        log.write("\n========== [%s] %s | %s ==========\n> %s:\n%s\n%s\n" % (
            kind, now(), detail, "PROMPT" if kind == "run" else "ANSWER",
            prompt.replace("\n" + MARK + "\n", "\n" + MARK + " \n").replace(MARK, MARK + " "), MARK))


def select_options(store, name, opts):
    meta = store.read(name)
    result = dict(opts)
    for key in ("engine", "model", "effort", "dir"):
        if key not in result:
            result[key] = (meta.get("resolved_model") if key == "model" else "") or meta.get(key) or {"engine": "claude", "model": "", "effort": "", "dir": os.getcwd()}[key]
    result["dir"] = native_path(result["dir"])
    if not Path(result["dir"]).is_dir():
        raise ValueError("working directory does not exist: " + result["dir"])
    return result


def preflight(store, name, opts, resume):
    from .providers import get_provider
    resolved = select_options(store, name, opts)
    provider = get_provider(resolved["engine"])
    model, effort = provider.resolve(resolved["model"], resolved["effort"])
    session = store.session(name) if resume else ""
    provider.command(model, effort, resolved["dir"], session=session, chat_only=os.environ.get("AGENT_CHAT_ONLY") == "1")
    if resume:
        if not provider.supports_resume:
            raise ValueError("engine '%s' cannot resume (supports_resume=false); start fresh with run or restart --fresh" % resolved["engine"])
        if not session:
            raise ValueError("could not find a session id (task '%s'); start fresh" % name)
        provider.prepare_resume(session, resolved["dir"])
    return resolved, provider, model, effort, session


def stop(store, name, by="orchestrator", reason="stopped by orchestrator"):
    from .process import kill_tree, signal_task
    valid_name(name)
    meta = store.read(name)
    if not meta:
        raise ValueError("stop: no such task '%s'" % name)
    state = effective(store, name, meta)
    if state not in ("running", "idle", "orphaned", "stalled"):
        if state == "stopped":
            print(stop_block(store, name), end="")
        else:
            print("[agent.sh] %s already %s; OK" % (name, state))
        return 0
    if meta.get("core_version") and owner_alive(meta):
        with Lock(store.path(name, ".inbox")):
            atomic_write(store.path(name, ".control"), json.dumps(dict(action="stop", by=by, reason=reason, generation=meta.get("generation"))))
            signal_task(meta)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            data = store.read(name)
            if data.get("state") != "running":
                print(stop_block(store, name, data), end="")
                return 0
            Event().wait(0.1)
    if meta.get("provider_pid"):
        kill_tree(meta["provider_pid"], stamp=meta.get("provider_start", ""), job_name=meta.get("job_name", ""))
    elif meta.get("pid"):
        kill_tree(meta["pid"], winpid=meta.get("winpid", ""), stamp="" if not meta.get("core_version") else meta.get("pid_start", ""))
    print(record_stop(store, name, by, reason), end="")
    render_md(store, name)
    return 0


def send(store, ref, text, opts):
    name = store.resolve(ref)
    resolved = select_options(store, name, opts)
    from .providers import get_provider
    provider = get_provider(resolved["engine"])
    if not provider.supports_resume:
        raise ValueError("engine '%s' cannot resume (supports_resume=false); start fresh" % resolved["engine"])
    if not store.read(name):
        store.update(name, engine=resolved["engine"], dir=resolved["dir"], session=ref)
    with Lock(store.path(name, ".inbox")):
        state = effective(store, name)
        if state in ("running", "idle"):
            if opts.get("--flush"):
                print("[agent.sh] %s running; inbox will drain after turn" % name)
                return 0
            if opts.get("--now") and not store.session(name):
                raise ValueError("session id is not available yet; use send without --now to queue while the current turn starts")
            sequence = store.enqueue_locked(name, text)
            if opts.get("--now"):
                from .process import signal_task
                meta = store.read(name)
                if meta.get("core_version"):
                    atomic_write(store.path(name, ".control"), json.dumps(dict(action="interrupt", generation=meta.get("generation"), by="orchestrator", reason="send --now")))
                    signal_task(meta)
                else:
                    # Legacy owners cannot preserve their launcher after interruption.
                    raise ValueError("send --now requires a Python owner; stop then send --flush for a legacy task")
            print("[agent.sh] queued (#%s) task=%s" % (sequence, name))
            return 0
    resolved, provider, model, effort, session = preflight(store, name, opts, True)
    with Lock(store.path(name, ".owner"), timeout=0):
        with Lock(store.path(name, ".inbox")):
            if not opts.get("--flush"):
                store.enqueue_locked(name, text)
            files, prompt = store.batch_locked(name)
        if not files:
            print("[agent.sh] inbox empty: " + name)
            return 0
        return execute(store, name, resolved, provider, model, effort, session, prompt, True, files)


def run(store, name, prompt, opts, fresh=False):
    valid_name(name)
    defaults = dict(engine="claude", model="", effort="", dir=os.getcwd())
    defaults.update(opts)
    resolved, provider, model, effort, _ = preflight(store, name, defaults, False)
    if not fresh and effective(store, name) in ("running", "idle", "orphaned"):
        raise ValueError("run: task '%s' is already running; use send" % name)
    with Lock(store.path(name, ".owner"), timeout=0):
        if not fresh:
            atomic_write(store.path(name, ".log"), "")
        original = store.path(name, ".original.prompt")
        atomic_write(original, prompt)
        store.update(name, original_prompt=original.as_posix(), original_engine=resolved["engine"],
                     original_model=model, original_effort=effort, original_dir=resolved["dir"])
        return execute(store, name, resolved, provider, model, effort, "", prompt, False, [])


def execute(store, name, opts, provider, model, effort, session, prompt, resume, delivered):
    from .process import pid_stamp
    from .providers import failure_reason
    started_epoch = time.time()
    generation = uuid.uuid4().hex
    old = store.read(name)
    offset = store.path(name, ".log").stat().st_size if store.path(name, ".log").exists() else 0
    store.update(name, core_version="2", generation=generation, state="running", exit="", reason="",
                 engine=provider.engine, model=model + ("-" + effort if effort else ""), resolved_model=model, effort=effort, dir=opts["dir"],
                 pid=os.getpid(), winpid=os.getpid() if os.name == "nt" else "", pid_start=pid_stamp(os.getpid()),
                 session=session, session_log_offset=offset if not resume else old.get("session_log_offset", "0"),
                 detached=int(os.environ.get("AGENT_DETACHED") == "1"), wait_pid="", wait_start="", wait_token="",
                 started=now(), started_epoch=started_epoch, parent=opts.get("parent", os.environ.get("AGENT_PARENT", old.get("parent", ""))),
                 orchestrator=os.environ.get("AGENT_ORCHESTRATOR_ID", old.get("orchestrator", "")), last_send_error="", timeout="", silence="")
    begin_snapshot(store, name, opts["dir"])
    try:
        store.path(name, ".stop").unlink()
    except OSError:
        pass
    turns = retries = 0
    notes = []
    while True:
        augmented = prompt
        if not opts.get("--no-progress"):
            augmented += progress(name, resume)
        if not resume and not (opts.get("--no-terse") or opts.get("--verbose")):
            augmented += TERSE
        header(store, name, "reply" if resume else "run", augmented, "task=%s engine=%s session=%s" % (name, provider.engine, session))
        turn = Turn(store, name, provider, model, effort, opts["dir"], session, augmented, opts.get("terminal", False))
        try:
            code, output_filter, action, reason, by = turn.run()
        except (OSError, RuntimeError, ValueError) as error:
            code, output_filter, action, reason, by = 3, None, "failure", str(error), "watchdog"
        session = (output_filter.session if output_filter else "") or store.session(name)
        if session:
            store.update(name, session=session)
        answer = output_filter.last_assistant if output_filter else ""
        answer = answer or last_output(store.path(name, ".log"))
        retry_output = "".join(output_filter.raw) if output_filter else answer
        if action == "stop":
            state = "error" if code == 124 else "silent" if code == 125 else "stopped"
            code = code if code in (124, 125) else STOP_EXIT
            block = record_stop(store, name, by or "orchestrator", reason, code, state, answer, turn.last_activity)
            if code == 124:
                store.update(name, timeout=number_env("AGENT_TIMEOUT_SEC", 1800))
            if code == 125:
                store.update(name, silence=number_env("AGENT_SILENCE_SEC", 600))
            print("".join(notes) + block, end="", flush=True)
            render_md(store, name)
            return code
        if action == "interrupt":
            notes.append("↻ INTERRUPTED+RESUMED task=%s session=%s\n" % (name, session))
            code = 0
            delivered = []
        elif code and provider.retry_reason(retry_output, code, session) and retries < min(provider.retry_limit, number_env("AGENT_RETRIES", provider.retry_limit)):
            retries += 1
            with store.path(name, ".log").open("a", encoding="utf-8") as log:
                log.write("[agent.sh] RETRY %s after %s\n" % (retries, provider.retry_reason(retry_output, code, session)))
            if not retry_wait(turn, number_env("AGENT_RETRY_DELAY", provider.retry_delay(retries))):
                block = record_stop(store, name, turn.by or "orchestrator", turn.reason, STOP_EXIT, "stopped", answer, turn.last_activity)
                print("".join(notes) + block, end="", flush=True)
                render_md(store, name)
                return STOP_EXIT
            resume = bool(session)
            if resume:
                provider.prepare_resume(session, opts["dir"])
            continue
        elif (action == "failure" and code == 126) or (output_filter and output_filter.final_failure(code)):
            reason = reason or (output_filter.final_failure(code) if output_filter else failure_reason(answer))
            block = record_stop(store, name, by or "watchdog", reason, 126, "limited", answer, turn.last_activity)
            print("".join(notes) + block, end="", flush=True)
            render_md(store, name)
            return 126
        with Lock(store.path(name, ".inbox")):
            if code == 0 and action != "interrupt":
                for path in delivered:
                    path.unlink()
            files, batch = store.batch_locked(name)
            if files and code == 0 and turns < number_env("AGENT_INBOX_MAX_TURNS", 32) and session and provider.supports_resume:
                delivered, prompt, resume = files, batch, True
                turns += 1
                continue
            if code == 0 and not answer.strip():
                code = 3
                reason = "provider returned an empty answer; inspect the working tree (changes may have landed)"
            names = changed_files(store, name, opts["dir"])
            state = "error" if code else "waiting" if files or looks_waiting(answer) else "done"
            store.update(name, state=state, exit=code, reason=" ".join((reason or ("provider exited %s" % code if code else "%s undelivered message(s); send --flush %s" % (len(files), name) if files else "")).split()),
                         files=len(names), changed_names=" | ".join(names))
        print("".join(notes), end="")
        if session:
            print("session id: " + session)
        print(MARK)
        if output_filter and output_filter.answer_path:
            for chunk in output_filter.iter_answer():
                print(chunk, end="")
            if not answer.endswith("\n"):
                print()
            sys.stdout.flush()
        else:
            print(answer, end="" if answer.endswith("\n") else "\n", flush=True)
        print("[agent.sh] %s task=%s exit=%s files=%s" % (state, name, code, len(names)), file=sys.stderr)
        render_md(store, name)
        return code


def restart(store, ref, text, opts):
    name = store.resolve(ref)
    meta = store.read(name)
    if opts.get("--fresh"):
        if text:
            raise ValueError("--fresh replays the original prompt; omit TEXT")
        path = Path(meta.get("original_prompt", ""))
        if not path.is_file():
            raise ValueError("original prompt unavailable for this legacy task")
        original = path.read_text(encoding="utf-8")
        fresh_opts = dict(opts)
        fresh_opts.update({key: meta.get("original_" + key, "") for key in ("engine", "model", "effort", "dir")})
        preflight(store, name, fresh_opts, False)
        stop(store, name)
        store.update(name, previous_session=store.session(name))
        return run(store, name, original, fresh_opts, fresh=True)
    resolved, provider, _, _, _ = preflight(store, name, opts, False)
    if not provider.supports_resume or not store.session(name):
        raise ValueError("restart requires a resumable session; use restart --fresh")
    stop(store, name)
    return send(store, name, text or DEFAULT_RESTART, opts)
