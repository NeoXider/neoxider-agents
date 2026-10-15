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
                        if problem == "deadline exceeded":
                            # The pipe drain reached its deadline between the check and the read:
                            # same outcome as the check above, not a helper error.
                            finish(engine, "whole doctor deadline exceeded" if time.monotonic() >= deadline else "provider diagnostic deadline exceeded")
                        elif problem:
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
    from .providers import known_limits
    for engine, row in sorted(known_limits(now=generated).items()):
        left = int(row["until"] - generated)
        remaining = "resets in %dd%02dh" % (left // 86400, left % 86400 // 3600) if left >= 86400 else "resets in %dh%02dm" % (left // 3600, left % 3600 // 60)
        lines += ["=== %s usage limit (last reported by a task) ===" % engine,
                  "  LIMITED   resets %s (%s)%s" % (row.get("resets") or "unknown", remaining,
                                                   "" if isinstance(row.get("resets_epoch"), (int, float)) else "; reset time not exact")]
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


# Phase 7 data APIs. These do not call doctor, providers, lifecycle or state.
from dataclasses import dataclass, asdict, field
from decimal import Decimal, InvalidOperation
import hashlib
import re
import stat
from typing import Any, Iterable, Mapping, Optional, Tuple

TEXT_LIMIT = 4096
SCAN_LIMIT = 65536
_SHA = re.compile(r"^[0-9a-f]{64}$")


def _valid_exit_code(value):
    """Portable signed32 or Windows unsigned DWORD32; preserve the raw integer."""
    return (isinstance(value, int) and not isinstance(value, bool)
            and -(2 ** 31) <= value < 2 ** 32)


def _safe(value, limit=TEXT_LIMIT):
    from activity import redact
    # Only redact the retained prefix plus lookahead, avoiding whole-output copies.
    # A credential crossing the boundary is still masked by its prefix/name.
    value = str(value)[:limit + 256]
    # activity.redact handles named credentials; additionally cover URL userinfo.
    value = re.sub(r"(https?://)[^/\s@]+@", r"\1[REDACTED]@", str(value))
    value = re.sub(r"(https?://)[^/\s?#]*:[^/\s?#]*$", r"\1[REDACTED]", value)
    value = re.sub(r"(?i)([?&](?:key|api_key|access_token|token|password|secret)=)[^&#\s]+",
                   r"\1[REDACTED]", value)
    return redact(value)[:limit]


@dataclass(frozen=True)
class FailureReport:
    category: str
    reason: str
    source: str
    confidence: str
    blocking: bool
    exit_code: Optional[int] = None
    reason_truncated: bool = False

    def __post_init__(self):
        if self.category not in ("transport", "tls", "network", "auth", "quota", "model_unavailable",
                                  "provider_oom", "watchdog_timeout", "watchdog_silence", "cancelled", "unknown"):
            raise ValueError("unknown failure category")
        if self.exit_code is not None and not _valid_exit_code(self.exit_code):
            raise ValueError("invalid failure exit code")
        object.__setattr__(self, "reason_truncated", self.reason_truncated or len(str(self.reason)) > TEXT_LIMIT)
        object.__setattr__(self, "reason", _safe(self.reason))


def _error_boundary(text):
    """Recognize envelopes, never search nested tool output or assistant content."""
    stripped = text.strip()
    if stripped.startswith("AGENT_PROVIDER_ERROR:"):
        return stripped.partition(":")[2].strip()
    try:
        event = json.loads(stripped)
    except (ValueError, TypeError):
        return None
    if not isinstance(event, dict) or event.get("role") in ("assistant", "tool", "user"):
        return None
    if event.get("type") in ("error", "turn.failed"):
        payload = event.get("error", event.get("message", event.get("part", {})))
    elif event.get("type") == "result" and event.get("is_error") is True:
        payload = event.get("result", event.get("subtype", ""))
    elif "error" in event and not any(key in event for key in ("content", "item", "tool", "part")):
        payload = event["error"]
    else:
        return None
    return payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)


