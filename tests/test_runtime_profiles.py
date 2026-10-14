"""Filesystem/process evidence only; no live provider or quota claims."""
import hashlib
import json
import os
from pathlib import Path
import contextlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from neoxider_agents import runtime_profiles as rp

CHILD = r'''
import sys
sys.path.insert(0, sys.argv[1])
from neoxider_agents.runtime_profiles import RuntimeProfiles, RuntimeProfileError
try:
    RuntimeProfiles(sys.argv[2]).create(sys.argv[3], 'opencode', sys.argv[4])
except RuntimeProfileError:
    sys.exit(7)
'''
WRITER = r'''
import os, sqlite3, sys, time
from pathlib import Path
p = Path(os.environ['XDG_DATA_HOME']) / 'opencode'
p.mkdir(exist_ok=True)
db = sqlite3.connect(str(p / 'opencode.db'))
db.execute('create table if not exists sessions (id text primary key)')
db.execute('insert into sessions values (?)', (sys.argv[1],))
time.sleep(.1)
db.commit()
print(db.execute('select id from sessions').fetchall())
db.close()
'''


class Profiles(unittest.TestCase):
    def test_package_module_location(self):
        self.assertEqual(Path(rp.__file__).resolve(),
                         ROOT / 'neoxider_agents' / 'runtime_profiles.py')
        self.assertFalse((ROOT / 'runtime_profiles.py').exists())

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / 'profiles'
        self.profiles = rp.RuntimeProfiles(self.root)
        self.exe = self.base / ('fixture.exe' if os.name == 'nt' else 'fixture')
        self.exe.write_bytes(b'fixture provider version 1')

    def create(self, task='task', engine='opencode', **kwargs):
        return self.profiles.create(task, engine, self.exe, **kwargs)

    def popen(self, args, **kwargs):
        hidden = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
        p = subprocess.Popen([sys.executable, '-B'] + args, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, **hidden, **kwargs)
        def stop():
            if p.poll() is None:
                p.kill()
            p.communicate(timeout=5)
        self.addCleanup(stop)
        return p

    def test_resume_and_environment_no_secrets_or_mutation(self):
        original = dict(os.environ)
        base = {'PATH': 'launch-path', 'HOME': 'original-home', 'OPENAI_API_KEY': 'SENTINEL-SECRET',
                'OTHER_TOKEN': 'DROP-SECRET', 'OPENCODE_DB': 'wrong.db',
                'XDG_DATA_HOME': 'wrong-home'}
        before = dict(base)
        m = self.create(version='1.2', credential_env_keys=('OPENAI_API_KEY',))
        env = self.profiles.environment('task', base)
        self.assertEqual(env['OPENAI_API_KEY'], 'SENTINEL-SECRET')
        self.assertNotIn('OTHER_TOKEN', env)
        self.assertNotIn('OPENCODE_DB', env)
        self.assertEqual(env['OPENCODE_DISABLE_MODELS_FETCH'], '1')
        self.assertEqual(m['version']['provenance'], 'supplied-unverified')
        marker = Path(m['paths']['data']) / 'session.marker'
        marker.write_text('session-123')
        resumed = rp.RuntimeProfiles(self.root)
        self.assertEqual(resumed.environment('task', base), env)
        self.assertEqual(marker.read_text(), 'session-123')
        self.assertEqual(len(set(m['paths'].values())), 4)
        self.assertEqual(base, before)
        self.assertEqual(dict(os.environ), original)
        for f in self.root.rglob('*'):
            if f.is_file():
                self.assertNotIn(b'SENTINEL-SECRET', f.read_bytes())
                self.assertNotIn(b'DROP-SECRET', f.read_bytes())

    def test_executable_changed_missing_relative_or_linked(self):
        self.create()
        self.exe.write_bytes(b'changed')
        with self.assertRaises(rp.RuntimeProfileError):
            self.profiles.load('task')
        self.exe.unlink()
        with self.assertRaises(rp.RuntimeProfileError):
            self.profiles.environment('task')
        with self.assertRaises(rp.RuntimeProfileError):
            self.profiles.create('other', 'opencode', 'relative.exe')
        self.profiles.cleanup('task')  # explicit destruction permits binary drift

    def test_manifest_tamper(self):
        self.create()
        manifest = self.root / 'task' / 'manifest.json'
        data = json.loads(manifest.read_text())
        data['paths']['data'] = str(self.base)
        manifest.write_text(json.dumps(data))
        with self.assertRaises(rp.RuntimeProfileError):
            self.profiles.load('task')
        with self.assertRaises(rp.RuntimeProfileError):
            self.profiles.cleanup('task')
        self.assertTrue(manifest.exists())

    def test_live_cleanup_and_destroy_invalidates_resume(self):
        self.create()
        with self.assertRaises(rp.RuntimeProfileError):
            self.profiles.cleanup('task', live=True)
        self.profiles.cleanup('task')
        with self.assertRaises(rp.RuntimeProfileError):
            self.profiles.load('task')
        self.assertTrue((self.root / '.runtime-owner').exists())

    def test_hardlink_cleanup_preflight_preserves_all(self):
        self.create()
        outside = self.base / 'outside'
        outside.write_text('must survive')
        os.link(str(outside), str(self.root / 'task' / 'data' / 'alias'))
        with self.assertRaises(rp.RuntimeProfileError):
            self.profiles.cleanup('task')
        self.assertEqual(outside.read_text(), 'must survive')
        self.assertTrue((self.root / 'task' / 'manifest.json').exists())
        (self.root / 'task' / 'data' / 'alias').unlink()

    def test_hardlinked_executable_and_seed_refused(self):
        alias = self.base / 'exe-alias'
        os.link(str(self.exe), str(alias))
        with self.assertRaises(rp.RuntimeProfileError):
            self.create()
        alias.unlink()
        source = self.base / 'public.json'
        source.write_text('{}')
        os.link(str(source), str(alias))
        with self.assertRaises(rp.RuntimeProfileError):
            self.create(seed_files={'config/opencode/opencode.json': source})

    def test_private_permission_failure_aborts(self):
        self.profiles._root(create=True)
        with mock.patch.object(rp, '_private', side_effect=PermissionError('ACL denied')):
            with self.assertRaises(PermissionError):
                self.create()
        self.assertFalse((self.root / 'task').exists())

    def test_symlink_root_and_cleanup(self):
        self.create()
        outside = self.base / 'outside-dir'
        outside.mkdir()
        sentinel = outside / 'sentinel'
        sentinel.write_text('survives')
        link = self.root / 'task' / 'data' / 'alias'
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest('OS cannot create symlinks: %s' % exc)
        try:
            with self.assertRaises(rp.RuntimeProfileError):
                self.profiles.cleanup('task')
            with self.assertRaises(rp.RuntimeProfileError):
                rp.RuntimeProfiles(link).create('bad', 'opencode', self.exe)
            self.assertEqual(sentinel.read_text(), 'survives')
        finally:
            link.unlink()

    @unittest.skipUnless(os.name == 'nt', 'Windows junction acceptance')
    def test_windows_junction_resume_and_cleanup(self):
        import _winapi
        self.create()
        outside = self.base / 'junction-target'
        outside.mkdir()
        (outside / 'sentinel').write_text('survives')
        junction = self.root / 'task' / 'data' / 'alias'
        _winapi.CreateJunction(str(outside), str(junction))
        try:
            with self.assertRaises(rp.RuntimeProfileError):
                self.profiles.load('task')
            with self.assertRaises(rp.RuntimeProfileError):
                self.profiles.cleanup('task')
            self.assertEqual((outside / 'sentinel').read_text(), 'survives')
        finally:
            junction.rmdir()

    def test_unsupported_engines_unsafe_tasks_and_unowned_root(self):
        for engine in ('claude', 'gemini', 'kimi', 'unknown'):
            with self.assertRaises(rp.RuntimeProfileError):
                self.create(engine=engine)
        for task in ('../escape', '.', '/abs', '', 'a/b', 'CON', 'LPT1', 'con.v2',
                     'AUX.config', 'com9.v2', 'foo.', 'foo ', 'x' * 129):
            with self.assertRaises(rp.RuntimeProfileError):
                self.create(task=task)
        self.root.mkdir()
        with self.assertRaises(rp.RuntimeProfileError):
            self.create()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_legacy_dotted_and_128_character_names(self):
        for name in ('foo.v2', 'foo..v2', 'f' * 128):
            manifest = self.create(name)
            self.assertEqual(manifest['task'], name)
            self.assertEqual(self.profiles.load(name), manifest)
            self.profiles.cleanup(name)

    def test_case_alias_refused_without_touching_original(self):
        m = self.create('Foo.v2')
        with self.assertRaises(rp.RuntimeProfileError):
            self.create('foo.v2')
        with self.assertRaises(rp.RuntimeProfileError):
            self.profiles.load('foo.v2')
        self.assertEqual(self.profiles.load('Foo.v2'), m)
        self.profiles.cleanup('Foo.v2')
        self.create('foo.v2')  # advisory guard does not reserve a destroyed name

    def test_multiprocess_case_alias_collision(self):
        children = [self.popen(['-c', CHILD, str(ROOT), str(self.root), name, str(self.exe)])
                    for name in ('Foo.v2', 'foo.v2')]
        evidence = [p.communicate(timeout=15) for p in children]
        self.assertEqual(sorted(p.returncode for p in children), [0, 7], evidence)
        winner = ('Foo.v2', 'foo.v2')[[p.returncode for p in children].index(0)]
        self.profiles.load(winner)

    def test_create_base_env_deprecated_and_not_retained(self):
        source = {'OPENAI_API_KEY': 'NOT-RETAINED'}
        with self.assertWarnsRegex(DeprecationWarning, 'deprecated and unused'):
            self.create(base_env=source)
        self.assertEqual(source, {'OPENAI_API_KEY': 'NOT-RETAINED'})
        self.assertNotIn(b'NOT-RETAINED', (self.root / 'task' / 'manifest.json').read_bytes())

    def test_actual_version_probe_receipt_and_tamper(self):
        import platform
        self.profiles.create('probe', 'opencode', sys.executable, version='supplied')
        manifest = (self.root / 'probe' / 'manifest.json').read_bytes()
        self.assertIsNone(self.profiles.version_receipt('probe'))
        receipt = self.profiles.probe_version('probe', base_env={'OPENAI_API_KEY': 'DO-NOT-PASS'})
        self.assertEqual(receipt['version'], platform.python_version())
        self.assertEqual(receipt['provenance'], 'observed-executable-output')
        self.assertEqual(receipt['executable']['sha256'], hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest())
        self.assertEqual(self.profiles.version_receipt('probe'), receipt)
        self.assertEqual((self.root / 'probe' / 'manifest.json').read_bytes(), manifest)
        with self.assertRaises(rp.RuntimeProfileError):
            self.profiles.probe_version('probe')
        target = self.root / 'probe' / 'version-receipt.json'
        envelope = json.loads(target.read_text())
        envelope['receipt']['version'] = 'fake'
        target.write_text(json.dumps(envelope))
        with self.assertRaises(rp.RuntimeProfileError):
            self.profiles.load('probe')

    def test_probe_timeout_overflow_nonzero_and_no_credentials(self):
        original_popen = subprocess.Popen
        for i, script in enumerate(("import time; time.sleep(10)",
                                    "import sys; sys.stdout.write('x'*100000); sys.stdout.flush()",
                                    "import sys; sys.exit(5)")):
            task = 'probe-%s' % i
            self.profiles.create(task, 'opencode', sys.executable,
                                 credential_env_keys=('OPENAI_API_KEY',))
            launched = []
            def launch(argv, **kwargs):
                self.assertEqual(argv, [sys.executable, '--version'])
                self.assertFalse(kwargs['shell'])
                self.assertNotIn('OPENAI_API_KEY', kwargs['env'])
                if os.name == 'nt':
                    self.assertTrue(kwargs['creationflags'] & subprocess.CREATE_NO_WINDOW)
                child = original_popen([sys.executable, '-B', '-c', script], **kwargs)
                launched.append(child)
                return child
            with mock.patch.object(rp.subprocess, 'Popen', side_effect=launch):
                with self.assertRaises(rp.RuntimeProfileError):
                    self.profiles.probe_version(task, timeout=.3, max_bytes=64,
                                                base_env={'OPENAI_API_KEY': 'SENTINEL'})
            self.assertTrue(launched)
            self.assertIsNotNone(launched[0].poll())
            self.assertFalse((self.root / task / 'version-receipt.json').exists())

    def test_public_seed_allowlist_and_hash(self):
        source = self.base / 'public.json'
        source.write_text('{"model":"opencode/public"}')
        m = self.create(seed_files={'config/opencode/opencode.json': source})
        self.assertEqual(m['seeds']['config/opencode/opencode.json'], hashlib.sha256(source.read_bytes()).hexdigest())
        self.assertEqual(source.read_text(), '{"model":"opencode/public"}')
        with self.assertRaises(rp.RuntimeProfileError):
            self.create('bad', seed_files={'data/opencode/auth.json': source})
        source.write_text('{"apiKey":"SENTINEL-SECRET"}')
        with self.assertRaises(rp.RuntimeProfileError):
            self.create('bad', seed_files={'config/opencode/opencode.json': source})
        target = self.root / 'task' / 'config' / 'opencode' / 'opencode.json'
        target.write_text('{}')
        with self.assertRaises(rp.RuntimeProfileError):
            self.profiles.load('task')

    def test_bad_config_policy_seed_refused_before_root_or_task_creation(self):
        source = self.base / 'seed.json'
        source.write_text(json.dumps({'tools': {'bash': True}, 'permission': {'bash': 'allow'}}))
        with self.assertRaises(rp.RuntimeProfileError):
            self.create('bad', seed_files={'config/opencode/opencode.json': source})
        self.assertFalse(self.root.exists())
        self.create('existing')
        owner = (self.root / '.runtime-owner').read_bytes()
        manifest = (self.root / 'existing' / 'manifest.json').read_bytes()
        before = set(self.root.iterdir())
        for payload in ({'ToOlS': {'bash': True}},
                        {'provider': {'public': {'Permission': {'bash': 'allow'}}}},
                        {'model': {'metadata': {'hooks': ['run']}}},
                        {'model': 'opencode/{ENV:API_KEY}'},
                        {'model': 'opencode/public', 'small_model': {'agent': 'runner'}},
                        {'provider': {'id': 'public', 'name': 'public metadata'}},
                        {'api_key': 'SENTINEL'}, {'model': 'opencode/public bash allow'}):
            with self.subTest(payload=payload):
                source.write_text(json.dumps(payload))
                with self.assertRaises(rp.RuntimeProfileError):
                    self.create('bad', seed_files={'config/opencode/opencode.json': source})
                self.assertEqual(set(self.root.iterdir()), before)
                self.assertEqual((self.root / '.runtime-owner').read_bytes(), owner)
                self.assertEqual((self.root / 'existing' / 'manifest.json').read_bytes(), manifest)
                self.assertFalse((self.root / 'bad').exists())

    def test_catalog_policy_and_credential_fields_nested_caseforms_refused(self):
        self.create('existing')
        owner = (self.root / '.runtime-owner').read_bytes()
        before = set(self.root.iterdir())
        source = self.base / 'seed.json'
        fields = ('tools', 'permission', 'permissions', 'agent', 'agents', 'system',
                  'prompt', 'hooks', 'subagent', 'auth', 'env', 'mcp', 'plugin',
                  'command', 'apiKey', 'access_token', 'tool_policy', 'per-mis_sions',
                  'Ｔｏｏｌｓ', 'to\u200bols')
        for key in fields:
            for spelling in (key, key.upper()):
                with self.subTest(key=spelling):
                    source.write_text(json.dumps({'models': [{'id': 'public', 'metadata':
                                                  {spelling: {'bash': 'allow'}}}]}))
                    with self.assertRaises(rp.RuntimeProfileError):
                        self.create('bad', seed_files={'cache/opencode/models.json': source})
                    self.assertEqual(set(self.root.iterdir()), before)
                    self.assertEqual((self.root / '.runtime-owner').read_bytes(), owner)
                    self.assertFalse((self.root / 'bad').exists())

    def test_public_model_catalog_metadata_and_selectors_accepted(self):
        source = self.base / 'seed.json'
        payload = {'models': [{'id': 'public', 'modeltoolcapability': True,
                   'tool_call': True, 'max_tokens': 32768, 'tokenizer': 'public-tokenizer',
                   'agentic': True, 'prompt_price': 0.1, 'authorization_supported': True}]}
        source.write_text(json.dumps(payload))
        for task, engine, destination in (('open', 'opencode', 'cache/opencode/models.json'),
                                          ('code', 'codex', 'state/models.json')):
            with self.subTest(engine=engine):
                manifest = self.create(task, engine, seed_files={destination: source})
                self.assertEqual((self.root / task / destination).read_bytes(), source.read_bytes())
                self.assertEqual(self.profiles.load(task), manifest)
        source.write_text(json.dumps({'model': 'opencode/public', 'small_model': 'provider/small.v2'}))
        self.create('config', seed_files={'config/opencode/opencode.json': source})

    def test_duplicate_nonfinite_and_disguised_capability_policy_refused(self):
        source = self.base / 'seed.json'
        for raw in ('{"metadata":{"tools":{"bash":true}},"metadata":{}}',
                    '{"max_tokens":NaN}',
                    '{"models":[{"modeltoolcapability":{"bash":"allow"}}]}',
                    '{"models":[{"description":"{EnV:API_KEY}"}]}',
                    '{"models":[{"description":"{file:/private/auth.json}"}]}',
                    '{"models":[{"description":"${API_KEY}"}]}'):
            with self.subTest(raw=raw):
                source.write_text(raw)
                with self.assertRaises(rp.RuntimeProfileError):
                    self.create('bad', seed_files={'cache/opencode/models.json': source})
                self.assertFalse(self.root.exists())

    def test_codex_private_home_no_auth_copy(self):
        m = self.create(engine='codex')
        env = self.profiles.environment('task', {'CODEX_HOME': 'global', 'OPENAI_API_KEY': 'not-selected'})
        self.assertEqual(env, {'CODEX_HOME': m['paths']['state']})
        self.assertFalse(list(self.root.rglob('auth.json')))

    def test_disk_failure_rolls_back_reservation(self):
        self.profiles._root(create=True)
        with mock.patch.object(rp, '_write', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.create()
        self.assertFalse((self.root / 'task').exists())
        self.create()  # failure did not claim successful retention

    def test_multiprocess_collision_and_disjoint_creations(self):
        args = ['-c', CHILD, str(ROOT), str(self.root), 'same', str(self.exe)]
        children = [self.popen(args) for _ in range(2)]
        evidence = [p.communicate(timeout=15) for p in children]
        self.assertEqual(sorted(p.returncode for p in children), [0, 7], evidence)
        self.profiles.load('same')
        children = [self.popen(['-c', CHILD, str(ROOT), str(self.root), name, str(self.exe)])
                    for name in ('first', 'second')]
        evidence = [p.communicate(timeout=15) for p in children]
        self.assertEqual([p.returncode for p in children], [0, 0], evidence)
        self.assertNotEqual(self.profiles.load('first')['paths'], self.profiles.load('second')['paths'])

    def test_concurrent_sqlite_writers_and_retained_resume(self):
        for name in ('first', 'second'):
            self.create(name)
        children = [self.popen(['-c', WRITER, name], env=self.profiles.environment(name))
                    for name in ('first', 'second')]
        evidence = [p.communicate(timeout=15) for p in children]
        self.assertEqual([p.returncode for p in children], [0, 0], evidence)
        self.assertEqual(evidence[0][0].strip(), b"[('first',)]")
        self.assertEqual(evidence[1][0].strip(), b"[('second',)]")
        import sqlite3
        for name in ('first', 'second'):
            env = rp.RuntimeProfiles(self.root).environment(name)
            with contextlib.closing(sqlite3.connect(str(Path(env['XDG_DATA_HOME']) / 'opencode' / 'opencode.db'))) as db:
                self.assertEqual(db.execute('select id from sessions').fetchall(), [(name,)])


if __name__ == '__main__':
    unittest.main()
