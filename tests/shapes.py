#!/usr/bin/env python3
"""Output-shape compatibility: record what every t3ctl command prints, compare builds.

Callers (the t3 skill, Cactus, Kosmi's vendored copy) parse t3ctl's JSON by
field name, so a release must not drop or retype a field. This runs one fixed
scenario against tests/fake_t3_server.py and reduces each command's output to
its shape (keys and value types, recursively; JSONL is the union of its lines).

  shapes.py record BIN_DIR OUT.json     # e.g. a `git archive v0.5.0 bin` extract
  shapes.py compare GOLDEN.json BIN_DIR # exit 1 on a dropped/retyped field

Added fields are fine. A field that was null in the golden run may now carry a
value, and vice versa (reported, not fatal): seeds are deterministic, but some
values (lastError, snoozedUntil) are legitimately null on one path.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_t3_server import Handler, Server, State  # noqa: E402

P = '11111111-1111-4111-8111-111111111111'
OURS = '22222222-2222-4222-8222-222222222222'      # [test], running, pending callback approval
OTHER = '33333333-3333-4333-8333-333333333333'     # not ours, completed with a reply
SETTLED = '44444444-4444-4444-8444-444444444444'
ARCHIVED = '55555555-5555-4555-8555-555555555555'
ASKING = '66666666-6666-4666-8666-666666666666'    # [test], waiting on a user question
QUESTIONS = [{'id': 'storage', 'header': 'Storage', 'question': 'Where?',
              'options': [{'label': 'Existing database', 'description': 'Reuse it'}]}]


def seed():
    sel = {'instanceId': 'codex', 'model': 'gpt-test'}
    return {
        'projects': [{'id': P, 'title': 'proj', 'workspaceRoot': '/srv/proj', 'defaultModelSelection': None}],
        'threads': [
            {'id': OURS, 'title': '[test] ours', 'projectId': P, 'runtimeMode': 'approval-required',
             'modelSelection': sel, 'updatedAt': '2026-09-08T10:00:00.000Z',
             'runs': [{'id': 't-1', 'status': 'running', 'userMessageId': 'm-1'}],
             'messages': [{'id': 'm-1', 'role': 'user', 'text': 'do it', 'createdAt': '2026-09-08T10:00:00.000Z', 'runId': 't-1'}],
             'requests': [{'id': 'r1', 'kind': 'command', 'status': 'pending', 'runId': 't-1',
                           'command': f't3-notify --thread {OURS} --status done "finished"'}]},
            {'id': OTHER, 'title': 'Other thread', 'projectId': P, 'runtimeMode': 'full-access',
             'modelSelection': sel, 'updatedAt': '2026-09-08T09:00:00.000Z',
             'runs': [{'id': 't-2', 'status': 'completed', 'userMessageId': 'm-2'}],
             'messages': [{'id': 'm-2', 'role': 'user', 'text': 'question', 'createdAt': '2026-09-08T09:00:00.000Z', 'runId': 't-2'},
                          {'id': 'm-3', 'role': 'assistant', 'text': 'assistant reply here', 'createdAt': '2026-09-08T09:01:00.000Z', 'runId': 't-2'}]},
            {'id': SETTLED, 'title': 'Settled thread', 'projectId': P, 'runtimeMode': 'approval-required',
             'settledOverride': 'settled', 'updatedAt': '2026-09-08T08:00:00.000Z', 'modelSelection': sel,
             'runs': [{'id': 't-3', 'status': 'completed', 'userMessageId': 'm-4'}],
             'messages': [{'id': 'm-4', 'role': 'user', 'text': 'old', 'createdAt': '2026-09-08T08:00:00.000Z', 'runId': 't-3'}]},
            {'id': ARCHIVED, 'title': 'Archived thread', 'projectId': P, 'runtimeMode': 'approval-required',
             'archivedAt': '2026-09-08T07:30:00.000Z', 'updatedAt': '2026-09-08T07:00:00.000Z', 'modelSelection': sel,
             'runs': [{'id': 't-4', 'status': 'completed', 'userMessageId': 'm-5'}],
             'messages': [{'id': 'm-5', 'role': 'user', 'text': 'archived', 'createdAt': '2026-09-08T07:00:00.000Z', 'runId': 't-4'}]},
            {'id': ASKING, 'title': '[test] asking', 'projectId': P, 'runtimeMode': 'approval-required',
             'modelSelection': sel, 'updatedAt': '2026-09-08T06:00:00.000Z',
             'runs': [{'id': 't-5', 'status': 'waiting', 'userMessageId': 'm-6'}],
             'messages': [{'id': 'm-6', 'role': 'user', 'text': 'plan it', 'createdAt': '2026-09-08T06:00:00.000Z', 'runId': 't-5'}],
             'requests': [{'id': 'q1', 'kind': 'user_input', 'status': 'pending', 'runId': 't-5', 'questions': QUESTIONS}]},
        ]}


def scenario(work: Path):
    """(name, argv, stdin) in order; later steps see earlier steps' effects."""
    (work / 'newproj').mkdir(exist_ok=True)
    return [
        ('projects', ['projects'], None),
        ('models', ['models'], None),
        ('list', ['list'], None),
        ('list --unarchived', ['list', '--unarchived'], None),
        ('list --active', ['list', '--active'], None),
        ('list --all', ['list', '--all'], None),
        ('show', ['show', OTHER, '-n', '2'], None),
        ('show pending', ['show', ASKING], None),
        ('last --json', ['last', OTHER, '--json'], None),
        ('search title', ['search', 'Other'], None),
        ('search message', ['search', 'assistant reply'], None),
        ('watch settled', ['watch', OTHER, '--timeout', '0'], None),
        ('watch pending-user-input', ['watch', ASKING, '--timeout', '0'], None),
        ('watch pending-approval', ['watch', OURS, '--timeout', '0'], None),
        ('approvals', ['approvals', OURS], None),
        ('approve-callbacks --dry-run', ['approve-callbacks', '--dry-run'], None),
        ('new', ['new', 'proj', 'hello', '--model', 'codex:gpt-test'], None),
        ('say', ['say', OTHER, 'follow up'], None),
        ('answer', ['answer', ASKING, 'q1', '--answers-file', '-'], json.dumps({'storage': 'Existing database'})),
        ('settle', ['settle', OTHER], None),
        ('unsettle', ['unsettle', OTHER], None),
        ('interrupt', ['interrupt', OURS], None),
        ('token status', ['token', 'status'], None),
        ('project set-model', ['project', 'set-model', 'proj', 'codex:gpt-test'], None),
        ('project add', ['project', 'add', str(work / 'newproj'), '--title', 'newproj'], None),
        ('approve', ['approve', OURS, 'r1'], None),
        ('rm', ['rm', OURS], None),
    ]


