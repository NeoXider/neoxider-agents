"""Bounded, allowlisted model discovery and explicit tracked OpenCode probes."""
import contextlib
import io
import json
import math
import os
import re
import tempfile
import time
from threading import Event

from .providers import ROOT, executable, get_provider

MAX_BYTES = 4 * 1024 * 1024
ID = re.compile(r"^[A-Za-z0-9._:-]+/[A-Za-z0-9._:/-]+$")
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def bounded_capture(argv, timeout=20):
    from .process import spawn
    from .providers import close_diagnostic_tree, drain_diagnostic_pipe
    data, sleeper = bytearray(), Event()
    deadline = time.monotonic() + timeout
    tree = spawn(argv, os.devnull)
    problem = ""
    try:
        while True:
            problem = drain_diagnostic_pipe(tree.process.stdout, data, MAX_BYTES, deadline)
            if problem or tree.process.poll() is not None:
                break
            sleeper.wait(min(0.02, max(0, deadline - time.monotonic())))
        code = tree.process.returncode
        if not problem:
            problem = drain_diagnostic_pipe(tree.process.stdout, data, MAX_BYTES, deadline)
    finally:
        cleanup = close_diagnostic_tree(tree)
    if "deadline" in problem:
        raise TimeoutError("model catalogue timed out")
    if problem or cleanup:
        raise RuntimeError(problem or cleanup)
    if code:
        # CLI diagnostics may contain credentials; never return their raw bytes.
        raise RuntimeError("model catalogue CLI failed (exit %s)" % code)
    return data.decode("utf-8-sig", "replace")


def _numbers(value):
    if not isinstance(value, dict):
        return {}
    return {key: number for key, number in value.items()
            if key in ("input", "output", "context") and isinstance(number, (int, float))
            and not isinstance(number, bool) and 0 <= number <= 10 ** 15 and math.isfinite(number)}


def _flags(value):
    return {key: flag for key, flag in value.items() if isinstance(flag, bool)} if isinstance(value, dict) else {}


def normalize(model_id, record):
    caps = record.get("capabilities", {})
    caps = caps if isinstance(caps, dict) else {}
    variants = record.get("variants")
    modes = sorted(key for key in variants if re.fullmatch(r"[A-Za-z0-9._-]+", key)) if isinstance(variants, dict) else []
    cost = _numbers(record.get("cost"))
    return {"id": model_id, "name": str(record.get("name") or model_id)[:160],
            "provider": model_id.split("/", 1)[0], "source": "cli-advertised",
            "capabilities": {key: caps[key] for key in ("reasoning", "toolcall", "attachment", "temperature")
                             if isinstance(caps.get(key), bool)},
            "input": _flags(caps.get("input")), "output": _flags(caps.get("output")),
            "limits": _numbers(record.get("limit")), "cost": cost,
            "free_advertised": cost.get("input") == 0 and cost.get("output") == 0 if
                               "input" in cost and "output" in cost else None,
            "efforts": modes, "effort_source": "cli-advertised" if modes else "unknown",
            "availability": "unprobed", "quota": None}


def parse_verbose(text):
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise ValueError("model catalogue exceeded output limit")
    text = ANSI.sub("", text)
    decoder, result, offset = json.JSONDecoder(), [], 0
    while offset < len(text):
        newline = text.find("\n", offset)
        end = newline if newline >= 0 else len(text)
        model_id = text[offset:end].strip()
        offset = end + 1
        if not ID.fullmatch(model_id):
            continue
        start = offset
        while start < len(text) and text[start].isspace():
            start += 1
        try:
            record, consumed = decoder.raw_decode(text, start)
        except ValueError as error:
            raise ValueError("invalid verbose model record: " + model_id) from error
        if not isinstance(record, dict):
            raise ValueError("model record must be an object: " + model_id)
        result.append(normalize(model_id, record))
        offset = consumed
        if len(result) > 10000:
            raise ValueError("model catalogue exceeded record limit")
    if not result:
        raise ValueError("CLI returned no verbose model records")
    return result


def catalogue(engine="opencode", provider="", refresh=False, timeout=20):
    get_provider(engine)  # Validate using installed provider plugins.
    if provider and not re.fullmatch(r"[A-Za-z0-9._-]+", provider):
        raise ValueError("invalid provider id")
    if engine == "opencode":
        argv = executable(engine) + ["models"] + ([provider] if provider else []) + ["--verbose"]
        if refresh:
            argv.append("--refresh")
        models = parse_verbose(bounded_capture(argv, timeout))
        if provider:
            models = [model for model in models if model["provider"] == provider]
        return {"engine": engine, "source": "cli-advertised", "models": models,
                "note": "Advertised data; availability, remaining quota and effective reasoning require separate evidence."}
    if provider:
        raise ValueError("--provider is supported by OpenCode only")
    config = json.loads((ROOT / "providers" / engine / "provider.json").read_text(encoding="utf-8"))
    models = [{"id": model, "name": model, "provider": engine, "source": "configured",
               "efforts": [], "effort_source": "unknown", "availability": "unprobed", "quota": None}
              for model in config.get("models", [])]
    return {"engine": engine, "source": "configured", "models": models,
            "note": "Configured hints only; this engine has no model discovery adapter."}


