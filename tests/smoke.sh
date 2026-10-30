#!/usr/bin/env bash
# smoke.sh — exercise bin/t3ctl against a fake t3 server (tests/fake_t3_server.py,
# whose /mcp is tests/fake_t3_mcp.py) and a fake t3 CLI (tests/fake_t3_cli.py).
#
# Covers both credentials (self-minted MCP sign-in with a ceiling, the
# least-privilege legacy session, rotation on 401, predecessor revocation,
# t3-token-renew in file mode), every client-side guard (each must refuse
# AND send nothing over MCP), dispatch shapes over MCP, the callback footer +
# approve-callbacks matching, say --expect-turn (stale and concurrent
# duplicate sends), --dispatch-id retries, last + the text cap, literal model
# selection, project add/set-model, search, contract-check, and the v0.5.0
# output shapes (tests/shapes.py).
#
#   bash tests/smoke.sh
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
T3CTL=$ROOT/bin/t3ctl
mkdir -p "$ROOT/tmp"
TMP=$(mktemp -d "$ROOT/tmp/smoke.XXXXXX")
cleanup() { [ -n "${SERVER_PID:-}" ] && kill "$SERVER_PID" 2>/dev/null || true; rm -rf "$TMP"; }
trap cleanup EXIT

pass=0
ok()   { pass=$((pass + 1)); echo "  ok   $*"; }
fail() { echo "  FAIL $*" >&2; exit 1; }
assert_contains() { grep -q -- "$2" <<<"$1" || fail "$3 (expected to contain: $2)"$'\n'"got: $1"; }
mcp_sends() { jq '[.[] | select(.type == "mcp.t3_thread_send" or .type == "mcp.t3_thread_launch")] | length' "$STATE" 2>/dev/null || echo 0; }

# ---- fixtures ---------------------------------------------------------------
PORT=$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])')
mkdir -p "$TMP/bin" "$TMP/t3home/userdata" "$TMP/auth"
TOKENS=$TMP/tokens; printf 'good\n' > "$TOKENS"
MCP_TOKENS=$TMP/mcp-tokens; : > "$MCP_TOKENS"
STATE=$TMP/state.json
PID_PROJECT=11111111-1111-4111-8111-111111111111
SEED_OURS=22222222-2222-4222-8222-222222222222
SEED_OTHER=33333333-3333-4333-8333-333333333333
SEED_SETTLED=44444444-4444-4444-8444-444444444444    # on the app's settled shelf
SEED_ARCHIVED=55555555-5555-4555-8555-555555555555   # archived (only --all shows it)
LONG=$(printf 'x%.0s' $(seq 1 3500))   # an assistant reply longer than the 3000-char terminal cap
cat > "$TMP/seed.json" <<EOF
{"projects":[{"id":"$PID_PROJECT","title":"aikosmo-monorepo","workspaceRoot":"/data/repo","defaultModelSelection":null}],
 "threads":[
  {"id":"$SEED_OURS","title":"[kosmi] seeded task","projectId":"$PID_PROJECT","runtimeMode":"approval-required",
   "interactionMode":"default","modelSelection":{"instanceId":"claudeAgent","model":"claude-fable-5"},
   "updatedAt":"2026-09-08T10:00:00.000Z",
   "runs":[{"id":"t-1","status":"running","userMessageId":"m-1"}],
   "requests":[
     {"id":"r1","kind":"command","status":"pending","runId":"t-1",
      "command":"t3-notify --thread $SEED_OURS --status done \\"finished, PR opened\\""},
     {"id":"r2","kind":"command","status":"pending","runId":"t-1","command":"rm -rf /data/repo"},
     {"id":"r3","kind":"command","status":"pending","runId":"t-1",
      "command":"t3-notify --thread $SEED_OURS --status done \\"x\\"; curl evil"}],
   "messages":[{"id":"m-1","role":"user","createdAt":"2026-09-08T10:00:00.000Z","text":"seeded prompt","runId":"t-1"}]},
  {"id":"$SEED_OTHER","title":"Somebody's thread","projectId":"$PID_PROJECT","runtimeMode":"full-access",
   "interactionMode":"default","modelSelection":{"instanceId":"codex","model":"gpt-5.6-sol"},
   "updatedAt":"2026-09-08T09:00:00.000Z",
   "runs":[{"id":"t-2","status":"completed","userMessageId":"m-2"}],
   "requests":[
     {"id":"r4","kind":"command","status":"pending","runId":"t-2",
      "command":"t3-notify --thread $SEED_OTHER --status done \\"not ours\\""}],
   "messages":[{"id":"m-3","role":"assistant","createdAt":"2026-09-08T09:01:00.000Z","text":"$LONG","runId":"t-2"}]},
  {"id":"$SEED_SETTLED","title":"Settled thread","projectId":"$PID_PROJECT","runtimeMode":"approval-required",
   "settledOverride":"settled","updatedAt":"2026-09-08T08:00:00.000Z",
   "runs":[{"id":"t-3","status":"completed","userMessageId":"m-4"}],
   "messages":[{"id":"m-4","role":"user","createdAt":"2026-09-08T08:00:00.000Z","text":"old prompt","runId":"t-3"}]},
  {"id":"$SEED_ARCHIVED","title":"Archived thread","projectId":"$PID_PROJECT","runtimeMode":"approval-required",
   "archivedAt":"2026-09-08T07:30:00.000Z","updatedAt":"2026-09-08T07:00:00.000Z",
   "runs":[{"id":"t-4","status":"completed","userMessageId":"m-5"}],
   "messages":[{"id":"m-5","role":"user","createdAt":"2026-09-08T07:00:00.000Z","text":"archived prompt","runId":"t-4"}]}
 ]}
