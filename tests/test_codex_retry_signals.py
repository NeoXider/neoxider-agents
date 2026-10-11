"""Real Codex-shaped streams through the existing watchdog, without API calls."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from neoxider_agents.output import OutputFilter
from neoxider_agents.runtime import Turn
from neoxider_agents.state import Store

RECONNECT = 'Reconnecting... 2/5 (stream disconnected before completion: tls handshake eof)'
CHILD = r'''
import json, pathlib, sys, time
mode = sys.argv[1]
sys.stdin.read()
def emit(event):
    print(json.dumps(event), flush=True)
emit({"type": "thread.started", "thread_id": "controlled-retry-session"})
message = "Reconnecting... 2/5 (stream disconnected before completion: tls handshake eof)"
if mode == "last":
    message = message.replace("2/5", "5/5")
if mode == "quota":
    message = "429 usage limit: quota exceeded"
emit({"type": "error", "message": message})
if mode == "hang":
    time.sleep(10)
else:
    time.sleep(0.6)
    if mode == "nonzero":
        raise SystemExit(7)
    if mode == "terminal":
        emit({"type": "turn.failed", "error": {"message": "all reconnect attempts exhausted: tls handshake eof"}})
        time.sleep(0.6)
        raise SystemExit(7)
    pathlib.Path("recovered.receipt").write_text("RECOVERED_OK", encoding="utf-8")
    emit({"type": "item.completed", "item": {"type": "agent_message", "text": "RECOVERED_OK"}})
    emit({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}})
'''


class FixtureProvider:
    engine = 'codex'

    def __init__(self, mode):
        self.mode = mode

    def command(self, model, effort, cwd, session='', chat_only=False):
        return [sys.executable, '-c', CHILD, self.mode], {}


class CodexRetrySignalTests(unittest.TestCase):
    def setUp(self):
        fixture_root = Path('D:/Temp/agents-ux/codex-retry-fixtures') if os.name == 'nt' else Path(tempfile.gettempdir()) / 'codex-retry-fixtures'
        fixture_root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=str(fixture_root))
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.store = Store(self.work / 'state')
        self.store.update('fixture', state='running', generation='controlled-generation', engine='codex', dir=str(self.work))

    def run_child(self, mode, deadline='3'):
        with mock.patch.dict(os.environ, AGENT_TIMEOUT_SEC=deadline, AGENT_SILENCE_SEC='0'), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            turn = Turn(self.store, 'fixture', FixtureProvider(mode), 'fixture', 'high', str(self.work), '', 'controlled prompt')
            try:
                return turn.run()
            finally:
                if turn.tree is not None:
                    turn.tree.process.stdout.close()  # This fixture owns the returned, reaped child pipe.

    def test_reconnect_then_normal_answer_keeps_child_alive_until_success(self):
        code, parser, action, reason, by = self.run_child('recover')
        self.assertEqual(code, 0, (action, reason, by))
        self.assertEqual(parser.provider_error, '')
        self.assertEqual(parser.last_assistant, 'RECOVERED_OK')
        self.assertEqual((self.work / 'recovered.receipt').read_text(encoding='utf-8'), 'RECOVERED_OK')
        log = self.store.path('fixture', '.log').read_text(encoding='utf-8')
        self.assertNotIn('AGENT_PROVIDER_ERROR:', log)
        self.assertIn('Reconnecting', log)

    def test_reconnect_does_not_disable_existing_deadline(self):
        code, parser, action, reason, by = self.run_child('hang', deadline='0.3')
        self.assertEqual(code, 124, (action, reason, by))
        self.assertEqual(parser.provider_error, '')
        self.assertIn('timeout', reason)
        self.assertFalse((self.work / 'recovered.receipt').exists())

    def test_last_announced_retry_can_still_recover_with_real_final_answer(self):
        code, parser, action, reason, by = self.run_child('last')
        self.assertEqual(code, 0, (action, reason, by))
        self.assertEqual(parser.provider_error, '')
        self.assertEqual(parser.last_assistant, 'RECOVERED_OK')
        self.assertEqual((self.work / 'recovered.receipt').read_text(encoding='utf-8'), 'RECOVERED_OK')

    def test_quota_event_still_stops_before_child_can_claim_success(self):
        code, parser, action, reason, by = self.run_child('quota')
        self.assertEqual(code, 126)
        self.assertIn('quota exceeded', parser.provider_error)
        self.assertFalse((self.work / 'recovered.receipt').exists())

    def test_exhausted_retries_nonzero_exit_cannot_become_success_or_quota(self):
        code, parser, action, reason, by = self.run_child('nonzero')
        self.assertEqual(code, 7, (action, reason, by))
        self.assertEqual(parser.provider_error, '')
        self.assertEqual(parser.final_failure(code), '')
        self.assertFalse((self.work / 'recovered.receipt').exists())

    def test_terminal_turn_failure_remains_fatal_after_retry(self):
        code, parser, action, reason, by = self.run_child('terminal')
        self.assertEqual(code, 126)
        self.assertIn('exhausted', parser.provider_error)
        self.assertFalse((self.work / 'recovered.receipt').exists())
        parser = OutputFilter('codex')
        rendered = parser.feed(json.dumps({'type': 'turn.failed', 'error': {'message': RECONNECT}}) + '\n')
        self.assertEqual(parser.provider_error, RECONNECT)
        self.assertIn('AGENT_PROVIDER_ERROR:', ''.join(rendered))

    def test_only_well_formed_transport_reconnect_status_is_nonfatal(self):
        messages = [RECONNECT, RECONNECT.replace('2/5', '1/5'), RECONNECT.replace('2/5', '5/5')]
        for message in messages:
            with self.subTest(message=message):
                parser = OutputFilter('codex')
                rendered = parser.feed(json.dumps({'type': 'error', 'message': message}) + '\n')
                self.assertEqual(parser.provider_error, '')
                self.assertNotIn('AGENT_PROVIDER_ERROR:', ''.join(rendered))
                self.assertIn('Reconnecting', ''.join(rendered))

    def test_permanent_unknown_and_malformed_error_shapes_remain_fatal(self):
        messages = [
            '429 usage limit', 'authentication failed', 'context window exceeded',
            'tls handshake eof', 'Reconnecting... 0/5 (stream disconnected before completion: tls handshake eof)',
            'Reconnecting... 6/5 (stream disconnected before completion: tls handshake eof)',
            'Reconnecting... 2/0 (stream disconnected before completion: tls handshake eof)',
            'Reconnecting... 2/5 (quota exceeded: tls handshake eof)',
            'Reconnecting... 2/5 (authentication failed: tls handshake eof)',
            'Reconnecting... 2/5 (context window exceeded: tls handshake eof)',
            'Reconnecting... 2/5 (unknown problem)',
        ]
        for message in messages:
            with self.subTest(message=message):
                parser = OutputFilter('codex')
                rendered = parser.feed(json.dumps({'type': 'error', 'message': message}) + '\n')
                self.assertEqual(parser.provider_error, message)
                self.assertIn('AGENT_PROVIDER_ERROR:', ''.join(rendered))

    def test_opencode_error_is_not_reclassified(self):
        parser = OutputFilter('opencode')
        rendered = parser.feed(json.dumps({'type': 'error', 'message': RECONNECT}) + '\n')
        self.assertEqual(parser.provider_error, RECONNECT)
        self.assertIn('AGENT_PROVIDER_ERROR:', ''.join(rendered))


if __name__ == '__main__':
    unittest.main()
