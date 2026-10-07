#!/usr/bin/env python3
"""Private structured stream capture and dependency-free activity digests.

Providers keep their existing clean stdout contract. AGENT_ACTIVITY_FILE opts into
an append-only, redacted sidecar; callers without that variable are unaffected.
"""
import argparse
from collections import deque
from datetime import datetime
import json
import os
from pathlib import Path
import re
import sys
import time

TAIL_BYTES = 65536
TOKEN = re.compile(r"(?<![\w])(?:[A-Za-z0-9_+/=-]{32,})(?![\w])")
AUTH = re.compile(r"(?i)(authorization\s*[:=]\s*)(?:bearer\s+|basic\s+)?[^\s,;\"}]+")
SECRET = re.compile(r"(?i)([\"']?(?:api[_-]?key|access[_-]?token|password|secret|token)[\"']?\s*[:=]\s*[\"']?)[^\s,;\"'}]+")
SECRET_KEYS = re.compile(r"(?i)^(?:authorization|api[_-]?key|access[_-]?token|password|secret|token)$")


def redact(text):
    """Mask headers, named credentials, and long opaque credential-like strings."""
    value = AUTH.sub(r"\1[REDACTED]", str(text))
    value = SECRET.sub(r"\1[REDACTED]", value)
    return TOKEN.sub("[REDACTED]", value)


def safe_event(value):
    if isinstance(value, dict):
        result = {}
        thinking = value.get("type") in ("reasoning", "thinking", "thinking_delta", "reasoning_delta")
        for key, item in value.items():
            if SECRET_KEYS.match(key):
                result[key] = "[REDACTED]"
            elif thinking and key in ("text", "thinking", "delta", "summary", "content"):
                result[key] = "thinking"
            else:
                result[key] = safe_event(item)
        return result
    if isinstance(value, list):
        return [safe_event(item) for item in value]
    return redact(value) if isinstance(value, str) else value


def native_path(value):
    # Native Windows Python receives environment values without Git Bash argv conversion.
    if os.name == "nt" and re.match(r"^/[a-zA-Z]/", value):
        return value[1].upper() + ":" + value[2:]
    return value