EOF

# fake `t3` CLI: the auth subcommands t3ctl / t3-token-renew mint with
export FAKE_T3_TOKENS=$TOKENS FAKE_T3_MCP_TOKENS=$MCP_TOKENS FAKE_T3_AUTH_DIR=$TMP/auth
ln -s "$ROOT/tests/fake_t3_cli.py" "$TMP/bin/t3"
ln -s "$ROOT/bin/t3-notify" "$TMP/bin/t3-notify"

# V2 run-correlation schema stand-in (the columns bin/t3-events' `managed` reads)
sqlite3 "$TMP/t3home/userdata/statev2.sqlite" <<'SQL'
CREATE TABLE orchestration_v2_projection_runs(run_id TEXT PRIMARY KEY, thread_id TEXT, ordinal INTEGER, payload_json TEXT);
SQL

python3 "$ROOT/tests/fake_t3_server.py" --port "$PORT" --tokens-file "$TOKENS" --mcp-tokens-file "$MCP_TOKENS" \
  --auth-dir "$TMP/auth" --state "$STATE" --seed "$TMP/seed.json" &
SERVER_PID=$!
for _ in $(seq 1 50); do curl -s -o /dev/null "http://127.0.0.1:$PORT/" && break; sleep 0.1; done

export PATH="$TMP/bin:$PATH"
# container mode, no /etc/t3ctl.conf: everything from the environment
export T3CTL_CONF=/dev/null T3CTL_URL="http://127.0.0.1:$PORT" T3CODE_HOME="$TMP/t3home" T3CTL_T3_BIN="$TMP/bin/t3"
export T3CTL_TOKEN_MODE=self T3CTL_TOKEN_FILE="$TMP/t3home/t3ctl.token" T3CTL_SESSION_FILE="$TMP/t3home/t3ctl.token.session"
export T3CTL_DB="$TMP/t3home/userdata/statev2.sqlite" T3CTL_TAG="[kosmi]" T3CTL_NOTIFY_BIN=t3-notify
export T3CTL_ALLOW_FULL_ACCESS=0 T3CTL_DENY_ORIGINS=webhook:auto-demo T3CTL_LOCK_DIR="$TMP/locks"
unset T3CTL_MAX_RUNNING T3CTL_MCP_ACCESS T3CTL_MCP_TOKEN_FILE T3CTL_EVENTS_ENABLED
MCP_FILE=$TMP/t3home/t3ctl.token.mcp            # default: next to the legacy token
MCP_SID_FILE=$TMP/t3home/t3ctl.token.session.mcp

echo "t3ctl smoke against fake t3 on :$PORT"

# ---- 1. both credentials: self-minted, least privilege, rotated on 401 -----
printf 'stale\n' > "$TMP/t3home/t3ctl.token"
out=$("$T3CTL" projects 2>"$TMP/err")
assert_contains "$out" '"title":"aikosmo-monorepo"' "projects over MCP"
[ -s "$MCP_FILE" ] || fail "MCP token not minted next to the legacy token ($MCP_FILE)"
assert_contains "$(cat "$TMP/err")" "minted mcp session" "MCP mint message on stderr"
mtok=$(cat "$MCP_FILE")
grep -q "^$mtok auto\$" "$MCP_TOKENS" || fail "MCP token must carry the deployment's ceiling (auto with ALLOW_FULL_ACCESS=0): $(cat "$MCP_TOKENS")"
grep -q -- '--scope orchestration:read --scope orchestration:operate' "$TMP/auth/calls" || fail "pairing code must carry exactly the ceiling's scopes"
[ "$(cat "$TMP/t3home/t3ctl.token")" = stale ] || fail "an MCP-only command must not touch the legacy token"
ok "MCP credential: minted via pairing-code sign-in, ceiling auto, next to the legacy token"

out=$("$T3CTL" approvals "$SEED_OURS" 2>"$TMP/err")
assert_contains "$out" '"requestId":"r1"' "approvals over the legacy path"
[ "$(cat "$TMP/t3home/t3ctl.token")" != stale ] || fail "legacy token was not re-minted"
assert_contains "$(cat "$TMP/err")" "minted rpc session" "legacy mint message"
ltok=$(cat "$TMP/t3home/t3ctl.token")
jq -e --arg t "$ltok" 'select(.token == $t) | .scopes == ["orchestration:read","orchestration:operate"] and .subject != "mcp-client"' "$TMP/auth/sessions.jsonl" >/dev/null \
  || fail "legacy session must hold orchestration:read + orchestration:operate only"
