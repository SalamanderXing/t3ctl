# t3ctl — drive T3 Code from an agent

Tooling that lets an agent (Hermes, in our case) start, watch, approve and
continue [T3 Code](https://github.com/pingdotgg/t3code) coding threads on the
same box, and lets those threads call the agent back. Extracted from the
`devbox-v2` box on 2026-09-08 so several devboxes can install the same,
versioned thing.

```
bin/t3ctl                 the CLI (JSON/JSONL out): projects, models, list, new, say, last,
                          show, search, watch, answer, approvals, approve, approve-callbacks,
                          settle/unsettle, interrupt/stop/rm, selftest, contract-check
                          (`list` with no flag is the app's inbox — unsettled,
                          unarchived; `--unarchived` is every non-archived
                          thread, `--all` adds archived, `--active` only in-flight)
bin/t3-mcp                T3's orchestration MCP endpoint (POST /mcp; stdlib Python):
                          the transport for everything MCP covers (since v0.6.0)
bin/t3ctl-legacy.sh       the part of t3ctl still on T3's private API (sourced):
                          approvals, the callback approver, stop, rm, list --all,
                          say mode changes, project add/set-model
bin/t3-token-renew        mints both 30 d credentials as the t3 user, probes each on
                          its own path, swaps them in root-only, revokes the
                          predecessors; alerts on failure
bin/t3-notify             a session's callback to the agent: signed POST to the hermes
                          webhook platform (route t3-callback)
bin/t3-rpc                one Effect-RPC call over t3's /ws WebSocket (stdlib Python);
                          the legacy path's commands go through it
bin/t3-events             session state events for the Hermes gateway (opt-in)
units/                    t3-token-renew.timer (weekly), t3-callback-approver.service
skills/devops/t3/         the agent-facing Hermes skill
install.sh                idempotent installer (run as root on the box)
t3ctl.conf.example        the one box-specific file → /etc/t3ctl.conf
docs/t3-hermes-control.md design, verified API contracts, T3 version-bump procedure
docs/COMPAT.md            which t3 nightlies each tag was verified against
```

## Install on a box

```bash
git clone https://github.com/SalamanderXing/t3ctl /opt/t3ctl   # or unpack `git archive <tag>`
git -C /opt/t3ctl checkout v0.1.0
/opt/t3ctl/install.sh
$EDITOR /etc/t3ctl.conf            # paths, tag, notify binary name, alert targets
systemctl start t3-token-renew     # mints both credentials
```

Then wire the callback (once per box) and the skill:

- **Callback route.** `hermes webhook subscribe t3-callback …` on the hermes
  side with a secret; write the same secret to `T3_NOTIFY_SECRET_FILE`
  (root:<t3 user> 0640). The route must carry `"toolsets": ["terminal"]`
  (hand-add it in `webhook_subscriptions.json`; the subscribe CLI has no flag
  and re-running it drops the key) and should frame the message as an
  untrusted status report. See docs §6.
- **Skill.** Add to the hermes `config.yaml`:
  ```yaml
  skills:
    external_dirs:
      - /usr/local/share/t3ctl/skills
  ```
  and restart hermes (the skill index is built at startup).
- **Verify:** `t3ctl selftest` — creates a tagged probe thread, expects
  PONG, forces the callback command, confirms the approver accepted it
  unattended, deletes the probe. Run it after every `T3_VERSION` bump too.

Pin the tag in the box's own repo and have its drift check compare
`/var/lib/t3ctl/version` (written by install.sh) against the pin.
`/var/lib/t3ctl/manifest.sha256` lists every installed path with its hash.

## Container mode (Kosmi)

The same tooling runs inside a Hermes container where there is no systemd
and t3ctl runs as the same user and `T3CODE_HOME` as the t3 server. The box
sets in `/etc/t3ctl.conf`:

```
T3CTL_TOKEN_MODE=self          # t3ctl mints/rotates its own sessions (flock-serialised, retried once on 401)
T3CODE_HOME=/data/t3
T3CTL_TOKEN_FILE=/data/t3/t3ctl.token
T3CTL_ALLOW_FULL_ACCESS=${T3CTL_ALLOW_FULL_ACCESS:-0}   # --full-access refused unless the box env opts in
T3CTL_DENY_ORIGINS=webhook:auto-demo   # unattended Hermes lanes may never reach t3
```

A box whose operator wants the agent's threads to run unattended sets
`T3CTL_ALLOW_FULL_ACCESS=1` and `T3CTL_DEFAULT_MODE=full-access` in the
container env; `new` then starts threads full-access without the flag, and
`new --mode <other>` is refused (an agent that read an older skill text tried
to "fix" a full-access thread back to approval-required, 2026-09-09).

and the entrypoint runs `t3ctl approve-callbacks --loop 15` itself instead of
the systemd unit. Extra commands that exist for fresh servers: `project add
<path>`, `project set-model <project> <instanceId:model>`, a literal
`--model instanceId:model`, `token status|renew`, and `contract-check` (a
read-only probe of every MCP tool and API/schema contract, for version bumps;
`selftest` stays the end-to-end probe). In self mode the MCP credential is
minted on first use too (next to `T3CTL_TOKEN_FILE`, as `….mcp`), with the
ceiling `T3CTL_MCP_ACCESS` — `auto` while `T3CTL_ALLOW_FULL_ACCESS=0`.
`tests/smoke.sh` exercises all of this against `tests/fake_t3_server.py`
(and its fake `/mcp`, `tests/fake_t3_mcp.py`) without a real t3.

## Why the pieces exist

- **Commands go over T3's MCP endpoint (v0.6.0).** The app's private
  Effect-RPC broke dispatch after T3 bumps (HTTP 500 nine times since
  2026-08-26); `/mcp` is the surface T3's own agents use. `new` launches an
  idle thread and sends the prompt with `clientRequestId` = the receipt's
  `dispatchId`, so a retried `new`/`say` (or `--dispatch-id`) returns the same
  thread and message instead of new work. `watch` blocks server-side in
  `t3_thread_wait` slices instead of polling. What MCP cannot do stays on the
  private API in `bin/t3ctl-legacy.sh`; docs/COMPAT.md lists each such call.
- **Two least-privilege credentials.** `/mcp` accepts only `mcp-client`
  sessions and every other path rejects them. The MCP credential carries
  `orchestration:read` + `orchestration:operate` and a runtime-mode ceiling
  (`T3CTL_MCP_ACCESS`); T3 grants a ceiling only through its OAuth approval,
  so it is minted with a one-time pairing code (a CLI-issued `mcp-client`
  session with operate is capped at approval-required and cannot message a
  full-access thread). At a full-access ceiling T3 attaches `settings:write`
  and `source-control:write`, usable on `/mcp` only. The legacy credential is
  an ordinary session with `T3CTL_TOKEN_SCOPES` (read + operate). Before
  v0.6.0 the single token held every scope, `access:write` included.
- **Guardrails are in t3ctl, not the API.** MCP enforces none of them. `new` starts threads in
  `T3CTL_DEFAULT_MODE` (approval-required unless the deployment says
  otherwise; a full-access default needs `T3CTL_ALLOW_FULL_ACCESS=1` and
  falls back to approval-required with a warning otherwise) with a title
  tag; `--full-access` must be spelled out;
  `new` refuses at `T3CTL_MAX_RUNNING` running tagged turns; `stop`/
  `interrupt`/`rm` refuse untagged threads without `--force`; `say` inherits
  the target thread's mode.
- **Tokens always expire** (30 d), hence the weekly rotation of both, each
  with a pre-swap probe on its own path and a post-swap revoke.
- **The callback needs the approver.** In approval-required mode the
  session's own `t3-notify` run is a command approval; without
  `t3-callback-approver` an unattended thread can never say "done". The
  approver accepts only that exact command, only in tagged threads.
- **The legacy path goes over the WebSocket.** t3 0.0.46 (orchestration V2)
  removed the HTTP dispatch route; reads stay HTTP GETs but need the
  `x-t3-orchestration-protocol: 2` header. `bin/t3-rpc` speaks just enough
  WebSocket + Effect-RPC for one request/response, so t3ctl needs no new
  dependencies. t3ctl ≥ v0.6.0 needs t3 ≥ 0.1.0 (MCP orchestration);
  v0.3.0–v0.5.0 need t3 ≥ 0.0.46; older t3 needs v0.2.0.
- **MCP and V2 data are normalised to the old output.** Runs, pending
  requests and timeline items are folded into the `latestTurn`/
  `approvals[].payload`/`userInputs` shapes the skill already reads, so agents
  see no change (`tests/shapes.py` diffs every command against v0.5.0's
  recorded shapes). An approval's `detail` is the exact command (from the
  `command_execution` item with the same native tool id), never the model's
  description of it — MCP cannot correlate the two, so approvals stay on the
  legacy path.

