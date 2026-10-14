"""Source-compiled recovery tests; isolated files/processes, no provider/API calls."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock


SOURCE = Path(__file__).absolute().parents[1] / "neoxider_agents" / "recovery.py"
CAPTURED_SOURCE = SOURCE.read_bytes()
SOURCE_SHA = hashlib.sha256(CAPTURED_SOURCE).hexdigest()
namespace = {"__file__": str(SOURCE), "__name__": "captured_recovery"}
exec(compile(CAPTURED_SOURCE, str(SOURCE), "exec"), namespace)
R = SimpleNamespace(**namespace)
CHILD = r'''
import os, sys, time
from pathlib import Path
p = Path(sys.argv[1])
raw = p.read_bytes()
ns = {'__file__': str(p), '__name__': 'captured_recovery'}
exec(compile(raw, str(p), 'exec'), ns)
store = ns['RecoveryStore'](sys.argv[2])
mode = sys.argv[3]
if mode == 'race':
    for i in range(5):
        store.record_intent('task', {'worker': sys.argv[4], 'iteration': i})
elif mode == 'crash':
    store.record_intent('task', {'durable_before_death': True})
    with ns['exclusive_lock'](store.root / '.lock'):
        print('UNFILTERED fixture output', flush=True)
        os._exit(23)
elif mode == 'owner':
    proof = ns['_owner']({'pid': str(os.getpid())})
    Path(sys.argv[4]).write_text(str(os.getpid()) + '\n' + proof['creation'], encoding='utf-8')
    print('ready', flush=True)
    time.sleep(60)
elif mode == 'read':
    print(len(store.history()), flush=True)
'''


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.task_root = self.base / 'tasks'
        self.source = self.base / 'source'
        self.profiles_root = self.base / 'profiles'
        for path in (self.task_root, self.source, self.profiles_root):
            path.mkdir()
        self.store = SimpleNamespace(root=self.task_root)
        self.profiles = SimpleNamespace(root=self.profiles_root)
        self.recovery = R.RecoveryStore(self.base / 'recovery')
        self.original = b'initial source\n'
        (self.source / 'a.txt').write_bytes(self.original)
        digest = R._sha(self.original)
        self.write('task.baseline.json', R._json({'files': {'a.txt': {'sha256': digest,
                   'size': len(self.original), 'type': 'file', 'content': digest}}}))
        (self.task_root / 'task.baseline.files').mkdir()
        self.write('task.baseline.files/' + digest, self.original)
        self.write('task.original.prompt', 'Original Юникод prompt'.encode('utf-8'))
        self.write('task.answer', b'Unfiltered answer [agent-activity] retained\n')
        self.write('task.log', b'---------- output ----------\n[agent-activity] raw\n')
        (self.task_root / 'task.inbox').mkdir()
        self.write('task.inbox/000000000001.msg', 'queued Юникод'.encode('utf-8'))
        self.receipt = {'schema': 1, 'task': 'task', 'id': '1' * 32, 'sequence': 1, 'rev': 1,
                        'state': 'accepted', 'delivery_status': 'pending',
                        'digest': 'sha256:' + R._sha('queued Юникод'.encode('utf-8'))}
        self.write('task.receipts.jsonl', R._json(self.receipt) + b'\n')
        self.meta = {'generation': 'generation1', 'session': 'session-1', 'state': 'stopped',
                     'pid': '2147483000', 'pid_start': 'not-alive', 'engine': 'codex',
                     'dir': str(self.source), 'original_prompt': str(self.task_root / 'task.original.prompt')}
        self.metadata()
        self.exe = self.base / 'fixture-executable'
        self.exe.write_bytes(b'fixture executable bytes')
        task_profile = self.profiles_root / 'task'
        task_profile.mkdir()
        paths = {n: str(task_profile / n) for n in ('config', 'cache', 'data', 'state')}
        for path in paths.values():
            Path(path).mkdir()
        owner = {'schema': 1, 'root': str(self.profiles_root), 'id': 'fixture-root-id'}
        self.manifest = {'schema': 1, 'task': 'task', 'provider': 'codex', 'paths': paths,
                         'executable': {'path': str(self.exe), 'sha256': R._sha(self.exe.read_bytes())}, 'seeds': {}}
        (self.profiles_root / '.runtime-owner').write_bytes(R._json(owner))
        (task_profile / 'manifest.json').write_bytes(R._json(self.manifest))
        (task_profile / '.profile-owner').write_bytes(R._json({'schema': 1, 'root_id': owner['id'],
          'task': 'task', 'path': str(task_profile), 'manifest_sha256': R._sha(R._json(self.manifest))}))
        evidence = self.base / 'root-inventory-review.txt'
        evidence.write_bytes(b'Root fixture declaration: all effects journaled; no external request made')
        self.ref = self.recovery.capture_reference(evidence)
        self.recovery.declare_effect_inventory('task', [self.ref])

    def write(self, name, data):
        (self.task_root / name).write_bytes(data)

    def metadata(self):
        self.write('task.meta', ''.join('%s=%s\n' % pair for pair in self.meta.items()).encode('utf-8'))

    def inspect(self):
        return self.recovery.inspect(self.store, 'task', self.profiles)

    def authorize(self, report, fresh=None):
        return self.recovery.authorize_resume(R._sha(R._json(report)), fresh or self.inspect())

    def popen(self, mode, *args):
        hidden = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
        child = subprocess.Popen([sys.executable, '-B', '-c', CHILD, str(SOURCE),
                  str(self.recovery.root), mode] + list(args), stdout=subprocess.PIPE,
                  stderr=subprocess.PIPE, **hidden)
        def cleanup():
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=5)
        self.addCleanup(cleanup)
        return child

    def test_real_capture_delta_review_and_retention(self):
        (self.source / 'a.txt').write_bytes(b'changed source')
        report = self.inspect()
        self.assertEqual(report['blockers'], [])
        self.assertEqual(report['delta'][0]['before']['sha256'], R._sha(self.original))
        self.assertEqual(report['delta'][0]['after']['sha256'], R._sha(b'changed source'))
        for label in ('original_prompt', 'last_answer', 'retained/task.log',
                      'retained/task.receipts.jsonl', 'retained/task.inbox/000000000001.msg', 'preimage/a.txt'):
            ref = report['captures'][label]
            self.assertEqual(R._sha((self.recovery.root / ref['ref']).read_bytes()), ref['sha256'])
        raw_log = report['captures']['retained/task.log']
        self.assertIn(b'[agent-activity] raw', (self.recovery.root / raw_log['ref']).read_bytes())
        receipt = self.authorize(report)
        self.assertEqual(receipt['record']['payload']['scope'], 'review_receipt_only')
        self.assertFalse(receipt['record']['payload']['external_effect_replay'])
        self.assertEqual(self.meta['session'], 'session-1')
        self.assertTrue((self.task_root / 'task.answer').exists())
        self.assertEqual(R.RecoveryStore(self.recovery.root).history()[-1]['kind'], 'review')

    def test_live_process_crash_start_recovery_and_reused_pid(self):
        proof = self.base / 'pid.txt'
        child = self.popen('owner', str(proof))
        self.assertEqual(child.stdout.readline().strip(), b'ready')
        pid, stamp = proof.read_text().splitlines()
        self.meta.update(pid=pid, pid_start=stamp)
        self.metadata()
        live = self.inspect()
        self.assertIn('live_owner', live['blockers'])
        if live['owner']['command_available']:
            self.assertRegex(live['owner']['command_sha256'], r'^[0-9a-f]{64}$')
            self.meta['owner_command_sha256'] = '0' * 64
            self.metadata()
            self.assertIn('unverifiable', self.inspect()['blockers'])
            del self.meta['owner_command_sha256']
            self.metadata()
        with self.assertRaises(R.RecoveryError):
            self.authorize(live)
        self.meta['pid_start'] = 'reused-creation-stamp'
        self.metadata()
        reused = self.inspect()
        self.assertEqual(reused['owner']['status'], 'reused_pid')
        self.assertNotIn('live_owner', reused['blockers'])
        child.kill()
        child.communicate(timeout=5)
        self.meta['pid_start'] = stamp
        self.metadata()
        dead = self.inspect()
        self.assertEqual(dead['owner']['status'], 'dead')
        self.authorize(dead)

    def test_process_death_releases_lock_preserves_intent_and_history(self):
        child = self.popen('crash')
        out, err = child.communicate(timeout=10)
        self.assertEqual(child.returncode, 23, err)
        self.assertEqual(out, b'UNFILTERED fixture output\r\n' if os.name == 'nt' else b'UNFILTERED fixture output\n')
        raw = self.base / 'unfiltered-output'
        raw.write_bytes(out + err)
        self.assertEqual(self.recovery.capture_reference(raw)['sha256'], R._sha(out + err))
        restarted = self.popen('read')
        output, error = restarted.communicate(timeout=10)
        self.assertEqual(restarted.returncode, 0, error)
        self.assertEqual(int(output), 2)
        self.assertTrue(self.recovery.history()[-1]['payload']['durable_before_death'])

    def test_subprocess_history_races_are_ordered_and_no_overwrite(self):
        children = [self.popen('race', str(i)) for i in range(3)]
        for child in children:
            out, err = child.communicate(timeout=15)
            self.assertEqual(child.returncode, 0, (out, err))
        records = self.recovery.history()
        self.assertEqual(len(records), 16)
        self.assertEqual([r['sequence'] for r in records], list(range(1, 17)))
        self.assertEqual(len({r['id'] for r in records}), 16)
        with self.assertRaises(FileExistsError):
            R._exclusive(self.recovery.root / 'records' / (records[0]['id'] + '.json'), b'erase')

    def test_unknown_post_journal_no_retry_and_explicit_root_resolution(self):
        intent = self.recovery.record_intent('task', {'effect': 'catalogunknownWardrobePOST',
                                                    'request': 'fixture only; no network'})
        before = (self.source / 'a.txt').read_bytes()
        effect = self.recovery.record_effect('task', 'catalogunknownWardrobePOST', intent['sha256'], 'unknown')
        report = self.inspect()
        self.assertIn('execution_may_have_happened:catalogunknownWardrobePOST', report['blockers'])
        with self.assertRaises(R.RecoveryError):
            self.authorize(report)
        with self.assertRaises(R.RecoveryError):
            self.recovery.replay_guard('task', 'catalogunknownWardrobePOST')
        self.recovery.resolve_effect('task', 'catalogunknownWardrobePOST', effect['sha256'], [self.ref])
        resolved = self.inspect()
        self.assertEqual(resolved['blockers'], [])
        self.authorize(resolved)
        with self.assertRaises(R.RecoveryError):
            self.recovery.replay_guard('task', 'catalogunknownWardrobePOST')
        self.assertEqual((self.source / 'a.txt').read_bytes(), before)

    def test_intended_confirmed_unrecognized_outcome_and_regression(self):
        intent = self.recovery.record_intent('task', {'intent': 'fixture'})
        self.recovery.record_effect('task', 'effect', intent['sha256'], 'intended')
        self.assertIn('execution_may_have_happened:effect', self.inspect()['blockers'])
        for outcome in ([], ['success'], [{'sha256': '0' * 64, 'ref': '../outside', 'size': 1}]):
            with self.assertRaises(R.RecoveryError):
                self.recovery.record_effect('task', 'effect', intent['sha256'], 'confirmed', outcome)
        self.recovery.record_effect('task', 'effect', intent['sha256'], 'confirmed', [self.ref])
        self.assertEqual(self.inspect()['blockers'], [])
        with self.assertRaises(R.RecoveryError):
            self.recovery.record_effect('task', 'effect', intent['sha256'], 'unknown')
        with self.assertRaises(R.RecoveryError):
            self.recovery.record_effect('task', 'other', intent['sha256'], 'success')

    def test_changed_files_and_generation_session_revalidate(self):
        for field, value in (('generation', 'new-generation'), ('session', 'new-session')):
            report = self.inspect()
            previous = self.meta[field]
            self.meta[field] = value
            self.metadata()
            with self.assertRaises(R.RecoveryError):
                self.authorize(report)
            self.meta[field] = previous
            self.metadata()
        report = self.inspect()
        fresh = self.inspect()
        self.write('task.inbox/000000000001.msg', b'changed after fresh inspection')
        with self.assertRaises(R.RecoveryError):
            self.authorize(report, fresh)

    def test_source_change_with_restored_timestamp_rejected(self):
        report = self.inspect()
        stamp = (self.source / 'a.txt').stat()
        (self.source / 'a.txt').write_bytes(b'mutated source\n')
        os.utime(str(self.source / 'a.txt'), ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        with self.assertRaises(R.RecoveryError):
            self.authorize(report)

    def test_ambiguous_inbox_and_runtime_session_file_drift(self):
        self.receipt['delivery_status'] = 'unknown'
        self.write('task.receipts.jsonl', R._json(self.receipt) + b'\n')
        self.assertIn('inbox_execution_may_have_happened', self.inspect()['blockers'])
        self.receipt['delivery_status'] = 'pending'
        self.write('task.receipts.jsonl', R._json(self.receipt) + b'\n')
        session = self.profiles_root / 'task' / 'data' / 'session.db'
        session.write_bytes(b'session before')
        report = self.inspect()
        session.write_bytes(b'session after')
        with self.assertRaises(R.RecoveryError):
            self.authorize(report)

    def test_profile_mismatch_executable_drift_and_sourcehash_drift(self):
        report = self.inspect()
        self.exe.write_bytes(b'changed executable')
        self.assertTrue(any(b.startswith('profile_mismatch:') for b in self.inspect()['blockers']))
        with self.assertRaises(R.RecoveryError):
            self.authorize(report)
        with mock.patch.dict(namespace, {'_COMPILED_SOURCE_SHA': '0' * 64}):
            self.assertIn('inspection_source_hash_drift', self.inspect()['blockers'])
        self.assertEqual(SOURCE_SHA, R._sha(SOURCE.read_bytes()))

    def test_actual_captured_source_drift_and_profile_seal_mismatch(self):
        copied = self.base / 'captured_recovery.py'
        copied.write_bytes(CAPTURED_SOURCE)
        copied_ns = {'__file__': str(copied), '__name__': 'isolated_captured_recovery'}
        exec(compile(CAPTURED_SOURCE, str(copied), 'exec'), copied_ns)
        copied.write_bytes(CAPTURED_SOURCE + b'\n# owned fixture drift\n')
        report = copied_ns['inspect_recovery'](self.store, 'task', self.profiles, recovery_store=self.recovery)
        self.assertIn('inspection_source_hash_drift', report['blockers'])
        seal = self.profiles_root / 'task' / '.profile-owner'
        original = seal.read_bytes()
        changed = json.loads(original)
        changed['root_id'] = 'another-owner'
        seal.write_bytes(R._json(changed))
        self.assertTrue(any(b.startswith('profile_mismatch:') for b in self.inspect()['blockers']))
        self.assertEqual(SOURCE_SHA, R._sha(SOURCE.read_bytes()))

    def test_unrecognized_receipt_and_reference_utf8(self):
        self.receipt['delivery_status'] = 'success'
        self.write('task.receipts.jsonl', R._json(self.receipt) + b'\n')
        self.assertIn('unrecognized_inbox_outcome', self.inspect()['blockers'])
        with self.assertRaises((R.RecoveryError, UnicodeError)):
            self.recovery.capture_reference(str(self.base / 'unsafe-\ud800'))
        self.write('task.baseline.json', R._json({'files': {'../escape': {'sha256': '0' * 64}}}))
        self.assertIn('unsafe_baseline_reference', self.inspect()['blockers'])

    def test_actual_readonly_git_failure_keeps_unfiltered_output(self):
        # Invalid owned .git fixture invokes real Git without init/commit/index writes.
        (self.source / '.git').mkdir()
        report = self.inspect()
        commands = report['git']['commands']
        if not commands:
            self.assertIn('snapshot_partial', report['blockers'])
            return  # Git absent is an honestly partial report.
        self.assertIn('git_inspection_failed', report['blockers'])
        self.assertEqual(len(commands), 2)
        for command in commands:
            self.assertIn('--no-optional-locks', command['argv'])
            self.assertNotEqual(command['exit'], 0)
            raw = (self.recovery.root / command['stderr']['ref']).read_bytes()
            self.assertTrue(raw)
            self.assertEqual(R._sha(raw), command['stderr']['sha256'])
        self.assertFalse((self.source / '.git' / 'index').exists())

    def test_partial_snapshot_baseline_and_missing_inventory_fail_honestly(self):
        with mock.patch.dict(namespace, {'MAX_FILES': 1}):
            report = self.inspect()
            self.assertTrue(report['snapshot']['partial'])
            self.assertIn('snapshot_partial', report['blockers'])
            with self.assertRaises(R.RecoveryError):
                self.authorize(report)
        self.write('task.baseline.json', R._json({'files': {}, 'partial': True}))
        self.assertIn('baseline_partial', self.inspect()['blockers'])
        other = R.RecoveryStore(self.base / 'other-recovery')
        self.assertIn('external_inventory_unknown', other.inspect(self.store, 'task', self.profiles)['blockers'])

    def test_task_traversal_symlinks_and_corrupt_history_not_erased(self):
        for name in ('../task', 'task/other', 'NUL', 'task.'):
            with self.assertRaises(R.RecoveryError):
                self.recovery.inspect(self.store, name, self.profiles)
        try:
            (self.source / 'linked.txt').symlink_to(self.exe)
        except OSError:
            pass  # Windows without symlink privilege; junction check is structural.
        else:
            self.assertIn('snapshot_partial', self.inspect()['blockers'])
        bad = self.recovery.root / 'records' / 'torn.json'
        bad.write_bytes(b'{"record":')
        with self.assertRaises(R.RecoveryError):
            self.recovery.history()
        self.assertEqual(bad.read_bytes(), b'{"record":')

    def test_mutated_or_fabricated_report_is_not_review_authority(self):
        original = self.inspect()
        fresh = self.inspect()
        fresh['metadata']['generation'] = 'fabricated'
        with self.assertRaises(R.RecoveryError):
            self.authorize(original, fresh)
        with self.assertRaises(R.RecoveryError):
            self.recovery.authorize_resume('0' * 64, original)

    def test_review_pins_original_sha_compares_stable_projection(self):
        original = self.inspect()
        with mock.patch.object(R.time, 'time_ns', return_value=original['captured_at_ns'] + 123456789):
            fresh = self.inspect()
        original_sha, fresh_sha = [R._sha(R._json(report)) for report in (original, fresh)]
        self.assertNotEqual(original_sha, fresh_sha)
        self.assertNotEqual(original['captured_at_ns'], fresh['captured_at_ns'])
        self.assertEqual(R.resume_projection(original), R.resume_projection(fresh))
        self.assertEqual(original['binding_sha256'], fresh['binding_sha256'])
        observation = dict(fresh, captured_at_ns=0, id='observation-only',
                           owner={'observed_at_ns': 42}, snapshot={'elapsed_seconds': 5})
        self.assertEqual(R.resume_projection(fresh), R.resume_projection(observation))
        receipt = self.recovery.authorize_resume(original_sha, fresh)['record']['payload']
        self.assertEqual(receipt['reviewed_report_sha256'], original_sha)
        self.assertEqual(receipt['fresh_report_sha256'], fresh_sha)

    def test_empty_journal_never_proves_no_unobserved_effects(self):
        other = R.RecoveryStore(self.base / 'unknown-recovery')
        report = other.inspect(self.store, 'task', self.profiles)
        self.assertEqual(report['effects']['effects'], {})
        self.assertEqual(report['effects']['coverage'], 'unknown')
        self.assertEqual(report['effects']['unobserved_coverage'], 'unknown')
        self.assertFalse(report['effects']['operator_review'])
        self.assertIn('operator_review_required:unobserved_effects', report['blockers'])
        with self.assertRaises(R.RecoveryError):
            other.authorize_resume(R._sha(R._json(report)), other.inspect(self.store, 'task', self.profiles))
        evidence = other.capture_reference(self.base / 'root-inventory-review.txt')
        # A prior inventory assertion is retained, but is not operator review.
        with R.exclusive_lock(other.root / '.lock'):
            other._record('task', 'inventory', {'evidence_refs': [evidence]})
        old_declaration = other.inspect(self.store, 'task', self.profiles)
        self.assertFalse(old_declaration['effects']['operator_review'])
        self.assertIn('operator_review_required:unobserved_effects', old_declaration['blockers'])
        with self.assertRaises(R.RecoveryError):
            other.review_unobserved_effects('task', [])
        review = other.review_unobserved_effects('task', [evidence])
        reviewed = other.inspect(self.store, 'task', self.profiles)
        self.assertEqual(reviewed['effects']['coverage'], 'unknown')
        self.assertEqual(reviewed['effects']['unobserved_coverage'], 'unknown')
        self.assertTrue(reviewed['effects']['operator_review'])
        receipt = other.authorize_resume(R._sha(R._json(reviewed)),
                         other.inspect(self.store, 'task', self.profiles))['record']['payload']
        self.assertEqual(receipt['operator_review_sha256'], review['sha256'])
        self.assertEqual(receipt['coverage'], 'unknown')
        self.assertFalse(receipt['external_effect_replay'])
        intent = other.record_intent('task', {'intent': 'registered fixture'})
        other.record_effect('task', 'registered', intent['sha256'], 'intended')
        registered = other.inspect(self.store, 'task', self.profiles)
        self.assertEqual(registered['effects']['coverage'], 'explicit-journal-only')
        self.assertEqual(registered['effects']['unobserved_coverage'], 'unknown')
        self.assertIn('execution_may_have_happened:registered', registered['blockers'])
        with self.assertRaises(R.RecoveryError):
            other.replay_guard('task', 'unregistered-http-post')


if __name__ == '__main__':
    unittest.main(verbosity=2)
