# Verified (t3ctl, t3) pairs

t3ctl uses t3's internal orchestration HTTP API (`/api/orchestration/*`,
`/api/auth/*`) and, for `search`, t3's SQLite schema. Neither is a public
contract. Add a row whenever `t3ctl selftest` and `t3ctl search` pass on a
new t3 version; the procedure for re-extracting the contracts after a break
is `t3-hermes-control.md` §4.

| t3ctl | t3 (`T3_VERSION`) | hermes-agent | verified | notes |
|---|---|---|---|---|
| v0.1.0 | 0.0.39-nightly.20260904.1280 | v0.21.0 (2026.8.31) | 2026-09-08 | selftest: PONG 9 s, callback auto-approved ~20 s; search OK; WebSocket ticket param is `wsTicket` |
| v0.1.0 | 0.0.41-nightly.20260911.1533 | v0.21.0 (2026.8.31) | 2026-09-11 | list/search OK; probe thread's callback (`Bash: t3-notify …` detail) auto-approved unattended within one 15 s cycle. `selftest` itself reported FAILED spuriously: the model ran the callback during the PONG leg with a `;` in its summary, which the approver refuses by design. Installing this nightly needs `npm_config_legacy_peer_deps=true` (npm loops on its effect peer pins) |
| v0.2.0 | 0.0.41-nightly.20260914.1700 | v0.21.2 (2026.9.11) | 2026-09-14 | `contract-check` ok (`dbSchemaChecked` true, 2 projects / 206 threads); `selftest` now passes clean — PONG 9 s, callback approved unattended in 18 s, probe deleted. The 2026-09-11 spurious FAILED is gone: selftest tolerates a callback made during the PONG leg. `legacy_peer_deps` still required to install the nightly |

Known fragile points, in the order they have actually broken or are most
likely to:

1. `search` — SQL lifted from `Layers/ProjectionSnapshotQuery.ts
   searchActiveThreadRows`; any column rename breaks it (HTTP commands keep
   working).
2. Approval payload shape — `activities[].payload.{requestId, requestType,
   detail, decision}`; the approver and `watch` depend on it.
3. `thread.turn.start` / `thread.create` command fields (`modelSelection`,
   `runtimeMode`, `interactionMode`, `branch`, `worktreePath`).
4. `/api/orchestration/shell` snapshot fields used by `list`/`watch`
   (`latestTurn.state`, `hasPendingApprovals`, `settledOverride`).
