# shellcheck shell=bash
# t3ctl-legacy.sh — the part of t3ctl that still speaks T3's private API.
#
# Sourced by bin/t3ctl. Since v0.6.0 t3ctl drives T3 through its MCP endpoint
# (bin/t3-mcp); everything here is what MCP does not offer, kept on the old
# transport with its own least-privilege token (T3CTL_TOKEN_FILE, scopes
# orchestration:read + orchestration:operate). Every dependency below is
# listed in docs/COMPAT.md and probed by `t3ctl contract-check`:
#
#   GET  /api/orchestration/shell                     list --all (MCP lists no archived
#                                                     threads), the callback approver
#   GET  /api/orchestration/threads/<id>/bounded      approvals: the exact command of an
#                                                     approval (MCP has no item correlation),
#                                                     pending userInputs (MCP drops their
#                                                     responseMode), stop: provider sessions
#   RPC  orchestration.dispatchCommand (/ws)          runtime-request.respond (approve; MCP
#                                                     cannot answer approvals), thread.delete
#                                                     (rm), run.interrupt + provider-session.detach
#                                                     (stop), thread.runtime-mode.set /
#                                                     thread.interaction-mode.set (say --mode,
#                                                     --plan/--execute: MCP keeps modes fixed)
#   RPC  projects.mutate (/ws)                        project add / set-model (MCP's project
#                                                     tools need a full-access ceiling)
#   RPC  orchestration.searchThreads (/ws)            contract-check only (WebSocket leg + auth)
#
# The approver (`approve-callbacks`) runs entirely on this path, so it keeps
# working with only this token, and it needs nothing MCP would add.

PROTO_HDR="x-t3-orchestration-protocol: 2"   # V2 read routes answer a bare 400 without it
# curl reads the Authorization header from a file descriptor (-H @file), so
# the token never appears in argv, which every local user can read in /proc.
auth_hdr() { printf 'Authorization: Bearer %s\n' "$1"; }
T3RPC=$(dirname "${BASH_SOURCE[0]}")/t3-rpc

token() {
  if [ ! -s "$TOKEN_FILE" ]; then
    [ "$TOKEN_MODE" = self ] || die "cannot read $TOKEN_FILE (root only; mint with: systemctl start t3-token-renew)"
    mint_token rpc "" || die "could not mint a t3 session token"
  fi
  tr -d ' \t\n' < "$TOKEN_FILE"
}

api() { # api METHOD PATH [JSON_BODY]
  local method=$1 path=$2 body=${3:-} out code resp tok
  tok=$(token) || exit 1
  local args=(-sS -m 60 -X "$method" -H "$PROTO_HDR" -w $'\n%{http_code}')
  [ -n "$body" ] && args+=(-H "Content-Type: application/json" --data-binary @-)
  out=$(printf '%s' "$body" | curl "${args[@]}" -H @<(auth_hdr "$tok") "$BASE$path") || die "cannot reach t3 at $BASE — is the t3 server up? (a full disk presents exactly like this: check df -h)"
  code=${out##*$'\n'}
  resp=${out%$'\n'*}
  if { [ "$code" = 401 ] || [ "$code" = 403 ]; } && [ "$TOKEN_MODE" = self ] && [ "${_T3CTL_RETRIED:-0}" = 0 ]; then
    # Expired/revoked session: rotate once and retry the same call.
    mint_token rpc "$tok" || die "HTTP $code and re-minting a session failed"
    _T3CTL_RETRIED=1 api "$method" "$path" "$body"
    return
  fi
  if [ "$code" != 200 ]; then
    echo "$resp" | head -c 600 >&2; echo >&2
    case $code in
      401|403) if [ "$TOKEN_MODE" = self ]; then die "HTTP $code — token expired or revoked; run: t3ctl token renew"; else die "HTTP $code — token expired or revoked; run: systemctl start t3-token-renew"; fi ;;
      *) die "HTTP $code from $method $path (if this started after a T3_VERSION bump, the internal API changed — run t3ctl contract-check, see docs/COMPAT.md)" ;;
    esac
  fi
  printf '%s' "$resp"
}