def shape(v):
    if isinstance(v, dict):
        return {'{}': {k: shape(x) for k, x in sorted(v.items())}}
    if isinstance(v, list):
        s = None
        for x in v:
            s = merge(s, shape(x))
        return {'[]': s}
    return {bool: 'bool', int: 'int', float: 'float', str: 'str', type(None): 'null'}[type(v)]


def merge(a, b):
    if a is None:
        return b
    if b is None:
        return a
    if isinstance(a, dict) and isinstance(b, dict) and a.keys() == b.keys():
        (k,) = a.keys()
        if k == '{}':
            keys = a[k].keys() | b[k].keys()
            return {k: {n: merge(a[k].get(n), b[k].get(n)) for n in sorted(keys)}}
        return {k: merge(a[k], b[k])}
    ta = a if isinstance(a, list) else [a]
    tb = b if isinstance(b, list) else [b]
    out = []
    for t in ta + tb:
        if t not in out:
            out.append(t)
    return out[0] if len(out) == 1 else out


def run(bin_dir: Path, env_extra=None):
    """Run the scenario with bin_dir/t3ctl; returns {name: shape}."""
    root = Path(__file__).resolve().parents[1] / 'tmp'
    root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=root) as tmp:
        work = Path(tmp)
        tokens = work / 'tokens'
        tokens.write_text('legacy-token\n')
        (work / 'token').write_text('legacy-token\n')
        mcp_tokens = work / 'mcp-tokens'
        mcp_tokens.write_text('mcp-token full-access\n')
        (work / 'token.mcp').write_text('mcp-token\n')
        server = Server(('127.0.0.1', 0), Handler)
        server.tokens_file = tokens
        server.mcp_tokens_file = mcp_tokens
        server.state = State(seed(), work / 'commands.json')
        threading.Thread(target=server.serve_forever, daemon=True).start()
        bindir = work / 'bin'
        bindir.mkdir()
        (bindir / 't3-notify').symlink_to(bin_dir / 't3-notify')
        env = {k: v for k, v in os.environ.items() if not k.startswith(('T3CTL_', 'HERMES_', 'T3_'))}
        env.update({'PATH': f'{bindir}:{env["PATH"]}', 'T3CTL_CONF': '/dev/null', 'T3CTL_TOKEN_MODE': 'file',
                    'T3CTL_TOKEN_FILE': str(work / 'token'), 'T3CTL_SESSION_FILE': str(work / 'session'),
                    'T3CTL_TAG': '[test]', 'T3CTL_URL': f'http://127.0.0.1:{server.server_port}',
                    'T3CTL_DENY_ORIGINS': '', 'T3CTL_TEXT_CAP': '0', 'T3CTL_LOCK_DIR': str(work / 'locks'),
                    'T3CTL_DB': str(work / 'missing.sqlite'), 'T3CODE_HOME': str(work)})
        env.update(env_extra or {})
        shapes = {}
        try:
            for name, argv, stdin in scenario(work):
                r = subprocess.run(['bash', str(bin_dir / 't3ctl'), *argv], input=stdin, text=True,
                                   capture_output=True, env=env, timeout=60)
                if r.returncode != 0:
                    shapes[name] = {'error': r.stderr.strip()[-400:]}
                    continue
                s, text, pos = None, r.stdout, 0
                dec = json.JSONDecoder()
                while True:   # JSONL or pretty-printed JSON (show)
                    while pos < len(text) and text[pos].isspace():
                        pos += 1
                    if pos >= len(text):
                        break
                    value, pos = dec.raw_decode(text, pos)
                    s = merge(s, shape(value))
                shapes[name] = s
        finally:
            server.shutdown()
            server.server_close()
        return shapes


