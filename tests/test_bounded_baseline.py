"""Offline bounded baseline regressions; scratch and provider state stay isolated."""
import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
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
