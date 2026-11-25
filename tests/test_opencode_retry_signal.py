import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from neoxider_agents.diagnostics import classify_failure

RETRY = {"type": "retry", "timestamp": 1795584015425, "sessionID": "ses_x", "messageID": "msg_x", "attempt": 2,
         "nextRetryAt": 1795584017451,
         "error": {"type": "provider.transport", "message": "ECONNRESET: The socket connection was closed unexpectedly."}}


class OpenCodeRetrySignalTests(unittest.TestCase):
    def classify(self, event):
        return classify_failure(json.dumps(event), source="provider_stream", trusted=True)

    def test_announced_retry_does_not_block(self):
        self.assertFalse(self.classify(RETRY).blocking)

    def test_retry_without_schedule_or_beyond_budget_still_blocks(self):
        no_schedule = dict(RETRY); no_schedule.pop("nextRetryAt")
        self.assertTrue(self.classify(no_schedule).blocking)
        exhausted = dict(RETRY, attempt=9)
        self.assertTrue(self.classify(exhausted).blocking)
        self.assertTrue(self.classify(dict(RETRY, attempt=True)).blocking)

    def test_terminal_error_event_still_blocks(self):
        error = {"type": "error", "sessionID": "ses_x", "error": {"type": "provider.transport", "message": "ECONNRESET"}}
        self.assertTrue(self.classify(error).blocking)


if __name__ == "__main__":
    unittest.main()