ok "legacy credential: ordinary session, orchestration:read + operate only"

# credentials are path-bound: neither is accepted on the other's path
code=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $mtok" -H 'x-t3-orchestration-protocol: 2' "http://127.0.0.1:$PORT/api/orchestration/shell")
[ "$code" = 401 ] || fail "MCP token must be refused on the legacy API (got $code)"
if T3CTL_MCP_BEARER="$ltok" "$ROOT/bin/t3-mcp" call t3_project_list 2>"$TMP/err"; then fail "legacy token must be refused on /mcp"; fi
assert_contains "$(cat "$TMP/err")" "HTTP 401 invalid_mcp_credential" "t3-mcp names the refusal"
if grep -q -- "$ltok" "$TMP/err"; then fail "t3-mcp must never print the credential"; fi
ok "each credential only works on its own path; errors never echo a token"

old_sids="$(cat "$MCP_SID_FILE") $(cat "$TMP/t3home/t3ctl.token.session")"
: > "$TOKENS"; : > "$MCP_TOKENS"               # revoke everything server-side
out=$("$T3CTL" token status); assert_contains "$out" '"valid":false' "token status after revoke"
out=$("$T3CTL" projects 2>/dev/null); assert_contains "$out" 'aikosmo-monorepo' "projects after mid-run 401 (MCP)"
out=$("$T3CTL" approvals "$SEED_OURS" 2>/dev/null); assert_contains "$out" '"requestId":"r1"' "approvals after mid-run 401 (legacy)"
for sid in $old_sids; do grep -q "revoked $sid" "$TMP/auth/revoked" || fail "predecessor session $sid was not revoked"; done
out=$("$T3CTL" token status)
assert_contains "$out" '"valid":true' "legacy token valid after rotation"
assert_contains "$out" '"mcp":{"tokenFile":"'"$MCP_FILE"'"' "token status reports the MCP credential"
jq -e '.mcp.valid == true and .mcp.access == "auto"' >/dev/null <<<"$out" || fail "MCP token valid after rotation: $out"
ok "401 on either path → that credential rotated once, predecessors revoked"

# a failed sign-in installs nothing, keeps the working token, revokes its unused pairing code
cp "$MCP_FILE" "$TMP/mcp.before"
if FAKE_T3_BAD_PAIRING=1 "$T3CTL" token renew >/dev/null 2>"$TMP/err"; then fail "renew with a refused sign-in must fail"; fi
assert_contains "$(cat "$TMP/err")" "MCP sign-in failed" "sign-in failure is reported"
cmp -s "$MCP_FILE" "$TMP/mcp.before" || fail "a failed sign-in must not replace the working MCP token"
grep -q "^pairing-revoked " "$TMP/auth/revoked" || fail "the unused pairing code must be revoked"
if grep -q "PAIR-" "$TMP/err"; then fail "the pairing code must never be printed"; fi
if FAKE_T3_FAIL="auth pairing create" "$T3CTL" token renew >/dev/null 2>"$TMP/err"; then fail "renew must fail when pairing create fails"; fi
cmp -s "$MCP_FILE" "$TMP/mcp.before" || fail "a failed pairing must not replace the working MCP token"
"$T3CTL" projects >/dev/null || fail "the kept MCP token still works"
out=$(T3CTL_ALLOW_FULL_ACCESS=1 T3CTL_MCP_ACCESS=full-access "$T3CTL" token renew 2>"$TMP/err")
assert_contains "$out" '"renewed":true' "token renew rotates both"
grep -q "^$(cat "$MCP_FILE") full-access\$" "$MCP_TOKENS" || fail "T3CTL_MCP_ACCESS=full-access (allowed) mints a full-access ceiling"
grep -q -- '--scope settings:write --scope source-control:write' "$TMP/auth/calls" || fail "a full-access sign-in needs the scopes T3 attaches to it"
T3CTL_ALLOW_FULL_ACCESS=0 T3CTL_MCP_ACCESS=full-access "$T3CTL" token status 2>"$TMP/err" | jq -e '.mcp.access == "auto"' >/dev/null \
  || fail "T3CTL_MCP_ACCESS=full-access must fall back when full access is disabled"
assert_contains "$(cat "$TMP/err")" "T3CTL_MCP_ACCESS=full-access ignored" "ceiling fallback is announced"
ok "token renew: both credentials; the ceiling follows T3CTL_MCP_ACCESS within the full-access policy"
"$T3CTL" token renew >/dev/null 2>&1   # back to the auto ceiling for the guard tests below

# ---- 1b. origin gate --------------------------------------------------------
if HERMES_SESSION_PLATFORM=webhook HERMES_SESSION_CHAT_ID="webhook:auto-demo:d1" HERMES_SESSION_CHAT_NAME="webhook/auto-demo" \
   "$T3CTL" projects 2>"$TMP/err"; then fail "auto-demo lane must be refused"; fi
