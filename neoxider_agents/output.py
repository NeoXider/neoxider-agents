"""Bounded in-process JSONL output filters and activity capture."""
from collections import OrderedDict, deque
import json
import os
from pathlib import Path
import time
import uuid
from .providers import failure_reason

MARK = "---------- output ----------"
LIMIT = 262144


def clean(text):
    return "\n".join(line + " " if line == MARK else line for line in str(text).split("\n"))


def error_text(value):
    if isinstance(value, dict):
        value = value.get("message") or value.get("error") or json.dumps(value, ensure_ascii=False)
    if isinstance(value, str) and value.strip().startswith("{"):
        try:
            inner = json.loads(value)
            nested = (inner.get("error") or {}).get("message") if isinstance(inner.get("error"), dict) else ""
            value = nested or inner.get("message") or value
        except (ValueError, AttributeError):
            pass
    return " ".join(str(value or "").split())


class OutputFilter:
    def __init__(self, engine, task="", logs=None):
        self.engine = engine
        self.session = ""
        self.last_assistant = ""
        self.provider_error = ""
        self.events = []
        self.had_answer = False
        self.raw = deque()
        self.raw_size = 0
        self.parts = OrderedDict()
        self.part_size = 0
        self.part_paths = OrderedDict()
        self.answers = deque()
        self.last_activity = time.monotonic()
        self.claude_delta = ""
        self.claude_streamed = False
        self.at_line_start = True
        self._finished = False
        self.answer_path = None
        self._spool_root = logs or os.environ.get("AGENT_CLI_LOGS")
        self._spool_prefix = task or os.environ.get("AGENT_TASK") or "output"
        self._spool_id = "%s-%s" % (os.getpid(), uuid.uuid4().hex)
        self._claude_new_message = True
        self.usage = {}
        self.cost = None

    def _spool(self, suffix="answer"):
        if not self._spool_root:
            self._spool_root = Path.home() / ".claude/agent-cli-logs"
        root = Path(self._spool_root)
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        name = "".join(c if c.isalnum() or c in "._-" else "_" for c in self._spool_prefix)
        path = root / (name + "." + self._spool_id + "." + suffix)
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        return path, os.fdopen(fd, "w", encoding="utf-8", newline="")

    def _set_answer(self, text):
        text = str(text)
        if self.answer_path:
            try:
                Path(self.answer_path).unlink()
            except OSError:
                pass
        self.answer_path = None
        if len(text) > LIMIT:
            path, stream = self._spool()
            with stream:
                stream.write(text)
            self.answer_path = path
        self.last_assistant = text[-LIMIT:]
        self.had_answer = bool(text)

    def _append_answer(self, text):
        if self.answer_path:
            with Path(self.answer_path).open("a", encoding="utf-8", newline="") as stream:
                stream.write(text)
            self.last_assistant = (self.last_assistant + text)[-LIMIT:]
            self.had_answer = True
        else:
            self._set_answer(self.last_assistant + text)

    def _part(self, key, text):
        if not self.part_paths and self.part_size - len(self.parts.get(key, "")) + len(text) <= LIMIT:
            self.part_size -= len(self.parts.get(key, ""))
            self.parts[key] = text
            self.part_size += len(text)
            self._set_answer("".join(self.parts.values()).strip())
            return
        if not self.part_paths:
            for previous, value in self.parts.items():
                path, stream = self._spool("part-" + str(len(self.part_paths)))
                with stream:
                    stream.write(value)
                self.part_paths[previous] = path
            self.parts.clear()
            self.part_size = 0
        if key in self.part_paths:
            path = self.part_paths[key]
            with path.open("w", encoding="utf-8", newline="") as stream:
                stream.write(text)
        else:
            path, stream = self._spool("part-" + str(len(self.part_paths)))
            with stream:
                stream.write(text)
            self.part_paths[key] = path
        chunks, remaining = [], LIMIT
        for path in reversed(self.part_paths.values()):
            with path.open("rb") as stream:
                stream.seek(0, 2)
                stream.seek(max(0, stream.tell() - remaining * 4))
                piece = stream.read(remaining * 4).decode("utf-8", "ignore")[-remaining:]
            chunks.insert(0, piece)
            remaining -= len(piece)
            if remaining <= 0:
                break
        self.last_assistant = "".join(chunks).strip()
        self.had_answer = bool(self.last_assistant)

    def _materialize_parts(self):
        path, stream = self._spool()
        with stream:
            for part in self.part_paths.values():
                with part.open("r", encoding="utf-8", newline="") as source:
                    while True:
                        block = source.read(65536)
                        if not block:
                            break
                        stream.write(block)
        self.answer_path = path
        for part in self.part_paths.values():
            part.unlink()
        self.part_paths.clear()
        self._trim_answer_file()
        self.had_answer = self._answer_bounds[1] > self._answer_bounds[0]

    def _trim_answer_file(self):
        # Find UTF-8 codepoint boundaries while trimming OpenCode's outer whitespace.
        end = self.answer_path.stat().st_size
        start = 0
        with self.answer_path.open("r", encoding="utf-8", newline="") as stream:
            while start < end:
                text = stream.read(65536)
                stripped = text.lstrip()
                if stripped:
                    start += len(text[:len(text) - len(stripped)].encode("utf-8"))
                    break
                start += len(text.encode("utf-8"))
        with self.answer_path.open("rb") as stream:
            while end > start:
                begin = max(start, end - 65536)
                stream.seek(begin)
                block = stream.read(end - begin)
                while block and block[0] & 0xC0 == 0x80:
                    begin += 1
                    block = block[1:]
                text = block.decode("utf-8")
                stripped = text.rstrip()
                if stripped:
                    end -= len(text[len(stripped):].encode("utf-8"))
                    break
                end = begin
        self._answer_bounds = (start, end)

    def _answer_chunks(self):
        # Only a possible marker prefix is buffered; giant single lines stay bounded.
        pending, at_start, result, last = "", True, [], ""
        with self.answer_path.open("rb") as raw:
            start, end = getattr(self, "_answer_bounds", (0, self.answer_path.stat().st_size))
            raw.seek(start)
            import codecs
            decoder = codecs.getincrementaldecoder("utf-8")()
            while raw.tell() < end:
                data = raw.read(min(65536, end - raw.tell()))
                for char in decoder.decode(data):
                    last = char
                    if at_start:
                        if char == "\n":
                            result.append(pending + (" " if pending == MARK else "") + char)
                            pending = ""
                        else:
                            pending += char
                            if not MARK.startswith(pending):
                                result.append(pending)
                                pending, at_start = "", False
                    else:
                        result.append(char)
                        if char == "\n":
                            at_start = True
                    if len(result) >= 16384:
                        yield "".join(result)
                        result = []
        if pending:
            result.append(pending + (" " if pending == MARK else ""))
        if last != "\n":
            result.append("\n")
        if result:
            yield "".join(result)

    def _session(self, session, output):
        if session and session != self.session:
            self.session = str(session)
            output.append("session id: " + self.session + "\n")

    def _error(self, value, output):
        message = error_text(value)
        if message:
            self.provider_error = message
            output.append("AGENT_PROVIDER_ERROR: " + message + "\n")

    def _remember(self, line):
        line = line[-LIMIT:]
        self.raw.append(line)
        self.raw_size += len(line)
        while self.raw_size > LIMIT and self.raw:
            self.raw_size -= len(self.raw.popleft())

    def _activity(self, event):
        from activity import digest
        from .logs import record_digest
        self.events = list(digest(event))
        target = os.environ.get("AGENT_ACTIVITY_FILE")
        if target:
            for kind, detail in self.events:
                record_digest(target, kind, detail, self.engine)
        tag = event.get("type", "")
        part = event.get("part") or {}
        usage = event.get("usage") or part.get("tokens") or part.get("usage")
        if isinstance(usage, dict) and tag in ("result", "turn.completed", "step_finish"):
            # Preserve provider-reported numeric counts only; no invented estimates.
            numeric = {k: v for k, v in usage.items() if isinstance(v, (int, float))}
            aliases = {"input": "input_tokens", "output": "output_tokens", "total": "total_tokens", "reasoning": "reasoning_tokens"}
            cache = usage.get("cache")
            if isinstance(cache, dict):
                for key in ("read", "write"):
                    if isinstance(cache.get(key), (int, float)):
                        numeric["cache_%s_tokens" % key] = cache[key]
            for key, value in numeric.items():
                key = aliases.get(key, key)
                self.usage[key] = self.usage.get(key, 0) + value
        cost = event.get("total_cost_usd", event.get("cost", part.get("cost")))
        if isinstance(cost, (int, float)) and tag in ("result", "turn.completed", "step_finish"):
            self.cost = (self.cost or 0) + cost

    def feed(self, line):
        self.events = []
        self._remember(line)
        output = []
        try:
            event = json.loads(line.strip())
        except ValueError:
            if self.engine == "opencode":
                output.append((line if line.startswith("[opencode] ") else "[opencode] " + line).rstrip("\r\n") + "\n")
            if self.engine in ("claude", "gemini") or self.engine not in ("codex", "kimi", "opencode"):
                session = line.strip().partition("session id: ")
                if session[1] and session[2]:
                    self.session = session[2]
                output.append(line if line.endswith("\n") else line + "\n")
                if not line.startswith(("[", "session id:", "AGENT_PROVIDER_ERROR:", MARK)):
                    self._append_answer(line)
            if line.startswith("AGENT_PROVIDER_ERROR: "):
                self.provider_error = line[len("AGENT_PROVIDER_ERROR: "):].strip()
            return output
        if not isinstance(event, dict):
            return output
        self._activity(event)
        tag = event.get("type") or event.get("role") or "event"
        if self.engine in ("codex", "kimi", "opencode") and time.monotonic() - self.last_activity >= 10:
            output.append("[%s] activity: %s\n" % (self.engine, tag))
            self.last_activity = time.monotonic()
        if self.engine == "codex":
            if tag == "thread.started":
                self._session(event.get("thread_id"), output)
            elif tag in ("item.started", "item.updated", "item.completed"):
                item = event.get("item") or {}
                if item.get("type") == "agent_message" and item.get("text") is not None:
                    self._set_answer(item["text"])
            elif tag in ("error", "turn.failed"):
                self._error(event.get("error") or event.get("message"), output)
        elif self.engine == "opencode":
            self._session(event.get("sessionID"), output)
            if tag == "text":
                part = event.get("part") or {}
                if part.get("text") is not None:
                    key = part.get("id") or "anonymous-%s" % (len(self.parts) + len(self.part_paths))
                    self._part(key, str(part["text"]))
            elif tag in ("error", "turn.failed"):
                self._error(event.get("error") or event.get("message"), output)
        elif self.engine == "kimi":
            if tag == "session.resume_hint":
                self._session(event.get("session_id"), output)
            elif event.get("role") == "assistant" and not event.get("tool_calls"):
                text = event.get("content")
                if isinstance(text, str) and text:
                    if tag == "acp.delta":
                        self._append_answer(text)
                        return output
                    if tag in ("acp.partial", "acp.final"):
                        self.answers.clear()
                        self._set_answer(text)
                    else:
                        self._append_answer(("\n" if self.had_answer else "") + text)
            elif tag == "error" or "error" in event:
                self._error(event.get("error") or event.get("message"), output)
        elif self.engine == "claude":
            self._claude(event, tag, output)
        else:
            output.append(line if line.endswith("\n") else line + "\n")
        return output

    def _claude(self, event, tag, output):
        if tag == "system" and event.get("subtype") == "init":
            self._session(event.get("session_id"), output)
        elif tag == "stream_event":
            nested = event.get("event") or {}
            if nested.get("type") == "message_start":
                self.claude_delta = ""
                self.claude_streamed = False
                self._claude_new_message = True
            elif nested.get("type") == "content_block_delta":
                delta = nested.get("delta") or {}
                if delta.get("type") == "text_delta" and delta.get("text"):
                    text = delta["text"]
                    self.claude_delta = (self.claude_delta + text)[-LIMIT:]
                    if self._claude_new_message:
                        self._set_answer(text)
                        self._claude_new_message = False
                    else:
                        self._append_answer(text)
                    self.claude_streamed = True
                    self.at_line_start = text.endswith("\n")
                    output.append(text)
        elif tag == "assistant":
            blocks = (event.get("message") or {}).get("content") or []
            text = "".join(block.get("text", "") for block in blocks if isinstance(block, dict) and block.get("type") == "text")
            if text:
                self._set_answer(text)
                if not self.claude_streamed:
                    output.append(clean(text))
                    self.at_line_start = text.endswith("\n")
            for block in blocks:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    output.append(("" if self.at_line_start else "\n") + "[agent-activity] tool " + block.get("name", "tool") + "\n")
                    self.at_line_start = True
            self.claude_streamed = False
        elif tag == "result":
            text = event.get("result") or ""
            if event.get("is_error"):
                self._error(text or event.get("subtype"), output)
            elif text:
                self._set_answer(text)
        elif tag == "error":
            self._error(event.get("error") or event.get("message"), output)

    def finish(self):
        if self._finished:
            return []
        self._finished = True
        if self.part_paths:
            self._materialize_parts()
        if self.had_answer:
            if self.answer_path:
                def replay():
                    yield ("" if self.at_line_start else "\n") + MARK + "\n"
                    for block in self._answer_chunks():
                        yield block
                return replay()
            answer = clean(self.last_assistant)
            return [("" if self.at_line_start else "\n") + MARK + "\n", answer + ("" if answer.endswith("\n") else "\n")]
        if self.provider_error:
            return [MARK + "\n", "[no agent message before the provider ended the turn: %s]\n" % self.provider_error]
        if self.engine in ("codex", "kimi", "opencode"):
            return list(self.raw)
        return []

    def final_failure(self, code):
        return self.provider_error or (failure_reason("".join(self.raw)) if code else "")

    def iter_answer(self):
        """Replay the complete clean answer without a marker or whole-file allocation."""
        if self.answer_path:
            return self._answer_chunks()
        answer = clean(self.last_assistant)
        return [answer + ("" if answer.endswith("\n") else "\n")]
