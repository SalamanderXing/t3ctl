"""Durable event transitions and parent-session delivery, with no external effects."""
import asyncio
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
loader = SourceFileLoader('t3_events_test', os.environ.get('T3_EVENTS_TEST_BIN', str(ROOT / 'bin/t3-events')))
spec = spec_from_loader(loader.name, loader)
events = module_from_spec(spec)
loader.exec_module(events)


class RelayCase(unittest.TestCase):
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


class EventsTest(RelayCase):
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


# The 2026-11-16 incident, run by run (ordinals 12-24 of the real thread; ids
# anonymised, kinds as T3 recorded them): a dispatch whose turn only
# acknowledged, then turns the thread started itself — delegated-task (Sol
# review) results, PR-watch CI notices, a background command — that merged,
# promoted and deployed, then a person typing in the web app.
T = 'mcp%3Alaunch%3Aabc'
INCIDENT = [
    # (run, startType, startMessageId, createdBy, creationSource, summary, reply)
    ('run-12', 'user_message', 'message', 'agent', 'mcp', 'ship it once the review is clean',
     'Got it. Sol round 2 is still running; I will merge after it comes back clean.'),
    ('run-13', 'notification', f'message:thread:{T}:ordinal:169:4dcf', None, None,
     'Delegated task "Sol review r2" finished', 'Sol r2 found one issue; fixed it and started round 3.'),
    ('run-14', 'notification', f'message:thread:{T}:ordinal:180:6561', None, None,
     'Delegated task "Sol review r3" finished', 'Round 3 clean on the backend; round 4 for the UI.'),
    ('run-15', 'notification', f'message:thread:{T}:ordinal:184:5fc8', None, None,
     'Delegated task "Sol review r4" finished', 'Round 4 found a nit; fixed, round 5 started.'),
    ('run-16', 'notification', f'message:thread:{T}:ordinal:192:5135', None, None,
     'Delegated task "Sol review r5" finished', 'Sol round 5 is clean. Opened the PR to dev.'),
    ('run-17', 'notification', 'message:pr-watch:2398', None, None, '#101: new comments',
     'Fixed the bot review comment and pushed.'),
    ('run-18', 'notification', 'message:pr-watch:6e0b', None, None, '#101: checks passed, new comments',
     'Checks passed on the previous head; waiting for the new one.'),
    ('run-19', 'notification', 'message:pr-watch:1f89', None, None, '#101: checks failed',
     'Both failures come from dev, not this PR.'),
    ('run-20', 'notification', 'message:pr-watch:93ba', None, None, '#101: new comments',
     'Only the preview bot. Nothing to do.'),
    ('run-21', 'notification', 'message:pr-watch:48f0', None, None, '#101: checks passed, new comments',
     '#101 is merged into dev. The promo PR #102 to main is open.'),
    ('run-22', 'notification', 'message:pr-watch:9ec1', None, None, '#102: checks failed, checks passed',
     'Promo #102 is merged into main; prod is still deploying.'),
    ('run-23', 'notification', f'message:thread:{T}:ordinal:1:34b8', None, None,
     'Command "Poll prod health until promo commit serves" finished', 'Prod serves the promo commit. Release is live.'),
    ('run-24', 'user_message', 'c5a8df1c-d7c0-4ab9-924f-24ec9e65d6b4', 'user', 'web', 'is this online then?',
     'Yes, it is live.'),
]