assert_contains "$(cat "$TMP/err")" "not available from this lane" "origin gate message"
out=$(HERMES_SESSION_PLATFORM=webhook HERMES_SESSION_CHAT_ID="webhook:t3-callback:d2" HERMES_SESSION_CHAT_NAME="webhook/t3-callback" "$T3CTL" projects)
assert_contains "$out" aikosmo-monorepo "t3-callback lane allowed"
out=$(HERMES_SESSION_PLATFORM=discord HERMES_SESSION_CHAT_ID="123" "$T3CTL" projects); assert_contains "$out" aikosmo-monorepo "discord lane allowed"
out=$(T3CTL_DENY_ORIGINS= HERMES_SESSION_PLATFORM=webhook HERMES_SESSION_CHAT_ID="webhook:auto-demo:d1" "$T3CTL" projects); assert_contains "$out" aikosmo-monorepo "empty T3CTL_DENY_ORIGINS = no gate (devbox default)"
before=$(mcp_sends)
if HERMES_SESSION_PLATFORM=webhook HERMES_SESSION_CHAT_ID="webhook:auto-demo:d1" "$T3CTL" new aikosmo-monorepo "x" 2>/dev/null; then fail "auto-demo lane must not start threads"; fi
[ "$(mcp_sends)" -eq "$before" ] || fail "a refused lane must send nothing over MCP"
ok "origin gate refuses the auto-demo lane only, before any MCP call"

# ---- 1c. numeric flags ------------------------------------------------------
if "$T3CTL" show "$SEED_OURS" -n two 2>"$TMP/err"; then fail "non-numeric -n must error"; fi
assert_contains "$(cat "$TMP/err")" "expects a non-negative integer" "numeric validation message"
if "$T3CTL" watch "$SEED_OURS" --timeout 1x 2>/dev/null; then fail "non-numeric --timeout must error"; fi
if "$T3CTL" search seeded --limit -1 2>/dev/null; then fail "negative --limit must error"; fi
ok "numeric flags reject non-numeric values"

# ---- 2. new: approval-required, [kosmi] title, callback footer, over MCP ----
out=$("$T3CTL" new aikosmo-monorepo "Fix the flaky test" --title "flaky test" --model claudeAgent:claude-fable-5-1)
assert_contains "$out" '"mode":"approval-required"' "new default mode"
assert_contains "$out" '"title":"\[kosmi\] flaky test"' "new title tag"
TID=$(jq -r .threadId <<<"$out")
DISPATCH=$(jq -r .dispatchId <<<"$out")
launch=$(jq -c '.[] | select(.type=="mcp.t3_thread_launch")' "$STATE" | tail -1)
assert_contains "$launch" '"modelSelection":{"instanceId":"claudeAgent","model":"claude-fable-5-1"}' "literal instanceId:model selection"
assert_contains "$launch" '"runtimeMode":"approval-required"' "launch mode"
assert_contains "$launch" "\"clientRequestId\":\"$DISPATCH\"" "launch is keyed by the dispatchId"
if grep -q '"message"' <<<"$launch"; then fail "launch must be idle: the footer needs the thread id first"; fi
turn=$(jq -c '.[] | select(.type=="mcp.t3_thread_send")' "$STATE" | tail -1)
assert_contains "$turn" "t3-notify --thread $TID --status done|blocked|approval" "callback footer names t3-notify + thread id"
assert_contains "$turn" '"mode":"queue"' "a message never steers"
assert_contains "$turn" "\"clientRequestId\":\"$DISPATCH\"" "send is keyed by the dispatchId"
assert_contains "$out" '"turn":"running"' "receipt carries the run the message started"
jq -e '.turnId != null and .messageId != null and .previousTurnId == "none"' >/dev/null <<<"$out" || fail "receipt fields: $out"
assert_contains "$out" '"monitoring":"manual","monitoringReason":"events-disabled"' "manual fallback names its reason"
ok "new → idle t3_thread_launch + t3_thread_send with footer"

# --dispatch-id retries return the same work instead of new work
before=$(jq '[.[] | select(.type=="mcp.t3_thread_send")] | length' "$STATE")
again=$("$T3CTL" new aikosmo-monorepo "Fix the flaky test" --title "flaky test" --model claudeAgent:claude-fable-5-1 --dispatch-id "$DISPATCH")
[ "$(jq -r .threadId <<<"$again")" = "$TID" ] || fail "a retried new must return the same thread"
[ "$(jq -r .messageId <<<"$again")" = "$(jq -r .messageId <<<"$out")" ] || fail "a retried new must return the same message"
[ "$(jq '[.[] | select(.type=="mcp.t3_thread_send")] | length' "$STATE")" -eq "$before" ] || fail "a retried new must not send again"
if "$T3CTL" new aikosmo-monorepo "x" --dispatch-id 'bad id!' 2>/dev/null; then fail "--dispatch-id is validated"; fi
ok "new --dispatch-id: idempotent retry (same thread, same message, nothing re-sent)"

