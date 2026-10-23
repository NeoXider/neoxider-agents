"""OpenCode aliases, chat-only profile and bounded transient retry policy."""
import os
import re
from neoxider_agents.providers import BaseProvider, CONFLICT, FAILURE, ROOT, TRANSIENT, executable


class Provider(BaseProvider):
    engine = "opencode"

    # Reduced-tool agent for chat-only traffic on routes whose provider gate refuses
    # tool-less sessions (OpenCode free tier answers FreeTierError to {"*": false}).
    # chat-freetier.json keeps the session tooled enough to pass the gate while still
    # forbidding shell, writes, edits, prompts, subagents and MCP execution.
    FREETIER_AGENT = "neoxider-freetier"

    FREE_TIER_ROUTE = re.compile(r"(?:^|/)(?:[A-Za-z0-9._:-]*-free|big-pickle)$")

    @classmethod
    def is_free_tier_route(cls, model):
        """True for OpenCode Zen free-tier model ids (`opencode/<name>-free`, `big-pickle`)."""
        return bool(model) and ("/" not in model or model.startswith("opencode/"))             and bool(cls.FREE_TIER_ROUTE.search(model))

    @property
    def retry_limit(self):
        value = os.environ.get("AGENT_OPENCODE_RETRIES", "3")
        return int(value) if value.isdigit() else 3

    def resolve(self, model="", effort=""):
        # Zen free tier as listed by `opencode models` on 2026-10-23; Muse Spark, x-preview and hy3
        # have left it. `free` is the owner's pick (DeepSeek flash), not a benchmark ranking.
        aliases = {"free": "deepseek-v4.1-flash-free", "deepseek": "deepseek-v4.1-flash-free",
                   "ds-flash": "deepseek-v4.1-flash-free",
                   "pickle": "big-pickle", "big-pickle": "big-pickle", "mimo": "mimo-v2.6-flash-free",
                   "nemotron": "nemotron-3-ultra-free", "ultra": "nemotron-3-ultra-free",
                   "lightning": "nemotron-3.5-lightning-free", "nemotron-fast": "nemotron-3.5-lightning-free"}
        return ("opencode/" + aliases[model] if model in aliases else model), effort

    def command(self, model, effort, cwd, session="", chat_only=False):
        chat_only = chat_only or os.environ.get("AGENT_CHAT_ONLY") == "1"
        args = executable(self.engine) + ["run", "--auto", "--format", "json", "--print-logs", "--log-level", "ERROR", "--dir", str(cwd)]
        if model:
            args += ["-m", model]
        if effort:
            args += ["--variant", effort]
        if session:
            args += ["-s", session]
        env = {"AGENT_OPENCODE_WRAPPED": "1"}
        if chat_only:
            # Routes of the OpenCode free tier refuse tool-less sessions (FreeTierError 403),
            # so chat-only traffic on them gets the reduced-tool profile automatically.
            # AGENT_OPENCODE_FREETIER_COMPAT=1 forces that profile on any route, =0 forces the
            # strict zero-tool profile everywhere. Routes behind the user's own login
            # (`opencode auth login`, OAuth/API key) are not gated and keep zero tools. Only
            # the two bundled profiles are reachable; the config path is never taken from the
            # environment, so this cannot weaken the lockdown to an arbitrary file.
            setting = os.environ.get("AGENT_OPENCODE_FREETIER_COMPAT")
            freetier = setting == "1" or (setting != "0" and self.is_free_tier_route(model))
            profile, agent = ("chat-freetier.json", self.FREETIER_AGENT) if freetier \
                else ("chat-only.json", "neoxider-chat-only")
            path = ROOT / "providers/opencode" / profile
            if not path.is_file():
                raise ValueError("bundled OpenCode chat-only config is unavailable")
            env["OPENCODE_CONFIG"] = str(path)
            args += ["--agent", agent]
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
