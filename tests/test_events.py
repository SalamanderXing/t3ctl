"""Durable event transitions and parent-session delivery, with no external effects."""
import asyncio
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
loader = SourceFileLoader('t3_events_test', os.environ.get('T3_EVENTS_TEST_BIN', str(ROOT / 'bin/t3-events')))
spec = spec_from_loader(loader.name, loader)
events = module_from_spec(spec)
loader.exec_module(events)


class EventsTest(unittest.TestCase):
    def setUp(self):
        (ROOT / 'tmp').mkdir(exist_ok=True)
        temp = tempfile.TemporaryDirectory(dir=ROOT / 'tmp')
        self.addCleanup(temp.cleanup)
        self.db = Path(temp.name) / 'events.sqlite'
        self.receipt = {'threadId': 'thread', 'dispatchId': 'dispatch', 'messageId': 'message', 'previousTurnId': 'old', 'turnId': 'turn'}
        self.env = {'T3CTL_EVENTS_ENABLED': '1', 'HERMES_SESSION_ID': 'parent', 'HERMES_SESSION_KEY': 'route', 'HERMES_SESSION_PLATFORM': 'discord'}
        self.assertTrue(events.register(self.receipt, self.env, self.db))
        self.runner = SimpleNamespace(
            _draining=False, _running_agents={},
            _classify_completion_target=AsyncMock(return_value='deliver'),
            _session_db=SimpleNamespace(get_compression_tip=AsyncMock(return_value='parent')),
            async_session_store=SimpleNamespace(lookup_by_session_key=AsyncMock(return_value=SimpleNamespace(session_id='parent'))),
            _dispatch_plugin_message_injection=AsyncMock(return_value=True))

    def observe(self, **fields):
        state = {'turnId': 'turn', 'reason': 'settled', 'turn': 'completed', 'lastAssistant': 'Artifact ready'}
        state.update(fields)
        with events.connect(self.db) as db:
            subscription = dict(db.execute('SELECT * FROM subscriptions').fetchone())
        events.observe(subscription, state, self.db)

    def test_restart_replay_and_duplicate_observation(self):
        self.observe()
        self.observe()
        self.assertEqual(len(events.pending(self.db)), 1)
        asyncio.run(events.deliver_pending(self.runner, self.db))
        asyncio.run(events.deliver_pending(self.runner, self.db))
        self.runner._dispatch_plugin_message_injection.assert_awaited_once()
        call = self.runner._dispatch_plugin_message_injection.call_args.kwargs
        self.assertEqual(call['session_key'], 'route')
        self.assertEqual(call['expected_session_id'], 'parent')
        self.assertIn('Artifact ready', call['content'])
        self.assertEqual(events.pending(self.db), [])

    def test_busy_parent_does_not_interrupt_or_consume_retry(self):
        self.observe()
        self.runner._running_agents['route'] = object()
        asyncio.run(events.deliver_pending(self.runner, self.db))
        self.runner._dispatch_plugin_message_injection.assert_not_awaited()
        self.assertEqual(events.pending(self.db)[0]['attempts'], 0)
        self.runner._running_agents.clear()
        asyncio.run(events.deliver_pending(self.runner, self.db))
        self.runner._dispatch_plugin_message_injection.assert_awaited_once()

    def test_new_session_never_receives_old_task(self):
        self.observe()
        self.runner.async_session_store.lookup_by_session_key.return_value.session_id = 'unrelated'
        asyncio.run(events.deliver_pending(self.runner, self.db))
        self.runner._dispatch_plugin_message_injection.assert_not_awaited()
        self.assertEqual(events.pending(self.db), [])

    def test_compression_continuation_keeps_ownership(self):
        self.observe()
        self.runner._session_db.get_compression_tip.return_value = 'compressed-parent'
        self.runner.async_session_store.lookup_by_session_key.return_value.session_id = 'compressed-parent'
        asyncio.run(events.deliver_pending(self.runner, self.db))
        self.assertEqual(self.runner._dispatch_plugin_message_injection.call_args.kwargs['expected_session_id'], 'compressed-parent')

    def test_retries_are_bounded_and_retained(self):
        self.observe()
        self.runner._dispatch_plugin_message_injection.return_value = False
        for _ in range(8):
            with events.connect(self.db) as db:
                db.execute('UPDATE events SET next_attempt=0')
            asyncio.run(events.deliver_pending(self.runner, self.db))
        self.assertEqual(events.pending(self.db), [])
        with events.connect(self.db) as db:
            row = db.execute('SELECT * FROM events').fetchone()
        self.assertEqual(row['disposition'], 'failed')
        self.assertEqual(row['attempts'], 8)

    def test_new_dispatch_supersedes_old_notifications(self):
        self.observe()
        events.register({**self.receipt, 'dispatchId': 'next', 'turnId': 'next-turn'}, self.env, self.db)
        self.assertEqual(events.pending(self.db), [])

    def test_new_dispatch_during_delivery_cannot_resurrect_superseded_event(self):
        self.observe()
        async def replace_dispatch(**kwargs):
            events.register({**self.receipt, 'dispatchId': 'next'}, self.env, self.db)
            return False
        self.runner._dispatch_plugin_message_injection.side_effect = replace_dispatch
        asyncio.run(events.deliver_pending(self.runner, self.db))
        with events.connect(self.db) as db:
            self.assertEqual(db.execute('SELECT disposition FROM events').fetchone()[0], 'superseded')

    def test_resolved_question_does_not_arrive_after_completion(self):
        self.observe(reason='pending-user-input', turn='running', userInputs=[{'requestId': 'question', 'questions': []}])
        self.observe(reason='timeout', turn='running')
        self.assertEqual(events.pending(self.db), [])
        self.observe()
        self.assertEqual(json.loads(events.pending(self.db)[0]['payload'])['status'], 'completed')

    def test_foreign_turn_is_not_reported_as_our_completion(self):
        self.observe(reason='timeout', turn='running')
        self.observe(turnId='someone-elses-turn', lastAssistant='Foreign output')
        self.assertEqual(json.loads(events.pending(self.db)[0]['payload'])['status'], 'superseded')

    def test_observation_outage_has_bounded_retries_and_reports_failure(self):
        with patch.object(events.subprocess, 'run', side_effect=OSError('server unavailable')) as run:
            for _ in range(8):
                with events.connect(self.db) as db:
                    db.execute('UPDATE subscriptions SET next_poll=0')
                events.poll(self.db)
            events.poll(self.db)
        self.assertEqual(run.call_count, 8)
        self.assertEqual(json.loads(events.pending(self.db)[0]['payload'])['status'], 'monitoring-failed')

    def test_manual_takeover_clears_old_subscription_and_pending_delivery(self):
        self.observe()
        self.assertFalse(events.register({**self.receipt, 'dispatchId': 'manual'}, {}, self.db))
        self.assertEqual(events.pending(self.db), [])
        self.assertFalse(events.managed('thread', self.db))
        with events.connect(self.db) as db:
            self.assertEqual(db.execute('SELECT disposition FROM events').fetchone()[0], 'superseded')

    def owner(self):
        with events.connect(self.db) as db:
            row = db.execute("SELECT dispatch_id FROM subscriptions WHERE thread_id='thread'").fetchone()
        return row[0] if row else None

    def test_replay_never_displaces_a_newer_dispatch(self):
        # Either order of a replayed A and a fresh B leaves B owning the thread
        # and B's pending event untouched; t3ctl holds no lock across this.
        replay = {**self.env, 'T3CTL_EVENTS_REPLAY': '1'}
        newer = {**self.receipt, 'dispatchId': 'newer', 'messageId': 'm2', 'previousTurnId': 'turn', 'turnId': 'turn2'}
        self.assertTrue(events.register(newer, self.env, self.db))
        with events.connect(self.db) as db:
            subscription = dict(db.execute('SELECT * FROM subscriptions').fetchone())
        events.observe(subscription, {'turnId': 'turn2', 'reason': 'settled', 'turn': 'completed', 'lastAssistant': 'B done'}, self.db)
        self.assertFalse(events.register(self.receipt, replay, self.db))   # replay of A after B registered
        self.assertEqual(self.owner(), 'newer')
        self.assertEqual([e['dispatch_id'] for e in events.pending(self.db)], ['newer'])
        self.assertEqual(events.register_decision(self.receipt, replay, self.db, self.db.with_suffix('.log')),
                         {'registered': False, 'reason': 'replay-superseded'})
        # An ineligible replay deletes and supersedes nothing either.
        self.assertFalse(events.register(self.receipt, {**replay, 'HERMES_SESSION_PLATFORM': 'webhook'}, self.db))
        self.assertEqual(self.owner(), 'newer')
        self.assertEqual(len(events.pending(self.db)), 1)
        # The other order: A's replay lands first (refreshing its own row), then B takes over.
        other = Path(self.db.parent) / 'other.sqlite'
        self.assertTrue(events.register(self.receipt, self.env, other))
        self.assertTrue(events.register(self.receipt, replay, other))
        self.assertTrue(events.register(newer, self.env, other))
        with events.connect(other) as db:
            self.assertEqual(db.execute('SELECT dispatch_id FROM subscriptions').fetchone()[0], 'newer')

    def test_concurrent_replay_and_newer_dispatch_always_leave_the_newer_owner(self):
        import threading
        replay = {**self.env, 'T3CTL_EVENTS_REPLAY': '1'}
        for i in range(30):
            path = Path(self.db.parent) / f'race-{i}.sqlite'
            self.assertTrue(events.register(self.receipt, self.env, path))   # A owns the thread
            newer = {**self.receipt, 'dispatchId': f'newer-{i}', 'messageId': f'm-{i}'}
            barrier = threading.Barrier(2)
            def run(receipt, env):
                barrier.wait()
                events.register(receipt, env, path)
            workers = [threading.Thread(target=run, args=(self.receipt, replay)), threading.Thread(target=run, args=(newer, self.env))]
            for w in workers: w.start()
            for w in workers: w.join()
            with events.connect(path) as db:
                self.assertEqual(db.execute('SELECT dispatch_id FROM subscriptions').fetchone()[0], f'newer-{i}')

    def test_replay_recovers_a_missing_registration(self):
        with events.connect(self.db) as db:
            db.execute('DELETE FROM subscriptions')
        self.assertTrue(events.register(self.receipt, {**self.env, 'T3CTL_EVENTS_REPLAY': '1'}, self.db))
        self.assertEqual(self.owner(), 'dispatch')

    def test_webhook_cannot_register_callback_loop(self):
        self.assertFalse(events.register(self.receipt, {**self.env, 'HERMES_SESSION_PLATFORM': 'webhook'}, self.db))

    def test_decide_names_the_first_reason_variables_only(self):
        self.assertEqual(events.decide(self.env), (True, None))
        self.assertEqual(events.decide({**self.env, 'T3CTL_EVENTS_ENABLED': '0'}), (False, 'events-disabled'))
        self.assertEqual(events.decide({k: v for k, v in self.env.items() if k != 'HERMES_SESSION_KEY'}),
                         (False, 'missing:HERMES_SESSION_KEY'))
        self.assertEqual(events.decide({**self.env, 'HERMES_SESSION_PLATFORM': 'webhook', 'HERMES_SESSION_CHAT_ID': 'webhook:x:1'}),
                         (False, 'platform:webhook-not-in-origins'))

    def test_register_decision_logs_and_reports_registration_errors(self):
        log = self.db.with_name('decisions.log')
        out = events.register_decision(self.receipt, self.env, self.db, log)
        self.assertEqual(out, {'registered': True})
        blocker = self.db.with_name('blocker')
        blocker.write_text('a file where the events DB directory should be')
        bad = events.register_decision(self.receipt, self.env, blocker / 'events.sqlite', log)
        self.assertEqual(bad, {'registered': False, 'reason': 'registration-error'})
        off = events.register_decision(self.receipt, {**self.env, 'T3CTL_EVENTS_ENABLED': '0'}, self.db, log)
        self.assertEqual(off, {'registered': False, 'reason': 'events-disabled'})
        lines = [json.loads(l) for l in log.read_text().splitlines()]
        self.assertEqual([(l['monitoring'], l['reason']) for l in lines],
                         [('events', None), ('manual', 'registration-error'), ('manual', 'events-disabled')])
        self.assertTrue(all(l['present'] == ['HERMES_SESSION_ID', 'HERMES_SESSION_KEY', 'HERMES_SESSION_PLATFORM'] for l in lines))
        self.assertNotIn('parent', log.read_text())

    def test_listed_persistent_webhook_origin_registers(self):
        webhook = {**self.env, 'HERMES_SESSION_PLATFORM': 'webhook',
                   'HERMES_SESSION_CHAT_ID': 'webhook:linear-comment:thread-abc',
                   'T3CTL_EVENTS_ORIGINS': 'webhook:other, webhook:linear-comment:thread-'}
        self.assertTrue(events.register(self.receipt, webhook, self.db))
        one_shot = {**webhook, 'HERMES_SESSION_CHAT_ID': 'webhook:linear-comment:0f6e'}
        self.assertFalse(events.eligible(one_shot))
        self.assertFalse(events.eligible({**webhook, 'T3CTL_EVENTS_ORIGINS': ''}))


if __name__ == '__main__':
    unittest.main()