out=$("$T3CTL" say "$TID" "also update the docs")
assert_contains "$out" '"mode":"approval-required"' "say inherits mode"
assert_contains "$(jq -c '.[] | select(.type=="mcp.t3_thread_send")' "$STATE" | tail -1)" '"mode":"queue"' "say to a busy thread queues, never steers"
ok "say inherits the thread's runtime mode"

# ---- 2b. say --expect-turn: a sender that read an older turn is refused ----
cur=$("$T3CTL" last "$TID" --json | jq -r .turnId)
[ -n "$cur" ] && [ "$cur" != null ] || fail "last --json carries the latest turn id"
before=$(mcp_sends)
if "$T3CTL" say "$TID" "stale follow-up" --expect-turn t-stale 2>"$TMP/err"; then fail "say --expect-turn with a stale turn must be refused"; fi
assert_contains "$(cat "$TMP/err")" "has moved past turn t-stale (latest is now $cur)" "stale-turn refusal names both turns"
[ "$(mcp_sends)" -eq "$before" ] || fail "refused say must not send over MCP"
out=$("$T3CTL" say "$TID" "round 2" --expect-turn "$cur"); assert_contains "$out" "\"previousTurnId\":\"$cur\"" "matching --expect-turn sends"
cur=$(jq -r .turnId <<<"$out")
"$T3CTL" say "$TID" "copy A" --expect-turn "$cur" >"$TMP/a" 2>&1 & pa=$!
"$T3CTL" say "$TID" "copy B" --expect-turn "$cur" >"$TMP/b" 2>&1 & pb=$!
ra=0; rb=0; wait $pa || ra=$?; wait $pb || rb=$?
[ $((ra == 0 ? 1 : 0)) -ne $((rb == 0 ? 1 : 0)) ] || fail "exactly one of two concurrent senders with the same --expect-turn must win (rc $ra/$rb): $(cat "$TMP/a" "$TMP/b")"
assert_contains "$(cat "$TMP/a" "$TMP/b")" "has moved past turn $cur" "the losing sender is told why"
ok "say --expect-turn refuses stale and concurrent duplicate sends"

# A say replayed with its --dispatch-id after its run became visible keeps
# the predecessor it was sent after: t3-events watches --after-turn it, and
# reporting the replayed run itself there would never deliver the reply.
cur=$("$T3CTL" last "$TID" --json | jq -r .turnId)
first=$("$T3CTL" say "$TID" "round 3" --dispatch-id say-replay-1 --expect-turn "$cur")
[ "$(jq -r .previousTurnId <<<"$first")" = "$cur" ] || fail "first say reports the turn it followed: $first"
again=$("$T3CTL" say "$TID" "round 3" --dispatch-id say-replay-1 --expect-turn "$cur")
[ "$(jq -r .messageId <<<"$again")" = "$(jq -r .messageId <<<"$first")" ] || fail "a replayed say returns the same message"
[ "$(jq -r .previousTurnId <<<"$again")" = "$cur" ] || fail "a replayed say keeps its original predecessor: $again"
[ "$(jq -r .previousTurnId <<<"$again")" != "$(jq -r .turnId <<<"$again")" ] || fail "a receipt never names its own run as its predecessor"
ok "say --dispatch-id replay keeps the turn it was first sent after"