def probe_status(code, meta, answer):
    reason = probe_reason(meta.get("reason", ""))
    if code == 0 and answer.strip() == "OK":
        return "ok"
    if code == 124:
        return "timeout"
    if code == 125:
        return "silent"
    if "free tier can only be used from within OpenCode" in reason:
        return "route-rejected"
    if re.search(r"rate.?limit|quota|429|credit|payment", reason, re.I):
        return "limited"
    if re.search(r"unauthoriz|auth|api.?key|log.?in|token expired", reason, re.I):
        return "auth-error"
    if re.search(r"model.*(unavailable|unknown|not found)|unrecognized_model", reason, re.I):
        return "model-error"
    if code == 3 and "empty answer" in reason:
        return "empty"
    if code:
        return "provider-error"
    return "empty" if not answer.strip() else "unexpected-answer"


def probe_reason(reason):
    """Surface the error message, never response headers/bodies or auth config."""
    from activity import redact
    try:
        record = json.loads(reason)
        message = record.get("data", {}).get("message") if isinstance(record, dict) else None
        reason = message if isinstance(message, str) else "Provider returned a structured error; see retained task logs."
    except (ValueError, AttributeError):
        pass
    return redact(reason)[:300]


def run_probe(store, opts):
    """One explicitly requested call. Never switch models or retry a failed probe."""
    from . import lifecycle
    from .cli import task_name
    from .logs import answer_text
    from .state import valid_name
    engine = opts.get("engine") or "opencode"
    if engine != "opencode":
        raise ValueError("probe currently supports OpenCode only")
    if not opts.get("model"):
        raise ValueError("probe requires an explicit -m MODEL; no fallback model is selected")
    timeout = int(opts.get("timeout", 200))
    if not 1 <= timeout <= 600:
        raise ValueError("probe --timeout must be between 1 and 600 seconds")
    provider = get_provider(engine)
    model, effort = provider.resolve(opts["model"], opts.get("effort", ""))
    name, reservation = opts.get("name"), None
    if not name:
        name, reservation = task_name(store, "probe " + model)
    valid_name(name)
    if reservation is None:
        reservation = store.path(name, ".reserve")
        try:
            fd = os.open(str(reservation), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as error:
            raise ValueError("probe task name is reserved; choose a new -t NAME") from error
        os.close(fd)
    started = time.monotonic()
    standard = bool(opts.get("--standard"))
    env = {"AGENT_CHAT_ONLY": "0" if standard else "1", "AGENT_TIMEOUT_SEC": str(timeout),
           "AGENT_OPENCODE_TIMEOUT_SEC": str(timeout),
           "AGENT_SILENCE_SEC": str(timeout), "AGENT_OPENCODE_RETRIES": "0",
           "AGENT_PROGRESS": "0"}
    previous = {key: os.environ.get(key) for key in env}
    try:
        if store.path(name, ".meta").exists():
            raise ValueError("probe task already exists; choose a new -t NAME")
        # GUI must invoke this CLI in a separate process, never mutate server-thread env.
        os.environ.update(env)
        with tempfile.TemporaryDirectory(prefix="neoxider-probe-") as folder:
            run_opts = dict(engine=engine, model=model, effort=effort, dir=folder, ask=True,
                            parent=opts.get("parent", os.environ.get("AGENT_PARENT", "")))
            run_opts["--no-progress"] = True
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                code = lifecycle.run(store, name, "Reply OK", run_opts)
            meta, answer = store.read(name), answer_text(store, name)
        status = probe_status(code, meta, answer)
        if meta.get("resolved_model", model) != model or meta.get("effort", effort) != effort:
            status = "route-mismatch"
        result = {"task": name, "engine": engine, "model_requested": model,
                  "model_forwarded": meta.get("resolved_model", model), "effort_requested": effort,
                  "effort_forwarded": meta.get("effort", effort), "effective_effort": None,
                  "effective_effort_source": "not-reported", "status": status,
                  "reply_ok": status == "ok", "exit_code": code,
                  "duration_s": round(time.monotonic() - started, 3),
                  "reason": probe_reason(meta.get("reason", "")),
                  "profile": "standard" if standard else "chat-only",
                  "scope": ("standard OpenCode in empty workspace; tools available; " if standard else "tools disabled; ") +
                           "only chat response checked; tool execution and remaining quota are unverified"}
        store.update(name, kind="model-probe", probe_status=status)
        return result, code if code else (0 if status == "ok" else 3)
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        if reservation:
            reservation.unlink(missing_ok=True)


def command(command_name, opts, args, store):
    if args:
        raise ValueError(command_name + ": use options; remove extra arguments")
    if command_name == "probe":
        payload, code = run_probe(store, opts)
        if opts.get("--json"):
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print("%s: %s / %s, forwarded effort=%s, effective effort=unknown; task=%s" %
                  (payload["status"], payload["engine"], payload["model_forwarded"],
                   payload["effort_forwarded"] or "default", payload["task"]))
            if payload["reason"]:
                print(payload["reason"])
        return code
    payload = catalogue(opts.get("engine", "opencode"), opts.get("provider", ""),
                        opts.get("--refresh", False))
    if opts.get("model"):
        model, unused = get_provider(payload["engine"]).resolve(opts["model"], "")
        payload["models"] = [item for item in payload["models"] if item["id"] == model]
        if not payload["models"]:
            raise ValueError("model not found in catalogue: " + model)
    if opts.get("--json"):
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for item in payload["models"]:
            print("%s | efforts=%s | reasoning=%s | tools=%s | free=%s | context=%s | availability=unprobed" %
                  (item["id"], ",".join(item["efforts"]) or "unknown",
                   item.get("capabilities", {}).get("reasoning", "unknown"),
                   item.get("capabilities", {}).get("toolcall", "unknown"),
                   item.get("free_advertised", "unknown"), item.get("limits", {}).get("context", "unknown")))
        print(payload["note"])
    return 0
