"""Core integration gates with real local provider processes; no network/model probes."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from neoxider_agents import orchestrator as sdk
from neoxider_agents.state import Store, Lock, atomic_write
from neoxider_agents.events import EventStore, EventCorruption
from neoxider_agents.recovery import RecoveryStore, RecoveryError
from neoxider_agents.runtime_profiles import RuntimeProfiles, RuntimeProfileError
from neoxider_agents.process import pid_alive
from neoxider_agents.diagnostics import VerificationReceipt, source_hash, CostFact

OUTPUT = Path('C:/AI/outputs/neoxider-parity-core-20261014') if os.name == 'nt' else Path(tempfile.gettempdir()) / 'neoxider-core'

PROVIDER = '''import os, sys
from neoxider_agents.providers import BaseProvider
class Provider(BaseProvider):
    engine = "codex"
    supports_resume = True
    def command(self, model, effort, cwd, session="", chat_only=False):
        return [sys.executable, SCRIPT, session or "ses_integration"], {}
'''

SCRIPT = r'''import json, os, sys, time
from pathlib import Path
work = Path.cwd()
prompt = sys.stdin.buffer.read().decode('utf-8')
counter = work / 'turns'
turn = int(counter.read_text()) + 1 if counter.exists() else 1
counter.write_text(str(turn))
(work / ('turn.%s.prompt' % turn)).write_text(prompt, encoding='utf-8')
(work / ('turn.%s.env' % turn)).write_text(json.dumps(dict(os.environ)), encoding='utf-8')
print(json.dumps({'type': 'thread.started', 'thread_id': sys.argv[1]}), flush=True)
(work / ('turn.%s.ready' % turn)).write_text(str(os.getpid()))
if 'BLOCK' in prompt:
    end = time.monotonic() + 60
    while not (work / ('turn.%s.release' % turn)).exists():
        if time.monotonic() >= end:
            sys.exit(99)
        time.sleep(0.025)
errors = {'ERR:OOM': 'RangeError: Out of memory', 'ERR:TLS': 'TLS certificate verify failed',
          'ERR:QUOTA': 'HTTP 429 rate limit exceeded', 'ERR:AUTH': 'HTTP 401 unauthorized',
          'ERR:NETWORK': 'ECONNREFUSED network error'}
if 'RETRY_RECOVER' in prompt or 'RETRY_NATIVE_EXIT' in prompt:
    print(json.dumps({'type': 'error', 'message': 'Reconnecting... 5/5 (stream disconnected before completion: tls handshake eof)'}), flush=True)
    time.sleep(0.2)
    if 'RETRY_NATIVE_EXIT' in prompt:
        sys.exit(7)
for marker, error in errors.items():
    if marker in prompt:
        print(json.dumps({'type': 'error', 'message': error}), flush=True)
        sys.exit(126)
if 'EMPTY' not in prompt:
    print(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'ANSWER ' + prompt}}), flush=True)
if 'REPORTED_COST' in prompt:
    print(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 3, 'output_tokens': 2}, 'total_cost_usd': 0}), flush=True)
'''


class CoreHarness(unittest.TestCase):
    def setUp(self):
        OUTPUT.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='integration-', dir=str(OUTPUT))
        self.base = Path(self.temp.name)
        self.logs = self.base / 'logs'
        self.work = self.base / 'work'
        self.work.mkdir()
        self.script = self.base / 'provider.py'
        self.script.write_text(SCRIPT, encoding='utf-8')
        self.providers = self.base / 'providers'
        (self.providers / 'codex').mkdir(parents=True)
        (self.providers / 'codex' / 'provider.py').write_text('SCRIPT = ' + repr(str(self.script)) + '\n' + PROVIDER, encoding='utf-8')
        self.env = dict(AGENT_PROVIDER_DIR=str(self.providers), AGENT_CONFIG=str(self.base / 'absent-config'),
                        AGENT_RETRIES='0', AGENT_TIMEOUT_SEC='60', AGENT_SILENCE_SEC='0',
                        PYTHONDONTWRITEBYTECODE='1', AGENT_PROGRESS='0', AGENT_KEEP_LOGS='0', AGENT_NOTIFY='0')
        self.clients = []

    def tearDown(self):
        for client in self.clients:
            client.close(detach=False)
        end = time.monotonic() + 5
        while True:
            try:
                self.temp.cleanup()
                break
            except PermissionError:
                if time.monotonic() > end:
                    raise
                time.sleep(0.05)

    def client(self, **kwargs):
        client = sdk.AgentClient(logs_root=self.logs, parent='integration-parent', env=self.env,
                                 poll=0.025, publish_timeout=10, control_timeout=10, **kwargs)
        self.clients.append(client)
        return client

    def spawn(self, prompt='hello', **kwargs):
        client = kwargs.pop('client', None) or self.client()
        name = kwargs.pop('name', 'core-' + uuid.uuid4().hex[:8])
        handle = client.spawn(prompt, engine='codex', name=name, cwd=self.work, **kwargs)
        return client, handle

    def until(self, predicate, seconds=10):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.025)
        self.fail('fixture deadline exceeded')

    def ready(self, client, handle, turn=1):
        self.until(lambda: (self.work / ('turn.%s.ready' % turn)).exists() and
                   client.store.read(handle.task).get('provider_pid') and client.status(handle).session)

    def release(self, turn=1):
        (self.work / ('turn.%s.release' % turn)).touch()


class CoreIntegrationTests(CoreHarness):
    def test_reconnect_recovery_publishes_current_answer_and_retains_session_after_reaping(self):
        client, handle = self.spawn('RETRY_RECOVER')
        result = handle.wait(timeout=15)
        self.assertEqual(result.state, 'done')
        self.assertEqual(result.exit_code, 0)
        self.assertIn('ANSWER RETRY_RECOVER', result.answer)
        events = client.events(handle).events
        self.assertTrue(any(event['kind'] == 'terminal' and event['data']['state'] == 'done' for event in events))
        self.until(lambda: not client._children)
        self.assertTrue(client.store.session(handle.task))
        self.assertEqual(client.store.session(handle.task), result.session)
        self.assertTrue(client.store.path(handle.task, '.answer').exists())

    def test_exhausted_reconnect_keeps_native_exit_and_unknown_delivery_without_replay(self):
        client, handle = self.spawn('BLOCK')
        self.ready(client, handle)
        receipt = handle.send('RETRY_NATIVE_EXIT')
        self.release()
        result = handle.wait(timeout=15)
        self.assertEqual(result.state, 'error')
        self.assertEqual(result.exit_code, 7)
        self.assertEqual(result.answer, '')
        self.assertEqual(result.failure['category'], 'tls')
        self.assertFalse(result.failure['blocking'])
        self.assertFalse(result.limit)
        self.assertTrue(client.receipt(handle, receipt.receipt_id).accepted)
        self.assertFalse(client.receipt(handle, receipt.receipt_id).delivered)
        self.assertFalse(client.receipt(handle, receipt.receipt_id).acknowledged)
        self.assertEqual(len(client.store.inbox(handle.task)), 1)
        self.assertEqual((self.work / 'turns').read_text(), '2')
        events = client.events(handle).events
        self.assertTrue(any(event['kind'] == 'delivery_unknown' for event in events))
        self.assertTrue(any(event['kind'] == 'terminal' and event['data']['exit_code'] == '7' for event in events))
        self.until(lambda: not client._children)
        self.assertTrue(client.store.session(handle.task))

    def test_stable_uuid_queue_and_explicit_ack_across_generation(self):
        client, handle = self.spawn('BLOCK')
        self.ready(client, handle)
        identifier = uuid.uuid4().hex
        receipt = handle.send('UNICODE Привет', message_id=identifier)
        duplicate = handle.send('UNICODE Привет', message_id=identifier)
        self.assertEqual((receipt.receipt_id, receipt.sequence), (identifier, duplicate.sequence))
        self.assertTrue(receipt.accepted)
        self.assertFalse(receipt.delivered)
        with self.assertRaises(ValueError):
            handle.send('changed payload', message_id=identifier)
        self.assertEqual(len(client.receipts(handle)), 1)
        generation = handle.generation
        self.release()
        result = handle.wait(timeout=15)
        self.assertEqual(result.state, 'done')
        self.assertTrue(client.receipt(handle, identifier).delivered)
        self.assertFalse(client.receipt(handle, identifier).acknowledged)
        with self.assertRaises(ValueError):
            client.acknowledge(handle, identifier, 'tool claimed applied', source='tool')
        self.assertTrue(client.acknowledge(handle, identifier, 'assistant explicitly confirms receipt').acknowledged)
        client.resume(handle, message='next turn')
        self.assertEqual(handle.wait(timeout=15).state, 'done')
        self.assertNotEqual(handle.generation, generation)
        self.assertTrue(client.receipt(handle, identifier).acknowledged)
        self.assertEqual(client.store.session(handle.task), result.session)

    def test_control_failure_keeps_receipt_queryable_without_new_id_retry(self):
        client, handle = self.spawn()
        handle.wait(timeout=15)
        identifier = uuid.uuid4().hex
        with mock.patch.object(client, '_launch', side_effect=OSError('transport refused')):
            receipt = handle.send('durable before transport', message_id=identifier)
        self.assertTrue(receipt.accepted)
        self.assertEqual(client.receipt(handle, identifier).sequence, receipt.sequence)
        self.assertEqual(len(client.store.inbox(handle.task)), 1)
        self.assertEqual(len(client.store.receipts(handle.task)), 1)
        retried = handle.send('durable before transport', message_id=identifier)
        self.assertEqual(retried.receipt_id, identifier)
        self.assertEqual(handle.wait(timeout=15).state, 'done')

    def test_corrupt_receipt_journal_never_becomes_exit_zero_completion(self):
        client, handle = self.spawn('BLOCK')
        self.ready(client, handle)
        receipt = handle.send('queued before corruption')
        path = client.store.path(handle.task, '.receipts.jsonl')
        with Lock(client.store.path(handle.task, '.inbox')):
            with path.open('ab') as out:
                out.write(b'{"invalid":"complete corrupt receipt"}\n')
        self.release()
        result = handle.wait(timeout=15)
        self.assertEqual(result.state, 'error')
        self.assertNotEqual(result.exit_code, 0)
        self.assertEqual(result.tests_status, 'not_run')
        self.assertTrue(client.store.inbox(handle.task))
        with self.assertRaises(ValueError):
            client.receipt(handle, receipt.receipt_id)

    def test_send_races_terminal_and_uses_safe_flush(self):
        client, handle = self.spawn('BLOCK')
        self.ready(client, handle)
        barrier = threading.Barrier(2)
        def send():
            barrier.wait()
            return handle.send('one exact payload', message_id='a' * 32)
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(send)
            barrier.wait()
            self.release()
            self.assertTrue(future.result(timeout=15).accepted)
        handle.wait(timeout=15)
        self.assertTrue(client.receipt(handle, 'a' * 32).delivered)
        prompts = [p.read_text(encoding='utf-8') for p in self.work.glob('turn.*.prompt')]
        self.assertEqual(sum('one exact payload' in prompt for prompt in prompts), 1)

    def test_pending_only_never_replays_unknown_attempt(self):
        client, handle = self.spawn()
        handle.wait(timeout=15)
        old = client.store.enqueue_receipt(handle.task, 'unknown external POST', message_id='b' * 32)
        path = client.store.path(handle.task, '.inbox') / ('%012d.msg' % old['sequence'])
        client.store.begin_delivery(handle.task, [path], generation=handle.generation)
        client.store.finish_delivery(handle.task, [path], generation=handle.generation, delivered=False, outcome='crash')
        new = handle.send('safe follow-up')
        result = handle.wait(timeout=15)
        self.assertEqual(result.state, 'waiting')
        self.assertTrue(client.receipt(handle, new.receipt_id).delivered)
        self.assertFalse(client.receipt(handle, old['id']).delivered)
        self.assertNotIn('unknown external POST', (self.work / 'turn.2.prompt').read_text())
        with self.assertRaises(ValueError):
            client.resume(handle, reviewed=True)
        with self.assertRaises(RecoveryError):
            client.resume(handle, reviewed=True, reviewed_report_sha='0' * 64)

    def test_isolated_retained_profile_actual_lease_and_no_parent_environment_mutation(self):
        before = dict(os.environ)
        client, handle = self.spawn('BLOCK', isolate_runtime=True, env={'AUTH_TEST_SECRET': 'allowed', 'UNAPPROVED_SECRET': 'refused'},
                                    credential_env_keys=('AUTH_TEST_SECRET',))
        self.ready(client, handle)
        meta = client.store.read(handle.task)
        lease = json.loads(meta['runtime_lease'])
        self.assertEqual(lease['generation'], handle.generation)
        self.assertTrue(lease['provider_start'])
        self.assertTrue(pid_alive(lease['provider_pid'], lease['provider_start']))
        env = json.loads((self.work / 'turn.1.env').read_text())
        self.assertEqual(env['AUTH_TEST_SECRET'], 'allowed')
        self.assertNotIn('UNAPPROVED_SECRET', env)
        self.assertEqual(env['CODEX_HOME'], str(Path(meta['runtime_profile_root']) / handle.task / 'state'))
        self.assertEqual(dict(os.environ), before)
        self.release()
        handle.wait(timeout=15)
        client.resume(handle, message='resume in same profile')
        handle.wait(timeout=15)
        resumed = json.loads((self.work / 'turn.2.env').read_text())
        self.assertEqual(resumed['CODEX_HOME'], env['CODEX_HOME'])
        profile = RuntimeProfiles(meta['runtime_profile_root']).validate(handle.task)
        self.assertEqual(profile['executable']['path'], str(Path(sys.executable).resolve()))
        self.assertTrue(Path(meta['runtime_profile_root']).joinpath(handle.task, 'manifest.json').exists())
        self.until(lambda: all(child.reaped or not child.alive() for child in client._children))

    def test_pinned_secondary_script_change_refuses_send_without_receipt_mutation(self):
        client, handle = self.spawn(isolate_runtime=True)
        handle.wait(timeout=15)
        self.script.write_text(SCRIPT + '\n# drift\n', encoding='utf-8')
        with self.assertRaises(ValueError):
            handle.send('do not enqueue')
        self.assertEqual(client.store.receipts(handle.task), [])

    def reviewed_fixture(self):
        (self.work / 'source.txt').write_text('source preimage', encoding='utf-8')
        client, handle = self.spawn(isolate_runtime=True)
        handle.wait(timeout=15)
        self.until(lambda: not client._children)
        recovery = RecoveryStore(self.logs / 'recovery')
        evidence = self.base / 'operator-review.txt'
        evidence.write_text('Operator reviewed fixture: no external operations were executed.', encoding='utf-8')
        ref = recovery.capture_reference(evidence)
        recovery.declare_effect_inventory(handle.task, [ref])
        return client, handle, recovery, ref

    def test_reviewed_resume_positive_and_stale_source_refusal(self):
        client, handle, recovery, ref = self.reviewed_fixture()
        report = client.inspect_recovery(handle)
        self.assertEqual(report['blockers'], [], json.dumps(report['blockers']))
        self.assertEqual(report['metadata']['generation'], handle.generation)
        client.resume(handle, message='reviewed next turn', reviewed=True, reviewed_report_sha=report['report_sha256'])
        self.assertEqual(handle.wait(timeout=15).state, 'done')
        self.until(lambda: not client._children)
        new_report = client.inspect_recovery(handle)
        (self.work / 'source.txt').write_text('after-review source drift')
        with self.assertRaises(RecoveryError):
            client.resume(handle, message='forbidden', reviewed=True, reviewed_report_sha=new_report['report_sha256'])
        self.assertEqual(len(client.store.receipts(handle.task)), 1)
        self.assertEqual((self.work / 'turns').read_text(), '2')

    def test_reviewed_partial_snapshot_and_unknown_external_post_refused_without_launch(self):
        client, handle, recovery, ref = self.reviewed_fixture()
        report = client.inspect_recovery(handle)
        initial_generation = handle.generation
        baseline_path = client.store.path(handle.task, '.baseline.json')
        baseline = json.loads(baseline_path.read_text())
        baseline['partial'] = True
        atomic_write(baseline_path, json.dumps(baseline))
        with self.assertRaises(RecoveryError):
            client.resume(handle, message='unsafe partial resume', reviewed=True, reviewed_report_sha=report['report_sha256'])
        baseline['partial'] = False
        atomic_write(baseline_path, json.dumps(baseline))
        intent = recovery.record_intent(handle.task, {'method': 'POST', 'url': 'fixture://external', 'replay': False})
        recovery.record_effect(handle.task, 'external-post', intent['sha256'], 'unknown')
        blocked = client.inspect_recovery(handle)
        self.assertTrue(any('execution_may_have_happened' in item for item in blocked['blockers']))
        with self.assertRaises(RecoveryError):
            client.resume(handle, message='unsafe POST replay', reviewed=True, reviewed_report_sha=blocked['report_sha256'])
        self.assertEqual(handle.generation, initial_generation)
        self.assertEqual((self.work / 'turns').read_text(), '1')
        self.assertEqual(client.store.receipts(handle.task), [])

    def test_runtime_cleanup_requires_actual_inactive_lease_and_retains_on_reap(self):
        client, handle = self.spawn('BLOCK', isolate_runtime=True)
        self.ready(client, handle)
        meta = client.store.read(handle.task)
        root = Path(meta['runtime_profile_root']) / handle.task
        with self.assertRaises((RuntimeProfileError, RuntimeError, ValueError)):
            client.cleanup_runtime(handle)
        self.assertTrue(root.exists())
        self.release()
        handle.wait(timeout=15)
        self.until(lambda: not client._children)
        self.assertTrue(root.exists())
        original_lease = client.store.read(handle.task)['runtime_lease']
        lease = json.loads(original_lease)
        lease['provider_start'] = ''
        client.store.update(handle.task, runtime_lease=json.dumps(lease))
        with self.assertRaises(RuntimeProfileError):
            client.cleanup_runtime(handle)
        client.store.update(handle.task, runtime_lease=original_lease)
        client.cleanup_runtime(handle)
        self.assertFalse(root.exists())
        self.assertTrue(client.store.session(handle.task))

    def test_unsupported_isolation_and_hard_cap_are_preflight_only(self):
        client = self.client()
        with self.assertRaises(sdk.OrchestratorError):
            client.spawn('hard cap', engine='codex', name='cap-refused', cwd=self.work, hard_cost_cap=10)
        self.assertFalse(client.store.read('cap-refused'))
        with self.assertRaises(sdk.SpawnError):
            client.spawn('unsupported', engine='gemini', name='isolation-refused', cwd=self.work, isolate_runtime=True)
        self.assertFalse(client.store.read('isolation-refused'))

    def test_failure_categories_end_to_end_quota_only_is_limited(self):
        for marker, category in [('OOM', 'provider_oom'), ('TLS', 'tls'), ('AUTH', 'auth'),
                                 ('NETWORK', 'network'), ('QUOTA', 'quota')]:
            with self.subTest(marker=marker):
                client, handle = self.spawn('ERR:' + marker, name='error-' + marker.lower())
                result = handle.wait(timeout=15)
                self.assertEqual(result.state, 'limited' if marker == 'QUOTA' else 'error')
                self.assertEqual(result.failure['category'], category)
                self.assertEqual(result.exit_code, 126)
                self.assertEqual(bool(result.limit), marker == 'QUOTA')
                events = client.events(handle).events
                self.assertTrue(any(event['kind'] == 'terminal' for event in events))

    def test_success_never_implies_tests_pass_and_caller_checks_bind_actual_source(self):
        client, handle = self.spawn('REPORTED_COST')
        result = handle.wait(timeout=15)
        self.assertEqual(result.tests_status, 'not_run')
        self.assertEqual(result.qa_status, 'not_run')
        self.assertEqual(result.cost_facts[0]['kind'], 'provider_reported')
        final_hash = source_hash(self.work, ['turns'])
        completed = subprocess.run([sys.executable, '-B', '-c', 'print("caller command ran")'], capture_output=True,
                                   **sdk.launch_kwargs())
        receipt = VerificationReceipt((sys.executable, '-B', '-c', 'print("caller command ran")'), completed.returncode,
                      completed.stdout.decode(), final_hash, 'caller', 'external_command', 'tests')
        checked = client.result(handle, verification_receipts=(receipt,), current_source_hash=final_hash,
                                cost_facts=(CostFact('catalog_estimate', 0, 'USD', 'advertised free'),))
        self.assertEqual(checked.tests_status, 'passed')
        (self.work / 'turns').write_text('source drift')
        checked = client.result(handle, verification_receipts=(receipt,), current_source_hash=source_hash(self.work, ['turns']),
                                cost_facts=(CostFact('catalog_estimate', 0, 'USD', 'advertised free'),))
        self.assertEqual(checked.tests_status, 'stale')
        self.assertEqual({fact['kind'] for fact in checked.cost_facts}, {'provider_reported', 'catalog_estimate'})

    def test_running_resume_never_returns_previous_answer_as_current_success(self):
        client, handle = self.spawn()
        self.assertIn('ANSWER', handle.wait(timeout=15).answer)
        client.resume(handle, message='BLOCK')
        self.ready(client, handle, 2)
        result = handle.result()
        self.assertFalse(result.settled)
        self.assertEqual(result.answer, '')
        self.assertIsNone(result.exit_code)
        self.release(2)
        handle.wait(timeout=15)

    def test_cli_stable_message_id_list_and_assistant_evidence(self):
        client, handle = self.spawn('BLOCK')
        self.ready(client, handle)
        environment = client._child_env()
        identifier = uuid.uuid4().hex
        def cli(*args):
            return subprocess.run([sys.executable, '-B', str(ROOT / 'agent.py')] + list(args), env=environment,
                                  capture_output=True, timeout=15, **sdk.launch_kwargs())
        self.assertEqual(cli('send', handle.task, '--message-id', identifier, 'from CLI').returncode, 0)
        duplicate = cli('reply', handle.task, '--message-id', identifier, 'from CLI')
        self.assertEqual(duplicate.returncode, 0, duplicate.stderr.decode())
        listed = cli('receipt', handle.task, '--list', '--json')
        self.assertEqual(len(json.loads(listed.stdout)), 1)
        self.assertNotEqual(cli('receipt', handle.task, identifier, '--ack', '--source', 'tool', '--evidence', 'fake').returncode, 0)
        self.release()
        handle.wait(timeout=15)
        ack = cli('receipt', handle.task, identifier, '--ack', '--evidence', 'Assistant confirms received')
        self.assertEqual(ack.returncode, 0, ack.stderr.decode())
        self.assertEqual(json.loads(ack.stdout)['state'], 'acknowledged')


if __name__ == '__main__':
    unittest.main()