# ---- 2c. last / text cap ----------------------------------------------------
out=$("$T3CTL" last "$SEED_OTHER"); [ ${#out} -eq 3500 ] || fail "last prints the whole reply as plain text (got ${#out} chars)"
out=$("$T3CTL" show "$SEED_OTHER"); assert_contains "$out" "$LONG\"" "piped show is not truncated"
out=$(T3CTL_TEXT_CAP=100 "$T3CTL" show "$SEED_OTHER"); assert_contains "$out" '…\[truncated\]' "explicit T3CTL_TEXT_CAP still truncates"
if "$T3CTL" last "$SEED_OURS" 2>"$TMP/err"; then fail "last on a thread without a reply must fail"; fi
assert_contains "$(cat "$TMP/err")" "no assistant reply" "last says why"
ok "last prints the full reply; piped output is uncapped"

# ---- 3. full-access gate ----------------------------------------------------
before=$(mcp_sends)
if "$T3CTL" new aikosmo-monorepo "x" --full-access 2>"$TMP/err"; then fail "--full-access must be refused with T3CTL_ALLOW_FULL_ACCESS=0"; fi
assert_contains "$(cat "$TMP/err")" "disabled on this deployment" "full-access refusal message"
if "$T3CTL" say "$SEED_OURS" "x" --full-access 2>"$TMP/err"; then fail "say --full-access must be refused with T3CTL_ALLOW_FULL_ACCESS=0"; fi
if T3CTL_ALLOW_FULL_ACCESS=1 "$T3CTL" new aikosmo-monorepo "x" --mode full-access 2>"$TMP/err"; then fail "--mode full-access must need the explicit flag"; fi
assert_contains "$(cat "$TMP/err")" "pass --full-access" "mode full-access refusal"
[ "$(mcp_sends)" -eq "$before" ] || fail "refused full-access must send nothing over MCP"
# allowed by policy but above the credential's ceiling: T3 itself refuses, and says why
if T3CTL_ALLOW_FULL_ACCESS=1 T3CTL_MAX_RUNNING=10 "$T3CTL" new aikosmo-monorepo "x" --full-access --model codex:gpt-5.6-sol 2>"$TMP/err"; then fail "a full-access launch above an auto ceiling must be refused by T3"; fi
assert_contains "$(cat "$TMP/err")" "runtime_mode_escalation_denied" "ceiling refusal carries T3's code"
assert_contains "$(cat "$TMP/err")" "capped below this thread's mode" "ceiling refusal explains the credential"
out=$(env -u T3CTL_ALLOW_FULL_ACCESS T3CTL_MAX_RUNNING=10 "$T3CTL" token renew 2>/dev/null)   # default policy → full-access ceiling
out=$(env -u T3CTL_ALLOW_FULL_ACCESS T3CTL_MAX_RUNNING=10 "$T3CTL" new aikosmo-monorepo "x" --full-access --model codex:gpt-5.6-sol); assert_contains "$out" '"mode":"full-access"' "default (unset) allows --full-access, devbox behaviour"
"$T3CTL" stop "$(jq -r .threadId <<<"$out")" >/dev/null   # keep the running count where the later cap test expects it
ok "full-access gate: refused at 0 before MCP, allowed by default, always needs the explicit flag; T3 enforces the ceiling"
out=$(env -u T3CTL_ALLOW_FULL_ACCESS T3CTL_DEFAULT_MODE=full-access T3CTL_MAX_RUNNING=10 "$T3CTL" new aikosmo-monorepo "x" --model codex:gpt-5.6-sol)
assert_contains "$out" '"mode":"full-access"' "T3CTL_DEFAULT_MODE=full-access makes new start full-access"
"$T3CTL" stop "$(jq -r .threadId <<<"$out")" >/dev/null
out=$(T3CTL_DEFAULT_MODE=full-access T3CTL_MAX_RUNNING=10 "$T3CTL" new aikosmo-monorepo "x" --model codex:gpt-5.6-sol 2>"$TMP/err")
assert_contains "$out" '"mode":"approval-required"' "full-access default falls back when T3CTL_ALLOW_FULL_ACCESS=0"
assert_contains "$(cat "$TMP/err")" "T3CTL_DEFAULT_MODE=full-access ignored" "fallback is announced"
"$T3CTL" stop "$(jq -r .threadId <<<"$out")" >/dev/null
ok "T3CTL_DEFAULT_MODE: honoured when allowed, falls back when not"
before=$(mcp_sends)
if env -u T3CTL_ALLOW_FULL_ACCESS T3CTL_DEFAULT_MODE=full-access "$T3CTL" new aikosmo-monorepo "x" --mode approval-required --model codex:gpt-5.6-sol 2>"$TMP/err"; then fail "--mode must not override a deployment-fixed T3CTL_DEFAULT_MODE"; fi
assert_contains "$(cat "$TMP/err")" "fixes the mode of new threads to full-access" "fixed-mode refusal names the mode"
[ "$(mcp_sends)" -eq "$before" ] || fail "fixed-mode refusal must send nothing"
ok "T3CTL_DEFAULT_MODE set → new --mode cannot downgrade/upgrade it"

# ---- 4. running cap ---------------------------------------------------------
before=$(mcp_sends)
if T3CTL_MAX_RUNNING=1 "$T3CTL" new aikosmo-monorepo "y" --model codex:gpt-5.6-sol 2>"$TMP/err"; then fail "running cap not enforced"; fi
assert_contains "$(cat "$TMP/err")" "already running (limit 1)" "cap message"
[ "$(mcp_sends)" -eq "$before" ] || fail "the running cap must refuse before launching"
ok "new refuses beyond T3CTL_MAX_RUNNING, before any launch"

# ---- 5. ownership guard -----------------------------------------------------
n_before=$(jq length "$STATE")
for op in stop interrupt rm; do
  if "$T3CTL" "$op" "$SEED_OTHER" 2>"$TMP/err"; then fail "$op on somebody else's thread must be refused"; fi
  assert_contains "$(cat "$TMP/err")" "somebody else's session" "ownership message ($op)"
done
[ "$(jq length "$STATE")" -eq "$n_before" ] || fail "refused stop/interrupt/rm must not dispatch anything"
out=$("$T3CTL" stop "$SEED_OTHER" --force); assert_contains "$out" '"done":true' "stop --force"
out=$("$T3CTL" interrupt "$SEED_OURS"); assert_contains "$out" '"op":"interrupt"' "interrupt own thread"
assert_contains "$(jq -c '.[] | select(.type=="mcp.t3_thread_interrupt")' "$STATE" | tail -1)" "\"threadId\":\"$SEED_OURS\"" "interrupt goes over MCP"
if "$T3CTL" interrupt "$SEED_OURS" 2>"$TMP/err"; then fail "interrupt of an idle thread must fail"; fi
assert_contains "$(cat "$TMP/err")" "no active run" "idle interrupt says why"
ok "stop/interrupt/rm refuse foreign threads without --force (nothing dispatched)"

# ---- 6. approve-callbacks (legacy path) -------------------------------------
out=$("$T3CTL" approve-callbacks --dry-run)
assert_contains "$out" '"requestId":"r1","detail":"t3-notify --thread '"$SEED_OURS"' --status done \\"finished, PR opened\\"","action":"would-accept"' "exact callback would be accepted"
assert_contains "$out" '"requestId":"r2","detail":"rm -rf /data/repo","action":"left-for-human"' "other command left for a human"
assert_contains "$out" '"requestId":"r3".*"action":"left-for-human"' "chained command left for a human"
if grep -q '"requestId":"r4"' <<<"$out"; then fail "foreign thread's callback must not be considered"; fi
out=$("$T3CTL" approve-callbacks)
assert_contains "$out" '"requestId":"r1".*"action":"accepted"' "callback accepted"
resp=$(jq -c '[.[] | select(.type=="runtime-request.respond")]' "$STATE")
[ "$(jq length <<<"$resp")" -eq 1 ] || fail "expected exactly one runtime-request.respond, got: $resp"
assert_contains "$resp" '"requestId":"r1","decision":"accept"' "accept dispatched for r1 only"
ok "approve-callbacks accepts only the exact t3-notify command in [kosmi] threads"

# ---- 7. list / show / search / settle --------------------------------------
# list filters: bare = inbox (unsettled, unarchived); --unsettled alias; --unarchived = settled too; --all = archived too
has()    { grep -q "$2" <<<"$1" || fail "$3 (missing $2)"; }
hasnot() { if grep -q "$2" <<<"$1"; then fail "$3 (unexpected $2)"; fi; }
out=$("$T3CTL" list)
has "$out" "$SEED_OURS" "bare list has the running unsettled thread"
has "$out" "$SEED_OTHER" "bare list has the idle unsettled thread"
hasnot "$out" "$SEED_SETTLED" "bare list is the inbox: settled threads excluded"
hasnot "$out" "$SEED_ARCHIVED" "bare list excludes archived threads"
[ "$("$T3CTL" list --unsettled)" = "$out" ] || fail "list --unsettled must equal the bare list"
out=$("$T3CTL" list --unarchived)
has "$out" "$SEED_OURS" "--unarchived has unsettled threads"
has "$out" "$SEED_SETTLED" "--unarchived adds settled threads"
hasnot "$out" "$SEED_ARCHIVED" "--unarchived excludes archived threads"
assert_contains "$(jq -c "select(.id==\"$SEED_SETTLED\")" <<<"$out")" '"settled":true' "settled flag reported"
out=$("$T3CTL" list --all)
has "$out" "$SEED_SETTLED" "--all has settled threads"
has "$out" "$SEED_ARCHIVED" "--all includes archived threads"
out=$("$T3CTL" list --active)
hasnot "$out" "$SEED_SETTLED" "--active excludes settled idle threads"
if "$T3CTL" list --bogus 2>"$TMP/err"; then fail "unknown list flag must error"; fi
assert_contains "$(cat "$TMP/err")" "unknown flag --bogus" "list flag error"
if "$T3CTL" list --all --active 2>/dev/null; then fail "list must take a single flag"; fi
ok "list: bare = inbox; --unsettled alias; --unarchived adds settled; --all adds archived; --active unchanged"
out=$("$T3CTL" show "$SEED_OURS" -n 2); assert_contains "$out" '"seeded prompt"' "show returns messages"
assert_contains "$out" '"requestId": "r2"' "show still lists approvals (legacy path)"
out=$("$T3CTL" search "seeded"); assert_contains "$out" '"matchedIn":"title"' "search matches titles"
out=$("$T3CTL" search "update the docs"); assert_contains "$out" '"matchedIn":"user"' "search matches message bodies"
out=$("$T3CTL" settle "$SEED_OTHER"); assert_contains "$out" '"op":"settle"' "settle has no ownership guard"
if "$T3CTL" list | grep -q "$SEED_OTHER"; then fail "a settled thread must leave the bare list"; fi
out=$("$T3CTL" unsettle "$SEED_OTHER"); assert_contains "$out" '"op":"unsettle"' "unsettle"
"$T3CTL" list | grep -q "$SEED_OTHER" || fail "an unsettled thread must return to the bare list"
assert_contains "$(jq -c '[.[] | select(.type=="mcp.t3_thread_organize") | .action]' "$STATE")" '"settle","unsettle"' "settle/unsettle go over MCP"
ok "list/show/search/settle"

# ---- 8. project add (idempotent) --------------------------------------------
out=$("$T3CTL" project add "$TMP" --title scratch); assert_contains "$out" '"created":true' "project add creates"
assert_contains "$(jq -c '.[] | select(.type=="project.create")' "$STATE" | tail -1)" "\"workspaceRoot\":\"$TMP\"" "project.create dispatched"
out=$("$T3CTL" project add "$TMP"); assert_contains "$out" '"created":false' "project add is idempotent on workspaceRoot"
out=$("$T3CTL" projects); assert_contains "$out" '"title":"scratch"' "new project listed"
ok "project add"

# ---- 9. project set-model + contract-check ---------------------------------
out=$("$T3CTL" project set-model aikosmo-monorepo claudeAgent:claude-fable-5-1)
assert_contains "$out" '"defaultModelSelection":{"instanceId":"claudeAgent","model":"claude-fable-5-1"}' "set-model output"
meta=$(jq -c '.[] | select(.type=="project.update")' "$STATE" | tail -1)
assert_contains "$meta" "\"projectId\":\"$PID_PROJECT\"" "project.update sent via projects.mutate"
out=$("$T3CTL" new aikosmo-monorepo "uses project default")
assert_contains "$(jq -c '.[] | select(.type=="mcp.t3_thread_launch")' "$STATE" | tail -1)" '"model":"claude-fable-5-1"' "new picks the project default"
out=$("$T3CTL" contract-check); assert_contains "$out" '"ok":true' "contract-check"; assert_contains "$out" '"rpc":true' "contract-check covered the WebSocket RPC"; assert_contains "$out" '"dbSchemaChecked":true' "contract-check covered the run-correlation schema"
jq -e '.mcp.missing == [] and .mcp.required >= 11 and .oauth == true' >/dev/null <<<"$out" || fail "contract-check covers MCP tools and the OAuth sign-in: $out"
ok "project set-model + contract-check"

# ---- 10. t3-token-renew (file mode, as on the box) --------------------------
mkdir -p "$TMP/box"
echo 'T3_VERSION=0.1.6-fake' > "$TMP/box/default-t3"
cat > "$TMP/box/t3ctl.conf" <<EOF
T3CTL_URL=http://127.0.0.1:$PORT
T3CTL_T3_VERSION_FILE=$TMP/box/default-t3
T3CTL_TOKEN_FILE=$TMP/box/t3.token
T3CTL_SESSION_FILE=$TMP/box/t3.session-id
T3CTL_TOKEN_LABEL=hermes-t3ctl
T3CTL_ALLOW_FULL_ACCESS=1
EOF
printf 'old-box-sid\n' > "$TMP/box/t3.session-id"; printf 'old-box-sid-mcp\n' > "$TMP/box/t3.session-id.mcp"
out=$(env -i PATH="$PATH" HOME="$HOME" T3CTL_CONF="$TMP/box/t3ctl.conf" T3CTL_T3_CLI="$TMP/bin/t3" \
  FAKE_T3_TOKENS="$TOKENS" FAKE_T3_MCP_TOKENS="$MCP_TOKENS" FAKE_T3_AUTH_DIR="$TMP/auth" "$ROOT/bin/t3-token-renew" 2>&1) \
  || fail "t3-token-renew failed: $out"
assert_contains "$out" "rotated (legacy session" "renew summary"
[ "$(stat -c %a "$TMP/box/t3.token")" = 600 ] && [ "$(stat -c %a "$TMP/box/t3.token.mcp")" = 600 ] || fail "tokens must be installed 0600"
grep -q "^$(cat "$TMP/box/t3.token.mcp") full-access\$" "$MCP_TOKENS" || fail "box MCP token: full-access ceiling (ALLOW_FULL_ACCESS=1)"
grep -q "^$(cat "$TMP/box/t3.token")\$" "$TOKENS" || fail "box legacy token accepted on the legacy path"
grep -q "revoked old-box-sid\$" "$TMP/auth/revoked" && grep -q "revoked old-box-sid-mcp\$" "$TMP/auth/revoked" || fail "renew revokes both predecessors"
[ -n "$(cat "$TMP/box/t3.session-id.mcp")" ] || fail "renew records the MCP session id for the next rotation"
if grep -qE 'tok-|mcp-[0-9a-f]{12}|PAIR-' <<<"$out"; then fail "t3-token-renew must not print credentials: $out"; fi
out=$(T3CTL_CONF="$TMP/box/t3ctl.conf" T3CTL_TOKEN_MODE=file "$T3CTL" projects); assert_contains "$out" aikosmo-monorepo "the box config works unchanged with the MCP token next to the legacy one"
ok "t3-token-renew: mints, probes, installs (0600) and revokes both; box config needs no new keys"

echo "all $pass checks passed"

python3 "$ROOT/tests/shapes.py" compare "$ROOT/tests/golden/v0.5.0-fake.json" "$ROOT/bin"
python3 -m unittest discover -s "$ROOT/tests" -p 'test_*.py'