def _failure_category(reason):
    # Concrete runtime/transport failures take precedence over incidental quota words.
    patterns = (
        ("provider_oom", r"out[ _-]of[ _-]memory|\boom\b|rangeerror.*oom|heap.*(?:limit|exhaust)|allocation failed|invalid string length"),
        ("tls", r"\btls\b|\bssl\b|certificate|cert_has_expired|unable_to_verify|self.signed|err_tls|_cert_"),
        ("network", r"econnreset|econnrefused|etimedout|eai_again|enotfound|socket hang up|network.error|dns|connection (?:reset|refused)|fetch failed"),
        ("auth", r"unauthoriz|forbidden|invalid.api.key|authentication|not logged in|token expired|session expired|(?:status(?:code)?|http)[\s\"=:]+(?:401|403)\b"),
        ("quota", r"rate[ _-]?limit|ratelimit|quota.exceeded|insufficient.(?:quota|credit)|usage.limit|payment.required|(?:status(?:code)?|http)[\s\"=:]+(?:429|402)\b"),
        ("model_unavailable", r"unrecognized_model|model_not_found|model[^\n]*(?:not found|unavailable|unknown|does not exist)|issue with the selected model"),
        ("transport", r"providerresponsestreamerror|stream (?:error|closed|interrupted)|bad gateway|service unavailable|gateway.time.?out|(?:status(?:code)?|http)[\s\"=:]+(?:500|502|503|504)\b"),
        ("cancelled", r"\bcancelled\b|\bcanceled\b|interrupted by user"),
    )
    for category, pattern in patterns:
        if re.search(pattern, reason, re.I):
            return category
    return "unknown"


def _error_signals(reason, depth=0):
    """Ignore request/tool/model content embedded inside a real error envelope."""
    if depth > 6:
        return ""
    if isinstance(reason, str):
        try:
            parsed = json.loads(reason)
        except (ValueError, TypeError):
            return reason
        if isinstance(parsed, dict):
            return _error_signals(parsed, depth + 1)
        return reason
    if isinstance(reason, dict):
        if reason.get("role") in ("assistant", "tool", "user"):
            return ""
        parts = [_error_signals(reason[key], depth + 1) for key in
                        ("name", "type", "message", "code", "status", "statusCode", "error", "data", "responseBody")
                        if key in reason]
        for key in ("code", "status", "statusCode"):
            if key in reason and str(reason[key]) in ("401", "403", "402", "429", "500", "502", "503", "504"):
                parts.append("HTTP " + str(reason[key]))
        return " ".join(parts)
    return str(reason) if isinstance(reason, (int, float)) else ""


def classify_failure(text, exit_code=None, watchdog=None, source="raw", trusted=False):
    """Pure classification; only caller-attested provider boundaries can block.

    source=provider_error attests an already extracted error payload. A trusted
    provider_stream needs a recognized envelope. Raw/model/tool text is advisory,
    even if it contains fake envelopes. Watchdog is a caller-owned signal.
    """
    if source not in ("raw", "provider_error", "provider_stream", "model", "tool", "wrapper"):
        raise ValueError("unknown failure source")
    if watchdog not in (None, "timeout", "silence", "cancelled"):
        raise ValueError("unknown watchdog signal")
    original = str(text or "")
    if watchdog:
        category = {"timeout": "watchdog_timeout", "silence": "watchdog_silence",
                    "cancelled": "cancelled"}[watchdog]
        return FailureReport(category, _safe(original), "wrapper", "explicit", True,
                             exit_code, len(original) > TEXT_LIMIT)
    bounded = original[:SCAN_LIMIT]
    payload = _error_boundary(bounded)
    authorized = source == "provider_error" or (source == "provider_stream" and trusted and payload is not None)
    reason = (payload if payload is not None else bounded) if authorized else bounded
    category = _failure_category(_error_signals(reason) if authorized else reason)
    if source == "wrapper" and exit_code in (130, -2):
        category, authorized = "cancelled", True
    return FailureReport(category, _safe(reason), source, "explicit" if authorized else "heuristic",
                         authorized,
                         exit_code, len(original) > SCAN_LIMIT or len(reason) > TEXT_LIMIT)


def _money(value):
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("money must be a finite nonnegative number")
    if isinstance(value, int) and value.bit_length() > 256:
        raise ValueError("money exceeds reporting precision")
    text = str(value)
    if len(text) > 128:
        raise ValueError("money exceeds reporting precision")
    try:
        amount = Decimal(text)
    except (InvalidOperation, ValueError):
        raise ValueError("money must be a finite nonnegative number")
    if not amount.is_finite() or amount < 0:
        raise ValueError("money must be a finite nonnegative number")
    return amount


