import os
import sys
import time
from neoxider_agents.providers import BaseProvider


class Provider(BaseProvider):
    engine = "fixture"
    supports_resume = True
    retry_limit = 1

    def retry_reason(self, output, code, session=""):
        return "fixture transient" if os.environ.get("FIXTURE_RETRY") == "1" and code == 42 else ""

    def command(self, model, effort, cwd, session="", chat_only=False):
        if os.environ.get("FIXTURE_MISSING_CLI") == "1":
            raise ValueError("fixture CLI not found")
        if os.environ.get("AGENT_DETACHED") == "1":
            time.sleep(float(os.environ.get("FIXTURE_START_DELAY", "0")))
        return [sys.executable, os.environ["FIXTURE_SCRIPT"], session or "ses_fixture_" + str(os.getpid())], {}
