"""Concurrent provider probes and bounded real command checks."""
import contextlib
import io
import json
import os
import secrets
import sys
import time
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from .providers import get_provider, provider_info


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
        info = dict(version="ERROR", available=False, login="", limits=None, note="probe returned invalid JSON")
    info.setdefault("engine", engine)
    limits = info.get("limits")
    if isinstance(limits, dict):
        buckets = [row for row in (limits.get("windows") or []) if isinstance(row, dict)]
        buckets += [limits[key] for key in ("primary", "secondary") if isinstance(limits.get(key), dict)]
        for row in buckets:
            row["resets_at"] = epoch(row.get("resets_at"))
    login = (info.get("login") or "").lower()
    info["state"] = ("not_installed" if not info.get("available") else "not_logged_in"
                     if any(word in login for word in ("required", "not logged", "logged out")) else "ok")
    return info


def render(results, deep_engines, deep, generated):
    lines = ["=== engines (CLI) ==="]
    for row in results:
        engine = row["engine"]
        if row["state"] == "not_installed":
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
                used = float(window.get("used_percent") or window.get("utilization") or 0)
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
    engines = list(provider_info())
    if single:
        if len(args) != 1:
            raise ValueError("provider-info needs ENGINE")
        print(json.dumps(get_provider(args[0]).doctor(), ensure_ascii=False))
        return 0
    deep_engines = []
    for engine in engines:
        provider = get_provider(engine)
        if engine == "codex" or callable(getattr(provider, "doctor_deep", None)):
            deep_engines.append(engine)

    def probe(engine):
        try:
            return get_provider(engine).doctor()
        except (OSError, ValueError, RuntimeError) as error:
            return dict(engine=engine, available=False, version="ERROR", note=str(error), limits=None)
    with ThreadPoolExecutor(max_workers=max(1, len(engines))) as pool:
        results = list(pool.map(probe, engines))
    results = [normalize(row, engine) for row, engine in zip(results, engines)]
    generated = time.time()
    raw = render(results, deep_engines, opts.get("--deep"), generated)
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
        if not row.get("available"):
            print("  %s shell: —    (%s not in PATH)" % (engine, engine))
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