@dataclass(frozen=True)
class CostFact:
    """Cumulative task/window amount; estimates never become billing evidence."""
    kind: str
    amount: Optional[Decimal] = None
    currency: Optional[str] = None
    provenance: str = ""
    measurement_window: Optional[Tuple[str, str]] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None

    def __post_init__(self):
        if self.kind not in ("catalog_estimate", "provider_reported", "confirmed_billing"):
            raise ValueError("unknown cost evidence kind")
        object.__setattr__(self, "amount", _money(self.amount))
        if self.currency is not None and not re.fullmatch(r"[A-Z]{3}", self.currency):
            raise ValueError("currency must be an uppercase three-letter code")
        if self.amount is not None and not self.currency:
            raise ValueError("known cost needs currency")
        if not self.provenance or len(self.provenance) > TEXT_LIMIT:
            raise ValueError("cost needs bounded provenance")
        object.__setattr__(self, "provenance", _safe(self.provenance))
        if self.measurement_window is not None:
            if not isinstance(self.measurement_window, (tuple, list)):
                raise ValueError("measurement window must be a tuple/list of endpoints")
            window = tuple(self.measurement_window)
            if len(window) != 2 or any(not isinstance(x, str) or not x or len(x) > 128 for x in window):
                raise ValueError("measurement window needs two bounded caller-defined endpoints")
            object.__setattr__(self, "measurement_window", tuple(_safe(x, 128) for x in window))
        for count in (self.input_tokens, self.output_tokens):
            if count is not None and (isinstance(count, bool) or not isinstance(count, int) or not 0 <= count < 2 ** 63):
                raise ValueError("usage must be a nonnegative integer or None")


@dataclass(frozen=True)
class CostPolicy:
    caller: str
    task: str
    cap: Optional[Decimal] = None
    currency: str = "USD"
    measurement_window: Optional[Tuple[str, str]] = None
    hard_billing_cap: bool = False
    uncertainty: str = "fail_closed"

    def __post_init__(self):
        if not self.caller or not self.task or max(len(self.caller), len(self.task)) > 256:
            raise ValueError("policy must belong to a bounded caller/task")
        object.__setattr__(self, "caller", _safe(self.caller, 256))
        object.__setattr__(self, "task", _safe(self.task, 256))
        object.__setattr__(self, "cap", _money(self.cap))
        if self.uncertainty not in ("fail_closed", "allow"):
            raise ValueError("uncertainty must be fail_closed or allow")
        if self.hard_billing_cap and self.cap is None:
            raise ValueError("hard billing cap needs an explicit cap")
        # Reuse currency/window validation without treating this policy as a fact.
        checked = CostFact("catalog_estimate", self.cap, self.currency, "caller policy", self.measurement_window)
        object.__setattr__(self, "measurement_window", checked.measurement_window)


@dataclass(frozen=True)
class CostDecision:
    stop: bool
    reason: str
    known_cost: Optional[Decimal]
    evidence_kind: Optional[str]
    cap: Optional[Decimal]
    uncertain: bool


@dataclass(frozen=True)
class CostGuard:
    """Frozen per-caller/task policy; returns decisions, never raises a cap or bills."""
    policy: CostPolicy

    def evaluate(self, facts: Iterable[CostFact], hard_cap_enforceable=False):
        policy = self.policy
        best, exceeded = None, None
        rank = {"provider_reported": 1, "confirmed_billing": 2}
        for fact in facts:
            if (fact.kind in rank and fact.amount is not None and fact.currency == policy.currency
                    and policy.measurement_window is not None and fact.measurement_window == policy.measurement_window):
                if policy.cap is not None and fact.amount >= policy.cap:
                    if exceeded is None or fact.amount > exceeded.amount:
                        exceeded = fact
                if best is None or (rank[fact.kind], fact.amount) > (rank[best.kind], best.amount):
                    best = fact
        known = best.amount if best is not None else None
        kind = best.kind if best is not None else None
        if policy.cap is None:
            return CostDecision(False, "no_cap", known, kind, None, known is None)
        # This is a prelaunch guard: equality leaves no spending headroom.
        if exceeded is not None:
            reason = "known_cost_exceeded" if exceeded.amount > policy.cap else "no_cost_headroom"
            return CostDecision(True, reason, exceeded.amount, exceeded.kind, policy.cap, False)
        uncertain = known is None or (policy.hard_billing_cap and not hard_cap_enforceable)
        if uncertain:
            return CostDecision(policy.hard_billing_cap or policy.uncertainty == "fail_closed",
                                "cost_uncertain", known, kind, policy.cap, True)
        return CostDecision(False, "within_cap", known, kind, policy.cap, False)


def _valid_hash(value):
    return isinstance(value, str) and _SHA.fullmatch(value) is not None


def _text_hash(text):
    digest = hashlib.sha256()
    for offset in range(0, len(text), 65536):
        digest.update(text[offset:offset + 65536].encode("utf-8"))
    return digest.hexdigest()