class FakeT3:
    """`t3ctl watch` / `t3ctl origins` over an in-memory thread, as the relay calls them."""

    def __init__(self):
        self.runs = [{'runId': 'old', 'status': 'completed', 'startType': 'user_message', 'startMessageId': 'old-message',
                      'createdBy': 'user', 'creationSource': 'web', 'startSummary': 'earlier', 'reply': 'Earlier work', 'messages': []}]
        self.pending = None      # (runId, 'pending-user-input'|'pending-approval', [requests])
        self.children = 0
        self.calls = []

    def fold_human_message(self, into, queued):
        """A person types while `into` runs: T3 queues a run, then moves the message into `into` and cancels it."""
        message = {'startType': 'user_message', 'startMessageId': f'{queued}-msg', 'createdBy': 'user', 'creationSource': 'web'}
        next(r for r in self.runs if r['runId'] == into)['messages'].append(message)
        self.add(queued, 'user_message', f'{queued}-msg', 'user', 'web', status='cancelled', started=False)

    def add(self, run, start_type='notification', mid=None, created_by=None, source=None, summary=None, reply=None,
            status='completed', started=True):
        self.runs.append({'runId': run, 'status': status, 'startType': start_type, 'startMessageId': mid or f'message:thread:{run}',
                          'createdBy': created_by, 'creationSource': source, 'startSummary': summary, 'reply': reply,
                          'startedAt': '2026-11-16T14:00:00Z' if started else None, 'messages': []})
        return self.runs[-1]

    def incident(self, upto):
        for run, typ, mid, by, src, summary, reply in INCIDENT[:upto]:
            self.add(run, typ, mid, by, src, summary, reply)

    def state(self, run):
        return {'completed': 'completed', 'failed': 'error', 'running': 'running', 'queued': 'running'}.get(run['status'], 'interrupted')

    def __call__(self, cli, cmd, thread, *args):
        self.calls.append((cmd, *args))
        opts = dict(zip(args[::2], args[1::2]))
        latest = self.runs[-1]
        if cmd == 'watch':
            if '--message-id' in opts:
                expected = next((r for r in self.runs if r['startMessageId'] == opts['--message-id']), None)
                if expected is None:
                    return {'turnId': latest['runId'], 'reason': 'not-visible', 'turn': self.state(latest)}
                if expected is not latest:
                    return {'turnId': latest['runId'], 'reason': 'superseded', 'turn': 'superseded', 'lastAssistant': None}
            if opts.get('--after-turn') == latest['runId']:
                return {'turnId': latest['runId'], 'reason': 'not-visible', 'turn': self.state(latest)}
            if self.pending and self.pending[0] == latest['runId']:
                key = 'userInputs' if self.pending[1] == 'pending-user-input' else 'approvals'
                return {'turnId': latest['runId'], 'reason': self.pending[1], 'turn': 'running', key: self.pending[2]}
            state = self.state(latest)
            return {'turnId': latest['runId'], 'reason': 'timeout' if state == 'running' else 'settled', 'turn': state,
                    'lastAssistant': latest['reply'], 'lastError': None}
        assert cmd == 'origins', cmd
        if '--message-id' in opts:
            after = next((r['runId'] for r in self.runs if r['startMessageId'] == opts['--message-id']), None)
        else:
            after = opts['--after-turn']
        ids = [r['runId'] for r in self.runs]
        later = self.runs[ids.index(after) + 1:]
        out = {'afterRunId': after, 'afterFound': True, 'activeRunId': latest['runId'] if self.state(latest) == 'running' else None,
               'pendingRequestCount': len(self.pending[2]) if self.pending else 0, 'classified': False,
               'runs': [{'runId': r['runId'], 'status': r['status'], 'state': self.state(r), 'startedAt': r.get('startedAt')} for r in later]}
        if '--children' in args:
            out['activeChildren'] = self.children
        live = [r for r in later if not (r['status'] == 'cancelled' and not r.get('startedAt'))]
        with_after = '--message-id' in opts
        if (live or with_after) and ('--classify' in args or self.pending or any(self.state(r) != 'running' for r in live)):
            out['classified'] = True

            def describe(row, r):
                row.update({k: r[k] for k in ('startType', 'startMessageId', 'createdBy', 'creationSource', 'startSummary', 'messages')})
                if row['state'] != 'running':
                    row.update(lastAssistant=r['reply'], lastError=None)
                return row
            for row, r in zip(out['runs'], later):
                if r in live:
                    describe(row, r)
            if with_after:
                r = self.runs[ids.index(after)]
                out['afterRun'] = describe({'runId': after, 'status': r['status'], 'state': self.state(r)}, r)
        return out


