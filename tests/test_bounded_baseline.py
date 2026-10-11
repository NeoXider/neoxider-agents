"""Offline bounded baseline regressions; scratch and provider state stay isolated."""
import contextlib
import io
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from neoxider_agents import reporting, lifecycle
from neoxider_agents.cli import main
from neoxider_agents.state import Store


class BoundedBaselineTests(unittest.TestCase):
    def setUp(self):
        base = Path('D:/Temp/baseline-merge/tests') if os.name == 'nt' else Path(tempfile.gettempdir()) / 'baseline-fix'
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=str(base))
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.work = self.base / 'work'
        self.work.mkdir()
        self.store = Store(self.base / 'state')
        env = mock.patch.dict(os.environ, AGENT_CLI_LOGS=self.store.root.as_posix(),
                              AGENT_BASELINE_BUDGET_SEC='20', AGENT_BASELINE_MAX_FILES='200000',
                              AGENT_BASELINE_SIZE_CAP_BYTES=str(8 * 1024 * 1024))
        env.start()
        self.addCleanup(env.stop)

    def put(self, name, data=b'content\n'):
        p = self.work / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return p

    def test_git_one_enumeration_never_walks_or_opens_ignored(self):
        (self.work / '.git').mkdir()
        self.put('source.txt')
        self.put('Library/cache.txt')
        original = Path.open
        def checked(path, *args, **kwargs):
            self.assertNotIn('Library', path.parts)
            return original(path, *args, **kwargs)
        reply = subprocess.CompletedProcess([], 0, stdout=b'source.txt\0')
        with mock.patch('subprocess.run', return_value=reply) as run, \
                mock.patch.object(reporting.os, 'walk', side_effect=AssertionError('git must not walk')), \
                mock.patch.object(Path, 'open', checked):
            result = reporting.snapshot(self.work)
        self.assertEqual(set(result), {'source.txt'})
        self.assertEqual(result.source, 'git')
        self.assertEqual(run.call_count, 1)
        command = run.call_args.args[0]
        self.assertEqual(command[-5:], ['ls-files', '-z', '--cached', '--others', '--exclude-standard'])
        self.assertGreater(run.call_args.kwargs['timeout'], 0)
        if os.name == 'nt':
            self.assertIn('creationflags', run.call_args.kwargs)

    def test_git_failure_and_timeout_use_bounded_fallback(self):
        (self.work / '.git').mkdir()
        self.put('source.txt')
        self.put('Library/cache.txt')
        for failure in (subprocess.CompletedProcess([], 1, stdout=b''), subprocess.TimeoutExpired('git', 1)):
            kw = {'side_effect': failure} if isinstance(failure, Exception) else {'return_value': failure}
            with mock.patch('subprocess.run', **kw):
                result = reporting.snapshot(self.work)
            self.assertEqual(set(result), {'source.txt'})
            self.assertEqual(result.source, 'walk')

    def test_git_listed_files_keep_fallback_only_cache_names(self):
        (self.work / '.git').mkdir()
        self.put('Library/tracked.txt')
        reply = subprocess.CompletedProcess([], 0, stdout=b'Library/tracked.txt\0')
        with mock.patch('subprocess.run', return_value=reply):
            result = reporting.snapshot(self.work)
        self.assertEqual(set(result), {'Library/tracked.txt'})

    def test_outside_git_uses_walk_without_git_process(self):
        self.put('a.txt')
        with mock.patch('subprocess.run', side_effect=AssertionError('outside git')):
            result = reporting.snapshot(self.work)
        self.assertEqual(set(result), {'a.txt'})
        self.assertEqual(result.source, 'walk')

    def test_fallback_ignores_defaults_but_keeps_plain_bin(self):
        for directory in ('Library', 'Temp', 'obj', 'Logs', '.vs', '.idea', '.gradle', '.next', 'coverage'):
            self.put(directory + '/cache.txt')
        self.put('bin/important.txt')
        original = Path.open
        def checked(path, *args, **kwargs):
            self.assertFalse(set(path.relative_to(self.work).parts) & set(reporting.IGNORED_DIRS))
            return original(path, *args, **kwargs)
        with mock.patch.object(Path, 'open', checked):
            self.assertEqual(set(reporting.snapshot(self.work)), {'bin/important.txt'})

    def test_file_limit_marks_partial_and_unseen_files_are_not_added(self):
        for n in range(8): self.put('%d.txt' % n)
        with mock.patch.dict(os.environ, AGENT_BASELINE_MAX_FILES='2'):
            reporting.begin_snapshot(self.store, 'task', self.work)
        baseline = reporting.read_baseline(self.store, 'task')
        self.assertTrue(baseline['partial'])
        self.assertIn('limit', baseline['reason'])
        out = io.StringIO()
        with contextlib.redirect_stderr(out):
            changed = reporting.changed_files(self.store, 'task', self.work)
            details = reporting.file_changes(self.store, 'task', self.work)
        self.assertEqual(changed, [])
        self.assertEqual(details, [])
        self.assertIn('baseline partial:', out.getvalue())
        self.assertIn('not tracked', out.getvalue())

    def test_zero_wall_budget_marks_partial(self):
        self.put('a.txt')
        with mock.patch.dict(os.environ, AGENT_BASELINE_BUDGET_SEC='0'):
            result = reporting.snapshot(self.work)
        self.assertTrue(result.partial)
        self.assertIn('wall time', result.reason)
        self.assertEqual(result, {})

    def test_scoped_git_never_opens_unowned_files_or_consumes_their_budget(self):
        self.put('owned/input.txt')
        for number in range(100):
            self.put('unowned/%d.dat' % number)
        candidates = ['unowned/%d.dat' % number for number in range(100)] + ['owned/input.txt']
        original = Path.open
        def checked(path, *args, **kwargs):
            self.assertNotIn('unowned', path.parts)
            return original(path, *args, **kwargs)
        with mock.patch.object(reporting, '_git_listed_files', return_value=candidates), \
                mock.patch.object(Path, 'open', checked), \
                mock.patch.dict(os.environ, AGENT_BASELINE_MAX_FILES='1'):
            result = reporting.snapshot(self.work, owns='owned/input.txt')
        self.assertEqual(set(result), {'owned/input.txt'})
        self.assertFalse(result.partial)

    def test_scoped_fallback_prunes_unrelated_subtrees_and_handles_partial_prefix(self):
        self.put('src/feature/nested/a.py')
        self.put('src/other/keep.txt')
        self.put('unowned/never.txt')
        original = reporting.os.walk
        visited = []
        def walk(*args, **kwargs):
            for parent, dirs, files in original(*args, **kwargs):
                visited.append(Path(parent).relative_to(self.work).as_posix())
                yield parent, dirs, files
        with mock.patch.object(reporting, '_git_listed_files', return_value=None), \
                mock.patch.object(reporting.os, 'walk', walk):
            result = reporting.snapshot(self.work, owns='src/fea?ure/*.py')
        self.assertEqual(set(result), {'src/feature/nested/a.py'})
        self.assertNotIn('unowned', visited)
        self.assertNotIn('src/other', visited)

    def test_scoped_snapshot_accepts_absolute_unicode_backslash_and_missing_paths(self):
        path = self.put('owned/данные.txt')
        self.put('elsewhere.txt')
        for owns in (str(path), 'owned\\данные.txt,owned/new.txt', './owned/../owned/данные.txt'):
            result = reporting.snapshot(self.work, owns=owns)
            self.assertEqual(set(result), {'owned/данные.txt'})
            self.assertFalse(result.partial)
        self.assertEqual(reporting.snapshot(self.work, owns=str(self.base / 'outside.txt')), {})

    def test_scoped_partial_counts_only_owned_candidates(self):
        for n in range(4): self.put('owned/%d.txt' % n)
        self.put('other.txt')
        candidates = ['owned/%d.txt' % n for n in range(4)] + ['other.txt']
        with mock.patch.object(reporting, '_git_listed_files', return_value=candidates), \
                mock.patch.dict(os.environ, AGENT_BASELINE_MAX_FILES='1'):
            result = reporting.snapshot(self.work, owns='owned/*.txt')
        self.assertTrue(result.partial)
        self.assertEqual(result.not_tracked, 3)

    def test_scoped_git_excludes_linked_ancestor_without_reading_outside_root(self):
        outside = self.base / 'outside'
        outside.mkdir()
        (outside / 'secret.txt').write_text('outside', encoding='utf-8')
        link = self.work / 'owned'
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError as error:
            self.skipTest('directory symlink unavailable: %s' % error)
        with mock.patch.object(reporting, '_git_listed_files', return_value=['owned/secret.txt']), \
                mock.patch.object(Path, 'open', side_effect=AssertionError('linked content opened')):
            result = reporting.snapshot(self.work, owns='owned/*')
        self.assertEqual(result, {})
        self.assertTrue(result.partial)
        self.assertEqual(result.reason, 'linked ancestor excluded')

    def junction_fixture(self):
        """A real junction to this test's own sibling, removed as a link only."""
        outside = self.base / 'outside'
        outside.mkdir()
        secret = outside / 'secret.txt'
        secret.write_bytes(b'controlled outside fixture\n')
        link = self.work / 'linked'
        self.assertEqual(link.parent.resolve(), self.work.resolve())
        self.assertEqual(outside.parent.resolve(), self.base.resolve())
        from neoxider_agents.process import hidden_kwargs
        made = subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J', str(link), str(outside)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5, **hidden_kwargs())
        if made.returncode:
            self.skipTest('junction creation unavailable: %s' % made.stderr.decode('utf-8', 'replace'))
        def cleanup():
            self.assertEqual(link.parent.resolve(), self.work.resolve())
            self.assertEqual(outside.parent.resolve(), self.base.resolve())
            self.assertEqual(link.resolve(), outside.resolve())
            self.assertEqual(link.lstat().st_reparse_tag, stat.IO_REPARSE_TAG_MOUNT_POINT)
            os.rmdir(link)  # Windows removes the junction itself, never its target tree.
            self.assertTrue(secret.is_file())
            self.assertEqual(secret.read_bytes(), b'controlled outside fixture\n')
        self.addCleanup(cleanup)
        self.assertFalse(link.is_symlink())
        self.assertEqual(link.lstat().st_reparse_tag, stat.IO_REPARSE_TAG_MOUNT_POINT)
        return link, outside

    def guarded_junction_snapshot(self, route, legacy=False):
        link, outside = self.junction_fixture()
        owned = self.put('owned/input.txt', b'owned file\n')
        self.put('normal/nested.txt', b'normal nested file\n')
        opened, escaped = [], []
        original = Path.open
        def guarded(path, *args, **kwargs):
            resolved = path.resolve()
            if self.work.resolve() not in resolved.parents:
                escaped.append(path.relative_to(self.work).as_posix())
                raise AssertionError('snapshot tried to open outside owned workspace: %s' % path)
            opened.append(path.relative_to(self.work).as_posix())
            return original(path, *args, **kwargs)
        candidates = ['linked/secret.txt', 'owned/input.txt', 'normal/nested.txt'] if route == 'git' else None
        result = None
        try:
            with mock.patch.object(reporting, '_git_listed_files', return_value=candidates), \
                    mock.patch.object(Path, 'open', guarded), \
                    mock.patch.object(Path, 'is_junction', None, create=True) if legacy else contextlib.nullcontext():
                result = reporting.snapshot(self.work)
            self.assertEqual(set(result), {'owned/input.txt', 'normal/nested.txt'})
            self.assertEqual(result['owned/input.txt']['sha256'], hashlib.sha256(b'owned file\n').hexdigest())
            self.assertEqual(result.source, route)
            self.assertTrue(result.partial)
            self.assertIsNone(result.not_tracked)
            self.assertIn('linked', result.reason)
            self.assertEqual(escaped, [])
        finally:
            print('[junction-fixture] ' + json.dumps(dict(route=route, legacy=legacy,
                  junction=str(link), target=str(outside), opened=opened, escaped=escaped,
                  partial=getattr(result, 'partial', None), keys=list(result) if result is not None else None)))

    @unittest.skipUnless(os.name == 'nt', 'real Windows directory junction')
    def test_git_junction_blocks_outside_bytes_but_hashes_owned_files(self):
        self.guarded_junction_snapshot('git')

    @unittest.skipUnless(os.name == 'nt', 'real Windows directory junction')
    def test_walk_junction_blocks_outside_bytes_but_hashes_owned_files(self):
        self.guarded_junction_snapshot('walk')

    @unittest.skipUnless(os.name == 'nt', 'Python 3.8 junction fallback on Windows')
    def test_git_junction_without_new_path_api_still_blocks_outside_bytes(self):
        self.guarded_junction_snapshot('git', legacy=True)

    @unittest.skipUnless(os.name == 'nt', 'Python 3.8 junction fallback on Windows')
    def test_walk_junction_without_new_path_api_still_blocks_outside_bytes(self):
        self.guarded_junction_snapshot('walk', legacy=True)

    @unittest.skipUnless(os.name == 'nt', 'real Windows directory junction')
    def test_unowned_junction_does_not_make_scoped_snapshot_partial(self):
        self.junction_fixture()
        self.put('owned/input.txt')
        for candidates in (None, ['linked/secret.txt', 'owned/input.txt']):
            with mock.patch.object(reporting, '_git_listed_files', return_value=candidates):
                result = reporting.snapshot(self.work, owns='owned/*')
            self.assertEqual(set(result), {'owned/input.txt'})
            self.assertFalse(result.partial)

    @unittest.skipUnless(os.name == 'nt', 'real Windows directory junction')
    def test_replacing_owned_directory_with_junction_keeps_delta_unknown(self):
        _, outside = self.junction_fixture()
        source = self.put('owned/input.txt')
        self.store.update('task', owns='owned/*')
        reporting.begin_snapshot(self.store, 'task', self.work)
        source.unlink()
        source.parent.rmdir()
        os.rename(self.work / 'linked', self.work / 'owned')
        # Restore the fixture link name before its verified cleanup runs.
        self.addCleanup(lambda: os.rename(self.work / 'owned', self.work / 'linked'))
        original = Path.open
        def guarded(path, *args, **kwargs):
            self.assertFalse(outside.resolve() in path.resolve().parents, 'outside bytes opened')
            return original(path, *args, **kwargs)
        err = io.StringIO()
        with mock.patch.object(Path, 'open', guarded), contextlib.redirect_stderr(err):
            self.assertEqual(reporting.changed_files(self.store, 'task', self.work), [])
            self.assertEqual(reporting.file_changes(self.store, 'task', self.work), [])
        self.assertIn('current snapshot partial:', err.getvalue())
        self.assertIn('current snapshot partial:', self.store.read('task')['baseline_warning'])

    @unittest.skipUnless(os.name == 'nt', 'Windows cloud reparse tag')
    def test_non_mountpoint_cloud_tag_keeps_owned_bytes_readable(self):
        cloud = self.work / 'cloud'
        self.put('cloud/input.txt', b'cloud owned bytes\n')
        original = Path.lstat
        def cloud_stat(path):
            return SimpleNamespace(st_mode=stat.S_IFDIR, st_reparse_tag=0x9000001A) if path == cloud else original(path)
        for api in (None, lambda path: False):
            for candidates in (None, ['cloud/input.txt']):
                with mock.patch.object(Path, 'is_junction', api, create=True), \
                        mock.patch.object(Path, 'lstat', cloud_stat), \
                        mock.patch.object(reporting, '_git_listed_files', return_value=candidates):
                    result = reporting.snapshot(self.work, owns='cloud/*')
                self.assertEqual(set(result), {'cloud/input.txt'})
                self.assertEqual(result['cloud/input.txt']['sha256'], hashlib.sha256(b'cloud owned bytes\n').hexdigest())
                self.assertFalse(result.partial)

    def test_unverifiable_directory_marks_partial_without_opening_its_bytes(self):
        bad = self.work / 'bad'
        self.put('bad/input.txt')
        self.put('good.txt')
        original_stat, original_open = Path.lstat, Path.open
        def denied(path):
            if path == bad:
                raise PermissionError('controlled directory verification failure')
            return original_stat(path)
        def guarded(path, *args, **kwargs):
            self.assertNotIn(bad, path.parents, 'unverifiable directory opened')
            return original_open(path, *args, **kwargs)
        for candidates in (None, ['bad/input.txt', 'good.txt']):
            # Force the legacy tag path so the injected lstat error is observable.
            with mock.patch.object(Path, 'is_junction', None, create=True), \
                    mock.patch.object(Path, 'lstat', denied), \
                    mock.patch.object(Path, 'open', guarded), \
                    mock.patch.object(reporting, '_git_listed_files', return_value=candidates):
                if os.name != 'nt':
                    with mock.patch.object(Path, 'is_symlink', side_effect=PermissionError('controlled verification failure')):
                        result = reporting.snapshot(self.work)
                else:
                    result = reporting.snapshot(self.work)
            self.assertEqual(set(result), {'good.txt'})
            self.assertTrue(result.partial)
            self.assertIsNone(result.not_tracked)
            self.assertEqual(result.reason, 'directory verification failed')

    def test_git_deleted_owned_directory_is_a_known_deletion_not_unknown(self):
        source = self.put('owned/input.txt')
        self.store.update('task', owns='owned/*')
        with mock.patch.object(reporting, '_git_listed_files', return_value=['owned/input.txt']):
            reporting.begin_snapshot(self.store, 'task', self.work)
            source.unlink()
            source.parent.rmdir()
            result = reporting.snapshot(self.work, owns='owned/*')
            self.assertFalse(result.partial)
            self.assertEqual(result, {})
            self.assertEqual(reporting.changed_files(self.store, 'task', self.work), ['owned/input.txt'])
            details = reporting.file_changes(self.store, 'task', self.work)
        self.assertEqual(len(details), 1)
        self.assertEqual(details[0]['status'], 'deleted')

    def test_scoped_zero_budget_is_partial_not_complete_empty(self):
        self.put('owned.txt')
        with mock.patch.object(reporting, '_git_listed_files', return_value=['owned.txt']), \
                mock.patch.dict(os.environ, AGENT_BASELINE_BUDGET_SEC='0'):
            result = reporting.snapshot(self.work, owns='owned.txt')
        self.assertTrue(result.partial)
        self.assertEqual(result, {})
        self.assertIsNone(result.not_tracked)

    def test_scoped_guard_rejects_linked_ancestor_even_without_os_symlink_permission(self):
        self.put('owned/secret.txt')
        original = Path.is_symlink
        def linked(path):
            return path == self.work / 'owned' or original(path)
        with mock.patch.object(reporting, '_git_listed_files', return_value=['owned/secret.txt']), \
                mock.patch.object(Path, 'is_symlink', linked), \
                mock.patch.object(Path, 'open', side_effect=AssertionError('linked content opened')):
            result = reporting.snapshot(self.work, owns='owned/*')
        self.assertEqual(result, {})
        self.assertTrue(result.partial)
        self.assertEqual(result.reason, 'linked ancestor excluded')

    def test_scoped_git_never_follows_outside_traversal_candidate(self):
        outside = self.base / 'outside.txt'
        outside.write_text('private', encoding='utf-8')
        with mock.patch.object(reporting, '_git_listed_files', return_value=['../outside.txt']), \
                mock.patch.object(Path, 'open', side_effect=AssertionError('outside file opened')):
            result = reporting.snapshot(self.work, owns='../outside.txt')
        self.assertEqual(result, {})

    @unittest.skipUnless(os.name == 'nt', 'Windows ownership casing')
    def test_scoped_windows_ownership_matches_case_insensitively(self):
        self.put('Owned/Mixed.txt')
        self.assertEqual(set(reporting.snapshot(self.work, owns='owned/MIXED.TXT')), {'Owned/Mixed.txt'})

    def test_fallback_byte_limit_marks_partial_before_opening_file(self):
        self.put('large.dat', b'x' * 100)
        with mock.patch.dict(os.environ, AGENT_BASELINE_MAX_BYTES='50'), \
                mock.patch.object(Path, 'open', side_effect=AssertionError('over-budget file opened')):
            result = reporting.snapshot(self.work)
        self.assertTrue(result.partial)
        self.assertIn('byte limit', result.reason)
        self.assertEqual(result, {})

    def test_upstream_default_fallback_file_limit_is_preserved(self):
        self.put('a.txt')
        self.put('b.txt')
        with mock.patch.dict(os.environ):
            os.environ.pop('AGENT_BASELINE_MAX_FILES', None)
            with mock.patch.object(reporting, 'WALK_FILE_LIMIT', 1):
                result = reporting.snapshot(self.work)
        self.assertEqual(len(result), 1)
        self.assertTrue(result.partial)

    def test_git_partial_has_exact_untracked_count(self):
        (self.work / '.git').mkdir()
        for n in range(5): self.put('%d.txt' % n)
        reply = subprocess.CompletedProcess([], 0, stdout=b'0.txt\0' + b'1.txt\0' + b'2.txt\0' + b'3.txt\0' + b'4.txt\0')
        with mock.patch.dict(os.environ, AGENT_BASELINE_MAX_FILES='2'), mock.patch('subprocess.run', return_value=reply):
            result = reporting.snapshot(self.work)
        self.assertTrue(result.partial)
        self.assertEqual(result.not_tracked, 3)

    def test_size_mtime_reuses_hash_and_force_baseline(self):
        path = self.put('a.txt')
        reporting.begin_snapshot(self.store, 'task', self.work)
        previous = reporting.read_baseline(self.store, 'task')['files']
        with mock.patch.object(Path, 'open', side_effect=AssertionError('unchanged content re-opened')):
            result = reporting.snapshot(self.work, previous=previous)
        self.assertEqual(result, previous)
        original = Path.open
        def checked(p, *args, **kwargs):
            self.assertNotEqual(p, path)
            return original(p, *args, **kwargs)
        with mock.patch.object(Path, 'open', checked):
            reporting.begin_snapshot(self.store, 'task', self.work, force=True)
        self.assertEqual(reporting.read_baseline(self.store, 'task')['files'], previous)

    def test_large_file_fingerprint_reads_only_edges(self):
        path = self.put('large.dat', b'x' * (9 * 1024 * 1024))
        original = Path.open
        reads = []
        class Reader:
            def __init__(self, source): self.source = source
            def __enter__(self): return self
            def __exit__(self, *args): self.source.close()
            def read(self, n=-1):
                block = self.source.read(n)
                reads.append(len(block))
                return block
            def seek(self, *args): return self.source.seek(*args)
        def opened(p, *args, **kwargs):
            source = original(p, *args, **kwargs)
            return Reader(source) if p == path else source
        with mock.patch.object(Path, 'open', opened):
            result = reporting.snapshot(self.work)
        self.assertTrue(result['large.dat']['fingerprint'])
        self.assertLessEqual(sum(reads), 128 * 1024)
        before = result['large.dat']
        stamp = path.stat()
        os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 1000000))
        after = reporting.snapshot(self.work)['large.dat']
        self.assertTrue(reporting._different(before, after))

    def test_delta_keeps_same_stat_content_detection(self):
        path = self.put('a.txt', b'old')
        reporting.begin_snapshot(self.store, 'task', self.work)
        stamp = path.stat()
        path.write_bytes(b'new')
        os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        self.assertEqual(reporting.changed_files(self.store, 'task', self.work), ['a.txt'])

    def test_orphan_effective_never_snapshots_or_records_stop(self):
        meta = dict(state='running', core_version='2', pid='99999999', provider_pid='', dir=self.work.as_posix())
        self.store.update('task', **meta)
        before = self.store.path('task', '.meta').read_bytes()
        with mock.patch.object(reporting, 'snapshot', side_effect=AssertionError('read path snapshot')), \
                mock.patch.object(lifecycle, 'record_stop', side_effect=AssertionError('read path record_stop')):
            start = time.perf_counter()
            self.assertEqual(lifecycle.effective(self.store, 'task', meta), 'stopped')
            self.assertLess(time.perf_counter() - start, .05)
        self.assertEqual(self.store.path('task', '.meta').read_bytes(), before)

    def test_five_orphan_list_status_under_one_second(self):
        big = Path('D:/Temp/baseline-merge/synthetic')
        directory = big if big.exists() else self.work
        for n in range(5):
            self.store.update('orphan%d' % n, state='running', core_version='2', pid=99999999,
                              dir=directory.as_posix(), engine='fixture', started_epoch=time.time())
        with mock.patch.object(reporting, 'snapshot', side_effect=AssertionError('polling snapshot')), \
                mock.patch.object(lifecycle, 'record_stop', side_effect=AssertionError('polling stop')), \
                contextlib.redirect_stdout(io.StringIO()):
            start = time.perf_counter()
            self.assertEqual(main(['list']), 0)
            for n in range(5): self.assertEqual(main(['status', 'orphan%d' % n]), 0)
            self.assertLess(time.perf_counter() - start, 1)

    def test_synthetic_50k_git_tree_never_opens_ignored_files(self):
        big = Path('D:/Temp/baseline-merge/synthetic')
        if os.name != 'nt': big = self.base / 'synthetic'
        if not big.exists():
            for directory, count in [('Assets', 50000), ('Library', 10000), ('Temp', 1000), ('obj', 1000), ('Logs', 1000)]:
                folder = big / directory
                folder.mkdir(parents=True)
                for n in range(count): (folder / ('%05d.txt' % n)).write_bytes(b'small\n')
            (big / '.git').mkdir()
        candidates = b'.gitignore\0' if (big / '.gitignore').exists() else b''
        candidates += b''.join(('Assets/%05d.txt\0' % n).encode() for n in range(50000))
        reply = subprocess.CompletedProcess([], 0, stdout=candidates)
        original = Path.open
        count = [0]
        def checked(path, *args, **kwargs):
            self.assertFalse({'Library', 'Temp', 'obj', 'Logs'} & set(path.relative_to(big).parts))
            count[0] += 1
            return original(path, *args, **kwargs)
        with mock.patch.object(Path, 'open', checked), mock.patch('subprocess.run', return_value=reply):
            start = time.perf_counter()
            result = reporting.snapshot(big)
            elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 21)
        if result.partial:
            self.assertIn('budget', result.reason)
            if result.not_tracked is not None: self.assertGreater(result.not_tracked, 0)
        self.assertGreater(len(result), 0)
        self.assertLessEqual(len(result), 50001)
        self.assertLessEqual(count[0], 50010)

    def test_partial_current_does_not_report_unseen_deletions(self):
        for n in range(5): self.put('%d.txt' % n)
        reporting.begin_snapshot(self.store, 'task', self.work)
        with mock.patch.dict(os.environ, AGENT_BASELINE_MAX_FILES='1'), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(reporting.changed_files(self.store, 'task', self.work), [])
            self.assertEqual(reporting.file_changes(self.store, 'task', self.work), [])

    def test_partial_diff_and_result_explicitly_warn(self):
        for n in range(5): self.put('%d.txt' % n)
        self.store.update('task', state='done', core_version=2, dir=self.work.as_posix(), exit=0)
        with mock.patch.dict(os.environ, AGENT_BASELINE_MAX_FILES='1'):
            reporting.begin_snapshot(self.store, 'task', self.work)
        for args in (['diff', 'task'], ['result', 'task', '--json']):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err): self.assertEqual(main(args), 0)
            self.assertIn('baseline partial:', out.getvalue() + err.getvalue())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(main(['top', '--once']), 0)
        self.assertIn('baseline partial:', out.getvalue())

    def test_orphan_result_computes_and_caches_delta_once(self):
        self.put('a.txt')
        self.store.update('task', state='running', core_version=2, pid=99999999, dir=self.work.as_posix())
        reporting.begin_snapshot(self.store, 'task', self.work)
        self.put('a.txt', b'changed')
        with mock.patch.object(reporting, 'snapshot', wraps=reporting.snapshot) as snap, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(['result', 'task', '--json']), 0)
            count = snap.call_count
            self.assertGreater(count, 0)
            self.assertTrue(self.store.path('task', '.changes.json').is_file())
            self.assertEqual(main(['result', 'task', '--json']), 0)
            self.assertEqual(snap.call_count, count)


if __name__ == '__main__':
    unittest.main()
