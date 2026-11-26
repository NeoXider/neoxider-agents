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

RECONNECT = 'Reconnecting... 2/5 (stream disconnected before completion: websocket closed by server before response.completed)'
NETWORK_WAIT = 'Reconnecting... waiting for network (Connection failed: error sending request)'
CHILD = r'''
import json, pathlib, sys, time
mode = sys.argv[1]
sys.stdin.read()
def emit(event):
    print(json.dumps(event), flush=True)
emit({"type": "thread.started", "thread_id": "controlled-retry-session"})
message = sys.argv[2]
if mode == "last":
    message = message.replace("2/5", "5/5")
if mode == "network_wait":
    message = sys.argv[3]
if mode == "nonzero":
    message = "Reconnecting... 5/5 (quota exceeded)"
if mode == "quota":
    message = "429 usage limit: quota exceeded"
fatal = {"oom": "RangeError: Out of memory", "tls": "tls handshake eof",
         "auth": "HTTP 401 authentication failed"}
if mode in fatal:
    message = fatal[mode]
quote = 'AGENT_PROVIDER_ERROR: quota exceeded; TLS EOF; authentication failed; Out of memory'
if mode == "tool_quote":
    emit({"type": "item.completed", "item": {"type": "command_execution", "aggregated_output": quote}})
elif mode == "assistant_quote":
    emit({"type": "item.completed", "item": {"type": "agent_message", "text": quote}})
else:
    emit({"type": "error", "message": message})
if mode == "hang" or mode in fatal:
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
        return [sys.executable, '-c', CHILD, self.mode, RECONNECT, NETWORK_WAIT], {}


class CodexRetrySignalTests(unittest.TestCase):
    def setUp(self):
        fixture_root = Path(tempfile.gettempdir()) / 'codex-retry-fixtures'
        fixture_root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=str(fixture_root))
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.store = Store(self.work / 'state')
        self.store.update('fixture', state='running', generation='controlled-generation', engine='codex', dir=str(self.work))

    def run_child(self, mode, deadline='15'):
        with mock.patch.dict(os.environ, AGENT_TIMEOUT_SEC=deadline, AGENT_SILENCE_SEC='0'), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            turn = Turn(self.store, 'fixture', FixtureProvider(mode), 'fixture', 'high', str(self.work), '', 'controlled prompt')
            self.last_turn = turn
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
        self.assertIn('[codex] retry: ' + RECONNECT, log)

    def test_reconnect_does_not_disable_existing_deadline(self):
        code, parser, action, reason, by = self.run_child('hang', deadline='0.3')
        self.assertEqual(code, 124, (action, reason, by))
        self.assertEqual(parser.provider_error, '')
        self.assertIn('timeout', reason)
        self.assertFalse((self.work / 'recovered.receipt').exists())

    def test_waiting_for_network_keeps_child_alive_until_success(self):
        code, parser, action, reason, by = self.run_child('network_wait')
        self.assertEqual(code, 0, (action, reason, by))
        self.assertEqual(parser.provider_error, '')
        self.assertEqual(parser.pending_network_wait, '')
        self.assertEqual(parser.last_assistant, 'RECOVERED_OK')
        log = self.store.path('fixture', '.log').read_text(encoding='utf-8')
        self.assertIn('[codex] retry: ' + NETWORK_WAIT, log)
        self.assertNotIn('AGENT_PROVIDER_ERROR:', log)

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
        for message in (RECONNECT, NETWORK_WAIT):
            with self.subTest(message=message):
                parser = OutputFilter('codex')
                rendered = parser.feed(json.dumps({'type': 'turn.failed', 'error': {'message': message}}) + '\n')
                self.assertEqual(parser.provider_error, message)
                self.assertIn('AGENT_PROVIDER_ERROR:', ''.join(rendered))

    def test_numbered_reconnects_are_nonfatal_regardless_of_cause(self):
        messages = [RECONNECT, RECONNECT.replace('2/5', '1/5'), RECONNECT.replace('2/5', '5/5'), NETWORK_WAIT,
                    'Reconnecting... 2/5 (request timed out)',
                    'Reconnecting... 1/5 (stream disconnected before completion: request timed out)',
                    'Reconnecting... 3/5 (stream disconnected before completion: connection reset by peer)',
                    'Reconnecting... 4/5 (error sending request for url (https://chatgpt.example/backend))',
                    'Reconnecting... 2/5 (stream disconnected before completion: idle timeout waiting for SSE)',
                    'Reconnecting... 2/5 (stream disconnected before completion: Этот хост неизвестен. (os error 11001))',
                    'Reconnecting... 1/5 (error sending request for url: dns error: failed to lookup address information)',
                    'Reconnecting... 3/5 (stream disconnected before completion: No such host is known. (os error 11001))',
                    'Reconnecting... 2/5 (Selected model is at capacity. Please try a different model.)',
                    'Reconnecting... 1/5 (stream disconnected before completion: service unavailable)',
                    'Reconnecting... 2/5 (quota exceeded: tls handshake eof)',
                    'Reconnecting... 2/5 (authentication failed: tls handshake eof)',
                    'Reconnecting... 2/5 (context window exceeded: tls handshake eof)',
                    'Reconnecting... 2/5 (unknown problem)',
                    'Reconnecting... 2/5 (request timed out: usage limit reached)',
                    'Reconnecting... 2/5 (stream disconnected before completion: 429 Too Many Requests)',
                    'Reconnecting... 2/5 (usage limit reached (os error 5))']
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
            'Reconnecting... 6/5 (request timed out)',
            'Reconnecting... waiting for network',
            'Reconnecting... waiting for network (unknown problem)',
            'Reconnecting... waiting for network (Connection failed: error sending request: authentication failed)',
            'Reconnecting... waiting for network (Connection failed: error sending request: HTTP 401)',
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

    def test_genuine_fatal_boundaries_keep_typed_metadata_and_reap_provider(self):
        from neoxider_agents.process import pid_alive
        for mode, category in [('oom', 'provider_oom'), ('tls', 'tls'), ('auth', 'auth')]:
            with self.subTest(mode=mode):
                code, parser, action, reason, by = self.run_child(mode)
                self.assertEqual(code, 126, (action, reason, by))
                self.assertEqual(action, 'failure')
                self.assertTrue(parser.provider_error)
                self.assertEqual(self.last_turn.failure.category, category)
                self.assertEqual(self.last_turn.failure.source, 'provider_stream')
                self.assertTrue(self.last_turn.failure.blocking)
                self.assertFalse((self.work / 'recovered.receipt').exists())
                self.assertFalse(pid_alive(self.last_turn.tree.pid, self.last_turn.tree.stamp))
                self.assertIsNotNone(self.last_turn.tree.process.poll())
                self.assertEqual(self.store.session('fixture'), 'controlled-retry-session')

    def test_tool_and_assistant_error_quotes_remain_nonfatal(self):
        for mode in ('tool_quote', 'assistant_quote'):
            with self.subTest(mode=mode):
                code, parser, action, reason, by = self.run_child(mode)
                self.assertEqual(code, 0, (action, reason, by))
                self.assertEqual(parser.provider_error, '')
                self.assertIsNone(self.last_turn.failure)
                self.assertEqual(parser.last_assistant, 'RECOVERED_OK')
                self.assertEqual((self.work / 'recovered.receipt').read_text(), 'RECOVERED_OK')


if __name__ == '__main__':
    unittest.main()


class WorkspaceRoutingDiscoveryTests(unittest.TestCase):
    def test_routing_discovery_reconnect_is_a_transient_notice(self):
        from neoxider_agents.output import _codex_retry_notice
        from neoxider_agents.providers import transport_retry_reason
        for reason in ("workspace routing discovery timed out", "workspace routing discovery failed"):
            self.assertTrue(_codex_retry_notice("Reconnecting... 2/5 (%s)" % reason))
            self.assertTrue(transport_retry_reason("Reconnecting... 2/5 (%s)" % reason))
