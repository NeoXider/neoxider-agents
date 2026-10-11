"""Claude Code command policy and alias resolution."""
import os
import re
import shlex
from neoxider_agents.providers import BaseProvider, capture, executable


class Provider(BaseProvider):
    engine = "claude"

    def resolve(self, model="", effort=""):
        match = re.fullmatch(r"(.*)-(low|medium|high|xhigh|max)", model)
        base, inferred = (match.group(1), match.group(2)) if match else (model, "")
        aliases = {"": "claude-opus-5", "default": "claude-opus-5", "opus5": "claude-opus-5",
                   "sonnet": "claude-sonnet-5", "sonnet55": "claude-sonnet-5-5",
                   "opus55": "claude-opus-5-5"}
        return aliases.get(base, base), effort or inferred or ("high" if base in ("sonnet", "sonnet55") else "")

    def command(self, model, effort, cwd, session="", chat_only=False):
        chat_only = chat_only or os.environ.get("AGENT_CHAT_ONLY") == "1"
        args = executable(self.engine) + ["-p", "--model", model]
        if effort:
            args += ["--effort", effort]
        if session:
            args += ["--resume", session]
        if chat_only:
            args += ["--permission-mode", "acceptEdits", "--strict-mcp-config", "--tools", ""]
        else:
            args += shlex.split(os.environ.get("AGENT_CLAUDE_PERMISSION") or "--dangerously-skip-permissions")
        if os.environ.get("AGENT_STREAM_TEXT") != "0":
            args += ["--output-format", "stream-json", "--include-partial-messages", "--verbose"]
        return args, {"PYTHONIOENCODING": "utf-8"}

    def doctor(self):
        info = super().doctor()
        if info["available"]:
            code, raw = capture(executable(self.engine) + ["auth", "status", "--json"])
            try:
                import json
                auth = json.loads(raw)
            except ValueError:
                auth = None
            if not isinstance(auth, dict) or not isinstance(auth.get("loggedIn"), bool):
                info.update(state="timeout" if code == 124 else "error", login="", limits=None,
                            note="Authentication status unknown: CLI probe did not return valid status")
                return info
            info["login"] = "CLI ok" if auth.get("loggedIn") else "not logged in"
            info["limits"], info["usage"], info["note"] = self._usage(info["version"], auth)
        return info

    @staticmethod
    def _usage(version, auth):
        import datetime
        import glob
        import json
        from pathlib import Path
        import time
        now = time.time()
        deadline = min(time.monotonic() + 1.5, float(os.environ.get("AGENT_DIAGNOSTIC_DEADLINE", "inf")))
        scanned_files = scanned_bytes = 0
        partial = False
        totals = {"5h": [0, 0], "7d": [0, 0]}
        for name in glob.iglob(str(Path.home() / ".claude/projects/**/*.jsonl"), recursive=True):
            if time.monotonic() >= deadline or scanned_files >= 1024 or scanned_bytes >= 16 * 1024 * 1024:
                partial = True
                break
            scanned_files += 1
            try:
                if os.path.getmtime(name) < now - 7 * 86400:
                    continue
                with open(name, "rb") as stream:
                    while True:
                        if time.monotonic() >= deadline or scanned_bytes >= 16 * 1024 * 1024:
                            partial = True
                            break
                        raw = stream.readline(min(4000001, 16 * 1024 * 1024 - scanned_bytes))
                        scanned_bytes += len(raw)
                        if not raw:
                            break
                        if len(raw) > 4000000:
                            while raw and not raw.endswith(b"\n"):
                                if time.monotonic() >= deadline or scanned_bytes >= 16 * 1024 * 1024:
                                    partial = True
                                    break
                                raw = stream.readline(min(4000001, 16 * 1024 * 1024 - scanned_bytes))
                                scanned_bytes += len(raw)
                            continue
                        if b'"usage"' not in raw or b'"assistant"' not in raw:
                            continue
                        try:
                            event = json.loads(raw)
                            stamp = datetime.datetime.fromisoformat(event["timestamp"].replace("Z", "+00:00")).timestamp()
                        except (ValueError, KeyError, TypeError):
                            continue
                        usage = (event.get("message") or {}).get("usage") or {}
                        for label, seconds in (("5h", 18000), ("7d", 604800)):
                            if stamp >= now - seconds:
                                totals[label][0] += (usage.get("input_tokens") or 0) + (usage.get("output_tokens") or 0)
                                totals[label][1] += (usage.get("cache_read_input_tokens") or 0) + (usage.get("cache_creation_input_tokens") or 0)
            except OSError:
                continue
        usage = {"source": "local_transcripts", "estimated": True, "partial": partial, "windows": [
            {"label": label, "window_minutes": minutes, "input_output_tokens": totals[label][0], "cache_tokens": totals[label][1]}
            for label, minutes in (("5h", 300), ("7d", 10080))]}
        note = "Local transcript estimate (usage so far, not a provider limit)."
        if partial:
            note += " Partial scan: observed counts only; complete usage unknown."
        try:
            import urllib.request
            creds = json.loads((Path.home() / ".claude/.credentials.json").read_text(encoding="utf-8"))
            oauth = creds.get("claudeAiOauth") or {}
            if oauth.get("accessToken") and (oauth.get("expiresAt") or 0) / 1000 > now + 30:
                request = urllib.request.Request("https://api.anthropic.com/api/oauth/usage", headers={
                    "Authorization": "Bearer " + oauth["accessToken"], "anthropic-beta": "oauth-2025-04-20",
                    "User-Agent": "claude-code/" + version.split()[0]})
                remaining = float(os.environ.get("AGENT_DIAGNOSTIC_DEADLINE", "inf")) - time.monotonic()
                if remaining <= 0:
                    return None, usage, note + " Provider usage deadline exceeded."
                with urllib.request.urlopen(request, timeout=min(5, remaining)) as response:
                    live = json.load(response)
                windows = []
                for key, label, minutes in (("five_hour", "5h", 300), ("seven_day", "7d", 10080)):
                    data = live.get(key) or {}
                    percent = data.get("utilization", data.get("used_percentage"))
                    if percent is not None:
                        windows.append({"label": label, "used_percent": float(percent), "window_minutes": minutes,
                                        "resets_at": data.get("resets_at")})
                if windows:
                    return {"source": "provider", "plan_type": oauth.get("subscriptionType") or auth.get("subscriptionType") or "?",
                            "windows": windows}, usage, "Live utilization from Claude's authenticated /api/oauth/usage endpoint."
        except (OSError, ValueError, TimeoutError):
            pass
        return None, usage, note
