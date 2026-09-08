# t3ctl — drive T3 Code from an agent

Tooling that lets an agent (Hermes, in our case) start, watch, approve and
continue [T3 Code](https://github.com/pingdotgg/t3code) coding threads on the
same box, and lets those threads call the agent back. Extracted from the
`devbox-v2` box on 2026-09-08 so several devboxes can install the same,
versioned thing.

```
bin/t3ctl                 the CLI (JSON/JSONL out): projects, models, list, new, say,
                          show, search, watch, approvals, approve, approve-callbacks,
                          settle/unsettle, interrupt/stop/rm, selftest
bin/t3-token-renew        mints a 30 d bearer token as the t3 user, probes it, swaps
                          it in root-only, revokes the predecessor; alerts on failure
bin/t3-notify             a session's callback to the agent: signed POST to the hermes
                          webhook platform (route t3-callback)
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
systemctl start t3-token-renew     # mints the first token
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
T3CTL_TOKEN_MODE=self          # t3ctl mints/rotates its own session (flock-serialised, retried once on 401)
T3CODE_HOME=/data/t3
T3CTL_TOKEN_FILE=/data/t3/t3ctl.token
T3CTL_ALLOW_FULL_ACCESS=0      # --full-access refused outright
T3CTL_DENY_ORIGINS=webhook:auto-demo   # unattended Hermes lanes may never reach t3
```

and the entrypoint runs `t3ctl approve-callbacks --loop 15` itself instead of
the systemd unit. Extra commands that exist for fresh servers: `project add
<path>`, `project set-model <project> <instanceId:model>`, a literal
`--model instanceId:model`, `token status|renew`, and `contract-check` (a
read-only probe of every API/schema contract, for version bumps; `selftest`
stays the end-to-end probe). `tests/smoke.sh` exercises all of this against
`tests/fake_t3_server.py` without a real t3.

## Why the pieces exist

- **Guardrails are in t3ctl, not the API.** `new` starts threads
  approval-required with a title tag; `--full-access` must be spelled out;
  `new` refuses at `T3CTL_MAX_RUNNING` running tagged turns; `stop`/
  `interrupt`/`rm` refuse untagged threads without `--force`; `say` inherits
  the target thread's mode.
- **Tokens always expire** (30 d, no `--scopes`), hence the weekly rotation
  with a pre-swap probe and post-swap revoke.
- **The callback needs the approver.** In approval-required mode the
  session's own `t3-notify` run is a command approval; without
  `t3-callback-approver` an unattended thread can never say "done". The
  approver accepts only that exact command, only in tagged threads.
- **`search` rides t3's SQLite schema** (the HTTP API has no message search);
  it is the first thing to break on a version bump. `selftest` does not cover
  it — run `t3ctl search <word>` by hand after a bump.

## Coupling to T3

t3ctl speaks t3's *internal* orchestration API and reads its SQLite. Both
change without notice between nightlies. `docs/COMPAT.md` records the
(t3ctl tag, t3 version) pairs that were verified; `docs/t3-hermes-control.md`
§4 is the procedure for re-deriving the contracts from the source map when a
bump breaks something.
