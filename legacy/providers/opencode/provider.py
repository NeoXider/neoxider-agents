"""OpenCode aliases, chat-only profile and bounded transient retry policy."""
import os
import re
from neoxider_agents.providers import BaseProvider, CONFLICT, FAILURE, ROOT, TRANSIENT, executable


class Provider(BaseProvider):
    engine = "opencode"

    @property
    def retry_limit(self):
        value = os.environ.get("AGENT_OPENCODE_RETRIES", "3")
        return int(value) if value.isdigit() else 3

    def resolve(self, model="", effort=""):
        aliases = {"free": "muse-spark-1.3-contributor-free", "spark": "muse-spark-1.3-contributor-free",
                   "muse": "muse-spark-1.3-contributor-free", "muse-spark": "muse-spark-1.3-contributor-free",
                   "ox": "x-preview-f-free", "ox-alpha": "x-preview-f-free", "alpha": "x-preview-f-free",
                   "pickle": "big-pickle", "big-pickle": "big-pickle", "hy3": "hy3-free", "mimo": "mimo-v2.5-free",
                   "nemotron": "nemotron-3-ultra-free", "ultra": "nemotron-3-ultra-free",
                   "lightning": "nemotron-3.5-lightning-free", "nemotron-fast": "nemotron-3.5-lightning-free"}
        return ("opencode/" + aliases[model] if model in aliases else model), effort

    def command(self, model, effort, cwd, session="", chat_only=False):
        chat_only = chat_only or os.environ.get("AGENT_CHAT_ONLY") == "1"
        args = executable(self.engine) + ["run", "--auto", "--format", "json", "--print-logs", "--log-level", "ERROR"]
        if model:
            args += ["-m", model]
        if effort:
            args += ["--variant", effort]
        if session:
            args += ["-s", session]
        env = {"AGENT_OPENCODE_WRAPPED": "1"}
        if chat_only:
            path = ROOT / "providers/opencode/chat-only.json"
            if not path.is_file():
                raise ValueError("bundled OpenCode chat-only config is unavailable")
            env["OPENCODE_CONFIG"] = str(path)
            args += ["--agent", "neoxider-chat-only"]
            for image in os.environ.get("AGENT_OPENCODE_IMAGE_PATHS", "").splitlines():
                if image and os.path.isfile(image):
                    args += ["-f", image]
        if (os.environ.get("AGENT_OPENCODE_KEEP_SMALL_MODEL") != "1" and not os.environ.get("OPENCODE_CONFIG_CONTENT")
                and re.fullmatch(r"[A-Za-z0-9._:/-]+", model)):
            env["OPENCODE_CONFIG_CONTENT"] = '{"small_model":"%s"}' % model
        return args, env

    def retry_reason(self, text, code, session=""):
        if code in (0, 124, 125, 126) or FAILURE.search(text) or CONFLICT.search(text):
            return ""
        return "transient provider/network failure" if TRANSIENT.search(text) else ""
