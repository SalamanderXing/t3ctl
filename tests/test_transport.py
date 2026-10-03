"""Exercise CLI transport through the HTTP server, without models or live state."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from test_events import events

from fake_t3_server import Handler, Server, State

ROOT = Path(__file__).resolve().parents[1]
CLI = os.environ.get('T3CTL_TEST_BIN', str(ROOT / 'bin/t3ctl'))


class TransportTest(unittest.TestCase):
    def setUp(self):
        (ROOT / 'tmp').mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / 'tmp')
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        token = self.path / 'token'
        token.write_text('test-token')
        self.thread = {
            'id': 'test-thread', 'title': '[test] task', 'projectId': 'project',
            'runtimeMode': 'approval-required', 'interactionMode': 'plan',
            'modelSelection': {'instanceId': 'codex', 'model': 'test'},
            'runs': [{'id': 'old', 'status': 'completed', 'userMessageId': 'old-message'}],
            'messages': [], 'requests': [],
        }
        self.server = Server(('127.0.0.1', 0), Handler)
        self.server.tokens_file = token
        self.server.state = State({'threads': [self.thread]}, self.path / 'commands.json')
        worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.source_db = self.path / 'statev2.sqlite'
        with sqlite3.connect(self.source_db) as db:
            db.execute('CREATE TABLE orchestration_v2_projection_runs (run_id TEXT, thread_id TEXT, ordinal INTEGER, payload_json TEXT)')
        self.project_run('old', 'old-message')
        dispatch = self.server.state.dispatch
        def dispatch_and_project(command):
            result = dispatch(command)
            if command['type'] == 'message.dispatch':
                self.project_run(self.thread['runs'][-1]['id'], command['messageId'])
            return result
        self.server.state.dispatch = dispatch_and_project
        self.env = {**os.environ, 'T3CTL_DB': str(self.source_db), 'T3CTL_CONF': '/dev/null', 'T3CTL_TOKEN_MODE': 'file',
                    'T3CTL_TOKEN_FILE': str(token), 'T3CTL_TAG': '[test]',
                    'T3CTL_URL': f'http://127.0.0.1:{self.server.server_port}',
                    'T3CTL_DENY_ORIGINS': '', 'T3CTL_TEXT_CAP': '3000'}

    def project_run(self, run_id, message_id):
        with sqlite3.connect(self.source_db) as db:
            ordinal = db.execute('SELECT COALESCE(MAX(ordinal), 0) + 1 FROM orchestration_v2_projection_runs').fetchone()[0]
            db.execute('INSERT INTO orchestration_v2_projection_runs VALUES (?,?,?,?)',
                       (run_id, 'test-thread', ordinal, json.dumps({'id': run_id, 'userMessageId': message_id})))

    def cli(self, *args, text=None, success=True):
        result = subprocess.run(['bash', CLI, *args], input=text, text=True,
                                capture_output=True, env=self.env, timeout=25)
        if success:
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)
        self.assertNotEqual(result.returncode, 0)
        return result

    def managed(self):
        result = subprocess.run([str(Path(CLI).parent / 't3-events'), 'managed', '--thread', 'test-thread'],
                                env=self.env, capture_output=True, text=True, check=True)
        return json.loads(result.stdout)['managed']

    QUESTIONS = [{'id': 'storage', 'header': 'Storage', 'question': 'Where?',
                  'options': [{'label': 'Existing database', 'description': 'Reuse it'}]}]

    def request(self, rid='input-1', status='pending'):
        return {'id': rid, 'kind': 'user_input', 'status': status, 'runId': 'old', 'questions': self.QUESTIONS}

    def test_large_watch_response(self):
        self.thread['messages'] = [{'role': 'assistant', 'text': 'result ' * 50000, 'runId': 'old'}]
        out = self.cli('watch', 'test-thread', '--timeout', '0')
        self.assertEqual(out['reason'], 'settled')
        self.assertLess(len(out['lastAssistant']), 3100)

    def test_literal_large_prompt_from_stdin(self):
        prompt = 'Keep `bodyData`, $(false), "quotes", \\slashes\n' * 5000
        out = self.cli('say', 'test-thread', '--prompt-file', '-', text=prompt)
        self.assertEqual(self.server.state.dispatched[-1]['text'], prompt)
        self.assertEqual(out['previousTurnId'], 'old')
        self.assertEqual(out['turnId'], self.thread['runs'][-1]['id'])

    def test_execute_exits_plan_without_changing_permissions(self):
        self.cli('say', 'test-thread', 'Implement', '--execute')
        types = [c['type'] for c in self.server.state.dispatched]
        # V2 messages carry no modes: plan → default is its own command, the
        # runtime mode is left alone.
        self.assertEqual(types, ['thread.interaction-mode.set', 'message.dispatch'])
        self.assertEqual(self.server.state.dispatched[0]['interactionMode'], 'default')
        self.assertEqual(self.thread['runtimeMode'], 'approval-required')

    def test_questions_visible_and_resolved_requests_excluded(self):
        self.thread['requests'] = [self.request('resolved', 'resolved'), self.request()]
        out = self.cli('watch', 'test-thread', '--timeout', '0')
        self.assertEqual(out['reason'], 'pending-user-input')
        self.assertEqual(out['userInputs'], [{'requestId': 'input-1', 'questions': self.QUESTIONS}])
        self.assertEqual(self.cli('show', 'test-thread')['userInputs'], out['userInputs'])

    def test_answer_uses_pending_request(self):
        self.thread['requests'] = [self.request()]
        answers = {'storage': 'Existing database'}
        self.cli('answer', 'test-thread', 'input-1', '--answers-file', '-', text=json.dumps(answers))
        cmd = self.server.state.dispatched[-1]
        self.assertEqual(cmd['type'], 'runtime-request.respond')
        self.assertEqual(cmd['answers'], answers)
        self.cli('answer', 'test-thread', 'unknown', '--answers-file', '-', text='{}', success=False)
        self.assertEqual(len(self.server.state.dispatched), 1)

    def test_old_completion_is_not_new_completion(self):
        self.thread['messages'] = [{'role': 'assistant', 'text': 'OLD RESULT', 'runId': 'old'}]
        out = self.cli('watch', 'test-thread', '--after-turn', 'old', '--timeout', '0')
        self.assertEqual(out['reason'], 'not-visible')
        self.assertIsNone(out['lastAssistant'])

    def test_dispatch_registers_origin_and_legacy_callback_is_suppressed(self):
        self.env.update({'T3CTL_EVENTS_ENABLED': '1', 'T3CTL_EVENTS_DB': str(self.path / 'events.sqlite'),
                         'HERMES_SESSION_ID': 'parent', 'HERMES_SESSION_KEY': 'route',
                         'HERMES_SESSION_PLATFORM': 'discord'})
        out = self.cli('say', 'test-thread', 'Continue')
        self.assertEqual(out['monitoring'], 'events')
        result = subprocess.run([str(ROOT / 'bin/t3-notify'), '--thread', 'test-thread', '--status', 'done', 'Ready'],
                                env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('legacy callback suppressed', result.stdout)

    def test_delayed_dispatch_cannot_adopt_foreign_output(self):
        outbox = self.path / 'events.sqlite'
        self.env.update({'T3CTL_EVENTS_ENABLED': '1', 'T3CTL_EVENTS_DB': str(outbox),
                         'HERMES_SESSION_ID': 'parent', 'HERMES_SESSION_KEY': 'route',
                         'HERMES_SESSION_PLATFORM': 'discord'})
        receipt = {'threadId': 'test-thread', 'dispatchId': 'dispatch', 'messageId': 'delayed-message',
                   'previousTurnId': 'old', 'turnId': None}
        self.assertTrue(events.register(receipt, self.env, outbox))
        self.thread['runs'].append({'id': 'foreign', 'status': 'completed', 'userMessageId': 'other'})
        self.thread['messages'] = [{'role': 'assistant', 'text': 'FOREIGN OUTPUT', 'runId': 'foreign'}]
        with patch.dict(os.environ, self.env, clear=True):
            events.poll(outbox, CLI)
        self.assertEqual(events.pending(outbox), [])
        # our message's run appears, but a later run already took the thread over
        self.thread['runs'].insert(-1, {'id': 'owned', 'status': 'completed', 'userMessageId': 'delayed-message'})
        with patch.dict(os.environ, self.env, clear=True):
            events.poll(outbox, CLI)
        payload = json.loads(events.pending(outbox)[0]['payload'])
        self.assertEqual(payload['status'], 'superseded')
        self.assertIsNone(payload['lastAssistant'])

    def test_message_correlation_can_resolve_a_delayed_owned_turn(self):
        self.thread['runs'].append({'id': 'owned', 'status': 'completed', 'userMessageId': 'message'})
        self.thread['messages'] = [{'role': 'assistant', 'text': 'OWNED OUTPUT', 'runId': 'owned'}]
        out = self.cli('watch', 'test-thread', '--message-id', 'message', '--timeout', '0')
        self.assertEqual(out['lastAssistant'], 'OWNED OUTPUT')
        self.assertEqual(out['turnId'], 'owned')

    def test_manual_or_desktop_takeover_restores_legacy_callback(self):
        outbox = self.path / 'events.sqlite'
        self.env.update({'T3CTL_EVENTS_ENABLED': '1', 'T3CTL_EVENTS_DB': str(outbox),
                         'HERMES_SESSION_ID': 'parent', 'HERMES_SESSION_KEY': 'route',
                         'HERMES_SESSION_PLATFORM': 'discord'})
        self.cli('say', 'test-thread', 'Managed work')
        self.assertTrue(self.managed())
        self.env['T3CTL_EVENTS_ENABLED'] = '0'
        self.assertFalse(self.managed())
        self.env['T3CTL_EVENTS_ENABLED'] = '1'
        self.project_run('desktop-run', 'desktop-message')   # a newer run started from the app
        self.assertFalse(self.managed())
        self.env['T3CTL_EVENTS_ENABLED'] = '0'
        out = self.cli('say', 'test-thread', 'Manual work')
        self.assertEqual(out['monitoring'], 'manual')
        self.assertFalse(self.managed())

    def test_ownership_lookup_failure_still_delivers_legacy_callback(self):
        received = []
        class CallbackHandler(Handler):
            def do_POST(self):
                received.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                self._json(200, {'status': 'ok'})
        gateway = Server(('127.0.0.1', 0), CallbackHandler)
        threading.Thread(target=gateway.serve_forever, daemon=True).start()
        self.addCleanup(gateway.server_close)
        self.addCleanup(gateway.shutdown)
        secret = self.path / 'notify-secret'
        secret.write_text('test-only-secret')
        self.env.update({'T3CTL_EVENTS_ENABLED': '1', 'T3CTL_EVENTS_DB': str(self.path / 'events.sqlite'),
                         'HERMES_SESSION_ID': 'parent', 'HERMES_SESSION_KEY': 'route',
                         'HERMES_SESSION_PLATFORM': 'discord', 'T3_NOTIFY_SECRET_FILE': str(secret),
                         'T3_NOTIFY_URL': f'http://127.0.0.1:{gateway.server_port}/callback'})
        self.cli('say', 'test-thread', 'Managed work')
        self.source_db.unlink()
        result = subprocess.run([str(Path(CLI).parent / 't3-notify'), '--thread', 'test-thread', '--status', 'done', 'Verified output'],
                                env=self.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]['message'], 'Verified output')
        self.assertIn('sending legacy callback', result.stderr)

    def test_outbox_failure_does_not_repeat_or_veto_primary_dispatch(self):
        self.env.update({'T3CTL_EVENTS_ENABLED': '1', 'T3CTL_EVENTS_DB': str(self.path / 'token' / 'events.sqlite'),
                         'HERMES_SESSION_ID': 'parent', 'HERMES_SESSION_KEY': 'route',
                         'HERMES_SESSION_PLATFORM': 'discord'})
        out = self.cli('say', 'test-thread', 'Continue')
        self.assertEqual(out['monitoring'], 'manual')
        self.assertEqual(len(self.server.state.dispatched), 1)

    def test_invalid_inputs_cannot_dispatch(self):
        self.cli('say', 'test-thread', 'text', '--prompt-file', '-', text='other', success=False)
        self.cli('answer', 'test-thread', 'input-1', '--answers-file', '-', text='[]', success=False)
        self.cli('say', 'test-thread', '--prompt-file', success=False)
        self.assertEqual(self.server.state.dispatched, [])


if __name__ == '__main__':
    unittest.main()
