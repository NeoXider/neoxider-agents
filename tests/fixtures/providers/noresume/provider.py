import os
import sys
from neoxider_agents.providers import BaseProvider


class Provider(BaseProvider):
    engine = "noresume"
    supports_resume = False

    def command(self, model, effort, cwd, session="", chat_only=False):
        return [sys.executable, os.environ["FIXTURE_SCRIPT"]], {}
