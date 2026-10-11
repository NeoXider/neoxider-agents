"""Concurrent provider probes and bounded real command checks."""
import contextlib
import io
import json
import os
import secrets
import sys
import time
from datetime import datetime, timezone
import tempfile
from threading import Event
from pathlib import Path
from .providers import ROOT, get_provider, provider_info, drain_diagnostic_pipe, close_diagnostic_tree

DOCTOR_SECONDS = 15.0
PROVIDER_SECONDS = 10.0
OUTPUT_BYTES = 256 * 1024


def _seconds(key, default):
    try:
        value = float(os.environ.get(key, default))
        return value if 0 < value < float("inf") else default
    except (ValueError, OverflowError):
        return default


def _unknown(engine, state, note):
    return dict(engine=engine, version="?", available=None, login="", limits=None,
                state=state, note=note)


def bounded_probes(engines, command=None):
    """Isolate resolution, filesystem and network work in owned diagnostic trees."""
    from .process import spawn
    from activity import redact
    whole = _seconds("AGENT_DOCTOR_TIMEOUT_SEC", DOCTOR_SECONDS)
    per = _seconds("AGENT_DOCTOR_PROVIDER_TIMEOUT_SEC", PROVIDER_SECONDS)
    deadline = time.monotonic() + whole
    active, results = {}, {}
    sleeper = Event()

    def finish(engine, failure=None, read_error=None):
        tree, buffer, unused = active[engine]
        problem = read_error or ""
        try:
            if not failure and not problem:
                problem = drain_diagnostic_pipe(tree.process.stdout, buffer, OUTPUT_BYTES, deadline)
        except (OSError, ValueError):
            problem = "diagnostic output read failed"
        finally:
            try:
                cleanup = close_diagnostic_tree(tree)
            finally:
                active.pop(engine, None)
        if cleanup:
            results[engine] = _unknown(engine, "error", cleanup)
            return
        if failure:
            results[engine] = _unknown(engine, "timeout", failure)
        elif problem:
            results[engine] = _unknown(engine, "error", problem)
        elif tree.process.returncode:
            results[engine] = _unknown(engine, "error", "diagnostic helper exited unsuccessfully")
        else:
            try:
                payload = json.loads(buffer.decode("utf-8"))
                if not isinstance(payload, dict) or payload.get("engine", engine) != engine:
                    raise ValueError("unexpected diagnostic result")
                results[engine] = json.loads(redact(json.dumps(payload, ensure_ascii=False)))
            except (ValueError, UnicodeError):
                results[engine] = _unknown(engine, "error", "diagnostic helper returned invalid JSON")

    with tempfile.TemporaryDirectory(prefix="neoxider-doctor-") as folder:
        prompt = os.devnull
        try:
            for engine in engines:
                if time.monotonic() >= deadline:
                    results[engine] = _unknown(engine, "timeout", "whole doctor deadline exceeded")
                    continue
                started = time.monotonic()
                env = dict(os.environ, AGENT_DIAGNOSTIC_HELPER="1",
                           AGENT_DIAGNOSTIC_DEADLINE=str(min(deadline, started + per)),
                           PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
                argv = command(engine) if command else [sys.executable, str(ROOT / "agent.py"), "provider-info", engine]
                try:
                    tree = spawn(argv, prompt, str(ROOT), env)
                    active[engine] = (tree, bytearray(), min(deadline, started + per))
                except (OSError, ValueError, RuntimeError):
                    results[engine] = _unknown(engine, "error", "diagnostic helper could not start")
            while active:
                now = time.monotonic()
                for engine, (tree, buffer, until) in list(active.items()):
                    if now >= until:
                        finish(engine, "whole doctor deadline exceeded" if now >= deadline else "provider diagnostic deadline exceeded")
                    else:
                        try:
                            problem = drain_diagnostic_pipe(tree.process.stdout, buffer, OUTPUT_BYTES, until)
                        except (OSError, ValueError):
                            problem = "diagnostic output read failed"
                        if problem:
                            finish(engine, read_error=problem)
                        elif tree.process.poll() is not None:
                            finish(engine)
                if active:
                    sleeper.wait(min(0.02, max(0, deadline - time.monotonic())))
        finally:
            for engine in list(active):
                finish(engine, "diagnostic supervisor cancelled")
    return [results[engine] for engine in engines]


def epoch(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return float(value)
    except ValueError:
        pass
    try:
        stamp = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        return (stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)).timestamp()
    except ValueError:
        return None


def windows(info):
    limits = info.get("limits") or {}
    if not isinstance(limits, dict):
        return []
    if isinstance(limits.get("windows"), list):
        return [row for row in limits["windows"] if isinstance(row, dict)]
    return [dict(limits[key], label=limits[key].get("label", key))
            for key in ("primary", "secondary") if isinstance(limits.get(key), dict)]


def normalize(info, engine):
    if not isinstance(info, dict):
        info = _unknown(engine, "error", "probe returned invalid JSON")
    info.setdefault("engine", engine)
    limits = info.get("limits")
    if isinstance(limits, dict):
        buckets = [row for row in (limits.get("windows") or []) if isinstance(row, dict)]
        buckets += [limits[key] for key in ("primary", "secondary") if isinstance(limits.get(key), dict)]
        for row in buckets:
            row["resets_at"] = epoch(row.get("resets_at"))
    login = (info.get("login") or "").lower()
    if info.get("state") in ("timeout", "error"):
        return info
    if info.get("available") is None:
        info.update(state="error", available=None, limits=None, note="diagnostic availability unknown")
        return info
    info["state"] = ("not_installed" if not info.get("available") else "not_logged_in"
                     if any(word in login for word in ("required", "not logged", "logged out")) else "ok")
    return info


def render(results, deep_engines, deep, generated):
    lines = ["=== engines (CLI) ==="]
    for row in results:
        engine = row["engine"]
        if row["state"] in ("timeout", "error"):
            lines.append("  %-9s %s (health/limits unknown: %s)" % (engine, row["state"], row.get("note", "")))
        elif row["state"] == "not_installed":
            lines.append("  %-9s -    (not in PATH)" % engine)
        else:
            lines.append("  %-9s ok   %s" % (engine, row.get("version") or "?"))
            if row.get("login"):
                lines.append("  %-9s auth %s" % (engine, row["login"]))
    for row in results:
        if row["engine"] not in ("codex", "claude"):
            continue
        live = windows(row)
        source = "latest session" if row["engine"] == "codex" else "provider API"
        if live:
            lines += ["=== %s rate limits (%s) ===" % (row["engine"], source),
                      "  plan: " + str((row.get("limits") or {}).get("plan_type") or "?")]
            for window in live:
                used_value = window.get("used_percent", window.get("utilization"))
                if used_value is None:
                    lines.append("  %-9s utilization unknown" % (window.get("label") or "window"))
                    continue
                used = float(used_value)
                size = max(0, min(10, int(used // 10)))
                minutes = window.get("window_minutes")
                label = "?" if not minutes else "%sd" % (minutes // 1440) if minutes % 1440 == 0 else "%sh" % (minutes // 60) if minutes % 60 == 0 else "%sm" % minutes
                reset = epoch(window.get("resets_at"))
                left = int(reset - generated) if reset else None
                note = "" if left is None else " resets soon" if left <= 0 else " resets in %dh%02dm" % (left // 3600, left % 3600 // 60)
                lines.append("  %-9s [%s] %4.0f%% (window %s)%s" % (window.get("label") or "window", "#" * size + "-" * (10 - size), used, label, note))
        elif row["engine"] == "codex":
            lines += ["=== codex rate limits (from latest session) ===", "  no rate-limit data in recent sessions"]
    claude = next((row for row in results if row["engine"] == "claude"), None)
    if claude and not windows(claude):
        lines += ["=== claude usage (local transcript estimate) ===", "  " + (claude.get("note") or "no local usage data")]
    lines.append("=== deep checks (real one-shot runs) ===")
    if not deep_engines:
        lines.append("  (no provider implements a deep check)")
    elif not deep:
        lines += ["  skipped - run 'agent.sh doctor --deep' to execute a shell command through each supported engine",
                  "  (that is the only check that catches 'the model answers but every command hangs')"]
    return "\n".join(lines) + "\n"


def doctor(store, opts, args, single=False):
    if args and not single:
        raise ValueError("doctor: unexpected argument '" + args[0] + "'")
    if opts.get("--deep") and opts.get("--json"):
        raise ValueError("doctor: --deep and --json cannot be combined")
    if single:
        if len(args) != 1:
            raise ValueError("provider-info needs ENGINE")
        if os.environ.get("AGENT_DIAGNOSTIC_HELPER") == "1":
            provider = get_provider(args[0])
            try:
                data = provider.doctor()
            except TimeoutError:
                data = _unknown(args[0], "timeout", "provider diagnostic timed out")
            except (OSError, ValueError, RuntimeError):
                data = _unknown(args[0], "error", "provider diagnostic failed")
            if not isinstance(data, dict):
                data = _unknown(args[0], "error", "provider returned invalid diagnostic data")
            data["_doctor_deep"] = args[0] == "codex" or callable(getattr(provider, "doctor_deep", None))
        else:
            data = bounded_probes(args)[0]
            data.pop("_doctor_deep", None)
        print(json.dumps(data, ensure_ascii=False))
        return 0
    engines = list(provider_info())
    results = bounded_probes(engines)
    deep_engines = [engine for engine, row in zip(engines, results)
                    if row.pop("_doctor_deep", engine == "codex")]
    results = [normalize(row, engine) for row, engine in zip(results, engines)]
    generated = time.time()
    raw = render(results, deep_engines, opts.get("--deep"), generated)
    raw += "=== diagnostic budgets: whole %.3fs / provider %.3fs / cleanup <=0.4s per helper ===\n" % (
        _seconds("AGENT_DOCTOR_TIMEOUT_SEC", DOCTOR_SECONDS),
        _seconds("AGENT_DOCTOR_PROVIDER_TIMEOUT_SEC", PROVIDER_SECONDS))
    if opts.get("--json"):
        print(json.dumps(dict(generated_at=generated, engines=results, raw=raw, deep_engines=deep_engines), ensure_ascii=False))
        return 0
    print(raw, end="")
    if not opts.get("--deep"):
        return 0
    from .lifecycle import run
    for row in results:
        if row["engine"] not in deep_engines:
            continue
        engine = row["engine"]
        if not row.get("available") or row.get("state") in ("timeout", "error"):
            reason = "health not verified" if row.get("state") in ("timeout", "error") else "%s not in PATH" % engine
            print("  %s shell: —    (%s)" % (engine, reason))
            continue
        directory = store.root / ("doctor-" + engine)
        directory.mkdir(exist_ok=True)
        sentinel = "sentinel-" + secrets.token_hex(6) + ".txt"
        (directory / sentinel).write_text("doctor", encoding="utf-8")
        name = "doctor-" + engine + "-" + secrets.token_hex(4)
        try:
            saved = {key: os.environ.get(key) for key in ("AGENT_TIMEOUT_SEC", "AGENT_CHAT_ONLY")}
            os.environ["AGENT_TIMEOUT_SEC"] = os.environ.get("AGENT_CODEX_DOCTOR_TIMEOUT", "60") if engine == "codex" else "60"
            os.environ["AGENT_CHAT_ONLY"] = "1"
            try:
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    rc = run(store, name, "Execute a shell command to list this working directory. Return only the filenames.",
                             dict(engine=engine, model=os.environ.get("AGENT_CODEX_DOCTOR_MODEL", "spark") if engine == "codex" else "",
                                  dir=str(directory), **{"--no-progress": True, "--no-terse": True}))
            finally:
                for key, value in saved.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
            from .state import last_output
            passed = rc == 0 and sentinel in last_output(store.path(name, ".log"))
        except Exception:
            passed = False
        print("  %s shell: %s (real command %s)" % (engine, "ok" if passed else "BROKEN", "executed" if passed else "not verified"))
    return 0