def _source_stat(path, regular=False):
    info = os.lstat(str(path))
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise ValueError("source path contains a symlink/reparse point")
    if regular:
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("source must be a regular file with exactly one link")
    elif not stat.S_ISDIR(info.st_mode):
        raise ValueError("source parent must be a directory")
    return info


def _source_identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, getattr(info, "st_file_attributes", 0),
            getattr(info, "st_reparse_tag", 0))


def _source_binding(info):
    # Windows path stat and descriptor stat may expose different ctime meanings.
    return _source_identity(info) + (info.st_nlink, info.st_size, info.st_mtime_ns)


def _source_version(info):
    return _source_binding(info) + (info.st_ctime_ns,)


def source_hash(root, paths, max_files=10000, max_bytes=512 * 1024 * 1024,
                max_file_bytes=64 * 1024 * 1024):
    """Bound source identity/content to stable ordinary files; not a sandbox.

    Reject observed reparse points and hardlinks. O_NOFOLLOW protects the final
    POSIX component when available; Windows uses checked identities, without an
    atomic no-follow guarantee. Concurrent observable changes raise ValueError.
    """
    for limit in (max_files, max_bytes, max_file_bytes):
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            raise ValueError("source limits must be nonnegative integers")
    root = Path(os.path.abspath(str(root)))
    names = set()
    for index, value in enumerate(paths):
        if index >= max_files:
            raise ValueError("source file count exceeds limit")
        name = str(value)
        if len(name) > 4096:
            raise ValueError("source path exceeds length limit")
        name = name.replace("\\", "/")
        relative = Path(name)
        if not name or relative.is_absolute() or ".." in relative.parts or ":" in name or not relative.parts:
            raise ValueError("source paths must be relative regular files")
        names.add(relative.as_posix())
        if len(names) > max_files:
            raise ValueError("source file count exceeds limit")
    digest = hashlib.sha256()
    total = 0
    for name in sorted(names):
        path = root / name
        parents = tuple(reversed(path.parents))
        parent_ids = tuple(_source_identity(_source_stat(parent)) for parent in parents)
        before = _source_stat(path, regular=True)
        if before.st_size > max_file_bytes or total + before.st_size > max_bytes:
            raise ValueError("source bytes exceed limit")
        content = hashlib.sha256()
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        fd = os.open(str(path), flags)
        try:
            stream = os.fdopen(fd, "rb")
        except BaseException:
            os.close(fd)
            raise
        with stream:
            opened = os.fstat(stream.fileno())
            if _source_binding(before) != _source_binding(opened):
                raise ValueError("source changed before hashing")
            count = 0
            for block in iter(lambda: stream.read(65536), b""):
                count += len(block)
                if count > max_file_bytes or total + count > max_bytes:
                    raise ValueError("source bytes exceed limit")
                content.update(block)
            after = os.fstat(stream.fileno())
            if _source_version(opened) != _source_version(after) or count != opened.st_size:
                raise ValueError("source changed while hashing")
            path_after = _source_stat(path, regular=True)
            if (_source_binding(after) != _source_binding(path_after)
                    or _source_version(before) != _source_version(path_after)):
                raise ValueError("source path changed while hashing")
            if parent_ids != tuple(_source_identity(_source_stat(parent)) for parent in parents):
                raise ValueError("source parent changed while hashing")
        total += count
        digest.update(json.dumps([name, content.hexdigest()], ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


@dataclass(frozen=True)
class VerificationReceipt:
    command: Tuple[str, ...]
    exit_code: Optional[int]
    result: str
    source_final_hash: str
    executed_by: str
    evidence_source: str
    check_kind: str = "command"
    result_sha256: str = field(init=False)
    result_truncated: bool = field(init=False)

    def __post_init__(self):
        if self.executed_by not in ("wrapper", "caller", "model_assertion"):
            raise ValueError("unknown executor")
        if self.evidence_source not in ("external_command", "model_assertion"):
            raise ValueError("unknown verification evidence")
        if self.executed_by == "model_assertion" and self.evidence_source != "model_assertion":
            raise ValueError("model assertion is not external execution")
        if self.check_kind not in ("command", "tests", "qa") or not _valid_hash(self.source_final_hash):
            raise ValueError("receipt needs check kind and exact source-final SHA256")
        command = tuple(self.command)
        if (not command or len(command) > 64 or any(not isinstance(x, str) or len(x) > TEXT_LIMIT for x in command)
                or sum(map(len, command)) > TEXT_LIMIT):
            raise ValueError("receipt needs an exact bounded argv")
        # Never silently truncate argv. Credentials must stay out of receipts.
        if any(_safe(arg) != arg for arg in command):
            raise ValueError("receipt command contains credentials; use a credential-free command")
        if self.exit_code is not None and not _valid_exit_code(self.exit_code):
            raise ValueError("exit code must be an integer or None")
        object.__setattr__(self, "command", command)
        original = str(self.result)
        object.__setattr__(self, "result_sha256", _text_hash(original))
        object.__setattr__(self, "result", _safe(original))
        object.__setattr__(self, "result_truncated", len(original) > TEXT_LIMIT)

    def status(self, current_source_hash):
        if self.executed_by == "model_assertion" or self.evidence_source == "model_assertion":
            return "model_assertion"
        if self.source_final_hash != current_source_hash:
            return "stale"
        if self.exit_code is None:
            return "not_run"
        return "passed" if self.exit_code == 0 else "failed"


def compact_result(result: Mapping[str, Any], receipts=(), current_source_hash=None,
                   max_files=64, max_receipts=16, failure=None, cost_facts=(),
                   cost_decision=None, max_cost_facts=16):
    """Bounded additive report; no task exit-to-tests inference or state mutation.

    Iterates deltas once without copying raw output/history. Hashes the full delta
    while retaining only a bounded prefix and explicit omitted counts.
    """
    if not 0 <= max_files <= 256 or not 0 <= max_receipts <= 64 or not 0 <= max_cost_facts <= 32:
        raise ValueError("compact report bounds exceed hard limits")
    if current_source_hash is not None and not _valid_hash(current_source_hash):
        raise ValueError("invalid current source hash")
    files, total, digest = [], 0, hashlib.sha256()
    for change in result.get("changed_files") or ():
        entry = {key: _safe(change.get(key, ""), 512) for key in ("path", "status")}
        for key in ("before_sha256", "after_sha256"):
            value = change.get(key)
            entry[key] = value if _valid_hash(value) else None
        entry["path_truncated"] = len(str(change.get("path", ""))) > 512
        # Bind even omitted or long path entries, without serializing arbitrary data.
        digest.update(json.dumps([str(change.get("path", "")), entry["status"],
                                  entry["before_sha256"], entry["after_sha256"]],
                                 ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        digest.update(b"\n")
        if len(files) < max_files:
            files.append(entry)
        total += 1
    checks, count = [], 0
    tests, qa = set(), set()
    for receipt in receipts:
        status = receipt.status(current_source_hash)
        if receipt.check_kind == "tests":
            tests.add(status)
        if receipt.check_kind == "qa":
            qa.add(status)
        if len(checks) < max_receipts:
            checks.append(dict(asdict(receipt), status=status))
        count += 1

    def check_status(statuses):
        statuses = statuses - {"model_assertion"}
        if "failed" in statuses:
            return "failed"
        if "stale" in statuses:
            return "stale"
        if "passed" in statuses and all(x == "passed" for x in statuses):
            return "passed"
        return "not_run"

    declared = result.get("scope_declared", ())
    if isinstance(declared, str):
        declared = (declared,)
    from itertools import islice
    scope = [_safe(item, 512) for item in islice(declared, 65)]
    costs, cost_count = [], 0
    for fact in cost_facts:
        if len(costs) < max_cost_facts:
            item = asdict(fact)
            item["amount"] = str(fact.amount) if fact.amount is not None else None
            costs.append(item)
        cost_count += 1
    decision = None
    if cost_decision is not None:
        decision = asdict(cost_decision)
        for key in ("known_cost", "cap"):
            decision[key] = str(decision[key]) if decision[key] is not None else None
    exit_code = result.get("exit_code")
    if not _valid_exit_code(exit_code):
        exit_code = None
    return dict(task=_safe(result.get("task", ""), 256), state=_safe(result.get("state", ""), 64),
                exit_code=exit_code, final_answer=_safe(result.get("final_answer", ""), 1024),
                source_final_hash=current_source_hash, changed_files=files, changed_files_count=total,
                changed_files_omitted=total - len(files), change_sha256=digest.hexdigest(),
                baseline_known=result.get("baseline_known") is True,
                baseline_partial=result.get("baseline_partial") is True,
                baseline_warning=_safe(result.get("baseline_warning", ""), 512),
                scope_declared=scope[:64], scope_truncated=len(scope) > 64,
                scope_enforced=result.get("scope_enforced") is True,
                verification=checks, verification_omitted=count - len(checks),
                tests_status=check_status(tests), tests_not_run=not bool(tests & {"passed", "failed", "stale"}),
                qa_status=check_status(qa), failure=asdict(failure) if failure is not None else None,
                cost_facts=costs, cost_facts_omitted=cost_count - len(costs), cost_decision=decision)
