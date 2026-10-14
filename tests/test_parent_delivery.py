"""Actual parent integration, durable replay and publisher crash gates."""
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
from unittest import mock

from test_core_parity_integration import CoreHarness, ROOT, sdk, EventStore, Store


class ParentDeliveryTests(CoreHarness):
    def test_quiet_cli_failure_labels_and_quota_marker_match_actual_provider_boundary(self):
        client = self.client()
        for marker, category in [('OOM', 'provider_oom'), ('TLS', 'tls'), ('AUTH', 'auth'), ('QUOTA', 'quota')]:
            with self.subTest(marker=marker):
                name = 'quiet-cli-' + marker.lower()
                completed = subprocess.run([sys.executable, '-B', str(ROOT / 'agent.py'), 'ask',
                    '-e', 'codex', '-t', name, '-C', str(self.work), '-P', 'integration-parent',
                    '--no-terse', 'ERR:' + marker], env=client._child_env(detached=True),
                    capture_output=True, timeout=15, **sdk.launch_kwargs())
                stdout = completed.stdout.decode('utf-8')
                stderr = completed.stderr.decode('utf-8')
                state = 'limited' if marker == 'QUOTA' else 'error'
                self.assertEqual(completed.returncode, 126, stdout + stderr)
                self.assertIn('neoxider: task=%s %s;' % (name, state), stderr)
                self.assertEqual('LIMIT_HIT' in stderr, marker == 'QUOTA')
                self.assertNotIn('LIMIT_HIT', stdout)
                if marker != 'QUOTA':
                    self.assertNotIn(' limited;', stderr)
                meta = client.store.read(name)
                self.assertEqual(meta['state'], state)
                self.assertEqual(meta['exit'], '126')
                failure = json.loads(meta['failure_json'])
                self.assertEqual(failure['category'], category)
                self.assertTrue(failure['blocking'])
                self.assertEqual(failure['source'], 'provider_stream')
                self.assertTrue(any(event['kind'] == 'terminal' for event in EventStore(self.logs).read(name)))

    def test_default_callback_fast_terminal_before_first_tick_resume_and_reattach(self):
        observed, notification, start_monitor = [], threading.Event(), threading.Event()
        original_monitor = sdk.AgentClient._monitor_loop
        def delayed_monitor(client):
            start_monitor.wait(20)
            original_monitor(client)
        def receive(event):
            if event.kind == 'settled':
                observed.append(event)
                notification.set()
        with mock.patch.object(sdk.AgentClient, '_monitor_loop', delayed_monitor):
            client = self.client(on_event=receive)
        try:
            _, handle = self.spawn('zero delay', client=client, name='fast-default-callback')
            child = client._child_for(handle.task)
            self.assertIsNotNone(child)
            self.assertEqual(child.process.wait(timeout=15), 0)
            self.assertFalse(observed, 'provider completes while the first monitor tick is blocked')
            start_monitor.set()
            self.assertTrue(notification.wait(5), 'host callback receives completion without status polling')
            terminal = next(row for row in EventStore(self.logs).read(handle.task) if row['kind'] == 'terminal')
            self.assertEqual(observed[0].event_id, terminal['id'])
            self.assertEqual(observed[0].generation, terminal['generation'])
            self.assertEqual(observed[0].parent, 'integration-parent')
            self.assertEqual(observed[0].state, 'done')
            notification.clear()
            client.resume(handle, message='second zero-delay turn')
            self.assertTrue(notification.wait(5))
            self.assertEqual(len({event.event_id for event in observed}), 2)
            self.assertEqual(len(observed), 2, 'legacy state polling never duplicates durable terminal dispatch')
            client.close(detach=True)
            reattached, replay = [], threading.Event()
            def observer(event):
                if event.kind == 'settled':
                    reattached.append(event)
                    if len(reattached) == 2:
                        replay.set()
            reconnect = self.client(on_event=observer)
            reconnect.attach(handle.task)
            self.assertTrue(replay.wait(5))
            self.assertEqual([event.event_id for event in reattached], [event.event_id for event in observed])
        finally:
            start_monitor.set()

    def test_parent_label_consumer_is_stably_normalized_and_remains_parent_bound(self):
        observed, notification = [], threading.Event()
        def callback(event):
            observed.append(event)
            if event.kind == 'terminal':
                notification.set()
        client = self.client(on_event=callback, consumer='/root/parent')
        _, handle = self.spawn(client=client)
        self.assertTrue(notification.wait(5))
        consumer = client._subscriptions[0][0]
        self.assertRegex(consumer, r'^consumer-[0-9a-f]{32}$')
        self.assertTrue(all(event.parent == 'integration-parent' for event in observed))
        self.until(lambda: EventStore(self.logs).cursor(handle.task, consumer) > 0)
        client.close(detach=True)
        reconnect = self.client(on_event=lambda event: None, consumer='/root/parent')
        reconnect.attach(handle.task)
        self.assertEqual(reconnect._subscriptions[0][0], consumer)

    def test_parent_wait_any_and_terminal_callback_without_manual_child_polling(self):
        received = []
        terminal = threading.Event()
        def callback(event):
            received.append(event)
            if event.kind == 'terminal':
                terminal.set()
        client = self.client(on_event=callback, consumer='parent-main')
        _, slow = self.spawn('BLOCK', client=client, name='slow-child')
        self.ready(client, slow)
        other = self.base / 'other'
        other.mkdir()
        quick = client.spawn('quick', engine='codex', name='quick-child', cwd=other)
        results = client.wait_any([slow, quick], timeout=15)
        self.assertEqual([result.task for result in results], ['quick-child'])
        self.assertTrue(terminal.wait(5))
        events = [event for event in received if event.task == quick.task]
        self.assertTrue(any(event.kind == 'born' for event in events))
        self.assertTrue(any(event.kind == 'terminal' for event in events))
        self.assertTrue(all(event.parent == 'integration-parent' for event in events))
        self.release()
        slow.wait(timeout=15)

    def test_failed_callback_no_ack_reconnect_replays_and_cross_generation_kept(self):
        failed = threading.Event()
        ids = []
        def callback(event):
            if event.kind == 'terminal':
                ids.append(event.event_id)
                failed.set()
                raise ValueError('subscriber failed after its side effect')
        client = self.client(on_event=callback, consumer='reconnect-parent')
        _, handle = self.spawn(client=client, name='reconnect-child')
        handle.wait(timeout=15)
        self.assertTrue(failed.wait(5))
        terminal = next(event for event in client.events(handle) if event['kind'] == 'terminal')
        self.until(lambda: bool(client.callback_errors))
        self.assertLess(EventStore(self.logs).cursor(handle.task, 'reconnect-parent'), terminal['seq'])
        client.close(detach=True)
        replayed = []
        replay = self.client(on_event=lambda event: replayed.append(event), consumer='reconnect-parent')
        attached = replay.attach(handle.task)
        self.until(lambda: any(event.event_id == ids[0] for event in replayed))
        self.until(lambda: EventStore(self.logs).cursor(handle.task, 'reconnect-parent') >= terminal['seq'])
        old_generation = attached.generation
        replay.resume(attached, message='second generation')
        attached.wait(timeout=15)
        self.until(lambda: any(event.kind == 'terminal' and event.generation != old_generation for event in replayed))
        terminals = [event for event in replay.events(attached) if event['kind'] == 'terminal']
        self.assertEqual(len(terminals), 2)
        self.assertEqual(len({event['id'] for event in terminals}), 2)
        self.assertEqual(len([event for event in replayed if event.event_id == ids[0]]), 1)

    def test_slow_subscriber_independent_from_other_subscriber_and_resource_reaper(self):
        entered, release, fast_terminal = threading.Event(), threading.Event(), threading.Event()
        def slow(event):
            if event.kind == 'terminal':
                entered.set()
                release.wait(15)
        client = self.client()
        client.subscribe(slow, consumer='slow-subscriber')
        client.subscribe(lambda event: fast_terminal.set() if event.kind == 'terminal' else None,
                         consumer='fast-subscriber')
        try:
            _, handle = self.spawn(client=client)
            handle.wait(timeout=15)
            self.assertTrue(entered.wait(5))
            self.assertTrue(fast_terminal.wait(5))
            self.until(lambda: not client._children)
            self.assertTrue(client.store.path(handle.task, '.answer').exists())
            self.assertTrue(client.store.session(handle.task))
        finally:
            release.set()

    def test_close_waits_for_real_callback_ack_file_creation_and_thread_exit(self):
        from neoxider_agents.process import pid_alive
        entered, callback_release, ack_entered, ack_release, joining, closed = [
            threading.Event() for _ in range(6)]
        records, writes, reports = [], [], []
        client = self.client()
        original_ack = EventStore.ack
        marker = self.logs / 'callback-ack-finished'

        def callback(event):
            if event.kind == 'terminal':
                records.append(event)
                entered.set()
                callback_release.wait(10)

        def ack(events, task, consumer, cursor):
            if records and cursor == records[0].sequence and consumer == 'close-barrier':
                ack_entered.set()
                ack_release.wait(10)
                result = original_ack(events, task, consumer, cursor)
                marker.write_text('durable ACK completed', encoding='utf-8')
                writes.append(closed.is_set())
                return result
            return original_ack(events, task, consumer, cursor)

        def close():
            reports.append(client.close())
            closed.set()

        client.subscribe(callback, consumer='close-barrier')
        consumer, cancel, subscriber = client._subscriptions[0]
        original_join = subscriber.join

        def join(timeout=None):
            joining.set()
            return original_join(timeout)

        closer = threading.Thread(target=close)
        try:
            with mock.patch.object(EventStore, 'ack', new=ack):
                _, handle = self.spawn('BLOCK', client=client, name='close-ack-child')
                self.ready(client, handle)
                child = client._child_for(handle.task)
                provider = client.store.read(handle.task)
                self.release()
                handle.wait(timeout=15)
                self.assertTrue(entered.wait(5))
                self.until(lambda: child.reaped)
                self.assertFalse(pid_alive(provider['provider_pid'], provider['provider_start']))
                self.assertFalse(child.thread.is_alive())
                self.assertTrue(child.process.stdout.closed)
                with mock.patch.object(subscriber, 'join', side_effect=join):
                    closer.start()
                    self.assertTrue(cancel.wait(5), 'close cancels the in-flight consumer')
                    joined = joining.wait(1)
                    if not joined:
                        self.assertTrue(closed.wait(5), 'baseline close returns before callback release')
                    returned_before_callback = closed.is_set()
                    callback_release.set()
                    self.assertTrue(ack_entered.wait(1))
                    returned_before_ack = closed.is_set()
                    ack_release.set()
                    closer.join(5)
                    original_join(5)  # observe the baseline's actual post-close ACK/write too
            self.assertTrue(closed.is_set())
            self.assertEqual(writes, [False], 'ACK/file creation must happen before close returns')
            self.assertTrue(joined, 'close must join the actual subscriber thread')
            self.assertFalse(returned_before_callback)
            self.assertFalse(returned_before_ack, 'callback return alone does not finish durable ACK')
            self.assertTrue(marker.is_file())
            self.assertFalse(subscriber.is_alive())
            self.assertEqual(EventStore(self.logs).cursor(handle.task, consumer), records[0].sequence)
            self.assertEqual(reports[0].pending_consumers, ())
            self.assertEqual(reports[0].pending_threads, ())
            self.assertFalse(client._monitor.is_alive())
            self.assertFalse(client._reaper.is_alive())
            self.assertTrue(client.store.path(handle.task, '.answer').is_file())
            self.assertTrue(client.store.session(handle.task))
        finally:
            callback_release.set()
            ack_release.set()
            if closer.ident is not None:
                closer.join(5)
            original_join(5)

    def test_close_waits_for_failed_callback_teardown_without_acknowledging(self):
        entered, release, joining = threading.Event(), threading.Event(), threading.Event()
        records, reports = [], []
        client = self.client()

        def callback(event):
            if event.kind == 'terminal':
                records.append(event)
                entered.set()
                release.wait(10)
                raise ValueError('callback failed during close')

        client.subscribe(callback, consumer='close-failed')
        subscriber = client._subscriptions[0][2]
        original_join = subscriber.join

        def join(timeout=None):
            joining.set()
            return original_join(timeout)

        closer = threading.Thread(target=lambda: reports.append(client.close()))
        try:
            _, handle = self.spawn(client=client, name='close-failed-child')
            handle.wait(timeout=15)
            self.assertTrue(entered.wait(5))
            with mock.patch.object(subscriber, 'join', side_effect=join):
                closer.start()
                self.assertTrue(joining.wait(1))
                release.set()
                closer.join(5)
            self.assertFalse(closer.is_alive())
            self.assertFalse(subscriber.is_alive())
            self.assertEqual(reports[0].pending_consumers, ())
            self.assertTrue(any(error[1:] == ('ValueError', 'callback failed during close')
                                for error in client.callback_errors))
            self.assertLess(EventStore(self.logs).cursor(handle.task, 'close-failed'), records[0].sequence)
        finally:
            release.set()
            if closer.ident is not None:
                closer.join(5)
            original_join(5)

    def test_terminal_publisher_process_crash_reuses_exact_captured_bytes(self):
        store = Store(self.logs)
        store.update('crash-child', state='running', generation='generation-one', parent='integration-parent',
                     session='session-retained', exit='', files='0')
        store.path('crash-child', '.answer').write_text('original captured answer', encoding='utf-8')
        code = '''import os, sys
sys.path.insert(0, sys.argv[1])
from neoxider_agents.state import Store
from neoxider_agents.events import EventStore
original = EventStore.emit
def crash(self, **kwargs):
    original(self, **kwargs)
    os._exit(23)
EventStore.emit = crash
Store(sys.argv[2]).terminal_event('crash-child', state='done', exit=0)
'''
        process = subprocess.run([sys.executable, '-B', '-c', code, str(ROOT), str(self.logs)],
                                 capture_output=True, timeout=15, **sdk.launch_kwargs())
        self.assertEqual(process.returncode, 23)
        intent = json.loads(store.read('crash-child')['terminal_intent'])
        stored = EventStore(self.logs).read('crash-child').events[0]
        self.assertEqual(stored['data'], intent['data'])
        store.path('crash-child', '.answer').write_text('later changed answer', encoding='utf-8')
        store.update('crash-child', generation='generation-two', state='running')
        self.assertTrue(store.publish_events('crash-child'))
        events = EventStore(self.logs).read('crash-child').events
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['generation'], 'generation-one')
        self.assertEqual(events[0]['data'], stored['data'])
        self.assertFalse(list(store.path('crash-child', '.event-outbox').glob('*.json')))

    def test_corruption_retains_outbox_and_refuses_false_completion(self):
        client, handle = self.spawn('BLOCK')
        self.ready(client, handle)
        event_path = EventStore(self.logs).path(handle.task)
        with event_path.open('ab') as out:
            out.write(b'{"complete":"interior corruption"}\n')
        self.release()
        with self.assertRaises(sdk.OrchestratorError):
            handle.wait(timeout=15)
        self.assertTrue(client.store.read(handle.task).get('event_pending') == '1')
        self.assertTrue(list(client.store.path(handle.task, '.event-outbox').glob('*.json')))
        self.assertTrue(handle.result().event_pending)
        self.assertEqual(handle.result().tests_status, 'not_run')

    def test_pending_intents_replay_in_creation_order_across_generation(self):
        store = Store(self.logs)
        store.update('ordered-child', generation='old-generation', parent='integration-parent')
        store.stage_event('ordered-child', 'born', {'state': 'running'}, event_id='f' * 32)
        original = store.stage_event('ordered-child', 'queued', {'sequence': 1}, event_id='0' * 32)
        duplicate = store.stage_event('ordered-child', 'queued', {'sequence': 1}, event_id='0' * 32)
        self.assertEqual(original, duplicate)
        store.update('ordered-child', generation='new-generation')
        store.terminal_event('ordered-child', state='done', exit=0)
        events = EventStore(self.logs).read('ordered-child').events
        self.assertEqual([event['kind'] for event in events], ['born', 'queued', 'terminal'])
        self.assertEqual([event['generation'] for event in events], ['old-generation', 'old-generation', 'new-generation'])