## Coupling to T3

Since v0.6.0 t3ctl mostly speaks T3's MCP endpoint, whose tools T3's own
agents use; what remains on t3's *internal* orchestration API (and
`t3-events managed`'s SQLite read) can change without notice between
nightlies. `docs/COMPAT.md` records the (t3ctl tag, t3 version) pairs that
were verified and every remaining internal dependency; `t3ctl contract-check`
probes all of them and names any missing MCP tool.

## Session state events

Opt-in: set `T3CTL_EVENTS_ENABLED=1` in both the gateway and t3ctl environment,
install `bin/t3-events` beside t3ctl, and import that same file as `t3_events`
in the Hermes gateway. Start `watch_gateway(runner)` with the gateway's
supervised task lifecycle. The Kosmi image provides the pinned integration.
The gateway injection method must accept `expected_session_id` and reject a
route that changed owners before acceptance.

`new`/`say` capture HERMES_SESSION_ID and HERMES_SESSION_KEY in a persistent
SQLite subscription; the receipt says `monitoring: events` only if registration
succeeded. Otherwise it says `monitoring: manual` with `monitoringReason`:
`events-disabled` (`T3CTL_EVENTS_ENABLED` is not 1), `missing:<VAR>[,<VAR>]`
(which of `HERMES_SESSION_ID`/`_KEY`/`_PLATFORM` was absent from t3ctl's
environment), `platform:<platform>-not-in-origins` (a webhook/api_server lane
not listed in `T3CTL_EVENTS_ORIGINS`), `registration-error` or
`relay-not-installed`. Each decision, events or manual, is appended as one JSON
line to `t3ctl-events.log` beside the events DB (`T3CTL_EVENTS_LOG`,
size-capped): timestamp, threadId, dispatchId, platform, outcome, reason and the
names — never values — of the `HERMES_SESSION_*` variables present. Interactive messaging gateways are supported. CLI, webhook and
api_server origins keep manual monitoring; they must not be promised an async
return path through a messaging adapter. The exception is a webhook/api_server
lane whose sessions persist across turns and whose adapter accepts injected
turns: list its chat-id prefixes in `T3CTL_EVENTS_ORIGINS` (comma-separated,
e.g. `webhook:linear-comment:thread-`). `t3-events eligible` is the single
decision both `new`'s prompt footer and registration use.

