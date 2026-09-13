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

    def test_webhook_cannot_register_callback_loop(self):
        self.assertFalse(events.register(self.receipt, {**self.env, 'HERMES_SESSION_PLATFORM': 'webhook'}, self.db))


if __name__ == '__main__':
    unittest.main()