def compare(golden, new, path=''):
    """Yield (fatal, message) for every golden field the new output lost or retyped."""
    if golden == 'null' or golden is None:
        return
    if new == 'null' or new is None:
        yield False, f'{path or "<root>"}: was {json.dumps(golden)[:80]}, now null/empty'
        return
    if isinstance(golden, list) or isinstance(new, list):
        g = golden if isinstance(golden, list) else [golden]
        n = new if isinstance(new, list) else [new]
        for t in g:
            if t == 'null':
                continue
            if t not in n and not any(isinstance(x, dict) and isinstance(t, dict) and x.keys() == t.keys() for x in n):
                yield True, f'{path}: type {json.dumps(t)[:80]} gone (now {json.dumps(n)[:80]})'
        for t in g:
            if isinstance(t, dict):
                for x in n:
                    if isinstance(x, dict) and x.keys() == t.keys():
                        yield from compare(t, x, path)
        return
    if isinstance(golden, dict):
        if not isinstance(new, dict) or golden.keys() != new.keys():
            yield True, f'{path}: was {next(iter(golden))}, now {json.dumps(new)[:80]}'
            return
        (k,) = golden.keys()
        if k == '[]':
            yield from compare(golden[k], new[k], path + '[]')
            return
        for field, sub in golden[k].items():
            if field not in new[k]:
                yield True, f'{path}.{field}: missing'
            else:
                yield from compare(sub, new[k][field], f'{path}.{field}')
        return
    if golden != new:
        yield True, f'{path}: {golden} → {new}'


def main():
    if len(sys.argv) == 4 and sys.argv[1] == 'record':
        shapes = run(Path(sys.argv[2]).resolve())
        Path(sys.argv[3]).write_text(json.dumps(shapes, indent=1, sort_keys=True) + '\n')
        bad = [n for n, s in shapes.items() if isinstance(s, dict) and 'error' in s]
        print(f'recorded {len(shapes)} commands' + (f'; FAILED: {bad}' if bad else ''))
        return 1 if bad else 0
    if len(sys.argv) == 4 and sys.argv[1] == 'compare':
        golden = json.loads(Path(sys.argv[2]).read_text())
        new = run(Path(sys.argv[3]).resolve())
        fatal = 0
        for name, g in golden.items():
            n = new.get(name)
            if isinstance(n, dict) and 'error' in n:
                print(f'FAIL {name}: command failed: {n["error"]}')
                fatal += 1
                continue
            for is_fatal, msg in compare(g, n):
                print(('FAIL ' if is_fatal else 'note ') + f'{name}{msg if msg.startswith(".") or msg.startswith("[") else " " + msg}')
                fatal += is_fatal
        print(f'{len(golden)} commands compared, {fatal} incompatibilities')
        return 1 if fatal else 0
    print(__doc__)
    return 64


if __name__ == '__main__':
    sys.exit(main())