class FollowTest(RelayCase):
    """After the dispatched turn ends, turns the thread starts itself are still reported."""

    def setUp(self):
        super().setUp()
        self.t3 = FakeT3()
        patcher = patch.object(events, 't3ctl_json', self.t3)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.environ = {k: v for k, v in os.environ.items() if not k.startswith('T3CTL_EVENTS_FOLLOW')}

    def poll(self, **env):
        with events.connect(self.db) as db:
            db.execute('UPDATE subscriptions SET next_poll=0')
        with patch.dict(os.environ, {**self.environ, **env}, clear=True):
            events.poll(self.db)

    def sub(self):
        with events.connect(self.db) as db:
            return dict(db.execute('SELECT * FROM subscriptions').fetchone())

    def payloads(self, disposition='pending'):
        with events.connect(self.db) as db:
            return [json.loads(r[0]) for r in db.execute(
                'SELECT payload FROM events WHERE disposition=? ORDER BY created_at, rowid', (disposition,))]

    def follow_to(self, upto, **env):
        """Play the incident up to run `upto` (1 = the dispatched turn), polling after each run."""
        for run, typ, mid, by, src, summary, reply in INCIDENT[:upto]:
            self.t3.add(run, typ, mid, by, src, summary, reply)
            self.poll(**env)

    def test_incident_sequence_reports_every_self_started_turn(self):
        self.follow_to(1)
        self.assertEqual(self.sub()['last_status'], 'following')
        self.assertEqual([(p['status'], p['origin']) for p in self.payloads()], [('completed', 'dispatch')])
        asyncio.run(events.deliver_pending(self.runner, self.db))
        # The Sol result starts a turn with no dispatch: running, nothing to say yet.
        run = self.t3.add('run-13', 'notification', INCIDENT[1][2], summary=INCIDENT[1][5], status='running')
        self.poll()
        self.assertEqual(self.payloads(), [])
        run.update(status='completed', reply=INCIDENT[1][6])
        self.poll()
        [event] = self.payloads()
        self.assertEqual((event['origin'], event['startKind'], event['turnId'], event['status']),
                         ('self', 'notification', 'run-13', 'completed'))
        self.assertEqual(event['startSummary'], 'Delegated task "Sol review r2" finished')
        self.assertEqual(event['lastAssistant'], INCIDENT[1][6])
        asyncio.run(events.deliver_pending(self.runner, self.db))
        call = self.runner._dispatch_plugin_message_injection.call_args.kwargs
        self.assertEqual((call['session_key'], call['expected_session_id']), ('route', 'parent'))
        self.assertIn('follow-up turn the thread started by itself', call['content'])
        # The rest of the release: every turn up to the deploy arrives, in order.
        for row in INCIDENT[2:12]:
            self.t3.add(*row)
            self.poll()
        delivered = self.payloads()
        self.assertEqual([p['turnId'] for p in delivered], [f'run-{n}' for n in range(14, 24)])
        self.assertIn('Release is live', delivered[-1]['lastAssistant'])
        self.assertEqual(delivered[-1]['startKind'], 'notification')
        self.assertTrue(all(p['startKind'] == 'pr-watch' for p in delivered[3:9]))
        # G types in the web app: the user took over, nothing more is sent.
        self.t3.add(*INCIDENT[12])
        self.poll()
        self.assertEqual(len(self.payloads()), 10)
        self.assertEqual((self.sub()['last_status'], self.sub()['follow_reason']), ('released', 'human'))
        self.t3.add('later', summary='Background command finished', reply='after takeover')
        self.poll()
        self.assertEqual(len(self.payloads()), 10)

    def test_turns_that_end_between_polls_are_each_reported(self):
        self.follow_to(1)
        for row in INCIDENT[5:9]:   # queued PR-watch notices run back to back
            self.t3.add(*row)
        self.poll()
        self.assertEqual([p['turnId'] for p in self.payloads() if p['origin'] == 'self'], ['run-17', 'run-18', 'run-19', 'run-20'])
        self.assertEqual(self.sub()['follow_turn'], 'run-20')

    def test_human_message_stops_following_even_after_self_turns(self):
        self.follow_to(2)
        self.t3.add(*INCIDENT[12])                       # the person's turn …
        self.t3.add('after', reply='queued subagent result')   # … then a subagent's
        self.poll()
        self.assertEqual([p['turnId'] for p in self.payloads()], ['run-12', 'run-13'])
        self.assertEqual(self.sub()['follow_reason'], 'human')
        self.assertEqual(self.sub()['last_status'], 'released')

    def test_another_senders_dispatch_stops_following(self):
        self.follow_to(1)
        self.t3.add('other', 'user_message', 'message:mcp:client%3Aother:thread-send:x', 'agent', 'mcp', reply='theirs')
        self.poll()
        self.assertEqual(self.sub()['follow_reason'], 'dispatch')
        self.assertEqual([p['origin'] for p in self.payloads()], ['dispatch'])

    def test_new_dispatch_replaces_the_follow(self):
        self.follow_to(2)
        self.t3.add('pending-self', reply='not yet delivered')
        self.poll()
        self.assertEqual(len(self.payloads()), 3)
        self.assertTrue(events.register({**self.receipt, 'dispatchId': 'next', 'messageId': 'next-message',
                                         'previousTurnId': 'pending-self'}, self.env, self.db))
        sub = self.sub()
        self.assertEqual((sub['dispatch_id'], sub['last_status'], sub['follow_turn'], sub['follow_events']), ('next', 'running', None, 0))
        self.assertEqual(self.payloads(), [])   # the old dispatch's undelivered follow-ups are superseded
        self.assertEqual(len(self.payloads('superseded')), 3)
        self.t3.add('next-run', 'user_message', 'next-message', 'agent', 'mcp', reply='Next done')
        self.poll()
        self.assertEqual([(p['turnId'], p['origin']) for p in self.payloads()], [('next-run', 'dispatch')])
        self.assertEqual(self.sub()['follow_turn'], 'next-run')

    def test_idle_thread_stops_following_unless_a_child_is_still_running(self):
        self.follow_to(1)
        self.poll()
        self.assertNotIn('--children', self.t3.calls[-1])   # not due: no child-thread listing
        with events.connect(self.db) as db:
            db.execute('UPDATE subscriptions SET follow_activity=?', (time.time() - 7 * 3600,))
        self.t3.children = 1        # a delegated review still running: keep following
        self.poll()
        self.assertIn('--children', self.t3.calls[-1])
        self.assertEqual(self.sub()['last_status'], 'following')
        self.t3.children = 0
        self.poll()
        self.assertEqual((self.sub()['last_status'], self.sub()['follow_reason']), ('released', 'idle'))
        self.assertEqual([p['origin'] for p in self.payloads()], ['dispatch'])   # silent stop

    def test_idle_window_is_configurable(self):
        self.follow_to(1)
        with events.connect(self.db) as db:
            db.execute('UPDATE subscriptions SET follow_activity=?', (time.time() - 2 * 3600,))
        self.poll()
        self.assertEqual(self.sub()['last_status'], 'following')   # 2 h < the default 6 h
        self.poll(T3CTL_EVENTS_FOLLOW_IDLE_HOURS='1')
        self.assertEqual(self.sub()['follow_reason'], 'idle')

    def test_running_turn_resets_the_idle_clock(self):
        self.follow_to(1)
        with events.connect(self.db) as db:
            db.execute('UPDATE subscriptions SET follow_activity=?', (time.time() - 7 * 3600,))
        self.t3.add('busy', status='running')
        self.poll()
        self.assertGreater(self.sub()['follow_activity'], time.time() - 60)
        self.assertEqual(self.sub()['last_status'], 'following')

    def test_cap_sends_one_final_event_then_stops(self):
        self.follow_to(1)
        for n in range(5):
            self.t3.add(f'self-{n}', reply=f'step {n}')
            self.poll(T3CTL_EVENTS_FOLLOW_CAP='3')
        statuses = [(p['turnId'], p['status']) for p in self.payloads() if p['origin'] == 'self']
        self.assertEqual(statuses, [('self-0', 'completed'), ('self-1', 'completed'), ('self-2', 'completed'),
                                    ('self-2', 'monitoring-stopped')])
        final = self.payloads()[-1]
        self.assertEqual((final['reason'], final['follow']['cap']), ('cap', 3))
        self.assertEqual((self.sub()['last_status'], self.sub()['follow_reason']), ('released', 'cap'))
        asyncio.run(events.deliver_pending(self.runner, self.db))
        self.assertIn('stopped following', self.runner._dispatch_plugin_message_injection.call_args.kwargs['content'])

    def test_default_cap_is_twenty(self):
        self.follow_to(1)
        for n in range(25):
            self.t3.add(f'self-{n}', reply=f'step {n}')
        self.poll()
        selfs = [p for p in self.payloads() if p['origin'] == 'self']
        self.assertEqual(len(selfs), 21)
        self.assertEqual(selfs[-1]['status'], 'monitoring-stopped')
        self.assertEqual(selfs[-2]['follow']['count'], 20)

    def test_pr_watch_turns_are_compact_by_default(self):
        self.follow_to(1)
        self.t3.add('ci', mid='message:pr-watch:1', summary='#101: checks failed', reply='x' * 5000)
        self.poll()
        event = self.payloads()[-1]
        self.assertEqual((event['startKind'], event['compact'], event['startSummary']), ('pr-watch', True, '#101: checks failed'))
        self.assertLess(len(event['lastAssistant']), 800)
        self.assertIn('t3ctl last thread', event['lastAssistant'])

    def test_pr_watch_full_and_skip_modes(self):
        self.follow_to(1)
        self.t3.add('ci-1', mid='message:pr-watch:1', reply='y' * 5000)
        self.poll(T3CTL_EVENTS_FOLLOW_PR_WATCH='full')
        self.assertEqual((self.payloads()[-1]['compact'], len(self.payloads()[-1]['lastAssistant'])), (False, 5000))
        self.t3.add('ci-2', mid='message:pr-watch:2', reply='noise')
        self.t3.add('sub', summary='Delegated task finished', reply='real work')
        self.poll(T3CTL_EVENTS_FOLLOW_PR_WATCH='skip')
        self.assertEqual([p['turnId'] for p in self.payloads()], ['run-12', 'ci-1', 'sub'])
        self.assertEqual(self.sub()['follow_turn'], 'sub')
        self.assertEqual(self.sub()['follow_events'], 2)   # a skipped turn uses none of the cap

    def test_no_double_delivery_across_polls_restarts_and_pending_requests(self):
        self.follow_to(1)
        run = self.t3.add('asks', status='running')
        question = [{'requestId': 'q1', 'questions': []}]
        self.t3.pending = ('asks', 'pending-user-input', question)
        for _ in range(3):
            self.poll()
        asks = [p for p in self.payloads() if p['origin'] == 'self']
        self.assertEqual([(p['turnId'], p['status'], p['requests']) for p in asks], [('asks', 'pending-user-input', question)])
        self.t3.pending = None
        run.update(status='completed', reply='answered and done')
        for _ in range(3):
            self.poll()
        selfs = [p for p in self.payloads() if p['origin'] == 'self']
        self.assertEqual([(p['turnId'], p['status']) for p in selfs], [('asks', 'completed')])   # the stale question is superseded
        self.assertEqual(len(self.payloads('superseded')), 1)
        asyncio.run(events.deliver_pending(self.runner, self.db))
        asyncio.run(events.deliver_pending(self.runner, self.db))
        self.assertEqual(self.runner._dispatch_plugin_message_injection.await_count, 2)   # dispatch + follow-up, once each
        # A gateway restart replays the same observation: same identity, nothing new.
        with events.connect(self.db) as db:
            db.execute("UPDATE subscriptions SET follow_turn='run-12'")
        self.poll()
        self.assertEqual(self.payloads(), [])

    def test_dispatched_turn_overtaken_by_a_self_turn_is_followed_not_superseded(self):
        # The ack ended and the subagent's turn began between two polls.
        self.t3.incident(2)
        self.t3.runs[-1]['status'] = 'running'
        self.poll()
        # its own end is reported as if seen in time, reply included; then the follow starts from it
        [ack] = self.payloads()
        self.assertEqual((ack['turnId'], ack['status'], ack['origin'], ack['lastAssistant']),
                         ('run-12', 'completed', 'dispatch', INCIDENT[0][6]))
        self.assertEqual((self.sub()['last_status'], self.sub()['follow_turn']), ('following', 'run-12'))
        self.assertIn('--classify', [c for c in self.t3.calls if c[0] == 'origins'][0])
        self.t3.runs[-1]['status'] = 'completed'
        self.poll()
        self.assertEqual([(p['turnId'], p['origin']) for p in self.payloads()], [('run-12', 'dispatch'), ('run-13', 'self')])

    def test_dispatched_turn_still_running_behind_a_queued_turn_waits(self):
        self.t3.add('ours', 'user_message', 'message', 'agent', 'mcp', status='running', reply='done later')
        self.t3.add('ci', mid='message:pr-watch:1', status='running')
        self.poll()
        self.assertEqual(self.payloads(), [])
        self.assertEqual(self.sub()['last_status'], 'running')
        self.t3.runs[1]['status'] = 'completed'
        self.poll()
        self.assertEqual([(p['turnId'], p['status']) for p in self.payloads()], [('ours', 'completed')])
        self.assertEqual(self.sub()['last_status'], 'following')

    def test_a_person_typing_into_the_dispatched_turn_is_still_superseded(self):
        self.t3.add('ours', 'user_message', 'message', 'agent', 'mcp', reply='ack')
        self.t3.fold_human_message('ours', 'queued')
        self.poll()
        self.assertEqual([p['status'] for p in self.payloads()], ['superseded'])

    def test_a_person_typing_into_a_running_self_turn_stops_the_follow_unreported(self):
        # 60 of the 99 never-started cancelled runs on the box are exactly this.
        self.follow_to(1)
        run = self.t3.add('ci', mid='message:pr-watch:1', status='running', reply='Sure, here is the screenshot.')
        self.t3.fold_human_message('ci', 'queued')
        self.poll()
        run['status'] = 'completed'
        self.poll()
        self.assertEqual([p['origin'] for p in self.payloads()], ['dispatch'])   # the person's reply is not sent
        self.assertEqual((self.sub()['last_status'], self.sub()['follow_reason']), ('released', 'human'))

    def test_a_cancelled_run_that_never_started_does_not_block_the_idle_stop(self):
        self.follow_to(1)
        self.t3.add('gone', 'notification', 'message:pr-watch:x', status='cancelled', started=False)
        self.poll()
        self.assertEqual(self.sub()['follow_turn'], 'gone')
        with events.connect(self.db) as db:
            db.execute('UPDATE subscriptions SET follow_activity=?', (time.time() - 7 * 3600,))
        self.poll()
        self.assertEqual(self.sub()['follow_reason'], 'idle')

    def test_a_replay_of_the_dispatch_does_not_restart_a_finished_follow(self):
        self.follow_to(2)
        self.t3.add(*INCIDENT[12])
        self.poll()
        replay = {**self.env, 'T3CTL_EVENTS_REPLAY': '1'}
        self.assertTrue(events.register(self.receipt, replay, self.db))
        self.assertEqual((self.sub()['last_status'], self.sub()['follow_reason']), ('released', 'human'))
        self.assertEqual(len(self.payloads()), 2)   # its undelivered follow-ups are not superseded either

    def test_cap_zero_means_no_cap(self):
        self.follow_to(1)
        for n in range(25):
            self.t3.add(f'self-{n}', reply=f'step {n}')
        self.poll(T3CTL_EVENTS_FOLLOW_CAP='0')
        self.assertEqual(len([p for p in self.payloads() if p['origin'] == 'self']), 25)
        self.assertEqual(self.sub()['last_status'], 'following')

    def test_dispatched_turn_overtaken_by_a_person_is_still_superseded(self):
        self.t3.add('ours', 'user_message', 'message', 'agent', 'mcp', reply='ack')
        self.t3.add(*INCIDENT[12])
        self.poll()
        self.assertEqual([p['status'] for p in self.payloads()], ['superseded'])
        self.assertEqual(self.sub()['last_status'], 'superseded')

    def test_follow_can_be_turned_off(self):
        self.follow_to(1, T3CTL_EVENTS_FOLLOW='0')
        self.assertEqual(self.sub()['last_status'], 'completed')
        self.t3.add(*INCIDENT[1])
        self.poll(T3CTL_EVENTS_FOLLOW='0')
        self.assertEqual(len(self.payloads()), 1)

    def test_follow_poll_outage_reports_monitoring_failed_and_keeps_undelivered_turns(self):
        self.follow_to(2)
        with patch.object(events, 't3ctl_json', side_effect=OSError('server unavailable')):
            for _ in range(8):
                self.poll()
        self.assertEqual([(p['turnId'], p['status']) for p in self.payloads()],
                         [('run-12', 'completed'), ('run-13', 'completed'), ('run-12', 'monitoring-failed')])
        self.assertEqual(self.sub()['last_status'], 'monitoring-failed')
        asyncio.run(events.deliver_pending(self.runner, self.db))
        self.assertNotIn('startKind: None', self.runner._dispatch_plugin_message_injection.call_args.kwargs['content'])

    def test_a_followed_turn_that_left_the_recent_runs_releases_instead_of_replaying(self):
        self.follow_to(1)
        original = self.t3.__call__
        def lost(cli, cmd, thread, *args):
            out = original(cli, cmd, thread, *args)
            return {**out, 'afterFound': False, 'runs': []} if cmd == 'origins' else out
        with patch.object(events, 't3ctl_json', lost):
            self.poll()
        self.assertEqual((self.sub()['last_status'], self.sub()['follow_reason']), ('released', 'lost'))

    def test_follow_events_are_cleaned_up_with_the_usual_retention(self):
        self.follow_to(2)
        asyncio.run(events.deliver_pending(self.runner, self.db))
        self.t3.add(*INCIDENT[12])
        self.poll()
        with events.connect(self.db) as db:
            db.execute('UPDATE events SET created_at=0')
            db.execute('UPDATE subscriptions SET created_at=0')
        self.poll()
        with events.connect(self.db) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM events').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM subscriptions').fetchone()[0], 0)


