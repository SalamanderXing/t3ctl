# Verified (t3ctl, t3) pairs

t3ctl uses t3's internal orchestration API — HTTP reads under
`/api/orchestration/*` and, since v0.3.0, Effect-RPC commands over `/ws` — and,
for `t3-events`, t3's SQLite schema. Neither is a public contract. Add a row whenever `t3ctl selftest` and `t3ctl search` pass on a
new t3 version; the procedure for re-extracting the contracts after a break
is `t3-hermes-control.md` §4.

| t3ctl | t3 (`T3_VERSION`) | hermes-agent | verified | notes |
|---|---|---|---|---|
| v0.1.0 | 0.0.39-nightly.20260904.1280 | v0.21.0 (2026.8.31) | 2026-09-08 | selftest: PONG 9 s, callback auto-approved ~20 s; search OK; WebSocket ticket param is `wsTicket` |
| v0.1.0 | 0.0.41-nightly.20260911.1533 | v0.21.0 (2026.8.31) | 2026-09-11 | list/search OK; probe thread's callback (`Bash: t3-notify …` detail) auto-approved unattended within one 15 s cycle. `selftest` itself reported FAILED spuriously: the model ran the callback during the PONG leg with a `;` in its summary, which the approver refuses by design. Installing this nightly needs `npm_config_legacy_peer_deps=true` (npm loops on its effect peer pins) |
| v0.2.0 | 0.0.41-nightly.20260914.1700 | v0.21.2 (2026.9.11) | 2026-09-14 | `contract-check` ok (`dbSchemaChecked` true, 2 projects / 206 threads); `selftest` now passes clean — PONG 9 s, callback approved unattended in 18 s, probe deleted. The 2026-09-11 spurious FAILED is gone: selftest tolerates a callback made during the PONG leg. `legacy_peer_deps` still required to install the nightly |
| v0.3.0 | 0.0.46-nightly.20261003.2623 | v0.21.5 (2026.9.24) | 2026-10-03 | First orchestration V2 build: commands over `/ws` RPC, reads with `x-t3-orchestration-protocol: 2`. `contract-check` ok (`rpc` + `dbSchemaChecked`, 2 projects / 322 threads); live `selftest` PONG 13 s, callback approved unattended in 21 s (codex, `bash -lc` wrapper); claude verified on a scratch server (PONG 10 s, callback 11 s, chained callback left for a human). V1 threads import without runs, so they list as `turn: none` until their next message. V2 records an interrupted claude run as `completed` |

**t3ctl ≤ v0.2.0 cannot drive t3 ≥ 0.0.46** (orchestration V2: HTTP
dispatch removed, protocol header required on reads) and v0.3.0 cannot drive
t3 < 0.0.46. Bump `T3_VERSION` and the t3ctl pin together. V2 seeds its own
`statev2.sqlite` from `state.sqlite` **once**, the first time it starts:
after a rollback to V1, move `statev2.sqlite*` aside before upgrading again or
everything done on V1 in between is missing from V2.

Known fragile points, in the order they have actually broken or are most
likely to:

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
