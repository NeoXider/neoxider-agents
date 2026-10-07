"""Kimi stdin prompt transport through its native ACP server."""
import os
import sys
from neoxider_agents.providers import BaseProvider, ROOT, capture, executable


class Provider(BaseProvider):
    engine = "kimi"

    def resolve(self, model="", effort=""):
        aliases = {"": "kimi-code/k3", "default": "kimi-code/k3", "k3": "kimi-code/k3", "kimi-k3": "kimi-code/k3",
                   "k3-256k": "kimi-code/k3-256k", "coding": "kimi-code/kimi-for-coding", "kimi-for-coding": "kimi-code/kimi-for-coding",
                   "highspeed": "kimi-code/kimi-for-coding-highspeed", "kimi-for-coding-highspeed": "kimi-code/kimi-for-coding-highspeed"}
        return aliases.get(model, model), ""

    def command(self, model, effort, cwd, session="", chat_only=False):
        args = [sys.executable, "-u", str(ROOT / "providers/kimi/acp_adapter.py"), "--cwd", str(cwd), "--model", model]
        if session:
            args += ["--session", session]
        if chat_only or os.environ.get("AGENT_CHAT_ONLY") == "1":
            args += ["--chat-only"]
        return args, {"PYTHONIOENCODING": "utf-8", "PYTHONPATH": str(ROOT)}

    def doctor(self):
        info = super().doctor()
        if info["available"]:
            import json
            _, raw = capture(executable(self.engine) + ["provider", "list", "--json"])
            try:
                data = json.loads(raw)
                configured = bool(data.get("default_model") or data.get("defaultModel") or data.get("models"))
            except (ValueError, AttributeError):
                configured = False
            info["login"] = "configured" if configured else "login required: run kimi login"
            info["note"] = "K3 default; no CLI limits endpoint."
        return info