class StartKindTest(unittest.TestCase):
    def test_kinds_seen_in_t3(self):
        kind = events.start_kind
        self.assertEqual(kind({'startType': 'user_message', 'startMessageId': 'c5a8', 'createdBy': 'user', 'creationSource': 'web'}), 'human')
        self.assertEqual(kind({'startType': 'user_message', 'startMessageId': 'a1', 'createdBy': 'user', 'creationSource': 'mobile'}), 'human')
        self.assertEqual(kind({'startType': 'user_message', 'startMessageId': 'message:mcp:client%3Ax:thread-send:d',
                               'createdBy': 'agent', 'creationSource': 'mcp'}), 'dispatch')
        self.assertEqual(kind({'startType': 'user_message', 'startMessageId': 'm', 'createdBy': 'agent', 'creationSource': 'mcp'}, 'm'),
                         'own-dispatch')
        self.assertEqual(kind({'startType': 'user_message', 'startMessageId': 'b1', 'createdBy': 'user', 'creationSource': 'mcp'}), 'dispatch')
        self.assertEqual(kind({'startType': 'notification', 'startMessageId': 'message:pr-watch:1'}), 'pr-watch')
        self.assertEqual(kind({'startType': 'notification', 'startMessageId': f'message:thread:{T}:ordinal:169:x'}), 'notification')
        self.assertEqual(kind({'startType': 'notification', 'startMessageId': 'message:restart-continuation:1'}), 'restart')
        self.assertEqual(kind({'startType': 'user_message', 'startMessageId': 'async-answer:runtime-request:x',
                               'createdBy': 'user', 'creationSource': 'server'}), 'answer')
        self.assertEqual(kind({'startType': 'user_message', 'startMessageId': 'scheduled-task-message:s:1',
                               'createdBy': 'agent', 'creationSource': 'mcp'}), 'scheduled')
        self.assertEqual(kind({'startType': 'user_message', 'startMessageId': f'message:thread:{T}:ordinal:145:x',
                               'createdBy': 'agent', 'creationSource': 'server'}), 'agent')
        self.assertEqual(kind({}), 'unknown')

    def test_settings_fall_back_to_defaults(self):
        config = events.follow_config({'T3CTL_EVENTS_FOLLOW_CAP': 'lots', 'T3CTL_EVENTS_FOLLOW_PR_WATCH': 'loud',
                                       'T3CTL_EVENTS_FOLLOW_IDLE_HOURS': '-1'})
        self.assertEqual((config['enabled'], config['cap'], config['pr_watch'], config['idle']), (True, 20, 'compact', 6 * 3600))
        self.assertFalse(events.follow_config({'T3CTL_EVENTS_FOLLOW': '0'})['enabled'])