rpc() { # rpc METHOD JSON_PAYLOAD → success value; one call over /ws (bin/t3-rpc)
  local method=$1 payload=$2 tok out rc
  tok=$(token) || exit 1
  out=$(printf '%s' "$payload" | T3CTL_URL="$BASE" T3CTL_BEARER="$tok" "$T3RPC" "$method" 2>&1); rc=$?
  if [ $rc -eq 3 ] && [[ $out == *"HTTP 401"* || $out == *"HTTP 403"* ]] && [ "$TOKEN_MODE" = self ] && [ "${_T3CTL_RETRIED:-0}" = 0 ]; then
    mint_token rpc "$tok" || die "RPC refused and re-minting a session failed"
    _T3CTL_RETRIED=1 rpc "$method" "$payload"
    return
  fi
  case $rc in
    0) printf '%s' "$out";;
    2) die "$method was rejected: $(head -c 600 <<<"$out")";;
    3) die "cannot reach t3's WebSocket RPC at $BASE: $out (HTTP 426 = protocol mismatch after a T3_VERSION bump — run t3ctl contract-check)";;
    *) die "$method failed: $(head -c 600 <<<"$out")";;
  esac
}

# Every client command carries who created it; t3's own orchestrator MCP marks
# agent-dispatched work agent/mcp, and the app labels such messages as agent-sent.
dispatch() { rpc orchestration.dispatchCommand "$(jq -c '. + {createdBy:"agent", creationSource:"mcp"}
  | if (.type == "thread.create" or .type == "message.dispatch") then . else del(.createdBy, .creationSource) end' <<<"$1")"; }

# V2 run status → the turn states t3ctl has always reported.
TURN_STATE='def turn_state: if . == null then "none"
    elif IN("preparing","queued","starting","running","waiting") then "running"
    elif . == "completed" then "completed" elif . == "failed" then "error"
    else "interrupted" end;'

# Shell snapshot, with the V1 fields the rest of this module (and its JSON
# output) reads: latestTurn{turnId,state}, session{status,lastError},
# hasPendingApprovals / hasPendingUserInput. Archived threads are folded back
# into .threads (they carry archivedAt), deleted ones dropped.
SHELL_NORM=$TURN_STATE'
  .threads = [(.threads + (.archivedThreads // []))[] | select(.deletedAt == null)
    | . + {latestTurn: (if .latestRunId == null then null else
             {turnId: .latestRunId,
              state: (if .activeRunId != null then "running"
                      elif .status == "idle" then "completed" else (.status | turn_state) end)} end),
           session: {status: (.activityRunStatus // (if .activeRunId != null then "running" else "idle" end)),
                     lastError: (.lastError // null)},
           hasPendingApprovals: (.pendingRuntimeRequest != null and .pendingRuntimeRequest.kind != "user_input"),
           hasPendingUserInput: (.pendingRuntimeRequest.kind == "user_input")}]'
shell_snap() { api GET /api/orchestration/shell | jq -c "$SHELL_NORM"; }

# Thread detail (bounded snapshot: full runs/requests, recent timeline) as
# {thread:{…, latestTurn, runs, messages, approvals, userInputs}}. An approval
# item only carries the model's description of the request; the exact command
# is on the command_execution item with the same native tool id, so `detail`
# is that command or null — never the description (the approver keys on it).
DETAIL_NORM=$TURN_STATE'
  .projection as $p
  | ($p.runs | sort_by(.ordinal)) as $runs
  | ($runs | last) as $last
  | ($p.runtimeRequests | map({(.id): .}) | add // {}) as $rr
  | ([$p.turnItems[] | select(.type == "command_execution" and .nativeItemRef.nativeId != null)
      | {(.nativeItemRef.nativeId): .input}] | add // {}) as $cmds
  | {thread: ($p.thread + {
      latestTurn: (if $last == null then null else
        {turnId: $last.id, state: ($last.status | turn_state), userMessageId: $last.userMessageId} end),
      approvals: [$p.turnItems[] | select(.type == "approval_request")
        | ($rr[.requestId] // {}) as $q
        | {summary: (.prompt // .title), turnId: .runId, createdAt: .startedAt,
           payload: {requestId, requestKind, status: ($q.status // "unknown"),
                     requestType: (if .requestKind == "command" then "command_execution_approval" else .requestKind end),
                     detail: (if .nativeItemRef.nativeId == null then null else $cmds[.nativeItemRef.nativeId] end),
                     decision: ($q.decision // null)}}],
      userInputs: [$p.turnItems[] | select(.type == "user_input_request")
        | select(($rr[.requestId].status // "") == "pending")
        | {requestId, questions} + (if .responseMode then {responseMode} else {} end)],
      providerSessions: [$p.providerSessions[] | {id, status}]})}'
uri() { jq -rn --arg s "$1" '$s | @uri'; }   # thread ids can hold %, : and / (delegated-task threads)
legacy_thread_detail() { # $1 thread id
  api GET "/api/orchestration/threads/$(uri "$1")/bounded" | jq -c "$DETAIL_NORM"
}

# ---- what MCP cannot do --------------------------------------------------------

legacy_list_all() { # every thread incl. archived (MCP's t3_thread_list has active ones only)
  shell_snap | jq -c '
    .projects as $ps
    | [.threads[]
       | {id, title,
          project: (.projectId as $p | ($ps[] | select(.id == $p) | .title)),
          mode: .runtimeMode,
          session: (.session.status // "none"),
          turn: (.latestTurn.state // "none"),
          settled: (.settledOverride == "settled"),
          snoozedUntil: (.snoozedUntil // null),
          pendingApprovals: .hasPendingApprovals,
          pendingUserInput: .hasPendingUserInput,
          lastError: (.session.lastError // null),
          updatedAt}]
    | sort_by(.updatedAt) | reverse | .[]'
}

legacy_approvals() { # $1 thread id → approval objects, one per line
  legacy_thread_detail "$1" | jq -c '.thread.approvals[]'
}

legacy_set_modes() { # $1 thread id, $2 runtime mode or "", $3 interaction mode or ""
  # MCP keeps a thread's modes fixed (t3_thread_configure: "Permission modes
  # stay"), so `say --mode/--plan/--execute` changes them here, before the
  # message goes out over MCP. check_mode already applied the policy.
  if [ -n "$2" ]; then
    dispatch "$(jq -cn --arg tid "$1" --arg m "$2" --arg cid "$(uuid)" \
      '{type:"thread.runtime-mode.set", commandId:$cid, threadId:$tid, runtimeMode:$m}')" >/dev/null || return 1
  fi
  if [ -n "$3" ]; then
    dispatch "$(jq -cn --arg tid "$1" --arg m "$3" --arg cid "$(uuid)" \
      '{type:"thread.interaction-mode.set", commandId:$cid, threadId:$tid, interactionMode:$m}')" >/dev/null || return 1
  fi
}

cmd_approve() {
  local thread=${1:-} rid=${2:-} decision=${3:-accept}
  [ -n "$thread" ] && [ -n "$rid" ] || die "usage: t3ctl approve <thread> <requestId> [accept|acceptForSession|acceptAlways|decline|cancel]"
  case $decision in accept|acceptForSession|acceptAlways|decline|cancel) ;; *)
    die "invalid decision '$decision'";; esac
  resolve_thread "$thread"
  dispatch "$(jq -cn --arg tid "$TID" --arg rid "$rid" --arg d "$decision" --arg cid "$(uuid)" '
    {type:"runtime-request.respond", commandId:$cid, threadId:$tid, requestId:$rid, decision:$d}')" >/dev/null || exit 1
  jq -cn --arg id "$TID" --arg rid "$rid" --arg d "$decision" \
    '{threadId:$id, requestId:$rid, decision:$d, responded:true}'
}

# The callback contract asks the session to run `$NOTIFY_CMD`, but in
# approval-required mode (the default for owned threads) running ANY command
# raises an approval — so an unattended thread could never report "done" or
# "waiting on approval" without someone approving the report itself (found
# 2026-09-08 via a probe thread). This accepts exactly that one command, in
# owned threads only, and nothing else: the detail must be a single notify invocation
# for the SAME thread id with a double-quoted summary free of shell syntax.
# Anything looser (another command chained on, backticks, $(), redirects)
# does not match and waits for a human/Cactus like before.
callback_pattern() { # $1 thread id → ERE for the approval detail
  local tid=$1
  # optional wrappers: codex shows `/usr/bin/bash -lc '<cmd>'`, claude shows
  # the bare command (sometimes `Bash: <cmd>`); summary = [^"'`$;&|<>\\]*
  printf '%s' "^((/usr/bin/)?bash -lc '|Bash: |Run: )?$NOTIFY_CMD --thread $tid --status (done|blocked|approval) \"[^\"'\`\$;&|<>\\\\]*\"'?\$"
}

approve_callbacks_once() { # $1 dry-run true/false → prints one JSON line per action
  local dry=$1 snap threads tid title detail rid pending pat
  snap=$(shell_snap) || return 1
  threads=$(jq -c --arg tag "$TAG" '.threads[] | select(.archivedAt == null and .hasPendingApprovals
             and (.title | startswith($tag))) | {id, title}' <<<"$snap")
  [ -n "$threads" ] || return 0
  while IFS= read -r t; do
    tid=$(jq -r .id <<<"$t"); title=$(jq -r .title <<<"$t")
    # pending = runtime requests still open; detail is the exact command (or
    # null when it cannot be tied to one — then it is left for a human)
    pending=$(legacy_thread_detail "$tid" | jq -c '
      .thread.approvals[] | select(.payload.status == "pending")
      | {requestId: .payload.requestId, requestType: .payload.requestType, detail: (.payload.detail // "")}')
    [ -n "$pending" ] || continue
    pat=$(callback_pattern "$tid")
    while IFS= read -r a; do
      rid=$(jq -r .requestId <<<"$a"); detail=$(jq -r .detail <<<"$a")
      if [ "$(jq -r .requestType <<<"$a")" = command_execution_approval ] && [[ $detail =~ $pat ]]; then
        if [ "$dry" = true ]; then
          jq -cn --arg tid "$tid" --arg title "$title" --arg rid "$rid" --arg d "$detail" \
            '{threadId:$tid, title:$title, requestId:$rid, detail:$d, action:"would-accept"}'
        else
          dispatch "$(jq -cn --arg tid "$tid" --arg rid "$rid" --arg cid "$(uuid)" '
            {type:"runtime-request.respond", commandId:$cid, threadId:$tid,
             requestId:$rid, decision:"accept"}')" >/dev/null || return 1
          jq -cn --arg tid "$tid" --arg title "$title" --arg rid "$rid" --arg d "$detail" --arg at "$(now)" \
            '{threadId:$tid, title:$title, requestId:$rid, detail:$d, action:"accepted", at:$at}'
        fi
      else
        jq -cn --arg tid "$tid" --arg title "$title" --arg rid "$rid" --arg d "$detail" \
          '{threadId:$tid, title:$title, requestId:$rid, detail:$d, action:"left-for-human"}'
      fi
    done <<<"$pending"
  done <<<"$threads"
}

cmd_approve_callbacks() {
  local loop=0 dry=false
  while [ $# -gt 0 ]; do case $1 in
    --loop) num --loop "${2:-}"; loop=$2; shift 2;;
    --dry-run) dry=true; shift;;
    *) die "usage: t3ctl approve-callbacks [--loop seconds] [--dry-run]";;
  esac; done
  if [ "$loop" -le 0 ]; then approve_callbacks_once "$dry"; return; fi
  # Service mode: one failed pass (t3 restarting, token rotating) must not
  # kill the loop — `die` exits the subshell only.
  while :; do
    ( approve_callbacks_once "$dry" ) || echo "approve-callbacks: pass failed (t3 down or token rotating?); retrying in ${loop}s" >&2
    sleep "$loop"
  done
}

legacy_stop() { # $1 thread id, $2 active run id or ""
  # V2 has no session-stop command and MCP's `stop` leaves provider sessions
  # attached: interrupt the active run, then detach every live session.
  local sid
  [ -z "$2" ] || dispatch "$(jq -cn --arg tid "$1" --arg run "$2" --arg cid "$(uuid)" \
    '{type:"run.interrupt", commandId:$cid, threadId:$tid, runId:$run}')" >/dev/null || return 1
  for sid in $(legacy_thread_detail "$1" | jq -r '.thread.providerSessions[] | select(.status != "stopped") | .id'); do
    dispatch "$(jq -cn --arg tid "$1" --arg sid "$sid" --arg cid "$(uuid)" \
      '{type:"provider-session.detach", commandId:$cid, threadId:$tid, providerSessionId:$sid, reason:"t3ctl stop"}')" >/dev/null || return 1
  done
}

legacy_rm() { # $1 thread id (MCP has no delete tool)
  dispatch "$(jq -cn --arg tid "$1" --arg cid "$(uuid)" '{type:"thread.delete", commandId:$cid, threadId:$tid}')" >/dev/null
}

cmd_project() {
  case ${1:-} in
    add)
      shift
      local path="" title=""
      while [ $# -gt 0 ]; do case $1 in
        --title) title=$2; shift 2;;
        -*) die "unknown flag $1";;
        *) [ -z "$path" ] && path=$1 || die "unexpected arg: $1"; shift;;
      esac; done
      [ -n "$path" ] || die "usage: t3ctl project add <path> [--title T]"
      [ -d "$path" ] || die "no such directory: $path"
      [ -n "$title" ] || title=$(basename "$path")
      local snap existing
      snap=$(shell_snap) || exit 1
      existing=$(jq -c --arg r "$path" '.projects[] | select(.workspaceRoot == $r) | {id, title, workspaceRoot}' <<<"$snap")
      if [ -n "$existing" ]; then jq -c '. + {created:false}' <<<"$existing"; return; fi
      local pid; pid=$(uuid)
      rpc projects.mutate "$(jq -cn --arg pid "$pid" --arg title "$title" --arg root "$path" --arg cid "$(uuid)" '
        {type:"project.create", commandId:$cid, projectId:$pid, title:$title, workspaceRoot:$root}')" >/dev/null || exit 1
      jq -cn --arg pid "$pid" --arg title "$title" --arg root "$path" '{id:$pid, title:$title, workspaceRoot:$root, created:true}';;
    set-model)
      local project=${2:-} sel=${3:-}
      [ -n "$project" ] && [ -n "$sel" ] || die "usage: t3ctl project set-model <project> <instanceId>:<model>"
      case $sel in *:*) ;; *) die "model must be instanceId:model (e.g. claudeAgent:claude-opus-5-5)";; esac
      resolve_project "$project"
      rpc projects.mutate "$(jq -cn --arg pid "$PID" --arg i "${sel%%:*}" --arg mo "${sel#*:}" --arg cid "$(uuid)" '
        {type:"project.update", commandId:$cid, projectId:$pid,
         defaultModelSelection:{instanceId:$i, model:$mo}}')" >/dev/null || exit 1
      jq -cn --arg pid "$PID" --arg i "${sel%%:*}" --arg mo "${sel#*:}" \
        '{projectId:$pid, defaultModelSelection:{instanceId:$i, model:$mo}}';;
    *) die "usage: t3ctl project add <path> [--title T] | set-model <project> <instanceId>:<model>";;
  esac
}

legacy_contract_check() { # → JSON fragment for contract-check; dies on a broken contract
  local raw n tid db_checked=false
  raw=$(api GET /api/orchestration/shell) || exit 1
  jq -e '(.projects | type == "array") and (.threads | type == "array") and (.archivedThreads | type == "array")' >/dev/null <<<"$raw" \
    || die "V2 shell snapshot shape changed (projects/threads/archivedThreads)"
  n=$(jq -r '.projects | length' <<<"$raw")
  tid=$(jq -r '.threads[0].id // empty' <<<"$raw")
  if [ -n "$tid" ]; then
    jq -e '.threads[0] | has("latestRunId") and has("activeRunId") and has("status") and has("pendingRuntimeRequest")
             and has("runtimeMode") and has("settledOverride") and has("archivedAt")' >/dev/null <<<"$raw" \
      || die "V2 thread shell fields changed (latestRunId/activeRunId/status/pendingRuntimeRequest/…)"
    api GET "/api/orchestration/threads/$(uri "$tid")/bounded" | jq -e '.projection
        | (.runs | type == "array") and (.runtimeRequests | type == "array") and (.messages | type == "array")
          and (.turnItems | type == "array") and (.providerSessions | type == "array") and (.thread | has("runtimeMode"))' >/dev/null \
      || die "V2 bounded thread snapshot shape changed"
  fi
  # The WebSocket RPC leg: protocol, auth and a read-only RPC.
  rpc orchestration.searchThreads '{"query":"t3ctl-contract-check","limit":1}' | jq -e '.matches | type == "array"' >/dev/null \
    || die "orchestration.searchThreads result shape changed (the /ws RPC leg)"
  if command -v sqlite3 >/dev/null && [ -r "$DB" ]; then
    # t3-events `managed` (run by t3-notify inside the coding session, which
    # holds no t3ctl credential) correlates dispatches with runs here, read-only
    sqlite3 -readonly "$DB" "SELECT run_id, thread_id, ordinal, json_extract(payload_json, '\$.userMessageId') FROM orchestration_v2_projection_runs LIMIT 0;" >/dev/null 2>&1 \
      || die "run-correlation SQL schema changed (orchestration_v2_projection_runs) — see docs/COMPAT.md"
    db_checked=true
  fi
  jq -cn --argjson projects "$n" --argjson threads "$(jq '(.threads|length) + (.archivedThreads|length)' <<<"$raw")" \
         --arg db "$DB" --argjson dbc "$db_checked" \
    '{projects:$projects, threads:$threads, rpc:true, db:$db, dbSchemaChecked:$dbc}'
}