def record_event(event, engine):
    target = os.environ.get("AGENT_ACTIVITY_FILE")
    if not target or not isinstance(event, dict):
        return
    data = json.dumps({"recorded_at": time.time(), "engine": engine,
                       "event": safe_event(event)}, ensure_ascii=False) + "\n"
    try:
        fd = os.open(native_path(target), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            payload = data.encode("utf-8")
            while payload:
                payload = payload[os.write(fd, payload):]
        finally:
            os.close(fd)
    except OSError:
        # Optional diagnostic capture must never invalidate the provider's answer.
        pass


def compact(value, limit=200):
    if isinstance(value, (dict, list)):
        value = json.dumps(safe_event(value), ensure_ascii=False, separators=(",", ":"))
    text = " ".join(redact(value).split())
    return text[:limit] + ("..." if len(text) > limit else "")


def tool_entry(name, args, result=None):
    args = args if isinstance(args, dict) else {"args": args}
    lower = name.lower()
    command = args.get("command") or args.get("cmd")
    path = args.get("file_path") or args.get("filePath") or args.get("path") or args.get("filename")
    if command or lower in ("bash", "shell", "exec_command", "command_execution"):
        detail = compact(command or args)
        exit_code = None
        if isinstance(result, dict):
            exit_code = result.get("exit_code", result.get("exitCode"))
            metadata = result.get("metadata") or {}
            if exit_code is None and isinstance(metadata, dict):
                exit_code = metadata.get("exit", metadata.get("exit_code"))
        if exit_code is not None:
            detail += " (exit %s)" % exit_code
        return "command", detail
    if path and any(word in lower for word in ("read", "edit", "write", "create", "patch")):
        kind = "read" if "read" in lower else "created" if any(word in lower for word in ("write", "create")) else "edited"
        return kind, compact(path)
    return "tool", compact(name + " " + compact(args))


def digest(event):
    """Yield (kind, short description) for supported engine events; never reasoning text."""
    if not isinstance(event, dict):
        return
    tag = event.get("type") or event.get("role") or ""
    if tag in ("error", "turn.failed", "retry", "retrying") or "error" in event:
        err = event.get("error") or event.get("message") or event.get("part") or tag
        yield "retry" if "retry" in tag else "error", compact(err)
        return
    if tag in ("turn.completed", "step_finish", "result"):
        part = event.get("part") or {}
        usage = event.get("usage") or part.get("tokens") or part.get("usage")
        if event.get("is_error"):
            yield "error", compact(event.get("result") or event.get("subtype"))
        yield "end", "turn ended" + (" usage=" + compact(usage) if usage else "")
        return
    if tag in ("item.started", "item.updated", "item.completed"):
        item = event.get("item") or {}
        itype = item.get("type", "")
        if itype == "command_execution":
            yield tool_entry(itype, item, item)
        elif itype == "file_change":
            for change in item.get("changes") or []:
                action = change.get("kind", "edited")
                yield "created" if action in ("add", "create") else "edited", compact(change.get("path", ""))
        elif itype == "agent_message":
            yield "text", compact(item.get("text", ""))
        elif itype in ("reasoning", "thinking"):
            yield "thinking", "thinking"
        elif "tool" in itype:
            yield tool_entry(item.get("tool", itype), item.get("arguments") or item.get("input") or {}, item)
        return
    if tag == "tool_use":
        part = event.get("part") or {}
        state = part.get("state") or {}
        yield tool_entry(part.get("tool", "tool"), state.get("input") or {}, state)
        return
    if tag in ("reasoning", "thinking"):
        yield "thinking", "thinking"
        return
    if tag == "text":
        yield "text", compact((event.get("part") or {}).get("text", event.get("text", "")))
        return
    if tag == "stream_event":
        nested = event.get("event") or {}
        block = nested.get("content_block") or nested.get("delta") or {}
        btype = block.get("type", "")
        if btype in ("thinking", "thinking_delta", "reasoning_delta"):
            yield "thinking", "thinking"
        elif btype == "text_delta":
            yield "text", compact(block.get("text", ""))
        elif btype == "tool_use":
            yield tool_entry(block.get("name", "tool"), block.get("input") or {})
        return
    if tag in ("assistant", "user") or event.get("role") in ("assistant", "tool"):
        message = event.get("message") or event
        content = message.get("content") or []
        if isinstance(content, str):
            if event.get("role") == "tool":
                yield "tool", compact(event.get("name", "tool") + " " + content)
            elif not message.get("tool_calls"):
                yield "text", compact(content)
        else:
            for block in content:
                btype = block.get("type", "") if isinstance(block, dict) else ""
                if btype == "text":
                    yield "text", compact(block.get("text", ""))
                elif btype in ("thinking", "reasoning"):
                    yield "thinking", "thinking"
                elif btype == "tool_use":
                    yield tool_entry(block.get("name", "tool"), block.get("input") or {})
                elif btype == "tool_result":
                    yield "error" if block.get("is_error") else "tool", compact("tool result " + str(block.get("content", "")))
        for call in message.get("tool_calls") or []:
            fn = call.get("function") or call
            args = fn.get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    pass
            yield tool_entry(fn.get("name", "tool"), args)


def event_time(record, fallback):
    value = record.get("recorded_at") or record.get("timestamp") or record.get("time")
    if isinstance(value, (int, float)):
        return value / 1000 if value > 100000000000 else value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return fallback


def source_path(log):
    sidecar = log.with_suffix(".activity.jsonl")
    return sidecar if sidecar.exists() else log


def read_tail_snapshot(path, size=TAIL_BYTES):
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            end = handle.tell()
            start = max(0, end - size)
            handle.seek(start)
            if start:
                handle.readline()
            return handle.read(max(0, end - handle.tell())).decode("utf-8", "replace").splitlines(), path.stat().st_mtime, end
    except OSError:
        return [], time.time(), 0


def read_tail(path, size=TAIL_BYTES):
    lines, stamp, _offset = read_tail_snapshot(path, size)
    return lines, stamp


def entries(lines, fallback, raw=False):
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            if line.strip():
                kind = "retry" if "RETRY" in line else "error" if "ERROR" in line else "text"
                yield fallback, kind, compact(line) if not raw else redact(line), False
            continue
        if not isinstance(record, dict):
            continue
        stamp = event_time(record, fallback)
        if "kind" in record and "detail" in record:
            yield stamp, record["kind"], redact(record["detail"]), True
            continue
        event = record.get("event") if "recorded_at" in record else record
        if raw:
            yield stamp, "raw", json.dumps(safe_event(event), ensure_ascii=False), True
        else:
            for kind, detail in digest(event):
                yield stamp, kind, detail, True


def settled(log):
    try:
        meta = dict(line.split("=", 1) for line in log.with_suffix(".meta").read_text(encoding="utf-8").splitlines() if "=" in line)
    except OSError:
        return True
    return meta.get("state", "") not in ("running", "idle")


def summary(log_path):
    """Cheap import API for the GUI: {'kind': str, 'age_sec': int}, or {}."""
    path = source_path(Path(log_path))
    lines, fallback = read_tail(path)
    recent = deque(entries(lines, fallback), maxlen=1)
    if not recent:
        try:
            if path.stat().st_size:
                return {"kind": "activity", "age_sec": max(0, int(time.time() - fallback))}
        except OSError:
            pass
        return {}
    stamp, kind, _detail, _structured = recent[0]
    return {"kind": kind, "age_sec": max(0, int(time.time() - stamp))}


def render(entry):
    stamp, kind, detail, _structured = entry
    age = max(0, int(time.time() - stamp))
    return "[-%ss] %-8s %s" % (age, kind, detail)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Readable, redacted CLI-agent activity")
    parser.add_argument("log", type=Path)
    parser.add_argument("-n", type=int, default=25)
    parser.add_argument("-f", "--follow", action="store_true")
    parser.add_argument("--raw", action="store_true", help="redacted structured events")
    parser.add_argument("--summary", action="store_true", help="cheap tail: kind|age in seconds")
    opts = parser.parse_args(argv)
    if opts.n < 1:
        parser.error("-n must be positive")
    source = source_path(opts.log)
    if opts.summary:
        latest = summary(opts.log)
        if latest:
            print("%s|%s" % (latest["kind"], latest["age_sec"]))
        return 0
    lines, fallback, offset = read_tail_snapshot(source, 524288)
    recent = deque(entries(lines, fallback, opts.raw), maxlen=opts.n)
    if recent and not any(entry[3] for entry in recent):
        print("[plain-text provider log; structured activity unavailable]")
    for entry in recent:
        print(render(entry), flush=True)
    if not opts.follow:
        return 0
    pending = b""
    while True:
        newest = source_path(opts.log)
        if newest != source:
            source, offset, pending = newest, 0, b""
        try:
            with source.open("rb") as handle:
                if source.stat().st_size < offset:
                    offset, pending = 0, b""
                handle.seek(offset)
                chunk = handle.read()
                offset = handle.tell()
            combined = pending + chunk
            pieces = combined.split(b"\n")
            pending = pieces.pop()
            for entry in entries((part.decode("utf-8", "replace") for part in pieces), time.time(), opts.raw):
                print(render(entry), flush=True)
        except OSError:
            pass
        if settled(opts.log):
            # Drain once more after the state changes, covering a final append before finish.
            try:
                with source.open("rb") as handle:
                    handle.seek(offset)
                    final = pending + handle.read()
                for entry in entries(final.decode("utf-8", "replace").splitlines(), time.time(), opts.raw):
                    print(render(entry), flush=True)
            except OSError:
                pass
            return 0
        time.sleep(0.2)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BrokenPipeError:
        raise SystemExit(0)