class ManagedFollowTest(unittest.TestCase):
    """t3-notify's suppression check reads T3's SQLite: the incident's runs and messages."""

    def setUp(self):
        (ROOT / 'tmp').mkdir(exist_ok=True)
        temp = tempfile.TemporaryDirectory(dir=ROOT / 'tmp')
        self.addCleanup(temp.cleanup)
        self.db = Path(temp.name) / 'events.sqlite'
        self.source = Path(temp.name) / 'statev2.sqlite'
        with sqlite3.connect(self.source) as db:
            db.execute('CREATE TABLE orchestration_v2_projection_runs (run_id TEXT, thread_id TEXT, ordinal INTEGER, payload_json TEXT)')
            db.execute('CREATE TABLE orchestration_v2_projection_messages (message_id TEXT PRIMARY KEY, thread_id TEXT, run_id TEXT, role TEXT, payload_json TEXT)')
        self.env = {'T3CTL_EVENTS_ENABLED': '1', 'HERMES_SESSION_ID': 'parent', 'HERMES_SESSION_KEY': 'route', 'HERMES_SESSION_PLATFORM': 'discord'}
        events.register({'threadId': 'thread', 'dispatchId': 'd', 'messageId': 'message', 'previousTurnId': 'old'}, self.env, self.db)

    def project(self, ordinal, run, typ, mid, by, src, *_):
        payload = {'createdBy': by or 'agent', 'creationSource': src or 'server', 'role': 'user'}
        if mid.startswith('message:pr-watch:'):
            payload['notification'] = {'source': {'kind': 'monitor'}}
        with sqlite3.connect(self.source) as db:
            db.execute('INSERT INTO orchestration_v2_projection_runs VALUES (?,?,?,?)',
                       (run, 'thread', ordinal, json.dumps({'id': run, 'userMessageId': mid})))
            db.execute('INSERT INTO orchestration_v2_projection_messages VALUES (?,?,?,?,?)', (mid, 'thread', run, 'user', json.dumps(payload)))

    def managed(self, **env):
        with patch.dict(os.environ, {'T3CTL_EVENTS_ENABLED': '1', **env}):
            return events.managed('thread', self.db, self.source)

    def test_self_started_turns_stay_managed_until_a_person_takes_over(self):
        self.project(12, *INCIDENT[0])
        self.assertTrue(self.managed())
        for n, row in enumerate(INCIDENT[1:12], start=13):
            self.project(n, *row)
            self.assertTrue(self.managed(), row[0])
        self.assertFalse(self.managed(T3CTL_EVENTS_FOLLOW='0'))
        self.project(24, *INCIDENT[12])
        self.assertFalse(self.managed())

    def test_a_person_folded_into_a_self_turn_restores_the_legacy_callback(self):
        self.project(12, *INCIDENT[0])
        self.project(13, *INCIDENT[5])   # a PR-watch turn …
        self.assertTrue(self.managed())
        with sqlite3.connect(self.source) as db:   # … into which T3 moved a person's message
            db.execute('INSERT INTO orchestration_v2_projection_messages VALUES (?,?,?,?,?)',
                       ('typed', 'thread', 'run-17', 'user', json.dumps({'createdBy': 'user', 'creationSource': 'web'})))
        self.assertFalse(self.managed())

    def test_skipped_pr_watch_turn_keeps_its_own_callback(self):
        self.project(12, *INCIDENT[0])
        self.project(13, *INCIDENT[5])
        self.assertTrue(self.managed())
        self.assertFalse(self.managed(T3CTL_EVENTS_FOLLOW_PR_WATCH='skip'))
        self.project(14, *INCIDENT[1])
        self.assertTrue(self.managed(T3CTL_EVENTS_FOLLOW_PR_WATCH='skip'))

    def test_a_turn_started_while_the_dispatch_waited_on_a_question_stays_managed(self):
        self.project(12, *INCIDENT[0])
        with events.connect(self.db) as db:
            db.execute("UPDATE subscriptions SET last_status='pending-user-input'")
        self.project(13, *INCIDENT[1])
        self.assertTrue(self.managed())

    def test_released_follow_or_unreadable_message_restores_the_legacy_callback(self):
        self.project(12, *INCIDENT[0])
        self.project(13, *INCIDENT[1])
        with events.connect(self.db) as db:
            db.execute("UPDATE subscriptions SET last_status='released', follow_reason='cap'")
        self.assertFalse(self.managed())
        with events.connect(self.db) as db:
            db.execute("UPDATE subscriptions SET last_status='following'")
        self.assertTrue(self.managed())
        with sqlite3.connect(self.source) as db:
            db.execute('DELETE FROM orchestration_v2_projection_messages WHERE message_id=?', (INCIDENT[1][2],))
        self.assertFalse(self.managed())


