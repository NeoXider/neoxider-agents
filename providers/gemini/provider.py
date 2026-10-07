"""Gemini headless stdin execution; resume remains unsupported."""
import os
from neoxider_agents.providers import BaseProvider, executable


class Provider(BaseProvider):
    engine = "gemini"
    supports_resume = False

    def resolve(self, model="", effort=""):
        return model, ""

    def command(self, model, effort, cwd, session="", chat_only=False):
        if session:
            raise ValueError("gemini does not support resume; start a fresh run")
        args = executable(self.engine)
        if model:
            args += ["-m", model]
        args += ["--approval-mode", "plan"] if chat_only or os.environ.get("AGENT_CHAT_ONLY") == "1" else ["--yolo"]
        return args + ["-p", ""], {"GEMINI_CLI_NO_RELAUNCH": "1"}
