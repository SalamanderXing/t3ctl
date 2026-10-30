"""Exercise CLI transport through the HTTP server, without models or live state."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.request
from unittest.mock import patch

from test_events import events

from fake_t3_server import Handler, Server, State

ROOT = Path(__file__).resolve().parents[1]
CLI = os.environ.get('T3CTL_TEST_BIN', str(ROOT / 'bin/t3ctl'))
MCP = str(Path(CLI).parent / 't3-mcp')


class TransportTest(unittest.TestCase):
    def setUp(self):
        (ROOT / 'tmp').mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / 'tmp')
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        token = self.path / 'token'
        token.write_text('test-token')
        (self.path / 'token.mcp').write_text('test-mcp-token')     # default: next to the legacy token
        mcp_tokens = self.path / 'mcp-tokens'
        mcp_tokens.write_text('test-mcp-token full-access\n')
        self.thread = {
            'id': 'test-thread', 'title': '[test] task', 'projectId': 'project',
            'runtimeMode': 'approval-required', 'interactionMode': 'plan',
            'modelSelection': {'instanceId': 'codex', 'model': 'test'},
            'runs': [{'id': 'old', 'status': 'completed', 'userMessageId': 'old-message'}],
            'messages': [], 'requests': [],
        }
        self.server = Server(('127.0.0.1', 0), Handler)
        self.server.tokens_file = token
        self.server.mcp_tokens_file = mcp_tokens
        self.server.state = State({'threads': [self.thread]}, self.path / 'commands.json')
        worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.source_db = self.path / 'statev2.sqlite'
        with sqlite3.connect(self.source_db) as db:
            db.execute('CREATE TABLE orchestration_v2_projection_runs (run_id TEXT, thread_id TEXT, ordinal INTEGER, payload_json TEXT)')
        self.project_run('old', 'old-message')
        # every new run (legacy dispatch or MCP send) reaches t3's SQLite, which `managed` reads
        self.server.state.run_hooks.append(lambda thread, run, mid: self.project_run(run['id'], mid))
        self.env = {**{k: v for k, v in os.environ.items() if not k.startswith(('T3CTL_', 'HERMES_'))},
                    'T3CTL_DB': str(self.source_db), 'T3CTL_CONF': '/dev/null', 'T3CTL_TOKEN_MODE': 'file',
                    'T3CTL_TOKEN_FILE': str(token), 'T3CTL_TAG': '[test]',
                    'T3CTL_URL': f'http://127.0.0.1:{self.server.server_port}',
                    'T3CTL_DENY_ORIGINS': '', 'T3CTL_TEXT_CAP': '3000', 'T3CTL_LOCK_DIR': str(self.path / 'locks'),
                    'T3CODE_HOME': str(self.path)}

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

    def sent(self, kind=None):
        return [c for c in self.server.state.dispatched if kind is None or c['type'] == kind]

    def managed(self):
        result = subprocess.run([str(Path(CLI).parent / 't3-events'), 'managed', '--thread', 'test-thread'],
                                env=self.env, capture_output=True, text=True, check=True)
        return json.loads(result.stdout)['managed']

    def events_env(self, **extra):
        self.env.update({'T3CTL_EVENTS_ENABLED': '1', 'T3CTL_EVENTS_DB': str(self.path / 'events.sqlite'),
                         'HERMES_SESSION_ID': 'parent', 'HERMES_SESSION_KEY': 'route',
                         'HERMES_SESSION_PLATFORM': 'discord', **extra})

    def log_lines(self):
        log = self.path / 't3ctl-events.log'
        return [json.loads(l) for l in log.read_text().splitlines()] if log.exists() else []

    QUESTIONS = [{'id': 'storage', 'header': 'Storage', 'question': 'Where?',
                  'options': [{'label': 'Existing database', 'description': 'Reuse it'}]}]

    def request(self, rid='input-1', status='pending'):
        return {'id': rid, 'kind': 'user_input', 'status': status, 'runId': 'old', 'questions': self.QUESTIONS}

    def test_large_watch_response(self):
        self.thread['messages'] = [{'role': 'assistant', 'text': 'result ' * 50000, 'runId': 'old'}]
        out = self.cli('watch', 'test-thread', '--timeout', '0')
        self.assertEqual(out['reason'], 'settled')
        self.assertLess(len(out['lastAssistant']), 3100)

    def test_last_reassembles_a_reply_longer_than_one_mcp_item(self):
        reply = ''.join(f'{i:06d}' for i in range(20000))   # 120 000 chars > maxCharsPerItem
        self.thread['messages'] = [{'role': 'assistant', 'text': reply, 'runId': 'old'}]
        result = subprocess.run(['bash', CLI, 'last', 'test-thread'], text=True, capture_output=True, env=self.env, timeout=25)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, reply + '\n')

    def test_literal_large_prompt_from_stdin(self):
        prompt = 'Keep `bodyData`, $(false), "quotes", \\slashes\n' * 5000
        out = self.cli('say', 'test-thread', '--prompt-file', '-', text=prompt)
        self.assertEqual(self.sent('mcp.t3_thread_send')[-1]['message'], prompt)
        self.assertEqual(out['previousTurnId'], 'old')
        self.assertEqual(out['turnId'], self.thread['runs'][-1]['id'])
        self.assertEqual(out['dispatchId'], self.sent('mcp.t3_thread_send')[-1]['clientRequestId'])

    def test_execute_exits_plan_without_changing_permissions(self):
        self.cli('say', 'test-thread', 'Implement', '--execute')
        types = [c['type'] for c in self.server.state.dispatched]
        # MCP keeps modes fixed: plan → default is its own (legacy) command,
        # the runtime mode is left alone, the message goes over MCP.
        self.assertEqual(types, ['thread.interaction-mode.set', 'mcp.t3_thread_send'])
        self.assertEqual(self.server.state.dispatched[0]['interactionMode'], 'default')
        self.assertEqual(self.thread['runtimeMode'], 'approval-required')

    def test_say_model_configures_the_thread_over_mcp(self):
        self.cli('say', 'test-thread', 'Use the other model', '--model', 'claudeAgent:claude-opus-5-5')
        types = [c['type'] for c in self.server.state.dispatched]
        self.assertEqual(types, ['mcp.t3_thread_configure', 'mcp.t3_thread_send'])
        self.assertEqual(self.thread['modelSelection'], {'instanceId': 'claudeAgent', 'model': 'claude-opus-5-5'})

    def test_questions_visible_and_resolved_requests_excluded(self):
        self.thread['runs'][-1]['status'] = 'waiting'
        self.thread['requests'] = [self.request('resolved', 'resolved'), self.request()]
        out = self.cli('watch', 'test-thread', '--timeout', '0')
        self.assertEqual(out['reason'], 'pending-user-input')
        self.assertEqual(out['userInputs'], [{'requestId': 'input-1', 'questions': self.QUESTIONS}])
        self.assertEqual(self.cli('show', 'test-thread')['userInputs'], out['userInputs'])

    def test_answer_uses_pending_request(self):
        self.thread['requests'] = [self.request()]
        answers = {'storage': 'Existing database'}
        out = self.cli('answer', 'test-thread', 'input-1', '--answers-file', '-', text=json.dumps(answers))
        self.assertIn('sequence', out)
        cmd = self.sent()[-1]
        self.assertEqual(cmd['type'], 'mcp.t3_pending_request')
        self.assertEqual((cmd['action'], cmd['answers']), ('respond', answers))
        self.cli('answer', 'test-thread', 'unknown', '--answers-file', '-', text='{}', success=False)
        self.cli('answer', 'test-thread', 'input-1', '--answers-file', '-', text='{}', success=False)   # already answered
        self.assertEqual(len(self.sent()), 1)

    def test_old_completion_is_not_new_completion(self):
        self.thread['messages'] = [{'role': 'assistant', 'text': 'OLD RESULT', 'runId': 'old'}]
        out = self.cli('watch', 'test-thread', '--after-turn', 'old', '--timeout', '0')
        self.assertEqual(out['reason'], 'not-visible')
        self.assertIsNone(out['lastAssistant'])

    def test_watch_blocks_server_side_until_the_run_settles(self):
        self.thread['runs'].append({'id': 'live', 'status': 'running', 'userMessageId': 'm'})
        self.thread['messages'] = [{'role': 'assistant', 'text': 'DONE', 'runId': 'live'}]

        def finish():
            time.sleep(1.5)
            self.thread['runs'][-1]['status'] = 'completed'
        threading.Thread(target=finish, daemon=True).start()
        self.env['T3CTL_WATCH_SLICE'] = '10'
        out = self.cli('watch', 'test-thread', '--timeout', '20')
        self.assertEqual((out['reason'], out['turn'], out['lastAssistant']), ('settled', 'completed', 'DONE'))
        self.assertIn('t3_thread_wait', self.server.mcp_calls)

    def test_pending_approval_ends_a_watch_slice(self):
        self.thread['runs'].append({'id': 'live', 'status': 'running', 'userMessageId': 'm'})

        def ask():
            time.sleep(1)
            self.thread['requests'].append({'id': 'r9', 'kind': 'command', 'status': 'pending', 'runId': 'live', 'command': 'make'})
            self.thread['runs'][-1]['status'] = 'waiting'
        threading.Thread(target=ask, daemon=True).start()
        self.env['T3CTL_WATCH_SLICE'] = '1'
        out = self.cli('watch', 'test-thread', '--timeout', '20')
        self.assertEqual(out['reason'], 'pending-approval')
        self.assertEqual([a['payload']['detail'] for a in out['approvals']], ['make'])   # exact command, legacy path

    def test_dispatch_registers_origin_and_legacy_callback_is_suppressed(self):
        self.events_env()
        out = self.cli('say', 'test-thread', 'Continue')
        self.assertEqual(out['monitoring'], 'events')
        self.assertNotIn('monitoringReason', out)
        result = subprocess.run([str(ROOT / 'bin/t3-notify'), '--thread', 'test-thread', '--status', 'done', 'Ready'],
                                env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('legacy callback suppressed', result.stdout)
        line = self.log_lines()[-1]
        self.assertEqual((line['monitoring'], line['reason'], line['platform']), ('events', None, 'discord'))
        self.assertEqual(line['present'], ['HERMES_SESSION_ID', 'HERMES_SESSION_KEY', 'HERMES_SESSION_PLATFORM'])

    def test_every_manual_fallback_names_its_reason_without_values(self):
        cases = [
            ({'T3CTL_EVENTS_ENABLED': '0'}, 'events-disabled'),
            ({'HERMES_SESSION_KEY': ''}, 'missing:HERMES_SESSION_KEY'),
            ({'HERMES_SESSION_ID': '', 'HERMES_SESSION_PLATFORM': ''}, 'missing:HERMES_SESSION_ID,HERMES_SESSION_PLATFORM'),
            ({'HERMES_SESSION_PLATFORM': 'webhook', 'HERMES_SESSION_CHAT_ID': 'webhook:secret-route:d1'}, 'platform:webhook-not-in-origins'),
            ({'HERMES_SESSION_PLATFORM': 'api_server'}, 'platform:api_server-not-in-origins'),
        ]
        for extra, reason in cases:
            with self.subTest(reason=reason):
                self.events_env(**extra)
                out = self.cli('say', 'test-thread', 'Continue')
                self.assertEqual((out['monitoring'], out['monitoringReason']), ('manual', reason))
                line = self.log_lines()[-1]
                self.assertEqual((line['threadId'], line['monitoring'], line['reason']), ('test-thread', 'manual', reason))
                self.assertEqual(line['dispatchId'], out['dispatchId'])
                for value in ('parent', 'route', 'secret-route'):
                    self.assertNotIn(value, json.dumps(line))   # names only, never values

    def test_decision_log_is_size_capped(self):
        self.events_env(T3CTL_EVENTS_LOG_MAX='400')
        for _ in range(4):
            self.cli('say', 'test-thread', 'Continue')
        log = self.path / 't3ctl-events.log'
        self.assertLessEqual(log.stat().st_size, 400)
        self.assertTrue((self.path / 't3ctl-events.log.1').exists())

    def test_listed_webhook_origin_is_event_managed(self):
        self.events_env(HERMES_SESSION_PLATFORM='webhook', HERMES_SESSION_CHAT_ID='webhook:linear-comment:thread-root')
        out = self.cli('say', 'test-thread', 'One-shot')
        self.assertEqual((out['monitoring'], out['monitoringReason']), ('manual', 'platform:webhook-not-in-origins'))
        self.env['T3CTL_EVENTS_ORIGINS'] = 'webhook:linear-comment:thread-'
        self.assertEqual(self.cli('say', 'test-thread', 'Thread session')['monitoring'], 'events')

    def test_delayed_dispatch_cannot_adopt_foreign_output(self):
        outbox = self.path / 'events.sqlite'
        self.events_env()
        receipt = {'threadId': 'test-thread', 'dispatchId': 'dispatch', 'messageId': 'delayed-message',
                   'previousTurnId': 'old', 'turnId': None}
        self.assertTrue(events.register(receipt, self.env, outbox))
        self.thread['runs'].append({'id': 'foreign', 'status': 'completed', 'userMessageId': 'other'})
        self.thread['messages'] = [{'id': 'other', 'role': 'user', 'text': 'desktop', 'runId': 'foreign'},
                                   {'role': 'assistant', 'text': 'FOREIGN OUTPUT', 'runId': 'foreign'}]
        with patch.dict(os.environ, self.env, clear=True):
            events.poll(outbox, CLI)
        self.assertEqual(events.pending(outbox), [])
        # our message's run appears (the MCP user-message item records it), but a later run already took the thread over
        self.thread['runs'].insert(-1, {'id': 'owned', 'status': 'completed', 'userMessageId': 'delayed-message'})
        self.thread['messages'].insert(0, {'id': 'delayed-message', 'role': 'user', 'text': 'ours', 'runId': 'owned'})
        with patch.dict(os.environ, self.env, clear=True):
            events.poll(outbox, CLI)
        payload = json.loads(events.pending(outbox)[0]['payload'])
        self.assertEqual(payload['status'], 'superseded')
        self.assertIsNone(payload['lastAssistant'])

    def test_message_correlation_can_resolve_a_delayed_owned_turn(self):
        self.thread['runs'].append({'id': 'owned', 'status': 'completed', 'userMessageId': 'message'})
        self.thread['messages'] = [{'id': 'message', 'role': 'user', 'text': 'go', 'runId': 'owned'},
                                   {'role': 'assistant', 'text': 'OWNED OUTPUT', 'runId': 'owned'}]
        out = self.cli('watch', 'test-thread', '--message-id', 'message', '--timeout', '0')
        self.assertEqual(out['lastAssistant'], 'OWNED OUTPUT')
        self.assertEqual(out['turnId'], 'owned')

    def test_manual_or_desktop_takeover_restores_legacy_callback(self):
        self.events_env()
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
        self.events_env(T3_NOTIFY_SECRET_FILE=str(secret), T3_NOTIFY_URL=f'http://127.0.0.1:{gateway.server_port}/callback')
        self.cli('say', 'test-thread', 'Managed work')
        self.source_db.unlink()
        result = subprocess.run([str(Path(CLI).parent / 't3-notify'), '--thread', 'test-thread', '--status', 'done', 'Verified output'],
                                env=self.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]['message'], 'Verified output')
        self.assertIn('sending legacy callback', result.stderr)

    def test_outbox_failure_does_not_repeat_or_veto_primary_dispatch(self):
        self.events_env(T3CTL_EVENTS_DB=str(self.path / 'token' / 'events.sqlite'))
        out = self.cli('say', 'test-thread', 'Continue')
        self.assertEqual((out['monitoring'], out['monitoringReason']), ('manual', 'registration-error'))
        self.assertEqual(len(self.sent()), 1)

    def test_invalid_inputs_cannot_dispatch(self):
        self.cli('say', 'test-thread', 'text', '--prompt-file', '-', text='other', success=False)
        self.cli('answer', 'test-thread', 'input-1', '--answers-file', '-', text='[]', success=False)
        self.cli('say', 'test-thread', '--prompt-file', success=False)
        self.assertEqual(self.server.state.dispatched, [])

    # ---- MCP transport -------------------------------------------------------

    def mcp(self, *args, token='test-mcp-token'):
        return subprocess.run([MCP, *args], text=True, capture_output=True, timeout=25,
                              env={**self.env, 'T3CTL_MCP_BEARER': token})

    def test_sse_responses_are_parsed(self):
        self.server.mcp_sse = True
        self.thread['messages'] = [{'role': 'assistant', 'text': 'over SSE', 'runId': 'old'}]
        out = self.cli('last', 'test-thread', '--json')
        self.assertEqual(out['text'], 'over SSE')

    def test_protocol_header_and_session_are_sent(self):
        # the fake, like T3, answers 400 without them after initialize
        req = urllib.request.Request(f'http://127.0.0.1:{self.server.server_port}/mcp', method='POST',
                                     data=json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}).encode(),
                                     headers={'Authorization': 'Bearer test-mcp-token', 'Content-Type': 'application/json'})
        with self.assertRaises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(err.exception.code, 400)
        result = self.mcp('probe', '--t3ctl')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['missing'], [])

    def test_typed_failure_becomes_a_clear_error(self):
        result = self.mcp('call', 't3_thread_read', '{"threadId":"nope"}')
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stderr)['code'], 'thread_not_found')
        out = self.cli('show', 'nope', success=False)
        self.assertIn("no thread matches 'nope'", out.stderr)

    def test_ordinary_session_is_refused_on_mcp(self):
        result = self.mcp('call', 't3_project_list', token='test-token')
        self.assertEqual(result.returncode, 3)
        self.assertIn('HTTP 401 invalid_mcp_credential', result.stderr)
        self.assertNotIn('test-token', result.stderr)
        (self.path / 'token.mcp').write_text('test-token')
        out = self.cli('list', success=False)
        self.assertIn('MCP credential expired or revoked; run: systemctl start t3-token-renew', out.stderr)

    def test_ceiling_refusal_names_the_credential(self):
        (self.path / 'mcp-tokens').write_text('test-mcp-token approval-required\n')
        self.thread['runtimeMode'] = 'full-access'
        out = self.cli('say', 'test-thread', 'Continue', success=False)
        self.assertIn('runtime_mode_escalation_denied', out.stderr)
        self.assertIn('capped below this thread\'s mode (T3CTL_MCP_ACCESS=full-access', out.stderr)
        self.assertEqual(self.sent(), [])

    def test_contract_check_names_missing_tools(self):
        self.server.mcp_missing = ['t3_thread_wait', 't3_pending_request']
        out = self.cli('contract-check', success=False)
        self.assertIn("lacks tools t3ctl calls: t3_thread_wait, t3_pending_request", out.stderr)

    def test_an_idle_thread_waiting_on_a_question_is_active(self):
        # found live: a question can outlive its run; v0.5.0 listed it, the MCP status filter cannot see it
        self.thread['requests'] = [self.request()]
        rows = [json.loads(l) for l in subprocess.run(['bash', CLI, 'list', '--active'], text=True, capture_output=True,
                                                      env=self.env, timeout=25).stdout.splitlines()]
        self.assertEqual([(r['id'], r['turn'], r['pendingUserInput']) for r in rows], [('test-thread', 'completed', True)])

    def test_runless_v1_thread_shows_its_messages(self):
        self.thread['runs'] = []
        self.thread['messages'] = [{'role': 'user', 'text': 'v1 question', 'runId': None},
                                   {'role': 'assistant', 'text': 'v1 answer', 'runId': None}]
        self.assertEqual([m['text'] for m in self.cli('show', 'test-thread')['messages']], ['v1 question', 'v1 answer'])
        out = self.cli('last', 'test-thread', '--json')
        self.assertEqual((out['text'], out['turn'], out['turnId']), ('v1 answer', 'none', None))

    def test_turns_cancelled_before_starting_do_not_count(self):
        self.thread['runs'] = [{'id': 'r1', 'status': 'completed'}, {'id': 'r2', 'status': 'completed'},
                               {'id': 'r3', 'status': 'cancelled', 'neverStarted': True}]
        self.thread['messages'] = [{'role': 'assistant', 'text': 'first', 'runId': 'r1'},
                                   {'role': 'assistant', 'text': 'second', 'runId': 'r2'}]
        self.assertEqual([m['text'] for m in self.cli('show', 'test-thread', '-n', '2')['messages']], ['first', 'second'])

    def test_legacy_path_encodes_thread_ids(self):
        tid = 'thread:delegated-task:command%3Amcp%3Ax'   # T3 puts % in delegated-task thread ids
        self.server.state.threads[tid] = self.server.state.threads.pop('test-thread')
        self.thread['id'] = tid
        self.thread['requests'] = [{'id': 'r1', 'kind': 'command', 'status': 'pending', 'runId': 'old', 'command': 'ls'}]
        self.assertEqual(self.cli('show', tid)['approvals'][0]['payload']['detail'], 'ls')

    def test_refused_message_restores_the_modes_it_changed(self):
        # configured ceiling full-access, but this credential was minted at approval-required
        (self.path / 'mcp-tokens').write_text('test-mcp-token approval-required\n')
        out = self.cli('say', 'test-thread', 'Go', '--full-access', '--execute', success=False)
        self.assertIn('runtime_mode_escalation_denied', out.stderr)
        self.assertEqual((self.thread['runtimeMode'], self.thread['interactionMode']), ('approval-required', 'plan'))
        self.assertEqual(self.sent('mcp.t3_thread_send'), [])

    def test_ceiling_is_checked_before_anything_changes(self):
        self.env['T3CTL_MCP_ACCESS'] = 'auto'
        out = self.cli('say', 'test-thread', 'Go', '--full-access', success=False)
        self.assertIn('capped at auto (T3CTL_MCP_ACCESS)', out.stderr)
        self.assertEqual(self.server.state.dispatched, [])
        out = self.cli('new', 'project', 'x', '--full-access', '--model', 'codex:test', success=False)
        self.assertIn('capped at auto', out.stderr)
        self.assertEqual(self.server.state.dispatched, [])

    def test_question_response_mode_survives(self):
        self.thread['requests'] = [{**self.request(), 'responseMode': 'message'}]
        self.assertEqual(self.cli('watch', 'test-thread', '--timeout', '0')['userInputs'][0]['responseMode'], 'message')
        self.assertEqual(self.cli('show', 'test-thread')['userInputs'][0]['responseMode'], 'message')

    def test_dispatch_replay_across_a_rotated_credential_is_refused(self):
        first = self.cli('say', 'test-thread', 'hello', '--expect-turn', 'old', '--dispatch-id', 'replay-key-0001')
        # same credential: the lost-receipt retry returns the same message even though the turn moved
        again = self.cli('say', 'test-thread', 'hello', '--expect-turn', 'old', '--dispatch-id', 'replay-key-0001')
        self.assertEqual(first['messageId'], again['messageId'])
        self.assertEqual(len(self.sent('mcp.t3_thread_send')), 1)
        (self.path / 'mcp-tokens').write_text('test-mcp-token full-access\nrotated-token full-access\n')
        (self.path / 'token.mcp').write_text('rotated-token')
        out = self.cli('say', 'test-thread', 'hello', '--dispatch-id', 'replay-key-0001', success=False)
        self.assertIn('credential that has since been rotated', out.stderr)
        self.assertEqual(len(self.sent('mcp.t3_thread_send')), 1)
        ledger = (self.path / 'token.mcp.dispatches').read_text()
        self.assertNotIn('test-mcp-token', ledger)
        self.assertEqual(oct((self.path / 'token.mcp.dispatches').stat().st_mode & 0o777), '0o600')
        out = self.cli('say', 'other-thread', 'x', '--dispatch-id', 'replay-key-0001', success=False)

    def test_mid_dispatch_revocation_never_resends_under_a_new_credential(self):
        # self mode would normally re-mint on a 401 and retry; mid-dispatch it must not
        auth = self.path / 'auth'
        auth.mkdir()
        self.server.auth_dir = auth
        self.server.tokens_file.write_text('test-token\n')
        self.env.update({'T3CTL_TOKEN_MODE': 'self', 'T3CTL_T3_BIN': str(ROOT / 'tests/fake_t3_cli.py'),
                         'FAKE_T3_TOKENS': str(self.server.tokens_file), 'FAKE_T3_MCP_TOKENS': str(self.path / 'mcp-tokens'),
                         'FAKE_T3_AUTH_DIR': str(auth), 'T3CTL_SESSION_FILE': str(self.path / 'session')})
        self.server.mcp_revoke_after = 't3_thread_send'
        out = self.cli('say', 'test-thread', 'once', '--dispatch-id', 'pinned-key-0001', success=False)
        self.assertIn('may be partly sent', out.stderr)
        self.assertEqual(len(self.sent('mcp.t3_thread_send')), 1)
        self.assertFalse((auth / 'calls').exists() and 'pairing' in (auth / 'calls').read_text())   # no re-mint mid-dispatch
        # the next command re-mints as usual, and a replay of the dispatch under that credential is refused
        out = self.cli('say', 'test-thread', 'once', '--dispatch-id', 'pinned-key-0001', success=False)
        self.assertIn('since been rotated', out.stderr)
        self.assertEqual(len(self.sent('mcp.t3_thread_send')), 1)

    def test_model_and_mode_changes_are_rolled_back_when_the_send_is_refused(self):
        (self.path / 'mcp-tokens').write_text('test-mcp-token approval-required\n')
        out = self.cli('say', 'test-thread', 'Go', '--full-access', '--execute', '--model', 'claudeAgent:other', success=False)
        self.assertIn('runtime_mode_escalation_denied', out.stderr)
        self.assertEqual((self.thread['runtimeMode'], self.thread['interactionMode']), ('approval-required', 'plan'))
        self.assertEqual(self.thread['modelSelection'], {'instanceId': 'codex', 'model': 'test'})

    def test_uncertain_delivery_keeps_the_requested_settings(self):
        self.thread['runtimeMode'] = 'full-access'
        self.server.mcp_revoke_after = 't3_thread_send'     # accepted, then the reply is lost
        out = self.cli('say', 'test-thread', 'Go', '--mode', 'approval-required', success=False)
        self.assertIn('delivery of dispatch', out.stderr)
        self.assertNotIn('message was not sent', out.stderr)
        self.assertEqual(self.thread['runtimeMode'], 'approval-required')   # not rolled back under a running message
        self.assertEqual(len(self.sent('mcp.t3_thread_send')), 1)

    def test_accepted_replay_does_not_reapply_old_settings(self):
        a = self.cli('say', 'test-thread', 'A', '--execute', '--model', 'claudeAgent:model-a', '--dispatch-id', 'replay-a-0001')
        self.cli('say', 'test-thread', 'B', '--plan', '--model', 'claudeAgent:model-b')
        again = self.cli('say', 'test-thread', 'A', '--execute', '--model', 'claudeAgent:model-a', '--dispatch-id', 'replay-a-0001',
                         '--expect-turn', 'old')
        self.assertEqual(again['messageId'], a['messageId'])
        self.assertEqual((self.thread['interactionMode'], self.thread['modelSelection']['model']), ('plan', 'model-b'))
        self.assertEqual(len(self.sent('mcp.t3_thread_send')), 2)

    def test_orchestration_error_is_not_proof_of_non_delivery(self):
        self.thread['runtimeMode'] = 'full-access'
        real = __import__('fake_t3_mcp')._call
        def accept_then_fail(state, name, args, access, ns):
            out = real(state, name, args, access, ns)
            if name == 't3_thread_send' and args.get('clientRequestId') == 'uncertain-0001':
                raise __import__('fake_t3_mcp').Fail('orchestration_error', 'projection read failed')
            return out
        with patch('fake_t3_mcp._call', accept_then_fail):
            out = self.cli('say', 'test-thread', 'Go', '--mode', 'approval-required', '--dispatch-id', 'uncertain-0001', success=False)
            self.assertIn('delivery of dispatch', out.stderr)
            self.assertEqual(self.thread['runtimeMode'], 'approval-required')
            # a later dispatch changes settings; retrying the uncertain one must not undo them
            self.cli('say', 'test-thread', 'B', '--plan')
            out = self.cli('say', 'test-thread', 'Go', '--mode', 'auto', '--execute', '--dispatch-id', 'uncertain-0001', success=False)
        self.assertEqual((self.thread['runtimeMode'], self.thread['interactionMode']), ('approval-required', 'plan'))

    def test_a_refused_dispatch_is_not_a_replay(self):
        self.thread['archivedAt'] = '2026-09-08T00:00:00.000Z'      # T3 refuses the send: thread_not_sendable
        self.cli('say', 'test-thread', 'x', '--expect-turn', 'old', '--dispatch-id', 'refused-key-01', success=False)
        self.thread['archivedAt'] = None
        self.thread['runs'].append({'id': 'someone-else', 'status': 'completed', 'userMessageId': 'm'})
        out = self.cli('say', 'test-thread', 'x', '--expect-turn', 'old', '--dispatch-id', 'refused-key-01', success=False)
        self.assertIn('has moved past turn old', out.stderr)
        self.assertEqual(self.sent('mcp.t3_thread_send'), [])

    def test_an_unusable_ledger_refuses_to_dispatch(self):
        (self.path / 'token.mcp.dispatches').write_text('not json\n')
        out = self.cli('say', 'test-thread', 'x', success=False)
        self.assertIn('is not valid', out.stderr)
        self.env['T3CTL_DISPATCH_LEDGER'] = str(self.path / 'token' / 'ledger')   # a file where its directory should be
        out = self.cli('say', 'test-thread', 'x', success=False)
        self.assertIn('refused to dispatch', out.stderr)
        self.assertEqual(self.sent(), [])

    def test_finite_cap_above_one_item_still_marks_truncation(self):
        self.thread['messages'] = [{'role': 'assistant', 'text': 'y' * 90000, 'runId': 'old'}]
        self.env['T3CTL_TEXT_CAP'] = '80000'
        text = self.cli('watch', 'test-thread', '--timeout', '0')['lastAssistant']
        self.assertEqual(text, 'y' * 80000 + '…[truncated]')

    def test_prefix_resolution_and_say_idempotency(self):
        out = self.cli('say', 'test-th', 'hello', '--dispatch-id', 'retry-key-0001')
        again = self.cli('say', 'test-thread', 'hello', '--dispatch-id', 'retry-key-0001')
        self.assertEqual((out['messageId'], out['turnId']), (again['messageId'], again['turnId']))
        self.assertEqual(len(self.sent('mcp.t3_thread_send')), 1)


if __name__ == '__main__':
    unittest.main()