class SchemaTest(unittest.TestCase):
    def test_a_database_from_before_follow_mode_is_upgraded_in_place(self):
        (ROOT / 'tmp').mkdir(exist_ok=True)
        temp = tempfile.TemporaryDirectory(dir=ROOT / 'tmp')
        self.addCleanup(temp.cleanup)
        path = Path(temp.name) / 'old.sqlite'
        with sqlite3.connect(path) as db:
            db.execute('''CREATE TABLE subscriptions (
                thread_id TEXT PRIMARY KEY, dispatch_id TEXT NOT NULL,
                parent_session_id TEXT NOT NULL, session_key TEXT NOT NULL,
                previous_turn TEXT NOT NULL, message_id TEXT NOT NULL, turn_id TEXT, created_at REAL NOT NULL,
                last_status TEXT NOT NULL DEFAULT 'running',
                poll_failures INTEGER NOT NULL DEFAULT 0, next_poll REAL NOT NULL DEFAULT 0)''')
            db.execute("INSERT INTO subscriptions (thread_id, dispatch_id, parent_session_id, session_key, previous_turn, message_id, created_at) "
                       "VALUES ('t', 'd', 'p', 'k', 'old', 'm', 0)")
        with events.connect(path) as db:
            row = dict(db.execute('SELECT * FROM subscriptions').fetchone())
        self.assertEqual((row['follow_turn'], row['follow_events'], row['last_status']), (None, 0, 'running'))


if __name__ == '__main__':
    unittest.main()