The relay observes typed T3 state every 15 seconds (through `t3ctl watch
--timeout 0`, i.e. MCP), with no model calls while nothing changes. Completion, errors and pending input/approval requests create
a durable outbox event identified by dispatch + turn + state + request IDs.
New dispatches supersede pending events from older turns. A busy parent leaves
the event pending without consuming retry attempts. Closed or unrelated
sessions cannot adopt results; compression continuations retain ownership.
Delivery failures back off, stop after eight attempts, and remain visible via
`t3-events status`. Pending events expire after 48 hours; terminal history is
retained seven days. Registrations fail independently of the primary dispatch:
inspect `monitoring: manual` and use the receipt's messageId with `watch --message-id`.

The outbox survives restarts. Acknowledgement is after gateway adapter
acceptance, not after the resulting model turn finishes. A crash between
acceptance and acknowledgement can replay an event (at-least-once delivery);
verification and any follow-up side effects must remain idempotent.

Transport: `--prompt-file path|-` accepts literal multiline text; `--execute`
exits plan mode without changing runtime permissions. `show`/`watch` expose
`userInputs`; `answer THREAD REQUEST --answers-file path|-` responds using the
question IDs. `watch --after-turn PREVIOUS` never presents the previous turn
as the newly-dispatched turn. Its full JSON input goes through stdin, so large
responses cannot overflow the OS argument limit.

Run `bash tests/smoke.sh` (it ends with the v0.5.0 output-shape comparison and
the unit tests, `python3 -m unittest discover -s tests -p 'test_*.py'`).

State-read failures back off and stop after eight consecutive failures, emitting
`monitoring-failed` for the owner to inspect. `t3-events status` exposes retained
event outcomes and subscription states. A new dispatch resets monitoring.

Turn ownership is resolved from the dispatched message: T3 records the run
each user message started (the MCP user-message item for `watch` and the
relay's poll; `orchestration_v2_projection_runs` for `t3-events managed`, which
runs inside the coding session and holds no t3ctl credential), never from
whichever turn happens to appear next. Receipts include the dispatched message ID; `watch --message-id ID` waits
for that mapping or reports supersession without unrelated output. `contract-check`
validates these source columns. Successful unmanaged dispatches release the old
subscription, and callback suppression checks the actual current turn so desktop
takeovers also retain the legacy notification path.

If the duplicate-suppression lookup fails, `t3-notify` logs the failure and still
sends its legacy callback. Outbox/infrastructure failures propagate to Hermes'
existing bounded task supervisor (five rapid restarts, with logged counts and a
terminal give-up message); they do not start a second unbounded retry loop.
After that budget is exhausted, repair the storage problem and restart the
gateway to resume pending delivery. Inspect gateway logs for supervisor health;
`t3-events status` reports outbox records and cannot read an unavailable database.
