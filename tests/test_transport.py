"""Exercise CLI transport through the HTTP server, without models or live state."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest

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
            'latestTurn': {'turnId': 'old', 'state': 'completed'},
            'session': {'status': 'ready', 'lastError': None},
            'activities': [], 'messages': [], 'hasPendingUserInput': False,
        }
        self.server = Server(('127.0.0.1', 0), Handler)
        self.server.tokens_file = token
        self.server.state = State({'threads': [self.thread]}, self.path / 'commands.json')
        worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.env = {**os.environ, 'T3CTL_CONF': '/dev/null', 'T3CTL_TOKEN_MODE': 'file',
                    'T3CTL_TOKEN_FILE': str(token), 'T3CTL_TAG': '[test]',
                    'T3CTL_URL': f'http://127.0.0.1:{self.server.server_port}',
                    'T3CTL_DENY_ORIGINS': '', 'T3CTL_TEXT_CAP': '3000'}

    def cli(self, *args, text=None, success=True):
        result = subprocess.run(['bash', CLI, *args], input=text, text=True,
                                capture_output=True, env=self.env, timeout=25)
        if success:
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)
        self.assertNotEqual(result.returncode, 0)
        return result

    def request(self, rid='input-1'):
        return {'kind': 'user-input.requested', 'tone': 'info', 'turnId': 'old',
                'payload': {'requestId': rid, 'questions': [
                    {'id': 'storage', 'header': 'Storage', 'question': 'Where?',
                     'options': [{'label': 'Existing database', 'description': 'Reuse it'}]}]}}

    def test_large_watch_response(self):
        self.thread['messages'] = [{'role': 'assistant', 'text': 'result ' * 50000}]
        out = self.cli('watch', 'test-thread', '--timeout', '0')
        self.assertEqual(out['reason'], 'settled')
        self.assertLess(len(out['lastAssistant']), 3100)

    def test_literal_large_prompt_from_stdin(self):
        prompt = 'Keep `bodyData`, $(false), "quotes", \\slashes\n' * 5000
        out = self.cli('say', 'test-thread', '--prompt-file', '-', text=prompt)
        self.assertEqual(self.server.state.dispatched[-1]['message']['text'], prompt)
        self.assertEqual(out['previousTurnId'], 'old')
        self.assertEqual(out['turnId'], self.thread['latestTurn']['turnId'])

    def test_execute_exits_plan_without_changing_permissions(self):
        self.cli('say', 'test-thread', 'Implement', '--execute')
        cmd = self.server.state.dispatched[-1]
        self.assertEqual(cmd['interactionMode'], 'default')
        self.assertEqual(cmd['runtimeMode'], 'approval-required')

    def test_questions_visible_and_resolved_requests_excluded(self):
        self.thread['hasPendingUserInput'] = True
        self.thread['activities'] = [self.request('resolved'),
            {'kind': 'user-input.resolved', 'payload': {'requestId': 'resolved'}}, self.request()]
        out = self.cli('watch', 'test-thread', '--timeout', '0')
        self.assertEqual(out['reason'], 'pending-user-input')
        self.assertEqual(out['userInputs'], [self.request()['payload']])
        self.assertEqual(self.cli('show', 'test-thread')['userInputs'], out['userInputs'])

    def test_answer_uses_pending_request(self):
        self.thread['activities'] = [self.request()]
        answers = {'storage': 'Existing database'}
        self.cli('answer', 'test-thread', 'input-1', '--answers-file', '-', text=json.dumps(answers))
        cmd = self.server.state.dispatched[-1]
        self.assertEqual(cmd['type'], 'thread.user-input.respond')
        self.assertEqual(cmd['answers'], answers)
        self.cli('answer', 'test-thread', 'unknown', '--answers-file', '-', text='{}', success=False)
        self.assertEqual(len(self.server.state.dispatched), 1)

    def test_old_completion_is_not_new_completion(self):
        self.thread['messages'] = [{'role': 'assistant', 'text': 'OLD RESULT'}]
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

    def test_invalid_inputs_cannot_dispatch(self):
        self.cli('say', 'test-thread', 'text', '--prompt-file', '-', text='other', success=False)
        self.cli('answer', 'test-thread', 'input-1', '--answers-file', '-', text='[]', success=False)
        self.cli('say', 'test-thread', '--prompt-file', success=False)
        self.assertEqual(self.server.state.dispatched, [])


if __name__ == '__main__':
    unittest.main()
