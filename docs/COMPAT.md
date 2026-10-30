# Verified (t3ctl, t3) pairs

Since v0.6.0 t3ctl drives t3 through its orchestration MCP endpoint (`POST
/mcp`, the tools t3's own agents use). What MCP does not offer still uses t3's
internal orchestration API — HTTP reads under `/api/orchestration/*` and
Effect-RPC commands over `/ws` — and, for `t3-events managed`, t3's SQLite
schema; neither is a public contract. Both are listed under "Dependencies"
below and probed by `t3ctl contract-check`. Add a row whenever `t3ctl
contract-check` and `t3ctl selftest` pass on a new t3 version; the procedure
for re-extracting the internal contracts after a break is
`t3-hermes-control.md` §4.

| t3ctl | t3 (`T3_VERSION`) | hermes-agent | verified | notes |
|---|---|---|---|---|
| v0.1.0 | 0.0.39-nightly.20260904.1280 | v0.21.0 (2026.8.31) | 2026-09-08 | selftest: PONG 9 s, callback auto-approved ~20 s; search OK; WebSocket ticket param is `wsTicket` |
| v0.1.0 | 0.0.41-nightly.20260911.1533 | v0.21.0 (2026.8.31) | 2026-09-11 | list/search OK; probe thread's callback (`Bash: t3-notify …` detail) auto-approved unattended within one 15 s cycle. `selftest` itself reported FAILED spuriously: the model ran the callback during the PONG leg with a `;` in its summary, which the approver refuses by design. Installing this nightly needs `npm_config_legacy_peer_deps=true` (npm loops on its effect peer pins) |
| v0.2.0 | 0.0.41-nightly.20260914.1700 | v0.21.2 (2026.9.11) | 2026-09-14 | `contract-check` ok (`dbSchemaChecked` true, 2 projects / 206 threads); `selftest` now passes clean — PONG 9 s, callback approved unattended in 18 s, probe deleted. The 2026-09-11 spurious FAILED is gone: selftest tolerates a callback made during the PONG leg. `legacy_peer_deps` still required to install the nightly |
| v0.3.0 | 0.0.46-nightly.20261003.2623 | v0.21.5 (2026.9.24) | 2026-10-03 | First orchestration V2 build: commands over `/ws` RPC, reads with `x-t3-orchestration-protocol: 2`. `contract-check` ok (`rpc` + `dbSchemaChecked`, 2 projects / 322 threads); live `selftest` PONG 13 s, callback approved unattended in 21 s (codex, `bash -lc` wrapper); claude verified on a scratch server (PONG 10 s, callback 11 s, chained callback left for a human). V1 threads import without runs, so they list as `turn: none` until their next message. V2 records an interrupted claude run as `completed` |
| v0.3.0 | 0.0.46-nightly.20261014.3118 | v0.21.6 | 2026-10-14 | `contract-check` ok (`rpc` + `dbSchemaChecked`, 2 projects / 598 threads); `selftest` ok — PONG 72 s, callback approved unattended at 99 s (slow first turn right after the restart), probe deleted. Server took ~45 s to listen after restart |
| v0.4.0 | 0.0.46-nightly.20261014.3118 | v0.21.6 | 2026-10-14 | `contract-check` ok; `last` and a stale `say --expect-turn` refusal checked on a live thread; `selftest` ok with `T3CTL_DEFAULT_MODE=full-access` on the box (probe forced to approval-required) — PONG 101 s, callback approved unattended at 154 s, probe deleted |
| v0.5.0 | 0.0.46-nightly.20261014.3118 | v0.21.6 (2026.9.24) | 2026-10-19 | `contract-check` ok (`rpc` + `dbSchemaChecked`, 3 projects / 904 threads). `list` default changed to the inbox: bare `list` 68 rows vs 904 for `--unarchived`/`--all`, `--active` 4, on the live box. Also merges the persistent-webhook-origins event relay (`T3CTL_EVENTS_ORIGINS`, PR #3) that Kosmi's vendored copy was already pinned to. Suite: 18 smoke checks + 27 unit tests pass |
| v0.5.0 | 0.1.1-nightly.20261020.3411 | v0.22.2 | 2026-10-20 | `contract-check` ok (`rpc` + `dbSchemaChecked`, 4 projects / 1014 threads); `selftest` ok — PONG 18 s, callback approved unattended at 33 s, probe deleted. Still protocol 2, same V2 tables/command names. From 0.1.x the npm `t3` package is a launcher that downloads the release binary to `~/.t3/runtime/versions/<ver>/t3`. Migrations 63–66 (incl. DropUnusedV1Projections) |
| v0.5.0 | 0.1.6-nightly.20261030.3917 | v0.22.2 | 2026-10-30 | `contract-check` ok (`rpc` + `dbSchemaChecked`, 4 projects / 1484 threads); `selftest` ok — PONG 24 s, callback approved unattended at 42 s, probe deleted. Still protocol 2, same V2 tables/command names. Server took ~58 s to listen after restart |
| v0.6.0 | 0.1.6-nightly.20261030.3917 | devbox-20261028-1617-1-gbc959ca1fb | 2026-10-30 | First MCP build. `contract-check` ok: all 11 MCP tools t3ctl calls present (66 served, protocol `2025-06-18`), OAuth metadata, `rpc` + `dbSchemaChecked` (4 projects / 1505 threads). `selftest` on the MCP path ok — PONG 18 s, callback approved unattended 16 s, probe deleted. Live round trip `new` → `watch` → `last` → `say --expect-turn` → `watch`, stale `--expect-turn` refused, `answer` on a thread that asked through AskUserQuestion, `settle`/`unsettle`, `interrupt` (MCP), `approve`/`stop`/`rm` (legacy token, read + operate only). Shapes of 16 read-only commands identical to v0.5.0 on live data, and all 1505 `list --unarchived` rows identical value for value. Verified: a CLI-issued `mcp-client` session with operate is capped at approval-required (`runtime_mode_escalation_denied` on a full-access launch); the pairing-code OAuth sign-in grants the ceiling |

Behaviour changes callers may notice (not t3-version related):

- v0.6.0: transport and credentials, not output. Every command keeps its
  flags and its JSON/JSONL field names and types (`tests/shapes.py` against
  v0.5.0's recorded shapes); additions only:
  - receipts gain `monitoringReason` whenever `monitoring` is `manual`;
    `token status` gains an `mcp` object; `contract-check` gains `mcp` and
    `oauth`; `new`/`say` accept `--dispatch-id` (reuse a lost receipt's
    `dispatchId`: T3 returns the same thread/message, no new work).
  - threads `new` creates are now named `mcp:launch:<hash>` by T3 (were UUIDs).
  - message `createdAt` in `show` is the timeline item's last update
    (MCP exposes no creation time); for user messages that is the same.
  - `show` on a thread imported from V1 (no runs) lists its run-less
    messages from the last 100 timeline items; v0.5.0 listed whatever the
    bounded snapshot held (fewer).
  - `search` is T3's own `t3_thread_search` (titles first, then message
    text; `matchedIn` is still `title`, `user` or `assistant`); a `--limit`
    above 50 is capped at 50.
  - `models` counts only unarchived threads (MCP lists no archived ones).
  - `show -n N` reads at most the last 50 turns (`t3_thread_read`'s
    `runLimit` maximum); `-n 0` means those 50. v0.5.0 read a bounded
    snapshot, so `-n 0` was never the full history either.
  - a `--dispatch-id` replay under a rotated MCP credential is refused (T3
    de-duplicates only within one credential); t3ctl keeps a ledger of
    dispatch id → kind, target, credential fingerprint in
    `T3CTL_DISPATCH_LEDGER` (default `$T3CTL_MCP_TOKEN_FILE.dispatches`,
    0600, 35 days), reserved under a lock before sending and marked accepted
    on the receipt. A same-credential replay of an *accepted* dispatch
    returns the original message and skips `--expect-turn`/the running cap,
    which it already passed; a reserved-but-unconfirmed one is re-checked.
    A ledger that cannot be locked, read or written refuses the dispatch.
    Each dispatch pins its credential: a 401 mid-dispatch stops with "may be
    partly sent" instead of re-minting and re-sending.
  - `say` checks the target mode against `T3CTL_MCP_ACCESS` before changing
    anything, and if T3 still refuses the message (a credential minted at a
    lower ceiling) it restores the modes/model it had changed.
  - `watch` waits server-side (`t3_thread_wait`) in slices of
    `T3CTL_WATCH_SLICE` (15 s); a pending approval or question is noticed at
    the next slice, at most 15 s late. The relay's `--timeout 0` checks are
    unaffected.
  - timing on this box (1505 threads): `list` 1.3–3.7 s, `show`/`last`/`watch
    --timeout 0`/`search` under 1.5 s (all faster than v0.5.0's ~2–5 s), but
    `list --unarchived` ~10 s and `models` ~7 s: MCP pages threads 100 at a
    time and each page re-reads the whole shell server-side.
  - credentials: two tokens (see README). The legacy token is re-minted with
    `T3CTL_TOKEN_SCOPES` (read + operate) instead of every scope, and the
    MCP token cannot act on a thread in a mode above `T3CTL_MCP_ACCESS`
    (`runtime_mode_escalation_denied`, reported with the reason). With
    `T3CTL_ALLOW_FULL_ACCESS=0` that ceiling is `auto`, so `say` to someone
    else's full-access thread is refused by T3 rather than sent.
- v0.5.0: `t3ctl list` with no flag returns only the app's inbox
  (unsettled, unarchived threads) instead of every non-archived thread.
  `--unsettled` is an alias; the old default output is `--unarchived`;
  `--active` and `--all` are unchanged.

**t3ctl ≥ v0.6.0 needs t3 ≥ 0.1.0** (MCP orchestration: thread
launch/send/wait/read/organize, pending requests, `mcp-client` sessions and
the OAuth pairing-code approval; verified on 0.1.6-nightly.20261030.3917).
**t3ctl ≤ v0.2.0 cannot drive t3 ≥ 0.0.46** (orchestration V2: HTTP
dispatch removed, protocol header required on reads) and v0.3.0 cannot drive
t3 < 0.0.46. Bump `T3_VERSION` and the t3ctl pin together. V2 seeds its own
`statev2.sqlite` from `state.sqlite` **once**, the first time it starts:
after a rollback to V1, move `statev2.sqlite*` aside before upgrading again or
everything done on V1 in between is missing from V2.

## Dependencies (v0.6.0)

MCP (`POST /mcp`, `mcp-protocol-version: 2025-06-18`, `mcp-session-id`;
`contract-check` fails naming any missing tool): `t3_project_list`,
`t3_thread_list`, `t3_thread_read`, `t3_thread_search`, `t3_thread_launch`,
`t3_thread_send`, `t3_thread_wait`, `t3_thread_configure`,
`t3_thread_organize`, `t3_thread_interrupt`, `t3_pending_request`.

MCP credential minting: `t3 auth pairing create --scope …` (the scopes of
the access level, contracts `mcpClientScopes`), then `POST
/oauth/mcp/register`, `POST /oauth/mcp/decision` (`pairing-code`),
`POST /oauth/mcp/token` (PKCE S256); `t3 auth session list --json` finds the
new session id by its unique client label; `t3 auth session revoke`.
`contract-check` reads `/.well-known/oauth-authorization-server`.

Still on the internal API (`bin/t3ctl-legacy.sh`, legacy token), each because
MCP cannot do it:

| call | used by | why not MCP |
|---|---|---|
| `GET /api/orchestration/shell` | `list --all`, `approve-callbacks`, `project add` (existing check) | `t3_thread_list` has no archived threads; the approver reads only this path |
| `GET /api/orchestration/threads/<id>/bounded` | `approvals`, `approvals` and `userInputs` in `show`/`watch`, `approve-callbacks`, `stop` | MCP cannot tie an approval to its exact command (`nativeItemRef`); MCP's questions lack `responseMode`; provider sessions |
| RPC `orchestration.dispatchCommand` `runtime-request.respond` | `approve`, `approve-callbacks` | `t3_pending_request` cannot answer approvals |
| RPC `thread.delete` | `rm`, `selftest` cleanup | no delete tool |
| RPC `run.interrupt` + `provider-session.detach` | `stop` | MCP's stop leaves provider sessions attached |
| RPC `thread.runtime-mode.set`, `thread.interaction-mode.set` | `say --mode/--full-access/--plan/--execute` | MCP keeps a thread's modes fixed |
| RPC `projects.mutate` | `project add`, `project set-model` | `t3_project_create/update` need a full-access ceiling |
| RPC `orchestration.searchThreads` | `contract-check` only | probes the `/ws` leg and auth |
| SQLite `orchestration_v2_projection_runs` | `t3-events managed`/`turn` | runs inside the coding session (t3-notify, as the t3 user), which holds no t3ctl credential |

Fields derived rather than read: `lastError` is the latest run's `error`
item; pending approvals vs questions are `pendingRequestCount` minus
`t3_pending_request list`; message→run ownership is the user-message item's
`runId`.

Known fragile points, in the order they have actually broken or are most
likely to:

0. MCP tool names/arguments and the OAuth pairing-code approval (new in
   v0.6.0; `contract-check` covers the tools, a failed rotation alerts and
   keeps the working token).
1. The orchestration protocol version (`2`: the `x-t3-orchestration-protocol`
   header on reads, `?orchestrationProtocol=2` on `/ws`). A bump shows up as
   400s on reads and a 426 on the WebSocket upgrade.
2. Approval correlation — `approval_request` turn item ↔ `command_execution`
   item by `nativeItemRef.nativeId`; the approver keys on the command found
   there and fails closed (left for a human) when there is none.
3. V2 command fields (`thread.create`, `message.dispatch` with
   `dispatchMode` and `createdBy`/`creationSource`, `runtime-request.respond`,
   `run.interrupt`, `provider-session.detach`) and `projects.mutate`.
4. Shell thread fields used by `list`/`watch` (`latestRunId`, `activeRunId`,
   `status`, `pendingRuntimeRequest`, `settledOverride`) and the bounded
   snapshot's `runs`/`runtimeRequests`/`turnItems`/`messages`.
5. `t3-events`' SQL on `orchestration_v2_projection_runs`
   (`json_extract(payload_json, '$.userMessageId')`).
